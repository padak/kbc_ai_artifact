# Design systems — 0.17.0 amendment: role variables, CORS, public gallery

Date: 2026-09-15
Status: approved by the user in chat ("Ano, udělej 0.17.0 + demo"; "Veřejná
galerie /ds na hubu"). Amends `2026-09-14-design-systems-design.md`.

## Why

Two things the user asked for after 0.16.0 went live:

1. **Switch one document between design systems on the fly.** Today every
   design system emits its own token names (`--color-bg-page` in one,
   `--color-page` in another), so a document styled against one system cannot
   be re-skinned by pointing it at another system's CSS. The spec already has
   the bridge — `roles` — but only the hub's starter uses them.
2. **See the registered design systems without a token.** The catalogue API
   stays credentialed (Key decision 2), but the user wants the systems visible
   to anyone who opens the hub: a public gallery.

## Decisions

1. **Role variables.** `GET /ds/{ref}/css` and the starter's `<style>` emit,
   after the token blocks, one `:root` block of stable aliases derived from
   `bundle.roles`:
   - `--ds-<role>` for every scalar role, with the role name's `_` turned into
     `-`: `--ds-background`, `--ds-surface`, `--ds-text`, `--ds-muted`,
     `--ds-border`, `--ds-accent`, `--ds-on-accent`, `--ds-font-body`,
     `--ds-font-heading`, `--ds-font-mono`, `--ds-radius`; each is
     `var(<target variable>)` so it follows the mode automatically;
   - `--ds-chart-1` … `--ds-chart-N` for `chart_palette` and `--ds-chart-count: N`;
   - only roles the bundle declares are emitted; nothing is invented.
   The `variables` map in `GET /ds/{ref}/bundle` gains a `roles` sub-map
   (`"role" → "--ds-…"`) so agents never derive names. `?mode=light|dark`
   flat outputs carry the same alias block. The starter's role rules
   (`body{…}`, headings, links, `.ds-surface`) switch to the `--ds-*` names.
2. **CORS on public reads.** Every **id-resolved** `GET /ds/{id}/…` response
   (`versions`, `bundle`, `tokens`, `css`, `starter`, `guidance`) and the
   gallery's JSON carry `Access-Control-Allow-Origin: *` (and
   `Access-Control-Expose-Headers: X-Hub-Version`). Slug-resolved responses
   (credentialed) and every `/api/*` route do not. The page routes (`/ds`,
   `/ds/{id}`) do not need it. `HEAD`/`OPTIONS` need no special handling: a
   cross-origin `GET` with no custom headers is a simple request.
3. **Public gallery.** `GET /ds` (hub chrome, `pages.design_systems_gallery_page`)
   lists every design system that has at least one version, newest first:
   name, slug, description, owner project name, head version, updated date,
   a swatch strip (resolved `background`, `surface`, `text`, `accent` and up to
   six `chart_palette` colours of the head version's light set), and links to
   the style guide and the machine bundle. No credential; no `mine`. The same
   data as JSON at `GET /ds?format=json` →
   `{"design_systems": [{id, slug, name, description, owner: {project_name},
   head_version, updated_at, swatches: {background, surface, text, accent,
   chart: [...]}, urls: {page, bundle, css, starter}}]}` (owner project id and
   stack host are **not** in the public shape). Swatches are computed from the
   head version's base `TokenSet` via `designkit.role_values`, cached per
   `(id, version)` in the derived LRU. The landing page's design-systems
   section links "Browse the gallery" → `/ds`; `/llms.txt` and `/context`
   (`endpoints`, `design_systems.gallery`) name it; SKILL.md and the agent
   file mention it in one sentence as the human-facing list (agents keep
   using the credentialed API, which carries `mine` and the full owner).
   This deliberately makes names, slugs and descriptions public on this hub;
   the slug-needs-credential rule on `/ds/{slug}` reader routes stays — it
   still avoids an existence oracle for *unlisted* systems only in the sense
   that the gallery is the intended listing. Documented in README's Security
   model.
4. **The switcher demo** (`examples/switcher/index.html`, published as an
   artifact after the release) is a self-contained page styled only with
   `--ds-*` variables: it fetches `GET {hub}/ds?format=json`, fills a
   `<select>`, and on change swaps a `<link rel="stylesheet">` to the chosen
   system's `/ds/{id}/css`, loads that system's `fonts` (from
   `/ds/{id}/bundle`), and re-renders a chart.js chart with `--ds-chart-*`
   read via `getComputedStyle`. Component HTML is not switched (each system
   has its own); the demo shows that tokens, type, colour and charts follow.
   Linked from the showcase README ("Try switching styles live") and from the
   gallery page header.

## Version

0.17.0: `pyproject.toml`, `plugin.json`, `marketplace.json`, README mention,
CHANGELOG head `## 0.17.0 — One document, ten looks (2026-09-15)`.

## Tests

- tokens/designkit: `--ds-*` block present only for declared roles, `_`→`-`,
  chart aliases and count, `variables.roles` map, starter role rules use
  `--ds-*`.
- api: CORS header on id-resolved reads and on `/ds?format=json`, absent on
  slug-resolved reads and on `/api/*`; `/ds` page lists registered systems
  and not meta-only ones; JSON shape exact (no project id / stack host);
  `/context` lists `GET /ds`; `/llms.txt` mentions `/ds`.
- docs test: SKILL and agent mention the gallery and `--ds-*`.
