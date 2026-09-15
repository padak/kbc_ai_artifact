"""Artifact envelopes, artifact meta records and the versioned serving store.

The single source of truth for served artifacts is Storage Files of the *host*
project. Since phase 2 (community versioning) one artifact is spread over
several files:

- one **version** file per version, ``artifact-{id}-v{n}.json``, tagged
  ``artifact-hub``, ``artifact-id-{id}`` and ``artifact-ver-{n}``. It holds the
  content (html + source), the verified ``author``, a ``status`` ("live" or
  "proposed") and an optional ``note``.
- one **meta** file ``artifact-{id}-meta.json``, tagged ``artifact-hub``,
  ``artifact-id-{id}``, ``artifact-meta`` and ``artifact-owner-{key}``. It holds
  artifact-level state: owner, password record, contribution and comment
  policy (``accept_versions_mode``, ``contributors``, ``comments_mode``), the
  draft/final ``status`` and the head pointer. The owner tag lives on the meta
  file so listing an owner's artifacts stays a single tag search.

Inline comment threads live in their own files with their own tags and their
own store — see :mod:`src.comments`. The artifact index never sees them.

Schema-1 artifacts (one ``artifact-{id}.json`` envelope carrying owner and
password inline, tagged with the owner tag and no version/meta tag) are still
readable: they are treated as version 1 "live" and their meta record is
synthesized on read (:func:`migrate_legacy`) and materialized on the next write.

Because the app container has no permanent disk, the process keeps only:

- an in-memory index ``{artifact_id: _ArtEntry}`` rebuilt on startup by listing
  files tagged ``artifact-hub`` (:meth:`ArtifactStore.hydrate`),
- small in-process LRUs of parsed :class:`Envelope` / :class:`ArtifactMeta`
  objects keyed by ``(artifact_id, file_id)``, and
- a disk cache of raw JSON under ``cache_dir`` (pure cache — safe to wipe).

This module is deliberately pure: it never reads the clock and never touches the
network directly. Timestamps are produced by the caller; all Storage access goes
through the injected :class:`~src.kbc.FilesBackend`. The single ID-generating
exception is :meth:`ArtifactStore.rotate_share`, whose default generator is
:func:`src.security.new_artifact_id` (imported lazily, and overridable by the
caller for deterministic tests).

**Public share IDs (0.7.0).** ``/a/{...}`` URLs address an artifact by its
*share ID*, not by its internal artifact ID. A fresh artifact's share ID equals
its artifact ID (so every pre-0.7.0 URL keeps working), but
:meth:`ArtifactStore.rotate_share` mints a new one — after which the previous
share ID *and* the bare artifact ID both stop resolving publicly
(:meth:`ArtifactStore.resolve_share`). The internal artifact ID stays the handle
for owner ``/api/*`` operations.

**Trash (0.7.0).** :meth:`ArtifactStore.trash` is a reversible soft delete: the
meta record moves to status ``trashed`` (remembering the status to come back to
in ``restore_status``), the public link stops resolving, and
:meth:`ArtifactStore.restore` puts it back. :meth:`ArtifactStore.delete` remains
the irreversible purge.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
import logging
import os
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from src.kbc import BackendError, FileInfo, FilesBackend

logger = logging.getLogger(__name__)

TAG_ALL = "artifact-hub"
TAG_META = "artifact-meta"
_TAG_ID_PREFIX = "artifact-id-"
_TAG_OWNER_PREFIX = "artifact-owner-"
_TAG_VER_PREFIX = "artifact-ver-"

# Characters accepted in an artifact ID when it is used to build a cache file
# name. Artifact IDs come from ``secrets.token_urlsafe`` so this is a superset
# of what we generate; anything else simply skips the disk cache.
_SAFE_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)

# The disk cache holds plaintext-sensitive JSON — meta records carry the
# password and guest-invitation PBKDF2 records and semi-secret webhook URLs —
# under predictable file names, so it must never be world-readable. The cache
# directory is created/forced to 0o700 and every cache file written 0o600
# (owner-only). Enforced on POSIX; a no-op-ish best effort elsewhere.
CACHE_DIR_MODE = 0o700
CACHE_FILE_MODE = 0o600


def _harden_cache_dir(cache_dir: Path) -> None:
    """Create ``cache_dir`` if needed and force it to owner-only (0o700) perms.

    ``mkdir(mode=...)`` is masked by the process umask and leaves an existing
    directory's perms untouched, so an explicit ``chmod`` is what actually
    guarantees the mode. The chmod is best effort (logged, not raised): the
    disk cache is optional and the app degrades gracefully without it.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        cache_dir.chmod(CACHE_DIR_MODE)
    except OSError as exc:
        logger.warning("Cannot set cache dir perms on %s: %s", cache_dir, exc)


def _write_private_bytes(tmp: Path, raw: bytes) -> None:
    """Write ``raw`` to ``tmp`` atomically-preparable, with owner-only perms.

    The file is created via ``os.open`` with an explicit 0o600 create mode and
    then ``chmod``-ed, because the create mode is masked by the umask. Callers
    ``os.replace`` the temp file onto the final name afterwards; ``replace``
    keeps the inode (and therefore the 0o600 perms) intact.
    """
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, CACHE_FILE_MODE)
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)
    os.chmod(tmp, CACHE_FILE_MODE)


_SOURCE_TYPES = ("html", "markdown", "git-html", "git-markdown")

#: Version status values. "proposed" content is moderated: readable only by the
#: artifact owner or the submitting author, never served as the head.
STATUS_LIVE = "live"
STATUS_PROPOSED = "proposed"
_STATUSES = (STATUS_LIVE, STATUS_PROPOSED)

#: Head pointer modes of :class:`ArtifactMeta`.
HEAD_LATEST = "latest"
HEAD_PINNED = "pinned"
_HEAD_MODES = (HEAD_LATEST, HEAD_PINNED)

#: Who may submit a version (:attr:`ArtifactMeta.accept_versions_mode`).
ACCEPT_OFF = "off"
ACCEPT_ANYONE = "anyone"
ACCEPT_ALLOWLIST = "allowlist"
ACCEPT_MODES = (ACCEPT_OFF, ACCEPT_ANYONE, ACCEPT_ALLOWLIST)
#: Modes that mean "someone other than the owner may contribute".
_ACCEPT_ON_MODES = (ACCEPT_ANYONE, ACCEPT_ALLOWLIST)

#: Who may comment (:attr:`ArtifactMeta.comments_mode`).
COMMENTS_ANYONE = "anyone"
COMMENTS_ALLOWLIST = "allowlist"
COMMENTS_OFF = "off"
COMMENTS_MODES = (COMMENTS_ANYONE, COMMENTS_ALLOWLIST, COMMENTS_OFF)

#: Artifact lifecycle status. "final" freezes new versions and new comments for
#: *everyone*, the owner included; reopening is an owner action in the API layer
#: (set the status back to "draft"). "trashed" is the soft-deleted state: the
#: artifact is frozen *and* its public link stops resolving, until the owner
#: either restores it (back to :attr:`ArtifactMeta.restore_status`) or purges it
#: for good with :meth:`ArtifactStore.delete`.
ARTIFACT_DRAFT = "draft"
ARTIFACT_FINAL = "final"
ARTIFACT_TRASHED = "trashed"
ARTIFACT_STATUSES = (ARTIFACT_DRAFT, ARTIFACT_FINAL, ARTIFACT_TRASHED)
#: Statuses an API caller may set directly. "trashed" is deliberately excluded:
#: it is reached only through :meth:`ArtifactStore.trash`, which also records
#: ``trashed_at`` and the status to restore to, and left only through
#: :meth:`ArtifactStore.restore`.
ARTIFACT_SETTABLE_STATUSES = (ARTIFACT_DRAFT, ARTIFACT_FINAL)

#: Schema version written by this module.
SCHEMA_VERSION = 2


class RevisionLedger:
    """A monotonic mutation counter per artifact, for O(1) change detection.

    Closes REL-100-003. ``GET /a/{id}/live`` used to enumerate every version
    and every comment thread just to hash them into an ETag, which meant a
    conditional poll that answers "nothing changed" still paid the full price
    of finding that out. With this ledger the poll turns
    :meth:`token` into an ETag and answers 304 having read one dictionary
    entry.

    **Why an in-memory counter is exact here, not an approximation.** Per
    CLAUDE.md this service is deployed as exactly one Keboola Data App
    container running exactly one uvicorn process, and that is a deployment
    invariant rather than a scaling choice -- the version allocator, the
    per-process locks and the StateDB snapshot cycle already depend on it.
    Under it, this process is the only writer that exists, and no artifact can
    change without passing through the store method that bumps its number. So
    "same token" really does mean "same state", with no window in which
    somebody else moved the artifact behind our back.

    **Why a boot nonce.** The ledger does not survive a restart, and a bare
    counter restarting at zero would let a cold process re-issue a tag it had
    already used for older content -- a client would be told "unchanged" about
    something that had in fact moved. Mixing a random per-process nonce into
    every token makes that impossible: after a restart the same artifact in
    the same state hands out a *different* tag, so a stale client refetches
    once (cheap, correct) instead of missing an update (silent, wrong).

    **Why reads never insert.** :meth:`token` treats an unknown artifact as
    revision 0 rather than creating an entry, and :meth:`forget` drops an
    artifact when it is purged. Between them the ledger stays bounded by the
    number of live artifacts, so neither anonymous polling of made-up ids nor
    a create/purge loop can grow it -- the failure mode SEC-100-002 found in
    the lock registry.
    """

    def __init__(self) -> None:
        # 128 bits: this only has to be unique across process lifetimes, and
        # it is public (it ends up hashed into an ETag), never a secret.
        self._boot = secrets.token_hex(16)
        self._revisions: dict[str, int] = {}
        # Its own lock, never taken while a store lock is held, so bumping can
        # be called from inside a store method without any ordering concern.
        self._lock = threading.Lock()

    def bump(self, artifact_id: str) -> int:
        """Record one committed mutation of an artifact; returns the new number."""
        with self._lock:
            value = self._revisions.get(artifact_id, 0) + 1
            self._revisions[artifact_id] = value
            return value

    def token(self, artifact_id: str) -> str:
        """This artifact's current revision, as an opaque per-process token."""
        with self._lock:
            value = self._revisions.get(artifact_id, 0)
        return f"{self._boot}:{value}"

    def forget(self, artifact_id: str) -> None:
        """Drop a purged artifact's entry. Ids are random and never come back."""
        with self._lock:
            self._revisions.pop(artifact_id, None)


def tag_for_id(artifact_id: str) -> str:
    """Storage tag identifying every file of a single artifact."""
    return f"{_TAG_ID_PREFIX}{artifact_id}"


def tag_for_owner(owner_key: str) -> str:
    """Storage tag identifying all artifacts of one owner (stack + project)."""
    return f"{_TAG_OWNER_PREFIX}{owner_key}"


def tag_for_version(version: int) -> str:
    """Storage tag identifying one version file of an artifact."""
    return f"{_TAG_VER_PREFIX}{version}"


def _tag_value(tags: list[str], prefix: str) -> str | None:
    for tag in tags or []:
        if tag.startswith(prefix):
            return tag[len(prefix) :]
    return None


def _version_from_tags(tags: list[str]) -> int | None:
    """Version number encoded in an ``artifact-ver-N`` tag, if any."""
    raw = _tag_value(tags, _TAG_VER_PREFIX)
    if raw is None:
        return None
    try:
        number = int(raw)
    except ValueError:
        return None
    return number if number > 0 else None


def _stack_host(stack_url: str) -> str:
    """Hostname of a stack URL — the only part of it we expose publicly."""
    if not stack_url:
        return ""
    return urlsplit(stack_url).hostname or ""


def _clean_revoked(entry: dict) -> bool:
    """Read one invitation's ``revoked`` flag, failing closed on anything odd.

    ``True``/``False`` are honoured, an absent key means "not revoked", and
    every other value -- a string, a number, a container -- is treated as
    revoked, because a revocation flag we cannot read must not keep granting
    access.
    """
    if "revoked" not in entry:
        return False
    return entry["revoked"] is not False


