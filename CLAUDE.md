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

## Storage model

Two kinds of Storage File back every artifact, both tagged for index rebuild:

- **Version files** `artifact-{id}-v{n}.json`, tagged `artifact-hub`,
  `artifact-id-{id}`, `artifact-ver-{n}`. One per submitted version; never
  overwritten.
- **Meta file** `artifact-{id}-meta.json`, tagged `artifact-hub`,
  `artifact-id-{id}`, `artifact-meta`, `artifact-owner-{key}`. Owner, password
  hash, `accept_versions`, head pointer.

On startup the in-memory index is rebuilt entirely by listing Storage Files
tagged `artifact-hub` (`ArtifactStore.hydrate` in `src/store.py`) — there is
no other source of truth. **If you add a field or a relationship the index
needs, it must be reconstructible from these tags/file contents alone; if it
isn't, that's a bug**, not a case to special-case around with local state.

## Secrets discipline

- Client Storage tokens and `git_token` are **transient, request-scope only**.
  Never persist them to Storage, disk, or an in-memory structure that outlives
  the request; never log them; never echo them back in a response or error.
- Every error path that can touch an authenticated URL (git clone output,
  subprocess stderr, etc.) must be scrubbed through `builder._scrub` before it
  reaches a log line, an exception message, or an API response.
- No tokens or secrets in git, ever — not in code, tests, fixtures, or commit
  messages.

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
| `src/pages.py` | Shared HTML shell + mini design system (light-first, JetBrains Mono headings/code + Inter prose, single accent color, graph-paper grid) for the landing page, unlock form, version picker, and any other human-facing page — reuse `pages._CSS` and its component patterns for new pages rather than inventing a new look |
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
  dev). Bump it, tag the commit, and cut a GitHub release for any
  user-visible change — don't let the reported `/health`/`/context` version
  drift from what's actually deployed.
