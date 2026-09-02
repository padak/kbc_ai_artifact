"""Interactive Keboola login: device authorization and PKCE.

A caller who has no Storage API token at hand can obtain a programmatic
session by signing in with their browser, on any stack the hub allows. Both
shapes Keboola Connection offers are implemented here:

* **Device authorization** (``POST /v1/auth/device`` then
  ``POST /v1/auth/device/token``) — the browser and the client need not be on
  the same machine, so this is the only flow a hosted hub can offer.
* **Authorization code + PKCE** (``GET /admin/auth/pkce/authorize`` then
  ``POST /v1/auth/pkce/token``) — one browser hop, nothing to type, but the
  stack accepts only an ``http://127.0.0.1:{port}/{path}`` (or ``[::1]``)
  redirect URI, so it is reachable only when the hub itself runs on loopback.

Both end at the same credential: an opaque ``kbc_at_*`` bearer plus a
``kbc_rt_*`` refresh token. On ``/v2/storage/*`` the stack exchanges that
bearer for the admin's own Storage token of the project named in
``X-KBC-ProjectId``, which is why :mod:`src.auth` and :mod:`src.kbc` can treat
it as one more way to say "this project's token".

Nothing here persists a credential. Every function takes what it needs as an
argument and hands the result back to the caller; the hub relays it to the
browser that started the login and forgets it.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

__all__ = [
    "ACCESS_TOKEN_PREFIX",
    "PAT_PREFIX",
    "Credential",
    "DeviceStart",
    "LoginError",
    "LoginPending",
    "LoginRejected",
    "LoginUnavailable",
    "PendingPkce",
    "PkceRegistry",
    "ProjectAccess",
    "SessionInfo",
    "authorize_url",
    "exchange_pkce",
    "introspect",
    "is_bearer_credential",
    "new_pkce_verifier",
    "new_state",
    "pkce_challenge",
    "poll_device",
    "refresh_credential",
    "revoke",
    "start_device",
]

#: Prefix of a programmatic-session access token (``KbcAccessTokenParser``).
ACCESS_TOKEN_PREFIX = "kbc_at_"
#: Prefix of a personal access token (``KbcPatTokenParser``). Authenticates the
#: same way as a session bearer, so the hub accepts it wherever one is accepted.
PAT_PREFIX = "kbc_pat_"

#: PKCE code verifier length in random bytes. base64url of 32 bytes is 43
#: characters, the minimum RFC 7636 (and the stack's validator) accepts.
_VERIFIER_BYTES = 32
#: ``state`` entropy in random bytes; base64url of 24 bytes is 32 characters,
#: comfortably inside the stack's 22..512 window.
_STATE_BYTES = 24

#: Device-flow error tokens that mean "keep polling" rather than "give up".
_PENDING_ERRORS = frozenset({"authorization_pending", "slow_down"})

#: Terminal error tokens that describe what a person did, not a fault of the
#: stack or of this service. Logged at INFO so a wall of WARNINGs means
#: something is actually wrong.
_USER_OUTCOMES = frozenset({"access_denied", "expired_token"})

#: How much of a stack's error token reaches a log line. It is a fixed enum in
#: practice, but it is parsed out of a response an intermediary may have
#: written, so it is bounded like any other untrusted string.
_MAX_LOGGED_ERROR_CHARS = 64


class LoginError(Exception):
    """Base class for every failure of an interactive login."""


class LoginUnavailable(LoginError):
    """The stack does not offer this flow, or could not be reached.

    A stack with the ``programmatic-auth`` / ``device-authorization-flow`` /
    ``pkce-authorization-flow`` features off answers 404, which is a
    configuration fact about that stack rather than a fault of the request.
    """


class LoginPending(LoginError):
    """The user has not finished approving yet; poll again after ``interval``."""

    def __init__(self, interval: int, slow_down: bool = False) -> None:
        super().__init__("Authorization is still pending.")
        self.interval = interval
        self.slow_down = slow_down


class LoginRejected(LoginError):
    """Terminal refusal: denied, expired, replayed, or malformed."""

    def __init__(self, message: str, error: str = "") -> None:
        super().__init__(message)
        #: The stack's RFC-style error token, when it sent one.
        self.error = error


@dataclass(frozen=True)
class Credential:
    """A programmatic session as the stack issued it."""

    stack_url: str
    access_token: str
    refresh_token: str
    expires_in: int
    session_id: str
    user_id: int
    user_email: str
    user_name: str


@dataclass(frozen=True)
class DeviceStart:
    """What the device flow needs the user to do, and how to wait for it."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class ProjectAccess:
    """One project a session may act on."""

    id: int
    name: str
    role: str