def _clean_invitations(raw: object) -> list[dict]:
    """Normalize a guest-invitation list read from an untrusted meta record.

    An entry survives only when it carries a non-empty string ``id`` and a
    ``secret`` that is at least shaped like a hash record (a dict) — anything
    else could never authenticate a guest, so keeping it would only grow the
    meta file. The remaining descriptive fields are coerced rather than
    dropped, so a half-written entry degrades to an unnamed invitation
    instead of taking the whole artifact down. ``revoked`` is the exception:
    it is a security decision, so it is read strictly and fails closed (see
    :func:`_clean_revoked`).

    This is the single place invitation records are normalized -- every
    reader downstream may therefore assume ``revoked`` is a real bool.
    """
    if not isinstance(raw, list):
        return []
    cleaned: list[dict] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        invitation_id = entry.get("id")
        if not isinstance(invitation_id, str) or not invitation_id:
            continue
        if invitation_id in seen:
            continue
        secret = entry.get("secret")
        if not isinstance(secret, dict):
            continue
        seen.add(invitation_id)
        cleaned.append(
            {
                "id": invitation_id,
                "name": str(entry.get("name") or ""),
                "secret": secret,
                "created_at": str(entry.get("created_at") or ""),
                # A revocation flag fails closed. Strict identity, not
                # truthiness: bool("false") is True while bool(0) is False,
                # so coercion lets the value's *type* decide whether a guest
                # still has a voice. A missing key is the one benign case --
                # that is simply an invitation written before this field --
                # so it keeps meaning "not revoked".
                "revoked": _clean_revoked(entry),
            }
        )
    return cleaned


def _clean_webhook_key_epoch_record(entry: object) -> dict | None:
    """Normalize one receiver's epoch record, or ``None`` if it is unusable.

    A usable record needs a non-empty string ``epoch`` — that is the value
    :func:`src.webhooks.receiver_signing_key` mixes into the derivation, so
    without it there is nothing to rotate to. ``previous_epoch`` may
    legitimately be ``None`` (the receiver's first-ever rotation, out of the
    epoch-less legacy state) or a non-empty string; anything else is coerced
    to ``None`` rather than dropping the whole record, since the current
    epoch is still perfectly usable without it — it just means a rotation in
    flight would stop offering the previous key's grace period early rather
    than accepting a forged one. ``rotated_at`` is coerced to a string the
    same tolerant way every other timestamp field on this class is.
    """
    if not isinstance(entry, dict):
        return None
    epoch = entry.get("epoch")
    if not isinstance(epoch, str) or not epoch:
        return None
    previous_epoch = entry.get("previous_epoch")
    if not isinstance(previous_epoch, str) or not previous_epoch:
        previous_epoch = None
    return {
        "epoch": epoch,
        "previous_epoch": previous_epoch,
        "rotated_at": str(entry.get("rotated_at") or ""),
    }


def _clean_webhook_key_epochs(raw: object) -> dict[str, dict]:
    """Normalize the webhook receiver key-rotation map read from meta.

    Keyed by receiver URL, so an entry survives only under a non-empty string
    key; the value goes through :func:`_clean_webhook_key_epoch_record` and is
    dropped entirely rather than kept half-shaped, exactly like
    :func:`_clean_invitations` drops an invitation it cannot use. Missing
    (every meta file written before SEC-100-006) or non-dict input becomes an
    empty map -- the same "no epoch on record" state a receiver that has never
    been rotated is already in, so :func:`src.webhooks.receiver_signing_key`
    falls back to its original epoch-less derivation and an old-shape
    persisted receiver keeps verifying without any migration step.
    """
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, dict] = {}
    for url, entry in raw.items():
        if not isinstance(url, str) or not url:
            continue
        record = _clean_webhook_key_epoch_record(entry)
        if record is not None:
            cleaned[url] = record
    return cleaned


@dataclass
class ArtifactMeta:
    """Artifact-level state: who owns it, who may contribute, where head points.

    **``accept_versions`` vs ``accept_versions_mode``.** Phase 3 turned the
    old boolean "anyone may submit a version" switch into a three-way mode
    (``off`` / ``anyone`` / ``allowlist``). To keep every existing caller and
    every meta file already in Storage working, the two live side by side:

    * :attr:`accept_versions_mode` is the **source of truth** — one of
      :data:`ACCEPT_MODES`, default :data:`ACCEPT_OFF`.
    * ``accept_versions`` is a **derived compatibility field**: a ``bool``
      property that reads ``True`` iff the mode is not ``off``. Assigning to it
      (``meta.accept_versions = True``) flips the mode between ``off`` and
      ``anyone``, and is a no-op when the mode already agrees — so turning an
      allowlisted artifact "on" does not silently widen it to ``anyone``.

    Both keys are written by :meth:`to_json`; :meth:`from_json` prefers
    ``accept_versions_mode`` when it carries a known mode and otherwise maps the
    legacy boolean (``false`` -> ``off``, ``true`` -> ``anyone``).

    Precedence when a caller passes both: the explicit ``accept_versions``
    boolean wins, because the generated ``__init__`` assigns the mode first and
    the compatibility setter second. Pass one or the other, not two
    contradictory values.

    The property is attached right after the class body (see
    ``_accept_versions_get``/``_accept_versions_set`` below) because a dataclass
    field and a property cannot be declared under the same name. The field is
    declared *after* ``accept_versions_mode`` so the generated ``__init__``
    assigns the mode first and the setter then sees the real mode; its default
    is ``None``, meaning "the caller said nothing, leave the mode alone".

    **``share_id`` vs ``id``.** ``id`` is the internal handle used by every
    ``/api/*`` owner operation and by every Storage tag; :attr:`share_id` is the
    *public* identifier that appears in ``/a/{...}`` URLs. They start out equal
    (and :meth:`from_json` fills an empty/missing ``share_id`` with ``id``, so
    every artifact written before 0.7.0 keeps its URL), and diverge the moment
    the owner rotates the link — see :meth:`ArtifactStore.rotate_share`.

    **Trash.** ``status == "trashed"`` is a soft delete: :attr:`trashed_at`
    records when, :attr:`restore_status` records the status to return to, and
    the public link is dead until :meth:`ArtifactStore.restore` runs.
    """

    id: str
    # {"stack_url": str, "project_id": int, "project_name": str, "key": str}
    owner: dict = field(default_factory=dict)
    # security.hash_password() record, or None when the artifact is public.
    password: dict | None = None
    # Source of truth for version contributions; one of ACCEPT_MODES.
    accept_versions_mode: str = ACCEPT_OFF
    # Compatibility alias for accept_versions_mode; see the class docstring.
    # Replaced by a property below — never read this annotation as storage.
    accept_versions: bool | None = None
    # Owner keys ("project@stackhost") allowed when a mode is "allowlist".
    contributors: list[str] = field(default_factory=list)
    # One of COMMENTS_MODES; comments are open by default.
    comments_mode: str = COMMENTS_ANYONE
    # One of ARTIFACT_STATUSES. "final" freezes versions and comments;
    # "trashed" additionally kills the public link (soft delete).
    status: str = ARTIFACT_DRAFT
    # Public identifier used in /a/{...} URLs. Empty means "same as id" and is
    # normalized to ``id`` in __post_init__, so it is never empty in practice.
    share_id: str = ""
    # ISO 8601 UTC of the soft delete; "" whenever the artifact is not trashed.
    trashed_at: str = ""
    # The status ``restore()`` returns to; one of ARTIFACT_SETTABLE_STATUSES.
    restore_status: str = ARTIFACT_DRAFT
    # Outbound webhook URLs the owner registered for this artifact. Semi-secret
    # (a Slack incoming-hook path *is* its credential), so the API layer never
    # echoes them outside the owner response that set them. Validation —
    # https-only, no SSRF targets, per-artifact cap — is the API layer's job
    # (:func:`src.webhooks.validate_webhook_url`); this module only stores the
    # list and normalizes obviously unusable entries away.
    webhooks: list[str] = field(default_factory=list)
    # HEAD_LATEST -> serve the newest live version; HEAD_PINNED -> head_version.
    head_mode: str = HEAD_LATEST
    head_version: int | None = None
    # ISO 8601 UTC; produced by the caller, never by this module.
    created_at: str = ""
    updated_at: str = ""
    schema: int = SCHEMA_VERSION
    # Guest invitations: named capabilities that let a human without a Keboola
    # account comment on this artifact. One entry per invited person, shaped
    #   {"id": str, "name": str, "secret": <security.hash_password record>,
    #    "created_at": str, "revoked": bool}
    # The *secret itself* is never here — only its PBKDF2 record, exactly like
    # ``password`` — so a leaked meta file cannot be replayed as an invitation.
    # Minting, verifying, capping and revoking are the API layer's job; this
    # module only stores the list and drops entries it could never use.
    # Declared last so every positional argument keeps the meaning it had
    # before 0.7.0.
    invitations: list[dict] = field(default_factory=list)
    # Highest version number ever allocated, kept so a number outlives the
    # version that held it: links, comment anchors and diffs all name versions
    # by number, and the newest one being deleted must not hand its number to
    # whatever is submitted next. Only ever written when the newest version is
    # deleted; 0 (or absent, for every older meta file) means "nothing beyond
    # what exists", which is exactly what max(existing) already gives.
    version_high_water: int = 0
    # SEC-100-006: per-receiver signing-key rotation. Keyed by webhook URL (an
    # entry only exists for a URL currently or formerly in ``webhooks`` that
    # has been rotated at least once); value shaped
    #   {"epoch": str, "previous_epoch": str | None, "rotated_at": str}
    # — see :func:`src.webhooks.mint_key_epoch`. No key *material* is ever
    # stored here, only the random epoch marker src.webhooks.receiver_signing_key
    # mixes into its derivation, so a leaked meta file no more discloses a
    # receiver's key than it already would have via ``webhooks`` + the hub's
    # own secret. Absent (every meta file written before this field existed,
    # and every receiver that has never been rotated) means "derive the
    # original epoch-less key" — the exact behaviour this field's absence
    # already had, so an old-shape persisted receiver keeps verifying without
    # a migration. Declared last, after ``version_high_water``, for the same
    # reason ``invitations`` is: positional ArtifactMeta(...) construction
    # elsewhere keeps meaning what it meant before this field existed.
    webhook_key_epochs: dict[str, dict] = field(default_factory=dict)
    # Does the hub's own reader menu -- the small corner control on
    # /a/{share_id} that tells a reader how to comment, see the history or
    # propose a version -- appear on this artifact's frame page? True for
    # every artifact, including every meta file written before 0.18.0, because
    # :meth:`from_json` reads a missing key as the default its caller passes
    # (True). It is hub chrome, never part of the published bytes: /a/{id}/raw
    # and every export are identical whichever way this is set. Declared last,
    # after ``webhook_key_epochs``, for the same reason that field is declared
    # after ``version_high_water``: positional ArtifactMeta(...) construction
    # elsewhere keeps meaning what it meant before this field existed.
    reader_menu: bool = True

    def __post_init__(self) -> None:
        """Normalize the enum-ish fields so an unknown value can never leak in."""
        if self.accept_versions_mode not in ACCEPT_MODES:
            self.accept_versions_mode = ACCEPT_OFF
        if self.comments_mode not in COMMENTS_MODES:
            self.comments_mode = COMMENTS_ANYONE
        if self.status not in ARTIFACT_STATUSES:
            self.status = ARTIFACT_DRAFT
        if self.restore_status not in ARTIFACT_SETTABLE_STATUSES:
            self.restore_status = ARTIFACT_DRAFT
        # An empty share_id means "the artifact has never been rotated", which
        # is exactly the pre-0.7.0 world where the public URL carried the
        # artifact id. Normalizing here keeps every reader (and to_json) free of
        # the empty case.
        if not isinstance(self.share_id, str) or not self.share_id:
            self.share_id = self.id
        if not isinstance(self.trashed_at, str):
            self.trashed_at = ""
        if self.status != ARTIFACT_TRASHED:
            self.trashed_at = ""
        if isinstance(self.contributors, list):
            self.contributors = [str(key) for key in self.contributors if key]
        else:
            self.contributors = []
        # Same tolerance as ``contributors``: anything that is not a non-empty
        # string is dropped rather than kept as an entry no delivery can use.
        if isinstance(self.webhooks, list):
            self.webhooks = [
                url for url in self.webhooks if isinstance(url, str) and url
            ]
        else:
            self.webhooks = []
        self.webhook_key_epochs = _clean_webhook_key_epochs(self.webhook_key_epochs)
        self.reader_menu = bool(self.reader_menu)
        self.invitations = _clean_invitations(self.invitations)

    @property
    def owner_key(self) -> str:
        """Owner key (stack + project), or "" when the record has no owner."""
        return str(self.owner.get("key") or "")

    def is_final(self) -> bool:
        """True when the artifact is frozen: no new versions, no new comments."""
        return self.status == ARTIFACT_FINAL

    def is_trashed(self) -> bool:
        """True when the artifact is in the trash (soft-deleted, link dead)."""
        return self.status == ARTIFACT_TRASHED

    def is_frozen(self) -> bool:
        """True when no new versions or comments are accepted, for any reason."""
        return self.is_final() or self.is_trashed()

    def allows_versions_from(self, key: str) -> bool:
        """May the project identified by ``key`` submit a version?

        A "final" artifact is frozen for everyone, the owner included — the API
        layer offers the owner a reopen path instead. A "trashed" artifact is
        frozen the same way, until it is restored. Otherwise the owner may
        always contribute, ``anyone`` opens it up to every verified project and
        ``allowlist`` restricts it to :attr:`contributors`.
        """
        if self.is_frozen():
            return False
        if key and key == self.owner_key:
            return True
        if self.accept_versions_mode == ACCEPT_ANYONE:
            return True
        if self.accept_versions_mode == ACCEPT_ALLOWLIST:
            return bool(key) and key in self.contributors
        return False

    def allows_comments_from(self, key: str) -> bool:
        """May the project identified by ``key`` comment? Same shape as versions."""
        if self.is_frozen():
            return False
        if key and key == self.owner_key:
            return True
        if self.comments_mode == COMMENTS_ANYONE:
            return True
        if self.comments_mode == COMMENTS_ALLOWLIST:
            return bool(key) and key in self.contributors
        return False

    def to_json(self) -> bytes:
        """Serialize the meta record to UTF-8 JSON bytes.

        Both ``accept_versions`` (legacy boolean) and ``accept_versions_mode``
        are written, so a rollback to a phase-2 build still reads the file.
        """
        payload = {
            "id": self.id,
            "owner": self.owner,
            "password": self.password,
            "accept_versions": self.accept_versions,
            "accept_versions_mode": self.accept_versions_mode,
            "contributors": self.contributors,
            "comments_mode": self.comments_mode,
            "status": self.status,
            "share_id": self.share_id,
            "trashed_at": self.trashed_at,
            "restore_status": self.restore_status,
            "webhooks": self.webhooks,
            "head_mode": self.head_mode,
            "head_version": self.head_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "schema": self.schema,
            "invitations": self.invitations,
            "version_high_water": self.version_high_water,
            "webhook_key_epochs": self.webhook_key_epochs,
            "reader_menu": self.reader_menu,
        }
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes) -> "ArtifactMeta":
        """Parse meta JSON tolerantly (unknown keys ignored, defaults applied).

        ``accept_versions_mode`` wins when it names a known mode; a file written
        before phase 3 has only the boolean ``accept_versions`` and maps to
        ``anyone``/``off``.

        Raises ``ValueError`` when the payload is not a JSON object or carries
        no usable artifact ID — callers treat that as a corrupt record.
        """
        data = _load_object(raw, "meta")

        artifact_id = data.get("id")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise ValueError("meta has no usable 'id'")

        owner = data.get("owner")
        password = data.get("password")
        head_mode = data.get("head_mode")
        head_version = data.get("head_version")
        schema = data.get("schema")

        raw_mode = data.get("accept_versions_mode")
        if raw_mode in ACCEPT_MODES:
            accept_mode = str(raw_mode)
        else:
            accept_mode = ACCEPT_ANYONE if data.get("accept_versions") else ACCEPT_OFF

        contributors = data.get("contributors")
        comments_mode = data.get("comments_mode")
        status = data.get("status")

        # A meta file written before 0.7.0 has no share_id at all; its public
        # URL was the artifact id, so that is what it keeps. __post_init__
        # applies the same fallback for an empty or non-string value.
        share_id = data.get("share_id")
        if not isinstance(share_id, str) or not share_id:
            share_id = artifact_id
        trashed_at = data.get("trashed_at")
        restore_status = data.get("restore_status")
        # Missing (every meta file written before 0.7.0) or non-list values
        # become an empty list; non-string entries inside a list are dropped by
        # __post_init__.
        webhooks = data.get("webhooks")
        # Same tolerance again: missing (every meta file written before guest
        # invitations existed) or unusable becomes an empty list, and
        # __post_init__ drops individual entries it cannot make sense of.
        invitations = data.get("invitations")

        return cls(
            id=artifact_id,
            owner=owner if isinstance(owner, dict) else {},
            password=password if isinstance(password, dict) else None,
            accept_versions_mode=accept_mode,
            contributors=(
                [str(key) for key in contributors if key]
                if isinstance(contributors, list)
                else []
            ),
            comments_mode=(
                comments_mode if comments_mode in COMMENTS_MODES else COMMENTS_ANYONE
            ),
            status=status if status in ARTIFACT_STATUSES else ARTIFACT_DRAFT,
            share_id=share_id,
            trashed_at=trashed_at if isinstance(trashed_at, str) else "",
            restore_status=(
                restore_status
                if restore_status in ARTIFACT_SETTABLE_STATUSES
                else ARTIFACT_DRAFT
            ),
            webhooks=webhooks if isinstance(webhooks, list) else [],
            head_mode=head_mode if head_mode in _HEAD_MODES else HEAD_LATEST,
            head_version=(
                head_version
                if isinstance(head_version, int) and not isinstance(head_version, bool)
                else None
            ),
            created_at=data.get("created_at") or "",
            updated_at=data.get("updated_at") or "",
            schema=schema if isinstance(schema, int) else SCHEMA_VERSION,
            invitations=invitations if isinstance(invitations, list) else [],
            version_high_water=(
                high_water
                if isinstance(high_water := data.get("version_high_water"), int)
                and not isinstance(high_water, bool)
                and high_water > 0
                else 0
            ),
            # SEC-100-006: absent (every meta file written before this field
            # existed) becomes an empty map in __post_init__, which is exactly
            # the "never rotated" state — the receiver keeps verifying under
            # its original epoch-less key with no migration needed.
            webhook_key_epochs=data.get("webhook_key_epochs"),
            # Absent in every meta file written before 0.18.0, and that has to
            # mean "on": the menu is the answer to "how do I comment on this?",
            # and an artifact published last month deserves it as much as one
            # published today. __post_init__ coerces whatever is here to bool.
            reader_menu=data.get("reader_menu", True),
        )


