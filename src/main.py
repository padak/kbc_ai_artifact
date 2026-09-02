"""FastAPI wiring for the KBC Artifact Hub.

Read path (public, unauthenticated): ``/a/{id}`` and friends serve artifacts
straight from the serving store. Write path (``/api/artifacts``): authenticated
with the caller's own Keboola Storage token, which is used once — to store the
canonical copy in the caller's project — and never persisted.

Since phase 2 an artifact is a *series of versions* plus an artifact-level meta
record. ``/a/{id}`` serves the head version (newest live, or a pinned one).
Owners add live versions; other projects may submit **proposals** when the owner
opted in with ``accept_versions`` — a proposal is readable only by the owner or
its author until the owner promotes it.

Two documents and one page are served straight from the repository: ``/skill``
and ``/agent`` hand an agent its instructions, and ``/admin`` is a completely
client-side moderation studio — a static HTML page whose JavaScript drives this
same API with a token the visitor pastes into their own browser, so the server
never sees a studio session.

Startup is deliberately tolerant: if Storage is unreachable while hydrating the
index, the process still boots and retries hydration on the next request, so a
transient Storage outage cannot put the app into a crash loop.
"""

from __future__ import annotations

import dataclasses
import fcntl
import functools
import hashlib
import ipaddress
import json
import logging
import os
import re
import sys
import threading
import tomllib
from collections import OrderedDict
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.openapi.utils import get_openapi
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from src import builder, export
from src.auth import (
    STACK_ALIASES,
    AuthError,
    Owner,
    StackError,
    StackUnreachableError,
    resolve_stack,
    verify_token,
)
from src.builder import BuildError, BuiltArtifact
from src.comments import (
    MAX_BODY_CHARS,
    MAX_QUOTE_CHARS,
    MAX_REPLIES_PER_THREAD,
    MAX_THREAD_BYTES,
    CommentStore,
    CommentThread,
    Reply,
    Selector,
    author_key_of,
    guest_author,
)
from src.config import Settings, load_settings
from src.diff import DiffError, compute_diff
from src.kbc import BackendError, KbcFilesBackend
from src.kbclogin import (
    Credential,
    LoginError,
    LoginPending,
    LoginUnavailable,
    PkceRegistry,
    authorize_url,
    exchange_pkce,
    introspect,
    is_bearer_credential,
    pkce_challenge,
    poll_device,
    refresh_credential,
    revoke,
    start_device,
)
from src.pages import (
    admin_page,
    artifact_frame_page,
    changelog_page,
    landing_page,
    login_page,
    review_page,
    unlock_page,
    versions_page,
    visual_diff_page,
)
from src.security import (
    KEY_LABEL_UNLOCK_COOKIE,
    KEY_LABEL_WEBHOOK,
    CookieSigner,
    check_password,
    derive_key,
    hash_password,
    new_artifact_id,
)
from src.statedb import StateDB, foreign_writer_detected
from src.store import (
    ACCEPT_ALLOWLIST,
    ACCEPT_ANYONE,
    ACCEPT_MODES,
    ACCEPT_OFF,
    ARTIFACT_DRAFT,
    ARTIFACT_FINAL,
    ARTIFACT_SETTABLE_STATUSES,
    ARTIFACT_TRASHED,
    COMMENTS_ALLOWLIST,
    COMMENTS_ANYONE,
    COMMENTS_MODES,
    COMMENTS_OFF,
    HEAD_LATEST,
    HEAD_PINNED,
    STATUS_LIVE,
    STATUS_PROPOSED,
    ArtifactMeta,
    ArtifactStore,
    Envelope,
    tag_for_id,
)
from src.webhooks import (
    WebhookDispatcher,
    WebhookEvent,
    mint_key_epoch,
    receiver_id_for,
    validate_webhook_url,
)

SERVICE_NAME = "kbc-artifact-hub"

#: Public source repository, surfaced on the landing page and in /context.
GITHUB_REPO_URL = "https://github.com/padak/kbc_ai_artifact"

#: Storage tags put on the canonical copy in the author's own project.
CANONICAL_TAG = "kbc-artifact"

#: Path of the agent-facing skill document, relative to the repository root.
SKILL_PATH = (
    Path(__file__).resolve().parent.parent / "skills/artifact-publisher/SKILL.md"
)

#: Path of the ready-to-install Claude Code subagent definition, served at
#: ``/agent``. Resolved exactly like :data:`SKILL_PATH`.
AGENT_PATH = (
    Path(__file__).resolve().parent.parent / "skills/artifact-hub-agent/AGENT.md"
)

#: Path of the repository changelog, served at ``/changelog`` (rendered) and
#: ``/changelog.md`` (raw). Resolved exactly like :data:`SKILL_PATH`. Read
#: fresh from disk on every request rather than cached at import, since it is
#: expected to be rewritten by other tooling while this process is running.
CHANGELOG_PATH = Path(__file__).resolve().parent.parent / "CHANGELOG.md"

#: ``/a/{id}/diff/{spec}`` accepts exactly ``<older>..<newer>``.
_DIFF_SPEC = re.compile(r"^(\d+)\.\.(\d+)$")

#: Longest contributor note accepted on a submitted version.
MAX_NOTE_CHARS = 500

#: Largest contributor allowlist an owner may set on one artifact.
MAX_CONTRIBUTORS = 50

#: Shape of an owner key: ``{project_id}@{stack hostname}`` (see
#: :meth:`src.auth.Owner.key`). Used to validate the contributor allowlist so a
#: typo becomes a 422 instead of an entry that can never match anybody.
_CONTRIBUTOR_KEY = re.compile(r"^[0-9]+@[A-Za-z0-9._-]+$")

#: Longest display name an invitation may carry.
MAX_INVITATION_NAME_CHARS = 80

#: Header carrying a guest's invitation credential, ``{invitation_id}.{secret}``.
#: The secret half rides the *fragment* of the review URL and is only ever put
#: in this header — never in a path, a query string or a cookie — so it stays
#: out of access logs, referrers and browser history on the server side.
GUEST_HEADER = "X-Artifact-Guest"

# --------------------------------------------------------------------------
# OpenAPI documentation constants
#
# Every route documents its parameters through these, so one wording change
# stays one edit and no operation can quietly ship an undescribed parameter.
# --------------------------------------------------------------------------

#: Description attached to every ``artifact_id`` path parameter of an
#: authenticated ``/api/*`` route: the *internal* handle, which never changes.
ARTIFACT_ID_DESC = (
    "Internal artifact identifier from the publish response ('id'). Stable for "
    "the life of the artifact and unaffected by link rotation, so it stays the "
    "handle for every authenticated operation. Unguessable by design; there is "
    "no public listing."
)

#: Description attached to the identifier in every public ``/a/{...}`` route.
#: The parameter is still *named* ``artifact_id`` (it identifies an artifact,
#: and renaming it would churn every documented path), but what belongs there
#: is the **share id**.
SHARE_ID_DESC = (
    "Public share identifier of the artifact — the capability part of the URL, "
    "as returned in 'share_id' and in every '*_url' field. Equal to the "
    "internal artifact id until the owner rotates the link with POST "
    "/api/artifacts/{id}/rotate-link, after which only the new share id "
    "resolves here and the old one (and the bare artifact id) answer 404. "
    "Unguessable by design; there is no public listing."
)

#: Description attached to every ``version`` path parameter.
VERSION_DESC = (
    "Version number as listed by GET /a/{id}/versions (1 for the first "
    "published version, counting up; numbers are never reused)."
)

#: Description attached to every ``thread_id`` path parameter.
THREAD_ID_DESC = (
    "Comment thread identifier, as returned by POST "
    "/api/artifacts/{id}/comments and listed by GET /a/{id}/comments."
)

#: Description of the identifier in the path of a comment *write*. These four
#: routes are the only ones that accept either half of the identity pair,
#: because the review UI and everyone holding a capability URL know an artifact
#: by its share id, while agents and owners address it by the internal one.
COMMENT_TARGET_ID_DESC = (
    "Either identifier of the artifact: the public share id that appears in "
    "its /a/{...} URLs, or — for the artifact's own owner, authenticated with "
    "a Storage token — the internal id from the publish response. The share id "
    "is resolved first and exactly as every other public path resolves one: a "
    "share id that has been rotated away no longer works, and neither does the "
    "bare internal id of an artifact whose link was rotated, except for that "
    "owner. Everyone else gets 404 for a revoked identifier, so rotating the "
    "link revokes comment writes too."
)

#: Description attached to every ``invitation_id`` path parameter.
INVITATION_ID_DESC = (
    "Invitation identifier, as returned by POST "
    "/api/artifacts/{id}/invitations and listed by GET "
    "/api/artifacts/{id}/invitations. This is the half of the invitation "
    "credential that is *not* secret."
)

#: Description of the ``spec`` path parameter of the diff endpoint.
SPEC_DESC = (
    "Two version numbers in the form OLD..NEW, for example 1..2. OLD must be "
    "strictly smaller than NEW -- every rendering labels the two sides that "
    "way, so a reversed or repeated spec is a 400, as is anything else."
)

#: Sentence appended to the description of reads that honour the password gate.
PASSWORD_GATE_NOTE = (
    "When the artifact is password-protected, machine clients send the "
    "password in the X-Artifact-Password header (Swagger UI cannot send it "
    "from this page); browsers use the unlock form at POST /a/{id}/unlock, "
    "which sets a signed cookie scoped to the artifact path and to the "
    "current password. Failed attempts are rate-limited per artifact and "
    "client address, so a wrong password may answer 429 instead of 401."
)

#: Password paragraph for GET /a/{id}/review only.
#:
#: The shared PASSWORD_GATE_NOTE promises browsers the standalone unlock form.
#: The review route deliberately does the opposite (see read_review): it serves
#: its credential-free shell with 200 and lets the page ask for the password in
#: place, so that navigating away cannot drop the URL fragment carrying an
#: invited guest's credential.
REVIEW_PASSWORD_NOTE = (
    "When the artifact is password-protected this route still answers 200. "
    "The shell it returns carries no artifact content and no credential of "
    "its own -- everything it displays comes from /raw, /versions and "
    "/comments, which stay gated -- so the page renders locked and asks for "
    "the password in its own panel rather than navigating to the standalone "
    "unlock form, which would drop the '#invite=...' fragment an invited "
    "guest arrives with. Machine clients send the password in the "
    "X-Artifact-Password header on the gated endpoints. Failed attempts are "
    "rate-limited per artifact and client address, so a wrong password may "
    "answer 429."
)

#: Paragraph appended to every public route that describes an artifact.
#:
#: An artifact carries two independent notions of "status" and they are easy to
#: confuse, so every payload that names one says which it is: a *version's*
#: status is 'live' or 'proposed', while the *document's* status is 'draft' or
#: 'final'. The document status is always reported under the unambiguous
#: 'document_status' key, on every endpoint.
STATUS_VS_DOCUMENT_STATUS_NOTE = (
    "**Two kinds of status.** A *version* has a status of 'live' or "
    "'proposed' (whether that version is served or is still a proposal); the "
    "*document* has a status of 'draft' or 'final' ('final' freezes new "
    "versions and new comments). The document's status is always reported "
    "under 'document_status', so it can be read the same way on every "
    "endpoint, and 'contributions_frozen' is the derived answer to \"may I "
    "still contribute?\" — true when the document is final (or trashed). "
    "'accept_versions'/'accept_versions_mode' remain the owner's raw setting "
    "and are not rewritten by the freeze, so check 'contributions_frozen' "
    "before submitting a version or a comment."
)

#: Paragraph appended to every comment-write route: the guest alternative.
GUEST_WRITE_NOTE = (
    "**Guests.** Instead of a Storage token, this route also accepts a guest "
    "invitation in the X-Artifact-Guest header, shaped "
    "'{invitation_id}.{secret}' — what the #invite= fragment of an invitation "
    "review URL carries. A guest is one invited human without a Keboola "
    "account: they may open threads, reply, and resolve or delete threads "
    "they opened themselves, and nothing else. 'comments_mode' does not gate "
    "them (the invitation is the grant, and the owner issued it), but a final "
    "or trashed artifact freezes them like everybody else, and they draw on "
    "the same daily comment budget, counted per invitation. Their comments "
    "are published as {'kind': 'guest', 'name': ...} — the invitation id "
    "never appears in a public response."
)

#: Paragraph appended to every comment-write route: which id the path takes.
COMMENT_TARGET_NOTE = (
    "**Either identifier works in the path** on this route, unlike the rest of "
    "/api/*: the public share id that /a/{...} URLs carry (resolved first) or "
    "the internal artifact id. Capability-URL holders, the review UI and "
    "invited guests only ever saw the share id, so refusing it here would "
    "make them unable to address the artifact they are looking at. The share "
    "id is resolved with the same rules as every public path, so rotating the "
    "link (POST /api/artifacts/{id}/rotate-link) revokes writes through the "
    "old link as well as reads; the internal id keeps working only for the "
    "artifact's own owner, authenticated with a Storage token."
)

#: Sentence appended to every comment-write route: the reader password applies.
COMMENT_PASSWORD_NOTE = (
    "**Password-protected artifacts.** The discussion is part of the "
    "protected document, so a write is gated exactly like a read: send the "
    "X-Artifact-Password header (or hold the unlock cookie from POST "
    "/a/{id}/unlock), or the answer is 401. This holds for guests too — an "
    "invitation is a grant to comment, never a way around the reader password "
    "— and for the owner, who reads through the same gate."
)

#: Reused ``responses`` entries, so the same failure never gets two wordings.
RESP_STACK_400 = {"description": "Unknown or disallowed X-Storage-Stack value."}
RESP_COMMENT_401 = {
    "description": (
        "Storage token missing or rejected by the stack — or, when an "
        "X-Artifact-Guest header was sent, an invitation that is unknown to "
        "this artifact, revoked, or whose secret does not verify (the three "
        "guest cases are deliberately indistinguishable) — or the artifact is "
        "password-protected and no valid X-Artifact-Password header or unlock "
        "cookie came with the request."
    )
}
RESP_COMMENTS_429 = {
    "description": (
        "A budget for this artifact is spent: either this project or this "
        "invitation reached HUB_MAX_COMMENTS_PER_DAY comments and replies on "
        "it today, or this client address made "
        "HUB_MAX_UNLOCK_ATTEMPTS_PER_HOUR failed password or invitation "
        "attempts on it this hour."
    )
}
RESP_COMMENT_MOD_429 = {
    "description": (
        "This client address made HUB_MAX_UNLOCK_ATTEMPTS_PER_HOUR failed "
        "password or invitation attempts on this artifact in the current hour."
    )
}
RESP_TOKEN_401 = {"description": "Storage token missing or rejected by the stack."}
RESP_STACK_502 = {
    "description": (
        "The caller's Keboola stack could not be reached to verify the token."
    )
}
RESP_HUB_502 = {
    "description": (
        "The hub's own Keboola Storage backend is unavailable; the artifact "
        "could not be read or written."
    )
}
RESP_NOT_FOUND = {"description": "No artifact exists with this id."}
RESP_UNLOCK_429 = {
    "description": (
        "Too many failed password attempts for this artifact — either from "
        "this client address in the current hour "
        "(HUB_MAX_UNLOCK_ATTEMPTS_PER_HOUR) or, as a backstop no change of "
        "address gets around, from every address together "
        "(HUB_MAX_UNLOCK_ATTEMPTS_PER_ARTIFACT_PER_HOUR)."
    )
}
RESP_GUEST_429 = {
    "description": (
        "Too many failed password *or* invitation attempts for this artifact "
        "in the current hour, counted separately for each and budgeted both "
        "per client address (HUB_MAX_UNLOCK_ATTEMPTS_PER_HOUR) and per "
        "artifact across all addresses "
        "(HUB_MAX_UNLOCK_ATTEMPTS_PER_ARTIFACT_PER_HOUR). Verifying an "
        "invitation secret is as expensive as verifying a password, so "
        "rejected credentials are budgeted the same way."
    )
}
#: SEC-100-004. Answered from the ASGI layer before routing, authentication,
#: lock allocation, body parsing or PBKDF2, so it is the one error that can
#: arrive without the route having run at all.
RESP_BODY_413 = {
    "description": (
        "The request body is larger than this route accepts. Routes that "
        "carry a document (publish, update, submit a version) allow a whole "
        "document; every other route allows HUB_MAX_SMALL_REQUEST_BYTES "
        "(256 KB by default). Both ceilings are reported by GET /context."
    )
}
RESP_VERSIONS_429 = {
    "description": (
        "This project reached HUB_MAX_VERSIONS_PER_DAY submitted versions for "
        "this artifact today; owner updates that add content count too."
    )
}
RESP_THREAD_404 = {
    "description": "No artifact with this id, or no such comment thread."
}
RESP_FINAL_409 = {
    "description": (
        "The artifact is frozen, so this write is refused. Either its status "
        "is 'final' (error 'document is final'; the owner reopens it with PUT "
        "/api/artifacts/{id} and {\"status\": \"draft\"}) or it is in the "
        "trash (error 'document is trashed'; the owner brings it back with "
        "POST /api/artifacts/{id}/restore)."
    )
}
RESP_OWNER_403 = {
    "description": (
        "Token is valid but not from the project that owns this artifact."
    )
}

#: SEC-075-011. The destructive routes answer 403 for a second reason the
#: non-destructive owner routes never do: the token belongs to the owning
#: project but does not satisfy this hub's HUB_DESTRUCTIVE_TOKEN_POLICY.
RESP_DESTRUCTIVE_403 = {
    "description": (
        "Token is valid but not from the project that owns this artifact; or "
        "it is, but this hub's destructive_token_policy (see GET /context, "
        "'limits') requires an admin or allowlisted token for destructive "
        "operations and this token is neither. The response detail says which."
    )
}

#: ``content`` blocks for the non-JSON responses, so /docs stops implying JSON.
CONTENT_HTML = {"text/html": {"schema": {"type": "string"}}}
CONTENT_MARKDOWN = {"text/markdown": {"schema": {"type": "string"}}}
CONTENT_TEXT = {"text/plain": {"schema": {"type": "string"}}}


class MarkdownResponse(Response):
    """A ``text/markdown`` response.

    Used only as a route's ``response_class`` so the generated OpenAPI document
    advertises the real content type of ``/skill`` and ``/agent``. The handlers
    still build their own :class:`Response`, so runtime behavior is unchanged.
    """

    media_type = "text/markdown; charset=utf-8"

class JSONFormatter(logging.Formatter):
    """Render each log record as one real JSON object.

    The previous format string interpolated ``%(message)s`` straight into a
    JSON-shaped template, so any log line carrying user-controlled text (an
    artifact id, a stack URL, a backend error) could inject a quote or a
    newline and either forge an extra record or make the line unparseable.
    ``json.dumps`` escapes quotes, backslashes and control characters,
    including newlines, so a message is always exactly one JSON string in
    exactly one line.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_log_handler = logging.StreamHandler(sys.stdout)
_log_handler.setFormatter(JSONFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_log_handler])
logger = logging.getLogger(__name__)


def _read_service_version() -> str:
    """The single source of truth for the service version.

    Prefers the installed package metadata; falls back to parsing the
    repository's ``pyproject.toml`` when the app runs from a source checkout
    that was never installed. Never invents a placeholder — a build that can
    supply neither is misconfigured and fails fast.
    """
    try:
        return package_version("kbc-artifact-hub")
    except PackageNotFoundError:
        pass
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        with pyproject.open("rb") as handle:
            return str(tomllib.load(handle)["project"]["version"])
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError(
            "Cannot determine the service version: the 'kbc-artifact-hub' "
            f"package is not installed and {pyproject} is unreadable ({exc})"
        ) from exc


SERVICE_VERSION = _read_service_version()

# Settings are read once at import time so a misconfigured deployment fails
# before the server starts accepting traffic.
settings: Settings = load_settings()

#: Guards the lazy re-hydration retry so concurrent requests do not all hammer
#: Storage at once after a failed startup hydration.
_hydrate_lock = threading.Lock()

#: Shape an artifact or share identifier taken from a URL path must have
#: before anything at all is allocated for it. Ids are minted by
#: ``secrets.token_urlsafe`` — 24 characters of the URL-safe alphabet — so
#: this is a deliberate superset: the same character set the stores accept for
#: cache file names (``_SAFE_ID_CHARS`` in src/store.py), with a length bound
#: on top. Rejecting rather than trimming *is* the normalization: an
#: identifier carrying whitespace, a slash, a dot or a hundred kilobytes of
#: padding is not a mangled real id, it is somebody probing (SEC-100-002).
_PATH_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class _ArtifactLockRegistry:
    """Bounded, reference-counted registry of per-artifact mutation locks.

    Every mutating ``/api/artifacts/{id}...`` route reads the artifact,
    decides, then writes -- through *separate* store locks -- so two requests
    on one artifact could interleave between the decision and the write and
    both act on a state that was no longer true: two promotions of one
    proposal both firing, two deletes jointly removing the last live version,
    a submission landing on a document finalized a moment earlier. Holding one
    lock per artifact for the whole handler serializes that. Sound because
    this process is the only writer (see CLAUDE.md, "Exactly one instance");
    with a second instance it would be no protection at all.

    SEC-100-002 replaced the plain dictionary this used to be. That one kept
    an entry for every key it was ever handed, and the comment routes handed
    it keys straight out of the URL: 250 anonymous requests naming made-up
    artifacts grew it by 250 entries that nothing would ever remove. Two
    things changed. Keys now have to survive :data:`_PATH_ID` and name an
    artifact that exists before a lock is taken at all (see
    :func:`_serialized_per_comment_target`), and the registry itself is
    reference-counted: an entry lives while somebody holds or waits for it,
    and once the last holder leaves it becomes idle. Idle entries are kept in
    a least-recently-used queue of at most ``HUB_LOCK_REGISTRY_MAX_ENTRIES``,
    so a hot artifact keeps its lock object across requests while a long tail
    of one-off artifacts is reclaimed.

    Dropping an *idle* entry is safe precisely because it is idle: a key with
    no holders has no lock state to lose, and the next request for it simply
    creates a fresh lock. The invariant that matters -- two concurrent
    requests for one key get the *same* lock object -- holds because the
    refcount is taken under the registry guard before the lock is acquired,
    so an entry cannot be reclaimed out from under a waiter.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        #: key -> how many requests hold or are waiting for its lock.
        self._holders: dict[str, int] = {}
        #: Keys with no holders, oldest first; the eviction queue.
        self._idle: "OrderedDict[str, threading.Lock]" = OrderedDict()

    def __len__(self) -> int:
        """How many locks the registry currently holds (idle plus in use)."""
        with self._guard:
            return len(self._locks)

    def clear(self) -> None:
        """Forget every lock. For tests; never called while requests run."""
        with self._guard:
            self._locks.clear()
            self._holders.clear()
            self._idle.clear()

    @contextmanager
    def hold(self, key: str):
        """Hold ``key``'s lock for the duration of the block."""
        lock = self._checkout(key)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            self._return(key)

    def _checkout(self, key: str) -> threading.Lock:
        """Claim a reference on ``key``'s lock, creating it if needed."""
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = self._locks[key] = threading.Lock()
            # No longer idle: it must not be evicted while we wait for it.
            self._idle.pop(key, None)
            self._holders[key] = self._holders.get(key, 0) + 1
            return lock

    def _return(self, key: str) -> None:
        """Drop a reference; the last one out makes the entry evictable."""
        with self._guard:
            remaining = self._holders.get(key, 1) - 1
            if remaining > 0:
                self._holders[key] = remaining
                return
            self._holders.pop(key, None)
            lock = self._locks.get(key)
            if lock is None:
                return
            self._idle[key] = lock
            self._idle.move_to_end(key)
            bound = max(0, settings.lock_registry_max_entries)
            while len(self._idle) > bound:
                stale, _ = self._idle.popitem(last=False)
                self._locks.pop(stale, None)


#: The process-wide registry. Named as it was before SEC-100-002 so the
#: reasoning in the routes still reads the same; ``len()`` and ``clear()``
#: keep working on it too.
_artifact_locks = _ArtifactLockRegistry()


def _serialized_per_artifact(route):
    """Route decorator: hold the artifact's lock for the whole handler.

    Applied directly above ``def``, so it is the innermost wrapper and the
    one FastAPI registers; ``functools.wraps`` keeps the signature, which is
    what FastAPI reads to resolve path, body and dependency parameters.
    Dependencies (``require_owner`` and its token-verify HTTP call) run
    before the handler is invoked, so they stay outside the lock; only the
    read-decide-write body is inside it.
    """
    @functools.wraps(route)
    def serialized(*args, **kwargs):
        with _artifact_locks.hold(kwargs["artifact_id"]):
            return route(*args, **kwargs)
    return serialized


def _serialized_per_comment_target(route):
    """Like :func:`_serialized_per_artifact`, for routes addressed by share id.

    The comment routes take either half of an artifact's identity pair in
    their path -- the public share id, or the internal id as an owner
    fallback (see :func:`_comment_target_of`). Keying the lock on the path
    parameter would therefore not serialize a comment with the owner routes,
    which always use the internal id, and a comment could land on a document
    being finalized in the same instant. So the key is resolved first, without
    authentication: ``resolve_share`` maps a live share id to the internal id
    and answers None for anything else -- a rotated-away link, or the bare
    internal id of a rotated artifact, which is still a real artifact and
    still has to serialize against its owner's routes.

    SEC-100-002 put two gates in front of that. The path identifier has to
    look like an identifier at all (:data:`_PATH_ID`), and it has to name an
    artifact that exists, *before* a lock is allocated for it. A made-up id
    used to mint a registry entry nobody would ever remove; now it takes no
    lock and the route answers exactly what it always did -- a 404, or the
    401/400 its authentication reaches first, since which of those a caller
    sees is a property the comment routes deliberately own (see
    :func:`_comment_writer`) and not something a lock decorator should
    decide. Running that request unlocked is safe because there is nothing to
    serialize it against: no artifact of that name exists to mutate.

    Resolving before authentication does mean an anonymous caller decides how
    often this lookup runs, so it is asked to be cheap: ``resolve_lock_target``
    answers both halves of the identity pair in a single Storage tag search
    instead of the two the first SEC-100-002 fix took, and the store remembers
    ids Storage has confirmed absent, so a made-up id repeated a thousand times
    reaches Storage once. See :meth:`src.store.ArtifactStore._resolve`.
    """
    @functools.wraps(route)
    def serialized(*args, **kwargs):
        request = kwargs["request"]
        path_id = kwargs["artifact_id"]
        if not _PATH_ID.match(path_id):
            # Nothing this shape was ever minted, so there is no artifact to
            # look up and no lock to take: answer without touching either.
            return _not_found(path_id)
        ensure_hydrated(request.app)
        store = request.app.state.store
        internal_id = store.resolve_lock_target(path_id)
        if internal_id is None:
            return route(*args, **kwargs)
        with _artifact_locks.hold(internal_id):
            return route(*args, **kwargs)
    return serialized

# --------------------------------------------------------------------------
# Rate-limit counters
#
# Since 0.7.0 every counter lives in the SQLite sidecar (``src.statedb``), which
# snapshots itself into Storage Files: a redeploy no longer hands everybody a
# fresh daily budget. The three scopes and their key conventions are fixed by
# statedb's contract:
#
#   scope "submissions"     key "{artifact_id}:{contributor_key}"  bucket UTC day
#   scope "comments"        key "{artifact_id}:{contributor_key}"  bucket UTC day
#   scope "unlock_failures" key "{artifact_id}:{client_ip}"        bucket UTC hour
#
# The key always starts with the *internal* artifact id, which is what
# ``StateDB.forget_artifact`` purges by — so purging an artifact takes its
# counters with it.
#
# The dicts below are a fallback for the window where no StateDB is attached to
# the app (an unstarted app object, or a Storage failure that made the sidecar
# unusable). Limits must keep being enforced then, so the counters degrade to
# the pre-0.7.0 per-process behavior rather than to "no limit at all".
# --------------------------------------------------------------------------

COUNTER_SUBMISSIONS = "submissions"
COUNTER_COMMENTS = "comments"
COUNTER_UNLOCK_FAILURES = "unlock_failures"
COUNTER_GUEST_FAILURES = "guest_failures"

#: The ``who`` half of a counter key standing for "every caller together".
#: SEC-100-003 gives each brute-force scope a second, address-independent
#: budget per artifact, and it shares the machinery of the per-address one by
#: using this sentinel in place of a client address. Real ``who`` values are
#: client addresses or ``"{project}@{stack}"`` contributor keys, neither of
#: which can contain ``*``, so the two buckets can never collide. The key
#: still starts with the internal artifact id, so purging an artifact takes it
#: along with everything else.
COUNTER_ANY_CLIENT = "*"

_fallback_counts: dict[tuple[str, str, str], int] = {}
_fallback_lock = threading.Lock()

#: Above this many live fallback buckets, stale ones are swept on the next bump.
_SUBMISSION_SWEEP_AT = 1000


def _now() -> str:
    """Current UTC timestamp, ISO 8601, second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _utc_day() -> str:
    """Current UTC calendar day, used as the rate-limit bucket."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _utc_hour() -> str:
    """Current UTC hour, used as the unlock-throttle bucket."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")


def _counter_key(artifact_id: str, who: str) -> str:
    """Counter key: the internal artifact id first, so purging can find it."""
    return f"{artifact_id}:{who}"


def _statedb(app_obj: FastAPI | None) -> StateDB | None:
    """The app's state sidecar, or None when there is none to use."""
    if app_obj is None:
        return None
    return getattr(app_obj.state, "statedb", None)


def _fallback_bump(scope: str, key: str, bucket: str) -> int:
    """Per-process counter used when the state sidecar is unavailable."""
    entry = (scope, key, bucket)
    with _fallback_lock:
        if len(_fallback_counts) > _SUBMISSION_SWEEP_AT:
            for stale in [k for k in _fallback_counts if k[2] != bucket]:
                del _fallback_counts[stale]
        value = _fallback_counts.get(entry, 0) + 1
        _fallback_counts[entry] = value
        return value


def _bump_counter(app_obj: FastAPI | None, scope: str, key: str, bucket: str) -> int:
    """Add one to a counter and return its new value, sidecar or fallback."""
    database = _statedb(app_obj)
    if database is not None:
        try:
            return database.bump(scope, key, bucket)
        except Exception as exc:  # noqa: BLE001 - a limit must survive a bad DB
            logger.warning("State counter bump failed (%s/%s): %s", scope, key, exc)
    return _fallback_bump(scope, key, bucket)


def _read_counter(app_obj: FastAPI | None, scope: str, key: str, bucket: str) -> int:
    """Current value of a counter, sidecar or fallback."""
    database = _statedb(app_obj)
    if database is not None:
        try:
            return database.count(scope, key, bucket)
        except Exception as exc:  # noqa: BLE001 - a limit must survive a bad DB
            logger.warning("State counter read failed (%s/%s): %s", scope, key, exc)
    with _fallback_lock:
        return _fallback_counts.get((scope, key, bucket), 0)


def _claim_slot(
    app_obj: FastAPI | None,
    scope: str,
    artifact_id: str,
    contributor_key: str,
    limit: int,
) -> bool:
    """Count one write against a per-(artifact, project, UTC day) bucket.

    Returns False when the bucket is exhausted. The bump happens either way —
    a caller who keeps hammering a spent budget keeps being counted, which is
    what makes the window self-limiting rather than a retry loop.
    """
    used = _bump_counter(
        app_obj, scope, _counter_key(artifact_id, contributor_key), _utc_day()
    )
    return used <= limit


def _claim_submission_slot(
    app_obj: FastAPI | None, artifact_id: str, contributor_key: str
) -> bool:
    """Count one version submission; False when the daily cap is exhausted."""
    return _claim_slot(
        app_obj,
        COUNTER_SUBMISSIONS,
        artifact_id,
        contributor_key,
        settings.max_versions_per_day,
    )


def _claim_comment_slot(
    app_obj: FastAPI | None, artifact_id: str, contributor_key: str
) -> bool:
    """Count one comment or reply; False when the daily cap is exhausted."""
    return _claim_slot(
        app_obj,
        COUNTER_COMMENTS,
        artifact_id,
        contributor_key,
        settings.max_comments_per_day,
    )


def _brute_force_throttled(
    app_obj: FastAPI | None, scope: str, artifact_id: str, client_ip: str
) -> bool:
    """True when either brute-force budget of one artifact is spent.

    Two budgets, both hourly, both counting only *failures*:

    * per ``(artifact, client address)``,
      ``HUB_MAX_UNLOCK_ATTEMPTS_PER_HOUR`` — the ordinary limit, small enough
      that a human who mistypes is never troubled by it;
    * per artifact across *every* address,
      ``HUB_MAX_UNLOCK_ATTEMPTS_PER_ARTIFACT_PER_HOUR`` — defence in depth for
      SEC-100-003. The per-address budget is only as strong as the address is
      hard to change, and the address is not always expensive: a NAT, a
      botnet, or (before this fix) a forged header all supply new ones for
      free. This second budget cannot be rotated away, so it is set far above
      any real audience of one document and stops industrial guessing rather
      than individual readers.
    """
    per_client = _read_counter(
        app_obj, scope, _counter_key(artifact_id, client_ip), _utc_hour()
    )
    if per_client >= settings.max_unlock_attempts_per_hour:
        return True
    per_artifact = _read_counter(
        app_obj, scope, _counter_key(artifact_id, COUNTER_ANY_CLIENT), _utc_hour()
    )
    return per_artifact >= settings.max_unlock_attempts_per_artifact_per_hour


def _record_brute_force_failure(
    app_obj: FastAPI | None, scope: str, artifact_id: str, client_ip: str
) -> None:
    """Count one failure against both budgets of :func:`_brute_force_throttled`."""
    _bump_counter(
        app_obj, scope, _counter_key(artifact_id, client_ip), _utc_hour()
    )
    _bump_counter(
        app_obj, scope, _counter_key(artifact_id, COUNTER_ANY_CLIENT), _utc_hour()
    )


def _unlock_throttled(
    app_obj: FastAPI | None, artifact_id: str, client_ip: str
) -> bool:
    """True when this artifact's failed-password budget is spent.

    Verifying an artifact password runs a full PBKDF2 (200k iterations), so an
    unthrottled gate is both a password oracle and a cheap way to burn the
    hub's CPU. Only failures are counted, so a legitimate reader who types the
    password correctly is never affected, and the hour in the bucket key makes
    the window reset on its own. See :func:`_brute_force_throttled` for the
    two budgets this consults.
    """
    return _brute_force_throttled(
        app_obj, COUNTER_UNLOCK_FAILURES, artifact_id, client_ip
    )


def _record_unlock_failure(
    app_obj: FastAPI | None, artifact_id: str, client_ip: str
) -> None:
    """Count one *failed* password attempt against the hourly budgets."""
    _record_brute_force_failure(
        app_obj, COUNTER_UNLOCK_FAILURES, artifact_id, client_ip
    )


def _guest_throttled(
    app_obj: FastAPI | None, artifact_id: str, client_ip: str
) -> bool:
    """True when this artifact's failed-invitation budget is spent.

    Checking an invitation secret runs the same full PBKDF2 as a password, and
    ``GET /a/{id}/guest`` is public, so a known invitation id would otherwise
    be a free CPU-exhaustion primitive as well as an offline-free oracle. The
    budgets are the unlock budgets in their own scope, so guest probing and
    password guessing cannot spend each other's allowance.
    """
    return _brute_force_throttled(
        app_obj, COUNTER_GUEST_FAILURES, artifact_id, client_ip
    )


def _record_guest_failure(
    app_obj: FastAPI | None, artifact_id: str, client_ip: str
) -> None:
    """Count one *failed* guest verification against the hourly budgets."""
    _record_brute_force_failure(
        app_obj, COUNTER_GUEST_FAILURES, artifact_id, client_ip
    )


def _record_view(app_obj: FastAPI | None, artifact_id: str, kind: str) -> None:
    """Count one successful read of an artifact. Never raises into serving.

    Analytics are strictly best effort: a broken or unstarted state sidecar
    must degrade to "no numbers", never to a failed page load.
    """
    database = _statedb(app_obj)
    if database is None:
        return
    try:
        database.record_view(artifact_id, _utc_day(), kind)
    except Exception as exc:  # noqa: BLE001 - analytics never break serving
        logger.warning("Could not record a %s view of %s: %s", kind, artifact_id, exc)


#: Either address flavour, as :mod:`ipaddress` hands them back.
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _peer_ip(request: Request) -> str:
    """The address the TCP connection actually came from, or ``"unknown"``."""
    client = request.client
    return client.host if client is not None else "unknown"


def _unmapped(address: IPAddress) -> IPAddress:
    """An IPv4-mapped IPv6 address as plain IPv4; anything else unchanged.

    SEC-100-003. A dual-stack listener reports an IPv4 peer as
    ``::ffff:10.0.0.5``, and that is *not* a member of ``10.0.0.0/8`` as far
    as :mod:`ipaddress` is concerned. Without this an operator who configured
    ``HUB_TRUSTED_PROXY_CIDRS`` perfectly correctly would still have every
    forwarded address ignored, with nothing to distinguish that from the
    headers simply not arriving. Normalizing in the bucket key too means the
    two spellings of one address cannot become two brute-force budgets.
    """
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped if mapped is not None else address


def _parse_client_address(raw: str | None) -> IPAddress | None:
    """One forwarded chain entry as a canonical address, or None if it is junk.

    SEC-100-003. Everything this returns ends up as the key of a persisted
    ``counters`` row, so an entry that is not an address must be dropped
    rather than passed through: before this the caller chose that key, its
    contents and its length outright. Tolerates the three shapes real proxies
    emit around an address — surrounding whitespace, an ``a.b.c.d:port``
    suffix, and a bracketed ``[v6]`` or ``[v6]:port`` literal — and rejects
    anything else.
    """
    if not raw:
        return None
    candidate = raw.strip()
    if candidate.startswith("["):
        closing = candidate.find("]")
        if closing == -1:
            return None
        candidate = candidate[1:closing]
    elif candidate.count(":") == 1:
        # Exactly one colon means "IPv4 with a port": a bare IPv6 literal
        # always carries at least two, so this cannot eat one of those.
        candidate = candidate.split(":", 1)[0]
    try:
        return _unmapped(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def _is_trusted_proxy(address: IPAddress) -> bool:
    """True when ``address`` sits in one of ``HUB_TRUSTED_PROXY_CIDRS``."""
    return any(address in network for network in settings.trusted_proxy_cidrs)


def _forwarded_client_trusted(request: Request) -> bool:
    """True when this request's forwarded client address may be believed.

    SEC-100-003. Two conditions, both required. The deployment has to have
    opted into forwarded headers at all (``HUB_TRUST_FORWARDED_HEADERS``,
    the same switch :func:`_forwarded_trusted` reads), *and* the direct peer —
    the address the connection came from, which no client can forge — has to
    fall inside one of ``HUB_TRUSTED_PROXY_CIDRS``. With no CIDRs configured
    nothing is ever trusted, which is the safe default: every caller behind
    the proxy then shares the proxy's bucket.

    Deliberately *not* the same predicate as :func:`_forwarded_trusted`: that
    one also insists no ``HUB_PUBLIC_BASE_URL`` is configured, because an
    explicit public origin outranks a forwarded one when naming URLs. That has
    nothing to do with who the client is, and production sets a base URL — so
    reusing it here would have made the trusted-proxy setting dead code in the
    one deployment that needs it.
    """
    if not settings.trust_forwarded_headers or not settings.trusted_proxy_cidrs:
        return False
    peer = _parse_client_address(_peer_ip(request))
    if peer is None:
        # Not an IP literal at all (a unix socket, a test transport): there is
        # no way to place it in a network, so it is not a trusted proxy.
        return False
    return _is_trusted_proxy(peer)


def _forwarded_chain_client(raw: str | None) -> IPAddress | None:
    """The rightmost entry of an ``X-Forwarded-For`` chain we did not write.

    SEC-100-003, second half. Every proxy in the path *appends* the peer it
    accepted the connection from — our own nginx does exactly that with
    ``$proxy_add_x_forwarded_for`` — so the chain reads oldest-first and the
    leftmost entry is whatever the original caller chose to send. Reading it
    was the bug: a guesser who prepended a different value each time got a
    fresh brute-force budget every attempt, which is the finding restated.

    So walk from the right instead, past the hops our own infrastructure
    added (the entries inside ``HUB_TRUSTED_PROXY_CIDRS``), and stop at the
    first entry neither we nor a trusted proxy vouched for: that is the
    closest thing to a real client this request carries. Unparsable entries
    are skipped rather than returned, and a chain that is nothing but trusted
    proxies (or nothing but junk) yields None, leaving the caller on the peer
    address.

    Note the corollary for operators: *every* hop's network has to be in
    ``HUB_TRUSTED_PROXY_CIDRS``, not just the one that opens the connection.
    Name only nginx's network and the walk stops at the platform proxy, so
    every reader shares that one bucket — safe, but no better than leaving
    the setting empty.

    Only the last ``HUB_MAX_FORWARDED_CHAIN_ENTRIES`` entries are examined, so
    a caller cannot buy an unbounded number of address parses with one very
    long header.
    """
    if not raw:
        return None
    # Clamped at zero because a negative maxsplit means "split everything" to
    # str.rsplit — a typo in HUB_MAX_FORWARDED_CHAIN_ENTRIES must not quietly
    # turn the cap off, which is the one thing it exists to prevent.
    cap = max(settings.max_forwarded_chain_entries, 0)
    # rsplit stops after `cap` splits, so the leftmost element is the whole
    # unexamined remainder of the chain rather than a single entry; drop it.
    entries = raw.rsplit(",", cap)
    if len(entries) > cap:
        entries = entries[1:]
    for entry in reversed(entries):
        address = _parse_client_address(entry)
        if address is not None and not _is_trusted_proxy(address):
            return address
    return None


def _client_ip(request: Request) -> str:
    """Best-effort client address, used only as a rate-limit bucket key.

    Until SEC-100-003 this returned ``X-Real-IP`` whenever it was present. The
    header is attacker-supplied, so a password guesser reset their own
    brute-force budget simply by changing it — a budget of one still allowed
    unlimited attempts. Forwarded addresses are now honoured only from a peer
    inside a configured trusted-proxy network
    (:func:`_forwarded_client_trusted`); otherwise the bucket key is the peer
    address, which nobody can choose.

    Order of preference, once the peer is trusted: ``X-Real-IP`` first,
    because our own nginx sets it from the connection it accepted and it is a
    single address needing no chain walk -- unless that address is itself a
    trusted proxy, in which case it names a hop rather than a client and the
    chain is consulted instead; then the rightmost entry of
    ``X-Forwarded-For`` that is not itself a trusted proxy (see
    :func:`_forwarded_chain_client` for why the *rightmost*). Either way the
    value has to parse as an IP address, and what is returned is that address
    canonicalized — a caller cannot make the key arbitrary text, nor make it
    long enough to bloat the persisted ``counters`` table.

    Even then this identity is only ever a bucket key: a forged value can
    split an attacker's own budget, never grant access or identify anybody.
    The address-independent per-artifact budget (see
    :func:`_unlock_throttled`) is what bounds an attacker who *can* change
    addresses cheaply.
    """
    peer = _peer_ip(request)
    if _forwarded_client_trusted(request):
        forwarded = _parse_client_address(request.headers.get("x-real-ip"))
        # In the real topology our nginx sets X-Real-IP from *its* peer,
        # which is the platform proxy in front of it, not the browser. A
        # value that names a trusted proxy therefore identifies a hop, not a
        # client, and would make every reader share that hop's budget; the
        # chain still carries the client, so walk it instead.
        if forwarded is not None and _is_trusted_proxy(forwarded):
            forwarded = None
        if forwarded is None:
            forwarded = _forwarded_chain_client(
                request.headers.get("x-forwarded-for")
            )
        if forwarded is not None:
            return str(forwarded)
    parsed_peer = _parse_client_address(peer)
    # A peer that is not an address literal (a unix socket, a test transport)
    # is still a fine bucket key: it is short, fixed, and nobody can choose it.
    return str(parsed_peer) if parsed_peer is not None else peer


def _version_rate_limited() -> JSONResponse:
    """429 shared by every route that adds a version (owner updates included)."""
    return JSONResponse(
        status_code=429,
        content={
            "error": "too many versions today",
            "detail": (
                f"Your project may submit {settings.max_versions_per_day} "
                "versions of one artifact per UTC day."
            ),
            "limit": settings.max_versions_per_day,
        },
    )


class SingleInstanceError(RuntimeError):
    """Startup refused because another process already holds the hub's lock."""


def acquire_instance_lock(path: Path):
    """Take the process-wide exclusive lock, or raise :class:`SingleInstanceError`.

    ARCH-100-001. Everything in this service that is correct at all is correct
    only under one writer: the in-memory artifact index, the per-process locks
    that serialize check-then-act mutations, the version-number allocator and
    the StateDB whole-snapshot cycle. CLAUDE.md states that as a deployment
    invariant ("Exactly one instance, ever"), and for a long time that was all
    it was -- a sentence. ``uvicorn --workers 2``, a second container mounted
    on the same cache directory, or a rolling deploy that overlapped would each
    have started happily and begun losing the other's writes.

    ``flock`` turns the sentence into a startup condition. The lock belongs to
    the open file description rather than to the process, so every way of
    ending up with two writers on one disk fails the same way: a uvicorn worker
    (each runs its own lifespan and so its own ``open()``), a second container,
    or a stray second interpreter. ``LOCK_NB`` means the loser fails fast with
    an explanation instead of hanging at boot with no output.

    The handle is returned rather than kept in a module global so tests can own
    one directly; the caller must hold it for the life of the process, because
    closing the file releases the lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # "a+" never truncates, so losing the race cannot destroy the winner's
    # record of which pid holds the lock.
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        try:
            handle.seek(0)
            holder = handle.read(200).strip() or "unknown"
        except OSError:  # pragma: no cover - reading the holder is a courtesy
            holder = "unknown"
        handle.close()
        raise SingleInstanceError(
            f"Another process already holds the hub instance lock {path} "
            f"(holder: {holder}). This deployment is exactly one instance, "
            "ever: one Keboola App is one organisation's hub, one container, "
            "one uvicorn process, no --workers (CLAUDE.md, \"Exactly one "
            "instance, ever\"). Two writers corrupt the artifact index, the "
            "version-number allocator and the state snapshots. Stop the other "
            "process, or give this one its own HUB_CACHE_DIR."
        ) from exc
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
    except OSError as exc:  # pragma: no cover - the lock is what matters
        logger.warning("Could not record the pid in the instance lock: %s", exc)
    return handle


def release_instance_lock(handle) -> None:
    """Drop the exclusive lock and close its handle. Never raises."""
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:  # noqa: BLE001 - shutdown must not raise
        logger.warning("Could not release the instance lock: %s", exc)
    finally:
        try:
            handle.close()
        except OSError as exc:  # noqa: BLE001 - shutdown must not raise
            logger.warning("Could not close the instance lock: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the stores and sidecars, and try (not require) an initial hydration.

    Four pieces of state hang off ``app.state``: the two content stores, the
    operational-state sidecar (:class:`~src.statedb.StateDB`: rate-limit
    counters and view analytics, snapshotted into Storage Files) and the
    outbound webhook dispatcher. The sidecar is started *after* hydration, so a
    slow Storage listing never delays the counters becoming usable, and both
    background workers are stopped on the way out — the sidecar's ``stop()``
    takes a final snapshot, so a clean shutdown loses nothing.
    """
    app.state.settings = settings
    # ARCH-100-001: before anything touches Storage or the cache, prove this is
    # the only process doing so. Failing here aborts startup, which is the
    # point -- a second writer that starts successfully is far more expensive
    # to notice than one that never starts.
    app.state.instance_lock = acquire_instance_lock(
        settings.cache_dir / settings.instance_lock_filename
    )
    # One backend instance serves both stores: they read the same host project
    # and only differ in the tags they list (``artifact-hub`` vs.
    # ``artifact-hub-cmt``), so neither ever sees the other's files. The state
    # sidecar shares it too, under its own disjoint ``artifact-hub-state`` tag.
    backend = KbcFilesBackend(settings.hub_stack_url, settings.hub_storage_token)
    app.state.store = ArtifactStore(
        backend,
        settings.cache_dir,
        settings.cache_max_entries,
        settings.max_versions,
        settings.max_envelope_bytes,
        settings.max_proposed_versions,
        reap_aborted_after_s=settings.reap_aborted_publish_after_s,
        negative_lookup_cache_entries=settings.negative_lookup_cache_entries,
    )
    app.state.comments = CommentStore(
        backend,
        settings.cache_dir,
        settings.cache_max_entries,
    )
    # Neither consumer of the master secret ever sees it raw: each gets its own
    # key derived under a distinct label (see security.derive_key). A webhook
    # receiver necessarily learns the key it verifies signatures with, and must
    # not thereby be able to mint unlock cookies. Switching to a derived key
    # invalidates unlock cookies issued by an older build — that is expected and
    # harmless: they are short-lived, and a reader simply unlocks once more.
    app.state.signer = CookieSigner(
        derive_key(settings.secret_key, KEY_LABEL_UNLOCK_COOKIE)
    )
    # PKCE sign-ins in flight. Process-local, and only ever populated on a
    # loopback hub — see PkceRegistry.
    app.state.pkce = PkceRegistry(
        settings.login_pkce_ttl_s, settings.login_max_pending_pkce
    )
    app.state.hydrated = False
    statedb = StateDB(
        backend,
        settings.cache_dir / settings.state_db_filename,
        settings.state_snapshot_interval_s,
        settings.state_max_snapshot_bytes,
    )
    app.state.statedb = statedb
    webhooks = WebhookDispatcher(
        settings.webhook_timeout_s,
        settings.webhook_max_attempts,
        derive_key(settings.secret_key, KEY_LABEL_WEBHOOK),
        queue_max=settings.webhook_queue_max,
    )
    # SEC-100-006 follow-up: without this, the overlap window is synced into
    # the dispatcher only when an owner calls GET .../webhooks or the
    # rotate-key route for the first time in this process's life (see the
    # comment on WebhookDispatcher._key_overlap_s). Setting it here too means
    # a delivery that goes out before either of those has ever been called
    # honours the configured grace period from the first request, instead of
    # silently running on the class default until an owner happens to look.
    webhooks.configure_key_overlap_s(settings.webhook_key_overlap_s)
    app.state.webhooks = webhooks
    try:
        artifacts, threads = _hydrate(app)
        app.state.hydrated = True
        logger.info(
            "Startup hydration complete: %d artifact(s), %d comment thread(s)",
            artifacts,
            threads,
        )
    except BackendError as exc:
        logger.error(
            "Startup hydration failed, serving in degraded mode: %s", exc
        )
    statedb.start()
    # Buckets are ISO-ish strings, so lexicographic order is chronological
    # order and today's day string sorts before today's hour strings
    # ("2026-09-01" < "2026-09-01T14"). Pruning at that boundary drops every
    # window that has already closed and keeps today's intact.
    try:
        removed = statedb.prune_counters_before(_utc_day())
        if removed:
            logger.info("Pruned %d expired rate-limit counter(s)", removed)
    except Exception as exc:  # noqa: BLE001 - housekeeping must not block boot
        logger.warning("Could not prune expired rate-limit counters: %s", exc)
    webhooks.start()
    try:
        yield
    finally:
        webhooks.stop()
        statedb.stop()
        release_instance_lock(getattr(app.state, "instance_lock", None))
        app.state.instance_lock = None


def _hydrate(app_obj: FastAPI) -> tuple[int, int]:
    """Rebuild both indexes from Storage; returns (artifacts, threads)."""
    artifacts = app_obj.state.store.hydrate()
    threads = app_obj.state.comments.hydrate()
    return artifacts, threads


#: Markdown shown at the top of the interactive docs (Swagger UI) and in the
#: generated OpenAPI document's ``info.description``.
API_DESCRIPTION = """\
Public hosting for self-contained HTML/Markdown artifacts, backed by Keboola
Storage. Anyone holding **any** Keboola Storage API token, on **any** Keboola
stack, can publish a document and get back an unguessable public URL.

## Authentication

Everything under `/api/artifacts` is authenticated with two headers instead
of a bearer token:

| Header | Meaning |
|---|---|
| `X-StorageApi-Token` | Any Keboola Storage API token |
| `X-Storage-Stack` | Stack alias (`us`, `gcp-us`, `eu`, `azure-eu`, `gcp-eu`) or a full `https://*.keboola.com` URL. `X-Kbc-Stack` is accepted as an alias for direct/local access. |

Use the **Authorize** button above to set both headers once for every request
made from this page.

## Learn more

- [`/admin`](/admin) — browser studio for artifact owners: review, diff,
  promote, reject, pin and delete versions. Your token stays in the tab.
- [`/agent`](/agent) — a ready-to-install Claude Code subagent definition
  (`install -d ~/.claude/agents && curl -fsSL {base}/agent -o
  ~/.claude/agents/artifact-hub.md`).
- [`/skill`](/skill) — SKILL.md teaching an AI agent how to publish
  artifacts, unassisted.
- [`/context`](/context) — machine-readable manifest of endpoints, auth
  model and limits.

## Artifact URLs are capabilities

Reading an artifact (`/a/{id}` and friends) needs no token: the unguessable
id in the URL *is* the access control. There is no public listing. An
optional password adds a second layer on top.

## Versioning

Updates never overwrite: each one adds a version. `/a/{id}` serves the head
(newest live version, or a pinned one). When the owner sets
`accept_versions`, any other project may submit a version — it lands as a
**proposal**, readable only by the owner and its author until the owner
promotes it.
"""

app = FastAPI(
    title="KBC Artifact Hub",
    version=SERVICE_VERSION,
    description=API_DESCRIPTION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
    openapi_url="/openapi.json",
    openapi_tags=[
        {
            "name": "public",
            "description": "Unauthenticated reads: artifact pages, version history, diffs.",
        },
        {
            "name": "artifacts",
            "description": "Authenticated artifact management (publish, update, list, delete).",
        },
        {
            "name": "versions",
            "description": "Authenticated community versioning: submit, promote, withdraw, pin.",
        },
        {
            "name": "comments",
            "description": (
                "Authenticated inline comment threads: create, reply, resolve "
                "and delete."
            ),
        },
        {
            "name": "service",
            "description": "Health, machine-readable manifest, and the agent-facing skill document.",
        },
    ],
)


#: Names of the two OpenAPI apiKey-in-header security schemes. Kept in one
#: place so the custom openapi() override and the per-route `security` lists
#: cannot drift apart.
_TOKEN_SECURITY = "StorageApiToken"
_STACK_SECURITY = "StorageStack"
_BEARER_SECURITY = "KeboolaBearer"
_PROJECT_SECURITY = "StorageProject"


def custom_openapi() -> dict[str, Any]:
    """Attach the two header-based auth schemes to every ``/api/*`` operation.

    FastAPI has no first-class concept of "two headers act together as
    credentials", so the schemes are declared as plain ``apiKey``-in-header
    security schemes and wired onto the relevant operations here. This is
    purely a documentation aid for Swagger UI's Authorize button — the actual
    runtime check stays in :func:`require_owner`, untouched.
    """
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
    )
    components = schema.setdefault("components", {})
    security_schemes = components.setdefault("securitySchemes", {})
    security_schemes[_TOKEN_SECURITY] = {
        "type": "apiKey",
        "in": "header",
        "name": "X-StorageApi-Token",
        "description": (
            "A Keboola Storage API token. A kbc_at_*/kbc_pat_* sign-in token "
            "goes in Authorization: Bearer instead."
        ),
    }
    security_schemes[_STACK_SECURITY] = {
        "type": "apiKey",
        "in": "header",
        "name": "X-Storage-Stack",
        "description": (
            "Stack alias (us, gcp-us, eu, azure-eu, gcp-eu) or a full "
            "https://*.keboola.com URL."
        ),
    }
    security_schemes[_BEARER_SECURITY] = {
        "type": "http",
        "scheme": "bearer",
        "description": (
            "A Keboola sign-in token: a kbc_at_* session or a kbc_pat_* "
            "personal access token, with X-Storage-Project naming the project "
            "it acts as. A Storage API token belongs in X-StorageApi-Token "
            "instead; either header alone, never both."
        ),
    }
    security_schemes[_PROJECT_SECURITY] = {
        "type": "apiKey",
        "in": "header",
        "name": "X-Storage-Project",
        "description": (
            "Project id the caller is acting as. Required with a kbc_at_* or "
            "kbc_pat_* credential, ignored with a Storage token."
        ),
    }
    # Two alternatives, each a complete way to authenticate: the header pair
    # every Storage token has always used, or a bearer plus the project it
    # acts as. Swagger's Authorize dialog offers both.
    security_requirement = [
        {_TOKEN_SECURITY: [], _STACK_SECURITY: [], _PROJECT_SECURITY: []},
        {_BEARER_SECURITY: [], _STACK_SECURITY: [], _PROJECT_SECURITY: []},
    ]
    for path, methods in schema.get("paths", {}).items():
        if not path.startswith("/api/"):
            continue
        for operation in methods.values():
            if isinstance(operation, dict):
                operation["security"] = security_requirement
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi


def ensure_hydrated(app_obj: FastAPI) -> None:
    """Retry index hydration once per call while the indexes are not hydrated.

    Covers both the artifact index and the comment-thread index; the single
    flag flips only when both rebuilt. A failure here is not fatal: both stores
    fall back to a per-artifact Storage lookup, so individual reads still work
    while the full index is missing.
    """
    if getattr(app_obj.state, "hydrated", False):
        return
    with _hydrate_lock:
        if getattr(app_obj.state, "hydrated", False):
            return
        try:
            artifacts, threads = _hydrate(app_obj)
        except BackendError as exc:
            logger.warning("Deferred hydration attempt failed: %s", exc)
            return
        app_obj.state.hydrated = True
        logger.info(
            "Deferred hydration complete: %d artifact(s), %d comment thread(s)",
            artifacts,
            threads,
        )


# --------------------------------------------------------------------------
# Middleware and error handling
# --------------------------------------------------------------------------


#: Forwarded schemes we are willing to trust; anything else is ignored.
_PUBLIC_SCHEMES = frozenset({"http", "https"})

#: A ``host[:port]`` we are willing to write into the request scope: a
#: registered name or IPv4 literal, or a bracketed IPv6 literal, each with an
#: optional port. Deliberately strict — a malformed forwarded value must be
#: dropped rather than turned into a broken absolute URL.
_PUBLIC_HOST = re.compile(r"^(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)(?::[0-9]{1,5})?$")


def _first_forwarded(raw: str | None) -> str:
    """First entry of a comma-separated ``X-Forwarded-*`` value, stripped."""
    if not raw:
        return ""
    return raw.split(",", 1)[0].strip()


def _forwarded_trusted() -> bool:
    """True when ``X-Forwarded-Host``/``-Proto`` may name the public origin.

    Only when no explicit ``HUB_PUBLIC_BASE_URL`` is configured (that one wins
    outright) *and* the deployment opted in with
    ``HUB_TRUST_FORWARDED_HEADERS``. Otherwise the headers are client-supplied
    and unverifiable, so they are ignored entirely.
    """
    return not settings.public_base_url and settings.trust_forwarded_headers


@lru_cache(maxsize=8)
def _public_origin(public_base_url: str | None) -> tuple[str, str] | None:
    """``(scheme, netloc)`` of ``HUB_PUBLIC_BASE_URL``, or None when unusable.

    Memoized, so the configured value is parsed once instead of on every
    request. Keying the cache on the value rather than parsing at import time
    keeps the helper honest when the module-level settings object is swapped
    (as the test suite does).
    """
    if not public_base_url:
        return None
    parsed = urlsplit(public_base_url)
    scheme = parsed.scheme.lower()
    if scheme not in _PUBLIC_SCHEMES or not _PUBLIC_HOST.match(parsed.netloc):
        return None
    return scheme, parsed.netloc


def _set_host_header(scope: dict[str, Any], host: str) -> None:
    """Replace (or insert) the ``host`` entry of an ASGI scope's header list.

    Every other header is carried over untouched and in order; duplicate
    ``Host`` headers collapse into the single normalized one.
    """
    encoded = host.encode("latin-1")
    headers: list[tuple[bytes, bytes]] = []
    replaced = False
    for name, value in scope["headers"]:
        if name == b"host":
            if replaced:
                continue
            headers.append((name, encoded))
            replaced = True
        else:
            headers.append((name, value))
    if not replaced:
        headers.append((b"host", encoded))
    scope["headers"] = headers


@app.middleware("http")
async def public_origin(request: Request, call_next):
    """Normalize the request scope to the origin the client actually used.

    The Keboola data-app platform proxy terminates TLS and rewrites ``Host`` to
    the internal cluster service name (``app-*.sandbox.svc.cluster.local``),
    forwarding the real values in ``X-Forwarded-Proto`` / ``X-Forwarded-Host``.
    Starlette builds *absolute* URLs straight from the ASGI scope, so without
    this normalization a request for ``/a/{id}/`` gets a 307 whose ``Location``
    names the internal hostname — both a leak and unreachable for the client.

    Rewriting the scheme and the ``Host`` header here, before routing, fixes
    every absolute URL Starlette generates (trailing-slash redirects today,
    ``url_for`` tomorrow). :func:`base_url` never had this bug because it reads
    the forwarded headers itself, which is why JSON payload URLs were correct
    while redirects were not.

    Precedence: an explicitly configured ``HUB_PUBLIC_BASE_URL`` wins, then —
    *only* when ``HUB_TRUST_FORWARDED_HEADERS`` is on — the forwarded headers;
    when neither yields a usable value the scope is left exactly as it
    arrived. Forwarded headers are off by default because any direct client
    can send them: honoring them unconditionally let an attacker choose the
    host in our redirects and generated links. Production sets
    HUB_PUBLIC_BASE_URL, so nothing changes there; a developer running behind
    a local proxy opts in.
    """
    origin = _public_origin(settings.public_base_url)
    if origin is not None:
        scheme, host = origin
    elif _forwarded_trusted():
        scheme = _first_forwarded(request.headers.get("x-forwarded-proto")).lower()
        host = _first_forwarded(request.headers.get("x-forwarded-host"))
        if scheme not in _PUBLIC_SCHEMES:
            scheme = ""
        if not _PUBLIC_HOST.match(host):
            host = ""
    else:
        scheme = ""
        host = ""
    if scheme:
        request.scope["scheme"] = scheme
    if host:
        _set_host_header(request.scope, host)
    return await call_next(request)


#: Methods whose requests carry no body this service ever reads. Budgeting
#: them would only add work to the cheapest requests we serve.
_BODILESS_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "DELETE", "TRACE"})

#: The routes whose body legitimately carries a whole document, as
#: ``(method, path pattern)``. Everything else — comments, replies, guest
#: invitations, the unlock form, webhook management, policy-only calls, the
#: platform's ``POST /`` probe — gets the much smaller
#: ``HUB_MAX_SMALL_REQUEST_BYTES``. The list is deliberately an allowlist:
#: a route added later is bounded tightly until somebody decides otherwise,
#: which is the safe direction to be wrong in.
_CONTENT_REQUEST_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("POST", re.compile(r"^/api/artifacts/?$")),
    ("PUT", re.compile(r"^/api/artifacts/[^/]+/?$")),
    ("POST", re.compile(r"^/api/artifacts/[^/]+/versions/?$")),
)


def _request_body_limit(method: str, path: str) -> int:
    """Largest request body this method and path may send, in bytes."""
    for route_method, pattern in _CONTENT_REQUEST_ROUTES:
        if method == route_method and pattern.match(path):
            return settings.max_content_request_bytes
    return settings.max_small_request_bytes


def _declared_content_length(scope: dict[str, Any]) -> int | None:
    """The request's ``Content-Length``, or None when absent or unparseable."""
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _send_body_too_large(send: Any, limit: int) -> None:
    """Answer 413 straight from the ASGI layer, without invoking the app."""
    payload = json.dumps(
        {
            "error": "request body too large",
            "detail": (
                f"This endpoint accepts at most {limit} bytes of request "
                "body. Send less, or use a route meant for documents."
            ),
            "limit": limit,
        }
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class RequestBodyLimitMiddleware:
    """Reject an oversized request body before anything expensive touches it.

    SEC-100-004. Every budget this service had was applied *after* the body
    had already been read and parsed: an anonymous caller could post three
    megabytes at a comment route, have it streamed in, JSON-decoded and
    validated into Pydantic models, and only then be told the artifact does
    not exist. Authentication, the per-artifact lock and PBKDF2 all sit behind
    that same parse, so the cheapest request an attacker can send was also one
    of the most expensive to refuse.

    This is raw ASGI rather than a ``BaseHTTPMiddleware`` on purpose: it has
    to see the request *before* Starlette builds a ``Request`` object and
    before FastAPI reads the body, and it has to be able to wrap ``receive``.
    Two gates, because either alone is bypassable:

    * a declared ``Content-Length`` above the ceiling is refused outright,
      with the body never read at all;
    * ``receive`` is wrapped and the bytes that actually arrive are counted,
      so a chunked request (no ``Content-Length``) and a request that lies
      about its length are both cut off the moment they cross the ceiling.
      Nothing is buffered: the stream is closed by handing the application a
      disconnect, which unwinds it, and whatever it answers is replaced by
      the 413 on the way out.

    ``POST /`` — the platform's startup probe — carries no body and passes
    through untouched, as it must (see CLAUDE.md).
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method", "GET")).upper()
        if method in _BODILESS_METHODS:
            await self.app(scope, receive, send)
            return

        limit = _request_body_limit(method, str(scope.get("path", "")))
        declared = _declared_content_length(scope)
        if declared is not None and declared > limit:
            await _send_body_too_large(send, limit)
            return

        seen = 0
        over = False
        replaced = False

        async def counted_receive() -> dict[str, Any]:
            nonlocal seen, over
            message = await receive()
            if message.get("type") == "http.request":
                seen += len(message.get("body", b"") or b"")
                if seen > limit:
                    over = True
                    # Close the body rather than buffer the rest of it. The
                    # application sees a disconnected client, stops reading
                    # and unwinds; ``guarded_send`` turns its answer into
                    # the 413 this really is.
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: dict[str, Any]) -> None:
            nonlocal replaced
            if replaced:
                # Our 413 is already on the wire; the application's own
                # response body would only corrupt it.
                return
            if over and message.get("type") == "http.response.start":
                replaced = True
                await _send_body_too_large(send, limit)
                return
            await send(message)

        await self.app(scope, counted_receive, guarded_send)


# Registered here, between the two HTTP middlewares, so it runs before routing,
# authentication, lock allocation, JSON/Pydantic parsing and PBKDF2 — while
# still sitting inside ``artifact_headers``, which therefore decorates the 413
# exactly like every other response.
app.add_middleware(RequestBodyLimitMiddleware)


@app.middleware("http")
async def artifact_headers(request: Request, call_next):
    """Keep artifact responses out of search indexes, shared caches and referrers."""
    response = await call_next(request)
    # Every response, not just /a/: a capability URL *is* the credential, and
    # the hub's pages pull their fonts from a third-party origin. Under a
    # browser's default policy that stylesheet request carries the referring
    # page's origin, and a link a reader follows out of a document carries the
    # full URL -- share id included. "no-referrer" is affordable here because
    # nothing this service does needs a Referer: its own fetches are
    # same-origin and authenticate with headers, not with where they came from.
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/a/"):
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        # setdefault, not assignment: a handler that has already asked for
        # something stricter (GET /a/{id}/live sends "no-store", so no
        # intermediary can ever answer a change-detection poll from a cache)
        # keeps what it set.
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


@app.exception_handler(BackendError)
async def backend_error_handler(request: Request, exc: BackendError) -> Response:
    """Any unhandled Storage failure surfaces as 502, never as a 500."""
    logger.error("Storage backend failure on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=502,
        content={"error": "storage backend unavailable", "detail": str(exc)},
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def base_url(request: Request) -> str:
    """Absolute base URL of this service as seen by the client.

    Same precedence as the :func:`public_origin` middleware: the configured
    public base URL, then the forwarded headers but only when they are trusted
    (see :func:`_forwarded_trusted`), then the request as it arrived.
    """
    if settings.public_base_url:
        return settings.public_base_url
    scheme = request.url.scheme
    host = request.url.netloc
    if _forwarded_trusted():
        scheme = request.headers.get("x-forwarded-proto") or scheme
        host = request.headers.get("x-forwarded-host") or host
    return f"{scheme.split(',')[0].strip()}://{host.split(',')[0].strip()}"


def artifact_urls(base: str, share_id: str) -> dict[str, str]:
    """Every public URL of one artifact, built from its **share id**.

    Since 0.7.0 ``/a/{...}`` addresses an artifact by the public share id its
    meta record publishes, not by the internal artifact id the ``/api/*``
    routes use. The two are equal until the owner rotates the link, after which
    only the share id resolves publicly — so every URL the API hands out has to
    be built from :attr:`~src.store.ArtifactMeta.share_id`.
    """
    root = f"{base.rstrip('/')}/a/{share_id}"
    return {
        "url": root,
        "raw_url": f"{root}/raw",
        "source_url": f"{root}/source",
        "meta_url": f"{root}/meta",
        "versions_url": f"{root}/versions",
    }


def client_credential(request: Request) -> str:
    """The caller's credential, from the one header its kind belongs in.

    Each kind of credential has exactly one home, the same one it has on a
    Keboola stack:

    * ``X-StorageApi-Token`` — a Storage API token, and nothing else.
    * ``Authorization: Bearer`` — a programmatic bearer (``kbc_at_*`` session,
      ``kbc_pat_*`` personal access token), which additionally names its
      project in ``X-Storage-Project``.

    Putting one in the other's header is refused rather than quietly accepted.
    They are different kinds of credential with different scopes, and a header
    called ``X-StorageApi-Token`` holding a session token is exactly the
    confusion that makes people unable to find where a bearer goes. The 400
    names the header the value does belong in, so the mistake teaches its own
    fix.

    Two credentials at once is refused for a different reason: whichever the
    hub picked, the caller believed it was using the other.
    """
    bearer = ""
    raw = request.headers.get("authorization", "").strip()
    if raw[:7].lower() == "bearer ":
        bearer = raw[7:].strip()
    header = request.headers.get("x-storageapi-token", "").strip()
    if bearer and header:
        raise HTTPException(
            status_code=400,
            detail=(
                "Send one credential: either X-StorageApi-Token with a "
                "Storage API token, or Authorization: Bearer with a Keboola "
                "sign-in token — not both."
            ),
        )
    if header and is_bearer_credential(header):
        raise HTTPException(
            status_code=400,
            detail=(
                "That is a Keboola sign-in token, not a Storage API token. "
                "Send it as 'Authorization: Bearer <token>', with the project "
                "it acts as in X-Storage-Project."
            ),
        )
    if bearer and not is_bearer_credential(bearer):
        raise HTTPException(
            status_code=400,
            detail=(
                "Authorization: Bearer carries a Keboola sign-in token "
                "(kbc_at_… or kbc_pat_…). Send a Storage API token as "
                "'X-StorageApi-Token: <token>' instead."
            ),
        )
    if not bearer and not header and request.headers.get("x-storage-project"):
        # A signed-in caller always sends both; a project header arriving
        # alone means the credential was dropped in transit, and the likeliest
        # dropper is a proxy in front of this hub -- the same thing that
        # already strips X-Kbc-*. Saying so beats "you sent no credential",
        # which is what this otherwise looks like from in here.
        raise HTTPException(
            status_code=401,
            detail=(
                "X-Storage-Project arrived without a credential. If you sent "
                "Authorization: Bearer, something between you and this hub "
                "dropped it — GET /health/headers lists the headers that "
                "actually arrived."
            ),
        )
    return bearer or header


def credential_project(request: Request) -> int | None:
    """The project a signed-in credential is acting as, if one was named.

    Only a programmatic bearer needs it — a Storage token names its own
    project — so an absent header is not an error here; :func:`verify_token`
    is the one that insists when the credential cannot do without it.
    """
    raw = request.headers.get("x-storage-project", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="X-Storage-Project must be a Keboola project id (an integer)",
        ) from exc


def require_owner(request: Request) -> tuple[Owner, str]:
    """Authenticate the caller and return (caller identity, raw token).

    The raw token is returned because the canonical copy of the artifact is
    uploaded to the caller's own project with it. It is never stored or logged.
    """
    token = client_credential(request)
    # Primary header is X-Storage-Stack: the platform proxy in front of
    # deployed data apps strips X-Kbc-* headers, so that name never arrives.
    # X-Kbc-Stack is kept as an alias for direct/local access.
    raw_stack = request.headers.get("x-storage-stack", "") or request.headers.get(
        "x-kbc-stack", ""
    )
    try:
        stack_url = resolve_stack(raw_stack, settings.extra_stacks)
    except StackError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        owner = verify_token(
            stack_url,
            token,
            settings.token_verify_timeout_s,
            credential_project(request),
        )
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except StackUnreachableError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return owner, token


def optional_caller(request: Request) -> Owner | None:
    """Identify the caller from the management headers when they are present.

    Used by the read path to decide whether a *proposed* version may be shown.
    Anonymous reads are the norm here, so a missing or unusable credential is
    simply "no identity" rather than an error.
    """
    raw_stack = request.headers.get("x-storage-stack", "") or request.headers.get(
        "x-kbc-stack", ""
    )
    try:
        token = client_credential(request)
    except HTTPException:
        # Two conflicting credentials on a public read is one more unusable
        # credential, not a reason to refuse the read.
        return None
    if not token or not raw_stack:
        return None
    try:
        stack_url = resolve_stack(raw_stack, settings.extra_stacks)
        return verify_token(
            stack_url,
            token,
            settings.token_verify_timeout_s,
            credential_project(request),
        )
    except (StackError, AuthError, StackUnreachableError, HTTPException) as exc:
        # HTTPException lands here too: a malformed X-Storage-Project is one
        # more unusable credential on a path whose norm is no credential at
        # all, and a public read must not turn into a 400 over it.
        logger.info("Ignoring unusable read credentials: %s", exc)
        return None


def unlock_cookie_name(meta: ArtifactMeta) -> str:
    """Name of the unlock cookie for one artifact.

    Keyed by the **share id**, because a cookie is only sent back when its path
    prefixes the request path — and the path the browser sees is
    ``/a/{share_id}``. The signed *value* still carries the internal artifact
    id (see :func:`unlock_artifact`), so the cookie identifies an artifact, not
    a URL. A rotated link therefore also drops every unlock cookie: the new
    path has no cookie yet, and readers unlock again.
    """
    return f"art_{meta.share_id}"


def password_scope(meta: ArtifactMeta) -> str:
    """Cookie scope binding an unlock cookie to the *current* password record.

    A cookie signed under the previous password no longer verifies once the
    owner replaces or clears the password, because the scope mixed into the
    signature changed. Only a prefix of the stored PBKDF2 digest is used: it
    is already a one-way hash, never leaves the server, and a prefix is enough
    to change whenever the record does.
    """
    record = meta.password
    if not isinstance(record, dict):
        return "open"
    digest = record.get("hash")
    if not isinstance(digest, str) or not digest:
        return "open"
    return digest[:16]


def reader_allowed(meta: ArtifactMeta, request: Request) -> bool:
    """True when the caller may read a (possibly password-protected) artifact.

    Raises ``HTTPException`` 429 when this client has burnt its hourly budget
    of failed password attempts on this artifact — a wrong password is cheap
    to send and expensive (PBKDF2) to check.
    """
    if not meta.password:
        return True
    # The cookie is checked first: it is a cheap HMAC, and a reader who
    # already unlocked must never be caught by the brute-force throttle.
    cookie = request.cookies.get(unlock_cookie_name(meta))
    if cookie and request.app.state.signer.check(
        meta.id, cookie, settings.unlock_cookie_max_age_s, password_scope(meta)
    ):
        return True
    supplied = request.headers.get("x-artifact-password")
    if not supplied:
        return False
    client_ip = _client_ip(request)
    # Throttle buckets are keyed by the *internal* id, so rotating the link
    # does not hand an attacker a fresh budget.
    if _unlock_throttled(request.app, meta.id, client_ip):
        raise HTTPException(
            status_code=429,
            detail=(
                "too many wrong passwords for this artifact; at most "
                f"{settings.max_unlock_attempts_per_hour} failed attempts per "
                "hour are allowed from one address, and "
                f"{settings.max_unlock_attempts_per_artifact_per_hour} from "
                "all addresses together"
            ),
        )
    if check_password(supplied, meta.password):
        return True
    _record_unlock_failure(request.app, meta.id, client_ip)
    return False


def may_see(meta: ArtifactMeta, envelope: Envelope, caller: Owner | None) -> bool:
    """True unless this is someone else's proposal.

    Proposals are moderated content: only the artifact owner and the version's
    own author may read them until the owner promotes them.
    """
    if envelope.status != STATUS_PROPOSED:
        return True
    if caller is None:
        return False
    return caller.key in (meta.owner_key, envelope.author_key)


@app.get(
    "/health/headers",
    tags=["service"],
    summary="Diagnostic: header names received by the app",
    description=(
        "Lists the names (never the values) of request headers that reached "
        "this process. Exists to detect reverse proxies that silently strip "
        "custom headers such as X-StorageApi-Token or X-Storage-Stack, which "
        "would otherwise break header-based authentication in a way that is "
        "hard to diagnose from the outside."
    ),
    responses={
        200: {
            "description": (
                "JSON object with 'received_header_names': the sorted header "
                "names of this request, values omitted."
            )
        }
    },
)
def health_headers(request: Request) -> dict[str, Any]:
    """Diagnostic: names of request headers that reached the app.

    Values are never echoed — this exists to detect reverse proxies that strip
    custom headers (which would silently break header-based authentication).
    """
    return {"received_header_names": sorted(request.headers.keys())}


def _framed(
    request: Request,
    meta: ArtifactMeta,
    envelope: Envelope,
    *,
    pinned: bool = False,
) -> HTMLResponse:
    """One version, wrapped in the zero-chrome sandboxed-iframe page.

    The browser-facing read paths never hand a publisher's document to the
    hub's own origin any more: the artifact runs inside an iframe sandboxed
    without ``allow-same-origin``, i.e. in an opaque origin, so its scripts
    cannot reach the ``sessionStorage`` that ``/admin`` and ``/a/{id}/review``
    use for a visitor's Storage token. ``frame-ancestors 'self'`` keeps the
    wrapper itself from being embedded elsewhere; no other CSP directive is
    set, because the artifact inside ``srcdoc`` must keep rendering exactly as
    published. Machines that need the bytes use ``/a/{id}/raw``.

    The wrapper also carries the live-update shell, which polls
    ``GET /a/{id}/live`` and swaps a new head version in (or offers a banner —
    see :func:`~src.pages.artifact_frame_page`). ``pinned`` marks the
    ``/a/{id}/v/{n}`` route, where the reader asked for one specific version
    and the document is therefore never swapped underneath them.
    """
    return HTMLResponse(
        artifact_frame_page(
            envelope.title,
            envelope.html,
            base_url=base_url(request),
            share_id=meta.share_id,
            pinned_version=envelope.version if pinned else None,
        ),
        headers={"Content-Security-Policy": "frame-ancestors 'self'"},
    )


def _not_found(artifact_id: str) -> JSONResponse:
    """Identical answer whether the artifact never existed or was deleted."""
    return JSONResponse(
        status_code=404,
        content={"error": "artifact not found", "id": artifact_id},
    )


def _version_not_found(artifact_id: str, version: int) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "error": "version not found",
            "id": artifact_id,
            "version": version,
        },
    )


def _password_required() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={
            "error": "password required",
            "hint": "send X-Artifact-Password header",
        },
    )


def _proposal_hidden(artifact_id: str, version: int) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={
            "error": "proposed version is not public",
            "detail": (
                "Proposed versions stay private until the artifact owner "
                "promotes them. Only the owner and the version's author can "
                "read one, by sending X-StorageApi-Token and X-Storage-Stack."
            ),
            "id": artifact_id,
            "version": version,
        },
    )


def _thread_not_found(artifact_id: str, thread_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "error": "comment thread not found",
            "id": artifact_id,
            "thread_id": thread_id,
        },
    )


def _document_frozen(meta: ArtifactMeta, what: str) -> JSONResponse:
    """409 for any write frozen by the artifact's status.

    Two very different states freeze an artifact, and telling them apart is the
    whole point of this answer: ``final`` is a deliberate "this is done", while
    ``trashed`` means the artifact is in the trash and its public link is dead.
    The caller needs to know which, because the way out differs — reopen with
    ``PUT /api/artifacts/{id}`` versus restore with
    ``POST /api/artifacts/{id}/restore``.
    """
    if meta.is_trashed():
        return JSONResponse(
            status_code=409,
            content={
                "error": "document is trashed",
                "detail": (
                    f"This artifact is in the trash, so {what} are frozen and "
                    "its public link no longer resolves. Its owner can bring "
                    "it back with POST /api/artifacts/{id}/restore."
                ),
                "id": meta.id,
            },
        )
    return JSONResponse(
        status_code=409,
        content={
            "error": "document is final",
            "detail": (
                f"This artifact is marked final, so {what} are frozen. Its "
                "owner can reopen it with PUT /api/artifacts/{id} and "
                '{"status": "draft"}.'
            ),
            "id": meta.id,
        },
    )


def _comments_closed(meta: ArtifactMeta) -> JSONResponse:
    """403 for a caller the comment policy does not admit."""
    if meta.comments_mode == "off":
        detail = (
            "commenting is closed on this artifact; its owner can reopen it "
            "with comments_mode 'anyone' or 'allowlist'"
        )
    else:
        detail = (
            "this artifact only accepts comments from projects on its "
            "contributor allowlist; ask its owner to add your project"
        )
    return JSONResponse(
        status_code=403,
        content={"error": "comments not allowed", "detail": detail, "id": meta.id},
    )


def _meta_of(request: Request, artifact_id: str) -> ArtifactMeta | None:
    """Fetch an artifact's meta record, hydrating the index first when needed.

    Takes an **internal** artifact id — the ``/api/*`` handle. Public routes go
    through :func:`_public_meta_of`, which resolves the share id first.
    """
    ensure_hydrated(request.app)
    return request.app.state.store.get_meta(artifact_id)


def _public_meta_of(request: Request, public_id: str) -> ArtifactMeta | None:
    """Resolve a public ``/a/{...}`` identifier and load its meta record.

    ``public_id`` is a *share id*: what the URL a reader holds actually
    carries. :meth:`~src.store.ArtifactStore.resolve_share` maps it to the
    internal artifact id, and answers ``None`` for everything that must not
    resolve publicly — a rotated-away link, the bare artifact id of a rotated
    artifact, an artifact in the trash, or an identifier naming nothing. All
    four are the same 404 to the caller, deliberately: distinguishing them
    would turn the endpoint into an oracle for revoked links.

    Every public handler starts here and then works with ``meta.id``; nothing
    downstream ever sees the share id again except URL building.
    """
    ensure_hydrated(request.app)
    artifact_id = request.app.state.store.resolve_share(public_id)
    if artifact_id is None:
        return None
    return request.app.state.store.get_meta(artifact_id)


def _comment_target_of(
    request: Request, path_id: str, caller: Owner | None = None
) -> ArtifactMeta | None:
    """Resolve the identifier in a comment-write path, *share id first*.

    The comment write routes are the one place where both halves of an
    artifact's identity pair are legitimate. The review UI, a capability-URL
    holder and every invited guest only ever saw the **share id**, because that
    is what ``/a/{...}`` carries; an owner also holds the internal id (it is
    what the publish response and ``/api/artifacts`` hand them).

    The share id is therefore resolved the way every other public ``/a/`` path
    resolves one — through :meth:`~src.store.ArtifactStore.resolve_share`, which
    answers ``None`` for a rotated-away link, for the bare internal id of a
    rotated artifact, and for an artifact in the trash. Looking the internal id
    up first (as this helper used to) quietly defeated that: because a fresh
    artifact's share id *is* its internal id, the original public identifier
    kept working for comment writes forever, and rotation stopped being
    revocation for anybody who had seen the link.

    The internal id survives only as an **owner** fallback, and only for a
    token-authenticated caller who owns the artifact — the API ergonomics an
    agent depends on, with none of the public reach. A guest, a stranger's
    token and an anonymous caller all get ``None`` (a 404) for it.
    """
    ensure_hydrated(request.app)
    store = request.app.state.store
    internal_id = store.resolve_share(path_id)
    if internal_id is not None:
        return store.get_meta(internal_id)
    if caller is None:
        return None
    meta = store.get_meta(path_id)
    if meta is None or meta.owner_key != caller.key:
        return None
    return meta


# ------------------------------------------------------------ guest invitations


def _guest_credential(request: Request) -> tuple[str, str] | None:
    """Split the ``X-Artifact-Guest`` header into ``(invitation_id, secret)``.

    ``None`` means "this caller is not presenting a guest credential at all" —
    the header is absent or blank — which is what sends the request down the
    ordinary token-authenticated path. A header that is *present* but
    unparseable is a guest whose link got mangled in a chat client, so it
    raises the guest 401 rather than falling through to token auth and
    answering something about Storage stacks they have never heard of.
    """
    raw = request.headers.get(GUEST_HEADER.lower(), "").strip()
    if not raw:
        return None
    invitation_id, separator, secret = raw.partition(".")
    if not separator or not invitation_id or not secret:
        raise _guest_refused()
    return invitation_id, secret


def _guest_refused() -> HTTPException:
    """The single 401 every guest-credential failure answers with.

    Malformed, unknown, revoked and wrong-secret are deliberately one answer:
    telling them apart would turn the header into an oracle over other
    people's invitations.
    """
    return HTTPException(
        status_code=401,
        detail=(
            "this invitation link is not valid for this artifact; it may have "
            "been revoked, or the link may be incomplete — ask whoever "
            "invited you for a fresh one"
        ),
    )


def _verify_guest(meta: ArtifactMeta, credential: tuple[str, str]) -> dict:
    """Return the invitation this credential proves, or raise 401.

    Three things have to hold: the invitation still exists on this artifact, it
    has not been revoked, and the secret verifies against its stored PBKDF2
    record. All three failures answer :func:`_guest_refused` — a guest must not
    be able to tell "revoked" from "never existed" from "wrong secret".
    """
    invitation_id, secret = credential
    for invitation in meta.invitations or []:
        if invitation.get("id") != invitation_id:
            continue
        if invitation.get("revoked"):
            break
        record = invitation.get("secret")
        if isinstance(record, dict) and check_password(secret, record):
            return invitation
        break
    raise _guest_refused()


def _verify_guest_checked(
    request: Request, meta: ArtifactMeta, credential: tuple[str, str]
) -> dict:
    """:func:`_verify_guest` behind the same failed-attempt budget as unlock.

    Every route that accepts an ``X-Artifact-Guest`` credential goes through
    here rather than calling :func:`_verify_guest` directly. Only *failures*
    are counted, so an invited guest working normally is never affected, while
    somebody grinding secrets against a known invitation id gets 429 long
    before they have spent much of the hub's CPU on PBKDF2.

    Buckets are keyed by the *internal* artifact id, so rotating the link (or
    addressing the artifact by its other identifier) does not hand an attacker
    a fresh budget.
    """
    client_ip = _client_ip(request)
    if _guest_throttled(request.app, meta.id, client_ip):
        raise HTTPException(
            status_code=429,
            detail=(
                "too many rejected invitation credentials for this artifact; "
                f"at most {settings.max_unlock_attempts_per_hour} failed "
                "attempts per hour are allowed from one address, and "
                f"{settings.max_unlock_attempts_per_artifact_per_hour} from "
                "all addresses together"
            ),
        )
    try:
        return _verify_guest(meta, credential)
    except HTTPException:
        _record_guest_failure(request.app, meta.id, client_ip)
        raise


def _guest_identity(invitation: dict) -> dict:
    """The author record stored beside a guest's comment."""
    return guest_author(
        str(invitation.get("id") or ""), str(invitation.get("name") or "")
    )


def _guest_actor(invitation: dict) -> str:
    """How a guest is named in a webhook notification."""
    name = str(invitation.get("name") or "").strip() or "someone"
    return f"{name} (guest)"


def _public_invitation(invitation: dict) -> dict:
    """One invitation as its owner may see it — never including the secret."""
    return {
        "id": str(invitation.get("id") or ""),
        "name": str(invitation.get("name") or ""),
        "created_at": str(invitation.get("created_at") or ""),
        "revoked": bool(invitation.get("revoked")),
    }


def _make_room_for_invitation(meta: ArtifactMeta) -> None:
    """Ensure one more invitation fits, or raise 422.

    The cap bounds the meta record, which is a single Storage File rewritten on
    every artifact change — an unbounded list would eventually make the
    artifact itself expensive to load. Revoked invitations are tombstones with
    no remaining use (their secret can never verify again), so the oldest of
    them are dropped to make room before the cap is enforced. That is what
    makes "revoke somebody, invite somebody else" work indefinitely while the
    stored list stays bounded.
    """
    limit = settings.max_invitations_per_artifact
    if len(meta.invitations) < limit:
        return
    live = [inv for inv in meta.invitations if not inv.get("revoked")]
    # Oldest revoked first — the list keeps creation order.
    droppable = [str(inv.get("id")) for inv in meta.invitations if inv.get("revoked")]
    room_needed = len(meta.invitations) - limit + 1
    if room_needed > len(droppable):
        raise HTTPException(
            status_code=422,
            detail=(
                f"this artifact already has {len(live)} live invitations, "
                f"which is the limit of {limit} per artifact; revoke one "
                "before inviting somebody else"
            ),
        )
    dropped = set(droppable[:room_needed])
    meta.invitations = [
        inv for inv in meta.invitations if str(inv.get("id")) not in dropped
    ]


def _owner_only(meta: ArtifactMeta, caller: Owner) -> None:
    """Raise 403 unless the caller's project owns the artifact."""
    if meta.owner_key != caller.key:
        raise HTTPException(
            status_code=403, detail="this artifact belongs to another project"
        )


def _destructive_authority(owner: Owner) -> None:
    """Raise 403 unless this token may run a *destructive* route.

    SEC-075-011. ``_owner_only`` above answers "is this the owning project?",
    which is a project-level question: every Storage token of that project —
    including a read-only or single-purpose one — passes it. That is fine for
    a route that changes a setting and reversible for one that moves an
    artifact to the trash, but it also meant any such token could purge an
    artifact outright, rotate its public link, or delete a version, with no
    way for an operator to narrow that.

    ``HUB_DESTRUCTIVE_TOKEN_POLICY`` is that narrowing, and it is opt-in:

    * ``project`` (default) — the historical behaviour, unchanged. Every token
      of the owning project keeps full destructive authority.
    * ``admin`` — the token must additionally be a master token or belong to a
      project user whose ``admin.role`` is ``admin``.
    * ``allowlist`` — the token's own id must appear in
      ``HUB_DESTRUCTIVE_TOKEN_IDS``.

    This gate applies to the irreversible or link-breaking routes only: soft
    delete, purge, rotate-link, version delete, and webhook key rotation.
    **Non-destructive owner routes are deliberately not affected** — update,
    head pin, promote, settings, invitations, stats and trash restore stay
    project-authorized under every policy, because narrowing them would turn a
    security control into a workflow blocker without removing anything an
    attacker could not simply do again.

    The 403 detail names the active policy and what the credential lacked. It
    never echoes the token, and never the allowlist either: an outsider must
    not learn which token ids would work.
    """
    policy = settings.destructive_token_policy
    if policy == "project":
        return
    if policy == "admin":
        if owner.is_project_admin:
            return
        raise HTTPException(
            status_code=403,
            detail=(
                "this hub runs destructive_token_policy=admin: a destructive "
                "operation needs a master token, or a token belonging to a "
                "project user with the admin role. This token is neither."
            ),
        )
    if policy == "allowlist":
        if owner.token_id and owner.token_id in settings.destructive_token_ids:
            return
        raise HTTPException(
            status_code=403,
            detail=(
                "this hub runs destructive_token_policy=allowlist: a "
                "destructive operation needs a token whose id the operator "
                "listed in HUB_DESTRUCTIVE_TOKEN_IDS. This token's id is not "
                "listed."
            ),
        )
    # Unreachable: load_settings rejects any other value at startup. Refusing
    # rather than falling through keeps a future third policy from defaulting
    # to "allow everything" if somebody adds it here and forgets this branch.
    raise HTTPException(
        status_code=403,
        detail="destructive operations are disabled by this hub's token policy",
    )


def _emit_webhook(
    request: Request,
    meta: ArtifactMeta,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    actor: Owner | None = None,
    actor_name: str | None = None,
) -> None:
    """Queue one webhook delivery per URL this artifact registered.

    A no-op when the artifact registered nothing, which is the common case, so
    the cost of the feature on an ordinary publish is one attribute read.

    What goes into the payload is deliberately narrow: the *internal* artifact
    id (the handle its owner already knows), the title, whatever version or
    thread the event is about, the acting project's **name** — never a token,
    never an owner key, never a password record — and the public ``url``, built
    from the share id. That last one is the only capability in the envelope,
    and it is the very link whose owner configured the receiver.

    Never raises into the request path: a webhook is a notification about
    something that already happened and is already durable in Storage.
    """
    urls = list(meta.webhooks or [])
    if not urls:
        return
    dispatcher = getattr(request.app.state, "webhooks", None)
    if dispatcher is None:
        return
    # SEC-100-006 follow-up: the dispatcher's epoch cache starts empty on
    # every process start (see WebhookDispatcher._epochs) and used to be
    # seeded only when an owner called GET .../webhooks or the rotate-key
    # route -- so after a restart, a receiver that had already been rotated
    # was signed with the legacy, epoch-less key until its owner happened to
    # look, and would reject the genuinely current delivery. The persisted
    # record (ArtifactMeta.webhook_key_epochs) is the source of truth on
    # every emit, not just on a read or a rotation; reseeding it here is one
    # dict assignment per receiver and keeps the live cache always current.
    epochs = meta.webhook_key_epochs or {}
    for url in urls:
        dispatcher.seed_epoch(meta.id, url, epochs.get(url))
    body: dict[str, Any] = {
        "artifact_id": meta.id,
        "url": f"{base_url(request).rstrip('/')}/a/{meta.share_id}",
        **(payload or {}),
    }
    if actor is not None:
        body["actor"] = actor.project_name
    elif actor_name:
        # A guest has no project to name, so the receiver gets the display name
        # their inviter chose, marked as a guest. Still not a capability: it is
        # a label, and it is the one the artifact's own owner typed.
        body["actor"] = actor_name
    try:
        dispatcher.emit(
            urls,
            WebhookEvent(
                artifact_id=meta.id, kind=kind, payload=body, created_at=_now()
            ),
        )
    except Exception as exc:  # noqa: BLE001 - notifications never break a write
        logger.warning(
            "Could not queue the %s webhook for artifact %s: %s", kind, meta.id, exc
        )


def _validate_webhooks(urls: list[str]) -> list[str]:
    """Clean and validate a webhook URL list, or raise 422.

    Each entry goes through :func:`src.webhooks.validate_webhook_url`, which
    enforces https and refuses hosts resolving into private, loopback,
    link-local, reserved or cloud-metadata ranges — the same SSRF guard git
    clones get. A refusal is a 422 carrying the validator's own sentence, so
    the owner learns *why* rather than just "invalid".
    """
    cleaned: list[str] = []
    for raw in urls:
        candidate = str(raw).strip()
        if not candidate:
            continue
        try:
            normalized = validate_webhook_url(candidate)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if normalized not in cleaned:
            cleaned.append(normalized)
    if len(cleaned) > settings.max_webhooks_per_artifact:
        raise HTTPException(
            status_code=422,
            detail=(
                f"too many webhooks ({len(cleaned)}); the limit is "
                f"{settings.max_webhooks_per_artifact} per artifact"
            ),
        )
    return cleaned


# --------------------------------------------------------------------------
# Request bodies
# --------------------------------------------------------------------------


class PublishBody(BaseModel):
    """Body of ``POST /api/artifacts``: exactly one content field is required.

    Provide exactly one of ``html``, ``markdown`` or ``git_url``.
    ``git_token``/``git_username`` are transient clone credentials for a
    private repository: like the Storage token, they are used during the
    request and never stored, logged or echoed back.
    """

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "markdown": (
                        "# Q3 review\n\n"
                        "Shipped the new ingest path.\n\n"
                        "| metric | Q2 | Q3 |\n|---|---|---|\n"
                        "| runs | 120 | 184 |\n"
                    ),
                    "title": "Q3 review",
                    "accept_versions": True,
                }
            ]
        }
    }

    html: str | None = Field(
        None,
        description=(
            "Complete HTML document, served as-is. Mutually exclusive with "
            "'markdown' and 'git_url'."
        ),
    )
    markdown_source: str | None = Field(
        None,
        description=(
            "The same document in Markdown, kept as the version's source. "
            "Only valid together with 'html' (422 otherwise) and it does not "
            "change what is served — the rendered document stays exactly the "
            "HTML you submitted. Supply it whenever you publish HTML: other "
            "agents read documents through /a/{id}/export/markdown and "
            "/a/{id}/source, and without it they get a conversion of your "
            "HTML that loses charts, images and fine structure."
        ),
    )
    markdown: str | None = Field(
        None,
        description=(
            "Markdown source, rendered by the hub's built-in template (GFM "
            "tables, task lists, mermaid fences, syntax highlighting). "
            "Mutually exclusive with 'html' and 'git_url'."
        ),
    )
    git_url: str | None = Field(
        None,
        description=(
            "HTTPS git repository to clone (public, or private with "
            "'git_token'). Mutually exclusive with 'html' and 'markdown'."
        ),
    )
    git_ref: str | None = Field(
        None,
        description=(
            "Optional branch or tag to check out for 'git_url'. Not a commit "
            "id: the clone resolves this with git's --branch, which takes a "
            "branch or a tag only, and a commit id is refused with 422. Tag "
            "the commit to pin an immutable source."
        ),
    )
    git_path: str | None = Field(
        None,
        description=(
            "Optional entry file or directory inside the repository. "
            "Defaults to index.html, then README.md, then a single root "
            "*.html file."
        ),
    )
    git_username: str | None = Field(
        None,
        description=(
            "Username for git hosts that require one alongside 'git_token'. "
            "Defaults to 'x-access-token', which works for GitHub PATs and "
            "GitLab deploy tokens. Only valid together with 'git_url' (422 "
            "otherwise)."
        ),
    )
    git_token: str | None = Field(
        None,
        description=(
            "Personal access token for the git host (GitHub PAT, GitLab "
            "token, ...), used to clone a private repository. Transient: "
            "used only for the clone during this request, never stored, "
            "logged or returned. Only valid together with 'git_url' (422 "
            "otherwise)."
        ),
    )
    title: str | None = Field(
        None, description="Optional title; derived from the content when omitted."
    )
    password: str | None = Field(
        None,
        description=(
            "Optional reader password. Readers unlock via the "
            "X-Artifact-Password header or the web unlock form."
        ),
    )
    accept_versions: bool = Field(
        False,
        description=(
            "When true, any other Keboola project may submit versions of this "
            "artifact. Submissions from other projects always land as "
            "moderated proposals that only you can promote. Default false: "
            "only the owning project may add versions."
        ),
    )


class UpdateBody(BaseModel):
    """Body of ``PUT /api/artifacts/{id}``: every field is optional.

    At most one content field (``html``, ``markdown``, ``git_url``) may be
    given; omit all of them to leave the content unchanged. A content field
    adds a new live version — nothing is ever overwritten.
    """

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "markdown": "# Q3 review\n\nCorrected the Q3 run count.\n",
                    "title": "Q3 review (corrected)",
                    "accept_versions": True,
                }
            ]
        }
    }

    html: str | None = Field(
        None,
        description=(
            "Complete HTML document, served as-is. At most one of 'html', "
            "'markdown', 'git_url' may be given."
        ),
    )
    markdown_source: str | None = Field(
        None,
        description=(
            "The same document in Markdown, kept as the version's source. "
            "Only valid together with 'html' (422 otherwise) and it does not "
            "change what is served — the rendered document stays exactly the "
            "HTML you submitted. Supply it whenever you publish HTML: other "
            "agents read documents through /a/{id}/export/markdown and "
            "/a/{id}/source, and without it they get a conversion of your "
            "HTML that loses charts, images and fine structure."
        ),
    )
    markdown: str | None = Field(
        None,
        description=(
            "Markdown source, rendered by the hub's built-in template. At "
            "most one of 'html', 'markdown', 'git_url' may be given."
        ),
    )
    git_url: str | None = Field(
        None,
        description=(
            "HTTPS git repository to clone. At most one of 'html', "
            "'markdown', 'git_url' may be given."
        ),
    )
    git_ref: str | None = Field(
        None,
        description=(
            "Optional branch or tag to check out for 'git_url'. Not a commit "
            "id: the clone resolves this with git's --branch, which takes a "
            "branch or a tag only, and a commit id is refused with 422. Tag "
            "the commit to pin an immutable source."
        ),
    )
    git_path: str | None = Field(
        None,
        description=(
            "Optional entry file or directory inside the repository; see "
            "the same field on the publish body for the resolution order."
        ),
    )
    git_username: str | None = Field(
        None,
        description=(
            "Username for git hosts that require one. Only valid together "
            "with 'git_url' (422 otherwise)."
        ),
    )
    git_token: str | None = Field(
        None,
        description=(
            "Personal access token for the git host; transient, never "
            "stored, logged or returned. Only valid together with "
            "'git_url' (422 otherwise). Must be resent on every update that "
            "re-publishes from a private repository."
        ),
    )
    title: str | None = Field(
        None,
        description=(
            "Title of the new version. A title lives on a version, so this "
            "is only valid together with a content field (422 otherwise)."
        ),
    )
    password: str | None = Field(
        None, description="Set or replace the reader password."
    )
    clear_password: bool = Field(
        False, description="When true, remove any existing reader password."
    )
    accept_versions: bool | None = Field(
        None,
        description=(
            "Legacy two-state switch for version contributions: true means "
            "'anyone', false means 'off'. Prefer 'accept_versions_mode'; "
            "sending both is a 422. Omit to leave unchanged."
        ),
    )
    accept_versions_mode: str | None = Field(
        None,
        description=(
            "Who may submit versions: 'off' (owner only), 'anyone' (any "
            "verified Keboola project, moderated as proposals) or "
            "'allowlist' (only the projects in 'contributors'). Omit to "
            "leave unchanged."
        ),
    )
    contributors: list[str] | None = Field(
        None,
        description=(
            "Owner keys allowed by the 'allowlist' modes, each shaped "
            "'{project_id}@{stack hostname}' (for example "
            f"'123@connection.keboola.com'). At most {MAX_CONTRIBUTORS} "
            "entries. Replaces the whole list; omit to leave unchanged."
        ),
    )
    comments_mode: str | None = Field(
        None,
        description=(
            "Who may open inline comment threads: 'anyone' (the default), "
            "'allowlist' (only the projects in 'contributors') or 'off'. "
            "The owner may always comment unless the artifact is final. "
            "Omit to leave unchanged."
        ),
    )
    status: str | None = Field(
        None,
        description=(
            "'draft' (the default) or 'final'. Marking an artifact final "
            "freezes it: new versions and new comments answer 409 for "
            "everyone, the owner included. Set it back to 'draft' to reopen. "
            "'trashed' is deliberately not settable here — use DELETE "
            "/api/artifacts/{id} and POST /api/artifacts/{id}/restore, which "
            "also record when it was trashed and what to restore it to. Omit "
            "to leave unchanged."
        ),
    )
    webhooks: list[str] | None = Field(
        None,
        description=(
            "https URLs notified when something happens to this artifact "
            "(version published or proposed, proposal promoted, comment or "
            "reply, finalized, trashed, restored, link rotated). Each delivery "
            "is a small JSON envelope signed with X-Hub-Signature-256, keyed "
            "per receiver (read the keys from GET "
            "/api/artifacts/{id}/webhooks); a "
            "hooks.slack.com URL gets Slack's {\"text\": ...} shape instead. "
            "Replaces the whole list; [] clears it; omit to leave unchanged. "
            "URLs must be https and must not resolve to a private, loopback, "
            "link-local or metadata address. Treated as semi-secret: they are "
            "returned in this response only, never in GET /api/artifacts, "
            "which reports 'webhooks_count' instead."
        ),
    )


class CommentBody(BaseModel):
    """Body of ``POST /api/artifacts/{id}/comments``.

    The anchor is a W3C-style TextQuoteSelector captured from the *rendered*
    text of one version: the quote itself plus a little surrounding context so
    a repeated quote can still be told apart.
    """

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "version": 2,
                    "exact": "runs grew to 184",
                    "prefix": "In Q3 the ",
                    "suffix": " across every project.",
                    "body": "Is this the deduplicated count?",
                }
            ]
        }
    }

    version: int = Field(
        ...,
        description=(
            "Version the quote was taken from. The thread stays bound to it — "
            "there is no cross-version re-anchoring."
        ),
    )
    # SEC-100-004: every string here carries a length ceiling on the *field*,
    # so an oversized value is refused by request validation instead of
    # travelling all the way to the comment store. The values are the store's
    # own limits (src/comments.py), so nothing that used to be accepted is
    # rejected now; the anchor context shares the quote's ceiling because it
    # is the same rendered text, captured a few characters to either side.
    exact: str = Field(
        ...,
        max_length=MAX_QUOTE_CHARS,
        description=(
            "The quoted text itself, exactly as rendered. Must not be blank, "
            f"and at most {MAX_QUOTE_CHARS} characters."
        ),
    )
    prefix: str = Field(
        "",
        max_length=MAX_QUOTE_CHARS,
        description=(
            "Rendered text immediately before the quote (about 32 "
            "characters). Used to disambiguate a quote that occurs more than "
            "once."
        ),
    )
    suffix: str = Field(
        "",
        max_length=MAX_QUOTE_CHARS,
        description="Rendered text immediately after the quote (about 32 characters).",
    )
    body: str = Field(
        ...,
        max_length=MAX_BODY_CHARS,
        description=(
            "The comment itself, as plain text. Must not be blank, and at "
            f"most {MAX_BODY_CHARS} characters."
        ),
    )


class ReplyBody(BaseModel):
    """Body of ``POST /api/artifacts/{id}/comments/{tid}/replies``."""

    model_config = {
        "json_schema_extra": {
            "examples": [{"body": "Yes — deduplicated, same as Q2."}]
        }
    }

    # Same field-level ceiling as a thread body, for the same reason
    # (SEC-100-004).
    body: str = Field(
        ...,
        max_length=MAX_BODY_CHARS,
        description=(
            "The reply itself, as plain text. Must not be blank, and at most "
            f"{MAX_BODY_CHARS} characters."
        ),
    )


class ResolveBody(BaseModel):
    """Body of ``POST /api/artifacts/{id}/comments/{tid}/resolve``."""

    model_config = {"json_schema_extra": {"examples": [{"resolved": True}]}}

    resolved: bool = Field(
        True,
        description=(
            "true (the default, and what an empty body means) resolves the "
            "thread; false reopens a resolved one. Both are available to the "
            "artifact owner and to the thread's author."
        ),
    )


class InvitationBody(BaseModel):
    """Body of ``POST /api/artifacts/{id}/invitations``."""

    model_config = {"json_schema_extra": {"examples": [{"name": "Jana (legal)"}]}}

    name: str = Field(
        ...,
        min_length=1,
        max_length=MAX_INVITATION_NAME_CHARS,
        description=(
            "Who this invitation is for, as it will appear beside their "
            "comments. Purely a label chosen by the owner — the hub never "
            "verifies it and never emails it anywhere — but it is the only "
            "thing readers see about a guest, so make it recognisable."
        ),
    )


class VersionBody(BaseModel):
    """Body of ``POST /api/artifacts/{id}/versions``.

    Same content shape as publishing: exactly one of ``html``, ``markdown`` or
    ``git_url``, plus an optional note describing what changed.
    """

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "markdown": "# Q3 review\n\nFixed the Q3 totals.\n",
                    "note": "fix Q3 totals",
                }
            ]
        }
    }

    html: str | None = Field(None, description="Complete HTML document, served as-is.")
    markdown_source: str | None = Field(
        None,
        description=(
            "The same document in Markdown, kept as this version's source. "
            "Only valid together with 'html' (422 otherwise); it does not "
            "change what is served. Always send it when you submit HTML, so "
            "readers of /a/{id}/export/markdown and /a/{id}/source get your "
            "own Markdown rather than a lossy conversion of it."
        ),
    )
    markdown: str | None = Field(
        None, description="Markdown source, rendered by the built-in template."
    )
    git_url: str | None = Field(None, description="HTTPS git repository to clone.")
    git_ref: str | None = Field(
        None, description="Optional branch or tag (not a commit id)."
    )
    git_path: str | None = Field(
        None, description="Optional entry file or directory inside the repository."
    )
    git_username: str | None = Field(
        None, description="Only valid together with 'git_url' (422 otherwise)."
    )
    git_token: str | None = Field(
        None,
        description=(
            "Transient clone credential; only valid together with 'git_url'."
        ),
    )
    title: str | None = Field(
        None, description="Title of this version; derived from the content when omitted."
    )
    note: str | None = Field(
        None,
        max_length=MAX_NOTE_CHARS,
        description=(
            "Short description of what changed, shown in the version history. "
            f"At most {MAX_NOTE_CHARS} characters."
        ),
    )
    base_version: int | None = Field(
        None,
        description=(
            "The version this submission was written against. Must name a "
            "version that exists (422 otherwise). Recorded on the version and "
            "reported in the history, where a proposal whose base_version is "
            "no longer the head is flagged 'outdated': true — so a reviewer "
            "can see that the document moved on while the proposal was being "
            "written. Omit when you did not start from a specific version."
        ),
    )


class HeadBody(BaseModel):
    """Body of ``PUT /api/artifacts/{id}/head``."""

    model_config = {
        "json_schema_extra": {"examples": [{"mode": "pinned", "version": 2}]}
    }

    mode: str = Field(
        ...,
        description=(
            "'latest' to always serve the newest live version, or 'pinned' to "
            "serve one specific live version."
        ),
    )
    version: int | None = Field(
        None, description="Required (and must be a live version) when mode is 'pinned'."
    )


def _content_fields(body: PublishBody | UpdateBody | VersionBody) -> list[str]:
    """Names of the content fields present in a request body."""
    return [
        name
        for name in ("html", "markdown", "git_url")
        if getattr(body, name) is not None
    ]


def strip_git_userinfo(git_url: str) -> str:
    """Remove every credential-bearing part of a git URL: userinfo, query, fragment.

    A submitted ``https://user:token@github.com/org/repo`` used to be stored
    verbatim in the version envelope and echoed back through public metadata
    and version history, leaking the credential to every capability-URL
    holder. The builder already scrubs it out of clone output, but the
    envelope kept the original — so it is stripped here, before validation,
    storage or any response. Private clones do not need it: they authenticate
    with the separate, request-scoped ``git_token``/``git_username`` fields.

    The query string and fragment go the same way, and for the same reason: a
    secret hides just as well in ``?token=...`` (or ``#token=...``) as it does
    in the userinfo, and the stored envelope and the public git provenance
    (``/a/{id}/meta``, ``/a/{id}/versions``) would carry it verbatim. A clone
    URL needs neither component, so dropping both costs nothing and closes the
    remaining leak.
    """
    parsed = urlsplit(git_url)
    if not parsed.netloc:
        # Not a URL with an authority (e.g. an scp-style or relative form);
        # there is nothing to reliably split off, so leave it to validation.
        return git_url
    host = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def _normalize_git_url(body: PublishBody | UpdateBody | VersionBody) -> None:
    """Scrub ``body.git_url`` in place, before anything uses it.

    Runs before validation, cloning, storage and every response, so no route
    ever sees the userinfo, query or fragment the caller submitted.
    """
    if body.git_url is not None:
        body.git_url = strip_git_userinfo(str(body.git_url))


def _check_git_credentials(body: PublishBody | UpdateBody | VersionBody) -> None:
    """Reject git-only fields that were sent without a ``git_url``.

    Covers both the clone credentials and ``git_ref``/``git_path``: silently
    ignoring any of them would leave the caller believing a token was used, or
    a branch checked out, when it was not. So this is a hard 422.
    """
    if body.git_url is not None:
        return
    stray = [
        name
        for name in ("git_ref", "git_path", "git_username", "git_token")
        if getattr(body, name) is not None
    ]
    if stray:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{' and '.join(repr(name) for name in stray)} "
                f"{'is' if len(stray) == 1 else 'are'} only valid together with "
                "'git_url'"
            ),
        )


def _check_markdown_source(body: PublishBody | UpdateBody | VersionBody) -> None:
    """Reject ``markdown_source`` sent without an ``html`` document.

    It is the Markdown *of the submitted HTML*: with 'markdown' or 'git_url'
    there already is a source, and on its own there is no document it could
    belong to. Silently ignoring it would leave the caller believing their
    Markdown had been retained when it had not — so, exactly like a stray git
    field, this is a hard 422.
    """
    if body.markdown_source is not None and body.html is None:
        raise HTTPException(
            status_code=422,
            detail="'markdown_source' is only valid together with 'html'",
        )


def _validate_contributors(keys: list[str]) -> list[str]:
    """Clean and validate a contributor allowlist, or raise 422.

    Entries are owner keys (``{project_id}@{stack hostname}``); a typo would
    otherwise be stored as an entry that can never match anybody.
    """
    cleaned: list[str] = []
    for raw in keys:
        key = str(raw).strip()
        if not key:
            continue
        if not _CONTRIBUTOR_KEY.match(key):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"contributor {key!r} is not a project key; use "
                    "'{project_id}@{stack hostname}', for example "
                    "'123@connection.keboola.com'"
                ),
            )
        if key not in cleaned:
            cleaned.append(key)
    if len(cleaned) > MAX_CONTRIBUTORS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"too many contributors ({len(cleaned)}); the limit is "
                f"{MAX_CONTRIBUTORS}"
            ),
        )
    return cleaned


def _apply_policy(meta: ArtifactMeta, body: UpdateBody) -> None:
    """Apply the contribution/comment/status fields of a PUT onto ``meta``.

    Every unknown value is a 422 rather than a silent fallback, so an owner is
    never told "saved" about a policy the hub did not actually adopt.
    """
    if body.accept_versions is not None and body.accept_versions_mode is not None:
        raise HTTPException(
            status_code=422,
            detail=(
                "provide either 'accept_versions' (legacy boolean) or "
                "'accept_versions_mode', not both"
            ),
        )
    if body.accept_versions_mode is not None:
        if body.accept_versions_mode not in ACCEPT_MODES:
            raise HTTPException(
                status_code=422,
                detail=(
                    "accept_versions_mode must be one of "
                    f"{', '.join(ACCEPT_MODES)}"
                ),
            )
        meta.accept_versions_mode = body.accept_versions_mode
    elif body.accept_versions is not None:
        meta.accept_versions = bool(body.accept_versions)

    if body.contributors is not None:
        meta.contributors = _validate_contributors(body.contributors)

    if body.comments_mode is not None:
        if body.comments_mode not in COMMENTS_MODES:
            raise HTTPException(
                status_code=422,
                detail=f"comments_mode must be one of {', '.join(COMMENTS_MODES)}",
            )
        meta.comments_mode = body.comments_mode

    if body.webhooks is not None:
        meta.webhooks = _validate_webhooks(body.webhooks)

    if body.status is not None:
        # ARTIFACT_SETTABLE_STATUSES, not ARTIFACT_STATUSES: "trashed" is
        # reachable only through DELETE (which also stamps trashed_at and the
        # status to restore to) and left only through the restore route.
        # Accepting it here would produce a meta record that claims to be in
        # the trash without either of those, so it is a 422.
        if body.status not in ARTIFACT_SETTABLE_STATUSES:
            raise HTTPException(
                status_code=422,
                detail=(
                    "status must be one of "
                    f"{', '.join(ARTIFACT_SETTABLE_STATUSES)}"
                ),
            )
        if meta.is_trashed():
            raise HTTPException(
                status_code=409,
                detail=(
                    "this artifact is in the trash; restore it with POST "
                    "/api/artifacts/{id}/restore before changing its status"
                ),
            )
        meta.status = body.status


# --------------------------------------------------------------------------
# Settings direction: tightening vs loosening (SEC-100-001 / REL-075-004)
# --------------------------------------------------------------------------

#: Every field on :class:`~src.store.ArtifactMeta` that ``PUT /api/artifacts/
#: {id}`` can change and that decides *who may read or write* the artifact.
#: Deliberately explicit rather than derived: a new access-relevant setting has
#: to be classified here on purpose, and the test suite pins the list, so
#: forgetting one is a failing test rather than a silent fail-open.
#:
#: ``updated_at`` is intentionally absent — it changes on every update and
#: grants nobody anything, so counting it would make every request look like a
#: settings change and force a redundant Storage write.
_ACCESS_SETTINGS: tuple[str, ...] = (
    "password",
    "accept_versions_mode",
    "contributors",
    "comments_mode",
    "status",
    "webhooks",
)

#: How *wide* each mode is: a higher number lets more people in. Used only to
#: compare an old value with a new one, never persisted.
_ACCEPT_WIDTH = {ACCEPT_OFF: 0, ACCEPT_ALLOWLIST: 1, ACCEPT_ANYONE: 2}
_COMMENTS_WIDTH = {COMMENTS_OFF: 0, COMMENTS_ALLOWLIST: 1, COMMENTS_ANYONE: 2}
#: "final" freezes versions and comments, "trashed" kills the public link.
#: ``_apply_policy`` refuses to set "trashed" here, but ranking it keeps the
#: comparison total for a meta record that is already in the trash.
_STATUS_WIDTH = {ARTIFACT_TRASHED: 0, ARTIFACT_FINAL: 1, ARTIFACT_DRAFT: 2}


def _is_tightening(field_name: str, before: Any, after: Any) -> bool:
    """Does changing ``field_name`` from ``before`` to ``after`` reduce access?

    "Tightening" means the new value lets *no more* people read or contribute
    than the old one did. The rules, one per access-relevant setting:

    * ``password`` — setting one where there was none puts a gate up; replacing
      an existing one revokes the credential its old holders have. Neither
      widens anything, so both are tightening. Only clearing it is loosening.
    * ``accept_versions_mode`` / ``comments_mode`` — ranked by how many people
      the mode admits (``off`` < ``allowlist`` < ``anyone``); moving to an
      equal-or-narrower rank is tightening.
    * ``contributors`` — the allowlist itself. A new list that is a subset of
      the old one can only remove people; anything that introduces a key that
      was not there before is loosening.
    * ``status`` — ``draft`` is open, ``final`` freezes contributions,
      ``trashed`` additionally kills the public link. Freezing is tightening;
      reopening is loosening.
    * ``webhooks`` — an outbound copy of what happens to the artifact, so a URL
      that was not registered before is a new recipient, i.e. loosening.
      Removing one is tightening.

    Anything not enumerated above is treated as tightening, which is the
    fail-closed answer: an unclassified setting is then committed *before* the
    content, where the worst case is a change that survives a failed request
    while being no wider than what the owner asked for.
    """
    if before == after:
        return True
    if field_name == "password":
        return after is not None
    if field_name == "accept_versions_mode":
        return _ACCEPT_WIDTH.get(after, 0) <= _ACCEPT_WIDTH.get(before, 0)
    if field_name == "comments_mode":
        return _COMMENTS_WIDTH.get(after, 0) <= _COMMENTS_WIDTH.get(before, 0)
    if field_name == "status":
        return _STATUS_WIDTH.get(after, 0) <= _STATUS_WIDTH.get(before, 0)
    if field_name in ("contributors", "webhooks"):
        return set(after or ()) <= set(before or ())
    return True


def _tightening_half(previous: ArtifactMeta, candidate: ArtifactMeta) -> ArtifactMeta:
    """A copy of ``previous`` carrying only ``candidate``'s tightening changes.

    The result is by construction no less restrictive than either input, which
    is exactly what makes it safe to commit before the new content exists.
    """
    tightened = dataclasses.replace(previous)
    for field_name in _ACCESS_SETTINGS:
        before = getattr(previous, field_name)
        after = getattr(candidate, field_name)
        if _is_tightening(field_name, before, after):
            setattr(tightened, field_name, after)
    return tightened


def _access_differs(before: ArtifactMeta, after: ArtifactMeta) -> bool:
    """True when the two metas disagree about who may read or write."""
    return any(
        getattr(before, name) != getattr(after, name) for name in _ACCESS_SETTINGS
    )


#: 502 details for a partly-applied update. Each one names the state that is
#: actually in force, because the owner's next move depends on it: a rolled-back
#: update is retried whole, a stuck tightening needs the settings checked, and a
#: published-but-not-loosened artifact needs only the settings resent.
_UPDATE_ROLLED_BACK = (
    "No new version was published and the settings this request would have "
    "tightened were rolled back: the artifact's previous content and settings "
    "are what is in force. Retry the whole update."
)
_UPDATE_TIGHTENING_STUCK = (
    "No new version was published, and the settings this request tightened "
    "could not be rolled back: they are still in force over the previous "
    "content, which is more restrictive than before but not what you asked "
    "for. Check the artifact's settings before retrying."
)
_UPDATE_LOOSENING_PENDING = (
    "The new version was published and is live, but under the previous, more "
    "restrictive settings: the requested changes that widen access were not "
    "applied. Resend them."
)
_UPDATE_TIMESTAMP_PENDING = (
    "The new version was published and is live; only the artifact's "
    "updated-at timestamp could not be recorded, so access is unchanged."
)


def _partial_update_502(cause: Exception, statement: str) -> HTTPException:
    """A 502 that keeps the underlying cause *and* states what is in force."""
    reason = str(getattr(cause, "detail", None) or cause) or "storage was unavailable"
    return HTTPException(status_code=502, detail=f"{reason}. {statement}")


def _require_exactly_one_content(body: PublishBody | VersionBody) -> None:
    present = _content_fields(body)
    if len(present) != 1:
        raise HTTPException(
            status_code=422,
            detail=(
                "Provide exactly one of 'html', 'markdown' or 'git_url' "
                f"(got: {', '.join(present) if present else 'none'})"
            ),
        )


def _build(
    body: PublishBody | UpdateBody | VersionBody,
) -> tuple[BuiltArtifact, dict[str, Any]]:
    """Build the HTML for a request body and the ``source`` dict to store.

    Raises :class:`HTTPException` 413 when the result exceeds the size limit
    and 422 when the input cannot be built.
    """
    try:
        if body.html is not None:
            if len(body.html.encode("utf-8")) > settings.max_html_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        "HTML is too large; the limit is "
                        f"{settings.max_html_bytes} bytes"
                    ),
                )
            markdown_source = getattr(body, "markdown_source", None)
            if markdown_source is not None and (
                len(markdown_source.encode("utf-8")) > settings.max_html_bytes
            ):
                raise HTTPException(
                    status_code=413,
                    detail=(
                        "markdown_source is too large; the limit is "
                        f"{settings.max_html_bytes} bytes"
                    ),
                )
            built = builder.build_from_html(body.html, body.title)
            # Stored under the same "markdown" key the Markdown-authored path
            # uses, so head_source, the vault and /a/{id}/source all pick it
            # up with no further change. The *rendering* is untouched: what is
            # served stays exactly the submitted HTML.
            if markdown_source is not None:
                return built, {"markdown": markdown_source}
            return built, {}

        if body.markdown is not None:
            built = builder.build_from_markdown(body.markdown, body.title)
            if len(built.html.encode("utf-8")) > settings.max_html_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        "Rendered HTML is too large; the limit is "
                        f"{settings.max_html_bytes} bytes"
                    ),
                )
            return built, {"markdown": body.markdown}

        built = builder.build_from_git(
            str(body.git_url),
            body.git_ref,
            body.git_path,
            body.title,
            settings,
            git_username=body.git_username,
            git_token=body.git_token,
        )
        # Deliberately only url/ref/path/commit: the clone credentials are
        # transient and must not reach the stored envelope. "private" records
        # *that* a credential was needed (never the credential itself), so the
        # public read path can withhold the repository URL — the name of a
        # private repo is itself information its owner did not publish.
        source = {
            "git": {
                "url": str(body.git_url),
                "ref": body.git_ref,
                "path": body.git_path,
                "commit": built.git_commit,
            }
        }
        if body.git_token:
            source["git"]["private"] = True
        return built, source
    except BuildError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _store_canonical(owner: Owner, token: str, artifact_id: str, html: str) -> int:
    """Upload the canonical copy of the built HTML into the author's project.

    This is the one place a caller's credential does more than identify them,
    so it is also the one place its *write* capability matters: a read-only
    personal access token verifies perfectly and then cannot store the copy.
    The failure says so, since nothing else the caller did was wrong.

    **REL-075-009 — accepted residual: a process death here orphans this file.**

    The copy lands in the *caller's* project, reachable only with the caller's
    token, which is request-scope and never persisted (see CLAUDE.md, "Secrets
    discipline"). Every *caught* failure downstream cleans it up while the
    token is still in hand (:func:`_discard_canonical`). What cannot be cleaned
    up is the container dying between this upload and the hub-side version
    write: the token is gone, the hub has no record naming the file, and no
    reconciler can search another project's Storage. The window is process
    death only — a matter of milliseconds, and not something an API caller can
    trigger.

    Writing the external copy *last*, after the hub version file exists, would
    close the window. It is not available: ``canonical_file_id`` lives inside
    the version envelope, and version files are immutable by design (CLAUDE.md,
    "Storage model" — one file per submitted version, never overwritten), so
    the id cannot be filled in afterwards. Rewriting a version file to add it,
    or writing the version with ``canonical_file_id=None`` and following up,
    would trade a rare orphan in someone else's project for a hole in the
    immutability the whole version/diff/anchor model rests on. The id is purely
    informational anyway: it is echoed back once in the publish response and
    never resolved back to a file, so the orphan costs storage and tidiness,
    not correctness or confidentiality.

    An operator reaping one by hand searches the *author's own* project for
    Storage files tagged ``kbc-artifact`` (:data:`CANONICAL_TAG`) plus
    ``artifact-id-{id}``, and deletes any whose artifact the hub does not
    serve.
    """
    backend = KbcFilesBackend(owner.stack_url, token, owner.project_id)
    try:
        return backend.upload(
            f"artifact-{artifact_id}.html",
            html.encode("utf-8"),
            [CANONICAL_TAG, tag_for_id(artifact_id)],
        )
    except BackendError as exc:
        logger.error(
            "Canonical upload failed for artifact %s (project %s): %s",
            artifact_id,
            owner.project_id,
            exc,
        )
        raise HTTPException(
            status_code=502,
            detail=(
                "could not store canonical copy in your project — the "
                "credential must be able to write Storage Files there "
                "(a read-only personal access token cannot)"
            ),
        ) from exc


def _discard_canonical(owner: Owner, token: str, artifact_id: str, file_id: int) -> None:
    """Best-effort delete of a canonical copy whose version never landed.

    The copy lives in the *author's* project, which the hub can only reach
    with the caller's token during this request -- there is no later cleanup
    handle, no tag search across projects, no reconciler that could ever find
    it. So it is removed here, on the failure path, while the token is still
    in hand. A failure to remove it is logged with everything an operator
    needs to reap it by hand; it never masks the error that got us here.
    """
    try:
        KbcFilesBackend(owner.stack_url, token, owner.project_id).delete(file_id)
    except Exception as exc:  # noqa: BLE001 - the original failure must win
        logger.error(
            "Could not discard canonical file %s of artifact %s in project %s "
            "after its version write failed; it is an orphan in that project "
            "and needs reaping by hand: %s",
            file_id, artifact_id, owner.project_id, exc,
        )


def _identity(owner: Owner) -> dict[str, Any]:
    """The project identity recorded on a meta record or a version envelope."""
    return {
        "stack_url": owner.stack_url,
        "project_id": owner.project_id,
        "project_name": owner.project_name,
        "key": owner.key,
    }


def _public_version_meta(row: dict) -> dict:
    """One version's public metadata with a private repo's URL withheld.

    ``Envelope.public_meta`` lives in the store and hands out the whole ``git``
    dict; it cannot tell a public repository from one that needed a token. A
    private repository's URL names infrastructure its owner never published,
    so it is dropped here, at response time, while ref/path/commit and the
    ``private`` flag itself stay — provenance without the address.
    """
    git = row.get("git")
    if not isinstance(git, dict) or not git.get("private"):
        return row
    return {**row, "git": {k: v for k, v in git.items() if k != "url"}}


def _outdated_flag(row: dict, head_version: int | None) -> dict:
    """``{"outdated": bool}`` for a proposal, or nothing for any other row.

    A proposal is *outdated* when its author told us which version they wrote
    it against (``base_version``) and the head has moved on since. That is the
    one thing a reviewer cannot see from the numbers alone: a proposal based on
    v3 while the head is v5 was written without seeing two live versions, and
    promoting it silently reverts them.

    Only proposals carry the key: for a live version — already promoted, or the
    head itself — "outdated" would be meaningless rather than false.
    """
    if row.get("status") != STATUS_PROPOSED:
        return {}
    base = row.get("base_version")
    return {"outdated": base is not None and base != head_version}


def _head_version_of(request: Request, artifact_id: str) -> int | None:
    head = request.app.state.store.get_head(artifact_id)
    return head.version if head is not None else None


def _artifact_response(
    request: Request,
    meta: ArtifactMeta,
    envelope: Envelope,
    status_code: int,
) -> JSONResponse:
    """Standard management-API response describing one artifact."""
    payload = {
        "id": meta.id,
        # The public half of the identity pair. Equal to "id" until the link is
        # rotated; every URL below is built from it.
        "share_id": meta.share_id,
        "title": envelope.title,
        "protected": bool(meta.password),
        "accept_versions": meta.accept_versions,
        "accept_versions_mode": meta.accept_versions_mode,
        "contributors": list(meta.contributors),
        "comments_mode": meta.comments_mode,
        "artifact_status": meta.status,
        # Full URLs, not a count: this response only ever reaches the owning
        # project, which is who set them. The listing endpoint reports a count.
        "webhooks": list(meta.webhooks),
        "version": envelope.version,
        "status": envelope.status,
        "head_version": _head_version_of(request, meta.id),
        "owner_project_id": meta.owner.get("project_id"),
        "canonical_file_id": envelope.canonical_file_id,
        **artifact_urls(base_url(request), meta.share_id),
    }
    return JSONResponse(status_code=status_code, content=payload)


# --------------------------------------------------------------------------
# Public routes
# --------------------------------------------------------------------------


@app.get(
    "/",
    response_class=HTMLResponse,
    tags=["public"],
    summary="Landing page",
    description=(
        "Human-facing HTML documentation of the hub: what it does, how to "
        "authenticate, copy-pasteable curl examples, and links to the admin "
        "studio (/admin), the agent definition (/agent) and the skill "
        "(/skill). Needs no credentials and returns a complete HTML document."
    ),
    responses={
        200: {"description": "The landing page.", "content": CONTENT_HTML}
    },
)
def landing(request: Request) -> HTMLResponse:
    """Human-facing documentation page."""
    return HTMLResponse(
        landing_page(
            base_url(request),
            SERVICE_VERSION,
            GITHUB_REPO_URL,
            settings.demo_url,
        )
    )


@app.post(
    "/",
    response_class=PlainTextResponse,
    tags=["service"],
    summary="Platform startup probe",
    description=(
        "Always returns 200 with the plain-text body 'OK'. Used by the Keboola "
        "Data App platform proxy to check that the process is up; it takes no "
        "body, no parameters and no credentials, and is not meant to be called "
        "by clients."
    ),
    responses={
        200: {"description": "Always; body is 'OK'.", "content": CONTENT_TEXT}
    },
)
def landing_probe() -> PlainTextResponse:
    """Platform startup check — the proxy POSTs to ``/`` to see if we are up."""
    return PlainTextResponse("OK")


@app.get(
    "/health",
    tags=["service"],
    summary="Readiness, liveness and index statistics",
    description=(
        "Reports process liveness, the running service version, and whether "
        "the in-memory artifact index has finished hydrating from Storage. "
        "'hydrated': false means Storage was unreachable at startup and the "
        "hub is serving in degraded mode (individual artifacts still resolve, "
        "one Storage lookup at a time). This is also the readiness signal: it "
        "answers 503 when the hub has detected a second instance writing the "
        "same state, which breaks the exactly-one-instance invariant the "
        "artifact index, the version allocator and the state snapshots all "
        "depend on. Unauthenticated."
    ),
    responses={
        200: {
            "description": (
                "JSON with 'status', 'version', 'artifacts' (indexed count) "
                "and 'hydrated'."
            )
        },
        503: {
            "description": (
                "Not ready: another instance was detected writing this hub's "
                "state. The body carries 'status': 'unready' and a 'detail' "
                "naming the invariant. Operational state is read-only until "
                "one container is left running and restarted."
            )
        },
    },
)
def health(request: Request) -> JSONResponse:
    """Readiness and liveness, plus index statistics.

    ARCH-100-001: the readiness half. Detecting a second writer used to be a
    log line nobody read, while the process carried on serving and overwriting.
    Now the detection latches (see ``src.statedb``) and shows up here as a 503,
    so a platform health probe, a load balancer or an operator sees the broken
    invariant instead of having to grep for it.

    ``POST /`` is deliberately *not* affected: that is the Keboola platform's
    own startup check (CLAUDE.md rule 3) and must keep answering 200, or the
    container is killed and restarted into exactly the same situation.
    """
    body = {
        "status": "ok",
        "version": SERVICE_VERSION,
        "artifacts": request.app.state.store.count(),
        "hydrated": bool(getattr(request.app.state, "hydrated", False)),
    }
    foreign = foreign_writer_detected()
    if foreign is not None:
        return JSONResponse(
            {**body, "status": "unready", "detail": foreign},
            status_code=503,
        )
    return JSONResponse(body)


@app.get(
    "/context",
    tags=["service"],
    summary="Machine-readable service manifest",
    description=(
        "Full manifest for agents: endpoint catalog, auth model, stack "
        "aliases, publish body schema, the versioning model, and the limits "
        "this deployment is actually configured with. Intended to be fetched "
        "once before scripting against the API — it is the authoritative "
        "source when this document and the running service disagree. "
        "Unauthenticated; contains no owner or token details."
    ),
    responses={
        200: {
            "description": (
                "The manifest: 'service', 'version', 'base_url', 'documents' "
                "(the /agent and /skill files with their SHA-256 and the "
                "attested release asset to install from), 'auth', "
                "'endpoints', 'publish_body', 'versioning', 'limits', 'notes'."
            )
        }
    },
)
def context(request: Request) -> dict:
    """Machine-readable manifest of the service, for agents."""
    base = base_url(request)
    return {
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "base_url": base,
        "repository": GITHUB_REPO_URL,
        "documents": _documents_manifest(base),
        "description": (
            "Public hosting for self-contained HTML artifacts, backed by "
            "Keboola Storage. Any Keboola Storage API token from any stack can "
            "publish; the canonical copy is stored in the caller's own project "
            "and a serving copy in the hub's project. Updates add versions "
            "rather than overwriting, and an artifact can accept moderated "
            "version proposals from other projects."
        ),
        "auth": {
            "applies_to": "/api/*",
            "headers": {
                "Authorization": (
                    "'Bearer {token}' — a programmatic bearer obtained by "
                    "signing in: a kbc_at_* session or a kbc_pat_* personal "
                    "access token. Send X-Storage-Project with it. A Storage "
                    "token here is a 400; so is sending this together with "
                    "X-StorageApi-Token. On a deployed hub, GET "
                    "/health/headers reports whether the platform proxy in "
                    "front of it forwards this header"
                ),
                "X-StorageApi-Token": (
                    "any Keboola Storage API token, and nothing else — a "
                    "kbc_at_*/kbc_pat_* value here is a 400 naming "
                    "Authorization: Bearer as its header"
                ),
                "X-Storage-Stack": "stack alias or full https URL (X-Kbc-Stack accepted as alias for direct access)",
                "X-Storage-Project": (
                    "project id — required with a kbc_at_*/kbc_pat_* bearer, "
                    "which is scoped to a person rather than to one project, "
                    "and ignored with a Storage token, which names its own. "
                    "Not X-Kbc-*: the data-app proxy strips that family"
                ),
            },
            "stack_aliases": dict(STACK_ALIASES),
            "stack_rule": (
                "a full stack URL is accepted when it is https and its hostname "
                "ends with .keboola.com (plus any host configured in "
                "HUB_EXTRA_STACKS)"
            ),
            "verification": "GET {stack}/v2/storage/tokens/verify",
            "sign_in": {
                "page": "GET /login",
                "why": (
                    "obtains a credential without the caller having to find a "
                    "Storage API token first; the session it returns is used "
                    "on /api/* exactly like one, with X-Storage-Project added"
                ),
                "device_flow": (
                    "POST /login/device {'stack': ...} returns user_code and "
                    "verification_uri_complete; the person approves it in a "
                    "browser while the client polls POST /login/device/token "
                    "{'stack', 'device_code'} every 'interval' seconds until "
                    "it answers {'status': 'ok'}. Works from anywhere"
                ),
                "pkce_flow": (
                    "GET /login/pkce/start?stack=... redirects through the "
                    "stack's authorization screen back to /login/callback. "
                    "Offered only when this hub answers on http://127.0.0.1 "
                    "or http://[::1] — a Keboola stack accepts no other "
                    "redirect target for it"
                ),
                "scope": (
                    "a session authorizes against every project its approval "
                    "covered on the stack's own screen; X-Storage-Project "
                    "selects which of those a call acts as and restricts "
                    "nothing. The PKCE flow asks the stack for its project "
                    "picker by default; the device flow's approval page "
                    "offers the same choice"
                ),
                "lifecycle": (
                    "an access token lasts an hour; POST /login/refresh "
                    "{'stack', 'refresh_token'} renews it, POST /login/signout "
                    "{'stack', 'token'} revokes the session"
                ),
                "relayed_not_stored": (
                    "the hub calls the stack because a browser cannot (no "
                    "CORS) and hands the result straight back; it keeps no "
                    "session of its own"
                ),
            },
            "ownership": (
                "(normalized stack, project id); update, delete, promote and "
                "head pinning require a credential for the owning project — "
                "a Storage token, a session or a personal access token are "
                "interchangeable here"
            ),
            "destructive_policy_and_sign_in": (
                "a bearer is exchanged by the stack for the admin's own "
                "Storage token in the named project, so the claims "
                "destructive_token_policy reads describe that token — signing "
                "in is not a way around the policy"
            ),
            "write_capability": (
                "every /api route accepts all three credential shapes; the "
                "only capability difference is publishing, which stores the "
                "canonical copy as a Storage File in the caller's own "
                "project, so a read-only personal access token verifies and "
                "then fails that one write with 502"
            ),
            "token_storage": "never persisted; used only during the request",
            "reader_password_header": "X-Artifact-Password",
            "guest_header": (
                "X-Artifact-Guest: '{invitation_id}.{secret}' — an alternative "
                "to the storage token on the four comment-write routes and on "
                "GET /a/{id}/guest, for an invited human without a Keboola "
                "account. See the 'guests' section."
            ),
            "reading_proposals": (
                "the same two management headers may be sent on /a/{id}/v/{n} "
                "and /a/{id}/diff/{a}..{b} to read a proposed version as its "
                "author or as the artifact owner"
            ),
        },
        "endpoints": [
            {
                "method": "GET",
                "path": "/",
                "auth": "none",
                "purpose": "human-facing landing page (HTML)",
            },
            {
                "method": "POST",
                "path": "/",
                "auth": "none",
                "purpose": "platform startup check, returns OK",
            },
            {
                "method": "GET",
                "path": "/health",
                "auth": "none",
                "purpose": (
                    "readiness and liveness, service version and index "
                    "statistics; 503 when a second writing instance was "
                    "detected"
                ),
            },
            {
                "method": "GET",
                "path": "/health/headers",
                "auth": "none",
                "purpose": (
                    "diagnostic: the names (never the values) of the request "
                    "headers that reached the app, to detect a proxy that "
                    "strips X-StorageApi-Token or X-Storage-Stack"
                ),
            },
            {
                "method": "GET",
                "path": "/context",
                "auth": "none",
                "purpose": "this manifest",
            },
            {
                "method": "GET",
                "path": "/skill",
                "auth": "none",
                "purpose": "SKILL.md for agents (text/markdown)",
            },
            {
                "method": "GET",
                "path": "/login",
                "auth": "none",
                "purpose": "sign in to a Keboola stack in a browser (HTML)",
            },
            {
                "method": "POST",
                "path": "/login/device",
                "auth": "none",
                "purpose": (
                    "start a device-code sign-in on a stack; returns the code "
                    "to approve and how often to poll"
                ),
            },
            {
                "method": "POST",
                "path": "/login/device/token",
                "auth": "none",
                "purpose": (
                    "poll a device-code sign-in; 'pending' until approved, "
                    "then the session and the projects it reaches"
                ),
            },
            {
                "method": "GET",
                "path": "/login/pkce/start",
                "auth": "none",
                "purpose": (
                    "start a PKCE sign-in (loopback hubs only); redirects to "
                    "the stack"
                ),
            },
            {
                "method": "GET",
                "path": "/login/callback",
                "auth": "none",
                "purpose": "PKCE callback (loopback hubs only)",
            },
            {
                "method": "POST",
                "path": "/login/refresh",
                "auth": "none",
                "purpose": "renew a signed-in session from its refresh token",
            },
            {
                "method": "POST",
                "path": "/login/signout",
                "auth": "none",
                "purpose": "revoke a signed-in session on its stack",
            },
            {
                "method": "GET",
                "path": "/agent",
                "auth": "none",
                "purpose": (
                    "a ready-to-install Claude Code subagent definition "
                    "(text/markdown); install with 'install -d "
                    "~/.claude/agents && curl -fsSL {base}/agent -o "
                    "~/.claude/agents/artifact-hub.md'"
                ),
            },
            {
                "method": "GET",
                "path": "/changelog",
                "auth": "none",
                "purpose": (
                    "rendered CHANGELOG.md, through the standard artifact "
                    "template (HTML); read fresh from disk on every request"
                ),
            },
            {
                "method": "GET",
                "path": "/changelog.md",
                "auth": "none",
                "purpose": (
                    "raw CHANGELOG.md source (text/markdown), for machines; "
                    "read fresh from disk on every request"
                ),
            },
            {
                "method": "GET",
                "path": "/admin",
                "auth": (
                    "none to load the page; the visitor's own storage token, "
                    "entered in the browser, for every call it makes"
                ),
                "purpose": (
                    "owner/moderation studio (HTML): list your artifacts, "
                    "review and diff proposals, promote, reject, pin or "
                    "delete versions. The token stays in the browser tab "
                    "(sessionStorage) and is only sent as the usual "
                    "management headers."
                ),
            },
            {
                "method": "GET",
                "path": "/docs",
                "auth": "none",
                "purpose": "interactive Swagger UI for this API",
            },
            {
                "method": "GET",
                "path": "/openapi.json",
                "auth": "none",
                "purpose": "machine-readable OpenAPI schema for this API",
            },
            {
                "method": "GET",
                "path": "/a/{id}",
                "auth": "url capability; password form when protected",
                "purpose": (
                    "rendered head version, inside a full-viewport iframe "
                    "sandboxed without allow-same-origin (the document runs "
                    "in an opaque origin); use /a/{id}/raw for the bytes"
                ),
            },
            {
                "method": "POST",
                "path": "/a/{id}/unlock",
                "auth": "form field 'password'",
                "purpose": "unlock a protected artifact, sets a signed cookie",
            },
            {
                "method": "GET",
                "path": "/a/{id}/raw",
                "auth": "url capability; X-Artifact-Password when protected",
                "purpose": "the head version's HTML itself",
            },
            {
                "method": "GET",
                "path": "/a/{id}/source",
                "auth": "url capability; X-Artifact-Password when protected",
                "purpose": (
                    "original submitted source: the author's Markdown when "
                    "the version has one (markdown-authored, or html "
                    "published with markdown_source), else the HTML"
                ),
            },
            {
                "method": "GET",
                "path": "/a/{id}/meta",
                "auth": "url capability",
                "purpose": "public metadata, no owner details",
            },
            {
                "method": "GET",
                "path": "/a/{id}/live",
                "auth": "url capability",
                "purpose": (
                    "tiny change-detection snapshot (head version, counts, "
                    "document status) with a strong ETag; answers 304 to a "
                    "matching If-None-Match. This is what the artifact page, "
                    "the review UI and the admin studio poll so a reader sees "
                    "a new version without reloading. Readable while the "
                    "artifact is protected, exactly like /a/{id}/meta."
                ),
            },
            {
                "method": "GET",
                "path": "/a/{id}/v/{n}",
                "auth": "url capability; owner or author for proposals",
                "purpose": "one specific version",
            },
            {
                "method": "GET",
                "path": "/a/{id}/versions",
                "auth": "url capability",
                "purpose": "version history JSON, or ?format=html for a picker page",
            },
            {
                "method": "GET",
                "path": "/a/{id}/diff/{a}..{b}",
                "auth": "url capability; owner or author for proposals",
                "purpose": "diff two versions (?format=html|unified|json)",
            },
            {
                "method": "GET",
                "path": "/a/{id}/comments",
                "auth": "url capability",
                "purpose": "every inline comment thread as JSON, oldest first",
            },
            {
                "method": "GET",
                "path": "/a/{id}/review",
                "auth": (
                    "url capability to read; the visitor's own storage token, "
                    "entered in the browser, to comment"
                ),
                "purpose": (
                    "two-pane review UI (HTML): the document in a sandboxed "
                    "iframe beside its comment threads. Select text to open a "
                    "thread, click a highlight to jump to one, reply and "
                    "resolve in place."
                ),
            },
            {
                "method": "GET",
                "path": "/a/{id}/guest",
                "auth": "X-Artifact-Guest invitation credential",
                "purpose": (
                    "resolve a guest invitation to the display name it "
                    "carries, so a client can say who it is commenting as "
                    "before it writes anything; 401 for a missing, revoked or "
                    "wrong credential"
                ),
            },
            {
                "method": "GET",
                "path": "/a/{id}/export/markdown",
                "auth": "url capability",
                "purpose": (
                    "head version's Markdown as a file attachment: the "
                    "author's own source when there is one, otherwise "
                    "converted from the HTML document; the "
                    "X-Artifact-Markdown-Source header says which "
                    "('original' / 'converted')"
                ),
            },
            {
                "method": "GET",
                "path": "/a/{id}/export/vault",
                "auth": "url capability",
                "purpose": (
                    "the whole artifact as a ready-to-open Obsidian vault "
                    "(application/zip): INDEX.md, document.md, versions/, "
                    "comments/ and reasoning.md. Budgeted: 413 above "
                    "HUB_EXPORT_MAX_BYTES of history, 429 above "
                    "HUB_MAX_EXPORTS_PER_HOUR builds per client per hour"
                ),
            },
            {
                "method": "POST",
                "path": "/api/artifacts",
                "auth": "storage token",
                "purpose": "publish a new artifact",
            },
            {
                "method": "PUT",
                "path": "/api/artifacts/{id}",
                "auth": "storage token (owner project)",
                "purpose": "add a live version and/or change password and accept_versions",
            },
            {
                "method": "GET",
                "path": "/api/artifacts",
                "auth": "storage token",
                "purpose": "list the caller project's artifacts",
            },
            {
                "method": "DELETE",
                "path": "/api/artifacts/{id}",
                "auth": "storage token (owner project)",
                "purpose": (
                    "move the artifact to the trash: reversible soft delete "
                    "that freezes it and kills its public link"
                ),
            },
            {
                "method": "POST",
                "path": "/api/artifacts/{id}/restore",
                "auth": "storage token (owner project)",
                "purpose": (
                    "bring a trashed artifact back on the same share id and "
                    "URL"
                ),
            },
            {
                "method": "DELETE",
                "path": "/api/artifacts/{id}/purge",
                "auth": "storage token (owner project)",
                "purpose": (
                    "irreversibly erase every version, comment thread, meta "
                    "record and view statistic"
                ),
            },
            {
                "method": "GET",
                "path": "/api/artifacts/{id}/webhooks",
                "auth": "storage token (owner project)",
                "purpose": (
                    "list each registered webhook receiver with the signing "
                    "key its deliveries use"
                ),
            },
            {
                "method": "POST",
                "path": "/api/artifacts/{id}/webhooks/{receiver_id}/rotate-key",
                "auth": "storage token (owner project)",
                "purpose": (
                    "mint a fresh signing key for one receiver without "
                    "touching its URL; the previous key still verifies for "
                    "webhook_key_overlap_s seconds"
                ),
            },
            {
                "method": "POST",
                "path": "/api/artifacts/{id}/rotate-link",
                "auth": "storage token (owner project)",
                "purpose": (
                    "mint a new share id; the previous public link stops "
                    "resolving immediately"
                ),
            },
            {
                "method": "GET",
                "path": "/api/artifacts/{id}/stats",
                "auth": "storage token (owner project)",
                "purpose": (
                    "view counts for one artifact: total, by day and by "
                    "surface"
                ),
            },
            {
                "method": "POST",
                "path": "/api/artifacts/{id}/invitations",
                "auth": "storage token (owner project)",
                "purpose": (
                    "invite one person without a Keboola account to comment; "
                    "returns a review URL carrying a one-time secret in its "
                    "fragment"
                ),
            },
            {
                "method": "GET",
                "path": "/api/artifacts/{id}/invitations",
                "auth": "storage token (owner project)",
                "purpose": (
                    "list this artifact's guest invitations (id, name, "
                    "created_at, revoked); never the secrets"
                ),
            },
            {
                "method": "DELETE",
                "path": "/api/artifacts/{id}/invitations/{iid}",
                "auth": "storage token (owner project)",
                "purpose": (
                    "revoke one guest's invitation, leaving every other "
                    "invitation working"
                ),
            },
            {
                "method": "POST",
                "path": "/api/artifacts/{id}/versions",
                "auth": "storage token (any project when accept_versions)",
                "purpose": "submit a version; live for the owner, proposed otherwise",
            },
            {
                "method": "POST",
                "path": "/api/artifacts/{id}/versions/{n}/promote",
                "auth": "storage token (owner project)",
                "purpose": "promote a proposal to live",
            },
            {
                "method": "DELETE",
                "path": "/api/artifacts/{id}/versions/{n}",
                "auth": "storage token (owner, or the proposal's author)",
                "purpose": "delete a version, or withdraw your own proposal",
            },
            {
                "method": "PUT",
                "path": "/api/artifacts/{id}/head",
                "auth": "storage token (owner project)",
                "purpose": "serve the latest live version, or pin one",
            },
            {
                "method": "POST",
                "path": "/api/artifacts/{id}/comments",
                "auth": "storage token (subject to comments_mode)",
                "purpose": (
                    "open an inline comment thread on a quoted passage of one "
                    "version"
                ),
            },
            {
                "method": "POST",
                "path": "/api/artifacts/{id}/comments/{tid}/replies",
                "auth": "storage token (subject to comments_mode)",
                "purpose": "reply in an existing thread",
            },
            {
                "method": "POST",
                "path": "/api/artifacts/{id}/comments/{tid}/resolve",
                "auth": "storage token (owner, or the thread's author)",
                "purpose": (
                    "resolve a thread, or reopen it with {'resolved': false}"
                ),
            },
            {
                "method": "DELETE",
                "path": "/api/artifacts/{id}/comments/{tid}",
                "auth": "storage token (owner, or the thread's author)",
                "purpose": "delete a thread and its replies",
            },
        ],
        "publish_body": {
            "html": "string, complete HTML document, served as-is",
            "markdown": (
                "string, rendered by the built-in template (GFM tables, task "
                "lists, mermaid fences, syntax highlighting)"
            ),
            "markdown_source": (
                "string, optional; only valid together with html. The same "
                "document in Markdown, stored as the version's source and "
                "served by /a/{id}/source and /a/{id}/export/markdown, so "
                "other agents read your text instead of a lossy conversion "
                "of your HTML. It does not change what is rendered: the "
                "served document stays exactly the html you submitted"
            ),
            "git_url": "string, https git repository to clone (public, or private with git_token)",
            "git_ref": (
                "string, optional branch or tag for git_url; a commit id is "
                "refused -- tag it to pin an immutable source"
            ),
            "git_path": (
                "string, optional entry file or directory inside the repository; "
                "defaults to index.html, then README.md, then a single root *.html"
            ),
            "git_token": (
                "string, optional personal access token for the git host "
                "(GitHub PAT, GitLab token, ...) used to clone a private "
                "repository; transient — used only for the clone during this "
                "request, never stored, logged or returned"
            ),
            "git_username": (
                "string, optional username for git hosts that require one; "
                "defaults to 'x-access-token', which works for GitHub PATs and "
                "GitLab deploy tokens"
            ),
            "title": "string, optional; derived from the content when omitted",
            "password": "string, optional reader password",
            "accept_versions": (
                "bool, default false; when true other projects may submit "
                "moderated version proposals"
            ),
            "rules": [
                "exactly one of html, markdown, git_url",
                "markdown_source is only valid together with html (422 "
                "otherwise); always send it when you publish html, so "
                "/export/markdown and /a/{id}/source can serve your own "
                "Markdown rather than a conversion",
                "git_ref, git_path, git_token and git_username are only valid "
                "together with git_url (422 otherwise)",
                "userinfo (https://user:pass@host/...), the query string and "
                "the fragment are stripped from git_url before the URL is "
                "validated, cloned, stored or returned — a clone URL needs "
                "none of them, and each can carry a secret",
                "PUT accepts the same fields, all optional, plus clear_password, "
                "accept_versions/accept_versions_mode, contributors, "
                "comments_mode and status; a title is only valid together "
                "with new content, because a title lives on a version",
                "POST /api/artifacts/{id}/versions accepts the same content "
                f"fields plus an optional note (max {MAX_NOTE_CHARS} chars)",
            ],
        },
        "versioning": {
            "model": (
                "Updates never overwrite. Every submission becomes version "
                "next_version and is stored as its own Storage file with a "
                "verified author."
            ),
            "statuses": {
                "live": "servable; the head is chosen among live versions",
                "proposed": (
                    "moderated; readable only by the artifact owner and the "
                    "version's author until the owner promotes it"
                ),
            },
            "moderation": (
                "Owner submissions are always live. Other projects need "
                "accept_versions=true (403 otherwise) and always land as "
                "proposals; the owner promotes one with POST "
                "/api/artifacts/{id}/versions/{n}/promote."
            ),
            "head_pointer": (
                "PUT /api/artifacts/{id}/head with {'mode': 'latest'} serves the "
                "newest live version; {'mode': 'pinned', 'version': n} freezes "
                "/a/{id} on one live version."
            ),
            "diff": (
                "GET /a/{id}/diff/{older}..{newer} renders a side-by-side page; "
                "?format=unified returns text/plain, ?format=json returns the "
                "unified diff plus added/removed counts, and ?format=visual "
                "shows the two rendered documents themselves side by side in "
                "sandboxed iframes with synchronized scrolling. Markdown is "
                "compared when both versions carry it, otherwise the built "
                "HTML."
            ),
            "retention": (
                f"at most {settings.max_versions} live versions per artifact; "
                "the oldest live versions that are neither the head nor pinned "
                "are pruned. Proposals are never pruned."
            ),
            "rate_limit": (
                f"{settings.max_versions_per_day} submitted versions per "
                "project per artifact per UTC day (429 afterwards); owner "
                "content updates through PUT /api/artifacts/{id} count "
                "against the same budget"
            ),
            "deletion": (
                "The owner may delete any version except the last live one "
                "(409); a contributor may withdraw their own proposal."
            ),
        },
        "collaboration": {
            "accept_versions_mode": (
                "'off' (owner only, the default), 'anyone' (any verified "
                "project, moderated as proposals) or 'allowlist' (only the "
                "projects listed in contributors). The legacy boolean "
                "accept_versions still works: false maps to 'off', true to "
                "'anyone'."
            ),
            "comments_mode": (
                "'anyone' (the default), 'allowlist' or 'off'. The owner may "
                "always comment unless the artifact is final."
            ),
            "contributors": (
                "list of owner keys shaped '{project_id}@{stack hostname}', "
                f"at most {MAX_CONTRIBUTORS}; used by both allowlist modes"
            ),
            "status": (
                "'draft' or 'final'. 'final' freezes the artifact: new "
                "versions, new comments and content updates all answer 409 "
                "for everyone, the owner included. The owner reopens it with "
                "PUT /api/artifacts/{id} and {'status': 'draft'}."
            ),
            "comment_anchoring": (
                "A thread carries a W3C-style TextQuoteSelector (exact + "
                "prefix + suffix) captured from the rendered text of one "
                "version, and stays bound to that version: there is no "
                "cross-version re-anchoring, so a thread made on an older "
                "version may not highlight anything on the current one."
            ),
            "comment_visibility": (
                "Threads are readable by anyone holding the capability URL "
                "(GET /a/{id}/comments); identities are reduced to project "
                "id, project name and stack hostname, and a guest to "
                "{'kind': 'guest', 'name': ...}."
            ),
            "comment_addressing": (
                "The four comment-write routes accept either the public share "
                "id or the internal artifact id in their path (share id "
                "first), because the review UI and every capability-URL "
                "holder only ever saw the share id. The share id resolves "
                "under the public rules, so a rotated-away link cannot write "
                "either; the internal id works only for the artifact's own "
                "owner. Every other /api/* route takes the internal id only."
            ),
            "comment_password_gate": (
                "On a password-protected artifact, writing a comment needs "
                "the same X-Artifact-Password header (or unlock cookie) that "
                "reading it does — for guests and for the owner alike."
            ),
            "comment_rate_limit": (
                f"{settings.max_comments_per_day} comments and replies per "
                "project per artifact per UTC day (429 afterwards)"
            ),
            "comment_thread_budget": (
                f"one thread holds at most {MAX_REPLIES_PER_THREAD} replies "
                f"and {MAX_THREAD_BYTES} serialized bytes (422 past either). "
                "The per-day limit bounds the rate; these bound the object, "
                "which is rewritten whole on every reply and re-read on "
                "every listing."
            ),
            "export": (
                "GET /a/{id}/export/markdown downloads the served version as "
                "Markdown — the author's own source when the version has one "
                "(markdown-authored, or html published with markdown_source), "
                "otherwise converted from the HTML, which is lossy for charts "
                "and images; the X-Artifact-Markdown-Source header reports "
                "'original' or 'converted'. GET /a/{id}/export/vault "
                "downloads a deterministic Obsidian vault ZIP of the whole "
                "history and discussion."
            ),
        },
        "guests": {
            "model": (
                "A guest is one invited human without a Keboola account. An "
                "invitation is a named, revocable capability on one artifact: "
                "POST /api/artifacts/{id}/invitations mints it and returns a "
                "review URL of the form "
                "/a/{share_id}/review#invite={invitation_id}.{secret}."
            ),
            "secret": (
                "Shown exactly once and stored only as a PBKDF2 record, like a "
                "reader password. It rides the URL *fragment*, which browsers "
                "never send to a server, and reaches the API only in the "
                "X-Artifact-Guest header shaped '{invitation_id}.{secret}' — "
                "so it stays out of access logs and Referer headers. A lost "
                "link is replaced by revoking and minting another."
            ),
            "grants": (
                "Open a comment thread, reply, and resolve or delete threads "
                "the guest opened themselves. Never a version, never any "
                "/api/* management call, never another artifact. "
                "comments_mode does not gate a guest (the invitation is the "
                "grant); 'final' and 'trashed' freeze them like everybody "
                "else."
            ),
            "revocation": (
                "DELETE /api/artifacts/{id}/invitations/{iid} turns off one "
                "person's link immediately and leaves every other guest's "
                "working. Comments they already wrote stay."
            ),
            "identity": (
                "A guest's comments are published as {'kind': 'guest', "
                "'name': ...} — the name their inviter chose, and nothing "
                "else. Their daily comment budget is counted per invitation. "
                "GET /a/{id}/guest resolves a credential to that name."
            ),
        },
        "sharing": {
            "model": (
                "An artifact has two identifiers: the internal 'id' used by "
                "every /api/* call, and the public 'share_id' that appears in "
                "/a/{...} URLs. They are equal until the link is rotated."
            ),
            "rotation": (
                "POST /api/artifacts/{id}/rotate-link mints a new share_id. "
                "The previous share id stops resolving immediately, and so "
                "does the bare artifact id once the two differ — there is no "
                "grace period and no way to un-rotate. Unlock cookies are "
                "scoped to the old path and go with it."
            ),
            "trash": (
                "DELETE /api/artifacts/{id} is a reversible soft delete: the "
                "status becomes 'trashed', the public link 404s and versions "
                "and comments freeze (409), but the owner still sees the row "
                "in GET /api/artifacts with a 'trashed_at'. POST "
                "/api/artifacts/{id}/restore undoes it on the same URL; "
                "DELETE /api/artifacts/{id}/purge is the irreversible erase. "
                "'trashed' cannot be set through PUT status."
            ),
            "base_version": (
                "POST /api/artifacts/{id}/versions accepts an optional "
                "base_version naming the version the submission was written "
                "against (422 when it does not exist). GET /a/{id}/versions "
                "flags a proposal 'outdated': true when its base_version is no "
                "longer the head — the document moved on while it was being "
                "written."
            ),
        },
        "webhooks": {
            "registering": (
                "PUT /api/artifacts/{id} with 'webhooks': [...] replaces the "
                "artifact's list ([] clears it); at most "
                f"{settings.max_webhooks_per_artifact} URLs, https only, and "
                "hosts resolving to private, loopback, link-local or metadata "
                "addresses are refused with 422."
            ),
            "events": (
                "version.published, version.proposed, version.promoted, "
                "comment.created, comment.replied, artifact.finalized, "
                "artifact.trashed, artifact.restored, link.rotated. The "
                "initial publish (v1) emits nothing — the owner just did it."
            ),
            "delivery": (
                "POST of {'event', 'event_id', 'delivery_id', 'artifact_id', "
                "'payload', 'created_at'} signed with X-Hub-Signature-256: "
                "sha256=<hmac>; a hooks.slack.com URL gets Slack's "
                "{'text': ...} shape instead, with the ids in the "
                "X-Hub-Event-Id and X-Hub-Delivery-Id headers every delivery "
                f"carries. Up to {settings.webhook_max_attempts} attempts "
                "with backoff, best effort: the queue is in memory, bounded "
                f"at {settings.webhook_queue_max}, and a restart drops what "
                "was pending. The delivery id is stable across retries, so a "
                "receiver can recognise one instead of acting twice."
            ),
            "signing": (
                "Every receiver has its own signing key, derived from the "
                "hub's webhook key, the artifact and the receiver URL. GET "
                "/api/artifacts/{id}/webhooks (owner only) reports each URL "
                "with its key. A receiver therefore cannot forge a delivery "
                "for another receiver or another artifact, and its key "
                "reveals nothing about the hub's own."
            ),
            "rotation": (
                "POST /api/artifacts/{id}/webhooks/{receiver_id}/rotate-key "
                "(owner only, receiver_id from the 'id' field GET .../webhooks "
                "reports) mints a fresh key for one receiver without changing "
                f"its URL. For {settings.webhook_key_overlap_s} seconds "
                "(webhook_key_overlap_s) afterwards, deliveries carry both "
                "the new signature (X-Hub-Signature-256) and the previous "
                "one (X-Hub-Signature-256-Previous), then only the new one. "
                "Both the listing and the rotate response are "
                "Cache-Control: no-store."
            ),
            "secrecy": (
                "A webhook URL is itself a capability, so it is returned only "
                "in the owner PUT response that set it; GET /api/artifacts "
                "reports 'webhooks_count' instead."
            ),
        },
        "analytics": {
            "views": (
                "GET /api/artifacts/{id}/stats (owner only) reports total, "
                "per-day (30 days) and per-surface view counts. Surfaces are "
                "'page', 'raw', 'source' and 'version'."
            ),
            "privacy": (
                "Counts only — no reader identity, address or referrer is "
                "recorded. Purging an artifact forgets its numbers."
            ),
            "durability": (
                "Counters and view rows live in a SQLite sidecar snapshotted "
                "into the host project's Storage Files, so rate limits survive "
                "a redeploy; a crash can lose the last few minutes."
            ),
        },
        "limits": {
            "max_html_bytes": settings.max_html_bytes,
            "max_inline_image_bytes": settings.max_inline_image_bytes,
            "max_inline_total_bytes": settings.max_inline_total_bytes,
            "git_clone_timeout_s": settings.git_clone_timeout_s,
            "git_max_repo_bytes": settings.git_max_repo_bytes,
            "unlock_cookie_max_age_s": settings.unlock_cookie_max_age_s,
            "max_versions": settings.max_versions,
            "max_versions_per_day": settings.max_versions_per_day,
            "diff_max_bytes": settings.diff_max_bytes,
            "max_note_chars": MAX_NOTE_CHARS,
            "max_comments_per_day": settings.max_comments_per_day,
            "max_replies_per_thread": MAX_REPLIES_PER_THREAD,
            "max_thread_bytes": MAX_THREAD_BYTES,
            "max_contributors": MAX_CONTRIBUTORS,
            "max_unlock_attempts_per_hour": settings.max_unlock_attempts_per_hour,
            "max_unlock_attempts_per_artifact_per_hour": (
                settings.max_unlock_attempts_per_artifact_per_hour
            ),
            # SEC-100-004: the inbound body ceilings, so a client can size its
            # request before sending it rather than discovering a 413.
            "max_content_request_bytes": settings.max_content_request_bytes,
            "max_small_request_bytes": settings.max_small_request_bytes,
            "max_comment_body_chars": MAX_BODY_CHARS,
            "max_comment_quote_chars": MAX_QUOTE_CHARS,
            "max_webhooks_per_artifact": settings.max_webhooks_per_artifact,
            "webhook_timeout_s": settings.webhook_timeout_s,
            "webhook_max_attempts": settings.webhook_max_attempts,
            # SEC-100-006: how long a rotated receiver's previous key still
            # verifies (X-Hub-Signature-256-Previous) after
            # POST .../rotate-key -- see the "webhooks"."rotation" prose above
            # for the full explanation; duplicated here as a plain number
            # alongside its sibling webhook settings.
            "webhook_key_overlap_s": settings.webhook_key_overlap_s,
            "max_invitations_per_artifact": settings.max_invitations_per_artifact,
            "max_invitation_name_chars": MAX_INVITATION_NAME_CHARS,
            "export_max_bytes": settings.export_max_bytes,
            "max_exports_per_hour": settings.max_exports_per_hour,
            # SEC-075-011: which tokens of the owning project may run a
            # destructive route on this deployment. The name only — never the
            # allowlist's contents, which would tell an outsider exactly which
            # token ids to go looking for.
            "destructive_token_policy": settings.destructive_token_policy,
        },
        "notes": [
            "GET /a/{id} and /a/{id}/v/{n} return a wrapper page whose "
            "full-viewport iframe carries the artifact as srcdoc, sandboxed "
            "without allow-same-origin: the document runs in an opaque origin "
            "and cannot touch this origin's storage or cookies. "
            "GET /a/{id}/raw returns the same bytes unwrapped, for machines — "
            "anything that renders them does so in its own context.",
            "The reader password gate is throttled: after "
            f"{settings.max_unlock_attempts_per_hour} failed attempts per "
            "artifact per client address per hour it answers 429. Successful "
            "unlocks never count. An unlock cookie is bound to the password "
            "that issued it, so changing or clearing the password revokes "
            "every cookie immediately.",
            "Guest invitation credentials (X-Artifact-Guest) are throttled "
            "the same way and on the same budget size: after "
            f"{settings.max_unlock_attempts_per_hour} rejected credentials "
            "per artifact per client address per hour the answer is 429. "
            "Verifying an invitation secret costs a full PBKDF2, so an "
            "unthrottled public probe would be a CPU-exhaustion primitive.",
            "GET /a/{id}/export/vault is the most expensive read here: it "
            "diffs every visible version and converts every HTML document. It "
            f"refuses an artifact whose history exceeds {settings.export_max_bytes} "
            "bytes with 413 before building anything, and allows "
            f"{settings.max_exports_per_hour} builds of one artifact per "
            "client address per hour before answering 429. Pull a vault once "
            "and keep it; do not poll it.",
            "A version published from a private repository (one that needed "
            "git_token) reports git.private true in public metadata and "
            "history, and its git.url is withheld.",
            "Artifact URLs are capabilities: the unguessable id is the only "
            "access control by default, there is no public listing, and every "
            "/a/* response carries X-Robots-Tag: noindex, nofollow.",
            "/a/{...} addresses an artifact by its share_id, not by the "
            "internal artifact id the /api/* routes use. They start out equal; "
            "after POST /api/artifacts/{id}/rotate-link only the new share id "
            "resolves publicly, and the old link — plus the bare artifact id — "
            "answers 404 from the next request on.",
            "Destructive routes — DELETE /api/artifacts/{id}, "
            ".../purge, .../versions/{n} (as owner), "
            "POST .../rotate-link and POST .../webhooks/{receiver_id}"
            "/rotate-key — are additionally governed by this hub's "
            f"destructive_token_policy, currently "
            f"'{settings.destructive_token_policy}'. Under 'project' every "
            "token of the owning project may run them; under 'admin' the "
            "token must be a master token or belong to a project user with "
            "the admin role; under 'allowlist' its token id must be one the "
            "operator listed. A token that fails the policy gets 403 with a "
            "detail naming it. Non-destructive owner routes (update, head "
            "pin, promote, settings, invitations, stats, trash restore) are "
            "never affected, and a contributor withdrawing their own "
            "proposal is not a destructive operation.",
            "DELETE /api/artifacts/{id} moves an artifact to the trash "
            "(reversible with POST /api/artifacts/{id}/restore); DELETE "
            "/api/artifacts/{id}/purge is the irreversible erase. A trashed "
            "artifact 404s publicly but still appears in its owner's listing.",
            "An optional password adds a second layer; readers unlock in the "
            "browser (signed cookie scoped to the artifact path) or send the "
            "X-Artifact-Password header.",
            "Version history is visible to anyone holding the capability URL, "
            "but the content of a proposed version is not: only the owner and "
            "its author can read it.",
            "git_token follows the same rule as the Storage token: it is used "
            "only for the clone inside the request and is never written to the "
            "stored artifact, the logs, or any response.",
        ],
    }


@app.get(
    "/skill",
    tags=["service"],
    response_class=MarkdownResponse,
    summary="Agent-facing SKILL.md",
    description=(
        "Serves skills/artifact-publisher/SKILL.md verbatim as text/markdown, "
        "teaching an AI agent how to authenticate, publish artifacts and "
        "contribute versions unassisted. Unauthenticated, and identical for "
        "every caller. Use /agent instead for a ready-to-install Claude Code "
        "subagent definition."
    ),
    responses={
        200: {
            "description": "The SKILL.md document.",
            "content": CONTENT_MARKDOWN,
        },
        404: {"description": "SKILL.md is not readable on this deployment."},
    },
)
def skill(request: Request) -> Response:
    """Serve the agent-facing SKILL.md."""
    return _serve_document(request, SKILL_PATH, "skill document")


def _read_document(path: Path) -> tuple[bytes, str] | None:
    """The document's bytes and their SHA-256, fresh from disk; None if unreadable.

    Read on every request, like the changelog: the hash has to describe what
    is actually sent, and a copy cached at import time could not promise that.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        logger.error("%s not readable", path)
        return None
    return raw, hashlib.sha256(raw).hexdigest()


def _serve_document(request: Request, path: Path, what: str) -> Response:
    """Serve one of the instruction documents with its digest attached.

    The digest headers exist so an installer can compare what a hub serves
    with what the project released -- and refuse to install if they differ.
    They are a consistency check, not a trust anchor: a digest from the same
    origin as the content proves nothing about who wrote it. Provenance comes
    from the release assets and their attestation, which /context points at.
    """
    document = _read_document(path)
    if document is None:
        return JSONResponse(status_code=404, content={"error": f"{what} not available"})
    raw, digest = document
    etag = f'"sha256:{digest}"'
    headers = {
        "ETag": etag,
        "X-Content-SHA256": digest,
        "X-Hub-Version": SERVICE_VERSION,
    }
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(
        content=raw, media_type="text/markdown; charset=utf-8", headers=headers
    )


def _release_asset_url(filename: str) -> str:
    """Where the attested copy of a document lives for the running version."""
    return f"{GITHUB_REPO_URL}/releases/download/v{SERVICE_VERSION}/{filename}"


def _documents_manifest(base: str) -> dict[str, Any]:
    """The /context entry describing the instruction documents this hub serves."""
    manifest: dict[str, Any] = {}
    for key, route, path, filename in (
        ("agent", "/agent", AGENT_PATH, "AGENT.md"),
        ("skill", "/skill", SKILL_PATH, "SKILL.md"),
    ):
        document = _read_document(path)
        entry: dict[str, Any] = {
            "url": f"{base.rstrip('/')}{route}",
            "version": SERVICE_VERSION,
            "release_asset": _release_asset_url(filename),
        }
        if document is not None:
            raw, digest = document
            entry["sha256"] = digest
            entry["bytes"] = len(raw)
        manifest[key] = entry
    manifest["sums"] = _release_asset_url("SHA256SUMS")
    manifest["verify"] = (
        "Install from the release asset, not from the live endpoint: download "
        "the asset and SHA256SUMS for this version, check the sum, and run "
        f"'gh attestation verify <file> --repo padak/kbc_ai_artifact'. The "
        "sha256 here is what this hub serves; if it differs from the release, "
        "the hub is not running its own release."
    )
    return manifest


@app.get(
    "/agent",
    tags=["service"],
    response_class=MarkdownResponse,
    summary="Ready-to-install Claude Code subagent definition",
    description=(
        "Serves skills/artifact-hub-agent/AGENT.md verbatim as text/markdown: "
        "a self-contained Claude Code subagent definition (YAML front matter "
        "plus instructions) that knows how to publish, update and moderate "
        "artifacts on this hub. Install it with:\n\n"
        "`install -d ~/.claude/agents && curl -fsSL {base}/agent -o "
        "~/.claude/agents/artifact-hub.md`\n\n"
        "Unauthenticated, and identical for every caller."
    ),
    responses={
        200: {
            "description": "The AGENT.md subagent definition.",
            "content": CONTENT_MARKDOWN,
        },
        404: {"description": "AGENT.md is not readable on this deployment."},
    },
)
def agent(request: Request) -> Response:
    """Serve the Claude Code subagent definition this hub runs."""
    return _serve_document(request, AGENT_PATH, "agent definition")


def _read_changelog() -> str | None:
    """Read CHANGELOG.md fresh from disk; None when it cannot be read.

    Deliberately not cached at import or module scope: another process may
    rewrite the file while this one keeps running, and both /changelog and
    /changelog.md must reflect the current content on every request.
    """
    try:
        return CHANGELOG_PATH.read_text(encoding="utf-8")
    except OSError:
        logger.error("CHANGELOG.md not readable at %s", CHANGELOG_PATH)
        return None


def _changelog_not_found() -> JSONResponse:
    return JSONResponse(
        status_code=404, content={"error": "changelog not available"}
    )


@app.get(
    "/changelog",
    tags=["service"],
    response_class=HTMLResponse,
    summary="Rendered changelog",
    description=(
        "Serves CHANGELOG.md from the repository root, rendered as a page of "
        "this service rather than as a published artifact: the hub's own "
        "shell — graph-paper grid, monospace headings, the same footer as the "
        "landing page — wrapped around the rendered Markdown. Read fresh from "
        "disk on every request, so changes to the file appear immediately. "
        "Unauthenticated, and identical for every caller. Use /changelog.md "
        "for the raw source."
    ),
    responses={
        200: {"description": "The rendered changelog page.", "content": CONTENT_HTML},
        404: {"description": "CHANGELOG.md is not readable on this deployment."},
    },
)
def changelog() -> Response:
    """Serve CHANGELOG.md inside the service's own shell design system.

    The Markdown is rendered by the *builder's* configured markdown-it instance
    (tables, task lists, anchors — the same dialect a published artifact gets)
    but only down to a body fragment: the full artifact page template would
    bring its own standalone look, and the changelog is a page of this service,
    not somebody's document. ``src.pages`` supplies the chrome.
    """
    text = _read_changelog()
    if text is None:
        return _changelog_not_found()
    body_html = builder._render_markdown_body(text)
    return HTMLResponse(
        changelog_page(body_html, SERVICE_VERSION, GITHUB_REPO_URL)
    )


@app.get(
    "/changelog.md",
    tags=["service"],
    response_class=MarkdownResponse,
    summary="Raw changelog source",
    description=(
        "Serves CHANGELOG.md from the repository root verbatim as "
        "text/markdown, for machines that want the source rather than the "
        "rendered page. Read fresh from disk on every request. "
        "Unauthenticated, and identical for every caller. Use /changelog for "
        "the rendered HTML page."
    ),
    responses={
        200: {
            "description": "The CHANGELOG.md document.",
            "content": CONTENT_MARKDOWN,
        },
        404: {"description": "CHANGELOG.md is not readable on this deployment."},
    },
)
def changelog_md() -> Response:
    """Serve CHANGELOG.md verbatim as text/markdown."""
    text = _read_changelog()
    if text is None:
        return _changelog_not_found()
    return Response(content=text, media_type="text/markdown; charset=utf-8")


# --------------------------------------------------------------------------
# Interactive sign-in
# --------------------------------------------------------------------------


class DeviceStartBody(BaseModel):
    """Body of ``POST /login/device``."""

    model_config = {"json_schema_extra": {"examples": [{"stack": "eu"}]}}

    stack: str = Field(
        ...,
        description=(
            "Stack alias (us, gcp-us, eu, azure-eu, gcp-eu) or a full "
            "https://*.keboola.com URL to sign in against."
        ),
    )


class DevicePollBody(BaseModel):
    """Body of ``POST /login/device/token``."""

    stack: str = Field(..., description="The stack the sign-in was started on.")
    device_code: str = Field(
        ..., description="The deviceCode returned by POST /login/device."
    )


class RefreshBody(BaseModel):
    """Body of ``POST /login/refresh``."""

    stack: str = Field(..., description="The stack that issued the session.")
    refresh_token: str = Field(
        ..., description="The refresh token of the session to renew."
    )


class SignOutBody(BaseModel):
    """Body of ``POST /login/signout``."""

    stack: str = Field(..., description="The stack that issued the session.")
    token: str = Field(
        ..., description="The access or refresh token of the session to end."
    )


def _login_stack(raw: str) -> str:
    """Resolve a sign-in target the same way every other stack input is."""
    try:
        return resolve_stack(raw, settings.extra_stacks)
    except StackError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _login_unavailable(exc: LoginUnavailable) -> HTTPException:
    """502 for a stack that cannot, or will not, run this sign-in."""
    return HTTPException(status_code=502, detail=str(exc))


def _claim_login_slot(request: Request, scope: str, limit: int, what: str) -> None:
    """Charge one outbound sign-in call to this client's hourly budget, or 429.

    Every ``/login/*`` route is unauthenticated — the caller has no identity
    yet, that is the point — and every one of them makes the hub call a
    Keboola stack. Unbudgeted, that is an open relay onto somebody else's auth
    API, and the stack cannot close it from its side: the address *it* rate
    limits is the hub's, so one abuser there would spend the budget of every
    person using this hub. The bound has to be here, per caller.
    """
    client_ip = _client_ip(request)
    bucket = _utc_hour()
    if _read_counter(request.app, scope, client_ip, bucket) >= limit:
        raise HTTPException(
            status_code=429,
            detail=(
                f"too many {what} from your address; at most {limit} are "
                "allowed per hour"
            ),
        )
    _bump_counter(request.app, scope, client_ip, bucket)


def _claim_login(request: Request) -> None:
    """Charge starting or renewing a session: the low-frequency half."""
    _claim_login_slot(
        request, "login", settings.max_logins_per_hour, "sign-in requests"
    )


def _claim_login_poll(request: Request) -> None:
    """Charge one poll of a device sign-in: the high-frequency half.

    A device code lives 15 minutes and is polled every 5 seconds, so one
    honest sign-in is on the order of 180 calls — two orders of magnitude
    above what starting one costs. Sharing a budget with the starts would
    either throttle the normal flow or make the starts' budget meaningless,
    so polling is counted separately and bounded generously.
    """
    _claim_login_slot(
        request, "login-poll", settings.max_login_polls_per_hour, "sign-in polls"
    )


def _loopback_origin(base: str) -> bool:
    """True when ``base`` is the loopback origin PKCE requires.

    Connection accepts only ``http://127.0.0.1:{port}/{path}`` or the IPv6
    equivalent as a PKCE redirect URI (``PkceAuthorizeRequest`` in the
    connection repository), which makes the flow available exactly when the
    hub is reachable at one of those — a hub the user runs themselves.
    """
    parts = urlsplit(base)
    return parts.scheme == "http" and parts.hostname in ("127.0.0.1", "::1")


def _pkce_redirect_uri(base: str) -> str:
    """The loopback callback URI this hub registers with a stack."""
    return f"{base.rstrip('/')}/login/callback"


def _credential_payload(credential: Credential) -> dict[str, Any]:
    """A signed-in session, shaped for the browser that asked for it.

    The tokens are in here on purpose: they belong to the person signing in,
    and the hub's whole credential model is that they live in the visitor's
    own tab and never on this side. Every response carrying this is
    ``no-store``.
    """
    return {
        "stack": credential.stack_url,
        "access_token": credential.access_token,
        "refresh_token": credential.refresh_token,
        "expires_in": credential.expires_in,
        "user": {
            "id": credential.user_id,
            "email": credential.user_email,
            "name": credential.user_name,
        },
    }


def _session_payload(stack_url: str, credential: Credential) -> dict[str, Any]:
    """The projects a fresh session may act as, for the project picker.

    Introspection failing is not a failed sign-in: the credential is already
    valid. The picker then has nothing to list and asks for a project id
    instead, which still completes the login.
    """
    try:
        info = introspect(stack_url, credential.access_token, settings.login_timeout_s)
    except LoginError as exc:
        logger.info("Could not list projects for a fresh session: %s", exc)
        return {"projects": [], "projects_unavailable": True}
    return {
        "projects": [
            {"id": project.id, "name": project.name, "role": project.role}
            for project in info.projects
        ],
        "projects_unavailable": False,
    }


def _credential_response(payload: dict[str, Any]) -> JSONResponse:
    """A JSON response carrying a credential, kept out of every cache.

    Shares :func:`_no_store` with the webhook-key routes: both answer with
    something a cache must never hold, and one definition of what that means
    is one place to keep it right.
    """
    return _no_store(JSONResponse(content=payload))


@app.get(
    "/login",
    tags=["service"],
    response_class=HTMLResponse,
    summary="Sign in to Keboola (browser UI)",
    description=(
        "Signs a visitor in to any allowed Keboola stack without them having "
        "to find a Storage API token first.\n\n"
        "Two flows are offered, both of which Connection itself implements: "
        "**device authorization** (the page shows a short code, the visitor "
        "approves it in their Keboola tab) works everywhere, and "
        "**authorization code + PKCE** (one browser hop, nothing to type) is "
        "offered when this hub answers on a loopback origin, which is the "
        "only redirect target a stack accepts for it.\n\n"
        "The session that comes back is handed to the visitor's own tab and "
        "used exactly like a pasted Storage token: kept in sessionStorage, "
        "sent as X-StorageApi-Token plus X-Storage-Project. The hub relays "
        "the sign-in and keeps nothing."
    ),
    responses={200: {"description": "The sign-in page.", "content": CONTENT_HTML}},
)
def login(request: Request) -> HTMLResponse:
    """Serve the sign-in page."""
    base = base_url(request)
    return HTMLResponse(
        login_page(
            base,
            SERVICE_VERSION,
            GITHUB_REPO_URL,
            pkce_available=_loopback_origin(base),
            result=None,
        ),
        headers={"Cache-Control": "no-store"},
    )


@app.post(
    "/login/device",
    tags=["service"],
    summary="Start a device-code sign-in",
    description=(
        "Opens a device authorization on the named stack and returns the code "
        "the visitor approves in their browser, plus how often to poll "
        "POST /login/device/token.\n\n"
        "The hub makes this call on the visitor's behalf because a browser "
        "cannot: a Keboola stack sends no CORS headers for this origin."
    ),
    responses={
        200: {"description": "Device authorization opened."},
        400: RESP_STACK_400,
        429: {"description": "Too many sign-ins started from this address."},
        502: {
            "description": (
                "The stack could not be reached, or does not offer "
                "device-code sign-in."
            )
        },
    },
)
def login_device_start(request: Request, body: DeviceStartBody) -> JSONResponse:
    """Open a device authorization and return its user-facing codes."""
    stack_url = _login_stack(body.stack)
    _claim_login(request)
    try:
        start = start_device(stack_url, settings.login_client_id, settings.login_timeout_s)
    except LoginUnavailable as exc:
        raise _login_unavailable(exc) from exc
    except LoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _credential_response(
        {
            "stack": stack_url,
            "device_code": start.device_code,
            "user_code": start.user_code,
            "verification_uri": start.verification_uri,
            "verification_uri_complete": start.verification_uri_complete,
            "expires_in": start.expires_in,
            "interval": start.interval,
        }
    )


@app.post(
    "/login/device/token",
    tags=["service"],
    summary="Poll a device-code sign-in",
    description=(
        "Asks once whether the device authorization has been approved. "
        "Answers {'status': 'pending'} with the interval to wait while the "
        "visitor is still in their browser, and {'status': 'ok'} with the "
        "session and the projects it reaches once they approve."
    ),
    responses={
        200: {"description": "Still pending, or signed in."},
        400: {"description": "The sign-in was declined, expired, or is invalid."},
        502: {"description": "The stack could not be reached."},
    },
)
def login_device_poll(request: Request, body: DevicePollBody) -> JSONResponse:
    """Poll a device authorization once."""
    stack_url = _login_stack(body.stack)
    _claim_login_poll(request)
    try:
        credential = poll_device(
            stack_url,
            settings.login_client_id,
            body.device_code,
            settings.login_timeout_s,
        )
    except LoginPending as exc:
        return _credential_response(
            {
                "status": "pending",
                "interval": exc.interval,
                "slow_down": exc.slow_down,
            }
        )
    except LoginUnavailable as exc:
        raise _login_unavailable(exc) from exc
    except LoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _credential_response(
        {
            "status": "ok",
            "credential": _credential_payload(credential),
            **_session_payload(stack_url, credential),
        }
    )


@app.get(
    "/login/pkce/start",
    tags=["service"],
    summary="Start a PKCE sign-in (loopback hubs only)",
    description=(
        "Redirects the visitor to the stack's authorization screen with a "
        "freshly generated PKCE challenge. Available only when this hub "
        "answers on http://127.0.0.1 or http://[::1] — a Keboola stack "
        "accepts no other redirect target for this flow, so a hosted hub gets "
        "404 here and uses the device code instead."
    ),
    responses={
        307: {"description": "Redirect to the stack's authorization screen."},
        400: RESP_STACK_400,
        404: {"description": "This hub is not on a loopback origin."},
        429: {"description": "Too many sign-ins started from this address."},
    },
)
def login_pkce_start(
    request: Request,
    stack: str = Query(..., description="Stack alias or full https URL."),
    all_projects: bool = Query(
        False,
        description=(
            "Skip the stack's project picker and issue a session covering "
            "every project the admin belongs to. Off by default: a session "
            "narrowed on the stack's own screen is the smaller credential to "
            "be holding in a browser tab."
        ),
    ),
) -> RedirectResponse:
    """Begin a PKCE sign-in and send the browser to the stack."""
    base = base_url(request)
    if not _loopback_origin(base):
        raise HTTPException(
            status_code=404,
            detail=(
                "PKCE sign-in needs a loopback callback, which this hub does "
                "not have; use the device code instead"
            ),
        )
    stack_url = _login_stack(stack)
    _claim_login(request)
    redirect_uri = _pkce_redirect_uri(base)
    pending = request.app.state.pkce.start(stack_url, redirect_uri)
    return RedirectResponse(
        authorize_url(
            stack_url,
            settings.login_client_id,
            redirect_uri,
            pkce_challenge(pending.verifier),
            pending.state,
            pick_project=not all_projects,
        ),
        status_code=307,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@app.get(
    "/login/callback",
    tags=["service"],
    response_class=HTMLResponse,
    summary="PKCE callback (loopback hubs only)",
    description=(
        "Where the stack sends the browser back after a PKCE sign-in. "
        "Exchanges the authorization code for a session and re-serves the "
        "sign-in page on its project-picking step. A code is usable once: the "
        "pending verifier is consumed here, so a replayed callback fails."
    ),
    responses={
        200: {
            "description": "The sign-in page, ready to pick a project.",
            "content": CONTENT_HTML,
        },
        404: {"description": "This hub is not on a loopback origin."},
    },
)
def login_callback(
    request: Request,
    code: str = Query("", description="Authorization code minted by the stack."),
    state: str = Query("", description="The state this hub sent with the request."),
    error: str = Query("", description="Set instead of a code when refused."),
) -> HTMLResponse:
    """Finish a PKCE sign-in and hand the session to the page."""
    base = base_url(request)
    if not _loopback_origin(base):
        raise HTTPException(status_code=404, detail="No PKCE sign-in on this hub")
    result = _pkce_result(request, code, state, error)
    return HTMLResponse(
        login_page(
            base,
            SERVICE_VERSION,
            GITHUB_REPO_URL,
            pkce_available=True,
            result=result,
        ),
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def _pkce_result(
    request: Request, code: str, state: str, error: str
) -> dict[str, Any]:
    """Turn a PKCE callback into what the sign-in page should show next."""
    pending = request.app.state.pkce.take(state) if state else None
    if pending is None:
        # Either the state was never ours, the login already completed, or it
        # sat unfinished past its TTL. All three are "start again", and none
        # of them says which — a wrong state must not be a probe.
        return {"error": "This sign-in is no longer valid. Start again."}
    if error:
        return {"error": "The sign-in was declined in the browser."}
    if not code:
        return {"error": "The stack returned no authorization code."}
    try:
        credential = exchange_pkce(
            pending.stack_url,
            settings.login_client_id,
            code,
            pending.redirect_uri,
            pending.state,
            pending.verifier,
            settings.login_timeout_s,
        )
    except LoginError as exc:
        return {"error": str(exc)}
    return {
        "credential": _credential_payload(credential),
        **_session_payload(pending.stack_url, credential),
    }


@app.post(
    "/login/refresh",
    tags=["service"],
    summary="Renew a signed-in session",
    description=(
        "Exchanges a refresh token for a fresh access/refresh pair so a "
        "studio tab left open does not have to sign in again when its access "
        "token ages out. The old refresh token is spent by the stack."
    ),
    responses={
        200: {"description": "A renewed session."},
        400: {"description": "The refresh token is invalid, expired or revoked."},
        502: {"description": "The stack could not be reached."},
    },
)
def login_refresh(request: Request, body: RefreshBody) -> JSONResponse:
    """Rotate a session's refresh token."""
    stack_url = _login_stack(body.stack)
    _claim_login(request)
    try:
        credential = refresh_credential(
            stack_url, body.refresh_token, settings.login_timeout_s
        )
    except LoginUnavailable as exc:
        raise _login_unavailable(exc) from exc
    except LoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _credential_response({"credential": _credential_payload(credential)})


@app.post(
    "/login/signout",
    tags=["service"],
    summary="End a signed-in session",
    description=(
        "Revokes a session on its stack, so logging out of the studio also "
        "ends the credential rather than only forgetting it locally. Always "
        "answers 204: a credential the stack will not revoke is one it has "
        "already forgotten."
    ),
    responses={204: {"description": "The session is no longer usable."}},
    status_code=204,
)
def login_signout(request: Request, body: SignOutBody) -> Response:
    """Revoke a session on its stack."""
    stack_url = _login_stack(body.stack)
    _claim_login(request)
    revoke(stack_url, body.token, settings.login_timeout_s)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@app.get(
    "/admin",
    tags=["service"],
    response_class=HTMLResponse,
    summary="Owner and moderation studio (browser UI)",
    description=(
        "A single self-contained HTML page for artifact owners: list the "
        "artifacts your project owns, open a version history, read a pending "
        "proposal, diff it against the head, then promote, reject, pin or "
        "delete it, and toggle accept_versions.\n\n"
        "The page itself is public and needs no credentials — authentication "
        "happens entirely in the browser. The visitor pastes their Storage "
        "token and picks a stack; the page keeps them in a JavaScript variable "
        "and in sessionStorage (per-tab, cleared when the tab closes) and "
        "sends them as the usual X-StorageApi-Token / X-Storage-Stack headers "
        "on the very same API calls curl would make. The server never sees or "
        "stores the token beyond serving those ordinary API requests."
    ),
    responses={
        200: {"description": "The admin studio page.", "content": CONTENT_HTML}
    },
)
def admin(request: Request) -> HTMLResponse:
    """Serve the client-side moderation studio.

    Deliberately a static document: no server-side session, no token handling,
    nothing to log. Everything it does, it does from the visitor's browser
    against the public API.
    """
    return HTMLResponse(
        admin_page(base_url(request), SERVICE_VERSION, GITHUB_REPO_URL)
    )


@app.get(
    "/a/{artifact_id}",
    tags=["public"],
    response_class=HTMLResponse,
    summary="Rendered artifact page (head version)",
    description=(
        "Serves the head version — the newest live version, or the one the "
        "owner pinned. No token is needed: the unguessable id in the URL is "
        "the access control.\n\n"
        "The document is returned inside a minimal wrapper page: a "
        "full-viewport iframe carrying the artifact as srcdoc, sandboxed "
        "without allow-same-origin, so the artifact's own scripts run in an "
        "opaque origin and cannot reach this origin's storage or cookies. "
        "Readers see no difference; machines that want the bytes themselves "
        "use GET /a/{id}/raw.\n\n"
        + PASSWORD_GATE_NOTE
        + "\n\nUntil the caller is unlocked, this returns 401 with the unlock "
        "form as HTML rather than the artifact."
    ),
    responses={
        200: {
            "description": (
                "The wrapper page embedding the head version's HTML document "
                "in a sandboxed iframe."
            ),
            "content": CONTENT_HTML,
        },
        401: {
            "description": (
                "Password-protected artifact; the HTML unlock form is returned."
            ),
            "content": CONTENT_HTML,
        },
        404: {
            "description": (
                "No artifact exists with this id, or it has no live version."
            )
        },
        429: RESP_UNLOCK_429,
        502: RESP_HUB_502,
    },
)
def read_artifact(
    request: Request,
    artifact_id: str = PathParam(..., description=SHARE_ID_DESC),
) -> Response:
    """Serve the head version sandboxed, or the unlock form when protected."""
    # The path carries the *public* share id; everything past resolution works
    # with the internal artifact id (``meta.id``).
    public_id = artifact_id
    meta = _public_meta_of(request, public_id)
    if meta is None:
        return _not_found(public_id)
    if not reader_allowed(meta, request):
        return HTMLResponse(unlock_page(public_id, None), status_code=401)
    envelope = request.app.state.store.get_head(meta.id)
    if envelope is None:
        return _not_found(public_id)
    _record_view(request.app, meta.id, "page")
    return _framed(request, meta, envelope)


@app.post(
    "/a/{artifact_id}/unlock",
    tags=["public"],
    status_code=303,
    summary="Unlock a password-protected artifact",
    description=(
        "Target of the HTML unlock form; the password is sent as an "
        "application/x-www-form-urlencoded 'password' field. On a correct "
        "password this responds 303 to the artifact page and sets a signed, "
        "HttpOnly cookie scoped to /a/{id}, so later visits from that browser "
        "skip the form until the cookie expires. The cookie is bound to the "
        "password that issued it: changing or clearing the artifact's "
        "password revokes every cookie already handed out.\n\n"
        "Failed attempts are throttled per artifact and client address "
        "(HUB_MAX_UNLOCK_ATTEMPTS_PER_HOUR per hour, 429 afterwards); a "
        "correct password never counts against the budget. Machine clients do "
        "not need this endpoint at all — they send X-Artifact-Password on "
        "each read, under the same throttle."
    ),
    responses={
        303: {
            "description": (
                "Correct password; redirects to the artifact page with an "
                "unlock cookie set."
            )
        },
        401: {
            "description": (
                "Wrong password; the unlock form is returned with an error "
                "message."
            ),
            "content": CONTENT_HTML,
        },
        404: RESP_NOT_FOUND,
        429: {
            **RESP_UNLOCK_429,
            "content": CONTENT_HTML,
        },
        502: RESP_HUB_502,
    },
)
def unlock_artifact(
    request: Request,
    artifact_id: str = PathParam(..., description=SHARE_ID_DESC),
    password: str = Form(
        "", description="The artifact's reader password, from the unlock form."
    ),
) -> Response:
    """Password form target: on success set a signed, path-scoped cookie."""
    public_id = artifact_id
    meta = _public_meta_of(request, public_id)
    if meta is None:
        return _not_found(public_id)
    if meta.password:
        # Each attempt costs a full PBKDF2, so the budget is checked *before*
        # the hash runs; only failures are counted, so a reader who gets it
        # right on the first try is never throttled. The bucket is keyed by the
        # internal id, so rotating the link gives no fresh budget.
        client_ip = _client_ip(request)
        if _unlock_throttled(request.app, meta.id, client_ip):
            return HTMLResponse(
                unlock_page(
                    public_id,
                    "Too many attempts — wait an hour and try again",
                ),
                status_code=429,
            )
        if not check_password(password, meta.password):
            _record_unlock_failure(request.app, meta.id, client_ip)
            return HTMLResponse(
                unlock_page(public_id, "Wrong password"), status_code=401
            )
    response = RedirectResponse(f"/a/{meta.share_id}", status_code=303)
    response.set_cookie(
        # Name and path both carry the share id: a cookie only comes back when
        # its path prefixes the request path, and what the browser sees is
        # /a/{share_id}. The signed *value* carries the internal artifact id,
        # so the cookie identifies the artifact rather than the URL.
        key=unlock_cookie_name(meta),
        # Bound to the current password record: changing or clearing the
        # password invalidates every cookie issued under the old one.
        value=request.app.state.signer.make(meta.id, password_scope(meta)),
        path=f"/a/{meta.share_id}",
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=settings.unlock_cookie_max_age_s,
    )
    return response


# Publisher-controlled HTML served as a *top-level* document, not inside the
# wrapper page's sandboxed iframe. GET /a/{id} isolates artifact HTML with
# <iframe sandbox="allow-scripts ..."> (no allow-same-origin), so its scripts
# never touch the hub's origin. /raw and /source have no such wrapper, so the
# equivalent isolation has to come from the response itself: the CSP `sandbox`
# directive applies the very same sandbox flags to a top-level document,
# dropping it into a unique opaque origin. That is what stops a malicious
# artifact opened by a signed-in admin/reviewer from reading the hub-origin
# sessionStorage where their Keboola Storage token lives, or its cookies.
#
# Deliberately WITHOUT allow-same-origin — granting it would hand the document
# the hub's origin back and undo the whole protection.
#
# The body is never touched: machine clients (curl, fetch, agents) don't
# execute JavaScript and ignore these headers, so they still get the exact
# stored bytes.
SANDBOX_CSP = "sandbox allow-scripts allow-popups allow-forms allow-downloads"


def _sandboxed_html(html: str) -> HTMLResponse:
    """Serve publisher HTML as an opaque-origin, non-sniffed document."""
    return HTMLResponse(
        html,
        headers={
            "Content-Security-Policy": SANDBOX_CSP,
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get(
    "/a/{artifact_id}/raw",
    tags=["public"],
    response_class=HTMLResponse,
    summary="Raw artifact HTML (head version)",
    description=(
        "The exact built HTML of the head version, with no unlock-form "
        "fallback — meant for machine consumption. These are the bytes GET "
        "/a/{id} embeds in its sandboxed iframe; served here unwrapped, so "
        "anything that renders them does so in its own context.\n\n"
        "Because this is publisher-controlled HTML served as a top-level "
        "document, the response carries `Content-Security-Policy: sandbox "
        "allow-scripts allow-popups allow-forms allow-downloads` (plus "
        "`X-Content-Type-Options: nosniff`). A browser therefore renders it in "
        "a unique opaque origin — the same isolation the wrapper page gets "
        "from its sandboxed iframe — so artifact scripts cannot reach hub "
        "origin storage or cookies. Machine clients ignore these headers and "
        "receive the exact stored bytes, unchanged.\n\n"
        + PASSWORD_GATE_NOTE
        + "\n\nA protected "
        "artifact without a valid password answers 401 as JSON, never as a "
        "form."
    ),
    responses={
        200: {
            "description": "The head version's HTML document.",
            "content": CONTENT_HTML,
        },
        401: {
            "description": (
                "Password required or wrong; JSON hint pointing at the "
                "X-Artifact-Password header."
            )
        },
        404: {
            "description": (
                "No artifact exists with this id, or it has no live version."
            )
        },
        429: RESP_UNLOCK_429,
        502: RESP_HUB_502,
    },
)
def read_raw(
    request: Request,
    artifact_id: str = PathParam(..., description=SHARE_ID_DESC),
) -> Response:
    """The head version's HTML itself, for machines."""
    public_id = artifact_id
    meta = _public_meta_of(request, public_id)
    if meta is None:
        return _not_found(public_id)
    if not reader_allowed(meta, request):
        return _password_required()
    envelope = request.app.state.store.get_head(meta.id)
    if envelope is None:
        return _not_found(public_id)
    _record_view(request.app, meta.id, "raw")
    return _sandboxed_html(envelope.html)


@app.get(
    "/a/{artifact_id}/source",
    tags=["public"],
    summary="Original submitted source",
    description=(
        "Returns the head version's retained source: the original Markdown "
        "(as text/markdown) whenever the version has one — markdown-sourced "
        "artifacts, and HTML published with 'markdown_source' — otherwise the "
        "original HTML (as text/html) for html and git-html artifacts. "
        "Markdown rendered from a git repository has no retained source: that "
        "answers 404 with a JSON pointer back to the repository, ref and "
        "commit.\n\n"
        "This route never converts: it serves what was submitted. For "
        "Markdown of an HTML-only artifact, use /a/{id}/export/markdown.\n\n"
        "The HTML answer is publisher-controlled markup served as a top-level "
        "document, so — exactly like /a/{id}/raw — it carries "
        "`Content-Security-Policy: sandbox allow-scripts allow-popups "
        "allow-forms allow-downloads` and `X-Content-Type-Options: nosniff`. "
        "Browsers render it in a unique opaque origin (the isolation the "
        "wrapper page gets from its sandboxed iframe), while machine clients "
        "ignore the headers and receive the exact stored bytes. The Markdown "
        "answer is not executable and is served without that CSP.\n\n"
        + PASSWORD_GATE_NOTE
    ),
    responses={
        200: {
            "description": (
                "The retained source: Markdown for markdown artifacts, HTML "
                "otherwise."
            ),
            "content": {**CONTENT_MARKDOWN, **CONTENT_HTML},
        },
        401: {
            "description": (
                "Password required or wrong (see the X-Artifact-Password "
                "header)."
            )
        },
        404: {
            "description": (
                "No artifact with this id, it has no live version, or its "
                "source was not retained (git-sourced Markdown)."
            )
        },
        429: RESP_UNLOCK_429,
        502: RESP_HUB_502,
    },
)
def read_source(
    request: Request,
    artifact_id: str = PathParam(..., description=SHARE_ID_DESC),
) -> Response:
    """The retained source: the author's Markdown when there is one, else HTML."""
    public_id = artifact_id
    meta = _public_meta_of(request, public_id)
    if meta is None:
        return _not_found(public_id)
    if not reader_allowed(meta, request):
        return _password_required()
    envelope = request.app.state.store.get_head(meta.id)
    if envelope is None:
        return _not_found(public_id)

    markdown = envelope.source.get("markdown")
    if isinstance(markdown, str):
        _record_view(request.app, meta.id, "source")
        return PlainTextResponse(
            markdown, media_type="text/markdown; charset=utf-8"
        )
    if envelope.source_type in ("html", "git-html"):
        _record_view(request.app, meta.id, "source")
        # Executable publisher HTML at top level — same opaque-origin sandbox
        # as /raw. (The Markdown branch above needs none: it is inert text.)
        return _sandboxed_html(envelope.html)
    return JSONResponse(
        status_code=404,
        content={
            "error": "source not retained",
            "detail": (
                "This artifact was rendered from Markdown in a git repository; "
                "only the built HTML is kept. Fetch the source from the "
                "repository instead."
            ),
            "git": envelope.source.get("git"),
        },
    )


@app.get(
    "/a/{artifact_id}/meta",
    tags=["public"],
    summary="Public artifact metadata",
    description=(
        "JSON metadata of the artifact and its head version: title, source "
        "type, timestamps, size, head version, total and proposed version "
        "counts, the 'protected', 'accept_versions' and "
        "'accept_versions_mode' settings, the derived 'contributions_frozen' "
        "flag, and every public URL. Deliberately carries no owner identity "
        "and no password record. Unlike the content endpoints this is "
        "readable even when the artifact is password-protected — it is "
        "metadata only.\n\n" + STATUS_VS_DOCUMENT_STATUS_NOTE
    ),
    responses={
        200: {"description": "The artifact's public metadata."},
        404: {
            "description": (
                "No artifact exists with this id, or it has no live version."
            )
        },
        502: RESP_HUB_502,
    },
)
def read_meta(
    request: Request,
    artifact_id: str = PathParam(..., description=SHARE_ID_DESC),
) -> Response:
    """Public metadata; available even when the artifact is password-protected."""
    public_id = artifact_id
    meta = _public_meta_of(request, public_id)
    if meta is None:
        return _not_found(public_id)
    store = request.app.state.store
    head = store.get_head(meta.id)
    if head is None:
        return _not_found(public_id)
    versions = store.list_versions(meta.id)
    return JSONResponse(
        {
            **_public_version_meta(head.public_meta(is_head=True)),
            # Public metadata names the artifact by its public identifier: a
            # capability-URL holder is not entitled to the internal id.
            "id": meta.share_id,
            "protected": bool(meta.password),
            # The owner's raw contribution setting, kept raw: it is what the
            # owner configured, not what is currently possible.
            "accept_versions": meta.accept_versions,
            "accept_versions_mode": meta.accept_versions_mode,
            # ... while these two describe the *document*. The spread above
            # carries the head *version's* "status" ('live'/'proposed'), which
            # is a different axis entirely - see
            # STATUS_VS_DOCUMENT_STATUS_NOTE.
            "document_status": meta.status,
            "contributions_frozen": meta.is_frozen(),
            "created_at": meta.created_at,
            "updated_at": meta.updated_at,
            "head_version": head.version,
            "versions_count": len(versions),
            "proposed_count": sum(
                1 for row in versions if row.get("status") == STATUS_PROPOSED
            ),
            **artifact_urls(base_url(request), meta.share_id),
        }
    )


#: The fields ``GET /a/{id}/live`` reports. Kept as an explicit tuple because
#: :func:`_live_etag` has to stay a *complete* summary of them: every one of
#: these must be reachable only through a mutation that bumps an artifact's
#: revision, or the ETag would go stale while the body moved.
_LIVE_FIELDS = (
    "id",
    "head_version",
    "updated_at",
    "versions_count",
    "proposed_count",
    "comment_threads",
    "document_status",
    "contributions_frozen",
)


def _live_snapshot(request: Request, meta: ArtifactMeta) -> dict:
    """The tiny change-detection payload behind ``GET /a/{id}/live``.

    Built only when the ETag did *not* match, since this is the expensive
    half: it enumerates versions and comment threads. The result is projected
    through :data:`_LIVE_FIELDS` so the body can never quietly grow a field
    the revision-derived ETag does not summarise.
    """
    store = request.app.state.store
    head = store.get_head(meta.id)
    versions = store.list_versions(meta.id)
    snapshot = {
        # The public identifier, exactly as /a/{id}/meta reports it: a
        # capability-URL holder is not entitled to the internal id.
        "id": meta.share_id,
        "head_version": head.version if head is not None else None,
        "updated_at": meta.updated_at,
        "versions_count": len(versions),
        "proposed_count": sum(
            1 for row in versions if row.get("status") == STATUS_PROPOSED
        ),
        "comment_threads": len(request.app.state.comments.list_for(meta.id)),
        "document_status": meta.status,
        "contributions_frozen": meta.is_frozen(),
    }
    return {key: snapshot[key] for key in _LIVE_FIELDS}


def _live_etag(request: Request, meta: ArtifactMeta) -> str:
    """A strong ETag for ``GET /a/{id}/live``, derived without reading anything.

    Closes REL-100-003. This used to hash the finished snapshot, which meant a
    poll had to enumerate every version and every comment thread *before* it
    could discover that the answer was 304 — so the conditional request saved
    response bytes but none of the work, and a page left open in a tab (or a
    capability-URL holder in a loop) could amplify a trivial request into
    repeated Storage listings and envelope parsing. Now the tag comes from two
    in-memory counters, so an unchanged artifact costs one dictionary lookup
    each.

    The tag is a complete summary of :data:`_LIVE_FIELDS` because every one of
    those fields is reachable only through a store mutation that bumps a
    revision: ``head_version``, ``versions_count`` and ``proposed_count``
    through the version writes and deletes, ``id`` (the share id),
    ``updated_at``, ``document_status`` and ``contributions_frozen`` through
    the meta save every artifact-level change goes through, and
    ``comment_threads`` through the comment store's own ledger. That the two
    counters are in memory rather than persisted is exact, not approximate,
    under this service's single-writer deployment — see
    :class:`~src.store.RevisionLedger` for the argument, and for why a boot
    nonce makes a restart re-issue fresh tags instead of stale ones.

    Truncated to 32 hex characters: still 128 bits, and short enough to keep
    the request header small when a browser echoes it back on every poll.
    """
    parts = [
        # The share id, so a rotated link never inherits the old link's tag
        # even at the same revision.
        meta.share_id,
        request.app.state.store.revision(meta.id),
        request.app.state.comments.revision(meta.id),
    ]
    canonical = json.dumps(parts, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f'"{digest}"'


def _etag_matches(header: str | None, etag: str) -> bool:
    """True when an ``If-None-Match`` header covers ``etag``.

    Handles the comma-separated list and the ``W/`` weak prefix a browser or
    an intermediary may add; ``*`` matches anything, per RFC 9110.
    """
    if not header:
        return False
    for candidate in header.split(","):
        value = candidate.strip()
        if value == "*":
            return True
        if value.startswith("W/"):
            value = value[2:].strip()
        if value == etag:
            return True
    return False


@app.get(
    "/a/{artifact_id}/live",
    tags=["public"],
    summary="Change-detection snapshot",
    description=(
        "A deliberately tiny JSON snapshot of everything a reader's open page "
        "needs in order to notice that something moved: 'id', "
        "'head_version', 'updated_at', 'versions_count', 'proposed_count', "
        "'comment_threads', 'document_status' and 'contributions_frozen'. It "
        "carries no content, no owner identity and no password record — it is "
        "a change *signal*, not a payload, and the pages fetch the real thing "
        "(/a/{id}/raw, /a/{id}/versions, /a/{id}/comments) once it tells them "
        "to.\n\n"
        "This is what the rendered artifact page, the review UI and the admin "
        "studio poll, roughly every ten seconds while their tab is visible, "
        "to update a reader's screen without a reload. Polling — rather than "
        "server-sent events or a websocket — because this service runs behind "
        "a buffering platform proxy that would hold a long-lived stream; a "
        "conditional GET survives it.\n\n"
        "The response carries a strong `ETag`. Send it back as "
        "`If-None-Match` and an unchanged artifact answers **304 with no "
        "body** — and without the server enumerating anything either: the "
        "tag is derived from a per-artifact revision counter that "
        "every mutation bumps, so an unchanged poll is answered from memory. "
        "The tag is opaque and process-local; after a restart the same "
        "artifact answers with a *new* tag and one extra refresh, which is "
        "the safe direction to be wrong in. "
        "`Cache-Control: no-store` keeps any intermediary from ever serving a "
        "stale snapshot in place of a fresh one.\n\n"
        "Like GET /a/{id}/meta — and unlike every content endpoint — this is "
        "readable while the artifact is password-protected: it is metadata "
        "only, and a reader sitting behind the unlock form still has to pass "
        "that form to see anything it points at.\n\n"
        + STATUS_VS_DOCUMENT_STATUS_NOTE
    ),
    responses={
        200: {
            "description": (
                "The snapshot, with a strong ETag for the next conditional "
                "request."
            )
        },
        304: {
            "description": (
                "The If-None-Match tag still matches: nothing changed, and no "
                "body is sent."
            )
        },
        404: {
            "description": (
                "No artifact exists with this id — it never did, it was "
                "trashed or purged, or its share link has been rotated and "
                "this is the old one. A polling client stops on this."
            )
        },
        502: RESP_HUB_502,
    },
)
def read_live(
    request: Request,
    artifact_id: str = PathParam(..., description=SHARE_ID_DESC),
) -> Response:
    """Change-detection snapshot; public even when the artifact is protected."""
    public_id = artifact_id
    meta = _public_meta_of(request, public_id)
    if meta is None:
        return _not_found(public_id)
    # The ETag first, and the snapshot only if it does not match: enumerating
    # versions and threads to build a body nobody will receive is exactly the
    # amplification REL-100-003 describes.
    etag = _live_etag(request, meta)
    headers = {"ETag": etag, "Cache-Control": "no-store"}
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return JSONResponse(_live_snapshot(request, meta), headers=headers)

@app.get(
    "/a/{artifact_id}/v/{version}",
    tags=["public"],
    response_class=HTMLResponse,
    summary="One specific version",
    description=(
        "Serves a single version, live or proposed, in the same sandboxed "
        "wrapper page as GET /a/{id}. A "
        "*proposed* version is private: only the artifact owner and the "
        "version's own author may read it, and they identify themselves by "
        "sending the two management headers (X-StorageApi-Token and "
        "X-Storage-Stack) on this otherwise unauthenticated read. Anyone else "
        "gets 403.\n\n" + PASSWORD_GATE_NOTE
    ),
    responses={
        200: {
            "description": "That version's HTML document.",
            "content": CONTENT_HTML,
        },
        401: {
            "description": (
                "Password-protected artifact; the HTML unlock form is returned."
            ),
            "content": CONTENT_HTML,
        },
        403: {
            "description": (
                "This version is a proposal and the caller is neither the "
                "owner nor its author."
            )
        },
        404: {"description": "No artifact, or no such version."},
        422: {"description": "'version' is not an integer."},
        429: RESP_UNLOCK_429,
        502: RESP_HUB_502,
    },
)
def read_version(
    request: Request,
    artifact_id: str = PathParam(..., description=SHARE_ID_DESC),
    version: int = PathParam(..., description=VERSION_DESC),
) -> Response:
    """Serve one version, honoring both the password gate and proposal privacy."""
    public_id = artifact_id
    meta = _public_meta_of(request, public_id)
    if meta is None:
        return _not_found(public_id)
    if not reader_allowed(meta, request):
        return HTMLResponse(unlock_page(public_id, None), status_code=401)
    envelope = request.app.state.store.get_version(meta.id, version)
    if envelope is None:
        return _version_not_found(public_id, version)
    if not may_see(meta, envelope, optional_caller(request)):
        return _proposal_hidden(public_id, version)
    _record_view(request.app, meta.id, "version")
    return _framed(request, meta, envelope, pinned=True)


@app.get(
    "/a/{artifact_id}/versions",
    tags=["public"],
    summary="Version history",
    description=(
        "Lists every version (live and proposed) newest first, with its "
        "number, title, status, author project, note, size, source type and "
        "creation time, plus the current head version, the artifact's "
        "'protected', 'accept_versions' and 'accept_versions_mode' settings, "
        "its 'document_status' and the derived 'contributions_frozen' "
        "flag.\n\n" + STATUS_VS_DOCUMENT_STATUS_NOTE + "\n\n"
        "Each row's own 'status' is therefore the version's ('live' or "
        "'proposed'), never the document's.\n\n"
        "Proposal *metadata* is public to capability-URL holders; proposal "
        "*content* is not — fetching it still requires being the owner or the "
        "author (see GET /a/{id}/v/{n}).\n\n"
        "Every proposed row also carries 'outdated': true when the submitter "
        "declared a 'base_version' and the head has moved on since — the "
        "proposal was written without seeing the versions published after it. "
        "Live rows carry no 'outdated' key at all.\n\n"
        "The 'format' query parameter selects the rendering: 'json' (the "
        "default) returns the machine-readable history; 'html' returns a "
        "styled picker page with links to each version and to the diff of "
        "every adjacent pair. Any other value is treated as 'json'.\n\n"
        + PASSWORD_GATE_NOTE
    ),
    responses={
        200: {
            "description": (
                "The version history as JSON, or the picker page when "
                "format=html."
            ),
            "content": {"application/json": {}, **CONTENT_HTML},
        },
        401: {
            "description": (
                "Password required or wrong: JSON for format=json, the HTML "
                "unlock form for format=html."
            )
        },
        404: RESP_NOT_FOUND,
        429: RESP_UNLOCK_429,
        502: RESP_HUB_502,
    },
)
def read_versions(
    request: Request,
    artifact_id: str = PathParam(..., description=SHARE_ID_DESC),
    format: str = Query(
        "json",
        description=(
            "Rendering of the history: 'json' (default, machine-readable) or "
            "'html' (a human-readable picker page). Any other value falls "
            "back to 'json'."
        ),
        # Documentation only: the runtime deliberately accepts anything and
        # falls back to JSON, so declaring a Literal here would turn today's
        # silent fallback into a 422.
        json_schema_extra={"enum": ["json", "html"]},
    ),
) -> Response:
    """Version history as JSON, or a styled picker page with ?format=html."""
    public_id = artifact_id
    meta = _public_meta_of(request, public_id)
    if meta is None:
        return _not_found(public_id)
    wants_html = format == "html"
    if not reader_allowed(meta, request):
        if wants_html:
            return HTMLResponse(unlock_page(public_id, None), status_code=401)
        return _password_required()

    store = request.app.state.store
    versions = [_public_version_meta(row) for row in store.list_versions(meta.id)]
    head_version = _head_version_of(request, meta.id)
    base = base_url(request)

    if wants_html:
        return HTMLResponse(
            versions_page(
                base,
                public_id,
                versions,
                head_version,
                meta.accept_versions,
                bool(meta.password),
            )
        )

    root = f"{base.rstrip('/')}/a/{meta.share_id}"
    return JSONResponse(
        {
            "id": meta.share_id,
            "head_version": head_version,
            # Raw owner setting (see /a/{id}/meta) ...
            "accept_versions": meta.accept_versions,
            "accept_versions_mode": meta.accept_versions_mode,
            # ... and the document-level facts. Each row below carries its own
            # "status", which is the *version's* ('live'/'proposed').
            "document_status": meta.status,
            "contributions_frozen": meta.is_frozen(),
            "protected": bool(meta.password),
            "versions": [
                {
                    **row,
                    **_outdated_flag(row, head_version),
                    "url": f"{root}/v/{row['version']}",
                }
                for row in versions
            ],
        }
    )


@app.get(
    "/a/{artifact_id}/diff/{spec}",
    tags=["public"],
    summary="Diff two versions",
    description=(
        "Compares two versions of one artifact, given in the path as "
        "'{older}..{newer}' (for example 3..5); the older number must come "
        "first. Markdown is compared when "
        "both versions carry Markdown source, otherwise the built HTML is.\n\n"
        "The 'format' query parameter selects the rendering: 'html' (the "
        "default) is a side-by-side page for humans, 'unified' is a "
        "text/plain unified diff, 'json' returns the unified diff plus "
        "added/removed line counts, and 'visual' renders the two versions "
        "themselves side by side — each in its own iframe sandboxed without "
        "allow-same-origin, with the panes' scrolling synchronized — for "
        "comparing what a reader actually sees rather than the source that "
        "produced it. An unknown value is a 400.\n\n"
        "Either side may be a proposal, in which case the same rule as GET "
        "/a/{id}/v/{n} applies: send the two management headers to read it as "
        "the owner or the author, otherwise 403.\n\n" + PASSWORD_GATE_NOTE
    ),
    responses={
        200: {
            "description": (
                "The diff in the requested rendering: HTML page, plain-text "
                "unified diff, or JSON."
            ),
            "content": {
                **CONTENT_HTML,
                **CONTENT_TEXT,
                "application/json": {},
            },
        },
        400: {
            "description": (
                "Malformed diff spec (not OLD..NEW, or OLD not strictly "
                "smaller than NEW) or an unknown 'format'."
            )
        },
        401: {
            "description": (
                "Password required or wrong (see the X-Artifact-Password "
                "header)."
            )
        },
        403: {"description": "One side is a proposal the caller may not read."},
        404: {
            "description": "No artifact, or one of the versions does not exist."
        },
        413: {
            "description": (
                "One side is larger than the configured HUB_DIFF_MAX_BYTES. "
                "For 'visual' the rendered HTML of each side is what is "
                "measured, since that is what the page has to carry."
            )
        },
        429: RESP_UNLOCK_429,
        502: RESP_HUB_502,
    },
)
def read_diff(
    request: Request,
    artifact_id: str = PathParam(..., description=SHARE_ID_DESC),
    spec: str = PathParam(..., description=SPEC_DESC),
    format: str = Query(
        "html",
        description=(
            "Rendering of the diff: 'html' (default, side-by-side page), "
            "'unified' (text/plain unified diff), 'json' (unified diff plus "
            "added/removed counts) or 'visual' (the two rendered documents "
            "side by side in sandboxed iframes, scrolling in step). Anything "
            "else is a 400."
        ),
        # Documentation only — validation stays in compute_diff so an unknown
        # format keeps answering 400 rather than FastAPI's 422.
        json_schema_extra={"enum": ["html", "unified", "json", "visual"]},
    ),
) -> Response:
    """Diff two versions of one artifact in the requested rendering."""
    match = _DIFF_SPEC.match(spec)
    if match is None:
        return JSONResponse(
            status_code=400,
            content={
                "error": "malformed diff spec",
                "detail": "Use {older}..{newer}, for example 3..5",
                "spec": spec,
            },
        )
    older, newer = int(match.group(1)), int(match.group(2))
    if older >= newer:
        # The operands are *named* older and newer, and every rendering
        # labels them that way: the side-by-side page, the unified diff's
        # -/+ signs and the JSON "added"/"removed" counts. Accepting 2..1
        # would answer with additions labelled as removals, so a reader --
        # or an agent summarising the stats -- concludes the reverse of what
        # happened. Answered before any lookup, since ordering is a property
        # of the spec, not of the artifact.
        return JSONResponse(
            status_code=400,
            content={
                "error": "malformed diff spec",
                "detail": (
                    "The older version must come first and the two must "
                    "differ. Use {older}..{newer}, for example 3..5"
                ),
                "spec": spec,
            },
        )

    public_id = artifact_id
    meta = _public_meta_of(request, public_id)
    if meta is None:
        return _not_found(public_id)
    if not reader_allowed(meta, request):
        return _password_required()

    store = request.app.state.store
    caller = None
    envelopes: list[Envelope] = []
    for number in (older, newer):
        envelope = store.get_version(meta.id, number)
        if envelope is None:
            return _version_not_found(public_id, number)
        if envelope.status == STATUS_PROPOSED:
            if caller is None:
                caller = optional_caller(request)
            if not may_see(meta, envelope, caller):
                return _proposal_hidden(public_id, number)
        envelopes.append(envelope)

    if format == "visual":
        return _visual_diff(request, envelopes[0], envelopes[1])

    try:
        content_type, body = compute_diff(
            envelopes[0], envelopes[1], format, settings.diff_max_bytes
        )
    except DiffError as exc:
        status_code = 413 if "too large" in str(exc).lower() else 400
        return JSONResponse(status_code=status_code, content={"error": str(exc)})
    return Response(content=body, media_type=content_type)


def _visual_diff(request: Request, older: Envelope, newer: Envelope) -> Response:
    """Render the ``format=visual`` page: two documents, side by side.

    Unlike the other three formats this one carries the *rendered* documents
    rather than a comparison of their source, so the size guard is applied to
    the HTML each pane has to hold — one 10 MB artifact would otherwise arrive
    as a 20 MB page. The add/remove counts in the header still come from
    :func:`~src.diff.compute_diff`, so the numbers a reader sees here and on
    ``?format=json`` can never drift apart.
    """
    limit = settings.diff_max_bytes
    for env in (older, newer):
        size = len((env.html or "").encode("utf-8"))
        if size > limit:
            return JSONResponse(
                status_code=413,
                content={
                    "error": (
                        f"v{env.version} is too large to show side by side "
                        f"({size} bytes > {limit})"
                    )
                },
            )

    added = removed = None
    try:
        _content_type, payload = compute_diff(older, newer, "json", limit)
        stats = json.loads(payload).get("stats") or {}
        added, removed = stats.get("added"), stats.get("removed")
    except DiffError as exc:
        # The rendered sides fit but their comparable text does not (or cannot
        # be compared). The page is still the useful answer — it just says
        # nothing about how many lines moved.
        logger.info(
            "No diff statistics for the visual diff of %s (v%d..v%d): %s",
            older.id,
            older.version,
            newer.version,
            exc,
        )
    except (ValueError, TypeError) as exc:  # pragma: no cover - defensive
        logger.warning("Unreadable diff statistics: %s", exc)

    return HTMLResponse(
        visual_diff_page(older, newer, added=added, removed=removed),
        headers={"Content-Security-Policy": "frame-ancestors 'self'"},
    )


@app.get(
    "/a/{artifact_id}/comments",
    tags=["public"],
    summary="Inline comment threads",
    description=(
        "Every inline comment thread of this artifact, oldest first, together "
        "with the artifact's current 'comments_mode' and its draft/final "
        "document status — reported both as 'document_status' (the key every "
        "other endpoint uses) and, unchanged for backwards compatibility, as "
        "'status'. On this endpoint 'status' has always meant the "
        "*document's* status, not a version's; both keys carry the same "
        "value. Each thread carries its TextQuoteSelector (the quote plus "
        "a little surrounding context), the comment body, its replies, the "
        "resolved flag and the *project identity* of everyone who spoke — "
        "project id, project name and stack hostname only, never a full stack "
        "URL and never an internal owner key.\n\n"
        "Threads are public to capability-URL holders; writing one needs a "
        "Storage token (POST /api/artifacts/{id}/comments).\n\n"
        + STATUS_VS_DOCUMENT_STATUS_NOTE
        + "\n\n"
        + PASSWORD_GATE_NOTE
    ),
    responses={
        200: {
            "description": (
                "JSON with 'id', 'comments_mode', 'document_status' (also "
                "echoed as the legacy 'status') and the 'threads' array "
                "(possibly empty)."
            )
        },
        401: {
            "description": (
                "Password required or wrong (see the X-Artifact-Password "
                "header)."
            )
        },
        404: RESP_NOT_FOUND,
        429: RESP_UNLOCK_429,
        502: RESP_HUB_502,
    },
)
def read_comments(
    request: Request,
    artifact_id: str = PathParam(..., description=SHARE_ID_DESC),
) -> Response:
    """List every comment thread of one artifact, oldest first."""
    public_id = artifact_id
    meta = _public_meta_of(request, public_id)
    if meta is None:
        return _not_found(public_id)
    if not reader_allowed(meta, request):
        return _password_required()
    threads = request.app.state.comments.list_for(meta.id)
    return JSONResponse(
        {
            "id": meta.share_id,
            "comments_mode": meta.comments_mode,
            # "status" here has always meant the *document's* status, unlike
            # every other public payload where it is a version's. It stays
            # exactly as it is for existing callers (the review page reads
            # it), and "document_status" is the name to use going forward.
            "status": meta.status,
            "document_status": meta.status,
            "threads": [thread.public_dict() for thread in threads],
        }
    )


@app.get(
    "/a/{artifact_id}/export/markdown",
    tags=["public"],
    response_class=MarkdownResponse,
    summary="Download the head version's Markdown",
    description=(
        "Downloads the served version as a single Markdown file "
        "(text/markdown). Precedence: the author's own Markdown when the "
        "version has one — either because it was authored in Markdown, or "
        "because an HTML artifact was published with 'markdown_source' — and "
        "otherwise Markdown converted from the built HTML document.\n\n"
        "The X-Artifact-Markdown-Source response header says which you got: "
        "'original' for the author's own text (byte for byte), 'converted' "
        "for a conversion. A conversion is lossy — a <canvas> chart or an "
        "<svg> illustration degrades to a one-line placeholder and inlined "
        "images are dropped — so publishers are asked to always supply "
        "Markdown. In the rare case where conversion fails outright the "
        "header says 'none' and the HTML document itself is served "
        "(text/html), which is what this route used to do for every "
        "HTML-authored artifact.\n\n"
        "The response carries a Content-Disposition attachment filename "
        "derived from the version's title, so a browser 'Save as' lands on "
        "something readable.\n\n" + PASSWORD_GATE_NOTE
    ),
    responses={
        200: {
            "description": (
                "The head version's Markdown, as a file attachment (or the "
                "HTML document when conversion failed; see "
                "X-Artifact-Markdown-Source)."
            ),
            "content": {**CONTENT_MARKDOWN, **CONTENT_HTML},
            "headers": {
                "X-Artifact-Markdown-Source": {
                    "description": (
                        "'original' (the author's own Markdown, verbatim), "
                        "'converted' (reverse-engineered from the HTML "
                        "document) or 'none' (conversion failed; the HTML "
                        "document is served instead)."
                    ),
                    "schema": {
                        "type": "string",
                        "enum": ["original", "converted", "none"],
                    },
                }
            },
        },
        401: {
            "description": (
                "Password required or wrong (see the X-Artifact-Password "
                "header)."
            )
        },
        404: {
            "description": (
                "No artifact exists with this id, or it has no live version."
            )
        },
        429: RESP_UNLOCK_429,
        502: RESP_HUB_502,
    },
)
def export_markdown(
    request: Request,
    artifact_id: str = PathParam(..., description=SHARE_ID_DESC),
) -> Response:
    """The head version's Markdown (author's own, or converted) as a file."""
    public_id = artifact_id
    meta = _public_meta_of(request, public_id)
    if meta is None:
        return _not_found(public_id)
    if not reader_allowed(meta, request):
        return _password_required()
    head = request.app.state.store.get_head(meta.id)
    if head is None:
        return _not_found(public_id)

    filename, content_type, body, kind = export.head_source(head)
    return Response(
        content=body,
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # Callers care whether they are reading the author's own words or
            # something reverse-engineered from HTML.
            "X-Artifact-Markdown-Source": kind,
        },
    )


# --------------------------------------------------------------------------
# Vault export budgets (REL-100-002)
#
# Building a vault is the most expensive thing an unauthenticated caller can
# ask this service to do: it diffs every visible version against its
# predecessor and converts every HTML-authored one to Markdown. Two bounds sit
# in front of it, using the same counter machinery as every other limit here —
# a size ceiling on what a single build may chew through, and an hourly budget
# on how often one client may ask for a build of one artifact.
# --------------------------------------------------------------------------

#: Counter scope for vault builds; keyed ``{artifact_id}:{client_ip}``, bucketed
#: by UTC hour, exactly like the unlock throttle.
COUNTER_EXPORTS = "exports"


def _claim_export_slot(
    app_obj: FastAPI | None, artifact_id: str, client_ip: str
) -> bool:
    """Count one vault build; False once the hourly budget is spent.

    Bumped whether or not the budget was still open, like every other
    ``_claim_*`` here: a caller who keeps hammering a spent budget keeps being
    counted, so the window is self-limiting rather than a retry loop.
    """
    used = _bump_counter(
        app_obj,
        COUNTER_EXPORTS,
        _counter_key(artifact_id, client_ip),
        _utc_hour(),
    )
    return used <= settings.max_exports_per_hour


def _export_rate_limited() -> JSONResponse:
    """429 for a spent vault-export budget, in the shape the other limits use."""
    return JSONResponse(
        status_code=429,
        content={
            "error": "too many exports this hour",
            "detail": (
                f"At most {settings.max_exports_per_hour} vault exports of "
                "one artifact may be built per client address per hour "
                "(HUB_MAX_EXPORTS_PER_HOUR)."
            ),
            "limit": settings.max_exports_per_hour,
        },
    )


def _export_too_large(size: int, limit: int) -> JSONResponse:
    """413 for an artifact whose history is too large to export at all."""
    return JSONResponse(
        status_code=413,
        content={
            "error": "export too large",
            "detail": (
                f"This artifact's visible history and comments come to about "
                f"{size} characters, over this hub's {limit}-byte export "
                "ceiling (HUB_EXPORT_MAX_BYTES). Ask the owner to delete "
                "older versions, or export a single version with "
                "GET /a/{id}/export/markdown."
            ),
            "limit": limit,
        },
    )


@app.get(
    "/a/{artifact_id}/export/vault",
    tags=["public"],
    summary="Download an Obsidian vault of the whole artifact",
    description=(
        "Builds a ZIP holding a ready-to-open Obsidian vault: "
        "INDEX.md (a wikilink hub), document.md (the served version), "
        "versions/v{n}.md (one note per version — proposals included — with "
        "author, date, status, note and a diff against its predecessor), "
        "comments/{tid}.md (one note per thread: the quote, the discussion "
        "and the resolution) and reasoning.md, a chronological trail of how "
        "the document ended up this way.\n\n"
        "The archive is deterministic: the same artifact state always "
        "produces byte-identical bytes.\n\n"
        "Two bounds guard the build, because it is the most expensive thing "
        "an unauthenticated caller can ask for. An artifact whose visible "
        "history and comments exceed HUB_EXPORT_MAX_BYTES is refused with "
        "**413** before anything is rendered; export a single version with "
        "GET /a/{id}/export/markdown instead. And one client address may "
        "build HUB_MAX_EXPORTS_PER_HOUR vaults of one artifact per hour, "
        "after which the answer is **429**.\n\n" + PASSWORD_GATE_NOTE
    ),
    responses={
        200: {
            "description": "The vault, as a ZIP file attachment.",
            "content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}},
        },
        401: {
            "description": (
                "Password required or wrong (see the X-Artifact-Password "
                "header)."
            )
        },
        404: RESP_NOT_FOUND,
        413: {
            "description": (
                "This artifact's visible history and comments are larger than "
                "HUB_EXPORT_MAX_BYTES; no archive was built."
            )
        },
        429: {
            "description": (
                "Either this client address built HUB_MAX_EXPORTS_PER_HOUR "
                "vaults of this artifact in the current hour, or it made "
                "HUB_MAX_UNLOCK_ATTEMPTS_PER_HOUR failed password attempts "
                "on it."
            )
        },
        502: RESP_HUB_502,
    },
)
def export_vault(
    request: Request,
    artifact_id: str = PathParam(..., description=SHARE_ID_DESC),
) -> Response:
    """The whole artifact — versions, comments and timeline — as a vault ZIP."""
    public_id = artifact_id
    meta = _public_meta_of(request, public_id)
    if meta is None:
        return _not_found(public_id)
    if not reader_allowed(meta, request):
        return _password_required()

    # Budgets are claimed after the password gate, so a protected artifact's
    # export budget cannot be spent by somebody who never got past it, and
    # keyed by the *internal* id, so rotating the link does not hand a caller
    # a fresh allowance.
    if not _claim_export_slot(request.app, meta.id, _client_ip(request)):
        return _export_rate_limited()

    store = request.app.state.store
    # Proposals are moderated content: the public vault must not leak them.
    # Only the owner or a proposal's author (authenticated via the standard
    # token headers) gets them included in their download.
    caller = optional_caller(request)
    limit = settings.export_max_bytes

    # Refuse an oversized artifact *before* rendering, and before the load
    # itself finishes: the running total is checked after every envelope and
    # every thread as it comes in from Storage, not once at the end. Summing
    # a fully materialized (envelopes, threads) list first and estimating
    # afterwards would let the very allocation this budget exists to bound --
    # up to HUB_MAX_VERSIONS x HUB_MAX_ENVELOPE_BYTES of parsed envelopes --
    # happen before the budget is ever consulted (REL-100-002). The moment
    # the running total crosses the limit, loading stops: later versions are
    # never fetched, comments are never even listed, and whatever was
    # accumulated so far is discarded along with the request.
    envelopes: list[Envelope] = []
    running_total = 0
    for row in store.list_versions(meta.id):
        envelope = store.get_version(meta.id, row["version"])
        if envelope is None or not may_see(meta, envelope, caller):
            continue
        envelopes.append(envelope)
        running_total += export.envelope_source_chars(envelope)
        if 0 < limit < running_total:
            logger.info(
                "Refusing a vault export of artifact %s: about %d characters "
                "of source in the versions alone, over the %d limit",
                meta.id, running_total, limit,
            )
            return _export_too_large(running_total, limit)

    threads = request.app.state.comments.list_for(meta.id)
    for thread in threads:
        running_total += export.thread_source_chars(thread)
        if 0 < limit < running_total:
            logger.info(
                "Refusing a vault export of artifact %s: about %d characters "
                "of source, over the %d limit",
                meta.id, running_total, limit,
            )
            return _export_too_large(running_total, limit)

    estimate = running_total

    # Streamed into an owner-only scratch file rather than a BytesIO: the peak
    # memory of an anonymous request must not be the caller's to choose.
    try:
        filename, path, size = export.build_vault_file(
            meta, envelopes, threads, settings.cache_dir, max_bytes=limit
        )
    except export.ExportTooLarge as exc:
        # The write-time backstop fired, so the estimate under-counted. The
        # partial file is already gone; report the same 413 as the estimate.
        logger.warning(
            "Vault export of artifact %s overran the %d byte ceiling while "
            "being written (estimate said %d)",
            meta.id, exc.limit, estimate,
        )
        return _export_too_large(exc.size, exc.limit)
    except OSError as exc:
        logger.error("Cannot write a vault export of artifact %s: %s", meta.id, exc)
        return JSONResponse(
            status_code=502,
            content={
                "error": "export unavailable",
                "detail": "The vault could not be written on the server.",
            },
        )

    logger.info("Built a %d byte vault export of artifact %s", size, meta.id)
    return FileResponse(
        path,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        # The scratch file holds a full copy of an artifact that may be
        # password-protected; it lives exactly as long as the response does.
        background=BackgroundTask(_unlink_export, path),
    )


def _unlink_export(path: Path) -> None:
    """Remove a finished export's scratch file. Never raises into serving."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:  # noqa: BLE001 - cleanup must not break a response
        logger.warning("Could not remove the export scratch file %s: %s", path, exc)


@app.get(
    "/a/{artifact_id}/guest",
    tags=["public"],
    summary="Who am I, as an invited guest?",
    description=(
        "Checks a guest invitation and answers with the name it carries, so a "
        "client can say 'commenting as Jana (guest)' before anybody writes "
        "anything. Send the credential in the X-Artifact-Guest header, shaped "
        "'{invitation_id}.{secret}' — the two halves of what the review URL's "
        "#invite= fragment contains.\n\n"
        "The review UI calls this on load; an agent holding an invitation can "
        "use it the same way, as a cheap 'is this link still good?' probe. "
        "Nothing is recorded and nothing is returned but the display name and "
        "the (non-secret) invitation id.\n\n"
        "A missing, malformed, revoked or wrong credential all answer the same "
        "401, deliberately: telling them apart would make this an oracle over "
        "somebody else's invitation. Checking a credential runs a full PBKDF2, "
        "so *failed* checks are rate-limited per artifact and client address "
        "the way failed passwords are — grinding secrets against a known "
        "invitation id answers 429, not 401."
    ),
    responses={
        200: {
            "description": (
                "The invitation is valid; JSON with 'name', 'invitation_id' "
                "and the artifact's share 'id'."
            )
        },
        401: {
            "description": (
                "No usable X-Artifact-Guest credential for this artifact."
            )
        },
        404: RESP_NOT_FOUND,
        429: RESP_GUEST_429,
        502: RESP_HUB_502,
    },
)
def guest_identity(
    request: Request,
    artifact_id: str = PathParam(..., description=SHARE_ID_DESC),
) -> Response:
    """Resolve an invitation credential to the guest's display name."""
    public_id = artifact_id
    meta = _public_meta_of(request, public_id)
    if meta is None:
        return _not_found(public_id)
    # A password-protected artifact still gates its guests: an invitation is a
    # grant to *comment*, not a way around the reader password.
    if not reader_allowed(meta, request):
        return _password_required()

    credential = _guest_credential(request)
    if credential is None:
        raise HTTPException(
            status_code=401,
            detail=(
                "send the invitation credential in the "
                f"{GUEST_HEADER} header, shaped '{{invitation_id}}.{{secret}}'"
            ),
        )
    invitation = _verify_guest_checked(request, meta, credential)
    return JSONResponse(
        {
            "id": meta.share_id,
            "invitation_id": str(invitation.get("id") or ""),
            "name": str(invitation.get("name") or ""),
        }
    )


@app.get(
    "/a/{artifact_id}/review",
    tags=["public"],
    response_class=HTMLResponse,
    summary="Review UI (document plus inline comments)",
    description=(
        "A two-pane review page: the artifact on the left, its comment "
        "threads on the right. Select any text to open a thread on that "
        "quote, click a highlight to jump to its thread, and reply or resolve "
        "in place.\n\n"
        "The page itself is public and carries no credential. Its JavaScript "
        "fetches this artifact's /raw, /versions and /comments, injects a "
        "small annotation script into the fetched HTML and renders it in a "
        "srcdoc iframe sandboxed *without* allow-same-origin — so the "
        "artifact's own scripts run in an opaque origin and can reach neither "
        "the page nor the Storage token a visitor may sign in with. "
        "Commenting requires signing in inside the page; the token lives in "
        "sessionStorage (shared with /admin) and is only ever sent to this "
        "hub's own API.\n\n"
        "**Guest mode.** Opened through an invitation link — the URL POST "
        "/api/artifacts/{id}/invitations returns, ending in "
        "'#invite={invitation_id}.{secret}' — the page reads that fragment, "
        "clears it from the address bar, checks it against GET /a/{id}/guest "
        "and shows 'Commenting as {name} (guest)'. The composer then works "
        "with no Keboola account at all, sending the credential in the "
        "X-Artifact-Guest header. Browsers never send a fragment to a server, "
        "and the page never puts it in a URL, so the secret stays out of the "
        "hub's logs.\n\n" + REVIEW_PASSWORD_NOTE
    ),
    responses={
        200: {
            "description": (
                "The review page. For a password-protected artifact this is "
                "the same credential-free shell, which renders locked and "
                "asks for the password in place -- this route has no 401."
            ),
            "content": CONTENT_HTML,
        },
        404: RESP_NOT_FOUND,
        429: RESP_UNLOCK_429,
        502: RESP_HUB_502,
    },
)
def read_review(
    request: Request,
    artifact_id: str = PathParam(..., description=SHARE_ID_DESC),
) -> Response:
    """Serve the review shell; all of its data is fetched client-side."""
    public_id = artifact_id
    meta = _public_meta_of(request, public_id)
    if meta is None:
        return _not_found(public_id)
    # A protected artifact is deliberately NOT answered with the standalone
    # unlock form here. This shell carries no artifact content and no
    # credential — everything it displays comes from /raw, /versions and
    # /comments, which stay gated — so serving it while locked reveals
    # nothing. Redirecting to the standalone form instead would navigate away
    # and drop the URL fragment, which is exactly where an invited guest's
    # credential lives; the page's own unlock panel keeps the guest in place.
    # The page's own fetches are all /a/{...} reads, so it gets the *public*
    # identifier — the one those URLs have to carry.
    return HTMLResponse(
        review_page(base_url(request), public_id, SERVICE_VERSION)
    )


# --------------------------------------------------------------------------
# Management API
# --------------------------------------------------------------------------


@app.post(
    "/api/artifacts",
    status_code=201,
    tags=["artifacts"],
    summary="Publish a new artifact",
    description="Publish HTML, Markdown, or a git repository as a new artifact. Exactly one of 'html', 'markdown', 'git_url' must be provided. Requires a Storage token (X-StorageApi-Token) and stack (X-Storage-Stack); the token establishes the owning project and is used once to store a canonical copy there. Set 'accept_versions' to let other projects submit moderated version proposals.",
    responses={
        201: {
            "description": (
                "Artifact published as version 1; returns its id, title, "
                "flags, head version and every public URL."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        413: {"description": "Built HTML exceeds the configured size limit."},
        422: {
            "description": (
                "Invalid body: not exactly one content field, git credentials "
                "without git_url, or a build failure (bad repo, no entry "
                "file, markdown render error)."
            )
        },
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, the canonical upload into the caller's project "
                "failed, or the hub's own Storage is unavailable."
            )
        },
    },
)
def publish_artifact(
    body: PublishBody,
    request: Request,
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Publish a new artifact from HTML, Markdown or a git repository."""
    owner, token = auth
    ensure_hydrated(request.app)

    _normalize_git_url(body)
    _check_git_credentials(body)
    _check_markdown_source(body)
    _require_exactly_one_content(body)

    built, source = _build(body)
    artifact_id = new_artifact_id()

    now = _now()
    identity = _identity(owner)
    meta = ArtifactMeta(
        id=artifact_id,
        owner=identity,
        password=hash_password(body.password) if body.password else None,
        accept_versions=bool(body.accept_versions),
        head_mode=HEAD_LATEST,
        head_version=None,
        created_at=now,
        updated_at=now,
    )
    store = request.app.state.store
    # Ordering, and what it buys:
    #
    #   1. the hub-side meta record — the thing store.delete() can roll back,
    #   2. the canonical copy in the *caller's* project, which only needs the
    #      artifact id, and
    #   3. the version envelope, which needs the canonical file id.
    #
    # Uploading the canonical copy first (as this used to) meant a later
    # failure left a stray artifact-* file in someone else's project with
    # nothing on the hub pointing at it. Now a failed canonical upload rolls
    # the meta record back, so nothing half-published survives the request.
    #
    # Residual risk, deliberately not solved here: if the rollback delete
    # itself fails (logged at ERROR below), or the process dies between steps
    # 1 and 3, a meta record with no version can be left behind. It is inert —
    # every read path answers 404 for it, because get_head finds nothing — but
    # it does need reaping by hand. Real transactionality needs a two-phase
    # marker in the store and is out of scope.
    store.save_meta(meta)
    try:
        canonical_file_id = _store_canonical(owner, token, artifact_id, built.html)
    except Exception:
        rolled_back = False
        try:
            rolled_back = store.delete(artifact_id)
        except Exception:  # noqa: BLE001 - the original failure must win
            rolled_back = False
        if not rolled_back:
            logger.error(
                "Rollback of artifact %s failed after its canonical upload "
                "failed; a meta record with no version may remain in Storage "
                "and needs to be reaped by hand",
                artifact_id,
            )
        raise
    envelope = Envelope(
        id=artifact_id,
        version=1,
        title=built.title,
        html=built.html,
        source_type=built.source_type,
        source=source,
        author=identity,
        status=STATUS_LIVE,
        canonical_file_id=canonical_file_id,
        created_at=now,
    )
    try:
        store.add_version(envelope)
    except Exception:
        # Step 3 failed after steps 1 and 2 succeeded: undo both, in the
        # order that matters -- the canonical copy first, while the caller's
        # token is still usable, then the meta record. Either undo failing is
        # logged and the original failure still propagates as the 502.
        _discard_canonical(owner, token, artifact_id, canonical_file_id)
        try:
            rolled_back = store.delete(artifact_id)
        except Exception:  # noqa: BLE001 - the original failure must win
            rolled_back = False
        if not rolled_back:
            logger.error(
                "Rollback of artifact %s failed after its version write "
                "failed; a meta record with no version may remain and will "
                "be reaped at the next hydrate once old enough",
                artifact_id,
            )
        raise
    logger.info(
        "Published artifact %s (owner project %s, source %s, %d bytes, "
        "protected=%s, accept_versions=%s, canonical file %s)",
        artifact_id,
        owner.project_id,
        built.source_type,
        len(built.html.encode("utf-8")),
        bool(meta.password),
        meta.accept_versions,
        canonical_file_id,
    )
    return _artifact_response(request, meta, envelope, 201)


@app.put(
    "/api/artifacts/{artifact_id}",
    tags=["artifacts"],
    summary="Update an artifact",
    description=(
        "Owner-only. A content field ('html', 'markdown', 'git_url') adds a "
        "new live version; everything else changes artifact-level settings: "
        "'password' / 'clear_password', who may contribute versions "
        "('accept_versions_mode' or the legacy 'accept_versions', plus "
        "'contributors'), who may comment ('comments_mode'), and the "
        "draft/final 'status'. A title is only valid together with new "
        "content, because a title lives on a version.\n\n"
        "Marking the artifact 'final' freezes it: new versions and new "
        "comments answer 409 for everyone, the owner included, and so does a "
        "content update. Setting 'status' back to 'draft' reopens it — "
        "including in the same call that carries the new content."
    ),
    responses={
        200: {
            "description": (
                "Artifact updated; returns its current state, including the "
                "version this call produced and the version now served."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: {
            "description": (
                "Token is valid but not from the project that owns this "
                "artifact."
            )
        },
        404: RESP_NOT_FOUND,
        409: RESP_FINAL_409,
        413: {"description": "Built HTML exceeds the configured size limit."},
        422: {
            "description": (
                "Invalid body: more than one content field, git credentials "
                "without git_url, a title without content, both "
                "'accept_versions' and 'accept_versions_mode', an unknown "
                "mode or status, a malformed contributor key, or a build "
                "failure."
            )
        },
        429: RESP_VERSIONS_429,
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, the canonical upload failed, or the hub's own "
                "Storage is unavailable.\n\n"
                "When a request carried both content and settings, the "
                "'detail' names exactly what is in force, because a partly "
                "applied update is never left less restrictive than it was: "
                "either nothing was applied, or the settings that *narrow* "
                "access were applied without the new version (and could not "
                "be rolled back), or the new version is live under the "
                "previous, narrower settings and the changes that *widen* "
                "access must be resent. Read the detail before retrying."
            )
        },
    },
)
@_serialized_per_artifact
def update_artifact(
    body: UpdateBody,
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Add a live version and/or change the artifact-level settings.

    **Persistence contract (SEC-100-001).** When one request does both, the
    settings are split by direction and committed around the content:
    tightening first, then the version, then loosening. Whatever fails, the
    publicly reachable state is never less restrictive than the last durable
    commit, and the 502 says which state that is. See the long comment on the
    ordering below for the reasoning and :func:`_is_tightening` for how each
    individual setting is classified.
    """
    owner, token = auth
    ensure_hydrated(request.app)
    store = request.app.state.store

    meta = store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    _owner_only(meta, owner)

    _normalize_git_url(body)
    _check_git_credentials(body)
    _check_markdown_source(body)
    present = _content_fields(body)
    if len(present) > 1:
        raise HTTPException(
            status_code=422,
            detail=(
                "Provide at most one of 'html', 'markdown' or 'git_url' "
                f"(got: {', '.join(present)})"
            ),
        )
    if body.title is not None and not present:
        raise HTTPException(
            status_code=422,
            detail=(
                "'title' belongs to a version, so it can only be changed "
                "together with new content ('html', 'markdown' or 'git_url')"
            ),
        )

    # A frozen artifact takes no new content. "final" has an escape hatch: the
    # owner may reopen it in this very call by sending status="draft" alongside
    # the new content. "trashed" has none — restoring is its own route, so that
    # bringing an artifact back is never a side effect of editing it.
    if present and meta.is_frozen():
        if not (meta.is_final() and body.status == ARTIFACT_DRAFT):
            return _document_frozen(meta, "new versions")

    was_final = meta.is_final()

    # An owner update that carries content is a version submission like any
    # other, so it draws on the same per-project daily budget contributors
    # have. Checked before anything is persisted, so a throttled call changes
    # nothing at all.
    if present and not _claim_submission_slot(request.app, artifact_id, owner.key):
        return _version_rate_limited()

    now = _now()
    # Build first. A 413/422 from _build used to arrive *after* the settings
    # change had already been saved, leaving an update the caller was told
    # failed but which had half happened. Nothing is persisted until the new
    # content actually exists.
    built = source = None
    if present:
        built, source = _build(body)

    # Fail-closed write ordering (SEC-100-001, the regressed REL-075-004).
    #
    # A single PUT can carry both new content and new settings, and the two
    # naive orderings each fail open in one direction:
    #
    #   settings first (v0.7.5)  a failed content write left the new password,
    #                            policy or status in force although the caller
    #                            was told the update failed;
    #   content first (v0.10.0)  a failed settings write left the new -- often
    #                            confidential -- version as the *public head*
    #                            under the old, looser policy. Strictly worse:
    #                            less restrictive than the previous state and
    #                            than the requested one.
    #
    # Neither is safe on its own, because one request can both narrow access (a
    # password, submissions closed, the document finalized) and widen it
    # (clear_password, submissions reopened, un-finalized). So the settings are
    # split by direction -- see :func:`_is_tightening` for the per-setting
    # rules -- and committed around the content:
    #
    #   (a) tightening settings, (b) the new version, (c) loosening settings.
    #
    # The contract this is here to keep, and which the tests in
    # tests/test_review100_atomic_update.py pin at every step:
    #
    #   **after any partial failure the publicly reachable state is never less
    #   restrictive than the last durable commit.**
    #
    # Concretely: if (a) fails, nothing changed. If (b) fails, (a) is rolled
    # back best-effort and the 502 names whichever of the two states survived
    # -- retained tightening is acceptable and is stated plainly, retained
    # loosening never happens because loosening has not been written yet. If
    # (c) fails, the new content is live under the tightened/previous settings
    # and the 502 says the widening half must be resent.
    #
    # Everything is applied to *copies* (dataclasses.replace): store.get_meta
    # answers from cache, so mutating ``meta`` in place would let a failed
    # save_meta leave every later read on this process showing settings
    # Storage never received.
    candidate = dataclasses.replace(meta)
    if body.clear_password:
        candidate.password = None
    elif body.password:
        candidate.password = hash_password(body.password)
    _apply_policy(candidate, body)
    candidate.updated_at = now

    finalized_emitted = False
    if built is None:
        # No content means nothing to order the settings around, so this stays
        # the single save it has always been.
        envelope = store.get_head(artifact_id)
        if envelope is None:
            return _not_found(artifact_id)
        store.save_meta(candidate)
        meta = candidate
    else:
        previous = meta
        tightened = _tightening_half(previous, candidate)
        tightened.updated_at = now
        has_tightening = _access_differs(previous, tightened)
        has_loosening = _access_differs(tightened, candidate)

        # (a) Narrow access first, so a failure anywhere later can only leave
        # the artifact more locked down than it was, never less.
        if has_tightening:
            store.save_meta(tightened)
        committed = tightened if has_tightening else previous

        # (b) The content. Both halves of it -- the canonical copy in the
        # caller's project and the version envelope on the hub -- are undone on
        # failure, and so is (a).
        try:
            canonical_file_id = _store_canonical(owner, token, artifact_id, built.html)
            envelope = Envelope(
                id=artifact_id,
                # Replaced by add_version_next, which allocates atomically.
                version=0,
                title=built.title,
                html=built.html,
                source_type=built.source_type,
                source=source or {},
                author=_identity(owner),
                status=STATUS_LIVE,
                canonical_file_id=canonical_file_id,
                created_at=now,
            )
            try:
                envelope.version = store.add_version_next(envelope)
            except Exception:
                # The copy in the caller's project has no version pointing at
                # it and no other way to ever be found; discard it now, while
                # the caller's token still works. The failure still propagates.
                _discard_canonical(owner, token, artifact_id, canonical_file_id)
                raise
        except Exception as exc:
            if not has_tightening:
                # Nothing was committed at all, so the pre-existing behaviour
                # -- the raw failure, as a 502 -- is already accurate.
                raise
            rolled_back = False
            try:
                store.save_meta(dataclasses.replace(previous))
                rolled_back = True
            except Exception:  # noqa: BLE001 - the original failure must win
                logger.error(
                    "Artifact %s: the new version failed *and* the tightened "
                    "settings from the same request could not be rolled back; "
                    "they remain in force over the previous content and need "
                    "checking by hand",
                    artifact_id,
                )
            raise _partial_update_502(
                exc,
                _UPDATE_ROLLED_BACK if rolled_back else _UPDATE_TIGHTENING_STUCK,
            ) from exc

        # Durable now, so the notification is truthful. v1 is the publish
        # itself, which the owner just did by hand and does not need a
        # notification about; every later live version is news. The receivers
        # are the ones ``committed`` holds -- a request that removed a webhook
        # tightened it away in (a), and it must not be notified.
        if envelope.version > 1:
            _emit_webhook(
                request,
                committed,
                "version.published",
                {"title": envelope.title, "version": envelope.version},
                actor=owner,
            )
        # Finalizing is a tightening, so it became durable in (a). Emitting it
        # here rather than at the end of the handler means a failing (c) does
        # not swallow an event describing a freeze that really did take.
        if committed.status == ARTIFACT_FINAL and not was_final:
            _emit_webhook(
                request,
                committed,
                "artifact.finalized",
                {"title": envelope.title, "version": envelope.version},
                actor=owner,
            )
            finalized_emitted = True

        # (c) Widen access last, once the content it applies to exists. Also
        # the place a content-only update records its updated_at, which is why
        # this runs when there is no settings change at all.
        if has_loosening or not has_tightening:
            try:
                store.save_meta(candidate)
            except Exception as exc:
                logger.error(
                    "Artifact %s: version %d is published and live, but the "
                    "settings that widen access could not be saved; the "
                    "previous, more restrictive settings stay in force",
                    artifact_id,
                    envelope.version,
                )
                raise _partial_update_502(
                    exc,
                    _UPDATE_LOOSENING_PENDING
                    if has_loosening
                    else _UPDATE_TIMESTAMP_PENDING,
                ) from exc
            meta = candidate
        else:
            meta = tightened

    if meta.status == ARTIFACT_FINAL and not was_final and not finalized_emitted:
        _emit_webhook(
            request,
            meta,
            "artifact.finalized",
            {"title": envelope.title, "version": envelope.version},
            actor=owner,
        )

    logger.info(
        "Updated artifact %s (owner project %s, version %d, source %s, "
        "%d bytes, protected=%s, versions=%s, comments=%s, status=%s)",
        artifact_id,
        owner.project_id,
        envelope.version,
        envelope.source_type,
        len(envelope.html.encode("utf-8")),
        bool(meta.password),
        meta.accept_versions_mode,
        meta.comments_mode,
        meta.status,
    )
    return _artifact_response(request, meta, envelope, 200)


@app.get(
    "/api/artifacts",
    tags=["artifacts"],
    summary="List the caller's artifacts",
    description=(
        "Lists the artifacts owned by the caller's own project — ownership is "
        "the pair (stack, project id) derived from the Storage token, so the "
        "listing depends only on the credentials, never on a query. Each row "
        "carries the internal 'id' and the public 'share_id', the title, "
        "timestamps, 'protected' and 'accept_versions' flags, head version, "
        "total version count, pending 'proposed_count', the document status "
        "(as 'document_status', and unchanged as the original 'status') with "
        "'trashed_at' and the derived 'contributions_frozen' flag, a "
        "'webhooks_count' (never the URLs themselves), and every public URL "
        "built from the share id. Newest 'updated_at' first.\n\n"
        "Every row's 'status'/'document_status' here is the *document's* "
        "('draft', 'final' or 'trashed'), not a version's ('live' or "
        "'proposed').\n\n"
        "Trashed artifacts are listed too, with status 'trashed' and a "
        "'trashed_at' timestamp: this is the owner's own view, so it is also "
        "their trash can. Their public URLs answer 404 until they are "
        "restored. This is the endpoint the /admin studio calls to sign a "
        "visitor in."
    ),
    responses={
        200: {
            "description": (
                "JSON with 'project_id' and the 'artifacts' array (possibly "
                "empty)."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable."
            )
        },
    },
)
def list_artifacts(
    request: Request, auth: tuple[Owner, str] = Depends(require_owner)
) -> dict:
    """List the artifacts owned by the caller's project, trash included."""
    owner, _token = auth
    ensure_hydrated(request.app)
    store = request.app.state.store
    base = base_url(request)
    rows = store.list_owner(owner.key)
    artifacts = []
    for row in rows:
        meta = store.get_meta(row["id"])
        artifacts.append(
            {
                **row,
                # The row's "status" from the store is already the *document's*
                # status; mirror it under the name every other endpoint uses so
                # a consumer can read one key everywhere. "status" is kept.
                "document_status": row.get("status"),
                "contributions_frozen": row.get("status")
                in (ARTIFACT_FINAL, ARTIFACT_TRASHED),
                # A count, never the URLs: a webhook URL is a capability (a
                # Slack hook's path *is* its credential), so the only response
                # that echoes them is the owner PUT that set them.
                "webhooks_count": len(meta.webhooks) if meta is not None else 0,
                # Built from share_id, so a rotated link is reflected here.
                **artifact_urls(base, row["share_id"]),
            }
        )
    return {"project_id": owner.project_id, "artifacts": artifacts}


@app.delete(
    "/api/artifacts/{artifact_id}",
    tags=["artifacts"],
    summary="Move an artifact to the trash",
    description=(
        "Owner-only **soft** delete, reversible by design. Nothing is removed "
        "from Storage: the artifact's status becomes 'trashed', its public "
        "link stops resolving (every /a/{share_id} route answers 404, exactly "
        "as if it had never existed) and it is frozen — new versions and new "
        "comments answer 409.\n\n"
        "The artifact keeps appearing in GET /api/artifacts with status "
        "'trashed' and a 'trashed_at' timestamp, so its owner still sees it. "
        "POST /api/artifacts/{id}/restore brings it back to whatever status "
        "it had before, on the same share id and the same URL.\n\n"
        "To erase it for good — versions, comment threads, view statistics and "
        "counters — use DELETE /api/artifacts/{id}/purge, which is the "
        "irreversible one. The canonical copies in the authors' own Keboola "
        "projects are never touched by either route."
    ),
    responses={
        200: {
            "description": (
                "Moved to the trash (or already there — trashing twice is a "
                "successful no-op); JSON with 'trashed': true, the artifact "
                "id, the timestamp and a 'restore_hint' naming the way back."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: RESP_DESTRUCTIVE_403,
        404: RESP_NOT_FOUND,
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable so the "
                "meta record could not be rewritten."
            )
        },
    },
)
@_serialized_per_artifact
def delete_artifact(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Soft-delete: freeze the artifact and kill its public link."""
    owner, _token = auth
    ensure_hydrated(request.app)
    store = request.app.state.store

    meta = store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    _owner_only(meta, owner)
    _destructive_authority(owner)

    now = _now()
    if not store.trash(artifact_id, now):
        # trash() only answers False for an artifact it cannot find, and we
        # just loaded its meta — so this is a race with a concurrent purge.
        return _not_found(artifact_id)

    _emit_webhook(request, meta, "artifact.trashed", {"trashed_at": now}, actor=owner)
    logger.info(
        "Artifact %s moved to the trash (owner project %s)",
        artifact_id,
        owner.project_id,
    )
    return JSONResponse(
        {
            "trashed": True,
            "id": artifact_id,
            "trashed_at": meta.trashed_at or now,
            "restore_hint": (
                f"POST /api/artifacts/{artifact_id}/restore brings it back on "
                "the same URL; DELETE /api/artifacts/"
                f"{artifact_id}/purge erases it for good."
            ),
        }
    )


@app.post(
    "/api/artifacts/{artifact_id}/restore",
    tags=["artifacts"],
    summary="Restore an artifact from the trash",
    description=(
        "Owner-only. Undoes DELETE /api/artifacts/{id}: the artifact returns "
        "to the status it was trashed from ('draft' or 'final'), its public "
        "link resolves again on the *same* share id, and versions and comments "
        "unfreeze. Restoring an artifact that is not in the trash is a 409 — "
        "there is nothing to undo."
    ),
    responses={
        200: {
            "description": (
                "Restored; JSON with 'restored': true, the status it came back "
                "to, and its public URLs."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: RESP_OWNER_403,
        404: RESP_NOT_FOUND,
        409: {"description": "This artifact is not in the trash."},
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable."
            )
        },
    },
)
@_serialized_per_artifact
def restore_artifact(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Bring a trashed artifact back to the status it was trashed from."""
    owner, _token = auth
    ensure_hydrated(request.app)
    store = request.app.state.store

    meta = store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    _owner_only(meta, owner)

    if not meta.is_trashed():
        return JSONResponse(
            status_code=409,
            content={
                "error": "artifact is not in the trash",
                "detail": (
                    f"Its status is {meta.status!r}, so there is nothing to "
                    "restore."
                ),
                "id": artifact_id,
            },
        )

    if not store.restore(artifact_id, _now()):
        return _not_found(artifact_id)

    restored = store.get_meta(artifact_id) or meta
    _emit_webhook(
        request, restored, "artifact.restored", {"status": restored.status}, actor=owner
    )
    logger.info(
        "Artifact %s restored from the trash to %s (owner project %s)",
        artifact_id,
        restored.status,
        owner.project_id,
    )
    return JSONResponse(
        {
            "restored": True,
            "id": artifact_id,
            "share_id": restored.share_id,
            "artifact_status": restored.status,
            **artifact_urls(base_url(request), restored.share_id),
        }
    )


@app.delete(
    "/api/artifacts/{artifact_id}/purge",
    tags=["artifacts"],
    summary="Permanently erase an artifact",
    description=(
        "Owner-only, and **irreversible** — there is no undo and no trash to "
        "fall back on. Deletes every comment thread, then every version file "
        "and the meta record from the hub's project, and forgets the "
        "artifact's view statistics and rate-limit counters. All of its URLs "
        "stop resolving for good.\n\n"
        "The call is idempotent and resumable: each step is safe to repeat, "
        "and the meta record that authorizes the operation is erased strictly "
        "last — after the comment threads and after every version file — so a "
        "call that fails partway leaves the artifact still owned by you and "
        "can simply be retried with the same credentials, including after a "
        "restart.\n\n"
        "An artifact does not have to be in the trash first, but the gentler "
        "path is DELETE /api/artifacts/{id} (soft, reversible) followed by "
        "this once you are sure. The canonical copies in the authors' own "
        "Keboola projects are left untouched — this only erases the hub's."
    ),
    responses={
        200: {
            "description": (
                "Purged; JSON reporting how many comment threads went with it "
                "and confirming the canonical copies were kept."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: RESP_DESTRUCTIVE_403,
        404: RESP_NOT_FOUND,
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, the hub's own Storage is unavailable, or the "
                "erasure only partially succeeded — some stored files could "
                "not be removed, so content may still be readable and the "
                "call must be retried. Comment threads are erased first, then "
                "the version files, then the meta record, so a partial "
                "erasure at any point ('comment_threads_failed' says how many "
                "threads still have files in Storage) leaves the artifact and "
                "its ownership record deliberately in place: retrying the "
                "purge with the same credentials resumes it and finishes the "
                "job."
            )
        },
    },
)
@_serialized_per_artifact
def purge_artifact(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Erase every version; the authors' canonical copies are left untouched."""
    owner, _token = auth
    ensure_hydrated(request.app)
    store = request.app.state.store

    meta = store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    _owner_only(meta, owner)
    _destructive_authority(owner)

    # Children first, the authorizing record last. The order is the whole
    # design of this route: the meta record is what _owner_only reads, so
    # erasing it before the comments meant a partial comment failure answered
    # 502 "retry the purge" and the retry 404ed -- leaving comment files in
    # Storage that nothing could ever reach or erase again. Every step below
    # is idempotent, so a retry resumes rather than restarts: deleting
    # already-deleted comments reports zero, and store.delete keeps the index
    # whenever a file survives.
    #
    # Not a full tombstone: nothing here fences a concurrent write against an
    # artifact being purged. That needs shared, revisioned state (see the
    # v0.7.5 review) rather than ordering, so the race remains open.
    threads, failed_threads = request.app.state.comments.delete_all_for(artifact_id)
    if failed_threads:
        # Some comment files are still in Storage, so the erasure the owner
        # asked for did not fully happen. Reporting success would tell them a
        # discussion is gone while it is still readable by anything that can
        # reach those files. The artifact is deliberately left intact so the
        # retry this asks for can authenticate and finish.
        logger.error(
            "Partial purge of artifact %s: %d comment thread(s) still have "
            "Storage files; artifact kept so the purge can be retried",
            artifact_id,
            failed_threads,
        )
        return JSONResponse(
            status_code=502,
            content={
                "error": "comment threads not fully deleted",
                "detail": (
                    f"{failed_threads} of the artifact's comment threads "
                    "could not be erased: some stored comment files remain. "
                    "The artifact itself was left in place so this call can "
                    "be retried; retry the purge to finish erasing it."
                ),
                "id": artifact_id,
                "comment_threads_deleted": threads,
                "comment_threads_failed": failed_threads,
            },
        )

    if not store.delete(artifact_id):
        # store.delete only reports success when *every* authoritative file is
        # confirmed gone; on a partial failure it keeps the index so the
        # artifact stays readable. Reporting "deleted": true here would tell an
        # owner their content is unreachable while it is still being served.
        #
        # REL-100-001: whatever survived, the artifact's meta record survived
        # with it (store.delete deletes it strictly last), so this retry can
        # authenticate and finish -- including after a restart, since the meta
        # is what hydrate rebuilds the artifact from.
        logger.error(
            "Partial purge of artifact %s: some Storage files remain",
            artifact_id,
        )
        return JSONResponse(
            status_code=502,
            content={
                "error": "artifact not fully deleted",
                "detail": (
                    "Some stored files could not be removed, so the erasure "
                    "did not fully happen and content may still be readable. "
                    "The artifact's ownership record was deliberately kept so "
                    "this call can be retried; retry the purge to finish it."
                ),
                "id": artifact_id,
                "comment_threads_deleted": threads,
            },
        )
    # Same reasoning for the state sidecar: view rows and rate-limit counters
    # keyed by this artifact have nothing left to describe. Best effort — a
    # sidecar failure must not turn a completed purge into an error.
    database = _statedb(request.app)
    if database is not None:
        try:
            database.forget_artifact(artifact_id)
        except Exception as exc:  # noqa: BLE001 - the purge itself succeeded
            logger.warning(
                "Could not forget the state rows of artifact %s: %s",
                artifact_id,
                exc,
            )
    logger.info(
        "Purged artifact %s and %d comment thread(s) (owner project %s)",
        artifact_id,
        threads,
        owner.project_id,
    )
    return JSONResponse(
        {
            "deleted": True,
            "purged": True,
            "comment_threads_deleted": threads,
            "note": "canonical copies in the authors' projects were not touched",
        }
    )


def _no_store(response: Response) -> Response:
    """Force ``Cache-Control: no-store`` / ``Pragma: no-cache`` (SEC-100-006).

    Assignment, not ``setdefault``: the ``artifact_headers`` middleware only
    ever sets a *default* Cache-Control, and only on ``/a/*`` paths, so there
    is nothing here for this to lose a conflict with -- but a secret-bearing
    response must never depend on that staying true. Every response that can
    carry a webhook receiver's signing key (this listing, and the rotate-key
    response below) goes through this before it leaves the handler, so a
    shared or browser cache can never be the reason a rotated-away key stays
    reachable.
    """
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def _webhook_receiver_view(
    dispatcher: WebhookDispatcher, artifact_id: str, url: str, record: dict | None
) -> dict:
    """One receiver's entry for the webhook-keys listing and rotate response.

    ``record`` is the receiver's persisted epoch (``None`` for a receiver
    that has never been rotated); the dispatcher is seeded with it by the
    caller before this runs, so ``signing_key_for`` already reflects it.
    ``rotate_key_url`` is included so a caller does not have to reimplement
    :func:`src.webhooks.receiver_id_for` client-side just to rotate.
    """
    view = {
        "url": url,
        "id": receiver_id_for(url),
        "signing_key": dispatcher.signing_key_for(artifact_id, url),
        "rotated_at": record.get("rotated_at") if record else None,
        "rotate_key_url": (
            f"/api/artifacts/{artifact_id}/webhooks/{receiver_id_for(url)}/rotate-key"
        ),
    }
    return view


@app.get(
    "/api/artifacts/{artifact_id}/webhooks",
    tags=["artifacts"],
    summary="Read each webhook receiver's signing key",
    description=(
        "Owner-only. Lists the artifact's registered webhook URLs together "
        "with the key each one's deliveries are signed with, so you can "
        "configure the receiver to verify them.\n\n"
        "**Every receiver has its own key**, derived from the hub's webhook "
        "key, the artifact and the receiver URL. A receiver necessarily "
        "learns the key it verifies with, so a single shared key would let "
        "any receiver forge a delivery for any other receiver and any "
        "artifact; a per-receiver key is worth exactly its own feed and "
        "reveals nothing about the hub's key or another receiver's.\n\n"
        "The keys are on their own endpoint rather than in the general owner "
        "view because they are credentials: fetch one when you configure a "
        "receiver, store it where that receiver keeps its secrets, and do "
        "not log it. Registering the same URL again yields the same key "
        "**until it has been rotated** (SEC-100-006) — see `POST "
        ".../webhooks/{receiver_id}/rotate-key`, listed here as "
        "`rotate_key_url`, to mint an independent one without touching the "
        "URL. This response, like the rotate response, always carries "
        "`Cache-Control: no-store` and `Pragma: no-cache` — a receiver's key "
        "is a credential and must never be served from a shared or browser "
        "cache."
    ),
    responses={
        200: {
            "description": (
                "The registered receivers, each with its own signing key and "
                "a `rotate_key_url`; `rotated_at` is null for a receiver "
                "that has never been rotated."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: RESP_OWNER_403,
        404: RESP_NOT_FOUND,
    },
)
def read_webhook_keys(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Report the per-receiver signing keys to the artifact's owner."""
    owner, _token = auth
    ensure_hydrated(request.app)
    meta = request.app.state.store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    _owner_only(meta, owner)

    dispatcher = request.app.state.webhooks
    dispatcher.configure_key_overlap_s(settings.webhook_key_overlap_s)
    epochs = meta.webhook_key_epochs or {}
    urls = list(meta.webhooks or [])
    for url in urls:
        # Re-seed the live epoch cache from the durable record on every read,
        # not only on rotate: this is the point in the request lifecycle
        # where a fresh process picks the persisted epoch back up (see the
        # comment on WebhookDispatcher._epochs), and it costs nothing when
        # there is nothing to seed.
        dispatcher.seed_epoch(meta.id, url, epochs.get(url))
    return _no_store(
        JSONResponse(
            {
                "id": meta.id,
                "webhooks": [
                    _webhook_receiver_view(dispatcher, meta.id, url, epochs.get(url))
                    for url in urls
                ],
            }
        )
    )


@app.post(
    "/api/artifacts/{artifact_id}/webhooks/{receiver_id}/rotate-key",
    tags=["artifacts"],
    summary="Rotate one webhook receiver's signing key",
    description=(
        "Owner-only (SEC-100-006). Mints a fresh, independent signing key "
        "for one receiver without touching its URL, and returns the new "
        "key. `receiver_id` is the value each entry of `GET "
        ".../webhooks` reports as `id` (or the `rotate_key_url` there, used "
        "as-is).\n\n"
        "**A short grace period, not an instant cutover.** For "
        "`webhook_key_overlap_s` seconds after rotation (config "
        "`HUB_WEBHOOK_KEY_OVERLAP_S`, default 600), every delivery to this "
        "receiver carries *both* signatures: the new key in the usual "
        "`X-Hub-Signature-256`, and the previous key in "
        "`X-Hub-Signature-256-Previous`. A receiver that only ever checks "
        "the first header needs no code change and is protected the moment "
        "the response is returned; a receiver that wants zero-downtime "
        "rotation can accept either header until it has picked up the new "
        "key from this response. Past the overlap window only "
        "`X-Hub-Signature-256` is sent and the previous key no longer "
        "verifies anything, full stop.\n\n"
        "Rotating never changes the receiver's URL and never requires "
        "re-registering it — `webhooks` on `PUT /api/artifacts/{id}` is "
        "untouched by this call."
    ),
    responses={
        200: {
            "description": (
                "Rotated; JSON with the new `signing_key`, `rotated_at`, and "
                "`overlap_seconds` the previous key stays valid for. Carries "
                "`Cache-Control: no-store` and `Pragma: no-cache`."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: RESP_DESTRUCTIVE_403,
        404: {
            "description": (
                "No artifact with this id, or it has no registered receiver "
                "with this receiver_id."
            )
        },
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable so the "
                "meta record could not be rewritten."
            )
        },
    },
)
@_serialized_per_artifact
def rotate_webhook_key(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    receiver_id: str = PathParam(
        ...,
        description=(
            "A receiver's `id` from `GET /api/artifacts/{id}/webhooks` "
            "(a hash of its URL, not the URL itself)."
        ),
    ),
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Mint a fresh key epoch for one receiver, keeping its URL unchanged."""
    owner, _token = auth
    ensure_hydrated(request.app)
    store = request.app.state.store

    meta = store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    _owner_only(meta, owner)
    _destructive_authority(owner)

    url = next(
        (u for u in meta.webhooks or [] if receiver_id_for(u) == receiver_id), None
    )
    if url is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "webhook receiver not found",
                "id": artifact_id,
                "receiver_id": receiver_id,
            },
        )

    existing = (meta.webhook_key_epochs or {}).get(url)
    previous_epoch = existing.get("epoch") if existing else None
    new_record = mint_key_epoch(previous_epoch, now=_now())

    # Copy-then-save, not an in-place mutation: store.get_meta answers from an
    # in-process cache, so every other concurrent reader in this process sees
    # whatever ``meta`` holds right now. Building a new dict and handing
    # save_meta a dataclasses.replace()'d copy (same pattern as
    # revoke_invitation above) means a failed save leaves the cached meta --
    # and therefore every concurrent read -- still describing the key that is
    # actually still correct, instead of a rotation the write never durably
    # committed.
    updated_epochs = dict(meta.webhook_key_epochs or {})
    updated_epochs[url] = new_record
    candidate = dataclasses.replace(
        meta, webhook_key_epochs=updated_epochs, updated_at=_now()
    )
    store.save_meta(candidate)

    dispatcher = request.app.state.webhooks
    dispatcher.configure_key_overlap_s(settings.webhook_key_overlap_s)
    dispatcher.seed_epoch(candidate.id, url, new_record)

    logger.info(
        "Rotated the webhook signing key for artifact %s receiver %s "
        "(owner project %s)",
        artifact_id,
        receiver_id,
        owner.project_id,
    )
    return _no_store(
        JSONResponse(
            {
                "id": artifact_id,
                "receiver_id": receiver_id,
                "signing_key": dispatcher.signing_key_for(candidate.id, url),
                "rotated_at": new_record["rotated_at"],
                "overlap_seconds": settings.webhook_key_overlap_s,
                "note": (
                    "The previous key still verifies deliveries to this "
                    "receiver (X-Hub-Signature-256-Previous) for "
                    "overlap_seconds after rotated_at, then stops working."
                ),
            }
        )
    )


@app.post(
    "/api/artifacts/{artifact_id}/rotate-link",
    tags=["artifacts"],
    summary="Rotate the public link",
    description=(
        "Owner-only. Mints a fresh share id for the artifact and returns its "
        "new URLs.\n\n"
        "**The old link stops working immediately.** Anyone holding the "
        "previous /a/{share_id} URL — and anyone who noted the bare artifact "
        "id, which stops resolving publicly the moment it differs from the "
        "share id — gets a 404 from the next request on. That revocation is "
        "the entire point: a capability URL sent to the wrong person can be "
        "taken back. There is no grace period and no way to un-rotate, so "
        "reshare the new URL with everyone who should still have it.\n\n"
        "Unlock cookies handed out under the old link go with it (they are "
        "scoped to the old path), so readers of a password-protected artifact "
        "unlock once more. The internal artifact id, the content, the version "
        "history and the comment threads are all unchanged — this rotates the "
        "address, not the artifact."
    ),
    responses={
        200: {
            "description": (
                "Rotated; JSON with the new 'share_id', every public URL "
                "rebuilt from it, the previous share id and a warning that it "
                "no longer resolves."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: RESP_DESTRUCTIVE_403,
        404: RESP_NOT_FOUND,
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable so the "
                "meta record could not be rewritten."
            )
        },
    },
)
@_serialized_per_artifact
def rotate_link(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Mint a new public share id and revoke the previous link."""
    owner, _token = auth
    ensure_hydrated(request.app)
    store = request.app.state.store

    meta = store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    _owner_only(meta, owner)
    _destructive_authority(owner)

    previous = meta.share_id
    new_share = store.rotate_share(artifact_id, when_iso=_now())
    if new_share is None:
        return _not_found(artifact_id)

    # rotate_share persisted the new meta, so this reload sees the new share
    # id; the fallback only guards an impossible read failure right after it.
    rotated = store.get_meta(artifact_id) or meta
    _emit_webhook(
        request, rotated, "link.rotated", {"share_id": new_share}, actor=owner
    )
    logger.info(
        "Rotated the public link of artifact %s (owner project %s)",
        artifact_id,
        owner.project_id,
    )
    return JSONResponse(
        {
            "id": artifact_id,
            "share_id": new_share,
            "previous_share_id": previous,
            "warning": (
                "The previous link stopped working immediately: "
                f"/a/{previous} now answers 404, and so does the bare "
                "artifact id. Reshare the new URL with everyone who should "
                "still have access."
            ),
            **artifact_urls(base_url(request), new_share),
        }
    )


@app.get(
    "/api/artifacts/{artifact_id}/stats",
    tags=["artifacts"],
    summary="View statistics for one artifact",
    description=(
        "Owner-only. Reports how often the artifact was read: 'total' across "
        "all of recorded history, 'by_kind' (which surface — 'page' for the "
        "rendered wrapper, 'raw', 'source', 'version') and 'by_day' for the "
        "most recent 30 UTC days, oldest first so it charts as-is.\n\n"
        "Counts come from the hub's operational-state sidecar, which "
        "snapshots itself into Storage periodically: a crash can lose the last "
        "few minutes, and a purge forgets an artifact's numbers entirely. "
        "These are traffic figures, not an audit log — no reader identity, no "
        "address and no referrer is recorded anywhere."
    ),
    responses={
        200: {
            "description": (
                "JSON with 'id', 'share_id', 'total', 'by_day' and 'by_kind'. "
                "An artifact nobody has read yet reports zeros, not a 404."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: RESP_OWNER_403,
        404: RESP_NOT_FOUND,
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable."
            )
        },
    },
)
def artifact_stats(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Per-artifact view counts, for the owning project only."""
    owner, _token = auth
    ensure_hydrated(request.app)

    meta = request.app.state.store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    _owner_only(meta, owner)

    database = _statedb(request.app)
    empty = {"total": 0, "by_day": [], "by_kind": {}}
    if database is None:
        views = empty
    else:
        try:
            views = database.views(artifact_id)
        except Exception as exc:  # noqa: BLE001 - stats never 500 the owner
            logger.warning(
                "Could not read view statistics of artifact %s: %s",
                artifact_id,
                exc,
            )
            views = empty
    return JSONResponse({**views, "id": artifact_id, "share_id": meta.share_id})


# --------------------------------------------------------------------------
# Guest invitations
#
# An invitation is a named, revocable capability that lets one human without a
# Keboola account comment on one artifact. It is a pair: a public
# ``invitation_id`` stored in the artifact's meta record, and a secret that is
# shown **once**, hashed exactly like a reader password and never stored in the
# clear.
#
# The secret travels in the *fragment* of the review URL
# (``/a/{share}/review#invite={id}.{secret}``), which browsers never send to
# the server, and from there only ever into the ``X-Artifact-Guest`` request
# header. It therefore stays out of access logs, out of ``Referer`` and out of
# anything a proxy records — the same discipline the reader password gets.
# --------------------------------------------------------------------------


@app.post(
    "/api/artifacts/{artifact_id}/invitations",
    status_code=201,
    tags=["artifacts"],
    summary="Invite a guest to comment",
    description=(
        "Owner-only. Mints a named, revocable invitation that lets one person "
        "*without* a Keboola account comment on this artifact, and returns "
        "the review URL that carries it.\n\n"
        "**The secret is shown exactly once.** It is hashed before it is "
        "stored (PBKDF2, like a reader password), so neither this hub nor its "
        "owner can ever show it again — a lost link is replaced by revoking "
        "the invitation and minting another one. It rides the URL *fragment* "
        "(after the #), which browsers never send to a server, and reaches "
        "this API only in the X-Artifact-Guest header.\n\n"
        "What the invitation grants is narrow and only grows narrower: open a "
        "comment thread, reply, and resolve or delete threads the guest "
        "themselves opened. It never grants a version submission, never any "
        "/api/* management call, and never access to another artifact. "
        "'comments_mode' does not gate a guest — the invitation *is* the "
        "grant — but a final or trashed artifact is frozen for them exactly "
        "as it is for everybody else."
    ),
    responses={
        201: {
            "description": (
                "Invitation created; JSON with 'invitation_id', 'name' and "
                "the one-time 'review_url'."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: RESP_OWNER_403,
        404: RESP_NOT_FOUND,
        409: RESP_FINAL_409,
        422: {
            "description": (
                "The name is empty or longer than "
                f"{MAX_INVITATION_NAME_CHARS} characters, or the artifact "
                "already holds HUB_MAX_INVITATIONS_PER_ARTIFACT live "
                "invitations."
            )
        },
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable so the "
                "meta record could not be rewritten."
            )
        },
    },
)
@_serialized_per_artifact
def create_invitation(
    body: InvitationBody,
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Mint a one-time guest invitation and return its review URL."""
    owner, _token = auth
    ensure_hydrated(request.app)
    store = request.app.state.store

    meta = store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    _owner_only(meta, owner)
    if meta.is_frozen():
        return _document_frozen(meta, "new invitations")

    name = body.name.strip()
    if not name:
        raise HTTPException(
            status_code=422, detail="an invitation needs a name for the guest"
        )
    _make_room_for_invitation(meta)

    # Same generator as an artifact id: 18 random bytes, urlsafe — unguessable,
    # and free of the "." that separates the two halves of the credential.
    secret = new_artifact_id()
    invitation_id = new_artifact_id()
    meta.invitations = list(meta.invitations) + [
        {
            "id": invitation_id,
            "name": name,
            "secret": hash_password(secret),
            "created_at": _now(),
            "revoked": False,
        }
    ]
    meta.updated_at = _now()
    store.save_meta(meta)

    base = base_url(request).rstrip("/")
    logger.info(
        "Artifact %s invited a guest (%s, owner project %s)",
        artifact_id,
        invitation_id,
        owner.project_id,
    )
    return JSONResponse(
        status_code=201,
        content={
            "invitation_id": invitation_id,
            "name": name,
            "review_url": (
                f"{base}/a/{meta.share_id}/review#invite={invitation_id}.{secret}"
            ),
            "warning": (
                "This link is shown once and cannot be recovered — the secret "
                "is stored hashed. Send it to the person it names; anyone "
                "holding it can comment as them until you revoke it."
            ),
        },
    )


@app.get(
    "/api/artifacts/{artifact_id}/invitations",
    tags=["artifacts"],
    summary="List guest invitations",
    description=(
        "Owner-only. Lists this artifact's guest invitations: id, the name "
        "the owner gave, when it was minted and whether it has been revoked.\n\n"
        "The secrets are **not** here and cannot be recovered from anywhere — "
        "they are stored hashed and were shown once, when each invitation was "
        "created. To give somebody a working link again, revoke theirs and "
        "mint a new one."
    ),
    responses={
        200: {
            "description": (
                "JSON with 'id' and the 'invitations' array (possibly empty); "
                "no secrets."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: RESP_OWNER_403,
        404: RESP_NOT_FOUND,
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable."
            )
        },
    },
)
def list_invitations(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Every invitation of one artifact, secrets omitted."""
    owner, _token = auth
    ensure_hydrated(request.app)

    meta = request.app.state.store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    _owner_only(meta, owner)

    return JSONResponse(
        {
            "id": artifact_id,
            "invitations": [
                _public_invitation(invitation) for invitation in meta.invitations
            ],
        }
    )


@app.delete(
    "/api/artifacts/{artifact_id}/invitations/{invitation_id}",
    tags=["artifacts"],
    summary="Revoke a guest invitation",
    description=(
        "Owner-only, and per person: the named invitation stops working "
        "immediately while every other guest's link keeps working. Comments "
        "the guest already wrote stay — revoking withdraws the capability, "
        "not the contribution.\n\n"
        "Revoking is idempotent: revoking an already-revoked invitation is a "
        "successful no-op."
    ),
    responses={
        200: {"description": "Revoked; JSON with 'revoked': true."},
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: RESP_OWNER_403,
        404: {
            "description": (
                "No artifact with this id, or it has no such invitation."
            )
        },
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable so the "
                "meta record could not be rewritten."
            )
        },
    },
)
@_serialized_per_artifact
def revoke_invitation(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    invitation_id: str = PathParam(..., description=INVITATION_ID_DESC),
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Turn off one person's invitation without touching anybody else's."""
    owner, _token = auth
    ensure_hydrated(request.app)
    store = request.app.state.store

    meta = store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    _owner_only(meta, owner)

    target = None
    for invitation in meta.invitations:
        if invitation.get("id") == invitation_id:
            target = invitation
            break
    if target is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "invitation not found",
                "id": artifact_id,
                "invitation_id": invitation_id,
            },
        )

    if not target.get("revoked"):
        # A copy, not an in-place flag. store.get_meta answers from cache, so
        # ``meta`` is the object every later read in this process gets:
        # marking the invitation revoked before the save succeeds means a
        # failed revocation still reads as revoked here, while Storage --
        # and therefore any process that later rehydrates from it -- still
        # honours the guest. The owner is told 502 and retries, but a restart
        # rehydrates from Storage and quietly brings the guest back. save_meta
        # uploads before it caches, so handing it a copy leaves the original
        # untouched on failure.
        revoked = [
            {**invitation, "revoked": True}
            if invitation.get("id") == invitation_id
            else invitation
            for invitation in meta.invitations
        ]
        candidate = dataclasses.replace(
            meta, invitations=revoked, updated_at=_now()
        )
        store.save_meta(candidate)
        logger.info(
            "Artifact %s revoked guest invitation %s (owner project %s)",
            artifact_id,
            invitation_id,
            owner.project_id,
        )
    return JSONResponse(
        {
            "revoked": True,
            "id": artifact_id,
            "invitation_id": invitation_id,
            "name": str(target.get("name") or ""),
        }
    )


# --------------------------------------------------------------------------
# Community versioning
# --------------------------------------------------------------------------


@app.post(
    "/api/artifacts/{artifact_id}/versions",
    status_code=201,
    tags=["versions"],
    summary="Submit a new version",
    description=(
        "Adds a version to an existing artifact. The owning project's "
        "submissions go live immediately; another project may submit only "
        "when the owner opened the artifact (accept_versions_mode 'anyone', "
        "or 'allowlist' with that project on the contributor list), and its "
        "submission lands as a moderated proposal that stays private until "
        "the owner promotes it. A 'final' artifact accepts nothing from "
        "anybody (409). The canonical copy is stored in the *caller's* own "
        "project with the caller's token."
    ),
    responses={
        201: {
            "description": (
                "Version stored; returns its number, its status ('live' for "
                "the owner, 'proposed' otherwise), the note and its URL."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: {
            "description": (
                "This artifact does not accept versions from the caller's "
                "project (accept_versions is off, or the project is not on "
                "the contributor allowlist)."
            )
        },
        404: RESP_NOT_FOUND,
        409: RESP_FINAL_409,
        413: {"description": "Built HTML exceeds the configured size limit."},
        422: {
            "description": (
                "Invalid body: not exactly one content field, git credentials "
                "without git_url, an over-long note, or a build failure."
            )
        },
        429: RESP_VERSIONS_429,
        502: {
            "description": (
                "The caller's Keboola stack could not be reached, the "
                "canonical upload failed, or the hub's own Storage is "
                "unavailable."
            )
        },
    },
)
@_serialized_per_artifact
def submit_version(
    body: VersionBody,
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Submit one version: live for the owner, a moderated proposal otherwise."""
    caller, token = auth
    ensure_hydrated(request.app)
    store = request.app.state.store

    meta = store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)

    # A frozen artifact ("final", or in the trash) takes nothing from anybody,
    # so it gets its own, clearer answer instead of the generic "you may not
    # contribute" 403 that ``allows_versions_from`` would otherwise produce.
    if meta.is_frozen():
        return _document_frozen(meta, "new versions")

    is_owner = meta.owner_key == caller.key
    if not meta.allows_versions_from(caller.key):
        raise HTTPException(
            status_code=403,
            detail=(
                "this artifact does not accept versions from your project; "
                "its owner can open it with accept_versions (or "
                "accept_versions_mode 'anyone'), or add your project to the "
                "contributor allowlist"
            ),
        )

    _normalize_git_url(body)
    _check_git_credentials(body)
    _check_markdown_source(body)
    _require_exactly_one_content(body)

    # A base_version that names nothing would render the "outdated" flag
    # meaningless (and quietly mislead a reviewer), so it is a 422 rather than
    # a value we store and hope about.
    if body.base_version is not None:
        if store.get_version(artifact_id, body.base_version) is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"artifact {artifact_id} has no version "
                    f"{body.base_version}, so it cannot be a base_version"
                ),
            )

    if not _claim_submission_slot(request.app, artifact_id, caller.key):
        return _version_rate_limited()

    built, source = _build(body)
    # The canonical copy always goes to the *submitter's* project: whoever
    # wrote a version keeps its source of truth.
    canonical_file_id = _store_canonical(caller, token, artifact_id, built.html)

    status = STATUS_LIVE if is_owner else STATUS_PROPOSED
    envelope = Envelope(
        id=artifact_id,
        # Replaced by add_version_next, which allocates atomically.
        version=0,
        title=built.title,
        html=built.html,
        source_type=built.source_type,
        source=source,
        author=_identity(caller),
        status=status,
        note=body.note or None,
        base_version=body.base_version,
        canonical_file_id=canonical_file_id,
        created_at=_now(),
    )
    envelope.version = store.add_version_next(envelope)
    # A proposal is news for the owner; an owner's own live version is news for
    # everybody watching, except the v1 that publishing already produced.
    if status == STATUS_PROPOSED:
        _emit_webhook(
            request,
            meta,
            "version.proposed",
            {
                "title": envelope.title,
                "version": envelope.version,
                "note": envelope.note,
                "base_version": envelope.base_version,
            },
            actor=caller,
        )
    elif envelope.version > 1:
        _emit_webhook(
            request,
            meta,
            "version.published",
            {
                "title": envelope.title,
                "version": envelope.version,
                "note": envelope.note,
            },
            actor=caller,
        )
    logger.info(
        "Artifact %s got version %d (%s) from project %s",
        artifact_id,
        envelope.version,
        status,
        caller.project_id,
    )
    return JSONResponse(
        status_code=201,
        content={
            "id": artifact_id,
            "version": envelope.version,
            "status": envelope.status,
            "note": envelope.note,
            "base_version": envelope.base_version,
            # Built from the share id: the version has to be readable at the
            # artifact's *public* address, not its internal handle.
            "url": (
                f"{base_url(request).rstrip('/')}/a/{meta.share_id}"
                f"/v/{envelope.version}"
            ),
        },
    )


@app.post(
    "/api/artifacts/{artifact_id}/versions/{version}/promote",
    tags=["versions"],
    summary="Promote a proposal to live",
    description="Owner-only. Marks a proposed version live so it can be served. With the default head mode ('latest') the promoted version immediately becomes what /a/{id} serves.",
    responses={
        200: {
            "description": (
                "Version promoted; reports its new status, the head mode and "
                "the version now served as head."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: {"description": "Only the owning project may promote a version."},
        404: {"description": "No artifact, or no such version."},
        409: {"description": "That version is already live."},
        422: {"description": "'version' is not an integer."},
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable."
            )
        },
    },
)
@_serialized_per_artifact
def promote_version(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    version: int = PathParam(..., description=VERSION_DESC),
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Owner approves a proposal: its status becomes live."""
    owner, _token = auth
    ensure_hydrated(request.app)
    store = request.app.state.store

    meta = store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    _owner_only(meta, owner)

    envelope = store.get_version(artifact_id, version)
    if envelope is None:
        return _version_not_found(artifact_id, version)
    if envelope.status == STATUS_LIVE:
        return JSONResponse(
            status_code=409,
            content={
                "error": "version is already live",
                "id": artifact_id,
                "version": version,
            },
        )

    if not store.set_status(artifact_id, version, STATUS_LIVE):
        return _version_not_found(artifact_id, version)

    head_version = _head_version_of(request, artifact_id)
    _emit_webhook(
        request,
        meta,
        "version.promoted",
        {
            "title": envelope.title,
            "version": version,
            "head_version": head_version,
        },
        actor=owner,
    )
    logger.info(
        "Promoted version %d of artifact %s (head is now v%s)",
        version,
        artifact_id,
        head_version,
    )
    return JSONResponse(
        {
            "id": artifact_id,
            "version": version,
            "status": STATUS_LIVE,
            "head_mode": meta.head_mode,
            "head_version": head_version,
            "url": (
                f"{base_url(request).rstrip('/')}/a/{meta.share_id}/v/{version}"
            ),
        }
    )


@app.delete(
    "/api/artifacts/{artifact_id}/versions/{version}",
    tags=["versions"],
    summary="Delete a version or withdraw a proposal",
    description="The owning project may delete any version except the last live one and the version the head is currently pinned to. A contributor may delete only their own proposal — withdrawing a submission they no longer stand behind. Deleting the pinned version is refused with 409: pin the head to another live version, or switch it back to 'latest', and then delete.",
    responses={
        200: {
            "description": (
                "Version deleted; reports the version now served as head."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: {
            "description": (
                "Not the owner, and not the author of this proposal; or the "
                "owner, but without the admin/allowlisted token this hub's "
                "destructive_token_policy requires (see GET /context, "
                "'limits'). Withdrawing your own proposal is never gated by "
                "that policy."
            )
        },
        404: {"description": "No artifact, or no such version."},
        409: {
            "description": (
                "This is the only live version and an artifact must keep one, "
                "or this is the version the head is pinned to — re-pin the "
                "head or switch it to 'latest' first."
            )
        },
        422: {"description": "'version' is not an integer."},
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, the hub's own Storage is unavailable, or the "
                "delete only partially succeeded — the stored file could not "
                "be removed, so the version is still readable and the call "
                "must be retried."
            )
        },
    },
)
@_serialized_per_artifact
def delete_version(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    version: int = PathParam(..., description=VERSION_DESC),
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Owner deletes any version; a contributor withdraws their own proposal."""
    caller, _token = auth
    ensure_hydrated(request.app)
    store = request.app.state.store

    meta = store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    envelope = store.get_version(artifact_id, version)
    if envelope is None:
        return _version_not_found(artifact_id, version)

    withdrawing_own_proposal = (
        envelope.status == STATUS_PROPOSED and envelope.author_key == caller.key
    )
    if meta.owner_key != caller.key and not withdrawing_own_proposal:
        raise HTTPException(
            status_code=403,
            detail=(
                "only the artifact owner can delete this version; a "
                "contributor may withdraw their own proposal"
            ),
        )
    if not withdrawing_own_proposal:
        # SEC-075-011: only the *owner authority* path is gated. Withdrawing
        # a proposal you yourself submitted is removing your own contribution,
        # not exercising project-wide destructive power over someone else's
        # history, so no hub policy should stand between a contributor and
        # their own draft.
        _destructive_authority(caller)

    # store.delete_version answers False for two very different reasons: the
    # policy refusal below, and a backend delete that did not confirm. The
    # refusal is decided here so the remaining False can only mean "the files
    # are still in Storage", which is a 502, not a 409.
    other_live = [
        row
        for row in store.list_versions(artifact_id)
        if row.get("status") == STATUS_LIVE and row.get("version") != version
    ]
    if envelope.status == STATUS_LIVE and not other_live:
        return JSONResponse(
            status_code=409,
            content={
                "error": "cannot delete the only live version",
                "detail": (
                    "An artifact must keep at least one live version. Publish "
                    "a replacement first, or delete the whole artifact."
                ),
                "id": artifact_id,
                "version": version,
            },
        )

    # COR-075-006: refuse to delete the version the head is pinned to.
    #
    # The delete used to succeed and leave the meta record naming a version
    # that no longer existed. Nothing was corrupted in a way a reader would
    # notice -- get_head logs the dangling pin and falls back to the newest
    # live version -- and that silence was the problem: the owner had asked
    # for one specific version to be served, the stored answer to "what does
    # /a/{id} serve" still said that version, and what was actually served was
    # something else. A pin is an explicit editorial decision, so resolving it
    # by guessing is worse than refusing.
    #
    # Of the two options in the review roadmap -- refuse, or silently rewrite
    # the head to "latest" in the same operation -- this is the refusal. It
    # keeps the head pointer something only the owner ever moves, and it costs
    # them one extra call that says out loud what the alternative would have
    # done behind their back. The invariant it establishes: after any
    # successful version delete, a stored "pinned" head still names a live
    # version.
    #
    # The check reads the meta record loaded above rather than the head
    # envelope, because a stale pin is exactly the state being guarded and
    # get_head would have already resolved it away. Both this route and
    # PUT /head are @_serialized_per_artifact, so the pin cannot move between
    # this check and the delete below.
    if meta.head_mode == HEAD_PINNED and meta.head_version == version:
        return JSONResponse(
            status_code=409,
            content={
                "error": "cannot delete the pinned head version",
                "detail": (
                    f"The head is pinned to version {version}, so deleting it "
                    "would leave the artifact pointing at a version that no "
                    "longer exists. Pin the head to another live version, or "
                    "switch it back to 'latest' "
                    f"(PUT /api/artifacts/{artifact_id}/head), then delete."
                ),
                "id": artifact_id,
                "version": version,
                "head_mode": meta.head_mode,
                "head_version": meta.head_version,
            },
        )

    if not store.delete_version(artifact_id, version):
        logger.error(
            "Partial delete of version %d of artifact %s: the Storage file "
            "could not be removed",
            version,
            artifact_id,
        )
        return JSONResponse(
            status_code=502,
            content={
                "error": "version not fully deleted",
                "detail": (
                    "Some stored files could not be removed; the version is "
                    "still readable. Retry the delete."
                ),
                "id": artifact_id,
                "version": version,
            },
        )

    logger.info(
        "Deleted version %d of artifact %s (by project %s)",
        version,
        artifact_id,
        caller.project_id,
    )
    return JSONResponse(
        {
            "deleted": True,
            "id": artifact_id,
            "version": version,
            "head_version": _head_version_of(request, artifact_id),
        }
    )


@app.put(
    "/api/artifacts/{artifact_id}/head",
    tags=["versions"],
    summary="Choose what /a/{id} serves",
    description="Owner-only. {'mode': 'latest'} always serves the newest live version; {'mode': 'pinned', 'version': n} freezes the artifact on one live version, which is also protected from retention pruning.",
    responses={
        200: {
            "description": (
                "Head pointer updated; reports the new head mode and the "
                "version now served."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: {
            "description": "Only the owning project may move the head pointer."
        },
        404: RESP_NOT_FOUND,
        422: {
            "description": (
                "Unknown mode, missing 'version' for 'pinned', or a version "
                "that does not exist or is not live."
            )
        },
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable."
            )
        },
    },
)
@_serialized_per_artifact
def set_head(
    body: HeadBody,
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Point the head at the newest live version, or pin it to one version."""
    owner, _token = auth
    ensure_hydrated(request.app)
    store = request.app.state.store

    meta = store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    _owner_only(meta, owner)

    if body.mode not in (HEAD_LATEST, HEAD_PINNED):
        raise HTTPException(
            status_code=422,
            detail=f"mode must be '{HEAD_LATEST}' or '{HEAD_PINNED}'",
        )

    if body.mode == HEAD_PINNED:
        if body.version is None:
            raise HTTPException(
                status_code=422, detail="'version' is required when mode is 'pinned'"
            )
        pinned = store.get_version(artifact_id, body.version)
        if pinned is None:
            raise HTTPException(
                status_code=422,
                detail=f"artifact {artifact_id} has no version {body.version}",
            )
        if pinned.status != STATUS_LIVE:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"version {body.version} is {pinned.status}; only a live "
                    "version can be pinned as head"
                ),
            )
        meta.head_mode = HEAD_PINNED
        meta.head_version = body.version
    else:
        meta.head_mode = HEAD_LATEST
        meta.head_version = None

    meta.updated_at = _now()
    store.save_meta(meta)
    head_version = _head_version_of(request, artifact_id)
    logger.info(
        "Artifact %s head set to %s (serving v%s)",
        artifact_id,
        meta.head_mode,
        head_version,
    )
    return JSONResponse(
        {
            "id": artifact_id,
            "head_mode": meta.head_mode,
            "head_version_served": head_version,
        }
    )


# --------------------------------------------------------------------------
# Inline comments
#
# Reading threads is public (GET /a/{id}/comments); writing one needs any
# verified Storage token, which is what ``require_owner`` returns — despite the
# name it authenticates *a* caller, and each handler below decides for itself
# whether that caller is the artifact owner, a thread author, or neither.
# --------------------------------------------------------------------------


def _thread_response(thread: CommentThread, status_code: int) -> JSONResponse:
    """One thread as the public projection, plus its id under 'thread_id'."""
    return JSONResponse(
        status_code=status_code,
        content={**thread.public_dict(), "thread_id": thread.id},
    )


def _comment_rate_limited(guest: bool = False) -> JSONResponse:
    who = "Your invitation" if guest else "Your project"
    return JSONResponse(
        status_code=429,
        content={
            "error": "too many comments today",
            "detail": (
                f"{who} may write {settings.max_comments_per_day} comments "
                "and replies on one artifact per UTC day."
            ),
            "limit": settings.max_comments_per_day,
        },
    )


def _comment_writer(request: Request) -> tuple[Owner | None, tuple[str, str] | None]:
    """Authenticate whoever is about to write a comment.

    Two credentials are accepted, and exactly one is used per request:

    * a Keboola Storage token (the usual two management headers), which
      identifies a verified *project*, or
    * an ``X-Artifact-Guest`` invitation credential, which identifies one
      invited *person* who has no Keboola account at all.

    When both arrive, the guest header wins: sending it is an unambiguous
    "act as this invitation", and a caller who wants their project's identity
    simply does not send it. (The review UI never sends both — it prefers the
    token it holds and drops the invitation for that request.)

    Returns ``(owner, None)`` or ``(None, credential)``. The guest credential
    comes back unverified on purpose: proving it needs the artifact's meta
    record, and that lookup belongs after this function so a bad *token* still
    answers 401 before the artifact is even looked up — the ordering every
    existing caller of these routes already depends on.
    """
    credential = _guest_credential(request)
    if credential is not None:
        return None, credential
    owner, _token = require_owner(request)
    return owner, None


def _comment_gate(request: Request, meta: ArtifactMeta) -> JSONResponse | None:
    """The reader gate a comment write must clear, or ``None`` when it did.

    The review surface of a password-protected artifact is part of that
    artifact: ``GET /a/{id}/comments`` needs the password to be read, and
    ``GET /a/{id}/guest`` already says in so many words that an invitation is
    a grant to comment, *not* a way around the reader password. Writing was
    the hole — a guest credential alone let somebody read and write the whole
    discussion of a protected document.

    The policy is exactly the read policy of ``/a/{id}/raw``: the password (or
    an unlock cookie) is required from *everyone*, with no exemption for a
    token-authenticated owner, because the read path grants none either. It
    answers the read path's 401, and ``reader_allowed`` itself raises the
    read path's 429 once the failed-attempt budget is spent.
    """
    if reader_allowed(meta, request):
        return None
    return _password_required()


def _comment_author(
    caller: Owner | None, invitation: dict | None
) -> tuple[dict, str]:
    """``(author record, rate-limit key)`` for a verified comment writer.

    The key is what the daily budget is counted against: an owner key for a
    project, ``guest:{invitation_id}`` for a guest. The two namespaces cannot
    collide, so one person's invitation can never spend a project's budget or
    vice versa.
    """
    if caller is not None:
        return _identity(caller), caller.key
    author = _guest_identity(invitation or {})
    return author, author_key_of(author)


def _may_moderate_thread(
    meta: ArtifactMeta,
    thread: CommentThread,
    caller: Owner | None,
    invitation: dict | None,
) -> bool:
    """May this writer resolve, reopen or delete ``thread``?

    The artifact owner may moderate anything on their own artifact, and any
    author may act on their own thread. A guest is only ever the second of
    those: an invitation grants a voice in the discussion, never authority over
    somebody else's part of it.
    """
    if caller is not None:
        return caller.key in (meta.owner_key, thread.author_key)
    author, key = _comment_author(None, invitation)
    del author
    return bool(key) and key == thread.author_key


@app.post(
    "/api/artifacts/{artifact_id}/comments",
    status_code=201,
    tags=["comments"],
    summary="Open an inline comment thread",
    description=(
        "Anchors a new thread to a quoted passage of one version, W3C "
        "annotation style: 'exact' is the quote as rendered, 'prefix' and "
        "'suffix' are about 32 characters of surrounding text so a repeated "
        "quote can still be told apart. The thread stays bound to the version "
        "it was made on — there is no cross-version re-anchoring — so a "
        "thread opened on an older version may no longer highlight anything "
        "on the current one.\n\n"
        "Any verified Keboola project may comment while 'comments_mode' is "
        "'anyone' (the default); 'allowlist' restricts it to the artifact's "
        "contributors and 'off' closes it. The owner may always comment "
        "unless the artifact is final.\n\n" + GUEST_WRITE_NOTE + "\n\n"
        + COMMENT_PASSWORD_NOTE + "\n\n" + COMMENT_TARGET_NOTE
    ),
    responses={
        201: {
            "description": (
                "Thread created; returns the whole thread (selector, body, "
                "author project or guest name, timestamps) plus 'thread_id'."
            )
        },
        400: RESP_STACK_400,
        401: RESP_COMMENT_401,
        403: {
            "description": (
                "Commenting is closed on this artifact, or the caller's "
                "project is not on its contributor allowlist."
            )
        },
        404: RESP_NOT_FOUND,
        409: RESP_FINAL_409,
        413: RESP_BODY_413,
        422: {
            "description": (
                "The referenced version does not exist, the quote is empty or "
                "too long, or the comment body is empty or too long."
            )
        },
        429: RESP_COMMENTS_429,
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable."
            )
        },
    },
)
@_serialized_per_comment_target
def create_comment(
    body: CommentBody,
    request: Request,
    artifact_id: str = PathParam(..., description=COMMENT_TARGET_ID_DESC),
) -> Response:
    """Open a thread anchored to a quoted passage of one version."""
    caller, credential = _comment_writer(request)
    meta = _comment_target_of(request, artifact_id, caller)
    if meta is None:
        return _not_found(artifact_id)
    locked = _comment_gate(request, meta)
    if locked is not None:
        return locked
    invitation = (
        _verify_guest_checked(request, meta, credential) if credential else None
    )

    if meta.is_frozen():
        return _document_frozen(meta, "new comments")
    # A guest is not subject to comments_mode: their invitation *is* the grant,
    # and it was issued by the very owner that mode belongs to.
    if caller is not None and not meta.allows_comments_from(caller.key):
        return _comments_closed(meta)

    store = request.app.state.store
    if store.get_version(meta.id, body.version) is None:
        raise HTTPException(
            status_code=422,
            detail=f"artifact {artifact_id} has no version {body.version}",
        )

    author, writer_key = _comment_author(caller, invitation)
    if not _claim_comment_slot(request.app, meta.id, writer_key):
        return _comment_rate_limited(guest=caller is None)

    thread = CommentThread(
        id=new_artifact_id(),
        artifact_id=meta.id,
        version=body.version,
        selector=Selector(
            exact=body.exact, prefix=body.prefix, suffix=body.suffix
        ),
        body=body.body,
        author=author,
        created_at=_now(),
    )
    try:
        request.app.state.comments.create(thread)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    _emit_webhook(
        request,
        meta,
        "comment.created",
        {"thread_id": thread.id, "version": thread.version, "quote": body.exact},
        actor=caller,
        actor_name=None if caller is not None else _guest_actor(invitation or {}),
    )
    logger.info(
        "Artifact %s got comment thread %s on v%d from %s",
        meta.id,
        thread.id,
        thread.version,
        f"project {caller.project_id}" if caller else f"guest {writer_key}",
    )
    return _thread_response(thread, 201)


@app.post(
    "/api/artifacts/{artifact_id}/comments/{thread_id}/replies",
    status_code=201,
    tags=["comments"],
    summary="Reply in a comment thread",
    description=(
        "Appends a reply to an existing thread. The same policy as opening a "
        "thread applies: 'comments_mode' decides who may write, the owner may "
        "always reply unless the artifact is final, and replies count against "
        "the same per-project daily cap.\n\n" + GUEST_WRITE_NOTE + "\n\n"
        + COMMENT_PASSWORD_NOTE + "\n\n" + COMMENT_TARGET_NOTE
    ),
    responses={
        201: {
            "description": (
                "Reply appended; returns the whole updated thread plus "
                "'thread_id'."
            )
        },
        400: RESP_STACK_400,
        401: RESP_COMMENT_401,
        403: {
            "description": (
                "Commenting is closed on this artifact, or the caller's "
                "project is not on its contributor allowlist."
            )
        },
        404: RESP_THREAD_404,
        409: RESP_FINAL_409,
        413: RESP_BODY_413,
        422: {"description": "The reply body is empty or too long."},
        429: RESP_COMMENTS_429,
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable."
            )
        },
    },
)
@_serialized_per_comment_target
def reply_to_comment(
    body: ReplyBody,
    request: Request,
    artifact_id: str = PathParam(..., description=COMMENT_TARGET_ID_DESC),
    thread_id: str = PathParam(..., description=THREAD_ID_DESC),
) -> Response:
    """Append one reply to an existing thread."""
    caller, credential = _comment_writer(request)
    meta = _comment_target_of(request, artifact_id, caller)
    if meta is None:
        return _not_found(artifact_id)
    locked = _comment_gate(request, meta)
    if locked is not None:
        return locked
    invitation = (
        _verify_guest_checked(request, meta, credential) if credential else None
    )

    if meta.is_frozen():
        return _document_frozen(meta, "new comments")
    if caller is not None and not meta.allows_comments_from(caller.key):
        return _comments_closed(meta)

    comments = request.app.state.comments
    thread = comments.get(meta.id, thread_id)
    if thread is None:
        return _thread_not_found(artifact_id, thread_id)

    author, writer_key = _comment_author(caller, invitation)
    if not _claim_comment_slot(request.app, meta.id, writer_key):
        return _comment_rate_limited(guest=caller is None)

    # A copy, not an append. CommentStore.get answers from its in-memory LRU,
    # so the object here is the very one every later read in this process
    # gets: mutating it before the write succeeds means a refused reply is
    # still shown, and is persisted by whatever writes the thread next.
    candidate = dataclasses.replace(
        thread,
        replies=[*thread.replies, Reply(author=author, body=body.body, created_at=_now())],
    )
    try:
        comments.update(candidate)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    thread = candidate

    _emit_webhook(
        request,
        meta,
        "comment.replied",
        {"thread_id": thread_id, "version": thread.version},
        actor=caller,
        actor_name=None if caller is not None else _guest_actor(invitation or {}),
    )
    logger.info(
        "Comment thread %s of artifact %s got a reply from %s",
        thread_id,
        meta.id,
        f"project {caller.project_id}" if caller else f"guest {writer_key}",
    )
    return _thread_response(thread, 201)


@app.post(
    "/api/artifacts/{artifact_id}/comments/{thread_id}/resolve",
    tags=["comments"],
    summary="Resolve or reopen a comment thread",
    description=(
        "Marks a thread resolved and records who resolved it. Available to "
        "the artifact owner and to the thread's own author; anyone else gets "
        "a 403.\n\n"
        "The body is optional: sending nothing (or {\"resolved\": true}) "
        "resolves the thread, and {\"resolved\": false} reopens a resolved "
        "one — the same two principals may do either. Asking for the state "
        "the thread is already in is a 409.\n\n"
        "A guest (X-Artifact-Guest) may resolve and reopen the threads they "
        "opened themselves, and only those — an invitation never carries "
        "moderation authority over anybody else's thread.\n\n"
        "**Frozen documents refuse this.** A 'final' or trashed document "
        "freezes the state of its discussion as well as its content, so "
        "resolving and reopening are 409 there, exactly like a new comment "
        "or reply.\n\n"
        + COMMENT_PASSWORD_NOTE + "\n\n" + COMMENT_TARGET_NOTE
    ),
    responses={
        200: {
            "description": (
                "Thread resolved or reopened; returns the whole updated "
                "thread plus 'thread_id'."
            )
        },
        400: RESP_STACK_400,
        401: RESP_COMMENT_401,
        403: {
            "description": (
                "Only the artifact owner and the thread's author may resolve "
                "or reopen it; a guest only their own threads."
            )
        },
        404: RESP_THREAD_404,
        409: {
            "description": (
                "The thread is already resolved (or already open, when "
                "reopening), or the document is frozen — 'final' or trashed "
                "freezes thread moderation too."
            )
        },
        429: RESP_COMMENT_MOD_429,
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable."
            )
        },
    },
)
@_serialized_per_comment_target
def resolve_comment(
    request: Request,
    artifact_id: str = PathParam(..., description=COMMENT_TARGET_ID_DESC),
    thread_id: str = PathParam(..., description=THREAD_ID_DESC),
    body: ResolveBody | None = None,
) -> Response:
    """Owner or thread author closes a thread — or reopens it."""
    caller, credential = _comment_writer(request)
    meta = _comment_target_of(request, artifact_id, caller)
    if meta is None:
        return _not_found(artifact_id)
    locked = _comment_gate(request, meta)
    if locked is not None:
        return locked
    invitation = (
        _verify_guest_checked(request, meta, credential) if credential else None
    )

    comments = request.app.state.comments
    thread = comments.get(meta.id, thread_id)
    if thread is None:
        return _thread_not_found(artifact_id, thread_id)

    if not _may_moderate_thread(meta, thread, caller, invitation):
        raise HTTPException(
            status_code=403,
            detail=(
                "only the artifact owner or the thread's author can resolve "
                "or reopen it"
            ),
        )
    if meta.is_frozen():
        # Same gate as a new comment or reply. Resolving and reopening are
        # not new content, but they change the state of the discussion a
        # frozen document is supposed to have fixed -- leaving them open
        # meant "final" froze what could be added and not what the record
        # said. Checked after authorization so a caller who may not moderate
        # at all still learns that first.
        return _document_frozen(meta, "thread moderation")

    wanted = True if body is None else bool(body.resolved)
    if thread.resolved == wanted:
        return JSONResponse(
            status_code=409,
            content={
                "error": (
                    "thread is already resolved" if wanted else "thread is already open"
                ),
                "id": artifact_id,
                "thread_id": thread_id,
            },
        )

    author, writer_key = _comment_author(caller, invitation)
    # Copied for the same reason as a reply: the thread is the cached object,
    # so a failed write must not leave it looking resolved to every later read.
    candidate = dataclasses.replace(
        thread, resolved=wanted, resolved_by=author if wanted else None
    )
    try:
        comments.update(candidate)
    except ValueError as exc:
        # A thread that is already over an aggregate budget must still answer
        # with a reason rather than a 500 -- resolving it adds no content.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    thread = candidate

    logger.info(
        "Comment thread %s of artifact %s %s by %s",
        thread_id,
        meta.id,
        "resolved" if wanted else "reopened",
        f"project {caller.project_id}" if caller else f"guest {writer_key}",
    )
    return _thread_response(thread, 200)


@app.delete(
    "/api/artifacts/{artifact_id}/comments/{thread_id}",
    tags=["comments"],
    summary="Delete a comment thread",
    description=(
        "Removes a thread and all its replies from Storage. Available to the "
        "artifact owner (moderation) and to the thread's own author "
        "(withdrawing a comment); anyone else gets a 403. Irreversible.\n\n"
        "A guest (X-Artifact-Guest) may withdraw the threads they opened "
        "themselves, and only those.\n\n"
        "**On a frozen document only the owner may delete.** A 'final' or "
        "trashed document freezes an author's withdrawal along with every "
        "other contribution (409), but deliberately not the owner's: a "
        "comment that has to come off a finished record — a leaked secret, "
        "someone's personal data — must stay removable without destroying "
        "the whole document.\n\n"
        + COMMENT_PASSWORD_NOTE + "\n\n" + COMMENT_TARGET_NOTE
    ),
    responses={
        200: {"description": "Thread deleted."},
        400: RESP_STACK_400,
        401: RESP_COMMENT_401,
        403: {
            "description": (
                "Only the artifact owner or the thread's author may delete "
                "it; a guest only their own threads."
            )
        },
        404: RESP_THREAD_404,
        409: {
            "description": (
                "The document is frozen ('final' or trashed) and the caller "
                "is not its owner, so this withdrawal is refused. The owner "
                "may still delete."
            )
        },
        429: RESP_COMMENT_MOD_429,
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, the hub's own Storage is unavailable, or the "
                "delete only partially succeeded — some stored files of the "
                "thread could not be removed, so it is still readable and the "
                "call must be retried."
            )
        },
    },
)
@_serialized_per_comment_target
def delete_comment(
    request: Request,
    artifact_id: str = PathParam(..., description=COMMENT_TARGET_ID_DESC),
    thread_id: str = PathParam(..., description=THREAD_ID_DESC),
) -> Response:
    """Owner moderates, or an author withdraws their own thread."""
    caller, credential = _comment_writer(request)
    meta = _comment_target_of(request, artifact_id, caller)
    if meta is None:
        return _not_found(artifact_id)
    locked = _comment_gate(request, meta)
    if locked is not None:
        return locked
    invitation = (
        _verify_guest_checked(request, meta, credential) if credential else None
    )

    comments = request.app.state.comments
    thread = comments.get(meta.id, thread_id)
    if thread is None:
        return _thread_not_found(artifact_id, thread_id)

    if not _may_moderate_thread(meta, thread, caller, invitation):
        raise HTTPException(
            status_code=403,
            detail=(
                "only the artifact owner or the thread's author can delete "
                "this comment"
            ),
        )
    # Deliberately narrowed rather than frozen outright. Removal is the one
    # comment mutation that has to survive a frozen document: a comment that
    # must come off a finished record -- a leaked secret, someone's personal
    # data -- would otherwise be unremovable short of destroying the whole
    # artifact. An author *withdrawing* their thread is a different act,
    # contribution-shaped like a reply, so that freezes with everything else
    # and only the owner's moderation remains.
    if meta.is_frozen() and (caller is None or caller.key != meta.owner_key):
        return _document_frozen(meta, "withdrawing your own comment")

    if not comments.delete(meta.id, thread_id):
        # delete() reports False both when nothing matched and when a backend
        # delete failed with files left behind. Re-reading tells the two apart:
        # a failed erasure keeps the thread listable, a concurrent delete does
        # not. Claiming "deleted" over a thread that is still readable is the
        # one answer this route must never give.
        if comments.get(meta.id, thread_id) is None:
            return _thread_not_found(artifact_id, thread_id)
        logger.error(
            "Partial delete of comment thread %s of artifact %s: some Storage "
            "files remain",
            thread_id,
            meta.id,
        )
        return JSONResponse(
            status_code=502,
            content={
                "error": "comment thread not fully deleted",
                "detail": (
                    "Some stored files of this comment thread could not be "
                    "removed; the thread is still readable. Retry the delete."
                ),
                "id": artifact_id,
                "thread_id": thread_id,
            },
        )
    _, writer_key = _comment_author(caller, invitation)
    logger.info(
        "Deleted comment thread %s of artifact %s (by %s)",
        thread_id,
        meta.id,
        f"project {caller.project_id}" if caller else f"guest {writer_key}",
    )
    return JSONResponse(
        {"deleted": True, "id": artifact_id, "thread_id": thread_id}
    )
