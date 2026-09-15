"""Application settings.

All configuration comes from environment variables. Required variables have no
defaults — the app fails fast at startup with a clear error instead of
inventing values. Optional limits have documented defaults overridable via env.
"""

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, see token_limits()
    from src.tokens import TokenLimits

REQUIRED_ENV = ["HUB_STORAGE_TOKEN", "HUB_STACK_URL", "HUB_SECRET_KEY"]

#: Shortest accepted ``HUB_SECRET_KEY``. That value is the HMAC key behind
#: every unlock cookie, so a short (therefore low-entropy) one is brute-forcible
#: offline and would let anybody mint their own unlock cookies. Rejected at
#: startup rather than silently accepted.
MIN_SECRET_KEY_CHARS = 32

#: Head-room added on top of the largest document the hub accepts when sizing
#: the inbound request-body ceiling of the content routes (SEC-100-004). A
#: document travels inside a JSON envelope, escaped, next to a handful of
#: sibling fields, so the request is always somewhat larger than the document
#: it carries; without this allowance a legitimate maximum-size publish would
#: be rejected before it was ever parsed.
REQUEST_ENVELOPE_SLACK_BYTES = 1024 * 1024

#: One parsed entry of ``HUB_TRUSTED_PROXY_CIDRS``.
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


#: Accepted values of ``HUB_DESTRUCTIVE_TOKEN_POLICY`` (SEC-075-011), from
#: widest to narrowest. ``project`` is the historical behaviour and stays the
#: default so an upgrade never silently locks an operator out of their own
#: artifacts; the other two are opt-in.
DESTRUCTIVE_TOKEN_POLICIES = ("project", "admin", "allowlist")
DEFAULT_DESTRUCTIVE_TOKEN_POLICY = "project"


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _cidr_env(name: str) -> tuple[IPNetwork, ...]:
    """Parse a comma-separated list of CIDR networks, or fail fast at startup.

    A malformed entry is a configuration error, not something to skip with a
    warning: this list decides whose ``X-Real-IP`` the brute-force limiter
    believes (SEC-100-003), so silently dropping an unparseable entry would
    quietly widen or narrow that trust in a way nobody would notice until it
    mattered. ``strict=False`` accepts a host address carrying a prefix
    (``10.0.0.7/8``) and a bare address (``10.0.0.7`` becomes ``/32``), which
    is how operators usually write down a proxy's address.
    """
    networks: list[IPNetwork] = []
    for entry in os.environ.get(name, "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError as exc:
            raise RuntimeError(
                f"{name} contains an entry that is not a valid CIDR network "
                f"({entry!r}): {exc}"
            ) from exc
    return tuple(networks)


def _destructive_policy_env(name: str) -> str:
    """Parse the destructive-token policy, or fail fast at startup.

    A typo here is not a small thing: ``HUB_DESTRUCTIVE_TOKEN_POLICY=admn``
    silently falling back to the default would leave an operator believing
    they had narrowed destructive authority when they had not. That is exactly
    the failure mode the control exists to prevent, so an unrecognized value
    stops the process instead.
    """
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return DEFAULT_DESTRUCTIVE_TOKEN_POLICY
    if raw not in DESTRUCTIVE_TOKEN_POLICIES:
        raise RuntimeError(
            f"{name}={raw!r} is not a known policy. Use one of: "
            f"{', '.join(DESTRUCTIVE_TOKEN_POLICIES)}."
        )
    return raw


def _token_ids_env(name: str) -> tuple[str, ...]:
    """Parse a comma-separated list of Storage token ids, order-preserving."""
    seen: list[str] = []
    for entry in os.environ.get(name, "").split(","):
        entry = entry.strip()
        if entry and entry not in seen:
            seen.append(entry)
    return tuple(seen)


def _hosts_env(name: str, default: str) -> tuple[str, ...]:
    """Parse a comma-separated host allowlist, order-preserving, blanks dropped."""
    raw = os.environ.get(name)
    source = raw if raw is not None and raw.strip() else default
    return tuple(h.strip() for h in source.split(",") if h.strip())


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # Host project access (where serving envelopes live)
    hub_storage_token: str
    hub_stack_url: str
    # Signs unlock cookies
    secret_key: str
    # Optional absolute base URL used in returned artifact URLs
    public_base_url: str | None = None
    # Optional showcase artifact linked from the landing page; absent = no link.
    demo_url: str | None = None
    # HUB_DESIGN_DEMO_URL: the published design-systems walkthrough the landing
    # page links next to the hub's own demo. Optional, like demo_url.
    design_demo_url: str | None = None
    cache_dir: Path = field(default_factory=lambda: Path("/tmp/artifact-cache"))

    # Limits (bytes / seconds / counts)
    max_html_bytes: int = 15 * 1024 * 1024
    max_inline_image_bytes: int = 5 * 1024 * 1024
    max_inline_total_bytes: int = 15 * 1024 * 1024
    git_clone_timeout_s: int = 90
    git_max_repo_bytes: int = 200 * 1024 * 1024
    # SSRF guard: reject git_url hosts that resolve to private/loopback/
    # link-local/reserved addresses (incl. cloud metadata endpoints). Set
    # HUB_GIT_ALLOW_PRIVATE_HOSTS=1 only for trusted self-hosting.
    git_allow_private_hosts: bool = False
    cache_max_entries: int = 200
    unlock_cookie_max_age_s: int = 12 * 3600
    token_verify_timeout_s: int = 15
    # Community versioning (phase 2)
    # Live versions kept per artifact; older non-head, non-pinned ones are pruned.
    max_versions: int = 50
    # Per-contributor cap on submitted versions per rolling day.
    max_versions_per_day: int = 20
    # Largest per-side payload the diff renderer will process.
    diff_max_bytes: int = 2 * 1024 * 1024
    # Project brain (phase 3)
    # Per-contributor cap on inline comments (threads + replies) per rolling day.
    max_comments_per_day: int = 100
    # Reader menu (0.18.0): the value a *newly published* artifact gets for
    # ArtifactMeta.reader_menu -- the small corner control on the frame page
    # that tells a reader how to comment, browse versions or propose one.
    # On by default: the menu exists because readers keep asking those
    # questions. It governs *new* artifacts only -- a meta record written
    # before the field existed always reads as on, whatever this says. Per
    # artifact, the owner overrides it with PUT /api/artifacts/{id}
    # {"reader_menu": false}.
    reader_menu_default: bool = True
    # Extra stack URLs (comma-separated) beyond the *.keboola.com rule
    extra_stacks: tuple[str, ...] = ()
    # Largest persisted envelope/meta record (bytes) the store will download or
    # read from disk cache before serving. Records above this are skipped as a
    # denial-of-service guard (env HUB_MAX_ENVELOPE_BYTES). 0 disables the bound.
    max_envelope_bytes: int = 20 * 1024 * 1024
    # A meta record with no version file is a publish that died between its
    # two writes. Hydrate deletes such records once they are older than this
    # (HUB_REAP_ABORTED_PUBLISH_AFTER_S), which is what makes them safely
    # distinguishable from a publish still in flight; 0 disables reaping.
    reap_aborted_publish_after_s: int = 3600
    # Per-artifact cap on retained "proposed" versions; the oldest proposals
    # above this are pruned (env HUB_MAX_PROPOSED_VERSIONS). Proposals are never
    # served as head, so pruning the oldest is always safe.
    max_proposed_versions: int = 50
    # Trust X-Forwarded-Host / X-Forwarded-Proto to name the public origin.
    # Off by default: a direct client can forge those headers, and the
    # deployed hub always sets HUB_PUBLIC_BASE_URL (which wins outright)
    # anyway. Local development behind a proxy opts in with
    # HUB_TRUST_FORWARDED_HEADERS=1.
    trust_forwarded_headers: bool = False
    # Networks whose members are believed when they name the real client in a
    # forwarded header (env HUB_TRUSTED_PROXY_CIDRS, comma-separated CIDRs).
    # SEC-100-003: X-Real-IP is the key of the brute-force budget, and anyone
    # can send it, so a caller could reset their own budget at will. A
    # forwarded client address is now honoured only when
    # HUB_TRUST_FORWARDED_HEADERS is on *and* the direct peer
    # (request.client.host) falls inside one of these networks. Empty (the
    # default) therefore means "never believe a forwarded client address" —
    # everything buckets by the peer address, which is safe but coarse behind
    # a proxy, since every caller then shares the proxy's bucket.
    trusted_proxy_cidrs: tuple[IPNetwork, ...] = ()
    # How many X-Forwarded-For entries the client-address walk examines,
    # counted from the right (env HUB_MAX_FORWARDED_CHAIN_ENTRIES).
    # SEC-100-003: the header is caller-supplied and arbitrarily long, and the
    # walk parses one address per entry, so an unbounded chain would let a
    # single request buy thousands of parses. Sixteen is far more hops than
    # any real path — client, platform proxy, nginx is three — and a chain
    # that reaches the cap without finding an untrusted entry simply falls
    # back to the peer address, which is the safe answer anyway.
    max_forwarded_chain_entries: int = 16
    # Which tokens of the owning project may run a *destructive* route — soft
    # delete, purge, rotate-link, version delete, webhook key rotation
    # (env HUB_DESTRUCTIVE_TOKEN_POLICY). SEC-075-011: ownership is a
    # (stack, project) pair, so before this every token of that project — a
    # read-only one included — could purge every artifact the project owns.
    #
    #   "project"   every token of the owning project (the historical
    #               behaviour, and the default: an upgrade must not lock an
    #               operator out of artifacts they already own)
    #   "admin"     a master token, or one belonging to a project user whose
    #               admin.role is "admin"
    #   "allowlist" only tokens whose id appears in
    #               HUB_DESTRUCTIVE_TOKEN_IDS
    #
    # Non-destructive owner routes (update, head pin, promote, settings,
    # invitations, stats, trash restore) are untouched by every mode.
    destructive_token_policy: str = DEFAULT_DESTRUCTIVE_TOKEN_POLICY
    # Storage token ids allowed to run destructive routes under the
    # "allowlist" policy (env HUB_DESTRUCTIVE_TOKEN_IDS, comma-separated).
    # A token id is an identifier, not a secret. Ignored in the other modes;
    # required non-empty in "allowlist", since an empty allowlist would refuse
    # the owner their own destructive routes with no way to tell that from a
    # deliberate lockdown.
    destructive_token_ids: tuple[str, ...] = ()
    # Failed unlock attempts allowed per (artifact, client IP) per UTC hour
    # before the password gate answers 429 (env
    # HUB_MAX_UNLOCK_ATTEMPTS_PER_HOUR). Each attempt costs a full PBKDF2
    # verification, so this bounds both brute force and the CPU it can burn.
    max_unlock_attempts_per_hour: int = 30
    # Failed unlock (or guest-credential) attempts allowed against one
    # artifact per UTC hour *from every address together*
    # (HUB_MAX_UNLOCK_ATTEMPTS_PER_ARTIFACT_PER_HOUR). Defence in depth for
    # SEC-100-003: the per-address budget above is only as strong as the
    # address is hard to change, and behind a NAT, a botnet or a proxy whose
    # network is trusted it can be changed cheaply. This budget cannot be
    # rotated away at all, so it is set far above what a real audience of one
    # document would ever spend — it is a stop on industrial guessing, not a
    # per-reader limit.
    max_unlock_attempts_per_artifact_per_hour: int = 500
    # Largest inbound request body accepted by every route that does not carry
    # a document — comments, replies, invitations, the unlock form, webhook
    # management, policy-only calls (HUB_MAX_SMALL_REQUEST_BYTES). SEC-100-004:
    # before this ceiling an anonymous caller could have megabytes read and
    # parsed before authentication, a lock or a 404 ever happened. The content
    # routes get the much larger ``max_content_request_bytes`` instead.
    max_small_request_bytes: int = 256 * 1024
    # Most per-artifact mutation locks kept once nothing holds them
    # (HUB_LOCK_REGISTRY_MAX_ENTRIES). SEC-100-002: the registry used to keep
    # an entry per key it ever saw, including keys invented by anonymous
    # callers. Idle entries above this are dropped least-recently-used; a lock
    # somebody is holding or waiting on is never dropped, so the bound cannot
    # break serialization.
    lock_registry_max_entries: int = 1024
    # Artifact ids Storage has confirmed do not exist, remembered so a repeat
    # lookup costs a dict hit instead of a tag search
    # (HUB_NEGATIVE_LOOKUP_CACHE_ENTRIES). SEC-100-002 moved target resolution
    # in front of authentication, which made every well-formed made-up id an
    # unauthenticated Storage round trip; this bounds the damage a stream of
    # them can do. Entries are dropped least-recently-used above this, and
    # invalidated by every write that can make an id appear, so the bound
    # trades memory for round trips and never for correctness. 0 disables the
    # cache entirely.
    negative_lookup_cache_entries: int = 4096
    # Operational state sidecar (phase 4, src/statedb.py)
    # Seconds between SQLite snapshots into the host project's Storage Files
    # (env HUB_STATE_SNAPSHOT_INTERVAL_S). 0 disables the background thread and
    # leaves snapshots to explicit calls — what the tests use.
    state_snapshot_interval_s: int = 300
    # Largest state snapshot the hub will upload or restore
    # (env HUB_STATE_MAX_SNAPSHOT_BYTES). 0 disables the bound. An oversized
    # snapshot is skipped with a warning rather than restored.
    state_max_snapshot_bytes: int = 50 * 1024 * 1024
    # File name of the SQLite sidecar; main.py joins it with cache_dir, which is
    # the only writable (and ephemeral) path the container has.
    state_db_filename: str = "state.sqlite3"
    # ARCH-100-001: file name of the exclusive startup lock, also under
    # cache_dir (HUB_INSTANCE_LOCK_FILENAME). The lifespan takes a non-blocking
    # flock on it and holds it for the life of the process, so a second uvicorn
    # worker or a second container sharing the disk fails to start instead of
    # quietly corrupting the state the whole design assumes only it writes.
    instance_lock_filename: str = "instance.lock"
    # Outbound webhooks (phase 4, src/webhooks.py)
    # Per-request HTTP timeout for a webhook delivery (HUB_WEBHOOK_TIMEOUT_S).
    webhook_timeout_s: int = 10
    # Total POST attempts per (url, event) before giving up
    # (HUB_WEBHOOK_MAX_ATTEMPTS); retries back off 2**n seconds, capped at 60.
    webhook_max_attempts: int = 3
    # How many webhook URLs one artifact may register
    # (HUB_MAX_WEBHOOKS_PER_ARTIFACT).
    max_webhooks_per_artifact: int = 5
    # Ceiling on queued-but-undelivered webhook deliveries
    # (HUB_WEBHOOK_QUEUE_MAX). One thread consumes them and sleeps through
    # retry backoff, so a stalled receiver would otherwise let the queue grow
    # without limit; past the ceiling the newest delivery is dropped with a
    # log line rather than blocking the request that produced it.
    webhook_queue_max: int = 1000
    # SEC-100-006: how long, in seconds, a receiver's previous signing key
    # stays valid after the owner rotates it (HUB_WEBHOOK_KEY_OVERLAP_S).
    # During this window a delivery carries both the current signature
    # (X-Hub-Signature-256) and the previous one
    # (X-Hub-Signature-256-Previous), so a receiver that has not yet picked
    # up the freshly rotated key does not immediately start failing
    # verification. 0 disables the grace period — rotation takes effect
    # immediately for every subsequent delivery.
    webhook_key_overlap_s: int = 600
    # Interactive sign-in (src/kbclogin.py)
    # Public client label the hub identifies itself with on a stack's device
    # and PKCE endpoints (HUB_LOGIN_CLIENT_ID). Not a secret and not a security
    # boundary — the stack uses it for rate-limit buckets and audit records.
    login_client_id: str = "kbc-artifact-hub"
    # Per-request HTTP timeout for a sign-in call to a stack
    # (HUB_LOGIN_TIMEOUT_S).
    login_timeout_s: int = 20
    # How long a started PKCE login may sit unfinished before its verifier is
    # dropped and the callback stops being accepted (HUB_LOGIN_PKCE_TTL_S).
    login_pkce_ttl_s: int = 600
    # Concurrently pending PKCE logins held in memory; the oldest above this
    # are dropped (HUB_LOGIN_MAX_PENDING_PKCE). PKCE is only offered on a
    # loopback origin, so this bounds a single developer's own tabs.
    login_max_pending_pkce: int = 64
    # Sign-ins one client address may start or renew per UTC hour before
    # /login answers 429 (HUB_MAX_LOGINS_PER_HOUR). Each one costs a call to a
    # stack, so this keeps the hub from becoming an open relay onto Keboola's
    # auth API. Two neighbours are counted apart under the same ceiling rather
    # than sharing this bucket: polling (below, far higher — one sign-in polls
    # for minutes) and signing out (a spent budget there would leave a live
    # session on the stack, so it must not be spendable by anything else).
    max_logins_per_hour: int = 30
    # Polls of an already-started device sign-in one client address may make
    # per UTC hour (HUB_MAX_LOGIN_POLLS_PER_HOUR). Counted apart from the
    # starts above because one honest sign-in polls every few seconds for up
    # to a quarter of an hour — sharing one budget would either throttle the
    # normal flow or make the start budget meaningless.
    max_login_polls_per_hour: int = 600
    # Guest invitations (0.7.0)
    # How many invitations one artifact may hold at once
    # (HUB_MAX_INVITATIONS_PER_ARTIFACT). Each entry is a named capability
    # stored in the artifact's meta record, so this bounds both the meta file
    # and the number of people who can comment without a Keboola account.
    max_invitations_per_artifact: int = 20
    # Vault exports (REL-100-002)
    # Ceiling on the source material one GET /a/{id}/export/vault may render,
    # and on the archive it may write (HUB_EXPORT_MAX_BYTES). 0 disables the
    # bound. Derived from the two limits that decide the worst case: an
    # artifact may keep HUB_MAX_VERSIONS (50) records of up to
    # HUB_MAX_ENVELOPE_BYTES (20 MiB) each, so an unbounded export is a ~1 GiB
    # request anybody holding the capability URL can repeat. The default is
    # three envelopes' worth -- comfortably above any real document's whole
    # history, far below what a container with one process can absorb.
    export_max_bytes: int = 64 * 1024 * 1024
    # Vault builds allowed per (artifact, client address) per UTC hour before
    # the export answers 429 (HUB_MAX_EXPORTS_PER_HOUR). Building a vault
    # diffs every version and converts every HTML document, so this bounds how
    # often one capability-URL holder can ask for that work.
    max_exports_per_hour: int = 20

    # --- design systems (spec 2026-09-14, Key decision 12) ---------------
    # Every design-system limit is a setting so no number lives in a route.
    # Largest normalised bundle one version may hold (HUB_DS_MAX_BUNDLE_BYTES).
    ds_max_bundle_bytes: int = 2 * 1024 * 1024
    # Design systems one project may own at once (HUB_DS_MAX_PER_PROJECT).
    ds_max_per_project: int = 20
    # Versions one design system may hold; an append past this is 409, never a
    # prune -- a version is immutable and only an explicit delete removes one
    # (HUB_DS_MAX_VERSIONS).
    ds_max_versions: int = 50
    # Per-owner cap on submitted versions per rolling day
    # (HUB_DS_MAX_VERSIONS_PER_DAY).
    ds_max_versions_per_day: int = 20
    # Token leaves one document may declare (HUB_DS_MAX_TOKENS).
    ds_max_tokens: int = 5000
    # Deepest DTCG group nesting accepted (HUB_DS_MAX_TOKEN_DEPTH).
    ds_max_token_depth: int = 16
    # Longest {alias} chain followed before the document is rejected
    # (HUB_DS_MAX_ALIAS_DEPTH).
    ds_max_alias_depth: int = 32
    # Components one bundle may carry (HUB_DS_MAX_COMPONENTS).
    ds_max_components: int = 100
    # html + css of a single component (HUB_DS_MAX_COMPONENT_BYTES).
    ds_max_component_bytes: int = 64 * 1024
    # The Markdown guide an agent reads (HUB_DS_MAX_GUIDANCE_BYTES).
    ds_max_guidance_bytes: int = 256 * 1024
    # Colors the chart_palette role may list (HUB_DS_MAX_PALETTE).
    ds_max_palette: int = 12
    # Stylesheet links one bundle may declare (HUB_DS_MAX_FONT_LINKS).
    ds_max_font_links: int = 4
    # Hosts a font link may point at (HUB_DS_FONT_HOSTS, comma-separated). A
    # bundle emits no user markup, only <link> elements at these hosts, so this
    # is the whole allowlist for third-party resources a design system pulls.
    ds_font_hosts: tuple[str, ...] = ("fonts.googleapis.com",)
    # Trimmed length of a design system's name (HUB_DS_MAX_NAME_CHARS).
    ds_max_name_chars: int = 80
    # Trimmed length of a description, component blurb or chart/diagram note
    # (HUB_DS_MAX_DESCRIPTION_CHARS).
    ds_max_description_chars: int = 500
    # Trimmed length of a version note (HUB_DS_MAX_NOTE_CHARS).
    ds_max_note_chars: int = 500
    # Derived CSS/starter renders kept in the bounded LRU, keyed by
    # (id, version, mode) (HUB_DS_DERIVED_CACHE_ENTRIES).
    ds_derived_cache_entries: int = 64
    # chart_palette colours the public gallery shows per design system; the
    # strip is a taste of the palette, not the palette (HUB_DS_GALLERY_SWATCHES).
    ds_gallery_swatches: int = 6
    # Design systems the public gallery lists at once. One anonymous request
    # costs a version read and a token parse per row, so the newest N are
    # listed and the answer says it truncated (HUB_DS_GALLERY_MAX_ROWS).
    ds_gallery_max_rows: int = 100

    @property
    def ds_content_request_bytes(self) -> int:
        """Inbound body ceiling for the routes that carry a design bundle.

        Derived like :attr:`max_content_request_bytes`, so it can never drift
        below the bundle the hub already promises to accept.
        """
        return self.ds_max_bundle_bytes + REQUEST_ENVELOPE_SLACK_BYTES

    def token_limits(self) -> "TokenLimits":
        """The three bounds ``src.tokens`` validates a document under."""
        # Local import: src.tokens is a leaf module, but config is imported by
        # everything, and keeping the dependency one-directional at import time
        # means no module cycle can ever form here.
        from src.tokens import TokenLimits

        return TokenLimits(
            max_depth=self.ds_max_token_depth,
            max_tokens=self.ds_max_tokens,
            max_alias_depth=self.ds_max_alias_depth,
        )

    @property
    def max_content_request_bytes(self) -> int:
        """Inbound body ceiling for the routes that carry a document.

        Derived rather than configured on its own, so it can never drift below
        the documents the hub already promises to accept: whichever of
        ``max_html_bytes`` and ``max_envelope_bytes`` is larger, plus
        :data:`REQUEST_ENVELOPE_SLACK_BYTES` for the JSON envelope around it.
        Raising either content limit raises this with it.
        """
        return (
            max(self.max_html_bytes, self.max_envelope_bytes)
            + REQUEST_ENVELOPE_SLACK_BYTES
        )


def load_settings() -> Settings:
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )
    secret_key = os.environ["HUB_SECRET_KEY"]
    if len(secret_key) < MIN_SECRET_KEY_CHARS:
        raise RuntimeError(
            f"HUB_SECRET_KEY is too short ({len(secret_key)} characters). It "
            "is the HMAC key for every unlock cookie and must be at least "
            f"{MIN_SECRET_KEY_CHARS} characters of high-entropy random data. "
            "Generate one with: python -c "
            "'import secrets; print(secrets.token_urlsafe(48))'"
        )
    extra = tuple(
        s.strip().rstrip("/")
        for s in os.environ.get("HUB_EXTRA_STACKS", "").split(",")
        if s.strip()
    )
    destructive_policy = _destructive_policy_env("HUB_DESTRUCTIVE_TOKEN_POLICY")
    destructive_token_ids = _token_ids_env("HUB_DESTRUCTIVE_TOKEN_IDS")
    if destructive_policy == "allowlist" and not destructive_token_ids:
        # SEC-075-011: an empty allowlist is never what somebody meant. It
        # would refuse every destructive call, including the operator's own,
        # and look identical to a deliberate freeze — so it is a startup
        # error, not a very strict configuration.
        raise RuntimeError(
            "HUB_DESTRUCTIVE_TOKEN_POLICY=allowlist requires a non-empty "
            "HUB_DESTRUCTIVE_TOKEN_IDS (comma-separated Storage token ids). "
            "Read a token's id from GET {stack}/v2/storage/tokens/verify."
        )
    return Settings(
        hub_storage_token=os.environ["HUB_STORAGE_TOKEN"],
        hub_stack_url=os.environ["HUB_STACK_URL"].rstrip("/"),
        secret_key=os.environ["HUB_SECRET_KEY"],
        public_base_url=os.environ.get("HUB_PUBLIC_BASE_URL", "").rstrip("/") or None,
        demo_url=os.environ.get("HUB_DEMO_URL", "").strip() or None,
        design_demo_url=os.environ.get("HUB_DESIGN_DEMO_URL", "").strip() or None,
        cache_dir=Path(os.environ.get("HUB_CACHE_DIR", "/tmp/artifact-cache")),
        max_html_bytes=_int_env("HUB_MAX_HTML_BYTES", 15 * 1024 * 1024),
        max_inline_image_bytes=_int_env("HUB_MAX_INLINE_IMAGE_BYTES", 5 * 1024 * 1024),
        max_inline_total_bytes=_int_env("HUB_MAX_INLINE_TOTAL_BYTES", 15 * 1024 * 1024),
        git_clone_timeout_s=_int_env("HUB_GIT_CLONE_TIMEOUT_S", 90),
        git_max_repo_bytes=_int_env("HUB_GIT_MAX_REPO_BYTES", 200 * 1024 * 1024),
        git_allow_private_hosts=_bool_env("HUB_GIT_ALLOW_PRIVATE_HOSTS", False),
        cache_max_entries=_int_env("HUB_CACHE_MAX_ENTRIES", 200),
        unlock_cookie_max_age_s=_int_env("HUB_UNLOCK_COOKIE_MAX_AGE_S", 12 * 3600),
        token_verify_timeout_s=_int_env("HUB_TOKEN_VERIFY_TIMEOUT_S", 15),
        max_versions=_int_env("HUB_MAX_VERSIONS", 50),
        max_versions_per_day=_int_env("HUB_MAX_VERSIONS_PER_DAY", 20),
        diff_max_bytes=_int_env("HUB_DIFF_MAX_BYTES", 2 * 1024 * 1024),
        max_comments_per_day=_int_env("HUB_MAX_COMMENTS_PER_DAY", 100),
        reader_menu_default=_bool_env("HUB_READER_MENU_DEFAULT", True),
        extra_stacks=extra,
        max_envelope_bytes=_int_env("HUB_MAX_ENVELOPE_BYTES", 20 * 1024 * 1024),
        reap_aborted_publish_after_s=_int_env("HUB_REAP_ABORTED_PUBLISH_AFTER_S", 3600),
        max_proposed_versions=_int_env("HUB_MAX_PROPOSED_VERSIONS", 50),
        trust_forwarded_headers=_bool_env("HUB_TRUST_FORWARDED_HEADERS", False),
        trusted_proxy_cidrs=_cidr_env("HUB_TRUSTED_PROXY_CIDRS"),
        max_forwarded_chain_entries=_int_env("HUB_MAX_FORWARDED_CHAIN_ENTRIES", 16),
        destructive_token_policy=destructive_policy,
        destructive_token_ids=destructive_token_ids,
        max_unlock_attempts_per_hour=_int_env(
            "HUB_MAX_UNLOCK_ATTEMPTS_PER_HOUR", 30
        ),
        max_unlock_attempts_per_artifact_per_hour=_int_env(
            "HUB_MAX_UNLOCK_ATTEMPTS_PER_ARTIFACT_PER_HOUR", 500
        ),
        max_small_request_bytes=_int_env("HUB_MAX_SMALL_REQUEST_BYTES", 256 * 1024),
        lock_registry_max_entries=_int_env("HUB_LOCK_REGISTRY_MAX_ENTRIES", 1024),
        negative_lookup_cache_entries=_int_env(
            "HUB_NEGATIVE_LOOKUP_CACHE_ENTRIES", 4096
        ),
        state_snapshot_interval_s=_int_env("HUB_STATE_SNAPSHOT_INTERVAL_S", 300),
        state_max_snapshot_bytes=_int_env(
            "HUB_STATE_MAX_SNAPSHOT_BYTES", 50 * 1024 * 1024
        ),
        state_db_filename=(
            os.environ.get("HUB_STATE_DB_FILENAME", "").strip() or "state.sqlite3"
        ),
        instance_lock_filename=(
            os.environ.get("HUB_INSTANCE_LOCK_FILENAME", "").strip()
            or "instance.lock"
        ),
        webhook_timeout_s=_int_env("HUB_WEBHOOK_TIMEOUT_S", 10),
        webhook_max_attempts=_int_env("HUB_WEBHOOK_MAX_ATTEMPTS", 3),
        max_webhooks_per_artifact=_int_env("HUB_MAX_WEBHOOKS_PER_ARTIFACT", 5),
        webhook_queue_max=_int_env("HUB_WEBHOOK_QUEUE_MAX", 1000),
        webhook_key_overlap_s=_int_env("HUB_WEBHOOK_KEY_OVERLAP_S", 600),
        max_invitations_per_artifact=_int_env(
            "HUB_MAX_INVITATIONS_PER_ARTIFACT", 20
        ),
        export_max_bytes=_int_env("HUB_EXPORT_MAX_BYTES", 64 * 1024 * 1024),
        max_exports_per_hour=_int_env("HUB_MAX_EXPORTS_PER_HOUR", 20),
        ds_max_bundle_bytes=_int_env("HUB_DS_MAX_BUNDLE_BYTES", 2 * 1024 * 1024),
        ds_max_per_project=_int_env("HUB_DS_MAX_PER_PROJECT", 20),
        ds_max_versions=_int_env("HUB_DS_MAX_VERSIONS", 50),
        ds_max_versions_per_day=_int_env("HUB_DS_MAX_VERSIONS_PER_DAY", 20),
        ds_max_tokens=_int_env("HUB_DS_MAX_TOKENS", 5000),
        ds_max_token_depth=_int_env("HUB_DS_MAX_TOKEN_DEPTH", 16),
        ds_max_alias_depth=_int_env("HUB_DS_MAX_ALIAS_DEPTH", 32),
        ds_max_components=_int_env("HUB_DS_MAX_COMPONENTS", 100),
        ds_max_component_bytes=_int_env("HUB_DS_MAX_COMPONENT_BYTES", 64 * 1024),
        ds_max_guidance_bytes=_int_env("HUB_DS_MAX_GUIDANCE_BYTES", 256 * 1024),
        ds_max_palette=_int_env("HUB_DS_MAX_PALETTE", 12),
        ds_max_font_links=_int_env("HUB_DS_MAX_FONT_LINKS", 4),
        ds_font_hosts=_hosts_env("HUB_DS_FONT_HOSTS", "fonts.googleapis.com"),
        ds_max_name_chars=_int_env("HUB_DS_MAX_NAME_CHARS", 80),
        ds_max_description_chars=_int_env("HUB_DS_MAX_DESCRIPTION_CHARS", 500),
        ds_max_note_chars=_int_env("HUB_DS_MAX_NOTE_CHARS", 500),
        ds_derived_cache_entries=_int_env("HUB_DS_DERIVED_CACHE_ENTRIES", 64),
        ds_gallery_swatches=_int_env("HUB_DS_GALLERY_SWATCHES", 6),
        ds_gallery_max_rows=_int_env("HUB_DS_GALLERY_MAX_ROWS", 100),
        login_client_id=(
            os.environ.get("HUB_LOGIN_CLIENT_ID", "").strip() or "kbc-artifact-hub"
        ),
        login_timeout_s=_int_env("HUB_LOGIN_TIMEOUT_S", 20),
        login_pkce_ttl_s=_int_env("HUB_LOGIN_PKCE_TTL_S", 600),
        login_max_pending_pkce=_int_env("HUB_LOGIN_MAX_PENDING_PKCE", 64),
        max_logins_per_hour=_int_env("HUB_MAX_LOGINS_PER_HOUR", 30),
        max_login_polls_per_hour=_int_env("HUB_MAX_LOGIN_POLLS_PER_HOUR", 600),
    )
