# Reader menu — 0.18.0 implementation report

Worktree: /Users/padak/github/kbc_ai_artifact/.claude/worktrees/reader-menu
Branch: feat/reader-menu (from origin/main @ 23b69a5)

## Commits
1. 4cec500 docs: reader menu design spec (0.18.0) — spec copied verbatim to
   docs/superpowers/specs/2026-09-15-reader-menu-design.md
2. 461f5ed feat(store): ArtifactMeta.reader_menu + Settings.reader_menu_default
3. 844d47c feat(pages): _MENU_CSS/_MENU_JS/reader_menu_html + frame page wiring
4. 6b27852 feat(api): PUT field, responses, frame calls, /context, admin checkbox
5. af2b262 docs: SKILL/agent/README + release bump to 0.18.0

## Changes
- src/store.py — `reader_menu: bool = True` declared LAST, after
  `webhook_key_epochs` (positional construction preserved); coerced to bool in
  `__post_init__`; written by `to_json`; `from_json` reads
  `data.get("reader_menu", True)` so every pre-0.18.0 meta file keeps the menu.
- src/config.py — `reader_menu_default: bool = True` via
  `_bool_env("HUB_READER_MENU_DEFAULT", True)`.
- src/pages.py — `READER_MENU_LABEL`, `_MENU_PROPOSAL_MODES`, `_MENU_CSS`,
  `_MENU_JS`, `_menu_item()`, `reader_menu_html(base, share_id,
  accept_versions_mode)`. `artifact_frame_page(..., reader_menu=False,
  accept_versions_mode="off")` appends the button + panel + script after the
  iframe and only then includes `_MENU_CSS`. Menu is positioned to step aside
  when the live-update banner is visible (`.ahlive:not([hidden]) ~ .ahmenu*`).
  Admin studio (`_ADMIN_JS`) gains a "show the reader menu on the artifact
  page" checkbox that PUTs `{"reader_menu": bool}` and reflects
  `data.reader_menu !== false`, modelled on the accept_versions toggle.
- src/main.py — `UpdateBody.reader_menu`; applied in the settings block next to
  `comments_mode` (NOT in the access-relevant settings list, never behind
  `_destructive_authority`); `publish_artifact` seeds
  `reader_menu=settings.reader_menu_default`; `_artifact_response`,
  `GET /api/artifacts` rows and `GET /a/{id}/meta` report it; the single
  `_frame_response` helper (used by both `read_artifact` and `read_version`)
  passes `reader_menu=meta.reader_menu,
  accept_versions_mode=meta.accept_versions_mode`; `/context` gains the
  publish/update prose and `limits.reader_menu_default`.
- Docs: skills/artifact-publisher/SKILL.md ("### The reader menu" + PUT table
  row), agents/artifact-hub.md (paragraph + table row), README (Features
  bullet, PUT row, `HUB_READER_MENU_DEFAULT` env row), CHANGELOG head
  `## 0.18.0 — Every artifact explains itself (2026-09-15)`.
- Release parity: pyproject 0.18.0, .claude-plugin/plugin.json,
  .claude-plugin/marketplace.json, uv.lock package line, README `--git-branch
  v0.18.0`.

## TDD evidence
Every unit was red first:
- store/config: 5 + 2 tests failed (`AttributeError: 'ArtifactMeta' object has
  no attribute 'reader_menu'`, field-order assertion, `Settings` has no
  `reader_menu_default`) → green after the implementation.
- pages: 6 of 7 failed (`TypeError: artifact_frame_page() got an unexpected
  keyword argument 'reader_menu'`) → green. Two assertions were tightened
  after seeing them fail for the wrong reason (the string "ahmenu" also
  appears in the head CSS, so the DOM-order and no-credential assertions now
  anchor on `id="ah-menu-btn"`).
- api/admin: 9 tests failed (KeyError 'reader_menu', label absent from the
  page, no admin checkbox) → green.
- docs: 2 tests failed before the paragraphs were written → green.

## Tests
`PATH=/Library/Developer/CommandLineTools/usr/bin:$PATH uv run pytest tests/ -q`
→ **1644 passed, 1 warning in 22.19s** (1624 before this work: +20 new).
`python3.11 -m py_compile src/*.py` → clean.
One pre-existing test was updated on purpose:
tests/test_review100_atomic_update.py pins the exact key set of the owner PUT
response; `reader_menu` was added to it with a comment.

## Concerns / notes
- The frame page is a zero-chrome shell and has never used `pages._CSS` (it
  inlines the tokens, exactly like `_FRAME_LIVE_CSS`). `_MENU_CSS` follows that
  established precedent — literal hex values identical to `_CSS`'s tokens,
  light + `prefers-color-scheme: dark` — rather than pulling the whole
  stylesheet into every artifact page.
- `_MENU_PROPOSAL_MODES = ("anyone", "allowlist")` is spelled out in pages.py
  instead of importing `src.store._ACCEPT_ON_MODES`, to keep pages.py's only
  src dependency `src.designkit`. A test pins the behaviour for both modes.
- The menu holds no credential (a test asserts `sessionStorage`, `hubSession`
  and `X-Storage-Token` never appear in it), so `window.hubSession` is
  deliberately absent.
- No focus trap (spec said not required); Escape closes and returns focus to
  the button, and an outside click closes.
- Not pushed, no PR opened, as instructed.
