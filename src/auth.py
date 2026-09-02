"""Client authentication: stack resolution and credential verification.

The management API takes one of two credentials, each in the header its kind
belongs in — the same split a Keboola stack itself uses:

  X-StorageApi-Token:    a Keboola Storage API token
  Authorization: Bearer  a programmatic bearer (``kbc_at_*`` session,
                         ``kbc_pat_*`` personal access token)

plus, on every request:

  X-Storage-Stack:       stack alias or full https URL
  X-Storage-Project:     project id — required with a bearer, which is scoped
                         to an admin rather than to one project

A Storage token names its project by itself. A bearer does not: the stack
exchanges it for the admin's own Storage token of the project named in
``X-KBC-ProjectId`` (see ``BearerTokenAuthenticator`` in Connection), so the
hub has to say which project the caller is acting as. Both end at the same
:class:`Owner`, which is why everything downstream is unaware of the
difference.

The ``X-Kbc-*`` spelling is not usable on the public side — the data-app proxy
strips those headers — hence ``X-Storage-Project`` on the way in and
``X-KBC-ProjectId`` only on the way out to a stack.

A stack URL is accepted when it is https and its hostname ends with
``.keboola.com``, or when it is explicitly listed in HUB_EXTRA_STACKS.
"""

import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from src.kbclogin import is_bearer_credential

logger = logging.getLogger(__name__)

STACK_ALIASES: dict[str, str] = {
    "us": "https://connection.keboola.com",
    "aws-us": "https://connection.keboola.com",
    "us-east4": "https://connection.us-east4.gcp.keboola.com",
    "gcp-us": "https://connection.us-east4.gcp.keboola.com",
    "eu": "https://connection.eu-central-1.keboola.com",
    "aws-eu": "https://connection.eu-central-1.keboola.com",
    "azure-eu": "https://connection.north-europe.azure.keboola.com",
    "north-europe": "https://connection.north-europe.azure.keboola.com",
    "gcp-eu": "https://connection.europe-west3.gcp.keboola.com",
    "europe-west3": "https://connection.europe-west3.gcp.keboola.com",
}


class StackError(ValueError):
    """Raised for an unknown or disallowed stack."""


class AuthError(Exception):
    """Raised when token verification fails (401 semantics)."""


class StackUnreachableError(Exception):
    """Raised when the stack cannot be reached (502 semantics)."""


#: ``admin.role`` values a stack has been seen to return. Anything outside
#: this set is treated as a non-admin role: an unrecognized role is schema
#: drift or a new, narrower role, and neither is a reason to hand out
#: destructive authority.
KNOWN_ADMIN_ROLES = frozenset({"admin", "guest", "readOnly", "share"})

#: The one ``admin.role`` that carries project administration.
ADMIN_ROLE = "admin"


@dataclass(frozen=True)
class Owner:
    stack_url: str
    project_id: int
    project_name: str
    # Non-secret, token-level claims from the same verify response
    # (SEC-075-011). The project identity above answers "which project is
    # this?"; these answer "what kind of credential is it?", which is what
    # HUB_DESTRUCTIVE_TOKEN_POLICY needs to decide whether a token may erase
    # things. They are request-scope only, exactly like the raw token: never
    # persisted into an envelope or meta record, never logged, never echoed.
    #
    # Every one of them defaults to the least-privileged value, and parsing
    # never raises: a stack that omits a claim, renames it, or answers with a
    # type nobody expected must degrade a caller to "ordinary token", not fail
    # the request and not accidentally promote them.
    token_id: str | None = None
    is_master_token: bool = False
    admin_role: str | None = None
    can_purge_trash: bool = False
    can_manage_tokens: bool = False

    @property
    def key(self) -> str:
        """Stable owner identifier usable as a Storage File tag."""
        host = urlparse(self.stack_url).hostname or "unknown"
        return f"{self.project_id}@{host}"

    @property
    def is_project_admin(self) -> bool:
        """True when this credential administers the project as a whole.

        A master token is the project's root credential; a user token whose
        ``admin.role`` is ``admin`` belongs to a project administrator. Any
        other role — and any token with no ``admin`` object at all, such as a
        narrowly scoped machine token — is not an administrator.
        """
        return self.is_master_token or self.admin_role == ADMIN_ROLE


