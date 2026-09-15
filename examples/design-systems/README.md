# Sample design systems

Ten ready-made design systems, one JSON file each. A file is the complete
`POST /api/design-systems` request body — `slug`, `name`, `description`, `note`
and the `bundle` (DTCG tokens with a `dark` mode, typed `roles`, a written
`guidance` brief, three to six HTML/CSS components, and the chart/diagram
conventions). They exist so a fresh agent session can say "use `exec-report`"
instead of re-describing a look, and so the hub has something real to show on
day one.

Every component's CSS references tokens as `var(--…)` only — no literal colour,
font or radius anywhere — so a system re-themes correctly in both modes. Per
Key decision 8 the base token document is always the **light** look and
`modes.dark` the dark one, including for the two dark-first systems; the tests
assert that by relative luminance. None of the bundles carries a logo, a
wordmark or anyone's marketing copy.

## The ten

**`tech-docs` — Technical Documentation.** For engineers reading API and
architecture references. Dense single column, monospace headings, code before
prose, one blue accent; callouts, signatures, parameter tables and stability
badges. Pick it when the reader is scanning for an exact name or value.

**`exec-report` — Executive Report.** For a management monthly or quarterly.
Serif headings, generous whitespace, restrained navy with a single gold
secondary, KPI cards and right-aligned tables, and a palette that survives a
grayscale printer. Pick it when the document will be forwarded and printed.

**`board-deck` — Board Presentation.** For a board or investor narrative read
as one long scroll. Very large Archivo headings, one vivid vermilion accent,
one idea per full-width section — warm off-white in light, charcoal in dark.
Pick it when the argument matters more than the detail.

**`software-manual` — Software Manual.** For administrators and operators.
Numbered procedures with verification steps, note/tip/caution/danger callouts,
setting tables and CLI blocks, teal accent. Pick it when the reader is at a
keyboard, mid-task.

**`end-user-guide` — End-User Guide.** For non-technical users who arrived
stuck. Rounded Nunito, large type, big screenshot slots, one "Do this" card per
task, warm orange at high contrast. Pick it when the reader did not choose the
software.

**`data-dashboard` — Data Dashboard.** For analysts and on-call operators. A
grid of KPI tiles and chart panels, compact scrolling tables, monospace
figures, cyan and lime. Dark-first — it is meant to be read in dark mode — but
the light base is a real light grey look, not an afterthought. Pick it when the
page lives on a second monitor all day.

**`keboola-website` — Keboola Website.** For public-facing content in
Keboola's brand: Manrope, brand blue on white with alternating tinted bands and
a deep navy inverted section, hero / feature cards / stat band / quote / CTA.
The palette and typography are derived from www.keboola.com; the brand assets
and copy are not. Pick it for launch notes, customer stories and one-pagers.

**`oldschool-memo` — Old-School Memo.** For internal memoranda that should read
as a matter of record: black Georgia on cream, TO/FROM/DATE/SUBJECT header,
numbered sections, fully ruled tables, underlined links, square corners, no
shadows, diagrams deliberately disabled. Pick it for policy notices.

**`academic-paper` — Academic Paper.** For research notes and whitepapers. A
narrow serif measure, numbered sections, numbered figures with captions,
footnotes and a reference list. Pick it when claims need to be attributable.

**`incident-postmortem` — Incident Post-mortem.** For SRE and on-call. Severity
badges, an absolute-UTC timeline, impact and action-item tables, with
red/amber/green reserved strictly for severity. Pick it when a reader who slept
through the incident must reconstruct it.

## Registering them

Validate locally first — no network, no credentials:

```bash
uv run python scripts/register_design_systems.py --dry-run
```

Then register against a deployed hub (the routes exist from 0.16.0 onwards).
`KBC_TOKEN` must belong to the project that should *own* the systems; it is
never printed:

```bash
export HUB_URL=https://your-hub.example.com
export KBC_STACK=connection.keboola.com
export KBC_TOKEN=...          # owner project's Storage API token
uv run python scripts/register_design_systems.py
```

The script lists the catalogue once, then creates each slug it does not find
and appends a version to each slug it does, printing `slug  id  version
page_url` per file. It exits non-zero on the first 4xx/5xx, reporting the
hub's own `detail`. Use `--only SLUG` (repeatable) to register a subset, and
`HUB_REGISTER_TIMEOUT_S` to change the 60-second per-request timeout.

## Changing a system

Edit the JSON, re-run `--dry-run`, and register again — an existing slug gets a
new version rather than an overwrite, so update the `note` to say what changed.
`tests/test_example_design_systems.py` re-validates every file on each test
run.