@dataclass(frozen=True)
class SessionInfo:
    """Introspection of a live session: who it is, and what it can reach."""

    session_id: str
    user_email: str
    user_name: str
    expires_at: str
    projects: tuple[ProjectAccess, ...]


def is_bearer_credential(token: str) -> bool:
    """True when ``token`` is a programmatic bearer rather than a Storage token."""
    return token.startswith(ACCESS_TOKEN_PREFIX) or token.startswith(PAT_PREFIX)


# --------------------------------------------------------------------------
# Device authorization
# --------------------------------------------------------------------------


def start_device(stack_url: str, client_id: str, timeout_s: int) -> DeviceStart:
    """Open a device authorization and return the codes the user needs."""
    payload = _post(
        stack_url,
        "/v1/auth/device",
        {"clientId": client_id, "scope": {"credentialType": "session"}},
        timeout_s,
    )
    try:
        return DeviceStart(
            device_code=str(payload["deviceCode"]),
            user_code=str(payload["userCode"]),
            verification_uri=str(payload["verificationUri"]),
            verification_uri_complete=str(payload["verificationUriComplete"]),
            expires_in=int(payload["expiresIn"]),
            interval=int(payload["interval"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LoginUnavailable(
            f"{stack_url} answered a device authorization without the "
            "fields the flow needs"
        ) from exc


def poll_device(
    stack_url: str, client_id: str, device_code: str, timeout_s: int
) -> Credential:
    """Ask once whether the device authorization has been approved.

    Raises :class:`LoginPending` while the user is still in the browser — the
    caller decides how long to keep waiting, so nothing here sleeps.
    """
    payload = _post(
        stack_url,
        "/v1/auth/device/token",
        {"clientId": client_id, "deviceCode": device_code},
        timeout_s,
    )
    return _credential(stack_url, payload)


# --------------------------------------------------------------------------
# Authorization code + PKCE
# --------------------------------------------------------------------------


def new_pkce_verifier() -> str:
    """A fresh PKCE code verifier (43 URL-safe characters)."""
    return _b64url(secrets.token_bytes(_VERIFIER_BYTES))


def pkce_challenge(verifier: str) -> str:
    """The S256 challenge for ``verifier``."""
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def new_state() -> str:
    """A fresh opaque ``state`` value binding a callback to its request."""
    return _b64url(secrets.token_bytes(_STATE_BYTES))


def authorize_url(
    stack_url: str,
    client_id: str,
    redirect_uri: str,
    challenge: str,
    state: str,
    pick_project: bool = True,
) -> str:
    """Build the browser URL that asks the stack to authorize this client.

    With ``pick_project`` the stack shows its own project picker and narrows
    the issued session to what the admin selects there; otherwise the session
    covers every project the admin is a member of.

    Narrowing is the default because of where the credential ends up: a
    browser tab. What a session may reach is decided on the stack's screen and
    nowhere else — this hub's own project step only chooses which of the
    reachable projects a call acts as, and is not a boundary.
    """
    query = httpx.QueryParams(
        {
            "responseType": "code",
            "clientId": client_id,
            "redirectUri": redirect_uri,
            "codeChallenge": challenge,
            "codeChallengeMethod": "S256",
            "state": state,
            "projectScope": "selected" if pick_project else "all",
        }
    )
    return f"{stack_url.rstrip('/')}/admin/auth/pkce/authorize?{query}"


@dataclass(frozen=True)
class PendingPkce:
    """A PKCE login that has been started and is waiting for its callback."""

    state: str
    verifier: str
    stack_url: str
    redirect_uri: str
    started_at: float


class PkceRegistry:
    """The PKCE logins this process started, keyed by ``state``.

    The verifier is the whole proof of possession, so it stays on the server:
    it is never written to a cookie, a URL, or the page. An entry is consumed
    on first use, which is also what makes a replayed callback fail.

    Process-local by design. PKCE is only ever offered when the hub answers on
    a loopback origin — the stack refuses any other redirect URI — and a
    loopback hub is one process serving one developer, so there is no worker
    to share this with.
    """

    def __init__(self, ttl_s: int, max_pending: int) -> None:
        self._ttl_s = ttl_s
        self._max_pending = max_pending
        self._lock = threading.Lock()
        self._pending: dict[str, PendingPkce] = {}

    def start(self, stack_url: str, redirect_uri: str) -> PendingPkce:
        """Mint a state/verifier pair and remember it until its callback."""
        entry = PendingPkce(
            state=new_state(),
            verifier=new_pkce_verifier(),
            stack_url=stack_url,
            redirect_uri=redirect_uri,
            started_at=time.monotonic(),
        )
        with self._lock:
            self._expire()
            # Oldest-first eviction: a flood of abandoned tabs must not push
            # out the login the user is actually finishing right now, and the
            # one they are finishing is the newest.
            while len(self._pending) >= self._max_pending:
                oldest = min(self._pending.values(), key=lambda p: p.started_at)
                del self._pending[oldest.state]
            self._pending[entry.state] = entry
        return entry

    def take(self, state: str) -> PendingPkce | None:
        """Consume the entry for ``state``, or None when there is none left."""
        with self._lock:
            self._expire()
            return self._pending.pop(state, None)

    def _expire(self) -> None:
        """Drop entries past their TTL. Callers hold the lock."""
        cutoff = time.monotonic() - self._ttl_s
        for state in [s for s, p in self._pending.items() if p.started_at < cutoff]:
            del self._pending[state]


def exchange_pkce(
    stack_url: str,
    client_id: str,
    code: str,
    redirect_uri: str,
    state: str,
    verifier: str,
    timeout_s: int,
) -> Credential:
    """Trade an authorization code plus its verifier for a session."""
    payload = _post(
        stack_url,
        "/v1/auth/pkce/token",
        {
            "clientId": client_id,
            "code": code,
            "redirectUri": redirect_uri,
            "state": state,
            "codeVerifier": verifier,
        },
        timeout_s,
    )
    return _credential(stack_url, payload)


# --------------------------------------------------------------------------
# Session lifecycle
# --------------------------------------------------------------------------


def introspect(stack_url: str, access_token: str, timeout_s: int) -> SessionInfo:
    """Describe a live session, including the projects it may act on."""
    payload = _request(
        "GET",
        stack_url,
        "/v1/auth/token/introspect",
        None,
        timeout_s,
        headers={"Authorization": f"Bearer {access_token}"},
        secret=access_token,
    )
    if not isinstance(payload, dict):
        raise LoginRejected("Token introspection returned no session.")
    user = payload.get("user")
    user = user if isinstance(user, dict) else {}
    return SessionInfo(
        session_id=str(payload.get("sessionId") or ""),
        user_email=str(user.get("email") or ""),
        user_name=str(user.get("name") or ""),
        expires_at=str(payload.get("expiresAt") or ""),
        projects=_projects(payload.get("projects")),
    )


def refresh_credential(
    stack_url: str, refresh_token: str, timeout_s: int
) -> Credential:
    """Rotate a refresh token into a fresh access/refresh pair."""
    payload = _post(
        stack_url, "/v1/auth/token/refresh", {"refreshToken": refresh_token}, timeout_s
    )
    return _credential(stack_url, payload)


def revoke(stack_url: str, token: str, timeout_s: int) -> None:
    """Revoke a session by its access or refresh token.

    A revocation that the stack refuses is logged and swallowed: the caller is
    signing out, and a credential the stack will not revoke is one it has
    already forgotten (or one it never knew).
    """
    try:
        _post(stack_url, "/v1/auth/token/revoke", {"token": token}, timeout_s)
    except LoginError as exc:
        logger.info("Sign-out revocation on %s did not take: %s", stack_url, exc)


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _b64url(raw: bytes) -> str:
    """base64url without padding, as every PKCE value is encoded."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _credential(stack_url: str, payload: Any) -> Credential:
    """Map a ``CliTokenResponse``/``LoginResponse`` body to a credential."""
    if not isinstance(payload, dict):
        raise LoginRejected("The stack returned no session.")
    user = payload.get("user")
    user = user if isinstance(user, dict) else {}
    try:
        access_token = str(payload["accessToken"])
        refresh_token = str(payload["refreshToken"])
    except KeyError as exc:
        raise LoginRejected("The stack returned a session without a token.") from exc
    if not access_token or not refresh_token:
        raise LoginRejected("The stack returned an empty session token.")
    try:
        expires_in = int(payload.get("expiresIn") or 0)
    except (TypeError, ValueError):
        expires_in = 0
    try:
        user_id = int(user.get("id") or 0)
    except (TypeError, ValueError):
        user_id = 0
    return Credential(
        stack_url=stack_url,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        session_id=str(payload.get("sessionId") or ""),
        user_id=user_id,
        user_email=str(user.get("email") or ""),
        user_name=str(user.get("name") or ""),
    )


def _projects(raw: Any) -> tuple[ProjectAccess, ...]:
    """Map the introspection ``projects`` array, skipping unusable entries.

    A frozen PAT scope may name a project that no longer exists (``name`` and
    ``role`` null); such an entry cannot be picked, so it is dropped rather
    than offered.
    """
    if not isinstance(raw, list):
        return ()
    found: list[ProjectAccess] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            project_id = int(entry["id"])
        except (KeyError, TypeError, ValueError):
            continue
        name = entry.get("name")
        role = entry.get("role")
        if not isinstance(name, str) or not isinstance(role, str):
            continue
        found.append(ProjectAccess(id=project_id, name=name, role=role))
    return tuple(found)


def _post(
    stack_url: str, path: str, body: dict[str, Any], timeout_s: int
) -> Any:
    """POST a JSON body to one of the stack's auth endpoints."""
    return _request("POST", stack_url, path, body, timeout_s)


def _request(
    method: str,
    stack_url: str,
    path: str,
    body: dict[str, Any] | None,
    timeout_s: int,
    headers: dict[str, str] | None = None,
    secret: str = "",
) -> Any:
    """Call one auth endpoint and normalize every failure into a LoginError.

    ``secret`` names a credential that must never reach a log line or an
    exception message; the transport error text is scrubbed of it before it
    goes anywhere.
    """
    url = f"{stack_url.rstrip('/')}{path}"
    try:
        response = httpx.request(
            method, url, json=body, headers=headers, timeout=timeout_s
        )
    except httpx.HTTPError as exc:
        detail = _redact(f"{exc}", secret)
        logger.warning("Auth call %s %s failed: %s", method, path, detail)
        raise LoginUnavailable(f"Could not reach {stack_url}: {detail}") from exc

    if response.status_code == 404:
        logger.warning(
            "Auth call %s %s on %s: 404, the stack does not offer this flow",
            method, path, stack_url,
        )
        raise LoginUnavailable(
            f"{stack_url} does not offer this sign-in method "
            "(programmatic auth is off on that stack)"
        )
    if response.status_code in (200, 201):
        try:
            return response.json()
        except ValueError as exc:
            logger.warning(
                "Auth call %s %s on %s answered %s with a non-JSON body",
                method, path, stack_url, response.status_code,
            )
            raise LoginUnavailable(
                f"{stack_url} answered {response.status_code} with a non-JSON body"
            ) from exc

    _raise_for_error(stack_url, path, method, response)


def _raise_for_error(
    stack_url: str, path: str, method: str, response: httpx.Response
) -> None:
    """Translate a non-2xx auth response into the right LoginError.

    Everything except a still-pending poll is logged. Without that, a stack
    that starts refusing every sign-in — a 5xx on its auth service, a WAF in
    front of it, a client id it no longer accepts — reaches the operator as
    nothing but a bare 400 in the access log, indistinguishable from one
    person declining in their browser. A pending poll is deliberately silent:
    it is the normal case and it repeats every few seconds.

    The stack's error *token* is logged, never its body: the body of a
    non-2xx from an intermediary is arbitrary text nobody here has vetted.
    """
    payload: Any = None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    error = ""
    interval = 0
    if isinstance(payload, dict):
        params = payload.get("params")
        if isinstance(params, dict):
            error = str(params.get("error") or "")
            try:
                interval = int(params.get("interval") or 0)
            except (TypeError, ValueError):
                interval = 0
        if not error:
            error = str(payload.get("error") or "")
    if error in _PENDING_ERRORS:
        raise LoginPending(interval=interval, slow_down=error == "slow_down")
    # Terminal refusals split by whose problem they are: a decline or an
    # expiry is a person's own outcome, anything else is the stack's.
    level = logger.info if error in _USER_OUTCOMES else logger.warning
    level(
        "Auth call %s %s on %s: %s%s",
        method, path, stack_url, response.status_code,
        f" ({error[:_MAX_LOGGED_ERROR_CHARS]})" if error else "",
    )
    if response.status_code == 429:
        raise LoginRejected(
            "The stack is rate-limiting sign-in attempts. Try again in a minute.",
            error=error or "rate_limited",
        )
    if response.status_code >= 500:
        raise LoginUnavailable(
            f"{stack_url} answered {response.status_code} to a sign-in call"
        )
    raise LoginRejected(_rejection_message(error), error=error)


def _rejection_message(error: str) -> str:
    """A message for a terminal refusal, phrased for the person signing in.

    Only the stack's fixed error token selects the text — a response body is
    never echoed back, so nothing a remote host writes can reach the browser.
    """
    return {
        "access_denied": "The sign-in was declined in the browser.",
        "expired_token": "The sign-in expired before it was approved. Start again.",
        "invalid_grant": "The sign-in could not be completed. Start again.",
        "invalid_request": "The sign-in request was rejected by the stack.",
        "invalid_client": "The stack refused this client. Start again.",
    }.get(error, "The stack refused the sign-in. Start again.")


def _redact(text: str, secret: str) -> str:
    """Remove ``secret`` from ``text`` so it cannot land in a log or a message."""
    return text.replace(secret, "<redacted>") if secret else text