def resolve_stack(raw: str, extra_stacks: tuple[str, ...] = ()) -> str:
    """Turn an alias or URL into a normalized allowed stack URL."""
    value = (raw or "").strip().rstrip("/")
    if not value:
        raise StackError("Missing X-Storage-Stack header (alias or https URL)")
    lowered = value.lower()
    if lowered in STACK_ALIASES:
        return STACK_ALIASES[lowered]
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise StackError(
            f"Stack {value!r} not allowed: use an alias "
            f"({', '.join(sorted(set(STACK_ALIASES)))}) or an https URL"
        )
    normalized = f"https://{parsed.hostname}"
    if parsed.hostname.endswith(".keboola.com") or normalized in extra_stacks:
        return normalized
    raise StackError(
        f"Stack {value!r} not allowed: hostname must end with .keboola.com "
        "or be listed in HUB_EXTRA_STACKS"
    )


def storage_headers(token: str, project_id: int | None) -> dict[str, str]:
    """Headers that authenticate one Storage API call as ``token``.

    A bearer is only half a credential — it says who, not where — so the
    project id travels with it on every Storage call, not just on verify.
    """
    if is_bearer_credential(token):
        if project_id is None:
            raise AuthError(
                "This token is a Keboola sign-in credential, so it needs a "
                "project: send X-Storage-Project with the project id"
            )
        return {
            "Authorization": f"Bearer {token}",
            "X-KBC-ProjectId": str(project_id),
        }
    return {"X-StorageApi-Token": token}


def verify_token(
    stack_url: str, token: str, timeout_s: int = 15, project_id: int | None = None
) -> Owner:
    """Verify a credential against its stack and return the owning project.

    ``project_id`` is the ``X-Storage-Project`` header. It is what makes a
    bearer usable at all, and it is *ignored* for a Storage API token, which
    names its own project: a caller who keeps the header set in their
    environment and then hands one call a Storage token of another project is
    describing the credential's project no better and no worse than the token
    itself does, so the token wins.
    """
    if not token:
        raise AuthError("Missing X-StorageApi-Token header")
    bearer = is_bearer_credential(token)
    url = f"{stack_url}/v2/storage/tokens/verify"
    try:
        response = httpx.get(
            url, headers=storage_headers(token, project_id), timeout=timeout_s
        )
    except httpx.HTTPError as exc:
        logger.warning("Stack %s unreachable: %s", stack_url, exc)
        raise StackUnreachableError(f"Could not reach {stack_url}: {exc}") from exc
    if response.status_code in (401, 403):
        if bearer:
            raise AuthError(
                f"The stack refused this sign-in for project {project_id} — "
                "the session may have expired, or it does not reach that project"
            )
        raise AuthError("Storage token rejected by the stack")
    if response.status_code != 200:
        raise StackUnreachableError(
            f"Unexpected {response.status_code} from {stack_url}"
        )
    # A 200 is not a promise of a well-formed body: a captive portal, a
    # misrouted proxy or a schema drift can all answer 200 with something else
    # entirely. Validate the shape before indexing into it, so a surprise
    # becomes a stable AuthError/StackUnreachableError instead of a 500.
    try:
        data = response.json()
    except ValueError as exc:
        logger.warning("Non-JSON token-verify response from %s: %s", stack_url, exc)
        raise StackUnreachableError(
            f"{stack_url} answered 200 with a non-JSON token-verify body"
        ) from exc
    if not isinstance(data, dict):
        raise AuthError("Token verify response is not a JSON object")
    owner = data.get("owner")
    if not isinstance(owner, dict):
        raise AuthError("Token verify response has no owner project")
    admin = data.get("admin")
    resolved_id = _owner_project_id(owner.get("id"))
    # A bearer is exchanged for a Storage token *of the requested project*, so
    # a different owner coming back means the request was routed somewhere
    # other than where the caller said. Refuse rather than record an identity
    # the caller did not ask for. A Storage token is not checked against the
    # header: it was never routed by it (see the docstring).
    if bearer and project_id is not None and resolved_id != project_id:
        raise AuthError(
            f"Stack resolved project {resolved_id} for a credential presented "
            f"as project {project_id}"
        )
    return Owner(
        stack_url=stack_url,
        project_id=resolved_id,
        project_name=_owner_project_name(owner.get("name")),
        # SEC-075-011: the token-level claims. Unlike the project identity
        # above, none of these can fail the request — see the Owner docstring.
        token_id=_claim_token_id(data.get("id")),
        is_master_token=_claim_flag(data.get("isMasterToken")),
        admin_role=_claim_admin_role(admin),
        can_purge_trash=_claim_flag(data.get("canPurgeTrash")),
        can_manage_tokens=_claim_flag(data.get("canManageTokens")),
    )