def _accept_versions_get(self: ArtifactMeta) -> bool:
    """``True`` iff someone other than the owner may submit versions."""
    return self.accept_versions_mode in _ACCEPT_ON_MODES


def _accept_versions_set(self: ArtifactMeta, value: bool | None) -> None:
    """Flip the mode on/off, preserving ``allowlist`` when it already agrees.

    ``None`` means "not specified" — used by the generated ``__init__`` when a
    caller passes only ``accept_versions_mode``.
    """
    if value is None:
        return
    wanted = bool(value)
    if wanted == (self.accept_versions_mode in _ACCEPT_ON_MODES):
        return
    self.accept_versions_mode = ACCEPT_ANYONE if wanted else ACCEPT_OFF


# Attached post-hoc: a dataclass field and a property cannot share a name, but a
# property installed after the fact is what the generated ``__init__`` assigns
# through, so ``ArtifactMeta(accept_versions=True)`` and
# ``meta.accept_versions = False`` both keep the mode in sync.
ArtifactMeta.accept_versions = property(  # type: ignore[assignment]
    _accept_versions_get, _accept_versions_set
)


@dataclass
class Envelope:
    """One *version* of an artifact, stored as JSON in Storage Files."""

    id: str
    version: int
    title: str
    html: str
    # One of _SOURCE_TYPES.
    source_type: str
    # Original input: {"markdown": "..."} / {"git": {...}} / {} for raw HTML.
    source: dict = field(default_factory=dict)
    # The verified submitter of this version:
    # {"stack_url": str, "project_id": int, "project_name": str, "key": str}
    author: dict = field(default_factory=dict)
    # STATUS_LIVE or STATUS_PROPOSED.
    status: str = STATUS_LIVE
    # Free-form contributor note ("what changed").
    note: str | None = None
    # The version this submission was written against, as reported by the
    # submitter. Purely informational at the storage layer: deciding whether a
    # proposal is "outdated" (head moved on since ``base_version``) is the API
    # layer's job. ``None`` means the submitter did not say.
    base_version: int | None = None
    # File ID of the canonical copy in the *author's* project (may be absent).
    canonical_file_id: int | None = None
    # ISO 8601 UTC; produced by the caller, never by this module.
    created_at: str = ""
    schema: int = SCHEMA_VERSION
    # Design-system provenance the submitter claimed for this version:
    # {"id": "ds_...", "slug": str, "version": int}. Declared last and
    # optional so positional construction elsewhere keeps working. It is a
    # claim of what was used, never a proof of conformity, and it is never
    # rewritten when the design system itself changes or is deleted.
    design_system: dict | None = None

    @property
    def author_key(self) -> str:
        """Author key (stack + project), or "" when the record has no author."""
        return str(self.author.get("key") or "")

    def to_json(self) -> bytes:
        """Serialize the version envelope to UTF-8 JSON bytes."""
        payload = {
            "id": self.id,
            "version": self.version,
            "title": self.title,
            "html": self.html,
            "source_type": self.source_type,
            "source": self.source,
            "author": self.author,
            "status": self.status,
            "note": self.note,
            "base_version": self.base_version,
            "canonical_file_id": self.canonical_file_id,
            "created_at": self.created_at,
            "schema": self.schema,
            "design_system": self.design_system,
        }
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes) -> "Envelope":
        """Parse version-envelope JSON, accepting legacy schema-1 payloads.

        Unknown keys are ignored and missing optional fields fall back to their
        defaults, so envelopes written by older/newer versions still load. In a
        schema-1 payload the submitter lived under ``owner`` (there was no
        distinction between owner and author yet) and there was no ``status``;
        such a record loads as a live version authored by its owner. The
        schema-1 ``password`` and ``updated_at`` keys are artifact-level state
        and are ignored here — see :func:`migrate_legacy`.

        Raises ``ValueError`` when the payload is not a JSON object or carries
        no usable artifact ID.
        """
        data = _load_object(raw, "envelope")

        artifact_id = data.get("id")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise ValueError("envelope has no usable 'id'")

        # ``title`` and ``html`` must be strings. A truthy non-string (e.g.
        # ``html={"x": 1}`` from a schema-drifting or tampered record) would
        # otherwise survive into the typed Envelope and blow up later at
        # ``html.encode()``; reject it here so callers quarantine the record as
        # corrupt (same ``ValueError`` path as a bad ``id``).
        title = data.get("title")
        if title is None:
            title = ""
        elif not isinstance(title, str):
            raise ValueError("envelope 'title' is not a string")
        html = data.get("html")
        if html is None:
            html = ""
        elif not isinstance(html, str):
            raise ValueError("envelope 'html' is not a string")

        source = data.get("source")
        author = data.get("author")
        if not isinstance(author, dict):
            legacy_owner = data.get("owner")
            author = legacy_owner if isinstance(legacy_owner, dict) else {}
        source_type = data.get("source_type")
        # Normalize an unknown/non-string source_type to the neutral "html"
        # (matches the status fallback below rather than raising).
        if source_type not in _SOURCE_TYPES:
            source_type = "html"
        status = data.get("status")
        note = data.get("note")
        base_version = data.get("base_version")
        canonical_file_id = data.get("canonical_file_id")
        version = data.get("version")
        schema = data.get("schema")

        return cls(
            id=artifact_id,
            version=(
                version
                if isinstance(version, int)
                and not isinstance(version, bool)
                and version > 0
                else 1
            ),
            title=title,
            html=html,
            source_type=source_type,
            source=source if isinstance(source, dict) else {},
            author=author,
            status=status if status in _STATUSES else STATUS_LIVE,
            note=note if isinstance(note, str) and note else None,
            base_version=(
                base_version
                if isinstance(base_version, int)
                and not isinstance(base_version, bool)
                and base_version > 0
                else None
            ),
            canonical_file_id=(
                canonical_file_id
                if isinstance(canonical_file_id, int)
                and not isinstance(canonical_file_id, bool)
                else None
            ),
            created_at=data.get("created_at") or "",
            schema=schema if isinstance(schema, int) else 1,
            # Tolerant: absent on every pre-0.16 envelope, and anything that
            # is not an object degrades to "no provenance claimed".
            design_system=(
                ds if isinstance(ds := data.get("design_system"), dict) else None
            ),
        )

    def public_meta(self, is_head: bool = False) -> dict:
        """Version metadata safe to expose to any capability-URL holder.

        Only the author's project identity and stack *hostname* are exposed —
        never a token, a full stack URL or the password record.
        """
        git = self.source.get("git") if self.source_type.startswith("git") else None
        return {
            "version": self.version,
            "title": self.title,
            "status": self.status,
            "note": self.note,
            "base_version": self.base_version,
            "created_at": self.created_at,
            "is_head": bool(is_head),
            "size_bytes": len(self.html.encode("utf-8")),
            "source_type": self.source_type,
            "author": {
                "project_id": self.author.get("project_id"),
                "project_name": self.author.get("project_name"),
                "stack_host": _stack_host(str(self.author.get("stack_url") or "")),
            },
            "git": git,
            "design_system": self.design_system,
        }


