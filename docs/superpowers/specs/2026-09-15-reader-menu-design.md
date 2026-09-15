# Reader menu — Design (0.18.0)

Date: 2026-09-15
Status: approved in chat by the user (default ON for every artifact, owner
toggle, separate release after design systems).

## Purpose

People who receive an artifact link keep asking "how do I comment on this?"
and "how do I propose a new version?". The answer should be on the page: a
small, unobtrusive control in the bottom-right corner of every artifact page
that opens a menu of what a reader can do here, with one-line how-tos.

## Decisions

1. **It lives in the hub's frame page, never in the artifact.** `GET /a/{id}`
   and `/a/{id}/v/{n}` already wrap the document in a sandboxed `srcdoc`
   iframe (`pages.artifact_frame_page`). The menu is hub chrome rendered in
   the parent document, so it works for HTML, Markdown and git artifacts,
   changes no published bytes, cannot be covered or read by the artifact's own
   scripts, and `/a/{id}/raw` stays byte-exact without it.
2. **Per-artifact toggle, default ON, existing artifacts included.**
   `ArtifactMeta.reader_menu: bool = True` (persisted in the meta JSON;
   `from_json` treats a missing key as `True`). Owner sets it through
   `PUT /api/artifacts/{id}` (`"reader_menu": false|true`, non-destructive
   owner route — never behind `_destructive_authority`) and through a
   checkbox in the admin studio next to `accept_versions_mode`. `/a/{id}/meta`
   and the artifact list report `reader_menu`.
3. **Contents.** A round button (hub accent, `?` glyph, `aria-label="What can
   I do with this document?"`), fixed bottom-right, 44 px, keyboard reachable;
   click/Enter opens a panel (hub design system: `pages._CSS` tokens, mono
   heading) with:
   - **Comment on this document** → `/a/{id}/review` — "Select a passage,
     write a note. You need a Keboola sign-in, or an invitation link from the
     owner."
   - **Versions and history** → `/a/{id}/versions?format=html`.
   - **Propose a new version** — how-to: "Publish your revision with
     `POST /api/artifacts/{id}/versions` (any Keboola token); the owner reviews
     it in the admin studio." Link to `/skill#versioning`. Shown only when
     `accept_versions_mode != off`; otherwise "This document does not accept
     proposals."
   - **Read as Markdown** → `/a/{id}/export/markdown`.
   - **Share with an AI assistant** — "Paste this link; the page tells the
     assistant where the document and the API live (`/llms.txt`)." Copy-link
     button (clipboard API; fallback: selectable input).
   - **About this hub** → `/`.
   - Footer: "Hidden by the owner? Toggle `reader_menu` in the admin studio."
   No credential is read or stored by the menu; it is static markup + a tiny
   toggle script (`_MENU_JS`) that only opens/closes and copies the URL.
4. **Where it does not appear.** `/raw`, `/source`, exports, the review UI
   (which has its own chrome), the unlock form, and when `reader_menu` is
   false.
5. **Settings.** `HUB_READER_MENU_DEFAULT` (`true`) decides the value a **new**
   artifact gets and the value assumed for meta records without the key.

## Endpoints and shapes

- `PUT /api/artifacts/{id}` body gains `reader_menu: bool | None`.
- `_artifact_response`, `GET /api/artifacts` rows and `GET /a/{id}/meta` gain
  `reader_menu: bool`.
- `/context`: `publish_body`/`update` prose + `limits.reader_menu_default`.

## Files

`src/store.py` (meta field + JSON), `src/config.py` (setting), `src/main.py`
(PUT handling, responses, frame call passes `meta.reader_menu` and
`accept_versions_mode`), `src/pages.py` (`_MENU_CSS`, `_MENU_JS`,
`reader_menu_html(base, share_id, accept_versions_mode)` used by
`artifact_frame_page` via a new keyword `reader_menu: bool = False` and
`accept_versions_mode: str`; admin studio checkbox), SKILL.md + agent file
(one paragraph: what the menu is, the toggle), README (Features bullet, PUT
row, env table), CHANGELOG `## 0.18.0 — Every artifact explains itself
(2026-09-15)`, version bump.

## Tests

Frame page contains the button and panel when enabled and nothing when
disabled; every link points at the right route for the share id; the proposal
item follows `accept_versions_mode`; `/raw` unchanged; meta default True for
old records; PUT toggles and echoes; admin studio markup contains the
checkbox; docs mention it; version parity.
