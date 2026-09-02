# CLAUDE.md — rules for AI contributors to this codebase

Project-specific nuances only. For what the service does and how to call it,
read `README.md` and `skills/artifact-publisher/SKILL.md` first.

## Runtime and pre-commit checks

- Runtime is **Python 3.11** (`.python-version`). Before any commit, both of
  these must pass:
  ```bash
  uv run pytest tests/ -q
  python3.11 -m py_compile src/*.py
  ```
- **f-string expressions must not contain backslashes** — this is a 3.11
  limitation (PEP 701's relaxed f-string grammar is 3.12+), and the
  interpreter that runs in production is 3.11, not whatever's on your PATH
  locally. `src/pages.py` already works around this once (search for
  "Hoisted out of the f-string" near line 508) — hoist the backslash-bearing
  expression (e.g. a `"\n".join(...)`) into a local variable before
  interpolating it, don't put it inline in `f"...{...}"`.
- Dependencies live only in `pyproject.toml`. Add/change them there and run
  `uv sync` — never `pip install` directly into the environment.

## Deployment shape: Keboola data app behind a proxy

The app runs as a Keboola Data App behind a platform proxy with three
consequences baked into `src/main.py`:

1. **The proxy strips `X-Kbc-*` request headers.** That's why the public
   auth header is `X-Storage-Stack`, not `X-Kbc-Stack` (the latter still
   works as an alias for direct/local access, since local `uvicorn` doesn't
   strip anything, but the deployed path only ever sees `X-Storage-Stack`).
2. **The proxy rewrites `Host` and terminates TLS**, forwarding real values
   via `X-Forwarded-Proto` / `X-Forwarded-Host`. Never build an absolute URL
   from the raw ASGI `Host` header or `request.url` directly — use
   `base_url(request)` (see `src/main.py`) or rely on the `public_origin`
   middleware, which normalizes `request.scope["scheme"]`/`Host` before
   routing so Starlette's own redirects and `url_for` are correct too.
3. **The platform POSTs `/` on startup** as its health check. Keep `POST /`
   returning 200 — do not remove or auth-gate it.
4. **The container has no permanent disk.** All durable state is Storage
   Files in the *host* project; anything written to local disk
   (`HUB_CACHE_DIR`) is an LRU cache only and must be safely rebuildable from
   Storage after a restart with an empty disk.
5. **Exactly one instance, ever.** One Keboola App is one organisation's hub,
   and a Data App is one container (single uvicorn process, no `--workers`,
   auto-suspend and restart — never two at once). This is a *deployment
   invariant*, not a scaling limit to engineer around: the in-memory artifact
   index, the per-process locks that serialize check-then-act mutations, the
   version-number allocator and the StateDB whole-snapshot cycle are all
   correct **only** under it. Do not add HA, replicas or `--workers`; if that
   ever changes, the state layer needs shared compare-and-swap, which Storage
   Files cannot provide (immutable, no create-if-absent) — that is a redesign,
   not a flag.

   The invariant is **enforced**, not merely documented (ARCH-100-001), and
   both halves must keep working:
   - `main.acquire_instance_lock` takes an exclusive non-blocking `flock` on
     `HUB_CACHE_DIR/instance.lock` in the lifespan and holds it for the life
     of the process. A second process on the same cache directory raises
     `SingleInstanceError` and fails to start. Do not move this after the
     stores are built, and do not make it best-effort.
   - `StateDB._retire_older_snapshots` catches the case the lock cannot see
     (two processes, two disks, one Storage project): it latches the
     process-wide flag in `src/statedb.py`, every `StateDB` write and snapshot
     then raises `ForeignWriterError` / no-ops, the foreign snapshot is left
     in place rather than deleted, and `GET /health` answers 503. `POST /`
     stays 200 (rule 3 above) — flipping it would only make the platform
     restart the container into the same conflict.

## Storage model

Two kinds of Storage File back every artifact, both tagged for index rebuild:

- **Version files** `artifact-{id}-v{n}.json`, tagged `artifact-hub`,
  `artifact-id-{id}`, `artifact-ver-{n}`. One per submitted version; never
  overwritten.
- **Meta file** `artifact-{id}-meta.json`, tagged `artifact-hub`,
  `artifact-id-{id}`, `artifact-meta`, `artifact-owner-{key}`. Owner, password
  hash, `accept_versions`, head pointer, and `version_high_water` — the
  highest version number ever allocated, written only when the newest
  version is deleted, so that number is never handed to new content after a
  restart (`ArtifactStore.delete_version` persists it *before* the file goes).

On startup the in-memory index is rebuilt entirely by listing Storage Files
tagged `artifact-hub` (`ArtifactStore.hydrate` in `src/store.py`) — there is
no other source of truth. **If you add a field or a relationship the index
needs, it must be reconstructible from these tags/file contents alone; if it
isn't, that's a bug**, not a case to special-case around with local state.

**Deleting an artifact deletes children first, the meta file strictly last**
(`ArtifactStore.delete`). The meta file is what authorizes the purge, so
removing it while any version/legacy file survives makes the leftover
unreachable *and* undeletable — the retry can no longer prove ownership. If
you add a new kind of per-artifact file, classify it as a child in
`ArtifactStore._classify_for_delete`. The other side of that ordering is that
a crash in the final gap leaves a meta record with no versions: that is a
legitimate, temporary state (inert publicly, still purgeable by the owner) and
`ArtifactStore._reap_aborted_publishes` clears it once it is too old to be an
operation still in flight.

## Known residual risks

Accepted, understood and deliberately not fixed. Add to this list rather than
rediscovering an item as a bug; each entry says why the fix is worse than the
risk and what an operator can do about it.

- **`REL-075-009` — a process death can orphan a canonical copy in the
  author's project.** Publishing writes a canonical HTML copy into the
  *caller's* own Keboola project (`_store_canonical` in `src/main.py`) before
  the hub's version file exists. Every caught failure afterwards deletes that
  copy while the caller's token is still in hand (`_discard_canonical`); the
  container *dying* in between cannot, because client tokens are request-scope
  only and the hub keeps no handle to another project's Storage. The window is
  process death alone — not anything an API caller can trigger. Reordering so
  the external copy is written last would close it, but `canonical_file_id`
  lives in the version envelope and version files are immutable (above), so
  that would mean rewriting a version file — a worse trade than the orphan,
  which is informational only: the id is echoed in the publish response and
  never resolved back to a file. **Operator remedy:** in the *author's* own
  project, list Storage files tagged `kbc-artifact` plus `artifact-id-{id}`
  and delete any whose artifact the hub does not serve.

## Secrets discipline

- Client Storage tokens and `git_token` are **transient, request-scope only**.
  Never persist them to Storage, disk, or an in-memory structure that outlives
  the request; never log them; never echo them back in a response or error.
- Every error path that can touch an authenticated URL (git clone output,
  subprocess stderr, etc.) must be scrubbed through `builder._scrub` before it
  reaches a log line, an exception message, or an API response.
- No tokens or secrets in git, ever — not in code, tests, fixtures, or commit
  messages.

## Known residual risks

These are documented, accepted design decisions from the v0.10.0 security
review — not bugs to silently "fix" by tightening code without a design
discussion. If you touch the areas below, preserve the documented behavior
and keep the README/comment cross-references intact.

- **`SEC-075-005` / `SEC-075-006` — resolver-to-connect DNS TOCTOU for
  outbound git and webhook traffic.** `_check_git_host` (`src/builder.py`)
  and the webhook URL validation (`src/webhooks.py`) resolve a hostname and
  reject private/loopback/link-local/reserved/metadata addresses, but git
  (via libcurl) and `httpx` each resolve the hostname again, independently,
  at connect time. Neither client supports pinning the connection to an
  already-validated IP, so this cannot be closed in application code with
  the current clients — it needs an egress policy/proxy in front of the
  container (see the "Network egress" part of README.md's *Security model*).
  Do not attempt to "fix" this with more application-side re-checks; another
  re-check only narrows the window, it does not close it.
- **`SEC-075-011` — project-wide destructive authority is the *default*, not
  the only option.** `require_owner`/`_owner_only` (`src/auth.py`,
  `src/main.py`) resolve ownership as a `(stack, project)` pair and
  deliberately still do: identity is the project. What used to follow from
  that — every token of the project, read-only ones included, could purge —
  is now an opt-in policy, `HUB_DESTRUCTIVE_TOKEN_POLICY`
  (`project` | `admin` | `allowlist`, **default `project`**, i.e. the
  historical behaviour), enforced by `_destructive_authority` in
  `src/main.py` on the destructive routes only: soft delete, purge, owner
  version delete, rotate-link and webhook key rotation. `Owner` carries the
  non-secret token claims the policy reads (`token_id`, `is_master_token`,
  `admin_role`, `can_purge_trash`, `can_manage_tokens`), parsed defensively —
  a missing or odd-shaped claim degrades to least privilege and never raises,
  and none of them is ever persisted or logged. **Do not extend the gate to
  non-destructive owner routes** (update, head pin, promote, settings,
  invitations, stats, trash restore) or to a contributor withdrawing their
  own proposal; that split is the design, documented in README.md's *Security
  model*, and `tests/test_review100_destructive_policy.py` pins it.
- **`SEC-100-005` — git redirects are disabled, not eliminated as an SSRF
  vector.** `_clone` (`src/builder.py`) runs with
  `-c http.followRedirects=false`, so a redirect to an unvalidated host now
  fails the clone instead of being silently followed. This closes the
  redirect-specific gap; the underlying resolver TOCTOU above it is not
  closed by this and is tracked separately as `SEC-075-006`.

## Architecture map

| Module | Responsibility |
|---|---|
| `src/config.py` | Env-driven `Settings`; required vars fail fast at startup (no invented defaults) |
| `src/auth.py` | Stack alias resolution + allowlist, `verify_token` against a stack's `/v2/storage/tokens/verify`, returns `Owner` |
| `src/kbc.py` | `FilesBackend` protocol; `KbcFilesBackend` (real, via `kbcstorage`) and `InMemoryFilesBackend` (tests) |
| `src/store.py` | `ArtifactStore`: version/meta envelope build & parse, publish/get/list/delete, startup `hydrate()`, disk LRU cache |
| `src/builder.py` | Markdown → HTML rendering (mermaid, tables, highlight.js), git clone + entry-file resolution + image inlining, `_scrub` for credential redaction |
| `src/diff.py` | Unified + side-by-side diff rendering (stdlib `difflib`, no new deps) |
| `src/security.py` | PBKDF2-SHA256 password hashing, signed unlock cookies (`itsdangerous`) |
| `src/pages.py` | Shared HTML shell + mini design system (light-first, JetBrains Mono headings/code + Inter prose, single accent color, graph-paper grid) for the landing page, unlock form, version picker, and any other human-facing page — reuse `pages._CSS` and its component patterns for new pages rather than inventing a new look. **A page that holds a credential includes `_SESSION_JS` and goes through `window.hubSession`** — reading and writing the one `sessionStorage` entry, the header each credential kind belongs in, renewal (deduplicated: the stack spends the refresh token on the first exchange) and revocation live there and nowhere else. `/admin`, `/a/{id}/review` and `/login` each had their own copy and the copies drifted; a fourth would too |
| `src/main.py` | FastAPI app: middleware (`public_origin`, `artifact_headers`), all routes, `/context` manifest |
| `tests/` | One test module per `src/` module (`test_auth.py`, `test_builder.py`, `test_store.py`, `test_diff.py`, `test_security.py`), plus `test_api.py` for route-level integration tests; `conftest.py` wires shared fixtures |

## Testing conventions

- Use `InMemoryFilesBackend` (`src/kbc.py`) in place of real Keboola Storage
  in every test — never call real Storage from the test suite.
- Patch `verify_token` (via `respx` or `monkeypatch`) rather than hitting a
  live stack.
- No live Keboola calls anywhere in `tests/`, full stop.

## Contributing changes: pull requests, not direct pushes

- **Never commit to `main` directly.** Branch, push the branch, open a PR
  with `gh pr create`, and let the PR carry the change onto `main`. This
  holds for every change, including a one-line fix and a release bump.
- Branch names describe the change, not the author: `fix/...`, `feat/...`,
  `docs/...`, `chore/...`.
- A PR body says what changed and, more importantly, **why** — the problem
  it solves, not a restatement of the diff. Note anything a reviewer should
  check by hand.
- The pre-commit checks in this file are pre-*PR* checks too: `uv run pytest
  tests/ -q` fully green and `python3.11 -m py_compile src/*.py` clean before
  you open it.
- A release still means bumping `[project].version`, tagging the **merge
  commit on `main`**, cutting the GitHub release and deploying — in that
  order, after the PR is merged. Never tag a branch.

## Deploy flow

- Push to GitHub `main` (`padak/kbc_ai_artifact`), then:
  ```bash
  kbagent data-app deploy --project artifacts --app-id 1304628444 --wait
  ```
- Secrets: `kbagent data-app secrets-set --secrets-file <file>` — never commit
  a secrets file; `HUB_STORAGE_TOKEN`, `HUB_STACK_URL`, `HUB_SECRET_KEY` are
  required and must never appear in tracked files.
- Logs: `kbagent data-app logs --project artifacts --app-id 1304628444`.
- **Version single source of truth is `pyproject.toml`** (`[project].version`,
  read at runtime via `importlib.metadata` with a `tomllib` fallback for local
  dev). Bump it and push a `vX.Y.Z` tag for any user-visible change — don't
  let the reported `/health`/`/context` version drift from what's actually
  deployed. **The release itself is made by CI** (`.github/workflows/release.yml`,
  on the tag push): it refuses a tag that disagrees with `pyproject.toml` or
  the changelog head, runs the suite, then creates the GitHub release with
  `AGENT.md`, `SKILL.md` and `SHA256SUMS` as assets and attests them
  (`gh attestation verify <file> --repo padak/kbc_ai_artifact`). Users
  install the agent from those assets, never from the live `/agent`; keep
  that true in the docs. Edit the release title/notes afterwards if needed,
  don't create the release by hand first.