def _load_object(raw: bytes, what: str) -> dict:
    """Decode UTF-8 JSON that must be an object, or raise ``ValueError``."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{what} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{what} JSON is not an object")
    return data


def migrate_legacy(raw: bytes) -> tuple[Envelope, ArtifactMeta]:
    """Split a legacy schema-1 envelope into (version 1 envelope, meta record).

    Nothing is written: the caller decides whether to materialize the meta file.
    """
    envelope = Envelope.from_json(raw)
    envelope.version = 1
    envelope.status = STATUS_LIVE

    data = _load_object(raw, "envelope")
    owner = data.get("owner")
    password = data.get("password")
    meta = ArtifactMeta(
        id=envelope.id,
        owner=owner if isinstance(owner, dict) else dict(envelope.author),
        password=password if isinstance(password, dict) else None,
        accept_versions_mode=ACCEPT_OFF,
        head_mode=HEAD_LATEST,
        head_version=None,
        created_at=envelope.created_at,
        updated_at=data.get("updated_at") or envelope.created_at,
        schema=SCHEMA_VERSION,
    )
    return envelope, meta


@dataclass
class _VerEntry:
    """Where one version file lives, plus its status once we have loaded it.

    ``status`` is not derivable from tags, so it stays ``None`` until the
    envelope has been read at least once (cheap thanks to the LRU).

    ``size_bytes`` is the Storage-reported size (0 when unknown); it lets the
    store skip an oversized record *before* downloading it.
    """

    file_id: int
    status: str | None = None
    size_bytes: int = 0


@dataclass
class _ArtEntry:
    """Every host-project file belonging to one artifact."""

    meta_file_id: int | None = None
    meta_size_bytes: int = 0
    # A schema-1 ``artifact-{id}.json`` envelope, if the artifact predates v2.
    legacy_file_id: int | None = None
    legacy_size_bytes: int = 0
    versions: dict[int, _VerEntry] = field(default_factory=dict)
    # Version numbers currently reserved by an in-flight ``add_version_next``
    # allocation. Kept out of ``versions`` (and out of reads) so a concurrent
    # allocation picks a distinct number without a half-written slot ever being
    # served. See :meth:`ArtifactStore.add_version_next`.
    reserving: set[int] = field(default_factory=set)
    # Highest version number ever allocated for this artifact, including ones
    # since deleted. Seeded from ``ArtifactMeta.version_high_water`` when the
    # meta record is loaded or saved, raised in memory on every allocation, and
    # persisted (via the meta record) when the newest version is deleted --
    # the one event that can make ``max(existing) + 1`` hand an old number to
    # new content. See :meth:`ArtifactStore.delete_version`.
    high_water: int = 0

    def version_numbers(self) -> list[int]:
        """All known version numbers, ascending; a legacy file counts as v1.

        Reserved-but-not-yet-written numbers are deliberately excluded: reads
        must never see an allocation that has not committed a file yet.
        """
        numbers = set(self.versions)
        if self.legacy_file_id is not None:
            numbers.add(1)
        return sorted(numbers)

    def next_free_number(self) -> int:
        """Lowest unused version number, counting reservations and legacy v1."""
        numbers = set(self.versions) | self.reserving | {self.high_water}
        if self.legacy_file_id is not None:
            numbers.add(1)
        return max(numbers) + 1 if max(numbers) > 0 else 1


class ArtifactStore:
    """Versioned serving store backed by Storage Files, with memory + disk caches.

    Thread safety: the index and the in-process LRUs are guarded by a lock;
    backend calls happen outside the lock so slow Storage requests never block
    other requests.
    """

    def __init__(
        self,
        backend: FilesBackend,
        cache_dir: Path,
        cache_max_entries: int,
        max_versions: int,
        max_envelope_bytes: int = 20 * 1024 * 1024,
        max_proposed_versions: int = 50,
        reap_aborted_after_s: int = 0,
        negative_lookup_cache_entries: int = 4096,
    ) -> None:
        self._backend = backend
        self._cache_dir = Path(cache_dir)
        self._cache_max_entries = max(0, int(cache_max_entries))
        self._max_versions = max(1, int(max_versions))
        # <= 0 means "no bound". Guards against a single oversized persisted
        # record exhausting worker memory on download/decode.
        self._max_envelope_bytes = int(max_envelope_bytes)
        # <= 0 means "no cap".
        self._max_proposed_versions = max(0, int(max_proposed_versions))
        # Seconds a meta record with no version may exist before hydrate
        # treats it as an aborted publish and deletes it; 0 disables.
        self._reap_aborted_after_s = max(0, int(reap_aborted_after_s))
        self._index: dict[str, _ArtEntry] = {}
        # Public share id -> artifact id, and its inverse. Share ids live inside
        # the meta *payload*, not in a Storage tag, so hydrate() cannot fill
        # these; they are learned lazily (every meta this store loads teaches it
        # one mapping) and are authoritative only as a cache — every hit is
        # re-checked against the meta record before it is trusted.
        self._share_index: dict[str, str] = {}
        self._share_of: dict[str, str] = {}
        # SEC-100-002: ids a Storage tag search has confirmed do not exist,
        # newest last so the bound evicts least-recently-used. Since target
        # resolution now runs *before* authentication (see
        # ``_serialized_per_comment_target`` in src/main.py), an anonymous
        # caller can otherwise turn a stream of well-formed made-up ids into
        # one Storage round trip each; remembering the miss makes a repeat
        # free. Membership is only ever a shortcut to "not found", so a stale
        # entry could only ever hide an artifact that appeared behind our
        # back -- which the single-writer invariant (CLAUDE.md, "Exactly one
        # instance, ever") does not allow: this process is the only writer, it
        # writes through this store, and every path that can make an id
        # resolvable drops the entry. A read racing a create in *this* process
        # is not a concern either, since the create's invalidation lands under
        # the same lock before the new artifact is reachable at all.
        self._negative_max_entries = max(0, int(negative_lookup_cache_entries))
        self._absent: OrderedDict[str, None] = OrderedDict()
        self._memory: OrderedDict[tuple[str, int], Envelope] = OrderedDict()
        self._meta_memory: OrderedDict[tuple[str, int], ArtifactMeta] = OrderedDict()
        self._lock = threading.Lock()
        # Bumped by every write method below, read by the live-poll ETag.
        # See :class:`RevisionLedger` (REL-100-003).
        self._revisions = RevisionLedger()
        try:
            _harden_cache_dir(self._cache_dir)
        except OSError as exc:  # Disk cache is optional — degrade gracefully.
            logger.warning("Cannot create cache dir %s: %s", self._cache_dir, exc)

    # ---------------------------------------------------------------- index

    def hydrate(self) -> int:
        """Rebuild the in-memory index from Storage. Returns artifact count.

        When several files describe the same thing (an interrupted write left an
        older file behind) the highest file ID — the newest upload — wins.
        """
        files = self._backend.search_by_tag(TAG_ALL)
        index: dict[str, _ArtEntry] = {}
        for info in files:
            artifact_id = _tag_value(info.tags, _TAG_ID_PREFIX)
            if not artifact_id:
                logger.warning(
                    "Skipping file %s (%s): no %s* tag",
                    info.id,
                    info.name,
                    _TAG_ID_PREFIX,
                )
                continue
            entry = index.setdefault(artifact_id, _ArtEntry())
            _absorb_file(entry, info)

        self._reap_aborted_publishes(index, {info.id: info for info in files})

        with self._lock:
            self._index = index
            # Share ids are not derivable from tags, so a full rebuild starts
            # from an empty share cache and re-learns lazily (resolve_share).
            self._share_index = {}
            self._share_of = {}
            # A rebuild is the moment Storage's own state becomes
            # authoritative again, so every remembered absence is discarded
            # rather than reconciled: some of them may be in the index we just
            # built (SEC-100-002).
            self._absent.clear()
        logger.info("Hydrated index with %d artifact(s)", len(index))
        return len(index)

    def _reap_aborted_publishes(
        self, index: dict[str, _ArtEntry], files: dict[int, FileInfo]
    ) -> None:
        """Delete meta records that have no version and are too old to be in flight.

        Two aborted operations leave this shape, and both want the same
        treatment.

        Publish writes the meta record first, so that a failed canonical
        upload can be rolled back by deleting it; if the process dies between
        that write and the version write, the record stays. It is inert --
        every read answers 404 for it, because there is no head -- but it is
        also invisible to every API and accumulates forever.

        Since REL-100-001, :meth:`delete` produces it too: a purge deletes
        every child before the authorizing meta record, so a process death in
        that last gap leaves a meta whose versions are all gone. **The
        deliberate choice is to keep such a record rather than treat it as
        garbage the moment it is seen**, because it is exactly what makes the
        purge finishable: the meta is the file ``_owner_only`` authorizes
        against, so while it exists the owner can retry and complete the
        erasure, and there is no content left for it to expose in the
        meantime. It shows up in the owner's listing as an artifact with zero
        versions -- visible, empty, and removable -- and never publicly.

        Age is the whole safety argument for eventually reaping it. Right
        after a restart this very shape is also what a publish looks like
        between its two writes, so only a record older than
        ``reap_aborted_after_s`` is touched -- an hour by default, against a
        request that takes seconds. A timestamp that cannot be read is treated
        as young. Runs only from :meth:`hydrate`, never from the lazy
        per-artifact fallback.
        """
        if self._reap_aborted_after_s <= 0:
            return
        for artifact_id, entry in list(index.items()):
            if entry.meta_file_id is None or entry.versions or entry.legacy_file_id is not None:
                continue
            info = files.get(entry.meta_file_id)
            age = _age_seconds(info.created) if info is not None else None
            if age is None or age < self._reap_aborted_after_s:
                logger.info(
                    "Artifact %s has a meta record but no version; leaving it "
                    "(age %s s, reaped after %d s)",
                    artifact_id, "unknown" if age is None else int(age), self._reap_aborted_after_s,
                )
                continue
            try:
                self._backend.delete(entry.meta_file_id)
            except Exception as exc:  # noqa: BLE001 - reaping is best effort
                logger.warning(
                    "Could not reap the aborted publish %s (meta file %s): %s",
                    artifact_id, entry.meta_file_id, exc,
                )
                continue
            del index[artifact_id]
            logger.warning(
                "Reaped aborted publish %s: meta file %s had no version for %d s",
                artifact_id, entry.meta_file_id, int(age),
            )

    def count(self) -> int:
        """Number of artifacts currently indexed."""
        with self._lock:
            return len(self._index)

    def revision(self, artifact_id: str) -> str:
        """Opaque token that changes whenever this artifact is mutated.

        The cheap half of the live poll: see :class:`RevisionLedger` for why
        an in-memory number is exact under this service's single-writer
        deployment, and why it carries a per-process boot nonce.
        """
        return self._revisions.token(artifact_id)

    def _resolve(self, artifact_id: str) -> _ArtEntry | None:
        """Index lookup with a Storage fallback for artifacts we never saw.

        The startup index can be incomplete -- degraded-mode startup (a failed
        hydration) serves with an empty index and discovers artifacts lazily --
        so an index miss is re-checked against Storage before we answer "not
        found", rather than trusting a startup snapshot that may not have run.

        SEC-100-002: that re-check used to run on *every* miss, including the
        made-up ids an unauthenticated caller supplies by the thousand, so a
        confirmed absence is now remembered in a bounded LRU and answered from
        memory. Nothing but a write of this process's own can make an absent
        id appear (single writer, CLAUDE.md), and every such write invalidates
        the entry, so a read that legitimately races a just-created artifact
        cannot be served a stale "missing".
        """
        with self._lock:
            entry = self._index.get(artifact_id)
            if entry is None and artifact_id in self._absent:
                self._absent.move_to_end(artifact_id)
                return None
        if entry is not None:
            return entry

        files = self._backend.search_by_tag(tag_for_id(artifact_id))
        if not files:
            with self._lock:
                self._remember_absent_locked(artifact_id)
            return None
        fresh = _ArtEntry()
        for info in files:
            _absorb_file(fresh, info)

        with self._lock:
            self._forget_absent_locked(artifact_id)
            current = self._index.get(artifact_id)
            if current is None:
                self._index[artifact_id] = fresh
                return fresh
            return current

    def negative_cache_size(self) -> int:
        """How many confirmed-absent ids are remembered right now.

        Exposed so the bound can be asserted on without reaching into the
        store's internals; nothing in the service reads it.
        """
        with self._lock:
            return len(self._absent)

    def _remember_absent_locked(self, artifact_id: str) -> None:
        """Record that Storage has no file for ``artifact_id``. Holds the lock."""
        if self._negative_max_entries <= 0:
            return
        self._absent[artifact_id] = None
        self._absent.move_to_end(artifact_id)
        while len(self._absent) > self._negative_max_entries:
            self._absent.popitem(last=False)

    def _forget_absent_locked(self, artifact_id: str) -> None:
        """Drop a remembered absence. Caller holds the lock.

        Called from every path that can make an id resolvable, which is what
        keeps the cache from ever answering "missing" about something that
        exists: creation and version writes (``save_meta``, ``add_version``,
        ``add_version_next``), a share id being learned or rotated
        (``_learn_share_locked``), a full rebuild (``hydrate``) and an
        explicit per-artifact re-read (``refresh``).
        """
        self._absent.pop(artifact_id, None)

    def refresh(self, artifact_id: str) -> bool:
        """Re-read one artifact's files from Storage, replacing its index entry.

        The startup index is built once and never expires, so it can lag
        behind whatever this process itself most recently wrote through a path
        that did not update it, or behind a Storage tag rebuild after a
        degraded-mode startup. ``refresh`` re-runs the artifact's tag search
        and swaps in a fresh entry (dropping this artifact's in-memory LRU so
        the next read reloads), letting a caller opt in to Storage's current
        state on demand. Returns ``True`` when the artifact still exists.

        Residual limitation: this is an explicit, per-artifact catch-up, not a
        subscription -- nothing calls it automatically, so a reader that never
        passes ``fresh=True`` only ever sees what the in-memory index already
        holds. Under the exactly-one-process invariant (CLAUDE.md, "Exactly
        one instance, ever") that index is always this process's own most
        recent write; ``fresh=True`` exists as defence in depth and to bypass
        the LRU, not to reconcile with a second writer, which this deployment
        does not allow to exist.
        """
        files = self._backend.search_by_tag(tag_for_id(artifact_id))
        if not files:
            with self._lock:
                self._index.pop(artifact_id, None)
                self._forget_memory_locked(artifact_id)
                self._forget_meta_locked(artifact_id)
                self._forget_share_locked(artifact_id)
                # Storage has just confirmed the absence, so record it the
                # same way a plain lookup miss would (SEC-100-002).
                self._remember_absent_locked(artifact_id)
            return False
        rebuilt = _ArtEntry()
        for info in files:
            _absorb_file(rebuilt, info)
        with self._lock:
            self._index[artifact_id] = rebuilt
            self._forget_absent_locked(artifact_id)
            self._forget_memory_locked(artifact_id)
            self._forget_meta_locked(artifact_id)
            # The refreshed meta file may carry a share id rotated since we
            # last learned it; drop what we knew and re-learn it on resolve.
            self._forget_share_locked(artifact_id)
        return True

    # ----------------------------------------------------------------- meta

    def get_meta(self, artifact_id: str) -> ArtifactMeta | None:
        """Artifact-level state, synthesized for legacy artifacts, else None."""
        entry = self._resolve(artifact_id)
        if entry is None:
            return None
        if entry.meta_file_id is not None:
            meta = self._load_meta(
                artifact_id,
                entry.meta_file_id,
                size_hint=entry.meta_size_bytes or None,
            )
            if meta is not None:
                return meta
        if entry.legacy_file_id is not None:
            migrated = self._load_legacy(
                artifact_id,
                entry.legacy_file_id,
                size_hint=entry.legacy_size_bytes or None,
            )
            if migrated is not None:
                return migrated[1]
        return None

    def save_meta(self, meta: ArtifactMeta) -> None:
        """Upload the meta file, retire older meta files and refresh caches."""
        artifact_id = meta.id
        owner_key = meta.owner_key
        if not owner_key:
            logger.warning("Saving meta of artifact %s without an owner key", artifact_id)
        raw = meta.to_json()
        tags = [TAG_ALL, tag_for_id(artifact_id), TAG_META, tag_for_owner(owner_key)]

        file_id = self._backend.upload(f"artifact-{artifact_id}-meta.json", raw, tags)

        with self._lock:
            entry = self._index.setdefault(artifact_id, _ArtEntry())
            self._forget_absent_locked(artifact_id)
            previous = entry.meta_file_id
            entry.meta_file_id = file_id
            self._forget_meta_locked(artifact_id)
            self._remember_meta_locked(artifact_id, file_id, meta)
            self._seed_high_water_locked(artifact_id, meta)
            # Keep the share index in step with what we just persisted; a
            # rotation drops the previous share id here (that revocation is the
            # whole point of rotating).
            self._learn_share_locked(artifact_id, meta.share_id)
        self._write_disk_cache(artifact_id, file_id, raw)
        if previous is not None and previous != file_id:
            self._delete_disk_cache(artifact_id, previous)
        self._delete_stale_meta_files(artifact_id, keep_file_id=file_id)
        # Every artifact-level change -- settings, password, head pin, status,
        # trash/restore, link rotation, invitations -- lands here, so this one
        # bump is what makes all of them visible to a live poll (REL-100-003).
        self._revisions.bump(artifact_id)

    def owner_key_of(self, artifact_id: str) -> str | None:
        """Owner key of an artifact, or None when the artifact is unknown."""
        meta = self.get_meta(artifact_id)
        return meta.owner_key if meta is not None else None

    # ------------------------------------------------------------ share ids

    def _learn_share_locked(self, artifact_id: str, share_id: str) -> None:
        """Record ``share_id`` as this artifact's public id. Caller holds lock."""
        if not share_id:
            share_id = artifact_id
        previous = self._share_of.get(artifact_id)
        if previous is not None and previous != share_id:
            if self._share_index.get(previous) == artifact_id:
                self._share_index.pop(previous, None)
        self._share_of[artifact_id] = share_id
        self._share_index[share_id] = artifact_id
        # A rotation mints an id an anonymous caller may already have probed
        # and had recorded as absent; both halves of the identity pair are
        # live from here on (SEC-100-002).
        self._forget_absent_locked(artifact_id)
        self._forget_absent_locked(share_id)

    def _forget_share_locked(self, artifact_id: str) -> None:
        """Drop everything we know about an artifact's share id. Holds lock."""
        previous = self._share_of.pop(artifact_id, None)
        if previous is not None and self._share_index.get(previous) == artifact_id:
            self._share_index.pop(previous, None)

    def _learn_share(self, artifact_id: str, share_id: str) -> None:
        with self._lock:
            self._learn_share_locked(artifact_id, share_id)

    def _share_hit(self, artifact_id: str, wanted: str) -> str | None:
        """Confirm ``wanted`` is still ``artifact_id``'s live public share id.

        Returns the artifact id on a confirmed, servable hit and ``None``
        otherwise — the mapping was stale (rotated elsewhere) or the artifact is
        in the trash, in which case its public link is dead by definition. Every
        meta actually loaded here also refreshes the share cache.
        """
        meta = self.get_meta(artifact_id)
        if meta is None:
            with self._lock:
                self._forget_share_locked(artifact_id)
            return None
        self._learn_share(artifact_id, meta.share_id)
        if meta.share_id != wanted or meta.is_trashed():
            return None
        return artifact_id

    def resolve_share(self, share_or_artifact_id: str) -> str | None:
        """Map a public ``/a/{...}`` identifier to an internal artifact id.

        Accepts the share id an artifact currently publishes — which, until the
        owner rotates the link, *is* its artifact id. Returns ``None`` for
        anything that must not resolve publicly:

        * a share id that has been rotated away (revoked),
        * the bare artifact id of an artifact whose share id has moved on — the
          internal id stops working as a public handle the moment they differ,
          and stays valid only for owner ``/api/*`` calls,
        * an artifact currently in the trash, and
        * an identifier that names nothing at all.

        Lookup order, cheapest first: the share cache, then the in-memory index
        (is this an artifact id?), then a scan of indexed artifacts whose share
        id we have not learned yet (their metas are loaded once and cached), and
        finally a Storage tag search for an artifact this process has not
        indexed yet. Every share id learned along the way is cached, so a warm
        index answers in O(1).
        """
        wanted = share_or_artifact_id
        if not wanted:
            return None

        with self._lock:
            cached = self._share_index.get(wanted)
        if cached is not None:
            meta = self.get_meta(cached)
            if meta is None:
                with self._lock:
                    self._forget_share_locked(cached)
            else:
                self._learn_share(cached, meta.share_id)
                if meta.share_id == wanted:
                    # The mapping still holds, so this is the final answer:
                    # servable, unless the artifact sits in the trash.
                    return None if meta.is_trashed() else cached
                # Rotated away since this process last learned it. ``wanted``
                # is revoked for this artifact; keep looking on the slower
                # paths in case it now belongs to a different one.

        with self._lock:
            known_artifact = wanted in self._index
        if known_artifact:
            # An artifact id whose meta names a *different* share id has been
            # rotated away: it is no longer a public handle. It cannot be some
            # other artifact's share id either — share ids are minted from the
            # same unguessable alphabet, so collisions do not happen.
            return self._share_hit(wanted, wanted)

        found = self._scan_for_share(wanted)
        if found is not None:
            return found

        # Not yet indexed by this process: fall back to a Storage tag search,
        # which is what every other read path does on an index miss.
        if self._resolve(wanted) is None:
            return None
        return self._share_hit(wanted, wanted)

    def resolve_lock_target(self, path_id: str) -> str | None:
        """Map either half of an artifact's identity pair to its internal id.

        What the comment routes need in order to take the right lock: a live
        share id resolves to the artifact behind it, and so does the bare
        internal id of an artifact whose public link has been rotated away or
        which sits in the trash -- both are still real artifacts whose
        mutations have to serialize against their owner's routes, even though
        neither is publicly servable. Anything naming no artifact at all
        answers ``None``.

        SEC-100-002: the caller used to get this by asking twice
        (``resolve_share`` and then ``get_meta``), which cost an unauthenticated
        caller two Storage tag searches per made-up id. Resolving both halves in
        one pass costs one -- and, for a repeat of the same id, none, since the
        confirmed absence is remembered (see :meth:`_resolve`).
        """
        if not path_id:
            return None
        internal_id = self.resolve_share(path_id)
        if internal_id is not None:
            return internal_id
        # ``resolve_share`` has already re-checked Storage for an id this
        # process has not indexed, so this second half reads the index (or the
        # negative cache) and never repeats the tag search.
        if self._resolve(path_id) is None:
            return None
        return path_id if self.get_meta(path_id) is not None else None

    def _scan_for_share(self, wanted: str) -> str | None:
        """Load the metas whose share id we do not know yet, looking for one.

        Every meta loaded teaches the share cache its mapping, so the scan pays
        for itself: a later lookup of any share id it saw is a dict hit. Metas
        are loaded outside the lock; only the cache updates take it. Returns
        ``None`` both when nothing matches and when the match is trashed (a
        trashed artifact has no live public link).
        """
        with self._lock:
            candidates = [
                artifact_id
                for artifact_id in self._index
                if artifact_id not in self._share_of
            ]
        for artifact_id in candidates:
            try:
                meta = self.get_meta(artifact_id)
            except BackendError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad record must not
                # break share resolution for everybody else.
                logger.warning(
                    "Cannot load meta of artifact %s while resolving a share "
                    "id: %s",
                    artifact_id,
                    exc,
                )
                continue
            if meta is None:
                continue
            self._learn_share(artifact_id, meta.share_id)
            if meta.share_id == wanted:
                return None if meta.is_trashed() else artifact_id
        return None

    def rotate_share(
        self,
        artifact_id: str,
        generate: Callable[[], str] | None = None,
        when_iso: str = "",
    ) -> str | None:
        """Mint a new public share id for an artifact and revoke the old link.

        After this, the previous share id stops resolving — and so does the bare
        artifact id, once it differs from the share id. That revocation is the
        entire point: a link handed to the wrong person can be taken back.

        ``generate`` defaults to :func:`src.security.new_artifact_id` (imported
        lazily so this module keeps no import-time dependency on it); tests pass
        their own to make rotation deterministic. ``when_iso``, when given,
        becomes the meta record's new ``updated_at``.

        Returns the new share id, or ``None`` when the artifact is unknown.
        """
        meta = self.get_meta(artifact_id)
        if meta is None:
            return None
        if generate is None:
            from src.security import new_artifact_id

            generate = new_artifact_id
        new_share = str(generate())
        if not new_share:
            raise ValueError("share id generator produced an empty id")

        previous = meta.share_id
        with self._lock:
            if previous and self._share_index.get(previous) == artifact_id:
                self._share_index.pop(previous, None)
            self._share_of.pop(artifact_id, None)
        # save_meta records the new mapping (and drops any residual old one).
        self.save_meta(
            replace(
                meta,
                share_id=new_share,
                updated_at=when_iso or meta.updated_at,
            )
        )
        logger.info("Rotated the share link of artifact %s", artifact_id)
        return new_share

    # ------------------------------------------------------------- versions

    def create(self, meta: ArtifactMeta, first: Envelope) -> None:
        """Publish a brand-new artifact: its meta record plus its first version."""
        self.save_meta(meta)
        self.add_version(first)

    def add_version(self, env: Envelope) -> None:
        """Upload one version file, index it and apply the retention policy.

        The caller supplies ``env.version``; concurrent submissions that both
        allocated the same number via :meth:`next_version` will collide (each
        overwrites the other's file). Use :meth:`add_version_next` to allocate
        and write atomically under the store lock instead.
        """
        artifact_id = env.id
        version = env.version
        raw = env.to_json()
        tags = [TAG_ALL, tag_for_id(artifact_id), tag_for_version(version)]

        # Upload first: a failure here leaves the index untouched.
        file_id = self._backend.upload(
            f"artifact-{artifact_id}-v{version}.json", raw, tags
        )

        with self._lock:
            entry = self._index.setdefault(artifact_id, _ArtEntry())
            self._forget_absent_locked(artifact_id)
            previous = entry.versions.get(version)
            entry.versions[version] = _VerEntry(
                file_id=file_id, status=env.status, size_bytes=len(raw)
            )
            if previous is not None:
                self._memory.pop((artifact_id, previous.file_id), None)
            self._remember_locked(artifact_id, file_id, env)
        self._write_disk_cache(artifact_id, file_id, raw)
        if previous is not None and previous.file_id != file_id:
            self._delete_file(artifact_id, previous.file_id)
        self._prune_versions(artifact_id)
        self._prune_proposals(artifact_id)
        # Also the choke point for set_status (promote/withdraw rewrite the
        # version file through here), so a live poll sees those too.
        self._revisions.bump(artifact_id)

    def add_version_next(self, env: Envelope) -> int:
        """Atomically allocate the next version number and write the version.

        This closes the read-then-write race in the ``next_version()`` +
        :meth:`add_version` pattern: two concurrent submissions in the *same
        process* are each assigned a distinct number, because the chosen number
        is reserved under the store lock before the (slow) upload runs, so the
        second allocation sees it taken. ``env.version`` is ignored and
        replaced; the assigned number is returned.

        Residual limitation: this coordinates a single process only, which is
        CLAUDE.md's deployment invariant ("Exactly one instance, ever") --
        running two of this service at once is not a supported configuration.
        If that invariant were ever violated, two processes could still pick
        the same number (no shared allocator); that needs shared state and is
        out of scope here.
        """
        artifact_id = env.id
        # Loading the meta record seeds the high-water mark into the index, so
        # a process that has not read this artifact yet cannot reuse a number
        # a previous process retired. Cached after the first read.
        self.get_meta(artifact_id)
        with self._lock:
            entry = self._index.setdefault(artifact_id, _ArtEntry())
            self._forget_absent_locked(artifact_id)
            version = entry.next_free_number()
            entry.reserving.add(version)

        assigned = replace(env, version=version)
        raw = assigned.to_json()
        tags = [TAG_ALL, tag_for_id(artifact_id), tag_for_version(version)]
        try:
            file_id = self._backend.upload(
                f"artifact-{artifact_id}-v{version}.json", raw, tags
            )
        except Exception:
            with self._lock:
                cur = self._index.get(artifact_id)
                if cur is not None:
                    cur.reserving.discard(version)
            raise

        with self._lock:
            entry = self._index.setdefault(artifact_id, _ArtEntry())
            self._forget_absent_locked(artifact_id)
            entry.reserving.discard(version)
            entry.high_water = max(entry.high_water, version)
            previous = entry.versions.get(version)
            entry.versions[version] = _VerEntry(
                file_id=file_id, status=assigned.status, size_bytes=len(raw)
            )
            if previous is not None:
                self._memory.pop((artifact_id, previous.file_id), None)
            self._remember_locked(artifact_id, file_id, assigned)
        self._write_disk_cache(artifact_id, file_id, raw)
        if previous is not None and previous.file_id != file_id:
            self._delete_file(artifact_id, previous.file_id)
        self._prune_versions(artifact_id)
        self._prune_proposals(artifact_id)
        self._revisions.bump(artifact_id)
        return version

    def get_version(
        self, artifact_id: str, version: int, fresh: bool = False
    ) -> Envelope | None:
        """One version by number, regardless of its status; None when missing.

        ``fresh=True`` re-reads the artifact's Storage tags first (see
        :meth:`refresh`) instead of trusting the in-memory index -- defence in
        depth under the exactly-one-process rule (CLAUDE.md), not a way to
        reconcile with a second writer, which this deployment never runs; the
        default stays fast (index only, with the usual miss fallback).
        """
        if fresh:
            self.refresh(artifact_id)
        entry = self._resolve(artifact_id)
        if entry is None:
            return None
        return self._load_version(artifact_id, entry, version)

    def get_head(self, artifact_id: str, fresh: bool = False) -> Envelope | None:
        """The version ``/a/{id}`` serves: the pinned one, else the newest live.

        ``fresh=True`` re-reads the artifact's Storage tags first (see
        :meth:`refresh`) instead of trusting the in-memory index -- defence in
        depth under the exactly-one-process rule (CLAUDE.md), not a way to
        reconcile with a second writer, which this deployment never runs; the
        default stays fast (index only, with the usual miss fallback).
        """
        if fresh:
            self.refresh(artifact_id)
        entry = self._resolve(artifact_id)
        if entry is None:
            return None

        meta = self.get_meta(artifact_id)
        if (
            meta is not None
            and meta.head_mode == HEAD_PINNED
            and meta.head_version is not None
        ):
            pinned = self._load_version(artifact_id, entry, meta.head_version)
            if pinned is not None and pinned.status == STATUS_LIVE:
                return pinned
            logger.warning(
                "Artifact %s pins version %s which is not live; "
                "falling back to the newest live version",
                artifact_id,
                meta.head_version,
            )

        for number in reversed(entry.version_numbers()):
            env = self._load_version(artifact_id, entry, number)
            if env is not None and env.status == STATUS_LIVE:
                return env
        return None

    def verify_single_head(self, artifact_id: str) -> bool:
        """Assert the single-live-head invariant is unambiguous and honoured.

        Best-effort pruning of superseded files can fail (Storage errors are
        logged, not raised), so two "live head" candidates could linger. This
        confirms :meth:`get_head` is still deterministic: the served head is the
        valid pinned live version, else the highest-numbered live version. Used
        by tests (and available to callers) to check the invariant after writes.

        Returns ``False`` when there is no live version at all (nothing to
        serve) or when the served head is not the deterministic choice.
        """
        entry = self._resolve(artifact_id)
        if entry is None:
            return False
        head = self.get_head(artifact_id)
        if head is None:
            return False
        live = [
            number
            for number in entry.version_numbers()
            if self._is_live(artifact_id, entry, number)
        ]
        if not live:
            return False
        meta = self.get_meta(artifact_id)
        if (
            meta is not None
            and meta.head_mode == HEAD_PINNED
            and meta.head_version in live
        ):
            return head.version == meta.head_version
        return head.version == max(live)

    def list_versions(self, artifact_id: str, fresh: bool = False) -> list[dict]:
        """Public metadata of every version (live and proposed), newest first.

        ``fresh=True`` re-reads the artifact's Storage tags first (see
        :meth:`refresh`) instead of trusting the in-memory index -- defence in
        depth under the exactly-one-process rule (CLAUDE.md), not a way to
        reconcile with a second writer, which this deployment never runs.
        """
        if fresh:
            self.refresh(artifact_id)
        entry = self._resolve(artifact_id)
        if entry is None:
            return []
        head = self.get_head(artifact_id)
        head_version = head.version if head is not None else None

        metas: list[dict] = []
        for number in reversed(entry.version_numbers()):
            try:
                env = self._load_version(artifact_id, entry, number)
            except BackendError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad record must not
                # break the whole history.
                logger.warning(
                    "Cannot load version %s of artifact %s: %s",
                    number,
                    artifact_id,
                    exc,
                )
                continue
            if env is None:
                continue
            metas.append(env.public_meta(is_head=number == head_version))
        return metas

    def next_version(self, artifact_id: str) -> int:
        """The number the next submitted version gets (proposals included).

        Read-only display helper. It accounts for in-flight reservations so it
        never shows a number an :meth:`add_version_next` is about to claim, but
        allocating with it and a separate :meth:`add_version` is still racy —
        prefer :meth:`add_version_next` for the actual write.
        """
        # Same seeding as add_version_next: without the meta record loaded, a
        # fresh process would show max(existing) + 1 and contradict what the
        # allocation is about to do.
        self.get_meta(artifact_id)
        entry = self._resolve(artifact_id)
        if entry is None:
            return 1
        with self._lock:
            return entry.next_free_number()

    def set_status(self, artifact_id: str, version: int, status: str) -> bool:
        """Rewrite one version file with a new status. False when not found."""
        if status not in _STATUSES:
            raise ValueError(f"Unknown status: {status!r}")
        entry = self._resolve(artifact_id)
        if entry is None:
            return False
        env = self._load_version(artifact_id, entry, version)
        if env is None:
            return False
        if env.status == status and version in entry.versions:
            return True

        # Never mutate the cached object: rewrite a copy.
        self.add_version(replace(env, status=status, version=version))
        # A legacy artifact now has a real version file; the stale schema-1
        # envelope would otherwise shadow it as "version 1".
        self._retire_legacy_if_covered(artifact_id, version)
        return True

    def delete_version(self, artifact_id: str, version: int) -> bool:
        """Delete one version. Refuses to remove the last live version.

        Deletes the authoritative Storage file(s) *first* and only drops the
        index/cache entry when the backend confirms deletion. If the backend
        delete fails the file still exists, so the index keeps pointing at it
        and this returns ``False`` — never a success claim over a file that is
        still there.

        ``False`` deliberately has exactly two meanings — "policy says no" and
        "the backend did not confirm" — because the API layer needs to tell a
        409 from a 502. The other destructive policy (COR-075-006: a version
        the head is pinned to may not be deleted, since that would leave the
        stored head naming a version that no longer exists) is therefore
        enforced in ``src.main.delete_version``, where it can carry its own
        409 and an explanation, rather than folded into this ``False``.
        """
        entry = self._resolve(artifact_id)
        if entry is None:
            return False
        numbers = entry.version_numbers()
        if version not in numbers:
            return False

        target = self._load_version(artifact_id, entry, version)
        if target is None:
            return False
        if target.status == STATUS_LIVE:
            live = [
                number
                for number in numbers
                if number != version and self._is_live(artifact_id, entry, number)
            ]
            if not live:
                logger.info(
                    "Refusing to delete the only live version %s of artifact %s",
                    version,
                    artifact_id,
                )
                return False

        if version == max(numbers):
            # Deleting the newest version is the one event after which
            # ``max(existing) + 1`` would hand this number to new content --
            # and every link, comment anchor and diff that named it would
            # silently point at something else. Persist the mark *before* the
            # file goes: if this write fails the version simply stays, whereas
            # the other order could lose the number across a restart.
            meta = self.get_meta(artifact_id)
            if meta is not None and meta.version_high_water < version:
                self.save_meta(replace(meta, version_high_water=version))
            with self._lock:
                entry.high_water = max(entry.high_water, version)

        with self._lock:
            ver_entry = entry.versions.get(version)
            legacy_file_id = (
                entry.legacy_file_id
                if version == 1 and entry.legacy_file_id is not None
                else None
            )

        # Delete the backend files first; only mutate the index on confirmed
        # success so a failed delete never leaves us claiming the version is gone.
        all_ok = True
        if ver_entry is not None:
            all_ok = self._delete_file_confirmed(artifact_id, ver_entry.file_id) and all_ok
        if legacy_file_id is not None:
            all_ok = (
                self._delete_file_confirmed(artifact_id, legacy_file_id) and all_ok
            )
        if not all_ok:
            return False

        with self._lock:
            popped = entry.versions.pop(version, None)
            if popped is not None:
                self._memory.pop((artifact_id, popped.file_id), None)
            if legacy_file_id is not None and entry.legacy_file_id == legacy_file_id:
                entry.legacy_file_id = None
                self._memory.pop((artifact_id, legacy_file_id), None)
        # Bumped only on a confirmed deletion: a failed backend delete left the
        # version in place, so nothing changed for a poller either.
        self._revisions.bump(artifact_id)
        return True

    # ---------------------------------------------------------------- trash

    def trash(self, artifact_id: str, when_iso: str) -> bool:
        """Soft-delete an artifact: freeze it and kill its public link.

        Nothing is removed from Storage — only the meta record changes, moving
        to :data:`ARTIFACT_TRASHED` and remembering the status to come back to
        in ``restore_status``. While trashed, :meth:`resolve_share` refuses the
        artifact's public identifier and no version or comment is accepted;
        :meth:`restore` undoes all of it and :meth:`delete` is still the
        irreversible purge.

        ``when_iso`` is the caller's timestamp (this module never reads the
        clock); it lands in ``trashed_at`` and, when non-empty, in
        ``updated_at``. Returns ``False`` only when the artifact is unknown;
        trashing an already-trashed artifact is a successful no-op that
        deliberately does not overwrite the remembered ``restore_status``.
        """
        meta = self.get_meta(artifact_id)
        if meta is None:
            return False
        if meta.is_trashed():
            return True
        self.save_meta(
            replace(
                meta,
                status=ARTIFACT_TRASHED,
                restore_status=(
                    meta.status
                    if meta.status in ARTIFACT_SETTABLE_STATUSES
                    else ARTIFACT_DRAFT
                ),
                trashed_at=when_iso,
                updated_at=when_iso or meta.updated_at,
            )
        )
        logger.info("Artifact %s moved to the trash", artifact_id)
        return True

    def restore(self, artifact_id: str, when_iso: str = "") -> bool:
        """Bring a trashed artifact back to the status it was trashed from.

        Returns ``False`` when the artifact is unknown or was not in the trash.
        ``when_iso``, when given, becomes the new ``updated_at``.
        """
        meta = self.get_meta(artifact_id)
        if meta is None or not meta.is_trashed():
            return False
        self.save_meta(
            replace(
                meta,
                status=meta.restore_status,
                trashed_at="",
                updated_at=when_iso or meta.updated_at,
            )
        )
        logger.info(
            "Artifact %s restored from the trash to %s",
            artifact_id,
            meta.restore_status,
        )
        return True

    # --------------------------------------------------------------- delete

    def delete(self, artifact_id: str) -> bool:
        """Delete every Storage file of an artifact and purge the caches.

        Attempts every file and returns ``True`` only when *all* authoritative
        files were confirmed deleted (and at least one existed). If any backend
        delete fails, the residual files stay in Storage, so the index/cache are
        left intact and this returns ``False`` — the caller must not report a
        full deletion. An unknown artifact (no files) also returns ``False``.

        **REL-100-001: children first, the meta record strictly last.** The tag
        search answers in no order the caller may rely on, and this used to walk
        it as it came. When a version delete failed *after* the meta file was
        already gone, the artifact lost the only record that
        :func:`src.main._owner_only` can authorize against: the route answered
        502 "retry the purge", the retry found no artifact and 404ed, and the
        surviving version file became unreachable and undeletable forever.

        So the files are classified (:meth:`_classify_for_delete`) and deleted
        in two phases. Every child — version files, a schema-1 legacy envelope,
        anything else tagged for this artifact — goes first; the meta record is
        touched only once every child is confirmed gone. The guarantee that buys
        is the one that matters: **whenever any child survives, its meta
        survives too**, so the purge stays authorizable and a retry (in this
        process or after a restart with an empty disk) resumes rather than
        strands. Deleting an already-deleted file is a no-op for the backend, so
        the retry is idempotent by construction.

        Superseded meta files (an interrupted :meth:`save_meta` can leave one)
        are deleted oldest-first, so the *authorizing* record — the highest file
        ID, which is what :func:`_absorb_file` elects — is the very last file to
        go.
        """
        files: list[FileInfo] = self._backend.search_by_tag(tag_for_id(artifact_id))
        if not files:
            return False
        children, metas = self._classify_for_delete(files)

        # Phase 1: every child. A failure here does not abort the phase — the
        # other children are independent files and clearing them is real
        # progress for the retry — but it does veto phase 2 entirely.
        all_ok = True
        for info in children:
            all_ok = self._delete_file_confirmed(artifact_id, info.id) and all_ok

        if not all_ok:
            logger.warning(
                "Partial delete of artifact %s: some Storage files remain; "
                "keeping its meta record and index entry so the purge stays "
                "authorized and can be retried",
                artifact_id,
            )
            self._resync_after_partial_delete(artifact_id)
            return False

        # Phase 2: the authorizing record, now that nothing it authorizes is
        # left. A failure here leaves a meta with no version — inert (every read
        # answers 404), still purgeable by the owner, and reaped by
        # :meth:`_reap_aborted_publishes` once it is too old to be in flight.
        for info in metas:
            all_ok = self._delete_file_confirmed(artifact_id, info.id) and all_ok

        if not all_ok:
            logger.warning(
                "Partial delete of artifact %s: its meta record could not be "
                "removed; the artifact has no content left and the purge can "
                "be retried",
                artifact_id,
            )
            self._resync_after_partial_delete(artifact_id)
            return False

        with self._lock:
            self._index.pop(artifact_id, None)
            self._forget_memory_locked(artifact_id)
            self._forget_meta_locked(artifact_id)
            self._forget_share_locked(artifact_id)
            # Every file of this artifact is gone from Storage, so the id is
            # absent in exactly the sense the negative cache records; saying
            # so here spares the next probe of a purged id a tag search
            # (SEC-100-002).
            self._remember_absent_locked(artifact_id)
        self._purge_disk_cache(artifact_id)
        # The artifact is gone, so its revision has nothing left to describe;
        # dropping it keeps a create/purge loop from growing the ledger.
        self._revisions.forget(artifact_id)
        return True

    def _resync_after_partial_delete(self, artifact_id: str) -> None:
        """Re-read an artifact's tags after a delete that only partly landed.

        Some of its files are gone but the index still names them, so a read
        would try to download a file that no longer exists. Re-running the tag
        search leaves the index describing what is actually in Storage — the
        meta record included, which is exactly what the retry needs to
        authenticate. Best effort: a failure here only costs accuracy of an
        entry the caller is already being told is incomplete.
        """
        try:
            self.refresh(artifact_id)
        except BackendError as exc:
            logger.warning(
                "Cannot refresh artifact %s after a partial delete: %s",
                artifact_id,
                exc,
            )

    @staticmethod
    def _classify_for_delete(
        files: list[FileInfo],
    ) -> tuple[list[FileInfo], list[FileInfo]]:
        """Split an artifact's files into (children, meta records) for REL-100-001.

        The split follows the Storage model exactly as documented in the module
        docstring and in CLAUDE.md: a meta record is the file tagged
        :data:`TAG_META`, a version file is tagged ``artifact-ver-{n}``, and a
        file with neither tag is a schema-1 legacy envelope (or some other
        record an older build wrote). Only the meta tag is treated as special —
        everything else is a child and goes first — because getting the
        classification wrong in the *other* direction is what created the
        unreachable orphan this fixes.

        Children come back newest-first (highest file ID), which is how the
        backend answers anyway; meta records come back oldest-first, so the
        authorizing one is deleted last of all.
        """
        children: list[FileInfo] = []
        metas: list[FileInfo] = []
        for info in files:
            if TAG_META in (info.tags or []):
                metas.append(info)
            else:
                children.append(info)
        children.sort(key=lambda info: info.id, reverse=True)
        metas.sort(key=lambda info: info.id)
        return children, metas

    # ----------------------------------------------------------------- list

    def list_owner(self, owner_key: str) -> list[dict]:
        """One owner's artifacts, newest ``updated_at`` first.

        Driven by the owner tag, which lives on the meta file (and, for legacy
        artifacts, on the schema-1 envelope), so a cold process answers without
        a full hydrate.
        """
        files = self._backend.search_by_tag(tag_for_owner(owner_key))
        artifact_ids: list[str] = []
        for info in files:
            artifact_id = _tag_value(info.tags, _TAG_ID_PREFIX)
            if artifact_id and artifact_id not in artifact_ids:
                artifact_ids.append(artifact_id)

        rows: list[dict] = []
        for artifact_id in artifact_ids:
            try:
                row = self._owner_row(artifact_id)
            except BackendError:
                # Storage is unhealthy — the caller maps this to 502.
                raise
            except Exception as exc:  # noqa: BLE001 - one bad record must not
                # break the whole listing.
                logger.warning(
                    "Cannot load artifact %s for listing: %s", artifact_id, exc
                )
                continue
            if row is not None:
                rows.append(row)

        rows.sort(key=lambda row: row.get("updated_at") or "", reverse=True)
        return rows

    def _owner_row(self, artifact_id: str) -> dict | None:
        entry = self._resolve(artifact_id)
        if entry is None:
            return None
        meta = self.get_meta(artifact_id)
        if meta is None:
            logger.warning("Artifact %s has no meta record; skipping", artifact_id)
            return None
        head = self.get_head(artifact_id)

        versions_count = 0
        proposed_count = 0
        for number in entry.version_numbers():
            env = self._load_version(artifact_id, entry, number)
            if env is None:
                continue
            versions_count += 1
            if env.status == STATUS_PROPOSED:
                proposed_count += 1

        return {
            "id": artifact_id,
            # The public identifier: equal to ``id`` until the link is rotated,
            # and what every /a/{...} URL for this row must be built from.
            "share_id": meta.share_id,
            "title": head.title if head is not None else "",
            "created_at": meta.created_at,
            "updated_at": meta.updated_at,
            "accept_versions": meta.accept_versions,
            "accept_versions_mode": meta.accept_versions_mode,
            "comments_mode": meta.comments_mode,
            "status": meta.status,
            "trashed_at": meta.trashed_at,
            "protected": bool(meta.password),
            "head_version": head.version if head is not None else None,
            "versions_count": versions_count,
            "proposed_count": proposed_count,
        }

    # ------------------------------------------------------------ retention

    def _prune_versions(self, artifact_id: str) -> None:
        """Drop the oldest prunable live versions above the retention limit.

        Never prunes a proposal, the version currently served as head, or the
        pinned version. Best effort: Storage failures are logged, not raised.
        """
        entry = self._resolve(artifact_id)
        if entry is None:
            return

        meta = self.get_meta(artifact_id)
        protected: set[int] = set()
        if meta is not None and meta.head_version is not None:
            protected.add(meta.head_version)
        head = self.get_head(artifact_id)
        if head is not None:
            protected.add(head.version)

        live: list[int] = []
        for number in entry.version_numbers():
            env = self._load_version(artifact_id, entry, number)
            if env is not None and env.status == STATUS_LIVE:
                live.append(number)

        prunable = [number for number in live if number not in protected]
        excess = len(live) - self._max_versions
        for number in prunable:
            if excess <= 0:
                break
            with self._lock:
                ver_entry = entry.versions.pop(number, None)
                legacy_file_id = None
                if number == 1 and entry.legacy_file_id is not None:
                    legacy_file_id = entry.legacy_file_id
                    entry.legacy_file_id = None
                if ver_entry is not None:
                    self._memory.pop((artifact_id, ver_entry.file_id), None)
                if legacy_file_id is not None:
                    self._memory.pop((artifact_id, legacy_file_id), None)
            if ver_entry is not None:
                self._delete_file(artifact_id, ver_entry.file_id)
            if legacy_file_id is not None:
                self._delete_file(artifact_id, legacy_file_id)
            logger.info("Pruned version %s of artifact %s", number, artifact_id)
            excess -= 1

    def _prune_proposals(self, artifact_id: str) -> None:
        """Drop the oldest proposed versions above ``max_proposed_versions``.

        Retention (:meth:`_prune_versions`) only counts *live* versions, so
        proposals could otherwise accumulate without bound as many projects
        submit. Proposals are never served as head, so pruning the oldest is
        safe; a proposal pinned as ``head_version`` (fallback target) is spared.
        Best effort: Storage failures are logged, not raised.
        """
        if self._max_proposed_versions <= 0:
            return
        entry = self._resolve(artifact_id)
        if entry is None:
            return

        meta = self.get_meta(artifact_id)
        protected: set[int] = set()
        if meta is not None and meta.head_version is not None:
            protected.add(meta.head_version)

        proposed: list[int] = []
        for number in entry.version_numbers():
            env = self._load_version(artifact_id, entry, number)
            if env is not None and env.status == STATUS_PROPOSED:
                proposed.append(number)

        prunable = [number for number in proposed if number not in protected]
        # version_numbers() is ascending, so ``prunable`` is oldest-first.
        excess = len(proposed) - self._max_proposed_versions
        for number in prunable:
            if excess <= 0:
                break
            with self._lock:
                ver_entry = entry.versions.pop(number, None)
                if ver_entry is not None:
                    self._memory.pop((artifact_id, ver_entry.file_id), None)
            if ver_entry is not None:
                self._delete_file(artifact_id, ver_entry.file_id)
            logger.info(
                "Pruned proposed version %s of artifact %s over the proposal cap",
                number,
                artifact_id,
            )
            excess -= 1

    def _retire_legacy_if_covered(self, artifact_id: str, version: int) -> None:
        """Delete the schema-1 envelope once a real v1 file supersedes it."""
        if version != 1:
            return
        with self._lock:
            entry = self._index.get(artifact_id)
            if entry is None or entry.legacy_file_id is None:
                return
            if 1 not in entry.versions:
                return
            legacy_file_id = entry.legacy_file_id
            entry.legacy_file_id = None
            self._memory.pop((artifact_id, legacy_file_id), None)
        self._delete_file(artifact_id, legacy_file_id)

    def _delete_stale_meta_files(self, artifact_id: str, keep_file_id: int) -> None:
        """Best-effort removal of superseded meta files. Never raises."""
        try:
            files = self._backend.search_by_tag(tag_for_id(artifact_id))
        except BackendError as exc:
            logger.warning(
                "Cannot list meta files of artifact %s: %s", artifact_id, exc
            )
            return
        for info in files:
            if info.id == keep_file_id or TAG_META not in (info.tags or []):
                continue
            self._delete_file(artifact_id, info.id)

    def _delete_file(self, artifact_id: str, file_id: int) -> None:
        """Delete one Storage file and its cache entry. Never raises."""
        try:
            self._backend.delete(file_id)
        except BackendError as exc:
            logger.warning(
                "Cannot delete file %s of artifact %s: %s", file_id, artifact_id, exc
            )
        self._delete_disk_cache(artifact_id, file_id)

    def _delete_file_confirmed(self, artifact_id: str, file_id: int) -> bool:
        """Delete one Storage file, returning whether the backend confirmed it.

        Unlike :meth:`_delete_file` this reports failure so callers that must
        not claim success over a still-present file (``delete_version``) can
        react. The disk cache copy is dropped either way — it is pure cache.
        """
        ok = True
        try:
            self._backend.delete(file_id)
        except BackendError as exc:
            ok = False
            logger.warning(
                "Cannot delete file %s of artifact %s: %s", file_id, artifact_id, exc
            )
        self._delete_disk_cache(artifact_id, file_id)
        return ok

    # ----------------------------------------------------------------- load

    def _is_live(self, artifact_id: str, entry: _ArtEntry, version: int) -> bool:
        env = self._load_version(artifact_id, entry, version)
        return env is not None and env.status == STATUS_LIVE

    def _load_version(
        self, artifact_id: str, entry: _ArtEntry, version: int
    ) -> Envelope | None:
        """Load one version, transparently migrating a legacy schema-1 file."""
        ver_entry = entry.versions.get(version)
        if ver_entry is not None:
            env = self._load_envelope(
                artifact_id, ver_entry.file_id, size_hint=ver_entry.size_bytes or None
            )
            if env is None:
                return None
            # The version tag is authoritative. A file tagged v{n} whose payload
            # claims a different version number is a mislabelled/cross-wired
            # record; skip it rather than serve content under the wrong number.
            if env.version != version:
                logger.warning(
                    "Version file %s of artifact %s is tagged v%s but its "
                    "payload declares v%s; skipping as corrupt",
                    ver_entry.file_id,
                    artifact_id,
                    version,
                    env.version,
                )
                return None
            ver_entry.status = env.status
            return env
        if version == 1 and entry.legacy_file_id is not None:
            migrated = self._load_legacy(
                artifact_id,
                entry.legacy_file_id,
                size_hint=entry.legacy_size_bytes or None,
            )
            return migrated[0] if migrated is not None else None
        return None

    def _load_legacy(
        self, artifact_id: str, file_id: int, size_hint: int | None = None
    ) -> tuple[Envelope, ArtifactMeta] | None:
        raw = self._read_raw(artifact_id, file_id, size_hint=size_hint)
        if raw is None:
            return None
        try:
            migrated = migrate_legacy(raw)
        except ValueError as exc:
            logger.error(
                "Corrupt legacy envelope for artifact %s (file %s): %s",
                artifact_id,
                file_id,
                exc,
            )
            return None
        # Cross-artifact guard: the legacy payload's own id must match the
        # artifact it was filed under.
        if migrated[0].id != artifact_id:
            logger.warning(
                "Legacy envelope in file %s is tagged for artifact %s but "
                "declares id %r; skipping as cross-wired/corrupt",
                file_id,
                artifact_id,
                migrated[0].id,
            )
            return None
        return migrated

    def _load_envelope(
        self, artifact_id: str, file_id: int, size_hint: int | None = None
    ) -> Envelope | None:
        key = (artifact_id, file_id)
        with self._lock:
            envelope = self._memory.get(key)
            if envelope is not None:
                self._memory.move_to_end(key)
                return envelope

        raw = self._read_raw(artifact_id, file_id, size_hint=size_hint)
        if raw is None:
            return None
        try:
            envelope = Envelope.from_json(raw)
        except ValueError as exc:
            logger.error(
                "Corrupt envelope in Storage for artifact %s (file %s): %s",
                artifact_id,
                file_id,
                exc,
            )
            return None
        # The file was selected by the ``artifact-id-{id}`` tag / index; refuse
        # to serve a payload whose own id disagrees. Otherwise a file tagged for
        # artifact A but carrying id B would leak B's content under A's
        # capability URL and authorization context.
        if envelope.id != artifact_id:
            logger.warning(
                "Envelope in file %s is tagged for artifact %s but declares id "
                "%r; skipping as cross-wired/corrupt",
                file_id,
                artifact_id,
                envelope.id,
            )
            return None
        with self._lock:
            self._remember_locked(artifact_id, file_id, envelope)
        return envelope

    def _load_meta(
        self, artifact_id: str, file_id: int, size_hint: int | None = None
    ) -> ArtifactMeta | None:
        key = (artifact_id, file_id)
        with self._lock:
            meta = self._meta_memory.get(key)
            if meta is not None:
                self._meta_memory.move_to_end(key)
                return meta

        raw = self._read_raw(artifact_id, file_id, size_hint=size_hint)
        if raw is None:
            return None
        try:
            meta = ArtifactMeta.from_json(raw)
        except ValueError as exc:
            logger.error(
                "Corrupt meta in Storage for artifact %s (file %s): %s",
                artifact_id,
                file_id,
                exc,
            )
            return None
        # Same cross-artifact guard as _load_envelope: the meta record's own id
        # must match the artifact it was filed under.
        if meta.id != artifact_id:
            logger.warning(
                "Meta in file %s is tagged for artifact %s but declares id %r; "
                "skipping as cross-wired/corrupt",
                file_id,
                artifact_id,
                meta.id,
            )
            return None
        with self._lock:
            self._remember_meta_locked(artifact_id, file_id, meta)
            self._seed_high_water_locked(artifact_id, meta)
        return meta

    def _seed_high_water_locked(self, artifact_id: str, meta: ArtifactMeta) -> None:
        """Carry the persisted high-water mark into the index. Caller holds the lock."""
        entry = self._index.get(artifact_id)
        if entry is not None and meta.version_high_water > entry.high_water:
            entry.high_water = meta.version_high_water

    def _too_large(self, size: int | None) -> bool:
        """True when ``size`` exceeds the configured envelope byte bound."""
        return (
            self._max_envelope_bytes > 0
            and size is not None
            and size > self._max_envelope_bytes
        )

    def _read_raw(
        self, artifact_id: str, file_id: int, size_hint: int | None = None
    ) -> bytes | None:
        """Disk cache -> Storage download. None when the file is unreadable.

        ``size_hint`` is the Storage-reported byte size (from the file listing).
        When it — or the actually downloaded payload — exceeds
        ``max_envelope_bytes`` the record is skipped (logged, treated as
        not-found) so an oversized persisted record cannot exhaust memory.

        ``BackendError`` from Storage reads propagates (the API maps it to 502).
        """
        if self._too_large(size_hint):
            logger.warning(
                "Skipping oversized file %s of artifact %s: %s bytes > limit %s",
                file_id,
                artifact_id,
                size_hint,
                self._max_envelope_bytes,
            )
            return None
        raw = self._read_disk_cache(artifact_id, file_id)
        if raw is not None:
            return raw
        raw = self._backend.download(file_id)
        if raw is None:
            return None
        if self._too_large(len(raw)):
            logger.warning(
                "Discarding oversized download of file %s of artifact %s: "
                "%s bytes > limit %s",
                file_id,
                artifact_id,
                len(raw),
                self._max_envelope_bytes,
            )
            return None
        self._write_disk_cache(artifact_id, file_id, raw)
        return raw

    # ---------------------------------------------------------- memory LRU

    def _remember_locked(
        self, artifact_id: str, file_id: int, envelope: Envelope
    ) -> None:
        """Insert into the envelope LRU. Caller must hold the lock."""
        if self._cache_max_entries <= 0:
            return
        key = (artifact_id, file_id)
        self._memory[key] = envelope
        self._memory.move_to_end(key)
        while len(self._memory) > self._cache_max_entries:
            self._memory.popitem(last=False)

    def _remember_meta_locked(
        self, artifact_id: str, file_id: int, meta: ArtifactMeta
    ) -> None:
        """Insert into the meta LRU. Caller must hold the lock."""
        if self._cache_max_entries <= 0:
            return
        key = (artifact_id, file_id)
        self._meta_memory[key] = meta
        self._meta_memory.move_to_end(key)
        while len(self._meta_memory) > self._cache_max_entries:
            self._meta_memory.popitem(last=False)

    def _forget_memory_locked(self, artifact_id: str) -> None:
        """Drop every envelope LRU entry of an artifact. Caller holds the lock."""
        for key in [k for k in self._memory if k[0] == artifact_id]:
            self._memory.pop(key, None)

    def _forget_meta_locked(self, artifact_id: str) -> None:
        """Drop every meta LRU entry of an artifact. Caller holds the lock."""
        for key in [k for k in self._meta_memory if k[0] == artifact_id]:
            self._meta_memory.pop(key, None)

    # --------------------------------------------------------- disk cache

    def _cache_path(self, artifact_id: str, file_id: int) -> Path | None:
        if not artifact_id or not set(artifact_id) <= _SAFE_ID_CHARS:
            return None
        return self._cache_dir / f"{artifact_id}-{file_id}.json"

    def _read_disk_cache(self, artifact_id: str, file_id: int) -> bytes | None:
        path = self._cache_path(artifact_id, file_id)
        if path is None:
            return None
        # Bound the read: a cache file above the envelope limit is dropped
        # rather than loaded into memory (it is pure cache, re-fetchable).
        if self._max_envelope_bytes > 0:
            try:
                cached_size = path.stat().st_size
            except FileNotFoundError:
                return None
            except OSError as exc:
                logger.warning("Cannot stat disk cache %s: %s", path, exc)
                return None
            if cached_size > self._max_envelope_bytes:
                logger.warning(
                    "Dropping oversized disk cache for artifact %s (file %s): "
                    "%s bytes > limit %s",
                    artifact_id,
                    file_id,
                    cached_size,
                    self._max_envelope_bytes,
                )
                self._delete_disk_cache(artifact_id, file_id)
                return None
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("Cannot read disk cache %s: %s", path, exc)
            return None
        try:
            json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning(
                "Corrupt disk cache for artifact %s (file %s): %s",
                artifact_id,
                file_id,
                exc,
            )
            self._delete_disk_cache(artifact_id, file_id)
            return None
        return raw

    def _write_disk_cache(self, artifact_id: str, file_id: int, raw: bytes) -> None:
        path = self._cache_path(artifact_id, file_id)
        if path is None:
            return
        tmp = path.parent / f"{path.name}.tmp"
        try:
            _harden_cache_dir(self._cache_dir)
            _write_private_bytes(tmp, raw)
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("Cannot write disk cache %s: %s", path, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _delete_disk_cache(self, artifact_id: str, file_id: int) -> None:
        path = self._cache_path(artifact_id, file_id)
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Cannot delete disk cache %s: %s", path, exc)

    def _purge_disk_cache(self, artifact_id: str) -> None:
        """Remove every cached file of an artifact."""
        if not artifact_id or not set(artifact_id) <= _SAFE_ID_CHARS:
            return
        try:
            candidates = list(self._cache_dir.glob(f"{artifact_id}-*.json"))
        except OSError as exc:
            logger.warning("Cannot scan cache dir %s: %s", self._cache_dir, exc)
            return
        for path in candidates:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Cannot prune disk cache %s: %s", path, exc)


def _age_seconds(created: str) -> float | None:
    """Seconds since an ISO timestamp as Storage reports it; None if unreadable.

    Storage answers with an offset; a naive value is read as UTC. Unreadable
    means "do not know", which the reaper treats as "too young to touch".
    """
    try:
        stamp = datetime.fromisoformat((created or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds()


def _absorb_file(entry: _ArtEntry, info: FileInfo) -> None:
    """Record one Storage file in an index entry (newest file ID wins)."""
    tags = info.tags or []
    if TAG_META in tags:
        if entry.meta_file_id is None or entry.meta_file_id < info.id:
            entry.meta_file_id = info.id
            entry.meta_size_bytes = info.size_bytes
        return
    version = _version_from_tags(tags)
    if version is not None:
        existing = entry.versions.get(version)
        if existing is None or existing.file_id < info.id:
            entry.versions[version] = _VerEntry(
                file_id=info.id, size_bytes=info.size_bytes
            )
        return
    # Neither meta nor version tag: a schema-1 single-envelope artifact.
    if entry.legacy_file_id is None or entry.legacy_file_id < info.id:
        entry.legacy_file_id = info.id
        entry.legacy_size_bytes = info.size_bytes
