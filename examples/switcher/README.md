# Style switcher demo

`index.html` is one self-contained HTML document that re-skins itself with
any registered design system on KBC Artifact Hub — a "quarterly product
update" report whose tiles, table, chart and callout are styled entirely
with `--ds-*` role variables (no hard-coded colours or font names). Picking
a different system from the top-bar `<select>` swaps one stylesheet link,
loads that system's fonts, and re-renders the chart with its chart
palette. The markup never changes; only the CSS does.

It exists to make the 0.17.0 role-variable contract tangible: a document
written once against `--ds-background`, `--ds-surface`, `--ds-text`,
`--ds-accent`, `--ds-chart-1..N` and friends can wear any of the hub's ten
sample systems (or a project's own registered one) without a rewrite.

## How it works

1. On load, fetches `GET {hub}/ds?format=json` — the public gallery, no
   credential required — and fills the `<select>` with every registered
   design system (name + head version).
2. Picks the system named in `?ds=<slug>`, or the first one in the
   gallery, and applies it:
   - swaps `<link id="ds-css">` to that system's `GET /ds/{id}/css` and
     waits for the `load` event;
   - fetches `GET /ds/{id}/bundle` and loads its `fonts[].href` into fresh
     `<link rel="stylesheet">` elements, removing the previous system's;
   - reads `--ds-chart-1..N` (and `--ds-chart-count`) off
     `getComputedStyle(document.documentElement)` and re-renders the
     chart.js bar chart with those colours and `--ds-font-body`;
   - points the "Style guide" link at the system's human-facing page and
     updates the URL with `history.replaceState` so the choice is
     shareable.
3. A light/dark toggle sets `data-theme` on `<html>`, which the loaded
   system's CSS responds to (falling back to `prefers-color-scheme` when a
   system doesn't define an explicit dark block).
4. A small neutral `:root` palette (marked with `/* fallback */` comments
   in the `<style>` block) keeps the page readable in the instant before
   the first system's CSS finishes loading, or if a fetch fails — every
   fetch failure surfaces as a visible banner, never only a console error.

## Hub target

The hub base URL comes from `<meta name="hub" content="...">` in the
`<head>`, defaulting to the production hub
(`https://artifact-hub-1304628444.hub.keboola.com`). Override it for local
testing with a query parameter:

```
index.html?hub=http://localhost:8000
```

Note that the `--ds-*` alias block, the public `/ds?format=json` gallery
and CORS on id-resolved `/ds/{id}/...` reads all ship in the 0.17.0
release (see
`docs/superpowers/specs/2026-09-15-design-systems-0.17-amendment.md`) — a
0.16.x hub answers 404/CORS-blocked for the calls this page makes.

## Publishing

Published as an artifact from this repo with `git_url` pointed at
`kbc_ai_artifact` and `git_path=examples/switcher` — the entry file is
resolved automatically (`index.html`). Nothing outside this one file is
required; everything is inlined, and the only external script is Chart.js
from jsdelivr, pinned to the same version the hub's own server-rendered
starters and style guides use (`builder.CHARTJS_JS`).

See also: `examples/showcase/README.md` for the sample systems this page
switches between, and the amendment doc above for the exact JSON/CSS
contract it depends on.
