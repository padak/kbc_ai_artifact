# KBC Artifact Hub

[![repo](https://img.shields.io/badge/github-padak%2Fkbc__ai__artifact-1442e0)](https://github.com/padak/kbc_ai_artifact)
[![releases](https://img.shields.io/badge/releases-latest-1442e0)](https://github.com/padak/kbc_ai_artifact/releases)
[![python](https://img.shields.io/badge/python-3.11%2B-1442e0)](https://www.python.org/)

Source, issues and tagged releases live at
<https://github.com/padak/kbc_ai_artifact> — the deployed service reports the
version it is running at `GET /health` and `GET /context`, which is the single
source of truth (read from the installed package metadata, itself derived from
`pyproject.toml`).

A Keboola App (FastAPI) that hosts self-contained HTML/Markdown artifacts
under public, unguessable URLs. Anyone holding **any** Keboola Storage API
token, on **any** Keboola stack, can publish a document; the service returns
a public URL that humans can open in a browser and machines can fetch as raw
content, source, or metadata over a small JSON API.

## Features

- Publish HTML, Markdown, or a git repository (public, or private via a
  transient access token) as a hosted artifact
- **Sign in instead of pasting a token** (`/login`): Keboola's device-code
  flow works anywhere, PKCE when the hub runs on loopback — the session is
  used on `/api/*` exactly like a Storage token
- Unguessable capability URLs (`token_urlsafe`, 24 chars) — no public listing,
  `X-Robots-Tag: noindex` on every artifact response
- Optional password protection, with a web unlock form and a machine header
- **Community versioning**: every update adds a version instead of overwriting,
  and `accept_versions` lets other Keboola projects submit versions of your
  artifact
- **Moderated proposals**: a submission from another project lands as a
  proposal whose content only you and its author can read, until you promote it
- **Diffs**: side-by-side HTML, unified text, or JSON with add/remove counts —
  standard library only, no new dependencies
- **Head pointer**: `/a/{id}` serves the newest live version, or one you pin
- Machine-readable API: `/context` manifest and a `/skill` SKILL.md an AI
  agent can read to learn how to publish and contribute, unassisted
- **Admin studio** (`/admin`): a browser moderation UI where an artifact's
  owner pastes their Storage token client-side (kept in `sessionStorage`,
  never sent to or stored by the server) to review proposal diffs, promote or
  reject them, pin the head version, and toggle `accept_versions` — all by
  clicking
- **Installable agent** (`/agent`): serves the Claude Code subagent
  definition this hub runs, distilled from `/skill`; install the attested
  copy from the matching GitHub release (see *Install the agent / skill*)
- **Inline comments and a review UI**: anyone with a Keboola token can leave
  threaded comments anchored to a quoted passage of a specific version
  (`GET/POST /a/{id}/comments`), and `GET /a/{id}/review` is a browser page
  where selecting text opens a comment composer — the artifact renders in a
  sandboxed iframe so its own scripts never see the reviewer's token
- **Markdown export** (`GET /a/{id}/export/markdown`): always Markdown — the
  author's own source when the version has one (published as `markdown`, or as
  `html` with `markdown_source`), otherwise converted from the HTML document,
  which keeps headings, tables, links, code and mermaid fences but degrades
  charts and images to a placeholder line. The
  `X-Artifact-Markdown-Source: original|converted` header reports which
- **Obsidian vault export** (`GET /a/{id}/export/vault`): a ZIP containing a
  ready-to-open vault — an `INDEX.md` hub, one note per version with its diff,
  one note per comment thread, and a chronological `reasoning.md` timeline —
  so Obsidian's own graph view becomes the artifact's knowledge graph. It is
  the most expensive request this service serves, so it is budgeted: an
  artifact whose visible history exceeds `HUB_EXPORT_MAX_BYTES` is refused
  with 413, one client may build `HUB_MAX_EXPORTS_PER_HOUR` vaults of one
  artifact per hour, and the archive is streamed through an owner-only
  temporary file rather than assembled in memory
- **Contributor allowlist**: `accept_versions_mode` and `comments_mode` can
  each be `off` / `anyone` / `allowlist`, restricting who may submit versions
  or comment to a `contributors` list of Keboola projects
- **Final status**: an owner can mark an artifact `final`, freezing new
  versions and comments for everyone (including the owner) until it is
  reopened
- **Trash, restore, and permanent purge**: `DELETE /api/artifacts/{id}` is a
  soft delete (public link dies, everything else is kept and restorable);
  `POST .../restore` brings it back on the same URL; `DELETE .../purge` is the
  separate, irreversible one
- **Link rotation**: `POST /api/artifacts/{id}/rotate-link` mints a fresh
  public share id and kills the old link instantly — the way to revoke a URL
  that was shared with the wrong person
- **`base_version` and staleness detection**: a submitted version can declare
  which version it was written against; `GET /a/{id}/versions` flags a
  proposal `outdated` when the head has moved on since, so reviewers know
  when a proposal's own diff no longer tells the whole story
- **Outbound webhooks**: register up to `HUB_MAX_WEBHOOKS_PER_ARTIFACT` https
  URLs per artifact and get a signed (`X-Hub-Signature-256`) JSON POST — or a
  formatted Slack message for a `hooks.slack.com` URL — on every version,
  comment, finalize, trash/restore and link-rotation event. Each receiver's
  key can be rotated independently of its URL (`POST .../webhooks/{receiver_id}
  /rotate-key`), with a short signed-overlap grace period so an in-flight
  delivery still verifies
- **Guest invitations**: `POST /api/artifacts/{id}/invitations` mints a named,
  revocable capability that lets one person with no Keboola account comment
  through the review UI, secret carried in the URL fragment and never logged
- **View statistics**: `GET /api/artifacts/{id}/stats` reports read counts by
  day and by surface (page/raw/source/version) for the artifact's owner
- **Visual diff**: `GET /a/{id}/diff/{a}..{b}?format=visual` renders both
  versions side by side in synced-scroll sandboxed iframes, for comparing what
  a reader actually sees rather than the underlying source
- Markdown rendering with GFM tables, task lists, mermaid diagrams, and
  syntax-highlighted code
- Survives restarts: the only durable state is Keboola Storage Files; local
  disk is a cache, not a source of truth
- Interactive API docs at `/docs` (Swagger UI) and a machine-readable schema
  at `/openapi.json`

## Architecture

**Publish flow.** A client sends content (`html`, `markdown`, or `git_url`)
plus a Storage token and stack to `POST /api/artifacts`. Publishers are asked
to always make a Markdown version available — publish `markdown` and let the
hub render it, or, when raw `html` is genuinely needed, send the optional
`markdown_source` alongside it (valid only with `html`; it does not change what
is rendered). Agents read documents through `/a/{id}/export/markdown` and
`/a/{id}/source`, and Markdown converted from HTML is lossy for charts and
images. The service verifies
the token against the caller's own stack to establish project identity, then
builds the final HTML (rendering Markdown or cloning+resolving a git repo as
needed). Two copies are written:

- a **canonical copy** of the built HTML as a Storage File in the **author's
  own project**, uploaded with the author's token, tagged `kbc-artifact` and
  `artifact-id-<id>`;
- a **version envelope** (HTML + source + verified author + status, as JSON) as
  a Storage File in the **host project**, named `artifact-<id>-v<n>.json` and
  tagged `artifact-hub`, `artifact-id-<id>`, `artifact-ver-<n>`, alongside an
  artifact-level **meta record** `artifact-<id>-meta.json` (owner, password
  hash, `accept_versions`, head pointer) tagged `artifact-hub`,
  `artifact-id-<id>`, `artifact-meta`, `artifact-owner-<key>`.

Updates never overwrite a version: each one uploads the next version file. A
submission from a project other than the owner is stored with status
`proposed` and is not served until the owner promotes it. Legacy single-file
envelopes from before versioning are read as version 1 and migrated on the
next write.

The service never persists client tokens; they are used only for the
duration of the request. The same holds for an optional `git_token` used to
clone a private repository: it lives only in the clone subprocess argument,
and git's output is scrubbed of credentials before any of it reaches a log
line or an error message.

**Read flow.** `GET /a/{id}` (and `/raw`, `/source`, `/meta`, `/v/{n}`,
`/versions`, `/diff/{a}..{b}`) is served from an in-memory index that maps
artifact ids to their version and meta files, backed by a disk LRU cache.
`/a/{id}` resolves the *head* — the newest live version, or the one the owner
pinned — and, since 0.6.0, `id` in that path is the artifact's **share id**:
equal to its internal id until the owner rotates the link, after which only
the new share id resolves publicly. On startup, since the app container has
no permanent disk, the service rebuilds this index from scratch by listing
every Storage File tagged `artifact-hub` in the host project — the Storage
Files of the host project are the single source of truth, and local disk
holds only a cache of recently-served envelopes.

**Rendering.** `/a/{id}` and `/a/{id}/v/{n}` serve the artifact inside a
`srcdoc` iframe sandboxed without `allow-same-origin` (0.6.0): the published
document's own scripts run in an opaque origin and can never reach the hub's
own origin, where `/admin` and `/review` keep a visitor's Storage token in
`sessionStorage`. `/a/{id}/raw` is unaffected — it stays the byte-exact
document for machine clients that just want the HTML.

## Project brain workflow

A published artifact is not just a document to link — it is one URL a whole
team of humans and AI agents can collaborate around. An agent publishes a
first draft; teammates and other agents open it, leave inline comments on the
specific passages they have questions about, or submit whole proposed
versions with a note explaining the change; anyone can fetch `/meta`,
`/versions`, and `/comments` to catch up on what has already been said before
adding their own point, so the discussion converges instead of repeating
itself. The owner reviews diffs and threads at `/admin` and `/review`,
promotes or rejects proposals, and — once the document is settled — marks it
`final`, freezing further changes for everyone — which every describing
payload reports as `document_status: "final"` and
`contributions_frozen: true`, so a contributor can check before it writes.
At that point `/export/vault` turns the whole history (every version,
every thread, every decision) into a
ready-to-open Obsidian vault: a permanent, browsable record of how the
document got to where it is, with nothing to reconstruct from memory.

## API reference

Public (no auth):

| Method | Path | Description |
|---|---|---|
| GET | `/` | Landing page (docs) |
| POST | `/` | Returns 200 (platform health check) |
| GET | `/context` | Machine-readable manifest |
| GET | `/skill` | SKILL.md (`text/markdown`) teaching agents how to publish |
| GET | `/agent` | The Claude Code subagent definition this hub runs (`text/markdown`, with `ETag`/`X-Content-SHA256`/`X-Hub-Version`); install the attested release copy instead |
| GET | `/admin` | Browser moderation studio (the visitor's credential is client-side only; never stored server-side) |
| GET | `/login` | Sign in to any allowed Keboola stack — device code everywhere, PKCE on a loopback hub |
| POST | `/login/device` | Start a device-code sign-in `{stack}` → `{user_code, verification_uri_complete, interval, ...}` |
| POST | `/login/device/token` | Poll it `{stack, device_code}` → `{"status": "pending"}` or `{"status": "ok", credential, projects}` |
| GET | `/login/pkce/start` | `?stack=` → redirect to the stack's authorization screen (404 unless the hub is on loopback) |
| GET | `/login/callback` | PKCE callback; finishes the sign-in and re-serves `/login` on its project-picking step |
| POST | `/login/refresh` | Renew a session `{stack, refresh_token}` |
| POST | `/login/signout` | Revoke a session on its stack `{stack, token}` |
| GET | `/docs` | Interactive Swagger UI for this API |
| GET | `/openapi.json` | Machine-readable OpenAPI schema for this API |
| GET | `/a/{id}` | Head version rendered in a sandboxed iframe, or the password unlock form |
| POST | `/a/{id}/unlock` | Password form target; sets a signed unlock cookie |
| GET | `/a/{id}/v/{n}` | One specific version (owner/author only when proposed) |
| GET | `/a/{id}/versions` | Version history JSON (each row's `status` is that version's `live`/`proposed`), plus the document-level `document_status` / `contributions_frozen` / `accept_versions_mode`; proposed rows flagged `outdated`; `?format=html` renders a picker page |
| GET | `/a/{id}/diff/{a}..{b}` | Diff two versions; `?format=html\|unified\|json\|visual` |
| GET | `/a/{id}/raw` | Raw built HTML, byte-exact, no iframe (password via `X-Artifact-Password` if protected) |
| GET | `/a/{id}/source` | Original submitted source, never converted (the author's Markdown when the version has one, else the HTML) |
| GET | `/a/{id}/meta` | Public metadata JSON (no owner details); `status` is the head **version's** (`live`/`proposed`), `document_status` is the **document's** (`draft`/`final`), with `contributions_frozen` and `accept_versions_mode` |
| GET | `/a/{id}/comments` | Every inline comment thread (open and resolved) as JSON, plus `comments_mode` and the document status as `document_status` (also as the legacy `status`, which on this endpoint has always meant the document) |
| GET | `/a/{id}/guest` | Resolve an `X-Artifact-Guest` credential to its display name |
| GET | `/a/{id}/review` | Browser review UI: select text to comment, sandboxed artifact iframe; also the guest entry point via a `#invite=` fragment |
| GET | `/a/{id}/export/markdown` | Head version as Markdown: the author's own source when there is one, otherwise converted from the HTML document (lossy for charts and images). `X-Artifact-Markdown-Source: original\|converted` reports which |
| GET | `/a/{id}/export/vault` | ZIP of a ready-to-open Obsidian vault (versions, comments, reasoning timeline), streamed rather than held in memory; 413 above `HUB_EXPORT_MAX_BYTES`, 429 above `HUB_MAX_EXPORTS_PER_HOUR` |
| GET | `/changelog` / `/changelog.md` | Rendered changelog (hub's own design) / raw source |
| GET | `/health` | Liveness check + service version + index stats |

**Authorization is per project, by design.** Ownership is `(stack, project)`:
any valid credential from the owning project carries full owner authority
— update, trash, restore, purge, rotate-link, promote — regardless of that
token's own scope. A Keboola project *is* the team, and one hub is one
organisation, so project membership is the intended boundary; the hub does
not layer roles of its own on top. Keep destructive tokens as safe as you
keep the project itself.

Authenticated (`X-StorageApi-Token` or `Authorization: Bearer`, plus
`X-Storage-Stack`, plus `X-Storage-Project` when the credential is a sign-in
bearer):

| Method | Path | Description |
|---|---|---|
| POST | `/api/artifacts` | Publish `{html[, markdown_source] \| markdown \| git_url[, git_ref, git_path, git_token, git_username], title?, password?, accept_versions?}` → `{id, version, head_version, url, raw_url, meta_url, versions_url, ...}` |
| PUT | `/api/artifacts/{id}` | Add a live version and/or change `password` / `clear_password` / `accept_versions_mode` / `contributors` / `comments_mode` / `status` / `webhooks` (owner project only) |
| GET | `/api/artifacts` | List the caller's project's own artifacts (trashed ones included, with `webhooks_count`); each row's `status` is the document's, mirrored as `document_status` with a derived `contributions_frozen` |
| DELETE | `/api/artifacts/{id}` | **Soft delete**: move to the trash — public link dies, everything is kept and restorable (owner project only) |
| POST | `/api/artifacts/{id}/restore` | Undo the soft delete: back on the same share id, same status as before (owner project only) |
| DELETE | `/api/artifacts/{id}/purge` | **Permanent** delete: erase every version, comment thread and the meta record — no undo (owner project only) |
| GET | `/api/artifacts/{id}/webhooks` | List each registered receiver with the key its deliveries are signed with (owner project only) |
| POST | `/api/artifacts/{id}/webhooks/{receiver_id}/rotate-key` | Mint a fresh signing key for one receiver without touching its URL; the previous key still verifies for `webhook_key_overlap_s` seconds (owner project only) |
| POST | `/api/artifacts/{id}/rotate-link` | Mint a fresh share id; the old link (and the bare internal id) stop resolving immediately (owner project only) |
| GET | `/api/artifacts/{id}/stats` | View counts: `total`, `by_day` (last 30 UTC days), `by_kind` (owner project only) |
| POST | `/api/artifacts/{id}/invitations` | Invite a guest to comment `{name}` → one-time `review_url` with the secret in the URL fragment (owner project only) |
| GET | `/api/artifacts/{id}/invitations` | List an artifact's guest invitations, no secrets (owner project only) |
| DELETE | `/api/artifacts/{id}/invitations/{iid}` | Revoke one invitation; everyone else's keeps working (owner project only) |
| POST | `/api/artifacts/{id}/versions` | Submit a version `{html[, markdown_source] \| markdown \| git_url, title?, note?, base_version?}` — live for the owner, proposed for any other project (409 when `status` is `final` or trashed) |
| POST | `/api/artifacts/{id}/versions/{n}/promote` | Promote a proposal to live (owner project only) |
| DELETE | `/api/artifacts/{id}/versions/{n}` | Delete a version (owner), or withdraw your own proposal (contributor). 409 for the last live version, and for the version the head is pinned to — re-pin or switch the head to `latest` first |
| PUT | `/api/artifacts/{id}/head` | `{"mode": "latest"}` or `{"mode": "pinned", "version": n}` (owner project only) |
| POST | `/api/artifacts/{id}/comments` | Open a comment thread `{version, exact, prefix, suffix, body}` (403 if closed/allowlisted, 409 if frozen, 429 past the daily cap); also accepts an `X-Artifact-Guest` credential in place of a Storage token |
| POST | `/api/artifacts/{id}/comments/{tid}/replies` | Reply to a thread `{body}` (guest credential accepted here too) |
| POST | `/api/artifacts/{id}/comments/{tid}/resolve` | Resolve or reopen a thread `{"resolved": true \| false}` (owner, thread author, or the guest who opened it) |
| DELETE | `/api/artifacts/{id}/comments/{tid}` | Delete a thread (owner, thread author, or the guest who opened it) |

Every version, comment/reply, finalize, trash/restore and link-rotation event
also fires any webhooks the artifact has registered (`X-Hub-Signature-256`
HMAC-signed JSON, keyed per receiver, or Slack's `{"text": ...}` shape for a
`hooks.slack.com` URL) — see *Outbound webhooks* above.

## Signing in instead of finding a token

`X-StorageApi-Token` takes either credential:

| Credential | Extra header | Where it comes from |
|---|---|---|
| Storage API token, in `X-StorageApi-Token` | none — it names its own project | the project's Storage settings |
| `kbc_at_*` session / `kbc_pat_*` personal access token, in `Authorization: Bearer` | `X-Storage-Project: <id>` | signing in (below), or the Keboola UI for a PAT |

Each kind goes in the header that kind belongs in, the same split a Keboola
stack uses: a Storage API token in `X-StorageApi-Token`, a sign-in in
`Authorization: Bearer`. Putting one in the other's header is a 400 naming the
right one, and so is sending both at once.

A bearer is scoped to a *person*, so it has to say which project it is acting
as; a Storage token already knows.

**What a sign-in can reach is decided on Keboola's screen, not here.** A
`kbc_at_*` session authorizes against every project its approval covered;
`X-Storage-Project` only selects which of those a given call acts as. The
PKCE flow therefore asks the stack for its project picker by default, and the
device flow's approval page offers the same choice — narrow it there if the
session should not span your whole account. The hub's own project step after
sign-in is a convenience, not a boundary.

On a deployed hub, `GET /health/headers` reports the header names that
actually reached the app — that is how you confirm the platform proxy in front
of it forwards `Authorization`. Everything else — ownership, moderation
rights, the canonical copy — is identical, because both resolve to the same
`(stack, project)` identity through the stack's own
`GET /v2/storage/tokens/verify`.

Every management route takes all three shapes — publishing, versioning,
moderation, comments, invitations, webhooks. The one capability difference is
the canonical copy: publishing writes a Storage File into *your* project, so a
**read-only** personal access token verifies fine and then fails that write
with a 502 that says so. Reading, moderating and commenting are unaffected.

**In a browser:** open `/login`, pick the stack, approve, pick a project. The
credential lands in the same `sessionStorage` entry `/admin` and
`/a/{id}/review` already read, so a pasted token and a sign-in are
interchangeable to every page.

**From a terminal or an agent:** drive the device flow, which needs no
callback URL and works from anywhere.

```bash
curl -s -X POST "$HUB/login/device" -H "Content-Type: application/json" \
  -d '{"stack": "eu"}'
# -> {"device_code": "...", "user_code": "ABCD-EFGH",
#     "verification_uri_complete": "https://.../admin/auth/device?userCode=...",
#     "expires_in": 900, "interval": 5}

# Approve that URL in a browser, meanwhile poll every "interval" seconds:
curl -s -X POST "$HUB/login/device/token" -H "Content-Type: application/json" \
  -d '{"stack": "eu", "device_code": "..."}'
# -> {"status": "pending", "interval": 5}
# -> {"status": "ok", "credential": {"access_token": "kbc_at_...", ...},
#     "projects": [{"id": 123, "name": "Test", "role": "admin"}]}
```

A second flow, authorization code + PKCE, is one browser hop with nothing to
type — but Keboola only accepts an `http://127.0.0.1:{port}/{path}` redirect
for it, so `/login` offers it exactly when the hub answers on loopback (a hub
you run yourself). A hosted hub answers 404 on `/login/pkce/start` and uses
the device code.

The hub makes these calls on the visitor's behalf because a browser cannot: a
Keboola stack sends no CORS headers for another origin. It relays the result
and keeps nothing — no server-side session, no stored token. Access tokens
last an hour and `/login/refresh` rotates them; `/login/signout` revokes the
session on the stack rather than only forgetting it locally.

Both flows need Keboola's programmatic auth enabled on the stack. Where it is
off, `/login/device` answers 502 saying so, and a Storage API token remains
the way in.

## Quick start (curl)

Set `$HUB` to the deployed base URL, put your credential and your stack alias
(`us`, `gcp-us`, `eu`, `azure-eu`, `gcp-eu`, or any full
`https://*.keboola.com` URL) in the environment, and define `hub` — a
one-line wrapper that hands curl the auth headers through a process
substitution, so the credential is never a command-line argument visible in
`ps`:

```bash
export KBC_TOKEN="…"        # a Storage API token, or a kbc_at_/kbc_pat_ one
export KBC_STACK=eu
export KBC_PROJECT=         # the project id, only for a sign-in credential
hub() {
  curl -s -K <(
    printf 'header = "X-StorageApi-Token: %s"\nheader = "X-Storage-Stack: %s"\n' "$KBC_TOKEN" "$KBC_STACK"
    [ -n "$KBC_PROJECT" ] && printf 'header = "X-Storage-Project: %s"\n' "$KBC_PROJECT"
  ) "$@"
}
```

A Storage token names its own project, so `KBC_PROJECT` stays empty for one;
a sign-in from `/login` is scoped to a person and needs it. A second identity
(a contributing project) is just a different credential for one call:
`KBC_TOKEN="$CONTRIBUTOR_TOKEN" hub …`.

```bash
# Publish HTML
hub -X POST "$HUB/api/artifacts" \
  -H "Content-Type: application/json" \
  -d '{"html": "<!doctype html><html><body><h1>Hello</h1></body></html>", "title": "My report"}'

# Publish Markdown
hub -X POST "$HUB/api/artifacts" \
  -H "Content-Type: application/json" \
  -d '{"markdown": "# Title\n\nSome content."}'

# Publish from a public git repo
hub -X POST "$HUB/api/artifacts" \
  -H "Content-Type: application/json" \
  -d '{"git_url": "https://github.com/org/repo", "git_ref": "main", "git_path": "docs/report.md"}'

# Read (public, no token)
hub "$HUB/a/<id>/raw"

# Update (owner project only)
hub -X PUT "$HUB/api/artifacts/<id>" \
  -H "Content-Type: application/json" \
  -d '{"markdown": "# Updated"}'

# Delete (owner project only)
hub -X DELETE "$HUB/api/artifacts/<id>"
```

Add `"password": "secret"` to any publish/update body to protect the
artifact; readers then need `X-Artifact-Password: secret` (machines) or the
web unlock form (browsers).

Publishing from a private git repo needs a `git_token` (a PAT for the git
host), which is transient — see *Security model* below. Never splice it into
a literal `-d` string; that puts the value in `curl`'s own argv for the
process's lifetime, the same problem `hub` above avoids for the Storage
token. Build the body with `jq` instead, reading the token from its own
environment via `env.GIT_TOKEN` — only the variable *name* appears in the
program text — and pipe the JSON to curl on stdin:

```bash
export GIT_TOKEN="…"   # a PAT for the git host, read-only / single-repo scope
jq -n --arg url "https://github.com/org/private-repo" \
      --arg path "docs/report.md" \
      '{git_url: $url, git_path: $path, git_token: env.GIT_TOKEN}' \
  | hub -X POST "$HUB/api/artifacts" \
      -H "Content-Type: application/json" \
      --data-binary @-
```

### Versioning (curl)

```bash
# Open an artifact to submissions from other projects
hub -X PUT "$HUB/api/artifacts/<id>" \
  -H "Content-Type: application/json" \
  -d '{"accept_versions": true}'

# Submit a version (any project; live for the owner, proposed for others)
KBC_TOKEN="$CONTRIBUTOR_TOKEN" hub -X POST "$HUB/api/artifacts/<id>/versions" \
  -H "Content-Type: application/json" \
  -d '{"markdown": "# Updated", "note": "fix the totals table"}'

# Review: history, one version, and the diff
hub "$HUB/a/<id>/versions"
hub "$HUB/a/<id>/v/2"
hub "$HUB/a/<id>/diff/1..2?format=unified"

# Promote a proposal (owner project only)
hub -X POST "$HUB/api/artifacts/<id>/versions/2/promote"

# Pin the head to one live version, or go back to "latest"
hub -X PUT "$HUB/api/artifacts/<id>/head" \
  -H "Content-Type: application/json" \
  -d '{"mode": "pinned", "version": 1}'

# A pinned version cannot be deleted (409) — the head would be left naming a
# version that no longer exists. Re-pin, or go back to "latest", then delete.

# Withdraw your own proposal (contributor project)
KBC_TOKEN="$CONTRIBUTOR_TOKEN" hub -X DELETE "$HUB/api/artifacts/<id>/versions/2"
```

`GET /a/{id}/versions?format=html` renders the same history as a styled page
with status badges and a diff link for every adjacent pair.

### Private repositories

`git_token` is a personal access token for the git host (GitHub PAT, GitLab
token, …); the optional `git_username` defaults to `x-access-token`, which is
what GitHub PATs and GitLab deploy tokens expect. Both are only valid
together with `git_url` — sending either on its own is a 422.

The token is **transient, exactly like the Storage token**: it is injected
into the clone URL for the duration of that one `git clone` subprocess and
nothing else. It is never written to the stored envelope (the recorded
`source.git` holds only the unauthenticated `url`, `ref`, `path` and
`commit`), never logged, never included in an error message, and never
returned in a response — so a later `PUT` re-publishing from the same private
repository has to send it again. Use the narrowest scope possible and revoke
the token when it is no longer needed. Note that the published artifact is
still served from a public URL: the token protects the clone, not the result.

## Install the agent / skill

Two files teach an AI agent this API: the Claude Code subagent (`AGENT.md`,
self-contained) and the skill (`SKILL.md`). The hub serves the copies it runs
at `/agent` and `/skill` — those are for *reading*. **Install from the
release**, whose copies are attested: `gh attestation verify` proves a file
was built by this repository's release workflow from the tagged commit, which
no download from the hub itself can prove. The installed subagent grants
Bash/Read/WebFetch authority, so that proof is the point.

```bash
V=$(curl -fsSL "$HUB/health" | jq -r .version)          # the release this hub runs
D=$(mktemp -d)
gh release download "v$V" --repo padak/kbc_ai_artifact -p AGENT.md -p SKILL.md -p SHA256SUMS -D "$D"
( cd "$D" && shasum -a 256 -c SHA256SUMS )                                 # integrity against the release
gh attestation verify "$D/AGENT.md" --repo padak/kbc_ai_artifact           # provenance: this repo's release workflow
gh attestation verify "$D/SKILL.md" --repo padak/kbc_ai_artifact
install -d ~/.claude/agents && install -m 0644 "$D/AGENT.md" ~/.claude/agents/artifact-hub.md
install -d ~/.claude/skills/artifact-publisher && install -m 0644 "$D/SKILL.md" ~/.claude/skills/artifact-publisher/SKILL.md
```

What each line proves: the sums check catches a corrupted or swapped
download; the attestation check catches a release asset that was not
produced by this repository's workflow (a compromised account uploading a
"fixed" file, say); installing from `$D` rather than from `$HUB` means the
hub's own state never decides what your agent is allowed to do. Review the
file before the first load and whenever you upgrade — verification proves
origin, not intent.

To also catch a hub that is not serving its own release, compare
`X-Content-SHA256` on `GET $HUB/agent` (also listed under `documents` in
`GET $HUB/context`) with the digest in `SHA256SUMS`: they must match. Without
`gh`, download the same assets from
`https://github.com/padak/kbc_ai_artifact/releases/download/v$V/` and run the
sums check — integrity only, no provenance.

## Local development

```bash
uv sync

HUB_STORAGE_TOKEN=your-token \
HUB_STACK_URL=https://connection.eu-central-1.keboola.com \
HUB_SECRET_KEY=some-local-secret \
uv run uvicorn src.main:app --port 8050
```

Or use `scripts/dev.sh`, which reads those three from a gitignored
`.env.local`, binds the loopback interface and sets `HUB_PUBLIC_BASE_URL` to
match — so `/login` also offers the PKCE flow, which a Keboola stack accepts
only for an `http://127.0.0.1` callback. It verifies `HUB_STORAGE_TOKEN`
against `HUB_STACK_URL` before starting, rather than booting a hub whose every
Storage read answers 401.

`HUB_STORAGE_TOKEN` and `HUB_STACK_URL` describe the **host project** — where
the hub keeps its own serving copies. They are unrelated to whoever signs in
at `/login`, who may be in any project on any stack.

Run the test suite:

```bash
uv run pytest
```

Tests use `InMemoryFilesBackend` in place of real Keboola Storage and mock
token verification (via `respx`/`monkeypatch`); no live Keboola calls are
made during testing.

## Configuration

All configuration comes from environment variables (`src/config.py`).
Required variables have no defaults — the app fails fast at startup if any
are missing. Everything else has a documented default, overridable via env.

| Variable | Default | Meaning |
|---|---|---|
| `HUB_STORAGE_TOKEN` | *required* | Storage API token for the host project (where serving envelopes live) |
| `HUB_STACK_URL` | *required* | Base URL of the host project's Keboola stack |
| `HUB_SECRET_KEY` | *required* | Master secret. Two independent subkeys are derived from it by HMAC-SHA256 over a label (see `src/security.py`'s `derive_key`): one signs password-unlock cookies, the other signs webhook deliveries — so disclosing the webhook key to a receiver never exposes the cookie-signing key |
| `HUB_PUBLIC_BASE_URL` | unset | Absolute base URL used when building returned artifact URLs, if the app can't infer it from the request |
| `HUB_CACHE_DIR` | `/tmp/artifact-cache` | Local disk LRU cache directory (not a source of truth) |
| `HUB_MAX_HTML_BYTES` | `15728640` (15 MB) | Max size of built HTML per artifact |
| `HUB_MAX_INLINE_IMAGE_BYTES` | `5242880` (5 MB) | Max size of a single image inlined as a data URI |
| `HUB_MAX_INLINE_TOTAL_BYTES` | `15728640` (15 MB) | Max total size of all inlined images per artifact |
| `HUB_GIT_CLONE_TIMEOUT_S` | `90` | Timeout for shallow git clones |
| `HUB_GIT_MAX_REPO_BYTES` | `209715200` (200 MB) | Max repository size accepted for a git-sourced publish |
| `HUB_GIT_ALLOW_PRIVATE_HOSTS` | `false` | Disable the SSRF guard that rejects `git_url` hosts resolving to private/loopback/link-local/reserved/metadata addresses — trusted self-hosting only |
| `HUB_CACHE_MAX_ENTRIES` | `200` | Max number of envelopes kept in the disk LRU cache |
| `HUB_UNLOCK_COOKIE_MAX_AGE_S` | `43200` (12 h) | Lifetime of a signed password-unlock cookie |
| `HUB_TOKEN_VERIFY_TIMEOUT_S` | `15` | Timeout for the `token verify` call to a caller's stack |
| `HUB_DESTRUCTIVE_TOKEN_POLICY` | `project` | Which tokens of the owning project may run a *destructive* route (soft delete, purge, owner version delete, rotate-link, webhook key rotation). `project` = any token of that project (the historical behaviour); `admin` = a master token or a project user with the `admin` role; `allowlist` = only ids in `HUB_DESTRUCTIVE_TOKEN_IDS`. Any other value fails at startup. Non-destructive owner routes are never affected. See *Security model* |
| `HUB_DESTRUCTIVE_TOKEN_IDS` | empty | Comma-separated Storage token **ids** (not tokens — an id is an identifier, not a secret) allowed to run destructive routes under `HUB_DESTRUCTIVE_TOKEN_POLICY=allowlist`. Required non-empty in that mode (an empty allowlist would refuse the operator their own routes, so it fails at startup); ignored in the other modes. Read a token's id from `GET {stack}/v2/storage/tokens/verify` |
| `HUB_MAX_VERSIONS` | `50` | Live versions kept per artifact; older non-head, non-pinned ones are pruned (this rule never counts or removes a proposal — proposals have their own cap, `HUB_MAX_PROPOSED_VERSIONS`) |
| `HUB_MAX_VERSIONS_PER_DAY` | `20` | Versions one project may submit for one artifact per UTC day |
| `HUB_MAX_COMMENTS_PER_DAY` | `100` | Comment threads plus replies one project (or one guest invitation) may submit for one artifact per UTC day |
| `HUB_DIFF_MAX_BYTES` | `2097152` (2 MB) | Largest per-side payload the diff renderer (including `format=visual`'s rendered HTML) will process (413 above it) |
| `HUB_EXTRA_STACKS` | empty | Comma-separated extra stack URLs allowed beyond the `*.keboola.com` rule |
| `HUB_MAX_ENVELOPE_BYTES` | `20971520` (20 MB) | Largest persisted envelope/meta record the store will download or read from cache before refusing it as a DoS guard; `0` disables the bound |
| `HUB_REAP_ABORTED_PUBLISH_AFTER_S` | `3600` | A meta record with no version is a publish that died between its two writes; startup deletes such records once older than this, so they cannot be a publish still in flight (`0` disables) |
| `HUB_MAX_PROPOSED_VERSIONS` | `50` | Per-artifact cap on retained proposed versions; the oldest proposals above this are pruned (proposals are never served as head, so this is always safe) |
| `HUB_TRUST_FORWARDED_HEADERS` | `false` | Trust `X-Forwarded-Host`/`X-Forwarded-Proto` to name the public origin when `HUB_PUBLIC_BASE_URL` is unset — only for local development behind a proxy; a direct client can forge these headers |
| `HUB_TRUSTED_PROXY_CIDRS` | empty | Comma-separated CIDR networks of **every** proxy hop in front of the hub, whose members may name the real client in `X-Real-IP` / `X-Forwarded-For`. Honoured only together with `HUB_TRUST_FORWARDED_HEADERS`, and only when the direct peer is inside one of them; empty means a forwarded client address is never believed and every caller buckets by the peer address. A malformed entry fails at startup. See *Deployment to Keboola* |
| `HUB_MAX_FORWARDED_CHAIN_ENTRIES` | `16` | How many `X-Forwarded-For` entries the client-address walk examines, counted from the right. The header is caller-supplied and arbitrarily long; this bounds the parsing one request can buy. Real paths are three hops, so raising it is rarely useful |
| `HUB_MAX_UNLOCK_ATTEMPTS_PER_HOUR` | `30` | Failed password-unlock (and guest-invitation) attempts allowed per (artifact, client address) per UTC hour before the gate answers 429 (each attempt costs a full PBKDF2 verification) |
| `HUB_MAX_UNLOCK_ATTEMPTS_PER_ARTIFACT_PER_HOUR` | `500` | The same budget again, per artifact across *all* addresses together — a backstop nobody can rotate away by changing address. Set well above any real audience of one document; it stops industrial guessing, not individual readers |
| `HUB_MAX_SMALL_REQUEST_BYTES` | `262144` (256 KB) | Largest inbound request body accepted by every route that does not carry a document — comments, replies, invitations, the unlock form, webhook management, policy-only updates. Enforced from the ASGI layer, before authentication, locking, JSON parsing or PBKDF2 (413 above it). The document routes (publish, update, submit a version) instead get `max(HUB_MAX_HTML_BYTES, HUB_MAX_ENVELOPE_BYTES)` plus 1 MB of JSON-envelope head-room, so raising either content limit raises what they accept |
| `HUB_LOCK_REGISTRY_MAX_ENTRIES` | `1024` | Idle per-artifact mutation locks kept before the least recently used are dropped. A lock somebody holds or waits on is never dropped, so this bounds memory without weakening serialization |
| `HUB_NEGATIVE_LOOKUP_CACHE_ENTRIES` | `4096` | Artifact ids Storage has confirmed do not exist, remembered so a repeated lookup of a made-up id costs a dict hit instead of a Storage tag search. Bounded least-recently-used; every write that can make an id appear drops its entry. `0` disables it |
| `HUB_STATE_SNAPSHOT_INTERVAL_S` | `300` | Seconds between snapshots of the rate-limit/analytics sidecar into Storage Files; `0` disables the background thread (snapshots then need an explicit call, as the tests do) |
| `HUB_STATE_MAX_SNAPSHOT_BYTES` | `52428800` (50 MB) | Largest state snapshot the hub will upload or restore; an oversized one is skipped with a warning rather than restored (`0` disables the bound) |
| `HUB_STATE_DB_FILENAME` | `state.sqlite3` | File name of the SQLite sidecar holding rate-limit counters and view analytics, under `HUB_CACHE_DIR` |
| `HUB_INSTANCE_LOCK_FILENAME` | `instance.lock` | File name, under `HUB_CACHE_DIR`, of the exclusive lock the process takes at startup and holds until it exits. A second process on the same cache directory fails to start (see *Deployment*, "One hub is one process") |
| `HUB_WEBHOOK_TIMEOUT_S` | `10` | Per-request HTTP timeout for one webhook delivery attempt |
| `HUB_WEBHOOK_MAX_ATTEMPTS` | `3` | Total POST attempts per (URL, event) before giving up; retries back off `2**n` seconds, capped at 60 |
| `HUB_WEBHOOK_QUEUE_MAX` | `1000` | Queued-but-undelivered webhook deliveries kept at once; past this the newest is dropped with a log line rather than blocking the request that produced it |
| `HUB_MAX_WEBHOOKS_PER_ARTIFACT` | `5` | How many webhook URLs one artifact may register |
| `HUB_WEBHOOK_KEY_OVERLAP_S` | `600` | Seconds a receiver's previous signing key stays valid after `POST .../webhooks/{receiver_id}/rotate-key` (carried in `X-Hub-Signature-256-Previous` alongside the new key's `X-Hub-Signature-256`); `0` disables the grace period |
| `HUB_MAX_INVITATIONS_PER_ARTIFACT` | `20` | How many live guest invitations one artifact may hold at once (revoked ones are reclaimed automatically to make room) |
| `HUB_EXPORT_MAX_BYTES` | `67108864` (64 MB) | Ceiling on the source material one vault export may render, and on the archive it writes; a larger artifact answers 413 before anything is built (`0` disables the bound) |
| `HUB_MAX_EXPORTS_PER_HOUR` | `20` | Vault exports of one artifact one client address may build per UTC hour before the export answers 429 |
| `HUB_LOGIN_CLIENT_ID` | `kbc-artifact-hub` | Public client label the hub identifies itself with on a stack's sign-in endpoints; a rate-limit and audit label, not a secret |
| `HUB_LOGIN_TIMEOUT_S` | `20` | Per-request HTTP timeout for a sign-in call to a stack |
| `HUB_LOGIN_PKCE_TTL_S` | `600` | How long a started PKCE sign-in may sit unfinished before its callback stops being accepted |
| `HUB_LOGIN_MAX_PENDING_PKCE` | `64` | Concurrently pending PKCE sign-ins held in memory; the oldest above this are dropped |
| `HUB_MAX_LOGINS_PER_HOUR` | `30` | Sign-in requests one client address may make per UTC hour (429 afterwards): starting a device or PKCE sign-in, renewing a session, signing out |
| `HUB_MAX_LOGIN_POLLS_PER_HOUR` | `600` | Polls of an already-started device sign-in one client address may make per UTC hour. Counted separately because one honest sign-in polls every few seconds for up to 15 minutes |

## Deployment to Keboola

The app is deployed from the public GitHub repository
`padak/kbc_ai_artifact` as a Keboola Data App. For production, name the
release tag explicitly — `--git-branch` defaults to
`main`, which is a moving target:

```bash
kbagent data-app create \
  --project artifacts \
  --git-repo https://github.com/padak/kbc_ai_artifact \
  --git-branch v0.12.0 \
  --git-public
```

**Why the tag is not optional in production.** The runner re-clones the
configured ref on every container start — a fresh deploy, a restart, or a
wake from auto-suspend — not only when you deploy. Left on a branch, a push
to `main` after review, or simply a restart landing on a newer commit than
the one last verified, changes what is actually running with no deliberate
deploy action behind it. A tag makes a given deployment stay tied to the
exact commit that was reviewed and versioned. CLAUDE.md's deploy flow
already tags the commit and cuts a GitHub release for every user-visible
change (see *Contributing* below), so the tag to pass always exists.

Pass the tag itself, not a commit SHA: this is a clone ref, and git's
`--branch` resolves a branch or a tag only — the same constraint the
service's own `git_ref` has. To pin a specific commit, tag it first.

Omitting `--git-branch` (tracking `main`) is fine for a development or
staging app where following the branch is the point.

**One hub is one process.** A Data App runs as a single container that
suspends when idle and restarts on demand — never two side by side — and one
hub serves one organisation. The service relies on that: its artifact index,
its locks and its state snapshots are designed for exactly one writer, and
Storage Files cannot coordinate two (they are immutable, with no
create-if-absent). Do not run it with `--workers`, behind a second copy of
itself, or with two containers sharing a cache directory; each organisation
runs its own app instead.

That is now enforced rather than assumed, in two places:

- **A startup lock.** The process takes an exclusive `flock` on
  `instance.lock` inside `HUB_CACHE_DIR` and holds it until it exits. A second
  worker or a second container on the same disk fails to start, with an error
  naming this rule, instead of starting successfully and quietly losing the
  first one's writes.
- **Readiness fails on a detected second writer.** Two processes on *different*
  disks cannot see each other's lock, but they do see each other's state
  snapshots in Storage. When the state sidecar finds a snapshot it did not
  write, the process stops writing operational state altogether (rate-limit
  counters fall back to per-process tallies, nothing is deleted, and both
  snapshots are left in Storage for you to compare) and `GET /health` starts
  answering **503** with a `detail` naming the invariant. `POST /` — the
  platform's own startup check — keeps answering 200, so the container is not
  restarted into the same broken situation; the fix is to stop the extra
  process and restart the remaining one.

**Tell the hub which proxy to believe.** The brute-force budgets behind the
artifact password gate and the guest-invitation gate are keyed on the client
address, and behind the platform proxy the only address the app sees on the
connection is the proxy's. The real one arrives in `X-Real-IP` /
`X-Forwarded-For` — but those headers are equally easy for a direct caller to
write, so the hub believes them only when the connection came from a network
you named:

```bash
kbagent data-app secrets-set --secrets-file <file>   # HUB_TRUST_FORWARDED_HEADERS=1
                                                     # HUB_TRUSTED_PROXY_CIDRS=10.0.0.0/8,198.51.100.0/24
```

Set `HUB_TRUSTED_PROXY_CIDRS` to the networks of your proxies (and
`HUB_TRUST_FORWARDED_HEADERS=1`, which both this and the public-origin
headers require). Multiple networks are comma-separated; a malformed entry
fails at startup rather than being skipped.

**List every hop, not just the nearest one.** Each proxy in the path
*appends* the address it accepted the connection from — nginx does this with
`$proxy_add_x_forwarded_for` — so `X-Forwarded-For` arrives oldest-first and
its **leftmost** entry is whatever the original caller chose to send. The hub
therefore reads the chain from the **right**, skipping entries that are
themselves inside `HUB_TRUSTED_PROXY_CIDRS`, and takes the first entry left
over as the client. If a hop's network is missing from the list, the walk
stops at that hop and every reader behind it shares its bucket — safe, but no
better than leaving the setting empty. Every candidate must also parse as an
IP address (a `host:port` or `[v6]:port` form is accepted, an IPv4-mapped
IPv6 address is folded back to its IPv4 form); anything else is ignored and
the hub falls back to the connection's own address. Only the last
`HUB_MAX_FORWARDED_CHAIN_ENTRIES` entries are looked at, so a very long
header costs no more than a short one.

**If you leave it unset, nothing breaks** — the hub falls back to the address
the connection actually came from, which behind the proxy is the proxy
itself. That is safe (no caller can choose their own bucket) but coarse:
every reader of a password-protected document shares one hourly budget, so
one person guessing passwords can push everyone else into 429 until the hour
rolls over. The per-artifact backstop
(`HUB_MAX_UNLOCK_ATTEMPTS_PER_ARTIFACT_PER_HOUR`) applies either way.

Secrets are set with `kbagent data-app secrets-set` and never committed to
the repository:

- `HUB_STORAGE_TOKEN` — a Storage token scoped to the host project, minted
  with `kbagent token create ... --can-read-all-file-uploads`
- `HUB_STACK_URL` — the host project's stack URL
- `HUB_SECRET_KEY` — a random master secret; the unlock-cookie and webhook
  signing keys are both derived from it (see *Configuration* above), never
  used directly

The nginx-to-app contract (nginx listens on `8888` and proxies to uvicorn on
`:8050`) is defined in `keboola-config/` (`keboola-config/nginx/sites/default.conf`,
`keboola-config/supervisord/services/app.conf`); it does not need to be
changed for normal application development.

## Security model

Artifact URLs are unguessable capabilities: possession of the URL is
sufficient to read the artifact, and there is no public listing or index to
discover them. Every artifact response sets `X-Robots-Tag: noindex` to keep
search engines out. Passwords are hashed with PBKDF2-SHA256 before storage in
the serving envelope, and unlocking one sets a signed cookie (via
`itsdangerous`) scoped to that artifact's own path (`path=/a/<share_id>`) and
to the current password's own hash, so a cookie signed under a since-changed
password no longer verifies, and an unlock on one artifact does not unlock
another.

**Ownership is the project; destructive authority is configurable.**
Every owner-only route (update, trash, restore, purge, rotate-link,
invitations, stats, promote, head) authorizes by verifying the caller's
Storage token against the stack and checking that it resolves to the same
`(stack, project)` pair that originally published the artifact. That check
is project-level by design and stays that way: it does not ask which token
of the project you used.

By itself that meant **any** valid Storage token of the owning project —
including a read-only or single-purpose one — could purge an artifact,
rotate its public link or delete a version (tracked as `SEC-075-011`).
`HUB_DESTRUCTIVE_TOKEN_POLICY` now lets an operator narrow exactly that,
using the token-level claims the same `tokens/verify` call already returns:

| Policy | Who may run a destructive route |
|---|---|
| `project` *(default)* | Any token of the owning project — the historical behaviour, unchanged |
| `admin` | A master token, or one belonging to a project user whose `admin.role` is `admin` |
| `allowlist` | Only tokens whose id is listed in `HUB_DESTRUCTIVE_TOKEN_IDS` |

The gate covers the irreversible and link-breaking routes only: `DELETE
/api/artifacts/{id}` (soft delete), `DELETE /api/artifacts/{id}/purge`,
`DELETE /api/artifacts/{id}/versions/{n}` when run as the owner, `POST
/api/artifacts/{id}/rotate-link` and `POST
/api/artifacts/{id}/webhooks/{receiver_id}/rotate-key`. **Non-destructive
owner routes are deliberately untouched** by every policy — update, head
pin, promote, settings, invitations, stats and trash restore stay
project-authorized, and a contributor withdrawing their *own* proposal is
never gated. A token that fails the policy gets `403` whose `detail` names
the active policy and what the credential lacked; the detail never echoes
the token and never reveals the allowlist's contents. The active policy
*name* (not its contents) is reported in `GET /context` under `limits`.

The default stays `project` so that upgrading cannot silently lock an
operator out of artifacts they already own — but **for a shared project,
set `HUB_DESTRUCTIVE_TOKEN_POLICY=admin`**. Use `allowlist` when even
project administrators should not be able to purge, and destructive work is
meant to run from one specific automation token.

**Mitigation without the policy, or in addition to it:** use a dedicated
Keboola project for artifact publishing (or a token issued only for that
purpose, in a project that holds nothing else sensitive) rather than
reusing a broadly-scoped production project's token — under the default
policy any token of that project can purge every artifact it owns.

**Network egress: git and webhook hostname checks are best-effort, not a
guarantee (`SEC-075-005`, `SEC-075-006`).** Before cloning a `git_url` and
before delivering a webhook, the hub resolves the target hostname and
rejects it if any resolved address is loopback, private (RFC1918/ULA),
link-local, reserved, or a known cloud-metadata address
(`169.254.169.254`, `*.internal`) — see `HUB_GIT_ALLOW_PRIVATE_HOSTS` below.
This check is re-run immediately before the git clone subprocess to narrow
the window, and the git clone itself now runs with
`-c http.followRedirects=false` (`SEC-100-005`) so a redirect to an
unvalidated host is refused instead of silently followed. None of that
closes the gap completely: both git (via libcurl) and the webhook HTTP
client (`httpx`) resolve the hostname a second time, independently, at
connect time — a DNS answer that changes between the hub's validation and
that second resolution (DNS rebinding) is a resolver-to-connect
time-of-check/time-of-use gap this application cannot close with its
current clients, because neither client exposes a way to pin the connection
to the IP address the hub already validated. **The reliable control is
operational, not this application:** run the hub's container behind an
egress policy or resolver-aware outbound proxy that denies connections to
loopback, RFC1918/ULA, link-local, cluster/service, and cloud-metadata
ranges regardless of what hostname validation concluded. Treat the
in-application hostname check as defense in depth, not the boundary.

**Signed-in sessions.** A `/login` session is a credential of the visitor's,
handled exactly like a pasted Storage token: the hub relays the sign-in to the
stack, hands the result to the tab that asked for it, and keeps no session of
its own — no cookie, no server-side store, nothing written to Storage or disk.
Every response that carries one is `no-store`, and the PKCE code verifier is
the one piece that stays on the server (in memory, single-use, TTL-bounded),
because it is the proof of possession the callback is checked against. The
`/login/*` endpoints are unauthenticated by necessity — the caller has no
identity yet — so starting a sign-in is rate-limited per client address
(`HUB_MAX_LOGINS_PER_HOUR`), which keeps the hub from being usable as an open
relay onto Keboola's auth API. What a session may do here is bounded by what
it may do on the stack: `/v2/storage/*` calls made with it resolve to the
admin's own Storage token *in the project named by `X-Storage-Project`*, and a
project the admin cannot reach comes back 403 from the stack itself.

**Capability revocation (0.7.0).** `POST /api/artifacts/{id}/rotate-link`
mints a fresh public share id and the previous one — plus the bare internal
id, once it differs from the share id — stops resolving immediately, with no
grace period; every unlock cookie issued under the old link goes with it.
`DELETE /api/artifacts/{id}` (trash) is the softer revocation: the link stops
resolving too, but nothing is discarded and `POST .../restore` brings it back
on the same URL; `DELETE .../purge` is the separate, irreversible one. A guest
invitation is revoked the same way, per person, via `DELETE
/api/artifacts/{id}/invitations/{iid}` — comments the guest already made stay,
only the capability to make new ones is withdrawn.

Community versioning is deliberately **moderated**: a version submitted by any
project other than the owner is stored as a proposal, is never served as the
head, and its content is readable only by the artifact owner and the version's
own author (both authenticate with the usual two management headers). Proposal
*metadata* — project name, note, timestamps, size, and (0.7.0) `base_version`
and `outdated` — is listed to anyone holding the capability URL, so treat
notes as public. Submissions are capped per contributing project per artifact
per day (`HUB_MAX_VERSIONS_PER_DAY`); since 0.7.0 that counter — and every
other rate-limit and view-count counter — lives in a SQLite sidecar
snapshotted into Storage Files rather than in process memory, so a budget
survives a redeploy instead of resetting every time the container restarts.
The sidecar is durable, not shared: this service runs as exactly one process
(see *Deployment* above), and a second one writing the same snapshots is an
error it detects and refuses to work through, not a supported topology.

Since 0.6.0, `/a/{id}` and `/a/{id}/v/{n}` render an artifact inside a
`srcdoc` iframe sandboxed without `allow-same-origin`, so a published
document's own scripts run in an opaque origin and cannot reach the hub's own
origin — where `/admin` and `/review` keep a signed-in visitor's Storage token
in `sessionStorage` — regardless of same-origin cookie scoping. `/raw` is
unaffected: it stays the exact bytes for machine clients.

A guest invitation's secret rides the URL *fragment* (`#invite=...`), which
browsers never send to a server, and only ever reaches the API in the
`X-Artifact-Guest` header — the same discipline as the artifact password,
never a query string, path segment, or cookie. Outbound webhook URLs are
treated as equally sensitive: they are only ever echoed back in the `PUT`
response that set them, never in `GET /api/artifacts` (which reports a count
instead), and every delivery is HMAC-signed (`X-Hub-Signature-256`) so a
receiver can reject a forged POST. Each receiver is signed with its **own**
key, bound to the artifact, the receiver URL and a rotatable epoch, so a
receiver cannot forge a delivery for a different receiver or a different
document; owners read those keys from `GET /api/artifacts/{id}/webhooks`.
Both that listing and `POST .../webhooks/{receiver_id}/rotate-key` carry
`Cache-Control: no-store` and `Pragma: no-cache`, since the response body is
a credential and must never be served from a shared or browser cache.

**Rotating a receiver's key (SEC-100-006).** Before rotation, a receiver's
key was derived purely from `(artifact_id, url)`: re-registering the same URL
always reproduced the same key, so a leaked key could only be invalidated by
changing the URL (revoking the receiver) or the hub's own master secret
(rotating every receiver on every artifact at once). `POST
/api/artifacts/{id}/webhooks/{receiver_id}/rotate-key` mints an independent,
random key epoch for one receiver without touching its URL or any other
receiver. For `HUB_WEBHOOK_KEY_OVERLAP_S` seconds afterwards (default 600),
deliveries to that receiver carry both the new signature
(`X-Hub-Signature-256`) and the previous one
(`X-Hub-Signature-256-Previous`), so a receiver that has not yet picked up
the new key from the rotate response keeps verifying deliveries; past the
window only the new signature is sent and the old key verifies nothing. A
meta record persisted before this field existed has no epoch on file at all,
which is read exactly like a receiver that has simply never been rotated —
no migration step, no re-registration.

## Contributing

See `CLAUDE.md` at the repo root for project-specific rules before changing
code: the 3.11 f-string-backslash gotcha, why absolute URLs must go through
`base_url()`/the `public_origin` middleware instead of the raw `Host` header,
the Storage-tag-driven index rebuild, and the secrets-scrubbing discipline
around client and git tokens. In short — `uv run pytest tests/ -q` and
`python3.11 -m py_compile src/*.py` must both be clean before any commit, and
tests must use `InMemoryFilesBackend` with `verify_token` patched, never a
live Keboola call.