def _claim_flag(raw: object) -> bool:
    """A boolean capability claim, defaulting to False for anything unusual.

    Only a real JSON ``true`` grants the flag. A stack that spells the value
    ``"true"``, ``1`` or anything else is answering in a shape this code has
    not verified, and the safe reading of an unverified shape is "no".
    """
    return raw is True


def _claim_token_id(raw: object) -> str | None:
    """The token's own id as a string, or None when absent or unusable.

    Some stacks serialize the id as a number, so an int is normalized to its
    decimal string — that is the form an operator copies into
    HUB_DESTRUCTIVE_TOKEN_IDS. The id is an identifier, never a secret, but it
    is still request-scope: it is matched against the allowlist and dropped.
    """
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return str(raw)
    if isinstance(raw, str):
        value = raw.strip()
        return value or None
    return None


def _claim_admin_role(raw: object) -> str | None:
    """The project user's role, or None when the token is not a user token.

    An unknown role string is deliberately *kept* rather than discarded: it is
    reported as-is so the value is visible, and because only the exact string
    ``admin`` grants authority (see :attr:`Owner.is_project_admin`), an
    unrecognized role is inert. Non-``admin`` values, including a role this
    version has never heard of, are simply not administrators.
    """
    if not isinstance(raw, dict):
        return None
    role = raw.get("role")
    if not isinstance(role, str):
        return None
    role = role.strip()
    if not role:
        return None
    if role not in KNOWN_ADMIN_ROLES:
        # Worth knowing about (a stack grew a role this version predates), but
        # never worth failing on: an unknown role is not ADMIN_ROLE, so it
        # already grants nothing. The role name is not a secret; the token is
        # not named here.
        logger.info("Unrecognized project admin role %r in token verify", role)
    return role


def _owner_project_id(raw: object) -> int:
    """The owner project id as an int, or ``AuthError`` when unusable.

    Keboola stacks return a JSON number, but some return the id as a decimal
    string; both are accepted. Anything else (missing, bool, list, dict,
    non-numeric string) is an authentication failure, never a crash.
    """
    if isinstance(raw, bool) or raw is None:
        raise AuthError("Token verify response has no owner project")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError as exc:
            raise AuthError(
                "Token verify response has a non-numeric owner project id"
            ) from exc
    raise AuthError("Token verify response has a non-numeric owner project id")


def _owner_project_name(raw: object) -> str:
    """The owner project name, normalized to a string ("" when absent)."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return str(raw)
    # A structured name is schema drift, not a name — do not stringify a dict
    # or list into the identity we hand every caller.
    raise AuthError("Token verify response has a non-scalar owner project name")
