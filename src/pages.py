"""Human-facing HTML shell pages and their shared mini design system.

Seven pages are rendered by the service itself:

- the landing page at ``/`` — what the hub is, what it does, and how to drive
  it from a terminal,
- the unlock form shown when a password-protected artifact is opened in a
  browser,
- the version picker at ``/a/{id}/versions?format=html``,
- the owner/moderation studio at ``/admin``, a single self-contained page whose
  vanilla JS talks to the same public API a terminal would, and
- the review UI at ``/a/{id}/review``, a two-pane reader that renders the
  artifact inside a sandboxed ``srcdoc`` iframe and keeps inline comment
  threads beside it,
- the changelog at ``/changelog``, this repository's ``CHANGELOG.md`` rendered
  into the same shell rather than into an artifact page, and
- the visual diff at ``/a/{id}/diff/{a}..{b}?format=visual``, two versions of
  one document side by side in their own sandboxed iframes, scrolling in step.

Artifact content itself is never templated here — it is served verbatim from
the version envelope.

**Design system.** One shared stylesheet (:data:`_CSS`) backs all pages:
light-first (a dark variant follows ``prefers-color-scheme``), monospace-forward
(JetBrains Mono for headings, labels and code; Inter for prose, both with full
local fallback stacks), a single electric-blue accent, a faint graph-paper grid
behind the page, ``//`` small-caps section labels, and dark terminal cards that
carry the ``$ curl`` examples as first-class content rather than decoration.
Google Fonts are linked but never required: every family has a local fallback
stack, so the pages degrade cleanly behind a proxy that blocks them.

Every dynamic value is escaped with :func:`html.escape` before it reaches the
markup.
"""

from __future__ import annotations

import html
import json
import re

#: Google Fonts, linked with ``display=swap``. Both families have full local
#: fallback stacks in ``--font-*`` below, so a blocked CDN costs nothing but
#: the exact letterforms.
_FONT_LINKS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    "family=Inter:wght@400;500;600&"
    'family=JetBrains+Mono:wght@400;500;700&display=swap">\n'
)

#: The shared design system. Light is the designed-for mode; the dark block
#: only re-points the tokens.
_CSS = """
:root {
  color-scheme: light dark;
  --font-mono: "JetBrains Mono", ui-monospace, SFMono-Regular, "SF Mono", Menlo,
    Consolas, "Liberation Mono", monospace;
  --font-sans: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
    "Helvetica Neue", Arial, sans-serif;

  --paper: #f6f8fb;
  --panel: #ffffff;
  --ink: #0d1622;
  --ink-2: #38455a;
  --muted: #697687;
  --line: #d8e0ea;
  --grid: #e6ecf3;
  --accent: #1442e0;
  --accent-ink: #1442e0;
  --accent-soft: #e7ecfd;
  --on-accent: #ffffff;
  --term-bg: #0d1622;
  --term-fg: #d9e3f0;
  --term-dim: #7d8ca3;
  --term-line: #24324a;
  --live: #0a7043;
  --live-soft: #dcf3e7;
  --proposed: #8a5300;
  --proposed-soft: #fbeeda;
  --danger: #b42318;
  --radius: 10px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --paper: #0b1119;
    --panel: #111a25;
    --ink: #e7edf5;
    --ink-2: #b3c0d1;
    --muted: #8e9cae;
    --line: #22303f;
    --grid: #141e2b;
    --accent: #7aa2ff;
    --accent-ink: #9dbaff;
    --accent-soft: #16233d;
    --on-accent: #0b1119;
    --term-bg: #060b12;
    --term-fg: #d9e3f0;
    --term-line: #1d2839;
    --live: #4ec98a;
    --live-soft: #10261c;
    --proposed: #e0a33f;
    --proposed-soft: #2a2010;
    --danger: #ff8a80;
  }
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: transparent;
  color: var(--ink);
  font-family: var(--font-sans);
  font-size: 16px;
  line-height: 1.65;
  -webkit-font-smoothing: antialiased;
}

/* Graph-paper texture: the only ornament on the page. Painted as a plain
   background on <html> rather than a fixed overlay, so it never becomes its
   own compositing layer. */
html {
  background-color: var(--paper);
  background-image:
    linear-gradient(var(--grid) 1px, transparent 1px),
    linear-gradient(90deg, var(--grid) 1px, transparent 1px);
  background-size: 34px 34px;
}

main { max-width: 60rem; margin: 0 auto; padding: 3.5rem 1.25rem 4rem; }

a { color: var(--accent-ink); text-decoration-thickness: 1px;
  text-underline-offset: .18em; }
a:hover { text-decoration-thickness: 2px; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px;
  border-radius: 4px; }

h1, h2, h3 { font-family: var(--font-mono); font-weight: 700;
  letter-spacing: -.02em; }
h1 { font-size: clamp(1.9rem, 1.2rem + 2.4vw, 2.9rem); line-height: 1.08;
  margin: 0 0 .6rem; }
h2 { font-size: 1.15rem; margin: 0 0 1rem; letter-spacing: -.01em; }
h3 { font-size: .95rem; margin: 0 0 .35rem; }
p { margin: .7rem 0; color: var(--ink-2); }

code, pre, .mono { font-family: var(--font-mono); font-size: .85rem; }
code { background: var(--accent-soft); color: var(--accent-ink);
  padding: .08rem .32rem; border-radius: 4px; }

/* -------- section label: "// what it does" -------------------------------- */
.label {
  font-family: var(--font-mono);
  font-size: .72rem;
  font-weight: 500;
  letter-spacing: .16em;
  text-transform: uppercase;
  color: var(--muted);
  margin: 3.25rem 0 .85rem;
  display: flex;
  align-items: center;
  gap: .6rem;
}
.label::before { content: "//"; color: var(--accent); font-weight: 700; }
.label::after { content: ""; flex: 1; height: 1px; background: var(--line); }

/* -------- badges ---------------------------------------------------------- */
.badge {
  display: inline-flex;
  align-items: center;
  gap: .4rem;
  font-family: var(--font-mono);
  font-size: .74rem;
  font-weight: 500;
  letter-spacing: .02em;
  padding: .2rem .55rem;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--panel);
  color: var(--ink-2);
  white-space: nowrap;
}
.badge--version { border-color: var(--accent); color: var(--accent-ink);
  background: var(--accent-soft); }
.badge--live { border-color: transparent; background: var(--live-soft);
  color: var(--live); }
.badge--proposed { border-color: transparent; background: var(--proposed-soft);
  color: var(--proposed); }
.badge--head { border-color: var(--accent); color: var(--accent-ink);
  background: transparent; }

/* -------- hero ------------------------------------------------------------ */
.hero { margin-bottom: 1rem; }
.hero .lead { font-size: 1.12rem; color: var(--ink-2); max-width: 44rem;
  margin: 0 0 1.25rem; }
.hero-meta { display: flex; flex-wrap: wrap; align-items: center; gap: .6rem;
  margin-bottom: 1.5rem; }
.hero-links { display: flex; flex-wrap: wrap; gap: .5rem; margin: 1.5rem 0 0; }
.hero-links a {
  font-family: var(--font-mono);
  font-size: .82rem;
  text-decoration: none;
  padding: .38rem .8rem;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--panel);
  color: var(--ink);
}
.hero-links a:hover { border-color: var(--accent); color: var(--accent-ink); }
.hero-links a.primary { background: var(--accent); border-color: var(--accent);
  color: var(--on-accent); }
.hero-links a.primary:hover { filter: brightness(1.1);
  color: var(--on-accent); }

/* -------- terminal card (the signature element) --------------------------- */
.term {
  background: var(--term-bg);
  border: 1px solid var(--term-line);
  border-radius: var(--radius);
  overflow: hidden;
  margin: .5rem 0 1.5rem;
  box-shadow: 0 1px 2px rgba(13, 22, 34, .06), 0 10px 24px rgba(13, 22, 34, .06);
}
.term-bar {
  display: flex;
  align-items: center;
  gap: .55rem;
  padding: .5rem .85rem;
  border-bottom: 1px solid var(--term-line);
  font-family: var(--font-mono);
  font-size: .72rem;
  letter-spacing: .1em;
  color: var(--term-dim);
}
.term-bar .dot { width: 9px; height: 9px; border-radius: 50%;
  background: #2f3f59; flex: none; }
.term-bar .dot:nth-child(2) { background: #2a3852; }
.term-bar .dot:nth-child(3) { background: #253048; }
.term-bar .term-title { margin-left: .35rem; }
.term pre {
  margin: 0;
  padding: 1rem 1.1rem;
  overflow-x: auto;
  color: var(--term-fg);
  font-family: var(--font-mono);
  font-size: .82rem;
  line-height: 1.75;
  white-space: pre;
}
.term code { background: none; color: inherit; padding: 0; font-size: inherit; }
.term .p { color: #5fd3a0; user-select: none; }
.term .c { color: #7d8ca3; }
.term .s { color: #ffc98a; }
.term .k { color: #8fb6ff; }

/* -------- credential switch ----------------------------------------------
   Every example carries both credential shapes and hides one, so switching
   is a class on the root element rather than a rewrite of the text. With no
   JavaScript the Storage-token form is what shows, which is the form that
   needs no sign-in to try. */
.cred-bearer { display: none; }
.auth-bearer .cred-tok { display: none; }
.auth-bearer .cred-bearer { display: inline; }

.credsw { margin-left: auto; display: flex; gap: 1px; flex: none;
  border: 1px solid var(--term-line); border-radius: 6px; overflow: hidden; }
.credsw button { font: inherit; letter-spacing: inherit; cursor: pointer;
  padding: .15rem .5rem; border: 0; background: transparent;
  color: var(--term-dim); text-transform: none; }
.credsw button:hover { color: var(--term-fg); }
.credsw button[aria-pressed="true"] { background: #223049; color: #cfe0ff; }

/* -------- feature grid ---------------------------------------------------- */
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(15.5rem, 1fr));
  gap: .8rem;
}
.card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 1rem 1.1rem 1.1rem;
}
.card h3 { font-size: .88rem; letter-spacing: .01em; color: var(--ink); }
.card h3::before { content: "┌ "; color: var(--accent); font-weight: 400; }
.card p { margin: 0; font-size: .9rem; line-height: 1.6; color: var(--muted); }

/* -------- tables ---------------------------------------------------------- */
.table-wrap { overflow-x: auto; border: 1px solid var(--line);
  border-radius: var(--radius); background: var(--panel); }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .55rem .85rem;
  border-bottom: 1px solid var(--line); vertical-align: top; font-size: .9rem; }
tr:last-child td { border-bottom: 0; }
th { font-family: var(--font-mono); font-weight: 500; font-size: .7rem;
  letter-spacing: .12em; text-transform: uppercase; color: var(--muted); }
td .mono, td code { white-space: nowrap; }

.note {
  border-left: 2px solid var(--accent);
  background: var(--panel);
  padding: .7rem 1rem;
  margin: 1rem 0;
  color: var(--muted);
  font-size: .9rem;
  border-radius: 0 var(--radius) var(--radius) 0;
}

footer {
  margin-top: 3.5rem;
  padding-top: 1.1rem;
  border-top: 1px solid var(--line);
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: .75rem;
  font-family: var(--font-mono);
  font-size: .78rem;
  color: var(--muted);
}
footer .spacer { flex: 1; }
"""

_UNLOCK_CSS = """
body { display: flex; align-items: center; justify-content: center;
  min-height: 100vh; padding: 1.25rem; }
.gate { width: 100%; max-width: 25rem; }
.gate .rule { font-family: var(--font-mono); font-size: .72rem;
  letter-spacing: .16em; text-transform: uppercase; color: var(--muted);
  margin-bottom: .6rem; }
.gate .rule::before { content: "//"; color: var(--accent); font-weight: 700;
  margin-right: .5rem; }
.gate .card { padding: 1.5rem; }
.gate h1 { font-size: 1.3rem; margin: 0 0 .3rem; }
.gate p { margin: 0 0 1.25rem; font-size: .9rem; color: var(--muted); }
label { display: block; font-family: var(--font-mono); font-size: .7rem;
  letter-spacing: .12em; text-transform: uppercase; color: var(--muted);
  margin-bottom: .4rem; }
input[type=password] {
  width: 100%;
  padding: .6rem .7rem;
  font-family: var(--font-mono);
  font-size: .9rem;
  color: var(--ink);
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: 8px;
}
button {
  margin-top: .9rem;
  width: 100%;
  padding: .6rem 1rem;
  font-family: var(--font-mono);
  font-size: .85rem;
  font-weight: 500;
  letter-spacing: .04em;
  color: var(--on-accent);
  background: var(--accent);
  border: 0;
  border-radius: 8px;
  cursor: pointer;
}
button:hover { filter: brightness(1.1); }
.error { color: var(--danger); font-family: var(--font-mono); font-size: .8rem;
  margin: .8rem 0 0; }
.hint { font-size: .8rem; margin: 1.1rem 0 0; color: var(--muted); }
"""

_VERSIONS_CSS = """
.vhead { display: flex; flex-wrap: wrap; align-items: baseline; gap: .6rem;
  margin-bottom: .4rem; }
.vlist { list-style: none; margin: 0; padding: 0; }
.vrow {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: .85rem 1rem;
  margin-bottom: .55rem;
}
.vrow.is-head { border-color: var(--accent); }
.vrow-top { display: flex; flex-wrap: wrap; align-items: center; gap: .5rem; }
.vrow-n { font-family: var(--font-mono); font-weight: 700; font-size: 1rem;
  color: var(--ink); }
.vrow-title { font-size: .92rem; color: var(--ink-2); flex: 1 1 12rem;
  min-width: 0; overflow-wrap: anywhere; }
.vrow-meta { font-family: var(--font-mono); font-size: .74rem;
  color: var(--muted); margin-top: .4rem; display: flex; flex-wrap: wrap;
  gap: .25rem .9rem; }
.vrow-note { font-size: .88rem; color: var(--ink-2); margin: .45rem 0 0;
  padding-left: .7rem; border-left: 2px solid var(--line);
  overflow-wrap: anywhere; }
.vrow-links { margin-top: .55rem; display: flex; flex-wrap: wrap; gap: .9rem;
  font-family: var(--font-mono); font-size: .78rem; }
.empty { font-family: var(--font-mono); font-size: .85rem; color: var(--muted); }
"""


#: Interactive-widget styles shared by every page whose JavaScript toggles
#: things: the studio (``/admin``) and the review UI (``/a/{id}/review``).
#: Structural tokens (colors, badges, cards, tables, terminal cards, footer)
#: stay in :data:`_CSS`.
_CONTROLS_CSS = """
/* Both pages toggle visibility with the `hidden` attribute, and several
   widgets set an explicit `display`, which would otherwise win over the
   user-agent's `[hidden] { display: none }`. */
[hidden] { display: none !important; }

/* -------- buttons --------------------------------------------------------- */
.btn {
  display: inline-flex;
  align-items: center;
  gap: .35rem;
  font-family: var(--font-mono);
  font-size: .78rem;
  font-weight: 500;
  line-height: 1.4;
  text-decoration: none;
  padding: .35rem .7rem;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
  color: var(--ink);
  cursor: pointer;
}
.btn:hover:not(:disabled) { border-color: var(--accent); color: var(--accent-ink); }
.btn:disabled { opacity: .5; cursor: progress; }
.btn-primary { background: var(--accent); border-color: var(--accent);
  color: var(--on-accent); }
.btn-primary:hover:not(:disabled) { filter: brightness(1.1);
  color: var(--on-accent); border-color: var(--accent); }
.btn-danger { color: var(--danger); border-color: var(--line); }
.btn-danger:hover:not(:disabled) { border-color: var(--danger);
  color: var(--danger); }
.btn-sm { font-size: .74rem; padding: .25rem .55rem; }
.btn-wide { width: 100%; justify-content: center; padding: .55rem 1rem;
  font-size: .85rem; margin-top: .3rem; }

/* -------- status lines ---------------------------------------------------- */
.err { font-family: var(--font-mono); font-size: .78rem; color: var(--danger);
  margin: .7rem 0 0; overflow-wrap: anywhere; }
.err::before { content: "! "; font-weight: 700; }
.loading { font-family: var(--font-mono); font-size: .8rem; color: var(--muted);
  padding: .6rem 0; }
.empty { font-family: var(--font-mono); font-size: .84rem; color: var(--muted);
  padding: .8rem 0; }
"""

#: Studio-only styles. Everything structural comes from :data:`_CSS` and every
#: button/status widget from :data:`_CONTROLS_CSS`; this block only adds what
#: the admin page introduces — the sign-in card, the artifact rows with their
#: expandable detail panel, and the preview modal.
_ADMIN_CSS = """
main { max-width: 68rem; }

.ahead { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 1rem;
  margin-bottom: .5rem; }
.ahead h1 { font-size: clamp(1.5rem, 1.1rem + 1.4vw, 2.1rem); margin: 0 0 .35rem; }
.ahead .lead { margin: 0; color: var(--ink-2); font-size: .95rem;
  max-width: 40rem; }
.ahead-right { margin-left: auto; display: flex; align-items: center;
  gap: .5rem; flex-wrap: wrap; }

/* -------- sign in --------------------------------------------------------- */
.login-card { max-width: 30rem; padding: 1.35rem 1.4rem 1.5rem; }
.login-card label { display: block; font-family: var(--font-mono);
  font-size: .7rem; letter-spacing: .12em; text-transform: uppercase;
  color: var(--muted); margin: .9rem 0 .35rem; }
.login-card label:first-of-type { margin-top: 0; }
.login-card input, .login-card select {
  width: 100%;
  padding: .55rem .65rem;
  font-family: var(--font-mono);
  font-size: .85rem;
  color: var(--ink);
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.hint { font-size: .82rem; color: var(--muted); margin: 1rem 0 0; }
.hint code { font-size: .78rem; }
/* A rule with a word in it: the flex children draw the two halves, so the
   label needs no background patch to sit on and cannot drift out of it. */
.login-or { display: flex; align-items: center; gap: .6rem; margin: 1.3rem 0 .1rem;
  font-family: var(--font-mono); font-size: .72rem; letter-spacing: .12em;
  text-transform: uppercase; color: var(--muted); }
.login-or::before, .login-or::after { content: ""; flex: 1;
  border-top: 1px solid var(--line); }

/* -------- toolbar --------------------------------------------------------- */
.toolbar { display: flex; align-items: center; gap: .6rem; margin-bottom: .7rem; }
.toolbar .spacer { flex: 1; }
.toolbar .mono { font-size: .76rem; color: var(--muted); }

/* -------- artifact rows --------------------------------------------------- */
.alist { list-style: none; margin: 0; padding: 0; }
.arow { background: var(--panel); border: 1px solid var(--line);
  border-radius: var(--radius); margin-bottom: .5rem; overflow: hidden; }
.arow.is-open { border-color: var(--accent); }
.arow-top { display: flex; flex-wrap: wrap; align-items: center; gap: .5rem;
  padding: .7rem .85rem; cursor: pointer; }
.arow-top:hover { background: var(--accent-soft); }
.arow-caret { font-family: var(--font-mono); color: var(--accent);
  width: .9rem; flex: none; }
.arow-title { font-weight: 500; color: var(--ink); flex: 1 1 12rem;
  min-width: 0; overflow-wrap: anywhere; }
.arow-badges { display: flex; flex-wrap: wrap; gap: .3rem; }
.arow-date { font-size: .72rem; color: var(--muted); white-space: nowrap; }
.idcopy {
  font-family: var(--font-mono);
  font-size: .74rem;
  color: var(--muted);
  background: var(--paper);
  border: 1px dashed var(--line);
  border-radius: 6px;
  padding: .12rem .4rem;
  cursor: pointer;
}
.idcopy:hover { border-color: var(--accent); color: var(--accent-ink); }
.idcopy.copied { border-style: solid; border-color: var(--live);
  color: var(--live); }

/* -------- detail panel ---------------------------------------------------- */
.apanel { border-top: 1px solid var(--line); padding: .85rem;
  background: var(--paper); }
.acontrols { display: flex; flex-wrap: wrap; align-items: center; gap: .5rem;
  margin-bottom: .8rem; }
.switch { display: inline-flex; align-items: center; gap: .4rem;
  font-family: var(--font-mono); font-size: .76rem; color: var(--ink-2);
  cursor: pointer; }
.switch input { accent-color: var(--accent); }
.vactions { display: flex; flex-wrap: wrap; gap: .3rem; }
.apanel table td { font-size: .82rem; }
.apanel .vnote { color: var(--muted); font-size: .8rem;
  overflow-wrap: anywhere; }

/* -------- panel sub-sections (stats, webhooks, invitations) --------------- */
.asec { border: 1px solid var(--line); border-radius: var(--radius);
  background: var(--panel); padding: .7rem .8rem; margin-bottom: .7rem; }
.asec h4 { font-family: var(--font-mono); font-size: .7rem; letter-spacing: .12em;
  text-transform: uppercase; color: var(--muted); margin: 0 0 .5rem;
  font-weight: 500; }
.asec .hint { margin: .5rem 0 0; font-size: .78rem; }
.asec-row { display: flex; flex-wrap: wrap; align-items: center; gap: .4rem; }
.asec-row input { flex: 1 1 16rem; min-width: 0; padding: .35rem .55rem;
  font-family: var(--font-mono); font-size: .78rem; color: var(--ink);
  background: var(--paper); border: 1px solid var(--line); border-radius: 8px; }

.chips { list-style: none; margin: 0 0 .5rem; padding: 0; display: flex;
  flex-direction: column; gap: .3rem; }
.chip { display: flex; flex-wrap: wrap; align-items: center; gap: .45rem;
  background: var(--paper); border: 1px solid var(--line); border-radius: 8px;
  padding: .3rem .5rem; }
.chip .chip-main { flex: 1 1 12rem; min-width: 0; font-family: var(--font-mono);
  font-size: .76rem; color: var(--ink); overflow-wrap: anywhere; }
.chip.is-off .chip-main { color: var(--muted); text-decoration: line-through; }

.spark { display: flex; align-items: flex-end; gap: 2px; height: 2.2rem;
  margin: .4rem 0 .2rem; }
.spark i { flex: 1; min-width: 2px; background: var(--accent-soft);
  border-top: 2px solid var(--accent); border-radius: 2px 2px 0 0; }
.stat-total { font-family: var(--font-mono); font-size: 1.15rem;
  font-weight: 700; color: var(--ink); }

/* -------- one-time secret ------------------------------------------------- */
.once { font-family: var(--font-mono); font-size: .76rem;
  background: var(--term-bg); color: var(--term-fg);
  border: 1px solid var(--term-line); border-radius: 8px;
  padding: .6rem .7rem; margin: .5rem 0; overflow-wrap: anywhere;
  user-select: all; }
.once-warn { color: var(--proposed); font-family: var(--font-mono);
  font-size: .76rem; margin: 0 0 .3rem; }

/* -------- preview modal --------------------------------------------------- */
.modal { position: fixed; inset: 0; background: rgba(6, 11, 18, .62);
  display: flex; align-items: center; justify-content: center; padding: 1.5rem;
  z-index: 50; }
.modal[hidden] { display: none; }
.modal-box { width: 100%; max-width: 62rem; height: 85vh; display: flex;
  flex-direction: column; background: var(--panel); border: 1px solid var(--line);
  border-radius: var(--radius); overflow: hidden; }
.modal-bar { display: flex; align-items: center; gap: .6rem;
  padding: .5rem .7rem; border-bottom: 1px solid var(--line); }
.modal-bar .spacer { flex: 1; }
.modal-title { font-size: .76rem; color: var(--muted); overflow-wrap: anywhere; }
.modal iframe { flex: 1; width: 100%; border: 0; background: #fff; }
"""

#: The credential every studio page holds, in one place: reading and writing
#: the one ``sessionStorage`` entry, choosing the header each kind of
#: credential belongs in, renewing a Keboola session, revoking it, and telling
#: a 401 about the credential apart from a 401 about the document.
#:
#: ``/admin``, ``/a/{id}/review`` and ``/login`` all hold the same record, so
#: all three take it from here: a second copy of any of this is a copy that
#: can disagree with the others. Installed as ``window.hubSession`` by its own
#: ``<script>``, ahead of the page's, and it reads ``window.HUB_BASE`` the way
#: every other snippet here does.
_SESSION_JS = """
(function () {
  "use strict";

  var BASE = String(window.HUB_BASE || "").replace(/\\/+$/, "");

  /* The credential lives in the page's closure and in sessionStorage, and
     nowhere else: not in a cookie, not in the URL, and not in any web storage
     that outlives the tab. sessionStorage is per-tab and cleared when the tab
     closes, so a reload keeps the session while closing the tab ends it. */
  var AUTH_KEY = "hub_admin_auth";

  /* The renewal in flight, if any. See renew(). */
  var renewing = null;

  function read() {
    try {
      var raw = window.sessionStorage.getItem(AUTH_KEY);
      if (!raw) { return null; }
      var parsed = JSON.parse(raw);
      if (parsed && parsed.token && parsed.stack) {
        /* Copied rather than picked apart field by field, so a record written
           by a newer /login keeps whatever else it carries. project and
           refresh come from /login only: a Keboola session names the project
           it acts as and can be renewed, a pasted Storage token does
           neither. One entry holds both shapes. */
        var record = Object.assign({}, parsed);
        record.project = parsed.project || null;
        record.refresh = parsed.refresh || null;
        return record;
      }
    } catch (err) {
      /* Storage disabled or unreadable: just sign in again. */
    }
    return null;
  }

  /* False when this browser refused to keep the record. A page that already
     holds the credential in memory can carry on — the session just will not
     survive a reload; /login, which has nowhere else to put it, says so. */
  function write(record) {
    try {
      window.sessionStorage.setItem(AUTH_KEY, JSON.stringify(record));
      return true;
    } catch (err) {
      return false;
    }
  }

  function forget() {
    try { window.sessionStorage.removeItem(AUTH_KEY); } catch (err) {}
  }

  /* Each kind of credential goes in the header that kind belongs in: a
     Storage API token in X-StorageApi-Token, a Keboola sign-in in the
     standard Authorization: Bearer. The hub refuses the other way round, and
     rightly — they are different credentials with different scopes.

     X-Storage-Project rides along with a sign-in, which is scoped to a person
     rather than to one project. X-Storage-Project, not X-Kbc-*: the data-app
     proxy strips that family before the hub ever sees it. */
  function headers(record, out) {
    if (/^kbc_(at|pat)_/.test(record.token)) {
      out["Authorization"] = "Bearer " + record.token;
    } else {
      out["X-StorageApi-Token"] = record.token;
    }
    out["X-Storage-Stack"] = record.stack;
    if (record.project) { out["X-Storage-Project"] = String(record.project); }
    return out;
  }

  async function exchange(record) {
    var resp = await fetch(BASE + "/login/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stack: record.stack, refresh_token: record.refresh })
    });
    if (!resp.ok) { return null; }
    var data = null;
    try { data = await resp.json(); } catch (err) { return null; }
    if (!data || !data.credential) { return null; }
    /* Merged onto the record, not rebuilt from a fixed field list: a renewal
       replaces the two tokens and must not quietly drop anything else the
       record holds. */
    var next = Object.assign({}, record, {
      token: data.credential.access_token,
      refresh: data.credential.refresh_token
    });
    write(next);
    return next;
  }

  /* A Keboola session ages out after an hour; a pasted Storage token does
     not. Rotating the refresh token in place keeps a tab that was left open
     working instead of sending the visitor back to sign in. Resolves to the
     renewed record, or null when there is nothing to renew and when the stack
     refused.

     One exchange at a time, and every caller awaits that same one: the stack
     spends the refresh token on the first request, so two 401s renewing
     independently would have the second told its session is dead — and it
     would have spent a sign-in slot to be told so. */
  function renew(record) {
    if (!record || !record.refresh) { return Promise.resolve(null); }
    if (!renewing) {
      renewing = exchange(record).catch(function () {
        /* Offline, or a refusal: the caller reports the failure it already
           had, which is more useful than one about the renewal. */
        return null;
      }).then(function (value) {
        renewing = null;
        return value;
      });
    }
    return renewing;
  }

  /* Signing out of a Keboola session revokes it on its stack, so the tokens
     are dead rather than merely forgotten here. Fire-and-forget: ending the
     local session must not wait on the network, or be blocked by it. */
  function end(record) {
    if (!record || !record.refresh) { return; }
    var payload = JSON.stringify({ stack: record.stack, token: record.refresh });
    try {
      fetch(BASE + "/login/signout", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: payload,
        keepalive: true
      }).catch(function () {});
    } catch (err) { /* offline: the session expires on its own */ }
  }

  /* Only the credential being refused says anything about the credential. A
     502 means the hub's own Storage is unavailable, which is the same for
     every visitor and is not a reason to throw away a working sign-in. */
  function rejected(err) {
    return !!err && (err.status === 401 || err.status === 403);
  }

  /* The reader gate's own 401 body: {"error": "password required"}. That is a
     401 about the *document*, not about the credential — an owner who is
     signed in gets it too — so renewing over it would spend a refresh
     token, and a sign-in slot, to be told exactly the same thing again. */
  function lockedBody(data) {
    return !!data && data.error === "password required";
  }

  function locked(err) {
    return !!err && err.status === 401 && lockedBody(err.payload);
  }

  window.hubSession = {
    read: read,
    write: write,
    forget: forget,
    headers: headers,
    renew: renew,
    end: end,
    rejected: rejected,
    locked: locked,
    lockedBody: lockedBody
  };
})();
"""

#: The whole studio, as one IIFE. Deliberately dependency-free and readable:
#: it only ever talks to the endpoints a terminal could call with curl, using
#: the two management headers the visitor supplied. Session handling comes
#: from :data:`_SESSION_JS`, which every page that holds a credential shares.
_ADMIN_JS = """
(function () {
  "use strict";

  var BASE = String(window.HUB_BASE || "").replace(/\\/+$/, "");

  /* Reading, renewing and revoking the credential, shared with every other
     page that holds one. This closure keeps the record itself. */
  var SESSION = window.hubSession;
  var auth = null;

  /* One live watch per *expanded* detail panel. A collapsed row polls
     nothing, and re-rendering or leaving the listing stops every watch it
     owns, so a studio left open overnight never accumulates them. */
  var watchers = [];

  function stopWatchers() {
    watchers.forEach(function (handle) {
      try { handle.stop(); } catch (err) { /* already gone */ }
    });
    watchers = [];
  }

  function $(id) { return document.getElementById(id); }
  function show(node, on) { node.hidden = !on; }

  /* Tiny escaper, kept for any place that must build markup as a string.
     Everything below prefers textContent, which cannot inject markup at all. */
  function esc(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }

  function badge(text, kind) {
    return el("span", "badge" + (kind ? " badge--" + kind : ""), text);
  }

  function setError(node, message) {
    node.textContent = message || "";
    node.hidden = !message;
  }

  /* ---------------------------------------------------------------- auth */

  /* The renewed record has to land in this closure as well as in storage,
     which is the one thing the shared session module cannot do for us. */
  async function renewSession() {
    var next = await SESSION.renew(auth);
    if (!next) { return false; }
    auth = next;
    return true;
  }

  function headers(withBody) {
    var out = SESSION.headers(auth, {});
    if (withBody) { out["Content-Type"] = "application/json"; }
    return out;
  }

  /* --------------------------------------------------------------- fetch */

  function apiMessage(status, data, text) {
    if (data && typeof data.detail === "string") { return data.detail; }
    if (data && data.detail) { return JSON.stringify(data.detail); }
    if (data && data.error) {
      return data.error + (data.detail ? " — " + data.detail : "");
    }
    if (text) { return "HTTP " + status + ": " + text.slice(0, 300); }
    return "HTTP " + status;
  }

  async function request(path, options) {
    var opts = options || {};
    var hasBody = opts.body !== undefined;
    var body = hasBody ? JSON.stringify(opts.body) : undefined;
    async function attempt() {
      var resp = await fetch(BASE + path, {
        method: opts.method || "GET",
        headers: headers(hasBody),
        body: body
      });
      var text = await resp.text();
      var data = null;
      try { data = text ? JSON.parse(text) : null; } catch (err) { data = null; }
      return { resp: resp, text: text, data: data };
    }
    var out = await attempt();
    /* One retry, and only on a 401 that is about the session: an aged-out
       access token is the single failure this page can put right by itself.
       The body decides, so the reader gate's own 401 does not spend a
       refresh token to be answered with the same 401 again. */
    if (out.resp.status === 401 && !SESSION.lockedBody(out.data)
        && await renewSession()) {
      out = await attempt();
    }
    if (!out.resp.ok) {
      var failure = new Error(apiMessage(out.resp.status, out.data, out.text));
      failure.status = out.resp.status;
      throw failure;
    }
    return out.data;
  }

  /* Proposed versions and diffs are 403 for an anonymous browser tab, so they
     are fetched with the auth headers and rendered into a sandboxed iframe
     rather than opened as a plain link. */
  async function requestHtml(path) {
    var resp = await fetch(BASE + path, { headers: headers(false) });
    var text = await resp.text();
    if (!resp.ok) {
      var data = null;
      try { data = JSON.parse(text); } catch (err) { data = null; }
      throw new Error(apiMessage(resp.status, data, text));
    }
    return text;
  }

  /* --------------------------------------------------------------- modal */

  function openModal(title, htmlText) {
    $("modal-title").textContent = title;
    /* srcdoc + a sandbox WITHOUT allow-same-origin: the preview runs in an
       opaque origin, so nothing inside an artifact can read this page's
       sessionStorage and steal the token. */
    $("modal-frame").srcdoc = htmlText;
    show($("modal"), true);
  }

  function closeModal() {
    show($("modal"), false);
    $("modal-frame").srcdoc = "";
  }

  /* A second, content-only modal. The preview modal above hands untrusted
     artifact HTML to a sandboxed iframe; this one shows nodes this script
     built itself, which is what a copy button needs to live in. */
  function openNote(title, nodes) {
    $("note-title").textContent = title;
    var body = $("note-body");
    body.textContent = "";
    nodes.forEach(function (node) { body.appendChild(node); });
    show($("note-modal"), true);
  }

  function closeNote() {
    show($("note-modal"), false);
    $("note-body").textContent = "";
  }

  /* ------------------------------------------------------------ clipboard */

  function copy(text, node, restore) {
    function done() {
      node.classList.add("copied");
      node.textContent = "copied";
      window.setTimeout(function () {
        node.classList.remove("copied");
        node.textContent = restore;
      }, 900);
    }
    function fallback() { window.prompt("Copy this:", text); }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, fallback);
    } else {
      fallback();
    }
  }

  /* ------------------------------------------------------- artifact list */

  function renderArtifacts(rows) {
    /* Every row is about to be thrown away; its watch goes with it. */
    stopWatchers();
    var list = $("artifacts");
    list.textContent = "";
    show($("empty"), rows.length === 0);
    $("count").textContent =
      rows.length + (rows.length === 1 ? " artifact" : " artifacts");
    rows.forEach(function (row) { list.appendChild(artifactRow(row)); });
  }

  function artifactRow(row) {
    var item = el("li", "arow");
    var top = el("div", "arow-top");
    top.setAttribute("role", "button");
    top.tabIndex = 0;

    var caret = el("span", "arow-caret", "\\u25b8");
    top.appendChild(caret);
    top.appendChild(el("span", "arow-title", row.title || "untitled"));

    var idBtn = el("button", "idcopy", row.id);
    idBtn.type = "button";
    idBtn.title = "Copy the artifact id";
    idBtn.addEventListener("click", function (event) {
      event.stopPropagation();
      copy(row.id, idBtn, row.id);
    });
    top.appendChild(idBtn);

    var badges = el("span", "arow-badges");
    var date = el("span", "arow-date");

    /* Rebuilt rather than patched, so a live refresh of an expanded row can
       call it again and get exactly the row a fresh listing would render. */
    function renderBadges() {
      badges.textContent = "";
      /* Status first: a trashed artifact's public link is dead, and that is
         the single most important thing about the row. */
      if (row.status === "trashed") {
        badges.appendChild(badge("trashed \\u00b7 link dead", "proposed"));
      } else if (row.status === "final") {
        badges.appendChild(badge("final"));
      }
      if (row.proposed_count) {
        badges.appendChild(badge(
          row.proposed_count +
            (row.proposed_count === 1 ? " proposal" : " proposals"),
          "proposed"
        ));
      }
      if (row.accept_versions) { badges.appendChild(badge("accepting versions")); }
      if (row.protected) { badges.appendChild(badge("protected")); }
      if (row.webhooks_count) {
        badges.appendChild(badge(
          row.webhooks_count +
            (row.webhooks_count === 1 ? " webhook" : " webhooks")
        ));
      }
      if (row.head_version) {
        badges.appendChild(badge("head v" + row.head_version, "head"));
      }
      date.textContent = String(row.updated_at || "").replace("T", " ");
    }

    renderBadges();
    top.appendChild(badges);
    top.appendChild(date);

    var panel = el("div", "apanel");
    panel.hidden = true;

    /* The panel's own watch, alive only while it is expanded. */
    var watch = null;

    function stopWatch() {
      if (!watch) { return; }
      try { watch.stop(); } catch (err) { /* already gone */ }
      var at = watchers.indexOf(watch);
      if (at !== -1) { watchers.splice(at, 1); }
      watch = null;
    }

    /* A change arrives while the operator is looking at the panel: rebuild
       its contents (versions table, proposal count, badges) in place. The row
       stays expanded and the page keeps its scroll offset, so nobody loses
       their place mid-moderation. */
    function onLive(next) {
      if (panel.hidden) { return; }
      var at = window.pageYOffset;
      row.head_version = next.head_version;
      row.proposed_count = next.proposed_count;
      row.status = next.document_status;
      row.updated_at = next.updated_at;
      renderBadges();
      loadPanel(row, panel).then(function () {
        window.scrollTo(0, at);
      });
    }

    function toggle() {
      var opening = panel.hidden;
      panel.hidden = !opening;
      item.classList.toggle("is-open", opening);
      caret.textContent = opening ? "\\u25be" : "\\u25b8";
      if (!opening) { stopWatch(); return; }
      loadPanel(row, panel);
      /* A trashed or purged artifact answers 404 here and the watch stops
         itself — no special case needed for it. */
      watch = window.AHLive.watch({
        base: BASE,
        id: row.share_id || row.id,
        onChange: onLive
      });
      watchers.push(watch);
    }

    top.addEventListener("click", toggle);
    top.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        toggle();
      }
    });

    item.appendChild(top);
    item.appendChild(panel);
    return item;
  }

  /* ------------------------------------------------------- detail panel */

  async function loadPanel(row, panel) {
    panel.textContent = "";
    panel.appendChild(el("div", "loading", "loading versions\\u2026"));
    try {
      /* The JSON history (not ?format=html) carries the proposal metadata,
         and the auth headers make proposals visible to the owner. Every /a/
         path is addressed by share_id, which is what the public URL carries
         once the owner has rotated the link; older listings without the field
         fall back to the id, where the two are still the same thing.

         A trashed artifact has no public link at all — /a/{id}/versions is a
         404 by design — so its history is skipped and the panel opens on the
         restore/purge controls, which is all there is to do with it. */
      var data = row.status === "trashed"
        ? { versions: [], head_version: null }
        : await request(
            "/a/" + encodeURIComponent(row.share_id || row.id) + "/versions"
          );
      var invitations = [];
      try {
        var invited = await request(
          "/api/artifacts/" + encodeURIComponent(row.id) + "/invitations"
        );
        invitations = (invited && invited.invitations) || [];
      } catch (err) {
        /* Guests are a side panel, not the point of this view: an artifact
           whose invitations cannot be read still opens for moderation. */
      }
      renderPanel(row, panel, data, invitations);
    } catch (err) {
      panel.textContent = "";
      var box = el("p", "err", err.message);
      panel.appendChild(box);
    }
  }

  function renderPanel(row, panel, data, invitations) {
    panel.textContent = "";

    /* Two identifiers, two jobs: `id` is the internal handle every /api/*
       call addresses, `pubId` is the share id every /a/ URL carries. They are
       equal until the owner rotates the link. */
    var id = encodeURIComponent(row.id);
    var share = row.share_id || row.id;
    var pubId = encodeURIComponent(share);
    var head = data.head_version;
    var publicUrl = BASE + "/a/" + share;
    var trashed = row.status === "trashed";

    var errBox = el("p", "err");
    errBox.hidden = true;

    function refresh() { loadPanel(row, panel); }

    function action(label, kind, handler) {
      var btn = el("button", "btn btn-sm" + (kind ? " btn-" + kind : ""), label);
      btn.type = "button";
      btn.addEventListener("click", async function () {
        btn.disabled = true;
        setError(errBox, "");
        try {
          await handler();
        } catch (err) {
          setError(errBox, err.message);
        } finally {
          btn.disabled = false;
        }
      });
      return btn;
    }

    /* ---- artifact-level controls ---- */
    var controls = el("div", "acontrols");

    var toggleLabel = el("label", "switch");
    var checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = !!data.accept_versions;
    checkbox.addEventListener("change", async function () {
      checkbox.disabled = true;
      setError(errBox, "");
      try {
        await request("/api/artifacts/" + id, {
          method: "PUT",
          body: { accept_versions: checkbox.checked }
        });
        refresh();
      } catch (err) {
        checkbox.checked = !checkbox.checked;
        setError(errBox, err.message);
      } finally {
        checkbox.disabled = false;
      }
    });
    toggleLabel.appendChild(checkbox);
    toggleLabel.appendChild(el("span", null, "accept versions from other projects"));
    controls.appendChild(toggleLabel);

    var openLink = el("a", "btn btn-sm", "Open artifact");
    openLink.href = publicUrl;
    openLink.target = "_blank";
    openLink.rel = "noopener";
    controls.appendChild(openLink);

    /* The review UI: the same document with its inline comment threads. */
    var reviewLink = el("a", "btn btn-sm", "Review");
    reviewLink.href = publicUrl + "/review";
    reviewLink.target = "_blank";
    reviewLink.rel = "noopener";
    controls.appendChild(reviewLink);

    var copyBtn = el("button", "btn btn-sm", "Copy public URL");
    copyBtn.type = "button";
    copyBtn.addEventListener("click", function () {
      copy(publicUrl, copyBtn, "Copy public URL");
    });
    controls.appendChild(copyBtn);

    controls.appendChild(action("Serve latest", "", async function () {
      await request("/api/artifacts/" + id + "/head", {
        method: "PUT",
        body: { mode: "latest" }
      });
      refresh();
    }));

    /* Rotating mints a new share id, so every URL on this page — and every
       link anybody was ever sent — changes. The whole listing is reloaded
       afterwards rather than this one panel. */
    controls.appendChild(action("Rotate link", "danger", async function () {
      if (!window.confirm(
        "Rotate the public link of this artifact?\\n\\n" +
        "The current URL stops working immediately and there is no way back. " +
        "Everyone who should still have access needs the new link."
      )) { return; }
      var result = await request("/api/artifacts/" + id + "/rotate-link",
        { method: "POST" });
      showNewLink(result);
      reload();
    }));

    if (trashed) {
      controls.appendChild(action("Restore", "primary", async function () {
        await request("/api/artifacts/" + id + "/restore", { method: "POST" });
        reload();
      }));
    } else {
      controls.appendChild(action("Trash", "danger", async function () {
        if (!window.confirm(
          "Move this artifact to the trash?\\n\\n" +
          "Its public link stops resolving and new versions and comments are " +
          "frozen. You can restore it later on the same URL."
        )) { return; }
        await request("/api/artifacts/" + id, { method: "DELETE" });
        reload();
      }));
    }

    /* Purge is the one irreversible button in the studio, so it asks for the
       word rather than for a click: a mis-aimed Enter cannot trigger it. */
    controls.appendChild(action("Purge", "danger", async function () {
      var typed = window.prompt(
        "This permanently erases every version, comment thread and view " +
        "statistic of this artifact. It cannot be undone.\\n\\n" +
        "Type PURGE to confirm:"
      );
      if (String(typed || "").trim().toUpperCase() !== "PURGE") { return; }
      await request("/api/artifacts/" + id + "/purge", { method: "DELETE" });
      reload();
    }));

    panel.appendChild(controls);
    panel.appendChild(errBox);

    panel.appendChild(statsSection(id, action));
    panel.appendChild(webhookSection(row, id, action));
    panel.appendChild(invitationSection(row, id, action, invitations || []));

    /* ---- version table ---- */
    var versions = data.versions || [];
    if (!versions.length) {
      panel.appendChild(el("p", "empty",
        trashed ? "in the trash \\u2014 restore it to see its versions"
                : "no versions"));
      return;
    }

    var wrap = el("div", "table-wrap");
    var table = document.createElement("table");
    var thead = document.createElement("tr");
    ["version", "status", "author", "note", "created", "actions"].forEach(
      function (name) { thead.appendChild(el("th", null, name)); }
    );
    table.appendChild(thead);

    versions.forEach(function (version) {
      var n = version.version;
      var proposed = version.status === "proposed";
      var tr = document.createElement("tr");

      tr.appendChild(el("td", "mono", "v" + n));

      var statusCell = el("td", "arow-badges");
      statusCell.appendChild(badge(version.status || "live",
        proposed ? "proposed" : "live"));
      if (version.is_head) { statusCell.appendChild(badge("HEAD", "head")); }
      tr.appendChild(statusCell);

      var author = version.author || {};
      tr.appendChild(el("td", null,
        author.project_name || author.project_id || "unknown"));
      tr.appendChild(el("td", "vnote", version.note || "\\u2014"));
      tr.appendChild(el("td", "mono",
        String(version.created_at || "").replace("T", " ")));

      var actions = el("td", "vactions");

      actions.appendChild(action("View", "", async function () {
        if (proposed) {
          var body = await requestHtml("/a/" + pubId + "/v/" + n);
          openModal("proposed v" + n + " \\u2014 " + row.id, body);
        } else {
          window.open(publicUrl + "/v/" + n, "_blank", "noopener");
        }
      }));

      if (head !== null && head !== undefined && n !== head) {
        actions.appendChild(action("Diff vs head", "", async function () {
          var body = await requestHtml(
            "/a/" + pubId + "/diff/" + head + ".." + n + "?format=html"
          );
          openModal("diff v" + head + "..v" + n + " \\u2014 " + row.id, body);
        }));
      }

      if (proposed) {
        actions.appendChild(action("Promote", "primary", async function () {
          if (!window.confirm(
            "Promote v" + n + " to live? It becomes servable immediately."
          )) { return; }
          await request("/api/artifacts/" + id + "/versions/" + n + "/promote",
            { method: "POST" });
          refresh();
        }));
        actions.appendChild(action("Reject", "danger", async function () {
          if (!window.confirm(
            "Reject proposal v" + n + "? It is deleted permanently."
          )) { return; }
          await request("/api/artifacts/" + id + "/versions/" + n,
            { method: "DELETE" });
          refresh();
        }));
      } else {
        actions.appendChild(action("Pin here", "", async function () {
          await request("/api/artifacts/" + id + "/head", {
            method: "PUT",
            body: { mode: "pinned", version: n }
          });
          refresh();
        }));
        if (n !== head) {
          actions.appendChild(action("Delete", "danger", async function () {
            if (!window.confirm(
              "Delete version v" + n + "? This is permanent."
            )) { return; }
            await request("/api/artifacts/" + id + "/versions/" + n,
              { method: "DELETE" });
            refresh();
          }));
        }
      }

      tr.appendChild(actions);
      table.appendChild(tr);
    });

    wrap.appendChild(table);
    panel.appendChild(wrap);
  }

  /* ------------------------------------------------- panel sub-sections */

  function section(heading) {
    var box = el("div", "asec");
    box.appendChild(el("h4", null, heading));
    return box;
  }

  /* A copyable block for something the server will never show again. */
  function onceBlock(label, value) {
    var wrap = el("div", null);
    wrap.appendChild(el("p", "once-warn", label));
    wrap.appendChild(el("div", "once", value));
    var btn = el("button", "btn btn-sm btn-primary", "Copy link");
    btn.type = "button";
    btn.addEventListener("click", function () { copy(value, btn, "Copy link"); });
    wrap.appendChild(btn);
    return wrap;
  }

  function showNewLink(result) {
    var url = result.url || (BASE + "/a/" + result.share_id);
    openNote("new public link", [
      onceBlock("The previous link is dead as of now. The new one:", url),
      el("p", "hint", result.warning ||
        "Reshare this URL with everyone who should still have access.")
    ]);
  }

  /* ---- view statistics ---- */

  function statsSection(id, action) {
    var box = section("views");
    var out = el("div", null);
    out.appendChild(el("p", "hint",
      "Read counts per surface and per day. Numbers only \\u2014 no reader " +
      "identity, address or referrer is recorded."));

    box.appendChild(action("Load stats", "", async function () {
      var data = await request("/api/artifacts/" + id + "/stats");
      renderStats(out, data);
    }));
    box.appendChild(out);
    return box;
  }

  function renderStats(out, data) {
    out.textContent = "";
    var total = Number(data.total || 0);
    out.appendChild(el("div", "stat-total", total + (total === 1 ? " view" : " views")));

    var days = data.by_day || [];
    if (days.length) {
      var peak = days.reduce(function (top, day) {
        return Math.max(top, Number(day.count || 0));
      }, 1);
      var spark = el("div", "spark");
      days.forEach(function (day) {
        var bar = document.createElement("i");
        bar.style.height =
          Math.max(6, Math.round((Number(day.count || 0) / peak) * 100)) + "%";
        bar.title = day.day + ": " + day.count;
        spark.appendChild(bar);
      });
      out.appendChild(spark);

      /* The bars carry the shape; the list carries the numbers, newest first
         and short enough to read at a glance. */
      var recent = days.slice(-7).reverse().map(function (day) {
        return day.day + "  " + day.count;
      });
      out.appendChild(el("p", "mono hint", recent.join("   \\u00b7   ")));
    } else {
      out.appendChild(el("p", "hint", "nobody has opened this artifact yet"));
    }

    var kinds = data.by_kind || {};
    var names = Object.keys(kinds);
    if (names.length) {
      out.appendChild(el("p", "mono hint", names.map(function (kind) {
        return kind + " " + kinds[kind];
      }).join("   \\u00b7   ")));
    }
  }

  /* ---- webhooks ---- */

  function webhookSection(row, id, action) {
    var box = section("webhooks");
    var body = el("div", null);
    /* A webhook URL is a capability (a Slack hook's path *is* its
       credential), so the hub returns the list only in the PUT that sets it —
       GET /api/artifacts reports a count. That is why an artifact with hooks
       already configured starts out unknown here: the only honest edit is a
       replacement, until a save teaches this tab what the set is. */
    var hooks = row.webhooks_count ? null : [];

    function save(next) {
      return async function () {
        var result = await request("/api/artifacts/" + id, {
          method: "PUT",
          body: { webhooks: next }
        });
        hooks = result.webhooks || [];
        row.webhooks_count = hooks.length;
        render();
      };
    }

    function render() {
      body.textContent = "";

      if (hooks === null) {
        body.appendChild(el("p", "hint",
          "This artifact has " + row.webhooks_count + " webhook URL(s). The " +
          "hub never lists them again after they are set, so they cannot be " +
          "removed one by one here \\u2014 saving below replaces the whole set."));
      } else if (hooks.length) {
        var list = el("ul", "chips");
        hooks.forEach(function (url) {
          var chip = el("li", "chip");
          chip.appendChild(el("span", "chip-main", url));
          chip.appendChild(action("Remove", "danger", save(
            hooks.filter(function (other) { return other !== url; })
          )));
          list.appendChild(chip);
        });
        body.appendChild(list);
      } else {
        body.appendChild(el("p", "hint",
          "No webhooks. Add an https URL to be notified about new versions, " +
          "proposals and comments; a hooks.slack.com URL gets Slack's own " +
          "message shape."));
      }

      var line = el("div", "asec-row");
      var input = document.createElement("input");
      input.type = "url";
      input.placeholder = "https://hooks.slack.com/services/...";
      input.spellcheck = false;
      line.appendChild(input);
      line.appendChild(action(
        hooks === null ? "Replace all" : "Add",
        "",
        async function () {
          var url = input.value.trim();
          if (!url) { return; }
          if (hooks === null && !window.confirm(
            "Replace all " + row.webhooks_count + " existing webhook URL(s) " +
            "with this one?"
          )) { return; }
          await save((hooks || []).concat([url]))();
        }
      ));
      body.appendChild(line);
    }

    render();
    box.appendChild(body);
    return box;
  }

  /* ---- guest invitations ---- */

  function invitationSection(row, id, action, invitations) {
    var box = section("guests");
    var body = el("div", null);
    var list = invitations.slice();

    function render() {
      body.textContent = "";
      var live = list.filter(function (inv) { return !inv.revoked; });

      if (list.length) {
        var chips = el("ul", "chips");
        list.forEach(function (inv) {
          var chip = el("li", "chip" + (inv.revoked ? " is-off" : ""));
          chip.appendChild(el("span", "chip-main",
            inv.name + (inv.revoked ? "  (revoked)" : "")));
          chip.appendChild(el("span", "arow-date",
            String(inv.created_at || "").replace("T", " ")));
          if (!inv.revoked) {
            chip.appendChild(action("Revoke", "danger", async function () {
              if (!window.confirm(
                "Revoke the invitation for " + inv.name + "?\\n\\n" +
                "Their link stops working immediately. Comments they already " +
                "left stay, and every other guest keeps their access."
              )) { return; }
              await request("/api/artifacts/" + id + "/invitations/" +
                encodeURIComponent(inv.id), { method: "DELETE" });
              inv.revoked = true;
              render();
            }));
          }
          chips.appendChild(chip);
        });
        body.appendChild(chips);
      }

      body.appendChild(el("p", "hint",
        live.length
          ? live.length + " active invitation(s). A guest can comment, reply " +
            "and resolve their own threads \\u2014 nothing else."
          : "Invite someone without a Keboola account to comment. They get a " +
            "review link that works only for them and only for this artifact."));

      var line = el("div", "asec-row");
      var input = document.createElement("input");
      input.type = "text";
      input.maxLength = 80;
      input.placeholder = "who is this for? e.g. Jana (legal)";
      line.appendChild(input);
      line.appendChild(action("Invite", "primary", async function () {
        var name = input.value.trim();
        if (!name) { return; }
        var result = await request("/api/artifacts/" + id + "/invitations", {
          method: "POST",
          body: { name: name }
        });
        input.value = "";
        list.push({
          id: result.invitation_id,
          name: result.name,
          created_at: "",
          revoked: false
        });
        render();
        openNote("invitation for " + result.name, [
          onceBlock(
            "This link is shown once and cannot be recovered \\u2014 copy it " +
            "now and send it to " + result.name + ".",
            result.review_url
          ),
          el("p", "hint", result.warning ||
            "Anyone holding this link can comment as " + result.name +
            " until you revoke it.")
        ]);
      }));
      body.appendChild(line);
    }

    render();
    box.appendChild(body);
    return box;
  }

  /* ------------------------------------------------------------ sessions */

  function enterStudio(data) {
    show($("login"), false);
    show($("studio"), true);
    show($("logout"), true);
    var pill = $("project-badge");
    pill.textContent = "project " + (data.project_id || "?") + " \\u00b7 " + auth.stack;
    show(pill, true);
    renderArtifacts(data.artifacts || []);
  }

  function leaveStudio() {
    stopWatchers();
    SESSION.end(auth);
    SESSION.forget();
    auth = null;
    $("artifacts").textContent = "";
    $("token").value = "";
    show($("studio"), false);
    show($("logout"), false);
    show($("project-badge"), false);
    show($("login"), true);
  }

  async function reload() {
    setError($("list-error"), "");
    show($("loading"), true);
    try {
      var data = await request("/api/artifacts");
      renderArtifacts(data.artifacts || []);
    } catch (err) {
      setError($("list-error"), err.message);
    } finally {
      show($("loading"), false);
    }
  }

  /* --------------------------------------------------------------- wiring */

  $("stack").addEventListener("change", function () {
    show($("custom-wrap"), $("stack").value === "__custom__");
  });

  $("login-form").addEventListener("submit", async function (event) {
    event.preventDefault();
    setError($("login-error"), "");

    var token = $("token").value.trim();
    var choice = $("stack").value;
    var stack = choice === "__custom__" ? $("custom").value.trim() : choice;
    if (!token) { setError($("login-error"), "Enter a Storage API token."); return; }
    if (!stack) { setError($("login-error"), "Enter the stack URL."); return; }

    var btn = $("login-btn");
    btn.disabled = true;
    auth = { token: token, stack: stack, project: null, refresh: null };
    try {
      var data = await request("/api/artifacts");
      SESSION.write(auth);
      $("token").value = "";
      enterStudio(data);
    } catch (err) {
      auth = null;
      setError($("login-error"), err.message);
    } finally {
      btn.disabled = false;
    }
  });

  $("logout").addEventListener("click", leaveStudio);
  $("refresh").addEventListener("click", reload);
  $("modal-close").addEventListener("click", closeModal);
  $("modal").addEventListener("click", function (event) {
    if (event.target === $("modal")) { closeModal(); }
  });
  $("note-close").addEventListener("click", closeNote);
  $("note-modal").addEventListener("click", function (event) {
    if (event.target === $("note-modal")) { closeNote(); }
  });
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") { return; }
    if (!$("modal").hidden) { closeModal(); }
    if (!$("note-modal").hidden) { closeNote(); }
  });

  /* --------------------------------------------------------------- start */

  auth = SESSION.read();
  if (auth) {
    var options = Array.prototype.map.call(
      $("stack").options, function (option) { return option.value; }
    );
    if (options.indexOf(auth.stack) !== -1) {
      $("stack").value = auth.stack;
    } else {
      $("stack").value = "__custom__";
      $("custom").value = auth.stack;
      show($("custom-wrap"), true);
    }
    show($("loading"), true);
    request("/api/artifacts").then(function (data) {
      show($("loading"), false);
      enterStudio(data);
    }, function (err) {
      show($("loading"), false);
      if (SESSION.rejected(err)) {
        SESSION.forget();
        auth = null;
        setError($("login-error"),
          "That session is no longer valid: " + err.message);
        return;
      }
      /* The credential is kept: signing in again would not fix this. */
      setError($("login-error"),
        "The hub could not answer just now, so your artifacts are not " +
        "listed. Your sign-in is still good \u2014 try Open studio again " +
        "in a moment. (" + err.message + ")");
    });
  }
})();
"""

#: Stack choices offered by the studio's dropdown, in the order shown. Kept as
#: the short, unambiguous aliases; anything else goes through "custom URL",
#: which the server validates exactly as it validates a curl call.
_ADMIN_STACKS = ("us", "gcp-us", "eu", "azure-eu", "gcp-eu")



# --------------------------------------------------------------------------
# Live updating
# --------------------------------------------------------------------------

#: The one polling helper every live-updating surface shares.
#:
#: Design note — why polling. This service runs as a Keboola Data App behind a
#: platform proxy that buffers responses, which would silently break
#: server-sent events, and a long-lived in-process subscription would not
#: survive the app's own auto-suspend/restart cycle (CLAUDE.md, "Exactly one
#: instance, ever" — a Data App is one process, restarted rather than scaled).
#: A conditional GET against a tiny snapshot endpoint (``GET /a/{id}/live``)
#: survives both: the proxy has nothing to buffer, and a poller that catches
#: the process mid-restart just sees a fresh baseline (a new boot nonce, see
#: ``RevisionLedger.token``) instead of a broken connection.
#:
#: Exposed as ``window.AHLive.watch(options)``:
#:
#: - ``base``/``id`` — where to poll (``{base}/a/{id}/live``),
#: - ``intervalMs`` — default 10s,
#: - ``onChange(next, previous)`` — called *only* when the snapshot actually
#:   differs from the one before it. The first response is a baseline and
#:   never fires it.
#:
#: The returned handle carries ``stop()`` and ``check()`` (poll now).
#:
#: Behaviour that the surfaces below rely on: polling pauses entirely while
#: ``document.hidden`` and does one immediate check when the tab comes back;
#: network and non-2xx answers back off exponentially (10s, 20s, 40s, capped
#: at 60s) and reset on the first success; a 404 stops the watch for good (the
#: artifact was trashed, purged, or its share link rotated). Nothing here ever
#: throws into the host page, and a slow ``onChange`` cannot make it poll
#: faster than its interval — the next tick is scheduled only once the current
#: one has fully finished.
_LIVE_JS = """
window.AHLive = (function () {
  "use strict";

  var INTERVAL_MS = 10000;
  var MAX_BACKOFF_MS = 60000;
  /* An immediate check (tab refocused, caller asked) still cannot fire more
     often than this, so flipping between tabs cannot turn into a hammer. */
  var MIN_GAP_MS = 1000;

  /* The snapshot fields that mean "something changed". Kept explicit so an
     endpoint that later grows a field does not silently start firing
     onChange for it. */
  var KEYS = [
    "head_version", "updated_at", "versions_count", "proposed_count",
    "comment_threads", "document_status", "contributions_frozen"
  ];

  function snapshotOf(data) {
    var out = {};
    if (!data || typeof data !== "object") { return out; }
    for (var i = 0; i < KEYS.length; i++) { out[KEYS[i]] = data[KEYS[i]]; }
    return out;
  }

  function differs(a, b) {
    if (!a || !b) { return false; }
    for (var i = 0; i < KEYS.length; i++) {
      if (String(a[KEYS[i]]) !== String(b[KEYS[i]])) { return true; }
    }
    return false;
  }

  function watch(options) {
    var opts = options || {};
    var base = String(opts.base || "").replace(/\\/+$/, "");
    var id = String(opts.id || "");
    var onChange = typeof opts.onChange === "function" ? opts.onChange : null;
    var every = Number(opts.intervalMs) > 0 ? Number(opts.intervalMs) : INTERVAL_MS;
    var url = base + "/a/" + encodeURIComponent(id) + "/live";

    var etag = null;
    var last = null;
    var timer = null;
    var stopped = false;
    var inFlight = false;
    var failures = 0;
    var lastAt = 0;

    var handle = {
      stop: function () { stop(); },
      check: function () { check(); },
      snapshot: function () { return last; }
    };

    if (!id || typeof window.fetch !== "function") {
      /* Nothing to poll, or a browser without fetch: hand back an inert
         handle rather than letting the caller special-case it. */
      return handle;
    }

    function cancel() {
      if (timer !== null) { window.clearTimeout(timer); timer = null; }
    }

    function schedule(ms) {
      cancel();
      if (stopped) { return; }
      timer = window.setTimeout(run, ms > 0 ? ms : 0);
    }

    function backoff() {
      var steps = failures > 4 ? 4 : failures;
      var ms = every * Math.pow(2, steps);
      return ms > MAX_BACKOFF_MS ? MAX_BACKOFF_MS : ms;
    }

    /* The single place the next tick is armed: called exactly once per
       completed poll, after any onChange has returned. A slow callback
       therefore delays the next request instead of overlapping with it. */
    function done(failed) {
      inFlight = false;
      lastAt = Date.now();
      if (stopped) { return; }
      failures = failed ? failures + 1 : 0;
      schedule(failed ? backoff() : every);
    }

    function deliver(data) {
      var next = snapshotOf(data);
      var previous = last;
      last = next;
      if (!previous || !onChange || !differs(previous, next)) { return; }
      try {
        onChange(next, previous);
      } catch (err) {
        /* A broken callback must never take the watch (or the page) down. */
      }
    }

    function run() {
      timer = null;
      if (stopped || inFlight) { return; }
      /* Paused, not stopped: visibilitychange re-arms it. */
      if (document.hidden) { return; }
      inFlight = true;
      var init = { credentials: "same-origin", cache: "no-store" };
      if (etag) { init.headers = { "If-None-Match": etag }; }
      var pending;
      try {
        pending = window.fetch(url, init);
      } catch (err) {
        done(true);
        return;
      }
      pending.then(function (resp) {
        /* Gone for good: trashed, purged, or the share link was rotated. */
        if (resp.status === 404) { stop(); return null; }
        if (resp.status === 304) { done(false); return null; }
        if (!resp.ok) { done(true); return null; }
        var tag = resp.headers && resp.headers.get
          ? resp.headers.get("ETag") : null;
        return resp.json().then(function (data) {
          if (tag) { etag = tag; }
          deliver(data);
          done(false);
        }, function () { done(true); });
      }, function () { done(true); });
    }

    function check() {
      if (stopped || inFlight) { return; }
      var since = Date.now() - lastAt;
      schedule(since >= MIN_GAP_MS ? 0 : MIN_GAP_MS - since);
    }

    function onVisibility() {
      if (stopped) { return; }
      if (document.hidden) { cancel(); return; }
      check();
    }

    function stop() {
      if (stopped) { return; }
      stopped = true;
      cancel();
      try {
        document.removeEventListener("visibilitychange", onVisibility);
      } catch (err) { /* nothing to detach */ }
    }

    try {
      document.addEventListener("visibilitychange", onVisibility);
    } catch (err) { /* no visibility API: it simply polls on */ }

    /* One immediate request to establish the baseline and the ETag. */
    schedule(0);
    return handle;
  }

  return { watch: watch };
})();
"""

#: Injected into an artifact's own document, inside the sandboxed ``srcdoc``
#: iframe, purely so the shell can tell whether the reader has scrolled into
#: the document before it swaps a new version in underneath them.
#:
#: The iframe has no ``allow-same-origin``, so the shell cannot read the
#: document's scroll position itself — this is the only way to learn it. The
#: script adds no capability: it posts ``{type: "ah-scroll", y}`` on load and,
#: throttled, on scroll, and reads nothing else. Every hook is wrapped, so an
#: artifact that overrides scrolling, blocks listeners or replaces ``parent``
#: simply produces no report and the shell falls back to showing its banner.
#:
#: Served inside a ``<script type="text/plain">`` element and injected as a
#: real script, so it must never contain the closing script tag sequence.
_SCROLL_REPORTER_JS = """
(function () {
  "use strict";

  var THROTTLE_MS = 200;
  var timer = null;
  var pending = 0;
  var reported = -1;

  function post(y) {
    try {
      parent.postMessage({ type: "ah-scroll", y: y }, "*");
    } catch (err) { /* detached, or no parent to talk to */ }
  }

  function documentY() {
    try {
      var root = document.documentElement || {};
      var body = document.body || {};
      var y = window.pageYOffset || root.scrollTop || body.scrollTop || 0;
      return y > 0 ? y : 0;
    } catch (err) {
      return 0;
    }
  }

  function flush() {
    timer = null;
    var y = pending > 0 ? pending : 0;
    if (y === reported) { return; }
    reported = y;
    post(y);
  }

  /* Capture phase, so a scrollable element inside the document counts too:
     a reader who has scrolled an inner pane is just as much "in the middle of
     something" as one who scrolled the page. */
  function onScroll(event) {
    var inner = 0;
    try {
      var target = event && event.target;
      if (target && target !== document && target.scrollTop) {
        inner = target.scrollTop;
      }
    } catch (err) { /* cross-document target: ignore it */ }
    var outer = documentY();
    pending = inner > outer ? inner : outer;
    if (timer !== null) { return; }
    timer = window.setTimeout(flush, THROTTLE_MS);
  }

  try {
    window.addEventListener("scroll", onScroll, true);
  } catch (err) { /* nothing to listen on */ }

  /* One report on load, so the shell knows the reader is at the top rather
     than having to guess from silence. */
  pending = documentY();
  flush();
})();
"""

#: The "a newer version is available" banner, shared by every live surface.
#: Fixed to the bottom-right, quiet until it has something to say, and in the
#: shell's own design language (mono label, single accent, the shared card
#: border) so it reads as part of the page rather than as a browser prompt.
_LIVE_CSS = """
.ahlive {
  position: fixed;
  right: 1rem;
  bottom: 1rem;
  z-index: 80;
  display: flex;
  gap: .6rem;
  align-items: center;
  padding: .55rem .7rem .55rem .85rem;
  border: 1px solid var(--line);
  border-left: 3px solid var(--accent);
  border-radius: var(--radius);
  background: var(--panel);
  box-shadow: 0 6px 22px rgba(13, 22, 34, .16);
  font-family: var(--font-mono);
  font-size: .78rem;
  color: var(--ink);
  max-width: min(26rem, calc(100vw - 2rem));
}
.ahlive[hidden] { display: none; }
.ahlive-text { line-height: 1.35; }
"""


#: The same banner for :func:`artifact_frame_page`, which is a bare shell with
#: none of :data:`_CSS`'s custom properties — the artifact owns the viewport
#: and the wrapper deliberately ships no design system into it. Self-contained
#: and light/dark aware, so the banner reads the same as everywhere else while
#: adding nothing the document could collide with.
_FRAME_LIVE_CSS = """
.ahlive{position:fixed;right:1rem;bottom:1rem;z-index:2147483000;display:flex;
gap:.6rem;align-items:center;padding:.55rem .7rem .55rem .85rem;
border:1px solid #d8e0ea;border-left:3px solid #1442e0;border-radius:10px;
background:#fff;color:#0d1622;box-shadow:0 6px 22px rgba(13,22,34,.16);
font-family:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,
monospace;font-size:.78rem;line-height:1.35;
max-width:min(26rem,calc(100vw - 2rem))}
.ahlive[hidden]{display:none}
.ahlive-btn{font:inherit;cursor:pointer;white-space:nowrap;
padding:.3rem .6rem;border-radius:6px;border:1px solid #1442e0;
background:#1442e0;color:#fff}
.ahlive-btn:hover{opacity:.88}
@media (prefers-color-scheme: dark){
.ahlive{border-color:#22303f;border-left-color:#7aa2ff;background:#111a25;
color:#e7edf5;box-shadow:0 6px 22px rgba(0,0,0,.5)}
.ahlive-btn{border-color:#7aa2ff;background:#7aa2ff;color:#0b1119}
}
"""


def _inject_before_body_end(document_html: str, script_source: str) -> str:
    """Append ``script_source`` as a real script tag, just inside ``</body>``.

    The same technique the review shell uses client-side: the snippet goes as
    late as possible so it runs after the artifact's own markup, and falls
    back to a plain append for a fragment with no ``</body>`` at all. The tag
    name is split so this file can never be mistaken for one that closes a
    script element.
    """
    snippet = "<scr" + "ipt>" + script_source + "</scr" + "ipt>"
    at = document_html.lower().rfind("</body>")
    if at < 0:
        return document_html + snippet
    return document_html[:at] + snippet + document_html[at:]


def _page(title: str, extra_css: str, body: str) -> str:
    """Wrap a body fragment in the shared HTML skeleton."""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(title)}</title>\n"
        f"{_FONT_LINKS}"
        f"<style>{_CSS}{extra_css}</style>\n"
        "</head>\n<body>\n"
        f"{body}\n"
        "</body>\n</html>\n"
    )


def _term(title: str, body: str, credential_switch: bool = False) -> str:
    """A terminal card. ``body`` is pre-escaped markup with optional spans.

    ``credential_switch`` puts a two-way control in the bar for an example
    that carries authentication headers. Every such control on a page drives
    the same root class, so switching one switches them all — the reader
    picks their credential once, not per example.
    """
    switch = _CREDENTIAL_SWITCH if credential_switch else ""
    return (
        f'<div class="term"><div class="term-bar">'
        '<span class="dot"></span><span class="dot"></span><span class="dot"></span>'
        f'<span class="term-title">{html.escape(title)}</span>{switch}</div>'
        f"<pre><code>{body}</code></pre></div>"
    )


#: The control ``_term`` puts in the bar of an example that authenticates.
#: Buttons, not a link or a checkbox: this changes what the page shows and
#: nothing else, and it must be reachable from the keyboard.
_CREDENTIAL_SWITCH = (
    '<span class="credsw" role="group" aria-label="credential">'
    '<button type="button" data-cred="tok" aria-pressed="true">token</button>'
    '<button type="button" data-cred="bearer" aria-pressed="false">sign-in</button>'
    "</span>"
)

#: The two authentication header blocks every credential-bearing example
#: shows, one of them hidden. Both are always in the markup, so the switch is
#: a CSS class rather than a rewrite — and a reader with JavaScript off still
#: sees a complete, working example.
_CRED_LINES = (
    '<span class="cred cred-tok">'
    '    -H <span class="s">"X-StorageApi-Token: $KBC_TOKEN"</span> \\\n'
    '    -H <span class="s">"X-Storage-Stack: eu"</span> \\\n'
    "</span>"
    '<span class="cred cred-bearer">'
    '    -H <span class="s">"Authorization: Bearer $KBC_TOKEN"</span> \\\n'
    '    -H <span class="s">"X-Storage-Stack: eu"</span> \\\n'
    '    -H <span class="s">"X-Storage-Project: $KBC_PROJECT"</span> \\\n'
    "</span>"
)

#: Same two blocks for an example whose auth headers are the last arguments,
#: so neither block ends in a line continuation.
_CRED_LINES_LAST = (
    '<span class="cred cred-tok">'
    '    -H <span class="s">"X-StorageApi-Token: $KBC_TOKEN"</span> \\\n'
    '    -H <span class="s">"X-Storage-Stack: eu"</span>'
    "</span>"
    '<span class="cred cred-bearer">'
    '    -H <span class="s">"Authorization: Bearer $KBC_TOKEN"</span> \\\n'
    '    -H <span class="s">"X-Storage-Stack: eu"</span> \\\n'
    '    -H <span class="s">"X-Storage-Project: $KBC_PROJECT"</span>'
    "</span>"
)

#: Wires every credential switch on a page to one root class.
_CREDENTIAL_JS = """
(function () {
  "use strict";

  /* Which credential the examples are written for. A reading preference, not
     a credential: nothing secret is involved, and it is per tab so it never
     outlives the visit. */
  var KEY = "hub_cred_style";
  var root = document.documentElement;
  var buttons = document.querySelectorAll(".credsw button");

  function apply(style) {
    var bearer = style === "bearer";
    root.classList.toggle("auth-bearer", bearer);
    Array.prototype.forEach.call(buttons, function (button) {
      var pressed = (button.getAttribute("data-cred") === "bearer") === bearer;
      button.setAttribute("aria-pressed", pressed ? "true" : "false");
    });
    try { window.sessionStorage.setItem(KEY, style); } catch (err) {}
  }

  Array.prototype.forEach.call(buttons, function (button) {
    button.addEventListener("click", function () {
      apply(button.getAttribute("data-cred"));
    });
  });

  var stored = null;
  try { stored = window.sessionStorage.getItem(KEY); } catch (err) {}
  if (stored === "bearer") { apply(stored); }
})();
"""


def _badge(text: str, kind: str = "") -> str:
    suffix = f" badge--{kind}" if kind else ""
    return f'<span class="badge{suffix}">{html.escape(text)}</span>'


def _card(heading: str, text: str) -> str:
    return (
        f'<div class="card"><h3>{html.escape(heading)}</h3>'
        f"<p>{text}</p></div>"
    )


def landing_page(
    base_url: str,
    service_version: str,
    github_url: str,
    demo_url: str | None = None,
) -> str:
    """Render the public landing page: what the hub is and how to drive it.

    ``demo_url`` points at a published showcase artifact. It is optional:
    when it is not configured the link is simply absent.
    """
    base = html.escape(base_url.rstrip("/"))
    version = html.escape(service_version)
    repo = html.escape(github_url.rstrip("/"))
    demo_link = (
        f'<a class="primary" href="{html.escape(demo_url.rstrip("/"))}">See the demo</a>'
        if demo_url
        else ""
    )

    hero_term = _term(
        "PUBLISH → PUBLIC URL",
        '<span class="p">$</span> curl -sX POST '
        f'<span class="s">"{base}/api/artifacts"</span> \\\n'
        + _CRED_LINES +
        '    -d <span class="s">\'{"markdown": "# Q3 review\\n\\nShipped."}\'</span>\n'
        '<span class="c">'
        '{"id": "aBcD3fGhIjKlMnOpQrSt", "version": 1, "head_version": 1,\n'
        f' "url": "{base}/a/aBcD3fGhIjKlMnOpQrSt"}}</span>\n'
        '<span class="p">$</span> open '
        f'<span class="k">{base}/a/aBcD3fGhIjKlMnOpQrSt</span>',
        credential_switch=True,
    )

    features = "".join(
        [
            _card(
                "publish anything",
                "HTML served as-is, Markdown rendered by the built-in template, "
                "or a git repository — public, or private with a transient "
                "access token that is used for the clone and never stored.",
            ),
            _card(
                "urls are capabilities",
                "Every artifact gets an unguessable id. There is no public "
                "listing and no index, and every read carries "
                "<code>X-Robots-Tag: noindex</code>.",
            ),
            _card(
                "optional password",
                "PBKDF2-hashed, with a browser unlock form and an "
                "<code>X-Artifact-Password</code> header for machines.",
            ),
            _card(
                "community versioning",
                "Every update is a new version. Open an artifact to "
                "contributions and other projects can submit proposals for you "
                "to review, diff and promote.",
            ),
            _card(
                "built for agents",
                f'<a href="{base}/context">/context</a> is a machine-readable '
                f'manifest, <a href="{base}/skill">/skill</a> is a SKILL.md an '
                f'agent reads to publish unassisted, <a href="{base}/agent">'
                "/agent</a> is a drop-in Claude Code subagent, and every read "
                "endpoint answers JSON or raw HTML.",
            ),
            _card(
                "moderate in the browser",
                f'<a href="{base}/admin">/admin</a> is an admin studio for '
                "owners: list your artifacts, read proposals, diff them "
                "against the head, then promote, reject, pin or delete. Your "
                "credential never leaves the tab.",
            ),
            _card(
                "sign in, or bring a token",
                f'<a href="{base}/login">/login</a> gets you a credential '
                "without finding a Storage token first — approve a short code "
                "in your Keboola tab, or one browser hop when you run the hub "
                "yourself. Scripts drive the same flow over JSON.",
            ),
            _card(
                "review and comment",
                "Every artifact has a review UI at "
                "<code>/a/{id}/review</code>: highlight a passage, leave an "
                "inline comment, reply and resolve — and export the whole "
                "trail as an Obsidian vault.",
            ),
            _card(
                "your project, your content",
                "The canonical copy of what you publish is a Storage File in "
                "your own Keboola project. The hub keeps only a serving copy, "
                "so a restart rebuilds everything from Storage.",
            ),
        ]
    )

    publish_term = _term(
        "POST /api/artifacts",
        '<span class="c"># Markdown — GFM tables, task lists, mermaid, '
        "highlighting</span>\n"
        f'<span class="p">$</span> curl -sX POST <span class="s">"{base}/api/artifacts"</span> \\\n'
        + _CRED_LINES +
        '    -H <span class="s">"Content-Type: application/json"</span> \\\n'
        '    -d <span class="s">\'{"markdown": "# Report\\n\\nBody.", '
        '"title": "Report", "accept_versions": true}\'</span>\n'
        "\n"
        '<span class="c"># A git repository (add git_token for a private one)</span>\n'
        f'<span class="p">$</span> curl -sX POST <span class="s">"{base}/api/artifacts"</span> \\\n'
        + _CRED_LINES +
        '    -d <span class="s">\'{"git_url": "https://github.com/org/repo", '
        '"git_path": "docs/report.md"}\'</span>',
        credential_switch=True,
    )

    versions_term = _term(
        "VERSIONING",
        '<span class="c"># Submit a version to someone else\'s artifact</span>\n'
        f'<span class="p">$</span> curl -sX POST <span class="s">"{base}/api/artifacts/$ID/versions"</span> \\\n'
        + _CRED_LINES +
        '    -d <span class="s">\'{"markdown": "# Report\\n\\nFixed the totals.", '
        '"note": "fix Q3 totals"}\'</span>\n'
        '<span class="c">{"version": 2, "status": "proposed"}</span>\n'
        "\n"
        '<span class="c"># Owner reviews, diffs, and promotes it</span>\n'
        f'<span class="p">$</span> open <span class="k">{base}/a/$ID/versions?format=html</span>\n'
        f'<span class="p">$</span> open <span class="k">{base}/a/$ID/diff/1..2</span>\n'
        f'<span class="p">$</span> curl -sX POST <span class="s">"{base}/api/artifacts/$ID/versions/2/promote"</span> \\\n'
        + _CRED_LINES_LAST,
        credential_switch=True,
    )

    agents_term = _term(
        "FOR AGENTS",
        '<span class="c"># Install the ready-made Claude Code subagent</span>\n'
        f'<span class="p">$</span> install -d ~/.claude/agents &amp;&amp; curl -fsSL '
        f'<span class="s">{base}/agent</span> -o <span class="k">~/.claude/agents/artifact-hub.md</span>\n'
        "\n"
        '<span class="c"># Or hand any agent the skill and the manifest</span>\n'
        f'<span class="p">$</span> curl -s <span class="s">{base}/skill</span>\n'
        f'<span class="p">$</span> curl -s <span class="s">{base}/context</span> | jq .endpoints',
    )

    # Hoisted out of the f-string below: Python 3.11 (the app runtime) does
    # not allow backslashes inside f-string expressions (PEP 701 is 3.12+).
    headers_term = _term(
        "HEADERS",
        '<span class="c"># A Storage API token names its own project</span>\n'
        '<span class="k">X-StorageApi-Token</span>: &lt;your Keboola Storage API token&gt;\n'
        '<span class="k">X-Storage-Stack</span>: us | gcp-us | eu | azure-eu | gcp-eu\n'
        "                 | https://*.keboola.com\n"
        "\n"
        '<span class="c"># A sign-in (kbc_at_) or a personal access token '
        "(kbc_pat_)</span>\n"
        '<span class="c"># is scoped to you, so it names the project too</span>\n'
        '<span class="k">Authorization</span>: Bearer kbc_at_&hellip; | kbc_pat_&hellip;\n'
        '<span class="k">X-Storage-Stack</span>: eu\n'
        '<span class="k">X-Storage-Project</span>: 1234',
    )

    login_term = _term(
        "SIGN IN FROM A TERMINAL",
        '<span class="c"># 1. Ask for a code</span>\n'
        f'<span class="p">$</span> curl -sX POST <span class="s">"{base}/login/device"</span> \\\n'
        '    -H <span class="s">"Content-Type: application/json"</span> '
        '-d <span class="s">\'{"stack": "eu"}\'</span>\n'
        '<span class="c">{"user_code": "ABCD-EFGH", "interval": 5,\n'
        ' "verification_uri_complete": "https://connection&hellip;/admin/auth/'
        'device?userCode=ABCD-EFGH"}</span>\n'
        "\n"
        '<span class="c"># 2. Approve that URL in a browser, poll until it stops '
        "pending</span>\n"
        f'<span class="p">$</span> curl -sX POST <span class="s">"{base}/login/device/token"</span> \\\n'
        '    -H <span class="s">"Content-Type: application/json"</span> '
        '-d <span class="s">\'{"stack": "eu", "device_code": "&hellip;"}\'</span>\n'
        '<span class="c">{"status": "ok", "credential": {"access_token": '
        '"kbc_at_&hellip;"},\n'
        ' "projects": [{"id": 1234, "name": "Analytics", "role": "admin"}]}</span>',
    )

    return _page(
        "KBC Artifact Hub",
        "",
        f"""<main>
<section class="hero">
<h1>KBC Artifact Hub</h1>
<p class="lead">Turn a document into a public URL with one curl call. Sign in
with your Keboola account, or bring any Storage API token from any stack —
that is the only credential you need.</p>
<div class="hero-meta">
{_badge(f"kbc-artifact-hub v{service_version}", "version")}
{_badge("no sign-up")}
{_badge("no build step")}
</div>
{hero_term}
<div class="hero-links">
{demo_link}
<a class="primary" href="{base}/login">Sign in</a>
<a class="primary" href="{base}/admin">Admin studio</a>
<a href="{repo}">GitHub repo</a>
<a href="{base}/docs">/docs</a>
<a href="{base}/skill">/skill</a>
<a href="{base}/agent">/agent</a>
<a href="{base}/context">/context</a>
<a href="{base}/changelog">Changelog</a>
</div>
</section>

<h2 class="label">what it does</h2>
<div class="grid">{features}</div>

<h2 class="label">authentication</h2>
<p>Everything under <code>/api/artifacts</code> is authenticated with headers,
not a session. Two credentials are accepted: a <strong>Storage API token</strong>,
or a <strong>Keboola sign-in</strong> (a <code>kbc_at_</code> session or a
<code>kbc_pat_</code> personal access token). A sign-in belongs to a person
rather than to one project, so it says which project it is acting as. The hub
verifies either against your own stack's
<code>/v2/storage/tokens/verify</code> and never stores it.</p>
{headers_term}
<p class="note">Each kind goes in the header that kind belongs in — the same
split a Keboola stack uses. A <code>kbc_at_</code>/<code>kbc_pat_</code> value
in <code>X-StorageApi-Token</code> is a 400 naming the right header, and so is
sending both at once. The examples below switch between the two.
<a href="{base}/health/headers">/health/headers</a> reports the header names
that actually reached the app.</p>
<p>Ownership is the pair (stack, project id) either way. Updating, deleting,
promoting and pinning all require a credential for the project that published
the artifact.</p>

<h2 class="label">no token? sign in</h2>
<p><a href="{base}/login">/login</a> gets you a credential without hunting one
down: pick your stack, approve the sign-in in your Keboola tab, pick the
project you are publishing as. It lands in the same browser tab the
<a href="{base}/admin">admin studio</a> reads, so a pasted token and a sign-in
are interchangeable everywhere.</p>
<p>Scripts and agents use the device flow, which needs no callback URL and
works from anywhere — including a browser on a different device.</p>
{login_term}
<p class="note">A second flow, authorization code + PKCE, is one browser hop
with nothing to type, but Keboola only accepts an <code>http://127.0.0.1</code>
callback for it — so <a href="{base}/login">/login</a> offers it exactly when
you run this hub yourself. Access tokens last an hour;
<code>/login/refresh</code> renews one and <code>/login/signout</code> revokes
the session on the stack. The hub relays the sign-in and keeps nothing.</p>

<h2 class="label">quick start</h2>
{publish_term}
<p class="note">Every example that authenticates has a
<strong>token / sign-in</strong> switch in its title bar. Flipping one flips
them all, so the whole page reads for whichever credential you actually
hold.</p>
<p class="note">Add <code>"password": "secret"</code> to protect the artifact,
or <code>"accept_versions": true</code> to let other projects submit versions
for your review.</p>

<h2 class="label">community versioning</h2>
<p>Every update is a new version, and nothing is overwritten. Owners publish
live versions; other projects submit <strong>proposals</strong> that stay
private to their author and the owner until the owner promotes one. The head
pointer decides what <code>/a/{{id}}</code> serves — the newest live version,
or one you pin.</p>
{versions_term}

<h2 class="label">for agents</h2>
<p><a href="{base}/agent">/agent</a> serves a ready-to-install Claude Code
subagent definition; drop it in <code>~/.claude/agents/</code> and the agent
knows how to publish, update and moderate artifacts on its own.
<a href="{base}/skill">/skill</a> is the same knowledge as a SKILL.md for any
other agent runtime, and <a href="{base}/context">/context</a> is the
machine-readable manifest of endpoints, limits and the auth model.</p>
{agents_term}

<h2 class="label">moderating in the browser</h2>
<p>Prefer clicking to curling? <a href="{base}/admin">/admin</a> is a
single-page admin studio for artifact owners: <a href="{base}/login">sign in
with Keboola</a> or paste a Storage token, see every artifact your project owns
with its pending proposals, read a proposal, diff it against the head, then
promote, reject, pin or delete. The credential stays in that browser tab — the
studio is a static page that calls the same public API.</p>

<h2 class="label">reading an artifact</h2>
<div class="table-wrap"><table>
<tr><th>Method</th><th>Path</th><th>Returns</th></tr>
<tr><td class="mono">GET</td><td><code>/a/{{id}}</code></td><td>The head version, rendered (or the unlock form)</td></tr>
<tr><td class="mono">GET</td><td><code>/a/{{id}}/v/{{n}}</code></td><td>One specific version</td></tr>
<tr><td class="mono">GET</td><td><code>/a/{{id}}/versions</code></td><td>Version history as JSON, or <code>?format=html</code> for a picker page</td></tr>
<tr><td class="mono">GET</td><td><code>/a/{{id}}/diff/{{a}}..{{b}}</code></td><td>Side-by-side diff; <code>?format=unified</code> or <code>json</code> for machines</td></tr>
<tr><td class="mono">GET</td><td><code>/a/{{id}}/raw</code></td><td>The HTML itself, no chrome</td></tr>
<tr><td class="mono">GET</td><td><code>/a/{{id}}/source</code></td><td>Original submitted source (Markdown or HTML)</td></tr>
<tr><td class="mono">GET</td><td><code>/a/{{id}}/meta</code></td><td>Public metadata JSON, no owner details</td></tr>
<tr><td class="mono">GET</td><td><code>/a/{{id}}/review</code></td><td>Two-pane review UI: the document plus its inline comment threads</td></tr>
<tr><td class="mono">GET</td><td><code>/a/{{id}}/comments</code></td><td>Every comment thread as JSON</td></tr>
<tr><td class="mono">GET</td><td><code>/a/{{id}}/export/markdown</code></td><td>The head version's source as a downloadable file</td></tr>
<tr><td class="mono">GET</td><td><code>/a/{{id}}/export/vault</code></td><td>A ready-to-open Obsidian vault (ZIP) of the whole history</td></tr>
<tr><td class="mono">POST</td><td><code>/a/{{id}}/unlock</code></td><td>Password form target; sets a signed, path-scoped cookie</td></tr>
</table></div>
<p class="note">Protected artifacts accept the password over the
<code>X-Artifact-Password</code> header on every read;
<code>/meta</code> stays public either way. Proposed versions are visible only
to the artifact owner and the version's author, who authenticate with the same
headers as any other management call.</p>

<h2 class="label">managing your artifacts</h2>
<div class="table-wrap"><table>
<tr><th>Method</th><th>Path</th><th>Purpose</th></tr>
<tr><td class="mono">POST</td><td><code>/api/artifacts</code></td><td>Publish a new artifact</td></tr>
<tr><td class="mono">PUT</td><td><code>/api/artifacts/{{id}}</code></td><td>Add a live version, or change password / <code>accept_versions</code></td></tr>
<tr><td class="mono">GET</td><td><code>/api/artifacts</code></td><td>List your project's artifacts</td></tr>
<tr><td class="mono">DELETE</td><td><code>/api/artifacts/{{id}}</code></td><td>Delete every version and the meta record</td></tr>
<tr><td class="mono">POST</td><td><code>/api/artifacts/{{id}}/versions</code></td><td>Submit a version (live for the owner, proposed for everyone else)</td></tr>
<tr><td class="mono">POST</td><td><code>/api/artifacts/{{id}}/versions/{{n}}/promote</code></td><td>Owner approves a proposal</td></tr>
<tr><td class="mono">DELETE</td><td><code>/api/artifacts/{{id}}/versions/{{n}}</code></td><td>Owner removes a version; a contributor withdraws their own proposal</td></tr>
<tr><td class="mono">PUT</td><td><code>/api/artifacts/{{id}}/head</code></td><td>Serve the latest live version, or pin one</td></tr>
<tr><td class="mono">POST</td><td><code>/api/artifacts/{{id}}/comments</code></td><td>Open an inline comment thread on a quoted passage</td></tr>
<tr><td class="mono">POST</td><td><code>/api/artifacts/{{id}}/comments/{{tid}}/replies</code></td><td>Reply in a thread</td></tr>
<tr><td class="mono">POST</td><td><code>/api/artifacts/{{id}}/comments/{{tid}}/resolve</code></td><td>Resolve a thread, or reopen it with <code>{{"resolved": false}}</code></td></tr>
<tr><td class="mono">DELETE</td><td><code>/api/artifacts/{{id}}/comments/{{tid}}</code></td><td>Delete a thread (owner, or its author)</td></tr>
</table></div>

<footer>
<span>kbc-artifact-hub v{version}</span>
<span class="spacer"></span>
<a href="{base}/admin">Admin studio</a>
<a href="{base}/agent">/agent</a>
<a href="{base}/changelog">Changelog</a>
<a href="{repo}">github.com/padak/kbc_ai_artifact</a>
<a href="{base}/health">/health</a>
</footer>
</main>
<script>{_CREDENTIAL_JS}</script>""",
    )


def admin_page(base_url: str, service_version: str, github_url: str) -> str:
    """Render the owner/moderation studio served at ``/admin``.

    The page is public and the server learns nothing from serving it: the
    visitor's Storage token is entered in the browser, kept in a JS closure and
    in ``sessionStorage`` (per-tab, cleared when the tab closes), and attached
    to the very same API calls a terminal would make with ``curl``. Nothing is
    proxied, stored or logged on this side — ``/admin`` is a static document.
    """
    base = html.escape(base_url.rstrip("/"))
    version = html.escape(service_version)
    repo = html.escape(github_url.rstrip("/"))

    options = "".join(
        f'<option value="{html.escape(alias)}">{html.escape(alias)}</option>'
        for alias in _ADMIN_STACKS
    )
    options += '<option value="__custom__">custom URL…</option>'

    body = f"""<main>
<header class="ahead">
<div>
<h1>Artifact Hub · Admin studio</h1>
<p class="lead">Review, diff, promote and prune the versions of the artifacts
your Keboola project owns — then watch their traffic, rotate a link that went
to the wrong person, invite a guest who has no Keboola account, wire up
webhooks, and trash or purge what is done. In the browser, over the same API
you can drive with curl.</p>
</div>
<div class="ahead-right">
<span class="badge badge--version" id="project-badge" hidden></span>
<button type="button" class="btn" id="logout" hidden>Log out</button>
</div>
</header>

<div class="loading" id="loading" hidden>loading…</div>

<section id="login">
<h2 class="label">sign in</h2>
<div class="card login-card">
<a class="btn btn-primary btn-wide" href="{base}/login">Sign in with Keboola</a>
<p class="hint">No token to find: approve the sign-in in your Keboola tab and
pick the project you are publishing as.</p>
<p class="login-or">or</p>
<form id="login-form" autocomplete="off">
<label for="token">Storage API token</label>
<input type="password" id="token" name="token" autocomplete="off"
  spellcheck="false" placeholder="your Keboola Storage API token">
<label for="stack">Stack</label>
<select id="stack" name="stack">{options}</select>
<div id="custom-wrap" hidden>
<label for="custom">Stack URL</label>
<input type="text" id="custom" name="custom" spellcheck="false"
  placeholder="https://connection.keboola.com">
</div>
<button type="submit" class="btn btn-primary btn-wide" id="login-btn">Open studio</button>
<p class="err" id="login-error" hidden></p>
</form>
<p class="hint">Either way the credential stays in this browser tab. Every
call goes straight to the same API you can use with curl.</p>
<p class="hint">It is held in <code>sessionStorage</code> only — never in a
cookie, never in the URL, never in any storage that outlives this tab — so a
reload keeps you signed in and closing the tab forgets the token.
<strong>Log out</strong> clears it right away.</p>
</div>
</section>

<section id="studio" hidden>
<h2 class="label">artifacts owned by this project</h2>
<div class="toolbar">
<button type="button" class="btn" id="refresh">Refresh</button>
<span class="spacer"></span>
<span class="mono" id="count"></span>
</div>
<p class="err" id="list-error" hidden></p>
<ul class="alist" id="artifacts"></ul>
<p class="empty" id="empty" hidden>No artifacts yet. Publish one with
<code>POST /api/artifacts</code> and it shows up here.</p>
</section>

<div class="modal" id="modal" hidden>
<div class="modal-box">
<div class="modal-bar">
<span class="modal-title mono" id="modal-title"></span>
<span class="spacer"></span>
<button type="button" class="btn" id="modal-close">Close</button>
</div>
<iframe id="modal-frame" title="artifact preview"
  sandbox="allow-scripts allow-popups"></iframe>
</div>
</div>

<div class="modal" id="note-modal" hidden>
<div class="modal-box" style="height:auto;max-width:38rem">
<div class="modal-bar">
<span class="modal-title mono" id="note-title"></span>
<span class="spacer"></span>
<button type="button" class="btn" id="note-close">Close</button>
</div>
<div class="apanel" id="note-body"></div>
</div>
</div>

<footer>
<span>kbc-artifact-hub v{version}</span>
<span class="spacer"></span>
<a href="{base}/">hub home</a>
<a href="{base}/docs">/docs</a>
<a href="{repo}">source</a>
</footer>
</main>
<script>window.HUB_BASE = "{base}";</script>
<script>"""

    return _page(
        "Artifact Hub · Admin studio",
        _CONTROLS_CSS + _ADMIN_CSS + _LIVE_CSS,
        body
        + _SESSION_JS
        + "</script>\n<script>"
        + _LIVE_JS
        + "</script>\n<script>"
        + _ADMIN_JS
        + "</script>",
    )


#: The wrapper shell's own script: watches ``/a/{id}/live`` and, when the head
#: version moves, either swaps the document in place or raises the banner.
#:
#: The rule is "never yank content out from under someone". The iframe has no
#: ``allow-same-origin``, so the shell cannot read the reader's scroll
#: position — :data:`_SCROLL_REPORTER_JS`, injected into the document, posts it
#: instead. The swap happens automatically only while the last reported offset
#: is 0 (or no report has arrived yet and the page has only just loaded);
#: anything else raises the banner and waits for a click. On ``/a/{id}/v/{n}``
#: the reader asked for one specific version, so there is no automatic swap at
#: all: the banner points at the new head and says so.
_FRAME_JS = """
(function () {
  "use strict";

  var BASE = String(window.AH_BASE || "").replace(/\\/+$/, "");
  var ID = String(window.AH_ID || "");
  /* A number on /a/{id}/v/{n}, null on the head page. Its presence is what
     turns the automatic swap off. */
  var PINNED = window.AH_PINNED;
  var GRACE_MS = 4000;

  var frame = document.getElementById("ah-frame");
  var banner = document.getElementById("ah-live");
  var label = document.getElementById("ah-live-text");
  var button = document.getElementById("ah-live-go");
  var reporter = document.getElementById("ah-reporter");
  if (!frame || !banner || !label || !button || !ID) { return; }

  /* null until the document reports; 0 means "at the top". */
  var scrollY = null;
  var loadedAt = Date.now();
  var head = null;
  var applying = false;

  function inject(text) {
    var source = reporter ? reporter.textContent : "";
    if (!source) { return text; }
    var snippet = "<scr" + "ipt>" + source + "</scr" + "ipt>";
    var at = String(text).toLowerCase().lastIndexOf("</body>");
    if (at < 0) { return text + snippet; }
    return text.slice(0, at) + snippet + text.slice(at);
  }

  function undisturbed() {
    if (scrollY === null) { return (Date.now() - loadedAt) < GRACE_MS; }
    return scrollY === 0;
  }

  function showBanner(message, action) {
    label.textContent = message;
    button.textContent = action;
    banner.hidden = false;
  }

  function hideBanner() { banner.hidden = true; }

  function swap() {
    if (applying) { return; }
    applying = true;
    window.fetch(BASE + "/a/" + encodeURIComponent(ID) + "/raw", {
      credentials: "same-origin",
      cache: "no-store"
    }).then(function (resp) {
      if (!resp.ok) { throw new Error("HTTP " + resp.status); }
      return resp.text();
    }).then(function (text) {
      frame.srcdoc = inject(text);
      scrollY = null;
      loadedAt = Date.now();
      hideBanner();
      applying = false;
    }, function () {
      /* The fetch failed (offline, or the reader lost the unlock cookie):
         leave the current document alone and keep the banner up so the reader
         can try again. */
      applying = false;
      showBanner(
        "A newer version is available \\u2014 could not load it just now.",
        "Try again"
      );
    });
  }

  button.addEventListener("click", function () {
    if (PINNED === null || PINNED === undefined) { swap(); return; }
    /* A pinned version is a different URL, not a swap. */
    window.location.href = BASE + "/a/" + encodeURIComponent(ID);
  });

  window.addEventListener("message", function (event) {
    /* Only the document frame may report; anything else is ignored. */
    if (!frame.contentWindow || event.source !== frame.contentWindow) { return; }
    var data = event.data;
    if (!data || typeof data !== "object" || data.type !== "ah-scroll") { return; }
    var y = Number(data.y);
    scrollY = isFinite(y) && y > 0 ? y : 0;
  });

  function onLive(next, previous) {
    if (String(next.head_version) === String(previous.head_version) &&
        String(next.updated_at) === String(previous.updated_at)) {
      /* Comments or proposals moved; the document itself did not. */
      return;
    }
    head = next.head_version;
    if (PINNED !== null && PINNED !== undefined) {
      if (String(head) === String(PINNED)) { hideBanner(); return; }
      showBanner(
        "v" + head + " is now the latest version of this document \\u2014 " +
        "you are reading v" + PINNED + ".",
        "Open the latest"
      );
      return;
    }
    if (undisturbed()) { swap(); return; }
    showBanner("A newer version is available.", "Show it");
  }

  window.AHLive.watch({ base: BASE, id: ID, onChange: onLive });
})();
"""


#: The visually-hidden note for machines (see :func:`agent_note`). The
#: standard accessible "sr-only" recipe: off-canvas and clipped, never
#: ``display:none`` or ``visibility:hidden``, so screen readers *and* the
#: HTML-to-text step of an AI assistant still read it while a sighted reader
#: sees exactly the page they saw before.
_AGENT_NOTE_CSS = (
    ".ah-agents{position:absolute;width:1px;height:1px;margin:-1px;padding:0;"
    "border:0;overflow:hidden;clip:rect(0 0 0 0);clip-path:inset(50%);"
    "white-space:nowrap}"
)


#: The ``rel="help"`` documents, in the order a machine should read them, each
#: with the title that tells them apart: the llms.txt entry point answers
#: "what is this link", SKILL.md works in any agent runtime, AGENT.md is a
#: Claude Code subagent definition. Shared by :func:`agent_head_links` and the
#: ``Link`` response header in ``main.artifact_headers`` so the two channels
#: cannot drift.
HELP_DOCUMENTS: tuple[tuple[str, str], ...] = (
    ("/llms.txt", "What this hub is and how to read a share link"),
    ("/skill", "SKILL.md for any agent runtime"),
    ("/agent", "Claude Code subagent definition"),
)


def agent_head_links(base: str, share_id: str) -> str:
    """``<link rel>`` relations that tell a machine where the real things are.

    Standard relation names, so a client that already understands them needs
    no hub-specific knowledge: ``alternate`` for the same document in another
    representation (the raw HTML, the Markdown export), ``service-desc`` for
    the API manifest, ``help`` for the documents that explain how to use it.
    There are three of those and they differ by audience, so each carries a
    ``title`` (see :data:`HELP_DOCUMENTS`) -- without one, three ``help``
    links would be indistinguishable to a machine.
    """
    base = base.rstrip("/")
    art = f"{base}/a/{html.escape(share_id, quote=True)}"
    helps = "".join(
        f'<link rel="help" href="{base}{route}" title="{title}">\n'
        for route, title in HELP_DOCUMENTS
    )
    return (
        f'<link rel="alternate" type="text/html" href="{art}/raw">\n'
        f'<link rel="alternate" type="text/markdown" href="{art}/export/markdown">\n'
        f'<link rel="service-desc" href="{base}/context">\n'
        + helps
    )


def agent_note(base: str, share_id: str) -> str:
    """A visually hidden ``<nav>`` telling an AI assistant where to look next.

    Why it exists: a reader forwards a share link to their assistant, the
    assistant fetches ``/a/{id}`` and its HTML-to-text step drops the
    ``srcdoc`` attribute the document lives in. Left with a bare title, it
    cannot tell this is an Artifact Hub, where the readable document is, or
    where the API is described. This block is the answer, in plain sentences
    with absolute URLs, because a text extractor keeps body text and drops
    ``<link>`` relations, comments and response headers.

    It is the hub's own text about the hub's own routes -- never anything
    from the artifact -- and it lives in the wrapper, so ``/a/{id}/raw`` stays
    the publisher's bytes. Hidden with :data:`_AGENT_NOTE_CSS`, the accessible
    way, so screen readers get the same orientation.
    """
    base = base.rstrip("/")
    safe_base = html.escape(base, quote=True)
    art = f"{safe_base}/a/{html.escape(share_id, quote=True)}"
    return (
        '<nav class="ah-agents" aria-label="For AI agents">\n'
        "<p>This page is an artifact served by KBC Artifact Hub, a Keboola "
        "Data App. The document you were sent is embedded above; the "
        f'unwrapped HTML is at <a href="{art}/raw">{art}/raw</a> and a '
        f'Markdown rendering at <a href="{art}/export/markdown">'
        f"{art}/export/markdown</a>. Version history: "
        f'<a href="{art}/versions">{art}/versions</a>. The share id in the '
        "URL is the only credential a reader needs; a password-protected "
        "artifact additionally takes the password in the X-Artifact-Password "
        "request header.</p>\n"
        "<p>To understand or operate this hub as an agent, read "
        f'<a href="{safe_base}/llms.txt">{safe_base}/llms.txt</a> first, then '
        f'<a href="{safe_base}/context">{safe_base}/context</a> (machine-readable '
        f'manifest of endpoints, auth and limits), <a href="{safe_base}/docs">'
        f"{safe_base}/docs</a> and <a href=\"{safe_base}/openapi.json\">"
        f"{safe_base}/openapi.json</a> (the REST API), "
        f'<a href="{safe_base}/skill">{safe_base}/skill</a> (a SKILL.md teaching '
        f'an agent to publish and contribute) and <a href="{safe_base}/agent">'
        f"{safe_base}/agent</a> (a Claude Code subagent definition).</p>\n"
        "</nav>\n"
    )


def artifact_frame_page(
    title: str,
    artifact_html: str,
    *,
    base_url: str = "",
    share_id: str = "",
    pinned_version: int | None = None,
    hub_version: str = "",
) -> str:
    """Wrap one artifact's built HTML in a zero-chrome sandboxed iframe.

    Published artifacts are publisher-controlled documents that may run
    arbitrary JavaScript. Serving them directly on the hub's own origin put
    that script in the same origin as ``/admin`` and ``/a/{id}/review``, whose
    ``sessionStorage`` holds a visitor's Keboola Storage token — one artifact
    could read another visitor's credential. Here the document is handed to
    the browser as the ``srcdoc`` of an iframe sandboxed *without*
    ``allow-same-origin``, so it runs in an opaque origin: no access to this
    origin's storage, cookies or DOM, while scripts, forms, popups and
    downloads inside the document keep working.

    ``srcdoc`` carries the whole document as an attribute *value*, so it is
    escaped with ``quote=True`` — an unescaped ``"`` in the artifact would
    otherwise close the attribute and put artifact markup back at top level.

    **Live updating.** Given ``share_id``, the shell also polls
    ``GET /a/{id}/live`` through :data:`_LIVE_JS` and reacts when the head
    version moves: it re-fetches ``/a/{id}/raw`` and replaces the ``srcdoc``
    when the reader is demonstrably not in the middle of something, and
    otherwise raises a discreet banner and waits for a click. "Not in the
    middle of something" cannot be observed from here — the frame is
    cross-origin by construction — so :data:`_SCROLL_REPORTER_JS` is injected
    into the document and posts ``{type: "ah-scroll", y}`` back. It adds no
    capability to the sandbox and reports nothing but an offset; an artifact
    that suppresses it simply gets the banner instead of the swap.

    ``pinned_version`` marks the ``/a/{id}/v/{n}`` route, where the reader
    asked for one specific version: there the document is never swapped, and
    the banner points at the new head instead.

    Visually this is a no-op until something actually changes: the frame has
    no border and fills the viewport, so a reader sees exactly what they saw
    before. Machines that want the bytes themselves keep using ``/a/{id}/raw``.
    """
    safe_title = html.escape(title or "Artifact", quote=True)
    document_html = artifact_html
    live = ""
    # Orientation for machines (see :func:`agent_note`): only when the page
    # knows its own address, because relative URLs would be useless to an
    # assistant that reads the extracted text rather than the page.
    note = agent_note(base_url, share_id) if base_url and share_id else ""
    head_links = (
        agent_head_links(base_url, share_id) if base_url and share_id else ""
    )
    generator = (
        '<meta name="generator" content="kbc-artifact-hub '
        f'{html.escape(hub_version, quote=True)}">\n'
        if hub_version
        else ""
    )
    if share_id:
        # The reporter goes into the document served right now; the same
        # source is kept in a text/plain block so the shell can re-inject it
        # into whatever it swaps in later.
        document_html = _inject_before_body_end(
            artifact_html, _SCROLL_REPORTER_JS
        )
        pinned = "null" if pinned_version is None else str(int(pinned_version))
        live = (
            '<div class="ahlive" id="ah-live" hidden>\n'
            '<span class="ahlive-text" id="ah-live-text">'
            "A newer version is available.</span>\n"
            '<button type="button" class="ahlive-btn" id="ah-live-go">'
            "Show it</button>\n"
            "</div>\n"
            '<script type="text/plain" id="ah-reporter">'
            + _SCROLL_REPORTER_JS
            + "</script>\n"
            "<script>"
            f'window.AH_BASE = "{html.escape(base_url.rstrip("/"), quote=True)}";'
            f' window.AH_ID = "{html.escape(share_id, quote=True)}";'
            f" window.AH_PINNED = {pinned};"
            "</script>\n"
            "<script>" + _LIVE_JS + "</script>\n"
            "<script>" + _FRAME_JS + "</script>\n"
        )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{safe_title}</title>\n"
        f"{generator}{head_links}"
        "<style>html,body{margin:0;padding:0;border:0;width:100%;height:100%;"
        "overflow:hidden}"
        "iframe{margin:0;padding:0;border:0;width:100%;height:100vh;"
        "display:block}"
        + _FRAME_LIVE_CSS
        + _AGENT_NOTE_CSS
        + "</style>\n"
        "</head>\n<body>\n"
        f'<iframe id="ah-frame" title="{safe_title}" '
        'sandbox="allow-scripts allow-popups allow-forms allow-downloads" '
        f'srcdoc="{html.escape(document_html, quote=True)}"></iframe>\n'
        f"{note}{live}"
        "</body>\n</html>\n"
    )


def unlock_page(
    artifact_id: str, error: str | None, *, base_url: str = ""
) -> str:
    """Render the password form for a protected artifact.

    This is the first thing an assistant meets when it is handed a link to
    a protected artifact, so given ``base_url`` it carries the same hidden
    :func:`agent_note` as the artifact page -- including the header the
    password goes in.
    """
    safe_id = html.escape(artifact_id)
    error_html = f'<p class="error">&gt; {html.escape(error)}</p>' if error else ""
    note = agent_note(base_url, artifact_id) if base_url else ""
    return _page(
        "Password required",
        _UNLOCK_CSS + _AGENT_NOTE_CSS,
        f"""<div class="gate">
<div class="rule">locked artifact</div>
<div class="card">
<h1>Password required</h1>
<p>This artifact is protected. Enter its password to continue.</p>
<form method="post" action="/a/{safe_id}/unlock">
<label for="password">Password</label>
<input type="password" id="password" name="password"
  autocomplete="current-password" autofocus required>
<button type="submit">Unlock</button>
{error_html}
</form>
<p class="hint">Machines send the password in the
<code>X-Artifact-Password</code> header instead.</p>
</div>
</div>
{note}""",
    )


def _version_row(
    base: str,
    artifact_id: str,
    version_meta: dict,
    older: int | None,
) -> str:
    """One row of the version picker; ``older`` is the adjacent older version."""
    number = version_meta.get("version")
    status = str(version_meta.get("status") or "live")
    is_head = bool(version_meta.get("is_head"))
    title = str(version_meta.get("title") or "untitled")
    note = version_meta.get("note")
    created = str(version_meta.get("created_at") or "")
    size = version_meta.get("size_bytes")
    author = version_meta.get("author") or {}
    project = author.get("project_name") or author.get("project_id") or "unknown"

    badges = _badge(status, "proposed" if status == "proposed" else "live")
    if is_head:
        badges += _badge("head", "head")

    meta_bits = [f"by {html.escape(str(project))}"]
    if created:
        meta_bits.append(html.escape(created))
    if isinstance(size, int):
        meta_bits.append(f"{size:,} bytes".replace(",", " "))
    source_type = version_meta.get("source_type")
    if source_type:
        meta_bits.append(html.escape(str(source_type)))

    note_html = (
        f'<p class="vrow-note">{html.escape(str(note))}</p>' if note else ""
    )

    links = [f'<a href="{base}/a/{artifact_id}/v/{number}">view v{number}</a>']
    if older is not None:
        links.append(
            f'<a href="{base}/a/{artifact_id}/diff/{older}..{number}">'
            f"diff v{older}..v{number}</a>"
        )

    return (
        f'<li class="vrow{" is-head" if is_head else ""}">'
        f'<div class="vrow-top"><span class="vrow-n">v{number}</span>'
        f'<span class="vrow-title">{html.escape(title)}</span>{badges}</div>'
        f'<div class="vrow-meta">{"".join(f"<span>{bit}</span>" for bit in meta_bits)}</div>'
        f"{note_html}"
        f'<div class="vrow-links">{"".join(links)}</div>'
        "</li>"
    )


def versions_page(
    base_url: str,
    artifact_id: str,
    versions: list[dict],
    head_version: int | None,
    accept_versions: bool,
    protected: bool,
) -> str:
    """Render the human-facing version history for one artifact.

    ``versions`` is the store's public metadata list, newest first, as returned
    by :meth:`~src.store.ArtifactStore.list_versions`.
    """
    base = html.escape(base_url.rstrip("/"))
    safe_id = html.escape(artifact_id)

    rows = "".join(
        _version_row(
            base,
            safe_id,
            version_meta,
            versions[index + 1].get("version") if index + 1 < len(versions) else None,
        )
        for index, version_meta in enumerate(versions)
    )
    if not rows:
        rows = '<li class="vrow"><span class="empty">no versions</span></li>'

    proposed = sum(1 for v in versions if v.get("status") == "proposed")
    facts = [
        _badge(f"{len(versions)} versions"),
        _badge(f"head v{head_version}" if head_version else "no live head", "head"),
    ]
    if proposed:
        facts.append(_badge(f"{proposed} proposed", "proposed"))
    facts.append(
        _badge("open to contributions" if accept_versions else "owner only")
    )
    if protected:
        facts.append(_badge("password protected"))

    submit = ""
    if accept_versions:
        submit = (
            '<h2 class="label">submit a version</h2>'
            "<p>Anyone with a Keboola Storage API token can propose a new "
            "version. Proposals stay private to you and the owner until the "
            "owner promotes one.</p>"
            + _term(
                "POST /api/artifacts/…/versions",
                f'<span class="p">$</span> curl -sX POST '
                f'<span class="s">"{base}/api/artifacts/{safe_id}/versions"</span> \\\n'
                + _CRED_LINES +
                '    -H <span class="s">"Content-Type: application/json"</span> \\\n'
                '    -d <span class="s">\'{"markdown": "# Updated\\n\\n...", '
                '"note": "what changed"}\'</span>',
                credential_switch=True,
            )
        )

    return _page(
        f"Versions — {artifact_id}",
        _VERSIONS_CSS,
        f"""<main>
<div class="vhead"><h1>Version history</h1></div>
<p class="lead"><code>{safe_id}</code></p>
<div class="hero-meta">{"".join(facts)}</div>
<div class="hero-links">
<a class="primary" href="{base}/a/{safe_id}">open head version</a>
<a href="{base}/a/{safe_id}/review">review &amp; comment</a>
<a href="{base}/a/{safe_id}/versions">JSON</a>
<a href="{base}/a/{safe_id}/meta">metadata</a>
</div>

<h2 class="label">versions</h2>
<ul class="vlist">{rows}</ul>
{submit}

<footer>
<span>kbc-artifact-hub</span>
<span class="spacer"></span>
<a href="{base}/">hub home</a>
</footer>
</main>
<script>{_CREDENTIAL_JS}</script>""",
    )


# --------------------------------------------------------------------------
# Review UI (/a/{id}/review)
# --------------------------------------------------------------------------

#: Review-only styles: the two-pane frame, the thread cards and the composer.
#: Buttons and status lines come from :data:`_CONTROLS_CSS`.
_REVIEW_CSS = """
html, body { height: 100%; }
html { background-image: none; }

.rv { display: flex; flex-direction: column; height: 100vh; }

.rv-top { display: flex; align-items: center; flex-wrap: wrap; gap: .5rem;
  padding: .5rem .85rem; border-bottom: 1px solid var(--line);
  background: var(--panel); }
.rv-brand { font-family: var(--font-mono); font-weight: 700; font-size: .88rem;
  letter-spacing: -.01em; }
.rv-top .spacer { flex: 1; }

.rv-body { flex: 1; display: flex; min-height: 0; }
.rv-doc { flex: 1; min-width: 0; background: #ffffff; position: relative; }
.rv-doc iframe { width: 100%; height: 100%; border: 0; background: #ffffff; }

.rv-side { width: 25rem; max-width: 45vw; flex: none; overflow-y: auto;
  border-left: 1px solid var(--line); background: var(--panel);
  padding: .85rem .9rem 2rem; }
.rv-side h2 { font-family: var(--font-mono); font-size: .72rem;
  letter-spacing: .16em; text-transform: uppercase; color: var(--muted);
  margin: 1.1rem 0 .5rem; }
.rv-side h2:first-child { margin-top: 0; }

.rv-hint { font-size: .82rem; color: var(--muted); margin: .3rem 0 0; }
.rv-quote { font-family: var(--font-mono); font-size: .78rem; color: var(--ink-2);
  border-left: 2px solid var(--accent); padding: .25rem .55rem; margin: 0 0 .5rem;
  background: var(--accent-soft); border-radius: 0 6px 6px 0;
  overflow-wrap: anywhere; }

.rv-field { width: 100%; padding: .5rem .6rem; font-family: var(--font-sans);
  font-size: .86rem; color: var(--ink); background: var(--paper);
  border: 1px solid var(--line); border-radius: 8px; }
textarea.rv-field { min-height: 5rem; resize: vertical; }
.rv-side label { display: block; font-family: var(--font-mono); font-size: .68rem;
  letter-spacing: .12em; text-transform: uppercase; color: var(--muted);
  margin: .6rem 0 .3rem; }
.rv-actions { display: flex; flex-wrap: wrap; gap: .35rem; margin-top: .5rem; }

.rv-threads { list-style: none; margin: .3rem 0 0; padding: 0; }
.rv-thread { border: 1px solid var(--line); border-radius: var(--radius);
  background: var(--paper); padding: .6rem .7rem; margin-bottom: .5rem; }
.rv-thread.is-active { border-color: var(--accent); }
.rv-thread.is-resolved { opacity: .72; }
.rv-thread-top { display: flex; flex-wrap: wrap; align-items: center;
  gap: .35rem; margin-bottom: .35rem; }
.rv-who { font-family: var(--font-mono); font-size: .74rem; color: var(--ink); }
.rv-when { font-family: var(--font-mono); font-size: .7rem; color: var(--muted); }
.rv-text { font-size: .88rem; color: var(--ink-2); margin: .35rem 0 0;
  overflow-wrap: anywhere; white-space: pre-wrap; }
.rv-replies { list-style: none; margin: .45rem 0 0; padding: 0 0 0 .6rem;
  border-left: 2px solid var(--line); }
.rv-replies li { margin-bottom: .35rem; }
.rv-orphan { font-family: var(--font-mono); font-size: .7rem; color: var(--proposed); }

/* Unlock panel, shown when a read or a write is refused because the artifact
   is password-protected. It borrows the standalone unlock page's shape (rule
   label + card + mono label) so the two gates read as the same thing in two
   places, and it is laid *over* the document pane rather than replacing it in
   the flow: opaque, so it reads as taking the pane's place, while a document
   that had already loaded (a write can be refused while the reads still pass
   on a cookie) is still underneath when the password lands. */
.rv-lock { position: absolute; inset: 0; z-index: 2; overflow-y: auto;
  display: flex; align-items: center; justify-content: center;
  padding: 1.25rem; background: var(--paper); }
.rv-lock-card { width: 100%; max-width: 26rem; }
.rv-lock .rule { font-family: var(--font-mono); font-size: .72rem;
  letter-spacing: .16em; text-transform: uppercase; color: var(--muted);
  margin-bottom: .6rem; }
.rv-lock .rule::before { content: "//"; color: var(--accent); font-weight: 700;
  margin-right: .5rem; }
.rv-lock .card { padding: 1.35rem; }
.rv-lock h1 { font-size: 1.1rem; margin: 0 0 .4rem; }
.rv-lock p { font-size: .86rem; line-height: 1.6; color: var(--muted);
  margin: 0 0 .7rem; }
.rv-lock label { display: block; font-family: var(--font-mono); font-size: .68rem;
  letter-spacing: .12em; text-transform: uppercase; color: var(--muted);
  margin: .7rem 0 .3rem; }
"""

#: The script injected into the artifact HTML before it is loaded into the
#: sandboxed iframe.
#:
#: It runs in an **opaque origin** (the iframe declares
#: ``sandbox="allow-scripts allow-popups"`` with no ``allow-same-origin``), so
#: it can never read the shell's DOM, cookies or ``sessionStorage`` — the
#: Storage token is structurally out of reach of anything an artifact author
#: wrote. Its only channel to the shell is ``postMessage``:
#:
#: - out: ``ah-ready``, ``ah-select`` (a TextQuoteSelector for the current
#:   selection), ``ah-open`` (a highlight was clicked) and ``ah-anchored``
#:   (which thread IDs actually matched this rendering),
#: - in: ``ah-anchors`` — the quotes to highlight.
#:
#: Served to the browser inside a ``<script type="text/plain">`` element and
#: injected by the shell as a real script, so it must never contain the closing
#: script tag sequence.
_ANNOTATION_JS = """
(function () {
  "use strict";

  var MAX_EXACT = 2000;
  var CTX = 32;
  var ATTR = "data-ah-tid";

  var style = document.createElement("style");
  style.textContent =
    "mark[" + ATTR + "]{background:rgba(250,204,21,.35);color:inherit;" +
    "outline:2px solid #f59e0b;outline-offset:1px;border-radius:2px;" +
    "cursor:pointer}" +
    "mark[" + ATTR + "]:hover{background:rgba(250,204,21,.62)}";
  (document.head || document.documentElement).appendChild(style);

  function post(message) {
    try { parent.postMessage(message, "*"); } catch (err) { /* detached */ }
  }

  /* The rendered document as one string, plus the text nodes that built it.
     Recomputed on demand: wrapping a quote in <mark> keeps the text identical
     but rearranges the nodes. */
  function flatten() {
    var root = document.body || document.documentElement;
    var parts = [];
    var text = "";
    if (!root) { return { text: text, parts: parts }; }
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var node;
    while ((node = walker.nextNode())) {
      var tag = node.parentNode ? node.parentNode.nodeName : "";
      if (tag === "SCRIPT" || tag === "STYLE" || tag === "NOSCRIPT") { continue; }
      var value = node.nodeValue || "";
      if (!value) { continue; }
      parts.push({ node: node, start: text.length, end: text.length + value.length });
      text += value;
    }
    return { text: text, parts: parts };
  }

  function offsetOf(flat, container, offset) {
    for (var i = 0; i < flat.parts.length; i++) {
      if (flat.parts[i].node === container) { return flat.parts[i].start + offset; }
    }
    return -1;
  }

  /* Longest common run of two strings, from the start or from the end. */
  function overlap(a, b, fromEnd) {
    var limit = Math.min(a.length, b.length);
    var i = 0;
    while (i < limit) {
      var ca = fromEnd ? a.charAt(a.length - 1 - i) : a.charAt(i);
      var cb = fromEnd ? b.charAt(b.length - 1 - i) : b.charAt(i);
      if (ca !== cb) { break; }
      i += 1;
    }
    return i;
  }

  /* Offset of the best occurrence of spec.exact, or -1. Ties between repeated
     quotes are broken by how much of the recorded prefix/suffix matches.

     This is the preferred path: a quote captured from a real browser selection
     is a slice of the very string searched here, so it always lands exactly. */
  function locateExact(text, spec) {
    var exact = String(spec.exact || "");
    if (!exact) { return -1; }
    var best = -1;
    var bestScore = -1;
    var from = 0;
    for (;;) {
      var at = text.indexOf(exact, from);
      if (at < 0) { break; }
      var before = text.slice(Math.max(0, at - CTX), at);
      var after = text.slice(at + exact.length, at + exact.length + CTX);
      var score = overlap(before, String(spec.prefix || ""), true) +
        overlap(after, String(spec.suffix || ""), false);
      if (score > bestScore) { bestScore = score; best = at; }
      from = at + 1;
    }
    return best;
  }

  /* Every run of whitespace — spaces, tabs, newlines and the non-breaking
     space \\u00a0 — counts as one ASCII space. The flattened document keeps the
     source HTML's own line breaks and indentation inside a paragraph, so a
     quote written by an agent through the API ("... 11% of signups") never
     matches the raw text ("... 11%\\n      of signups") without this. */
  var WS = /[\\s\\u00a0]/;

  function collapseText(value) {
    return String(value === undefined || value === null ? "" : value)
      .replace(/[\\s\\u00a0]+/g, " ");
  }

  /* The whitespace-collapsed projection of `text`, plus the index map back to
     raw offsets: normalized character i came from the raw span
     [starts[i], ends[i]). A normalized match [i, j) is therefore the raw span
     [starts[i], ends[j - 1]) — which is what rangeFor() needs, and which is
     generally *not* i + needle.length characters long. */
  function collapseFlat(text) {
    var out = "";
    var starts = [];
    var ends = [];
    var len = text.length;
    var i = 0;
    while (i < len) {
      var ch = text.charAt(i);
      if (WS.test(ch)) {
        var run = i;
        while (i < len && WS.test(text.charAt(i))) { i += 1; }
        out += " ";
        starts.push(run);
        ends.push(i);
        continue;
      }
      out += ch;
      starts.push(i);
      ends.push(i + 1);
      i += 1;
    }
    return { text: out, starts: starts, ends: ends };
  }

  /* Fallback for quotes whose whitespace does not match the document's:
     same prefix/suffix scoring, run over the normalized projection, with the
     winning normalized span mapped back to raw offsets. Returns null when the
     quote is empty once trimmed, when it is longer than we ever anchor, or
     when nothing matches — never a guess. */
  function locateNormalized(text, spec) {
    var needle = collapseText(spec.exact).replace(/^ +/, "").replace(/ +$/, "");
    if (!needle || needle.length > MAX_EXACT) { return null; }
    var flat = collapseFlat(text);
    var hay = flat.text;
    var prefix = collapseText(spec.prefix);
    var suffix = collapseText(spec.suffix);
    var best = -1;
    var bestScore = -1;
    var from = 0;
    for (;;) {
      var at = hay.indexOf(needle, from);
      if (at < 0) { break; }
      var before = hay.slice(Math.max(0, at - CTX), at);
      var after = hay.slice(at + needle.length, at + needle.length + CTX);
      var score = overlap(before, prefix, true) + overlap(after, suffix, false);
      if (score > bestScore) { bestScore = score; best = at; }
      from = at + 1;
    }
    if (best < 0) { return null; }
    var last = best + needle.length - 1;
    if (best >= flat.starts.length || last < 0 || last >= flat.ends.length) {
      return null;
    }
    return { start: flat.starts[best], end: flat.ends[last] };
  }

  /* The raw [start, end) span of the best occurrence of spec.exact, or null.
     An exact match always wins, at the same offset it has always had; the
     whitespace-tolerant pass only runs when the exact scan finds nothing. */
  function locate(text, spec) {
    var exact = String(spec.exact || "");
    if (!exact) { return null; }
    var at = locateExact(text, spec);
    if (at >= 0) { return { start: at, end: at + exact.length }; }
    try {
      return locateNormalized(text, spec);
    } catch (err) {
      return null;
    }
  }

  function rangeFor(flat, start, end) {
    var range = document.createRange();
    var started = false;
    for (var i = 0; i < flat.parts.length; i++) {
      var part = flat.parts[i];
      if (!started && start >= part.start && start < part.end) {
        range.setStart(part.node, start - part.start);
        started = true;
      }
      if (started && end > part.start && end <= part.end) {
        range.setEnd(part.node, end - part.start);
        return range;
      }
    }
    return null;
  }

  function clearMarks() {
    var marks = document.querySelectorAll("mark[" + ATTR + "]");
    for (var i = 0; i < marks.length; i++) {
      var mark = marks[i];
      var parent = mark.parentNode;
      if (!parent) { continue; }
      while (mark.firstChild) { parent.insertBefore(mark.firstChild, mark); }
      parent.removeChild(mark);
      if (parent.normalize) { parent.normalize(); }
    }
  }

  function wrap(range, tid) {
    var mark = document.createElement("mark");
    mark.setAttribute(ATTR, tid);
    try {
      range.surroundContents(mark);
      return true;
    } catch (err) {
      /* The quote crosses element boundaries; rebuild it instead. */
    }
    try {
      mark.appendChild(range.extractContents());
      range.insertNode(mark);
      return true;
    } catch (err2) {
      return false;
    }
  }

  /* Highlight every quote we can still find. A quote that no longer matches
     this rendering is skipped silently; the shell learns which ones anchored
     from the ah-anchored reply and labels the rest. */
  function applyAnchors(list) {
    clearMarks();
    var done = [];
    for (var i = 0; i < list.length; i++) {
      var spec = list[i];
      if (!spec || !spec.tid || !spec.exact) { continue; }
      var flat = flatten();
      var span = locate(flat.text, spec);
      if (!span || span.end <= span.start) { continue; }
      var range = rangeFor(flat, span.start, span.end);
      if (!range) { continue; }
      if (wrap(range, String(spec.tid))) { done.push(String(spec.tid)); }
    }
    post({ type: "ah-anchored", tids: done });
  }

  document.addEventListener("mouseup", function () {
    /* Let the browser finish updating the selection first. */
    window.setTimeout(function () {
      var selection = window.getSelection();
      if (!selection || selection.isCollapsed || selection.rangeCount === 0) {
        return;
      }
      var range = selection.getRangeAt(0);
      var flat = flatten();
      var start = offsetOf(flat, range.startContainer, range.startOffset);
      var end = offsetOf(flat, range.endContainer, range.endOffset);
      var exact;
      if (start >= 0 && end > start) {
        /* Slicing the flattened text guarantees the quote can be found again
           by the very same code path that anchors it. */
        exact = flat.text.slice(start, end);
      } else {
        exact = String(selection.toString());
        start = flat.text.indexOf(exact);
        end = start < 0 ? -1 : start + exact.length;
      }
      if (!exact || !exact.replace(/\\s+/g, "")) { return; }
      if (exact.length > MAX_EXACT) {
        exact = exact.slice(0, MAX_EXACT);
        end = start + exact.length;
      }
      var prefix = "";
      var suffix = "";
      if (start >= 0) {
        prefix = flat.text.slice(Math.max(0, start - CTX), start);
        suffix = flat.text.slice(end, end + CTX);
      }
      post({ type: "ah-select", exact: exact, prefix: prefix, suffix: suffix });
    }, 0);
  });

  document.addEventListener("click", function (event) {
    var node = event.target;
    while (node && node !== document) {
      if (node.nodeType === 1 && node.hasAttribute && node.hasAttribute(ATTR)) {
        post({ type: "ah-open", tid: node.getAttribute(ATTR) });
        return;
      }
      node = node.parentNode;
    }
  });

  window.addEventListener("message", function (event) {
    var data = event.data;
    if (!data || typeof data !== "object") { return; }
    if (data.type === "ah-anchors") { applyAnchors(data.anchors || []); }
  });

  post({ type: "ah-ready" });
})();
"""

#: The review shell, as one IIFE. It owns the credential; the artifact owns
#: nothing but its own opaque-origin iframe.
_REVIEW_JS = """
(function () {
  "use strict";

  var BASE = String(window.HUB_BASE || "").replace(/\\/+$/, "");
  var ID = String(window.HUB_ARTIFACT_ID || "");
  var PATH = BASE + "/a/" + encodeURIComponent(ID);

  /* Reading, renewing and revoking the credential, shared with /admin and
     /login on purpose: one sign-in serves every page. This closure keeps the
     record itself. */
  var SESSION = window.hubSession;
  var auth = null;

  /* An invited guest's credential, taken from the URL *fragment*
     (#invite={invitation_id}.{secret}). The fragment is never sent to a
     server by the browser, and this script never puts it in a URL either: it
     only ever travels in the X-Artifact-Guest request header, so it stays out
     of access logs, Referer headers and the hub's own routing. */
  var guest = null;
  /* True while the invitation could not be checked *because the document is
     locked* — /a/{id}/guest sits behind the same reader gate. The credential
     is kept and re-checked after unlocking, so a good invitation is never
     reported dead. */
  var guestPending = false;

  /* The reader password, once this tab unlocked the artifact. Memory only:
     never sessionStorage, never a URL, never logged. It exists because a
     comment *write* goes to /api/..., which the unlock cookie (path-scoped to
     /a/{id}) never reaches — those requests carry the password header
     instead. */
  var readerPassword = null;
  var locked = false;
  /* A write that was refused for the password, replayed after unlocking. */
  var pendingWrite = null;

  var threads = [];
  /* Reply drafts, keyed by thread id and held OUTSIDE the DOM. renderThreads()
     rebuilds every card from scratch, and a live refresh calls it: whatever
     somebody has typed has to survive that, so it lives here and the textarea
     is only a view of it. */
  var drafts = {};
  /* The reader's scroll offset inside the document frame, as reported by the
     injected scroll reporter. null = nothing reported yet. */
  var docScrollY = null;
  var docLoadedAt = Date.now();
  var headVersion = null;
  var commentsMode = "anyone";
  var artifactStatus = "draft";
  var anchoredIds = {};
  var selection = null;
  var activeId = null;

  function $(id) { return document.getElementById(id); }
  function show(node, on) { if (node) { node.hidden = !on; } }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }

  function badge(text, kind) {
    return el("span", "badge" + (kind ? " badge--" + kind : ""), text);
  }

  function setError(node, message) {
    node.textContent = message || "";
    node.hidden = !message;
  }

  function when(value) {
    return String(value || "").replace("T", " ").replace("+00:00", "");
  }

  function who(identity) {
    var data = identity || {};
    /* A guest is published as {kind: "guest", name}: no project, no stack. */
    if (data.kind === "guest") {
      return (data.name ? String(data.name) : "guest") + " (guest)";
    }
    if (data.project_name) { return String(data.project_name); }
    if (data.project_id !== undefined && data.project_id !== null) {
      return "project " + data.project_id;
    }
    return "unknown project";
  }

  /* ---------------------------------------------------------------- guest */

  /* Read #invite={invitation_id}.{secret} once, then clear the fragment from
     the address bar so the secret does not sit in a shared screen, a
     screenshot or the next person's history entry. The credential lives on in
     this closure for as long as the tab does. */
  function readInvite() {
    var hash = String(window.location.hash || "");
    var at = hash.indexOf("invite=");
    if (at < 0) { return null; }
    var value = hash.slice(at + 7).split("&")[0];
    if (!value || value.indexOf(".") < 1) { return null; }
    try {
      window.history.replaceState(null, "",
        window.location.pathname + window.location.search);
    } catch (err) {
      /* Non-fatal: the fragment simply stays visible. */
    }
    return { credential: decodeURIComponent(value), name: "" };
  }

  async function checkInvite() {
    if (!guest) { return; }
    try {
      /* Header only — the credential must never become part of a URL. This
         doubles as the validation step: a revoked or malformed invitation
         answers 401 here, before anybody writes a comment that would fail. */
      var data = JSON.parse(await read("/guest", true));
      guest.name = data.name || "";
      guestPending = false;
    } catch (err) {
      if (SESSION.locked(err)) {
        /* Locked document, not a dead invitation: /a/{id}/guest is behind the
           reader gate too. Hold on to the credential and ask again once the
           password lands. */
        guestPending = true;
        renderIdentity();
        return;
      }
      guest = null;
      setError($("rv-signin-error"),
        "That invitation link is not valid any more: " + err.message);
    }
    renderIdentity();
    renderThreads();
  }

  /* Exactly one of the three identity blocks is on screen. A Storage token
     wins over an invitation, because it names a verified project and the
     header code prefers it — the banner must never claim otherwise. */
  function renderIdentity() {
    var asProject = !!auth;
    var asGuest = !asProject && !!guest;
    show($("rv-account"), asProject);
    show($("rv-guest"), asGuest);
    show($("rv-signin"), !asProject && !asGuest);
    if (asGuest) {
      $("rv-guest-name").textContent =
        "Commenting as " + (guest.name || "a guest") + " (guest)";
    }
  }

  /* ---------------------------------------------------------------- auth */

  /* The renewed record has to land in this closure as well as in storage,
     which is the one thing the shared session module cannot do for us. */
  async function renewSession() {
    var next = await SESSION.renew(auth);
    if (!next) { return false; }
    auth = next;
    return true;
  }

  /* Whichever credential this visitor has. A guest never has a token and a
     signed-in project never needs the invitation, so the two are mutually
     exclusive; the token wins when somebody has both, because it identifies a
     verified project and the guest header does not. */
  function headers(withBody) {
    var out = {};
    if (auth) {
      SESSION.headers(auth, out);
    } else if (guest) {
      out["X-Artifact-Guest"] = guest.credential;
    }
    if (withBody) { out["Content-Type"] = "application/json"; }
    /* Comment writes live under /api/, which the unlock cookie is not scoped
       to, so an unlocked tab re-states the password on them the documented
       way. Absent on an unprotected artifact, where it is never obtained. */
    if (readerPassword) { out["X-Artifact-Password"] = readerPassword; }
    return out;
  }

  function canWrite() { return !!(auth || guest); }

  function apiMessage(status, data, text) {
    if (data && typeof data.detail === "string") { return data.detail; }
    if (data && data.detail) { return JSON.stringify(data.detail); }
    if (data && data.error) {
      return data.error + (data.detail ? " \\u2014 " + data.detail : "");
    }
    if (text) { return "HTTP " + status + ": " + text.slice(0, 300); }
    return "HTTP " + status;
  }

  /* Errors carry the HTTP status and parsed payload, because the reader gate
     is told apart from every other failure by exactly those two. */
  function apiError(status, data, text) {
    var err = new Error(apiMessage(status, data, text));
    err.status = status;
    err.payload = data;
    return err;
  }

  async function api(path, options) {
    var opts = options || {};
    var hasBody = opts.body !== undefined;
    var body = hasBody ? JSON.stringify(opts.body) : undefined;
    async function attempt() {
      var resp = await fetch(BASE + path, {
        method: opts.method || "GET",
        headers: headers(hasBody),
        body: body
      });
      var text = await resp.text();
      var data = null;
      try { data = text ? JSON.parse(text) : null; } catch (err) { data = null; }
      return { resp: resp, text: text, data: data };
    }
    var out = await attempt();
    /* One retry, and only on a 401 that is about the session: an aged-out
       access token is the single failure this page can put right by itself.
       The reader gate answers 401 as well \u2014 to a signed-in owner too \u2014
       so the body is read first, and renewing is skipped for that one. */
    if (out.resp.status === 401 && !SESSION.lockedBody(out.data)
        && await renewSession()) {
      out = await attempt();
    }
    if (!out.resp.ok) { throw apiError(out.resp.status, out.data, out.text); }
    return out.data;
  }

  /* Public reads. credentials:"same-origin" carries the unlock cookie of a
     password-protected artifact, which is scoped to /a/{id}. ``withGuest``
     adds the invitation header for the one read that needs it (/guest); the
     credential is never appended to the path or the query string. */
  async function read(path, withGuest) {
    var init = { credentials: "same-origin" };
    if (withGuest && guest) {
      init.headers = { "X-Artifact-Guest": guest.credential };
    }
    var resp = await fetch(PATH + path, init);
    var text = await resp.text();
    if (!resp.ok) {
      var data = null;
      try { data = JSON.parse(text); } catch (err) { data = null; }
      throw apiError(resp.status, data, text);
    }
    return text;
  }

  /* ----------------------------------------------------------------- lock */

  /* A password-protected artifact refuses /raw, /versions, /comments and the
     comment writes alike; this page has to be able to ask for that password
     itself, because a visitor who arrived on an invitation link has never
     seen the standalone unlock form and has no other way in. An invitation
     grants a voice, not a key — that stays true, the password is simply
     asked for here as well. */

  /* Confirmed against /a/{id}/meta, which is public even while the document
     is protected and reports the flag itself. Fetched once, and only after a
     read has actually been refused, so an unprotected artifact never issues
     this request at all. */
  var lockCheck = null;

  function protectedArtifact() {
    if (!lockCheck) {
      lockCheck = fetch(PATH + "/meta", { credentials: "same-origin" })
        .then(function (resp) { return resp.ok ? resp.json() : null; })
        .then(function (data) { return !!(data && data.protected); })
        .catch(function () { return false; });
    }
    return lockCheck;
  }

  function showLock() {
    locked = true;
    show($("rv-lock-guest"), !!guest);
    show($("rv-lock"), true);
    show($("rv-lock-side"), true);
    setError($("rv-error"), "");
    $("rv-head").textContent = "locked";
    var field = $("rv-lock-password");
    if (field) { field.focus(); }
  }

  /* True when this failure was the reader gate and the panel now has the
     screen; false when it was anything else and the caller should report it
     the way it always did. */
  async function lockDown(err) {
    if (!SESSION.locked(err)) { return false; }
    if (locked) { return true; }
    if (!(await protectedArtifact())) { return false; }
    showLock();
    return true;
  }

  function readFailed(err) {
    lockDown(err).then(function (handled) {
      if (!handled) { setError($("rv-error"), err.message); }
    });
  }

  function unlockMessage(status) {
    if (status === 429) {
      return "Too many attempts \\u2014 wait an hour and try again.";
    }
    return "That password was not accepted.";
  }

  async function unlock(value) {
    /* Form-encoded to the same endpoint the standalone form posts to, with
       the field name it reads. The password travels in this body and nowhere
       else. credentials:"same-origin" so the path-scoped cookie the 303 sets
       is kept and the reads below ride on it. */
    var resp = await fetch(PATH + "/unlock", {
      method: "POST",
      credentials: "same-origin",
      redirect: "manual",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: "password=" + encodeURIComponent(value)
    });
    if (resp.status === 401 || resp.status === 429) {
      throw new Error(unlockMessage(resp.status));
    }
    /* Success answers 303, whose body fetch cannot read across a document
       boundary, so the proof is a protected read that now goes through. */
    try {
      await read("/comments");
    } catch (err) {
      if (err.status === 401 || err.status === 429) {
        throw new Error(unlockMessage(err.status));
      }
      throw err;
    }
    readerPassword = value;
    locked = false;
    show($("rv-lock"), false);
    show($("rv-lock-side"), false);
    $("rv-lock-password").value = "";
    setError($("rv-lock-error"), "");
    if (guest && guestPending) { await checkInvite(); }
    await loadDocument();
    await loadVersions();
    await loadThreads();
    var retry = pendingWrite;
    pendingWrite = null;
    if (retry) { await retry(); }
  }

  /* Shared by every write path: a comment refused for the password puts the
     panel on screen and parks the write, instead of printing "password
     required" at somebody who has nowhere to type one. The composer and the
     reply box keep their text, so nothing typed is lost. */
  function writeFailed(err, errBox, message) {
    return lockDown(err).then(function (handled) {
      setError(errBox, handled ? message : err.message);
      return handled;
    });
  }

  /* ------------------------------------------------------------- document */

  /* The artifact is loaded into a srcdoc iframe sandboxed WITHOUT
     allow-same-origin, so its scripts run in an opaque origin and can reach
     neither this document nor the token in sessionStorage. */
  function inject(htmlText) {
    /* Two shell-authored scripts, in order: the annotation layer, and the
       scroll reporter that tells this page whether the reader has moved into
       the document. Neither adds a sandbox capability. */
    var snippet = "";
    ["rv-anno", "rv-scroll"].forEach(function (id) {
      var node = $(id);
      var source = node ? node.textContent : "";
      if (source) { snippet += "<scr" + "ipt>" + source + "</scr" + "ipt>"; }
    });
    var lower = String(htmlText).toLowerCase();
    var at = lower.lastIndexOf("</body>");
    if (at < 0) { return htmlText + snippet; }
    return htmlText.slice(0, at) + snippet + htmlText.slice(at);
  }

  async function loadDocument() {
    var text = await read("/raw");
    $("rv-frame").srcdoc = inject(text);
    docScrollY = null;
    docLoadedAt = Date.now();
  }

  async function loadVersions() {
    var data = JSON.parse(await read("/versions"));
    headVersion = data.head_version;
    $("rv-head").textContent = headVersion
      ? "commenting on v" + headVersion
      : "no live version";
  }

  async function loadThreads() {
    var data = JSON.parse(await read("/comments"));
    threads = data.threads || [];
    commentsMode = data.comments_mode || "anyone";
    artifactStatus = data.status || "draft";
    renderThreads();
    sendAnchors();
  }

  function sendAnchors() {
    var frame = $("rv-frame");
    if (!frame || !frame.contentWindow) { return; }
    var list = threads.map(function (thread) {
      var selector = thread.selector || {};
      return {
        tid: thread.id,
        exact: selector.exact || "",
        prefix: selector.prefix || "",
        suffix: selector.suffix || ""
      };
    });
    frame.contentWindow.postMessage({ type: "ah-anchors", anchors: list }, "*");
  }

  /* --------------------------------------------------------------- threads */

  function renderThreads() {
    var list = $("rv-threads");
    list.textContent = "";
    $("rv-count").textContent = threads.length +
      (threads.length === 1 ? " thread" : " threads");
    show($("rv-empty"), threads.length === 0);
    threads.forEach(function (thread) {
      list.appendChild(threadCard(thread));
    });
    var frozen = artifactStatus === "final";
    show($("rv-frozen"), frozen);
    show($("rv-closed"), !frozen && commentsMode === "off");
  }

  function threadCard(thread) {
    var item = el("li", "rv-thread");
    item.id = "rv-thread-" + thread.id;
    if (thread.resolved) { item.classList.add("is-resolved"); }
    if (activeId === thread.id) { item.classList.add("is-active"); }

    var top = el("div", "rv-thread-top");
    top.appendChild(el("span", "rv-who", who(thread.author)));
    top.appendChild(el("span", "rv-when", when(thread.created_at)));
    if (thread.version !== headVersion) {
      top.appendChild(badge("v" + thread.version));
    }
    if (thread.resolved) { top.appendChild(badge("resolved", "live")); }
    item.appendChild(top);

    var selector = thread.selector || {};
    item.appendChild(el("div", "rv-quote", selector.exact || ""));
    if (!anchoredIds[thread.id]) {
      item.appendChild(
        el("div", "rv-orphan", "quote not found on this version")
      );
    }
    item.appendChild(el("p", "rv-text", thread.body || ""));

    var replies = thread.replies || [];
    if (replies.length) {
      var replyList = el("ul", "rv-replies");
      replies.forEach(function (reply) {
        var row = document.createElement("li");
        var head = el("div", "rv-thread-top");
        head.appendChild(el("span", "rv-who", who(reply.author)));
        head.appendChild(el("span", "rv-when", when(reply.created_at)));
        row.appendChild(head);
        row.appendChild(el("p", "rv-text", reply.body || ""));
        replyList.appendChild(row);
      });
      item.appendChild(replyList);
    }

    /* Guests get the same buttons; the server decides what they may actually
       do with them (their own threads only) and says so in the error line. */
    if (canWrite()) { item.appendChild(threadActions(thread)); }
    item.addEventListener("click", function () {
      activeId = thread.id;
      renderThreads();
    });
    return item;
  }

  function threadActions(thread) {
    var wrap = el("div", null);
    var errBox = el("p", "err");
    errBox.hidden = true;

    var box = el("textarea", "rv-field");
    var draft = drafts[thread.id] || "";
    box.value = draft;
    /* An unfinished reply keeps the box open across re-renders; an empty one
       folds away again. */
    box.hidden = !draft;
    box.placeholder = "Reply\\u2026";
    box.addEventListener("input", function () {
      if (box.value) { drafts[thread.id] = box.value; }
      else { delete drafts[thread.id]; }
    });

    function run(button, handler) {
      button.disabled = true;
      setError(errBox, "");
      handler().then(function () {
        button.disabled = false;
      }, function (err) {
        writeFailed(err, errBox,
          "This document is password-protected \\u2014 unlock it on the left " +
          "and this will be sent."
        ).then(function (handled) {
          if (handled) { pendingWrite = handler; }
          button.disabled = false;
        });
      });
    }

    var actions = el("div", "rv-actions");
    var path = "/api/artifacts/" + encodeURIComponent(ID) + "/comments/" +
      encodeURIComponent(thread.id);

    var replyBtn = el("button", "btn btn-sm", "Reply");
    replyBtn.type = "button";
    replyBtn.addEventListener("click", function (event) {
      event.stopPropagation();
      if (box.hidden) { box.hidden = false; box.focus(); return; }
      var text = box.value.trim();
      if (!text) { box.hidden = true; delete drafts[thread.id]; return; }
      run(replyBtn, async function () {
        await api(path + "/replies", { method: "POST", body: { body: text } });
        box.value = "";
        delete drafts[thread.id];
        await loadThreads();
      });
    });
    actions.appendChild(replyBtn);

    var resolveBtn = el("button", "btn btn-sm",
      thread.resolved ? "Reopen" : "Resolve");
    resolveBtn.type = "button";
    resolveBtn.addEventListener("click", function (event) {
      event.stopPropagation();
      run(resolveBtn, async function () {
        await api(path + "/resolve", {
          method: "POST",
          body: { resolved: !thread.resolved }
        });
        await loadThreads();
      });
    });
    actions.appendChild(resolveBtn);

    var deleteBtn = el("button", "btn btn-sm btn-danger", "Delete");
    deleteBtn.type = "button";
    deleteBtn.addEventListener("click", function (event) {
      event.stopPropagation();
      if (!window.confirm("Delete this thread? This is permanent.")) { return; }
      run(deleteBtn, async function () {
        await api(path, { method: "DELETE" });
        await loadThreads();
      });
    });
    actions.appendChild(deleteBtn);

    wrap.appendChild(box);
    wrap.appendChild(actions);
    wrap.appendChild(errBox);
    return wrap;
  }

  function focusThread(tid) {
    activeId = tid;
    renderThreads();
    var node = $("rv-thread-" + tid);
    if (node && node.scrollIntoView) {
      node.scrollIntoView({ block: "center" });
    }
    show($("rv-side"), true);
  }

  /* -------------------------------------------------------------- composer */

  function openComposer(data) {
    selection = { exact: data.exact, prefix: data.prefix, suffix: data.suffix };
    $("rv-quote").textContent = data.exact;
    setError($("rv-composer-error"), "");
    show($("rv-composer"), true);
    show($("rv-side"), true);
    $("rv-comment").focus();
  }

  function closeComposer() {
    selection = null;
    $("rv-comment").value = "";
    show($("rv-composer"), false);
  }

  async function submitComment() {
    if (!selection) { return; }
    if (!canWrite()) {
      setError($("rv-composer-error"),
        "Sign in first to leave a comment \\u2014 or open this document " +
        "through an invitation link.");
      return;
    }
    var text = $("rv-comment").value.trim();
    if (!text) {
      setError($("rv-composer-error"), "Write something first.");
      return;
    }
    var button = $("rv-post");
    button.disabled = true;
    setError($("rv-composer-error"), "");
    try {
      await api("/api/artifacts/" + encodeURIComponent(ID) + "/comments", {
        method: "POST",
        body: {
          version: headVersion,
          exact: selection.exact,
          prefix: selection.prefix,
          suffix: selection.suffix,
          body: text
        }
      });
      closeComposer();
      await loadThreads();
    } catch (err) {
      /* The quote and the typed text stay in the composer, so unlocking and
         replaying posts exactly what was written. */
      if (await writeFailed(err, $("rv-composer-error"),
        "This document is password-protected \\u2014 unlock it on the left " +
        "and your comment will be posted.")) {
        pendingWrite = submitComment;
      }
    } finally {
      button.disabled = false;
    }
  }

  /* --------------------------------------------------------------- signin */

  function signedIn(projectId) {
    $("rv-project").textContent = "project " + (projectId || "?") +
      " \\u00b7 " + auth.stack;
    renderIdentity();
  }

  function signedOut() {
    SESSION.end(auth);
    auth = null;
    SESSION.forget();
    $("rv-token").value = "";
    /* Falls back to the guest banner when this visitor arrived through an
       invitation and signed in on top of it. */
    renderIdentity();
    renderThreads();
  }

  async function signIn(token, stack) {
    auth = { token: token, stack: stack, project: null, refresh: null };
    try {
      var data = await api("/api/artifacts");
      SESSION.write(auth);
      $("rv-token").value = "";
      signedIn(data.project_id);
      renderThreads();
    } catch (err) {
      auth = null;
      throw err;
    }
  }

  /* ----------------------------------------------------------------- live */

  /* How long after a document load a silent frame still counts as "at the
     top": the reporter posts once on load, so silence past this means it
     never ran and the shell should ask rather than assume. */
  var SCROLL_GRACE_MS = 4000;

  function atTop() {
    if (docScrollY === null) {
      return (Date.now() - docLoadedAt) < SCROLL_GRACE_MS;
    }
    return docScrollY === 0;
  }

  /* Refresh the threads without costing the reviewer their place. Drafts live
     outside the DOM (see `drafts`), so re-rendering cannot eat them; the
     sidebar's own scroll offset is restored by hand. */
  function refreshThreads() {
    var side = $("rv-side");
    var at = side ? side.scrollTop : 0;
    return loadThreads().then(function () {
      if (side) { side.scrollTop = at; }
    });
  }

  /* True only when swapping the document destroys nothing and yanks nothing:
     the reader is at the top, no comment or reply is half-written, the unlock
     panel is not up, and the composer is not holding a live selection. */
  function safeToSwap() {
    if (!atTop()) { return false; }
    var comment = $("rv-comment");
    if (comment && comment.value.trim()) { return false; }
    for (var tid in drafts) {
      if (Object.prototype.hasOwnProperty.call(drafts, tid) &&
          String(drafts[tid]).trim()) { return false; }
    }
    var lock = $("rv-lock");
    if (lock && !lock.hidden) { return false; }
    var composer = $("rv-composer");
    if (composer && !composer.hidden) { return false; }
    return true;
  }

  /* Move to the new head: the version badge and `headVersion` follow the
     document, never lead it, so a comment written here is always filed
     against the version actually on screen. */
  function applyDocument() {
    return loadVersions()
      .then(loadDocument)
      .then(refreshThreads)
      .then(function () { show($("rv-live"), false); })
      .catch(readFailed);
  }

  function onLive(next, previous) {
    if (String(next.head_version) === String(previous.head_version)) {
      /* Comments, proposals or the draft/final flag moved; the document did
         not. Refreshing those in place disturbs nobody. */
      loadVersions().then(refreshThreads).catch(function () {});
      return;
    }
    refreshThreads().catch(function () {});
    if (safeToSwap()) { applyDocument(); return; }
    $("rv-live-text").textContent =
      "v" + next.head_version + " of this document has been published.";
    show($("rv-live"), true);
  }

  /* ------------------------------------------------------------- messages */

  window.addEventListener("message", function (event) {
    var frame = $("rv-frame");
    /* Only the artifact frame may talk to the shell. */
    if (!frame || event.source !== frame.contentWindow) { return; }
    var data = event.data;
    if (!data || typeof data !== "object") { return; }
    if (data.type === "ah-scroll") {
      var y = Number(data.y);
      docScrollY = isFinite(y) && y > 0 ? y : 0;
      return;
    }
    if (data.type === "ah-ready") { sendAnchors(); return; }
    if (data.type === "ah-anchored") {
      anchoredIds = {};
      (data.tids || []).forEach(function (tid) {
        anchoredIds[String(tid)] = true;
      });
      renderThreads();
      return;
    }
    if (data.type === "ah-select") { openComposer(data); return; }
    if (data.type === "ah-open") { focusThread(String(data.tid)); }
  });

  /* --------------------------------------------------------------- wiring */

  $("rv-stack").addEventListener("change", function () {
    show($("rv-custom-wrap"), $("rv-stack").value === "__custom__");
  });

  $("rv-signin-form").addEventListener("submit", function (event) {
    event.preventDefault();
    setError($("rv-signin-error"), "");
    var token = $("rv-token").value.trim();
    var choice = $("rv-stack").value;
    var stack = choice === "__custom__" ? $("rv-custom").value.trim() : choice;
    if (!token) {
      setError($("rv-signin-error"), "Enter a Storage API token.");
      return;
    }
    if (!stack) {
      setError($("rv-signin-error"), "Enter the stack URL.");
      return;
    }
    var button = $("rv-signin-btn");
    button.disabled = true;
    signIn(token, stack).catch(function (err) {
      setError($("rv-signin-error"), err.message);
    }).then(function () {
      button.disabled = false;
    });
  });

  $("rv-lock-form").addEventListener("submit", function (event) {
    event.preventDefault();
    /* Not trimmed: a password may legitimately begin or end with a space. */
    var value = $("rv-lock-password").value;
    if (!value) {
      setError($("rv-lock-error"), "Enter the password.");
      return;
    }
    setError($("rv-lock-error"), "");
    var button = $("rv-lock-btn");
    button.disabled = true;
    unlock(value).catch(function (err) {
      setError($("rv-lock-error"), err.message);
    }).then(function () { button.disabled = false; });
  });

  $("rv-live-go").addEventListener("click", function () { applyDocument(); });

  $("rv-logout").addEventListener("click", signedOut);
  $("rv-post").addEventListener("click", submitComment);
  $("rv-cancel").addEventListener("click", closeComposer);
  $("rv-toggle").addEventListener("click", function () {
    var side = $("rv-side");
    show(side, side.hidden);
  });

  /* ---------------------------------------------------------------- start */

  guest = readInvite();
  if (guest) { checkInvite(); }

  auth = SESSION.read();
  if (auth) {
    var stacks = Array.prototype.map.call($("rv-stack").options,
      function (option) { return option.value; });
    if (stacks.indexOf(auth.stack) === -1) {
      $("rv-stack").value = "__custom__";
      $("rv-custom").value = auth.stack;
      show($("rv-custom-wrap"), true);
    } else {
      $("rv-stack").value = auth.stack;
    }
    api("/api/artifacts").then(function (data) {
      signedIn(data.project_id);
      renderThreads();
    }, function (err) {
      if (SESSION.rejected(err)) {
        auth = null;
        SESSION.forget();
        renderIdentity();
        setError($("rv-signin-error"),
          "That session is no longer valid: " + err.message);
        return;
      }
      /* A hub-side failure is the same for everyone; keep the credential and
         let the reader retry rather than making them sign in again. */
      setError($("rv-signin-error"),
        "The hub could not confirm your account just now \u2014 reload to " +
        "try again. (" + err.message + ")");
    });
  }

  loadDocument().catch(readFailed);
  loadVersions().then(loadThreads).catch(readFailed);

  window.AHLive.watch({ base: BASE, id: ID, onChange: onLive });
})();
"""


def review_page(base_url: str, artifact_id: str, service_version: str) -> str:
    """Render the two-pane review UI served at ``/a/{id}/review``.

    The page ships no artifact content and no credential of its own. Its
    JavaScript fetches ``/raw``, ``/versions`` and ``/comments`` for this
    artifact, injects :data:`_ANNOTATION_JS` into the fetched HTML and loads
    the result into a ``srcdoc`` iframe sandboxed *without*
    ``allow-same-origin``. The artifact therefore runs in an opaque origin: its
    scripts cannot read this document, its cookies or the Storage token the
    visitor may keep in ``sessionStorage`` (the same ``hub_admin_auth`` entry
    ``/admin`` uses, so one sign-in serves both pages). The two sides exchange
    nothing but ``postMessage`` envelopes.

    **Guest mode.** Opened through an invitation link — the URL the owner got
    from ``POST /api/artifacts/{id}/invitations``, whose ``#invite=`` fragment
    carries ``{invitation_id}.{secret}`` — the page reads that fragment, clears
    it from the address bar, validates it against ``GET /a/{id}/guest`` and
    then comments with an ``X-Artifact-Guest`` header instead of a token. The
    fragment never reaches the server as part of a URL, and the credential
    never leaves this tab.

    **Locked documents.** A password-protected artifact refuses ``/raw``,
    ``/versions``, ``/comments`` and every comment write with 401 ``password
    required``, and an invited guest has never seen the standalone unlock
    form. When a read or a write is refused that way - and ``/a/{id}/meta``,
    public even while the document is protected, confirms ``protected`` - the
    page swaps the document pane for its own unlock panel, which posts the
    password form-encoded to ``POST /a/{id}/unlock`` and re-tries a protected
    read to see whether the cookie took. The password is held in the page's
    closure for the life of the tab (never storage, never a URL) because
    comment writes go to ``/api/...``, which the unlock cookie - scoped to
    ``/a/{id}`` - never reaches; those requests carry it as the documented
    ``X-Artifact-Password`` header. An invitation still grants a voice, not a
    key: the panel says so rather than calling the invitation broken.
    """
    base = html.escape(base_url.rstrip("/"))
    safe_id = html.escape(artifact_id)

    options = "".join(
        f'<option value="{html.escape(alias)}">{html.escape(alias)}</option>'
        for alias in _ADMIN_STACKS
    )
    options += '<option value="__custom__">custom URL…</option>'

    body = f"""<div class="rv">
<header class="rv-top">
<span class="rv-brand">Artifact Hub · Review</span>
<span class="badge badge--version">v{html.escape(service_version)}</span>
<span class="badge" id="rv-head">loading…</span>
<span class="spacer"></span>
<a class="btn btn-sm" href="{base}/a/{safe_id}">Open document</a>
<a class="btn btn-sm" href="{base}/a/{safe_id}/versions?format=html">Versions</a>
<a class="btn btn-sm" href="{base}/a/{safe_id}/export/vault">Export vault</a>
<button type="button" class="btn btn-sm" id="rv-toggle">Comments</button>
</header>
<div class="rv-body">
<div class="rv-doc">
<iframe id="rv-frame" title="artifact under review"
  sandbox="allow-scripts allow-popups"></iframe>
<div class="rv-lock" id="rv-lock" hidden>
<div class="rv-lock-card">
<div class="rule">locked artifact</div>
<div class="card">
<h1>Password required</h1>
<p>This document is password-protected. Enter its password to read it and to
comment on it.</p>
<p id="rv-lock-guest" hidden>Your invitation is fine — it is what lets you
comment here without a Keboola account. The password is a separate lock on the
document itself, so you need it as well; whoever invited you can pass it on.</p>
<form id="rv-lock-form" autocomplete="off">
<label for="rv-lock-password">Password</label>
<input class="rv-field" type="password" id="rv-lock-password" name="password"
  autocomplete="current-password" spellcheck="false">
<button type="submit" class="btn btn-primary btn-wide" id="rv-lock-btn">Unlock</button>
<p class="err" id="rv-lock-error" hidden></p>
</form>
</div>
</div>
</div>
</div>
<aside class="rv-side" id="rv-side">
<h2>sign in</h2>
<div id="rv-signin">
<form id="rv-signin-form" autocomplete="off">
<label for="rv-token">Storage API token</label>
<input class="rv-field" type="password" id="rv-token" autocomplete="off"
  spellcheck="false" placeholder="your Keboola Storage API token">
<label for="rv-stack">Stack</label>
<select class="rv-field" id="rv-stack">{options}</select>
<div id="rv-custom-wrap" hidden>
<label for="rv-custom">Stack URL</label>
<input class="rv-field" type="text" id="rv-custom" spellcheck="false"
  placeholder="https://connection.keboola.com">
</div>
<button type="submit" class="btn btn-primary btn-wide" id="rv-signin-btn">Sign in to comment</button>
<p class="err" id="rv-signin-error" hidden></p>
</form>
<p class="rv-hint">Reading is public; commenting needs a Keboola identity.
Have no token at hand? <a href="{base}/login">Sign in with Keboola</a> and
come back — the credential is shared with this page.</p>
<p class="rv-hint">Either way it stays in this browser tab
(<code>sessionStorage</code>, shared with <a href="{base}/admin">/admin</a>)
and is never sent anywhere but this hub's own API.</p>
<p class="rv-hint">No Keboola account? The artifact's owner can send you a
guest invitation link, which lets you comment here without one.</p>
</div>
<div id="rv-account" hidden>
<div class="rv-thread-top">
<span class="badge badge--version" id="rv-project"></span>
<button type="button" class="btn btn-sm" id="rv-logout">Log out</button>
</div>
</div>
<div id="rv-guest" hidden>
<div class="rv-thread-top">
<span class="badge badge--version" id="rv-guest-name"></span>
</div>
<p class="rv-hint">You are here on an invitation, so no Keboola account is
needed. You can open threads, reply, and resolve or delete the threads you
opened. The invitation lives in this tab only — keep the link if you want to
come back, and expect it to stop working once whoever invited you revokes
it.</p>
</div>

<h2>comment</h2>
<div id="rv-composer" hidden>
<div class="rv-quote" id="rv-quote"></div>
<label for="rv-comment">Your comment</label>
<textarea class="rv-field" id="rv-comment"
  placeholder="What about this passage?"></textarea>
<div class="rv-actions">
<button type="button" class="btn btn-primary btn-sm" id="rv-post">Comment</button>
<button type="button" class="btn btn-sm" id="rv-cancel">Cancel</button>
</div>
<p class="err" id="rv-composer-error" hidden></p>
</div>
<p class="rv-hint" id="rv-hint">Select any text in the document to start a
thread. Threads stay attached to the version they were made on, so one made on
an older version may no longer match this text.</p>

<h2>threads</h2>
<div class="rv-thread-top">
<span class="rv-when" id="rv-count"></span>
</div>
<p class="rv-hint" id="rv-frozen" hidden>This document is final: no new
versions and no new comments.</p>
<p class="rv-hint" id="rv-closed" hidden>Commenting is closed on this
document.</p>
<p class="rv-hint" id="rv-lock-side" hidden>The discussion is part of the
protected document: unlock it on the left to read the threads and to add
one.</p>
<p class="err" id="rv-error" hidden></p>
<p class="empty" id="rv-empty" hidden>No comments yet.</p>
<ul class="rv-threads" id="rv-threads"></ul>
</aside>
</div>
<div class="ahlive" id="rv-live" hidden>
<span class="ahlive-text" id="rv-live-text">A newer version of this document
has been published.</span>
<button type="button" class="btn btn-sm btn-primary" id="rv-live-go">Show
it</button>
</div>
</div>
<script>window.HUB_BASE = "{base}"; window.HUB_ARTIFACT_ID = "{safe_id}";</script>
<script type="text/plain" id="rv-anno">"""

    return _page(
        f"Review — {artifact_id}",
        _CONTROLS_CSS + _REVIEW_CSS + _LIVE_CSS,
        body
        + _ANNOTATION_JS
        + '</script>\n<script type="text/plain" id="rv-scroll">'
        + _SCROLL_REPORTER_JS
        + "</script>\n<script>"
        + _LIVE_JS
        + "</script>\n<script>"
        + _SESSION_JS
        + "</script>\n<script>"
        + _REVIEW_JS
        + "</script>",
    )


# --------------------------------------------------------------------------
# Changelog
# --------------------------------------------------------------------------

#: Prose styles for the changelog body. The shell's own rules already size h1
#: and h2; what a long Markdown document adds is list, table and release-heading
#: rhythm. Each ``h2`` (one release) is dressed as the shell's ``//`` section
#: label — same monospace, same rule running to the right margin — so a rendered
#: changelog reads as a page of this service rather than as a pasted document.
_CHANGELOG_CSS = """
.changelog { max-width: 48rem; }
.changelog h2 {
  display: flex;
  align-items: center;
  gap: .6rem;
  font-size: 1rem;
  margin: 2.75rem 0 .85rem;
  padding-top: .2rem;
}
.changelog h2::before { content: "//"; color: var(--accent); font-weight: 700; }
.changelog h2::after { content: ""; flex: 1; height: 1px;
  background: var(--line); }
.changelog h3 { font-size: .82rem; letter-spacing: .12em;
  text-transform: uppercase; color: var(--muted); margin: 1.5rem 0 .4rem; }
.changelog ul, .changelog ol { padding-left: 1.15rem; margin: .5rem 0 1rem; }
.changelog li { margin: .3rem 0; color: var(--ink-2); }
.changelog li::marker { color: var(--accent); }
.changelog a { overflow-wrap: anywhere; }
.changelog table { margin: .5rem 0 1.25rem; }
.changelog pre {
  overflow-x: auto;
  background: var(--term-bg);
  color: var(--term-fg);
  border: 1px solid var(--term-line);
  border-radius: var(--radius);
  padding: .9rem 1rem;
  font-size: .8rem;
  line-height: 1.7;
}
.changelog pre code { background: none; color: inherit; padding: 0; }
.changelog blockquote { margin: 1rem 0; padding: .1rem 0 .1rem 1rem;
  border-left: 2px solid var(--accent); color: var(--muted); }
.changelog hr { border: 0; border-top: 1px solid var(--line); margin: 2rem 0; }
"""


#: Pulls the document's own leading ``<h1>`` out of the rendered fragment, so
#: it can become the page's hero instead of appearing a second time under one.
_LEADING_H1_RE = re.compile(r"\A\s*<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)


def _hoist_heading(body_html: str, fallback: str) -> tuple[str, str]:
    """Split a leading ``<h1>`` off a rendered fragment: ``(heading, rest)``.

    A Markdown document titles itself, and the shell wants that title in its
    hero rather than repeated below one. Anything else — a document that opens
    on a paragraph, or on an ``h2`` — keeps its shape and gets ``fallback``.
    """
    match = _LEADING_H1_RE.match(body_html)
    if match is None:
        return fallback, body_html
    heading = match.group(1).strip()
    return (heading or fallback), body_html[match.end():].lstrip()


def changelog_page(body_html: str, service_version: str, github_url: str) -> str:
    """Wrap pre-rendered changelog Markdown in the service's own shell.

    ``body_html`` is an HTML *fragment* — the caller renders it (``src.main``
    uses the builder's configured markdown-it, so the dialect matches what a
    published artifact gets) and this function supplies the chrome: the
    graph-paper grid, the monospace hero, the ``//`` label treatment adapted so
    each release heading reads like a section rule, and the same footer the
    landing page carries. The document's own leading ``h1`` becomes that hero,
    so the page is titled by the file rather than by this function.

    The fragment is trusted markup, not user input: it comes from a file in
    this repository, rendered by this process. Everything else on the page is
    escaped as usual.
    """
    version = html.escape(service_version)
    repo = html.escape(github_url.rstrip("/"))
    heading, body_html = _hoist_heading(body_html, "Changelog")

    body = f"""<main>
<header class="hero">
<h1>{heading}</h1>
<p class="lead">Every released change to the Artifact Hub, newest first. The
same file is served verbatim at <a href="/changelog.md">/changelog.md</a> for
machines.</p>
<div class="hero-meta">
<span class="badge badge--version">v{version}</span>
<span class="badge">running now</span>
</div>
</header>

<article class="changelog">
{body_html}
</article>

<footer>
<span>kbc-artifact-hub v{version}</span>
<span class="spacer"></span>
<a href="/">hub home</a>
<a href="/changelog.md">/changelog.md</a>
<a href="/docs">/docs</a>
<a href="{repo}">source</a>
</footer>
</main>"""

    return _page("Changelog · Artifact Hub", _CHANGELOG_CSS, body)


# --------------------------------------------------------------------------
# Visual diff
# --------------------------------------------------------------------------

_VISUAL_DIFF_CSS = """
html, body { height: 100%; }
html { background-image: none; }

.vd { display: flex; flex-direction: column; height: 100vh; }

.vd-top { display: flex; align-items: center; flex-wrap: wrap; gap: .5rem;
  padding: .5rem .85rem; border-bottom: 1px solid var(--line);
  background: var(--panel); }
.vd-brand { font-family: var(--font-mono); font-weight: 700; font-size: .88rem;
  letter-spacing: -.01em; }
.vd-top .spacer { flex: 1; }
.vd-added { color: var(--live); font-weight: 700; }
.vd-removed { color: var(--danger); font-weight: 700; }
.vd-stat { font-family: var(--font-mono); font-size: .76rem;
  color: var(--muted); }

.vd-body { flex: 1; display: flex; min-height: 0; }
.vd-pane { flex: 1 1 50%; min-width: 0; display: flex; flex-direction: column;
  background: #ffffff; }
.vd-pane + .vd-pane { border-left: 1px solid var(--line); }
.vd-head { display: flex; align-items: center; gap: .45rem;
  padding: .35rem .7rem; border-bottom: 1px solid var(--line);
  background: var(--panel); }
.vd-title { font-size: .78rem; color: var(--muted); overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.vd-pane iframe { flex: 1; width: 100%; border: 0; background: #ffffff; }

@media (max-width: 45rem) {
  .vd-body { flex-direction: column; }
  .vd-pane + .vd-pane { border-left: 0; border-top: 1px solid var(--line); }
}
"""

#: Injected into each side's document before it goes into its ``srcdoc``.
#:
#: It runs in an **opaque origin** (both frames are sandboxed without
#: ``allow-same-origin``), so its only channel to the shell is ``postMessage``:
#: it reports its own scroll position as a *ratio* of scrollable height, and
#: applies a ratio the shell relays from the other side. Ratios rather than
#: pixels, because the two versions are different documents of different
#: heights — matching absolute offsets would drift immediately.
#:
#: Everything here is best effort by design. An artifact whose own scripts throw
#: still renders; it just stops driving the other pane.
_VISUAL_SYNC_JS = """
(function () {
  "use strict";

  var SIDE = window.name || "";

  /* The ratio this frame was last *told* to scroll to, or -1 when its position
     is its own doing. It is how an echo is recognised without a timer: the
     scroll event caused by applying a relayed position looks exactly like this
     value, so it is swallowed once and the pair settles instead of chasing
     each other. Deliberately no setTimeout anywhere — a background tab
     throttles timers hard, and scroll sync that only works in a foreground tab
     would be worse than none. */
  var applied = -1;

  function scroller() {
    return document.scrollingElement || document.documentElement ||
      document.body;
  }

  function span() {
    var node = scroller();
    if (!node) { return 0; }
    return node.scrollHeight - node.clientHeight;
  }

  function ratio() {
    var node = scroller();
    var height = span();
    if (!node || height <= 0) { return 0; }
    return node.scrollTop / height;
  }

  /* Two positions count as the same when they are within a pixel or two of
     each other: scrollTop is an integer, so a ratio can never be reproduced
     exactly, and an off-by-one pixel must not read as a fresh scroll. */
  function same(a, b) {
    var height = span();
    if (height <= 0) { return true; }
    return Math.abs(a - b) * height < 2;
  }

  function post(message) {
    try { parent.postMessage(message, "*"); } catch (err) { /* detached */ }
  }

  window.addEventListener("scroll", function () {
    var now = ratio();
    if (applied >= 0 && same(now, applied)) {
      /* This is the scroll we were asked to make. Report nothing, and go back
         to treating our own movement as news. */
      applied = -1;
      return;
    }
    applied = -1;
    post({ type: "ahd-scroll", side: SIDE, ratio: now });
  }, { passive: true });

  window.addEventListener("message", function (event) {
    var data = event.data;
    if (!data || typeof data !== "object") { return; }
    if (data.type !== "ahd-scroll-to") { return; }
    var value = Number(data.ratio);
    if (!isFinite(value)) { return; }
    value = Math.max(0, Math.min(1, value));
    var node = scroller();
    var height = span();
    if (!node || height <= 0) { return; }
    /* Already there: moving nothing also emits nothing, which is what stops a
       relay from bouncing back and forth. */
    if (same(ratio(), value)) { return; }
    applied = value;
    try {
      node.scrollTop = value * height;
    } catch (err) {
      applied = -1;
    }
  });

  post({ type: "ahd-ready", side: SIDE });
})();
"""

#: The shell half of the scroll sync: relay each side's reported ratio to the
#: other, and never back to the sender.
_VISUAL_DIFF_JS = """
(function () {
  "use strict";

  var frames = {
    older: document.getElementById("vd-older"),
    newer: document.getElementById("vd-newer")
  };

  function other(side) {
    return side === "older" ? frames.newer : frames.older;
  }

  window.addEventListener("message", function (event) {
    var data = event.data;
    if (!data || typeof data !== "object") { return; }
    if (data.type !== "ahd-scroll") { return; }
    /* Only our own two frames may drive this page. */
    if (event.source !== frames.older.contentWindow &&
        event.source !== frames.newer.contentWindow) { return; }
    var side = event.source === frames.older.contentWindow ? "older" : "newer";
    var target = other(side);
    if (!target || !target.contentWindow) { return; }
    try {
      target.contentWindow.postMessage(
        { type: "ahd-scroll-to", ratio: data.ratio }, "*"
      );
    } catch (err) {
      /* One pane failing to follow must never break the other. */
    }
  });
})();
"""


def _inject_sync(document_html: str) -> str:
    """Append the scroll-sync script to one side's document.

    Inserted before the last ``</body>`` when there is one so the artifact's own
    ``DOMContentLoaded`` handlers still see the document they expect, and simply
    appended otherwise — a published artifact is not guaranteed to be a
    well-formed document.
    """
    snippet = f"<script>{_VISUAL_SYNC_JS}</script>"
    lower = document_html.lower()
    at = lower.rfind("</body>")
    if at < 0:
        return document_html + snippet
    return document_html[:at] + snippet + document_html[at:]


def _visual_pane(side: str, label: str, title: str, document_html: str) -> str:
    """One half of the visual diff: a header plus its sandboxed iframe.

    The document goes into ``srcdoc`` — escaped with ``quote=True``, since it is
    an attribute *value* — inside a frame sandboxed **without**
    ``allow-same-origin``. Both versions therefore run in their own opaque
    origins: they can neither read this page nor each other, which matters
    doubly here, where two mutually distrusting versions of a document are on
    screen at once.
    """
    safe_side = html.escape(side, quote=True)
    frame_title = html.escape(f"{label} — {title}", quote=True)
    return (
        f'<section class="vd-pane">'
        f'<div class="vd-head">'
        f'<span class="badge badge--version">{html.escape(label)}</span>'
        f'<span class="vd-title">{html.escape(title or "untitled")}</span>'
        "</div>"
        f'<iframe id="vd-{safe_side}" name="{safe_side}" title="{frame_title}" '
        'sandbox="allow-scripts allow-popups" '
        f'srcdoc="{html.escape(_inject_sync(document_html), quote=True)}">'
        "</iframe></section>"
    )


def visual_diff_page(
    older: object,
    newer: object,
    *,
    added: int | None = None,
    removed: int | None = None,
) -> str:
    """Render two versions of one artifact side by side, as they look.

    ``older`` and ``newer`` are version envelopes (anything carrying
    ``version``, ``title`` and ``html``). Each is loaded into its own sandboxed
    ``srcdoc`` iframe and the two panes scroll in step, so a reviewer compares
    the *rendered* documents rather than the source that produced them — which
    is what ``?format=html`` already does well.

    Scroll sync is best effort and says so: an artifact whose own scripts break,
    or one that scrolls an inner element rather than the document, simply stops
    driving the other pane. Nothing else on the page depends on it.

    ``added``/``removed`` are the line counts from the ordinary diff; pass
    ``None`` when they could not be computed and the header quietly omits them.
    """
    older_version = getattr(older, "version", "?")
    newer_version = getattr(newer, "version", "?")
    older_label = f"v{older_version}"
    newer_label = f"v{newer_version}"

    if added is None or removed is None:
        stats = '<span class="vd-stat">line counts unavailable</span>'
    else:
        stats = (
            '<span class="vd-stat">'
            f'<span class="vd-added">+{int(added)}</span> '
            f'<span class="vd-removed">-{int(removed)}</span></span>'
        )

    heading = html.escape(f"{older_label} → {newer_label}")
    panes = _visual_pane(
        "older",
        older_label,
        str(getattr(older, "title", "") or ""),
        str(getattr(older, "html", "") or ""),
    ) + _visual_pane(
        "newer",
        newer_label,
        str(getattr(newer, "title", "") or ""),
        str(getattr(newer, "html", "") or ""),
    )

    body = f"""<div class="vd">
<header class="vd-top">
<span class="vd-brand">Artifact Hub · Visual diff</span>
<span class="badge">{heading}</span>
{stats}
<span class="spacer"></span>
<span class="vd-stat">scrolling is synchronized</span>
</header>
<div class="vd-body">
{panes}
</div>
</div>
<script>{_VISUAL_DIFF_JS}</script>"""

    return _page(
        f"Visual diff — {older_label} vs {newer_label}",
        _CONTROLS_CSS + _VISUAL_DIFF_CSS,
        body,
    )


# --------------------------------------------------------------------------
# Sign-in page
# --------------------------------------------------------------------------

#: Sign-in styles: the method chooser, the device-code display, and the
#: project picker. Everything else comes from :data:`_CSS`,
#: :data:`_CONTROLS_CSS` and the login card in :data:`_ADMIN_CSS`.
_LOGIN_CSS = """
main { max-width: 44rem; }

.methods { display: flex; flex-direction: column; gap: .5rem; margin-top: 1rem; }
.method { display: flex; align-items: flex-start; gap: .7rem; text-align: left;
  width: 100%; padding: .8rem .9rem; border: 1px solid var(--line);
  border-radius: var(--radius); background: var(--panel); color: var(--ink);
  cursor: pointer; font: inherit; }
.method:hover:not(:disabled) { border-color: var(--accent);
  background: var(--accent-soft); }
.method:disabled { opacity: .55; cursor: not-allowed; }
.method-mark { font-family: var(--font-mono); color: var(--accent);
  flex: none; padding-top: .1rem; }
.method-body { min-width: 0; }
.method-name { font-weight: 500; display: block; }
.method-note { color: var(--muted); font-size: .82rem; display: block;
  margin-top: .15rem; }

.usercode { font-family: var(--font-mono); font-size: 2rem; font-weight: 700;
  letter-spacing: .18em; color: var(--ink); background: var(--paper);
  border: 1px dashed var(--line); border-radius: var(--radius);
  padding: .8rem 1rem; text-align: center; margin: .9rem 0 .6rem;
  user-select: all; }
.poll { font-family: var(--font-mono); font-size: .8rem; color: var(--muted); }

.plist { list-style: none; margin: .8rem 0 0; padding: 0; display: flex;
  flex-direction: column; gap: .35rem; max-height: 22rem; overflow-y: auto; }
.pitem { display: flex; align-items: center; gap: .6rem; width: 100%;
  padding: .55rem .7rem; border: 1px solid var(--line); border-radius: 8px;
  background: var(--panel); color: var(--ink); cursor: pointer; font: inherit;
  text-align: left; }
.pitem:hover { border-color: var(--accent); background: var(--accent-soft); }
.pitem-name { flex: 1 1 auto; min-width: 0; overflow-wrap: anywhere; }
.pitem-id { font-family: var(--font-mono); font-size: .74rem;
  color: var(--muted); flex: none; }
.whoami { font-family: var(--font-mono); font-size: .78rem; color: var(--muted);
  margin: 0 0 .2rem; overflow-wrap: anywhere; }
"""

#: The sign-in page, as one IIFE. It drives the same ``/login/*`` endpoints a
#: terminal could call with curl, and finishes by writing the credential into
#: the very same ``sessionStorage`` entry a pasted Storage token goes into —
#: so ``/admin`` and ``/a/{id}/review`` cannot tell the two apart.
_LOGIN_JS = """
(function () {
  "use strict";

  var BASE = String(window.HUB_BASE || "").replace(/\\/+$/, "");
  var PKCE = window.HUB_PKCE === true;
  var RESULT = window.HUB_LOGIN_RESULT || null;

  /* The same record /admin and the review page read, written through the
     module all three share. A sign-in is just another way to fill it in;
     nothing downstream knows which way it was filled. */
  var SESSION = window.hubSession;

  /* Set once a sign-in succeeds, and cleared when a project is chosen. Holds
     the session for exactly as long as the picker is on screen. */
  var pending = null;
  var pollTimer = null;
  var deviceDeadline = 0;

  /* Poll cadence, in seconds. The stack names the interval; these are what to
     do when it does not. RFC 8628 answers slow_down with "add five seconds",
     which matters because the hub forwards interval 0 whenever the stack
     omits one — without the step, slow_down would only change the label. */
  var DEFAULT_POLL_INTERVAL_S = 5;
  var SLOW_DOWN_STEP_S = 5;
  /* Ceiling on the doubling backoff a transient failure falls back to. */
  var MAX_POLL_INTERVAL_S = 30;

  function $(id) { return document.getElementById(id); }
  function show(node, on) { node.hidden = !on; }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }

  function setError(message) {
    var node = $("login-error");
    node.textContent = message || "";
    node.hidden = !message;
  }

  function step(name) {
    ["stack", "device", "project"].forEach(function (id) {
      show($("step-" + id), id === name);
    });
  }

  function chosenStack() {
    var value = $("stack").value;
    if (value === "__custom__") { return $("custom").value.trim(); }
    return value;
  }

  function apiMessage(status, data, text) {
    if (data && typeof data.detail === "string") { return data.detail; }
    if (data && data.detail) { return JSON.stringify(data.detail); }
    if (text) { return "HTTP " + status + ": " + text.slice(0, 300); }
    return "HTTP " + status;
  }

  async function post(path, body) {
    var resp = await fetch(BASE + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    var text = await resp.text();
    var data = null;
    try { data = text ? JSON.parse(text) : null; } catch (err) { data = null; }
    if (!resp.ok) {
      /* The status travels with the error: polling has to tell a terminal
         refusal from a failure that is worth another try. */
      var failure = new Error(apiMessage(resp.status, data, text));
      failure.status = resp.status;
      throw failure;
    }
    return data;
  }

  /* ------------------------------------------------------------- device */

  /* The approval URL comes from an allowlisted https stack, so it is already
     an https URL. Checked anyway: it is the one stack-supplied value that
     goes into an href and into window.open(), and neither should ever be
     handed a javascript: URL because a stack answered oddly. */
  function approvalUrl(raw) {
    return /^https:\/\//.test(String(raw || "")) ? String(raw) : "";
  }

  function stopPolling() {
    if (pollTimer) { window.clearTimeout(pollTimer); pollTimer = null; }
  }

  async function startDevice() {
    var stack = chosenStack();
    if (!stack) { setError("Choose a stack first."); return; }
    setError("");
    var button = $("m-device");
    button.disabled = true;
    try {
      var start = await post("/login/device", { stack: stack });
      var approval = approvalUrl(start.verification_uri_complete);
      $("usercode").textContent = start.user_code;
      $("verify-plain").textContent = start.verification_uri;
      var link = $("verify-link");
      if (approval) { link.href = approval; } else { link.removeAttribute("href"); }
      show(link, !!approval);
      deviceDeadline = Date.now() + (start.expires_in || 0) * 1000;
      step("device");
      /* Opened from the click that started the sign-in, so it is not a popup
         the browser blocks. A blocked one is not fatal: the link below the
         code does the same thing, and so does the plain URL under it. */
      if (approval) { window.open(approval, "_blank", "noopener"); }
      schedulePoll(start, start.interval || DEFAULT_POLL_INTERVAL_S);
    } catch (err) {
      setError(err.message);
    } finally {
      button.disabled = false;
    }
  }

  function schedulePoll(start, interval) {
    stopPolling();
    pollTimer = window.setTimeout(function () {
      pollDevice(start, interval);
    }, Math.max(1, interval) * 1000);
  }

  async function pollDevice(start, interval) {
    if (Date.now() > deviceDeadline) {
      setError("The code expired before it was approved. Start again.");
      step("stack");
      return;
    }
    try {
      var data = await post("/login/device/token", {
        stack: start.stack,
        device_code: start.device_code
      });
      if (data && data.status === "pending") {
        /* The stack's own interval when it sends one, plus RFC 8628's five
           seconds when it says slow_down. Kept for every later poll, not just
           the next one. */
        var next = data.interval || interval;
        if (data.slow_down) { next = Math.max(next, interval + SLOW_DOWN_STEP_S); }
        $("poll-note").textContent = data.slow_down
          ? "waiting for approval (slowing down)…"
          : "waiting for approval…";
        schedulePoll(start, next);
        return;
      }
      signedIn(data);
    } catch (err) {
      /* Only a terminal refusal ends the sign-in. Everything else — a network
         blip, this hub's own 429, a stack that timed out — leaves the device
         code valid until deviceDeadline, and starting over would cost the
         person a second code and a second approval tab for nothing. */
      if (err && err.status === 400) {
        stopPolling();
        setError(err.message);
        step("stack");
        return;
      }
      $("poll-note").textContent = "waiting for approval (retrying)…";
      schedulePoll(start, Math.min(interval * 2, MAX_POLL_INTERVAL_S));
    }
  }

  function cancelDevice() {
    stopPolling();
    setError("");
    step("stack");
  }

  /* -------------------------------------------------------------- pkce */

  function startPkce() {
    var stack = chosenStack();
    if (!stack) { setError("Choose a stack first."); return; }
    window.location.href = BASE + "/login/pkce/start?stack=" +
      encodeURIComponent(stack);
  }

  /* ------------------------------------------------------------ project */

  function signedIn(data) {
    stopPolling();
    pending = data.credential;
    var who = pending.user && (pending.user.email || pending.user.name);
    $("whoami").textContent = who ? "signed in as " + who : "";
    renderProjects(data.projects || [], data.projects_unavailable === true);
    step("project");
  }

  function renderProjects(projects, unavailable) {
    var list = $("projects");
    list.textContent = "";
    projects.forEach(function (project) {
      var button = el("button", "pitem");
      button.type = "button";
      button.appendChild(el("span", "pitem-name", project.name));
      button.appendChild(el("span", "pitem-id", project.role));
      button.appendChild(el("span", "pitem-id", "#" + project.id));
      button.addEventListener("click", function () { finish(project.id); });
      list.appendChild(button);
    });
    var none = projects.length === 0;
    show($("projects-empty"), none);
    show($("manual"), none || unavailable);
    $("projects-empty").textContent = unavailable
      ? "This stack did not list your projects. Enter a project id instead."
      : "Your account is not a member of any project on this stack yet.";
  }

  function finishManual() {
    var raw = $("project-id").value.trim();
    if (!/^[0-9]+$/.test(raw)) {
      setError("A project id is a number, e.g. 1234.");
      return;
    }
    finish(parseInt(raw, 10));
  }

  function finish(projectId) {
    setError("");
    var kept = SESSION.write({
      token: pending.access_token,
      refresh: pending.refresh_token,
      stack: pending.stack,
      project: projectId
    });
    if (!kept) {
      /* This page has nowhere else to put the session: the next page is a
         fresh document that reads it back out of storage. */
      setError("This browser refused to keep the session. Enable storage " +
        "for this site, or paste a Storage token into /admin instead.");
      return;
    }
    pending = null;
    window.location.href = BASE + "/admin";
  }

  /* --------------------------------------------------------------- boot */

  $("stack").addEventListener("change", function () {
    show($("custom-wrap"), $("stack").value === "__custom__");
  });
  $("m-device").addEventListener("click", startDevice);
  $("device-cancel").addEventListener("click", cancelDevice);
  $("project-go").addEventListener("click", finishManual);
  if (PKCE) { $("m-pkce").addEventListener("click", startPkce); }

  if (RESULT && RESULT.error) {
    setError(RESULT.error);
  } else if (RESULT && RESULT.credential) {
    signedIn(RESULT);
  }
})();
"""


def login_page(
    base_url: str,
    service_version: str,
    github_url: str,
    pkce_available: bool,
    result: dict | None,
) -> str:
    """Render the sign-in page served at ``/login``.

    ``pkce_available`` decides whether the one-hop browser flow is offered at
    all — a Keboola stack only accepts a loopback redirect for it. ``result``
    is the outcome of a PKCE callback: either ``{"error": ...}`` or a
    credential plus the projects it reaches, which boots the page straight
    into its project-picking step.
    """
    base = html.escape(base_url.rstrip("/"))
    version = html.escape(service_version)
    repo = html.escape(github_url.rstrip("/"))

    options = "".join(
        f'<option value="{html.escape(alias)}">{html.escape(alias)}</option>'
        for alias in _ADMIN_STACKS
    )
    options += '<option value="__custom__">custom URL…</option>'

    pkce_attrs = "" if pkce_available else " disabled"
    pkce_note = (
        "One hop through your browser. Nothing to type."
        if pkce_available
        else "Needs a hub on http://127.0.0.1 — a stack accepts no other "
        "callback for this flow."
    )

    # Hoisted out of the f-string: the JSON may contain a backslash escape,
    # which a 3.11 f-string expression may not.
    result_json = _login_result_json(result)

    body = f"""<main>
<header class="ahead">
<div>
<h1>Artifact Hub · Sign in</h1>
<p class="lead">Sign in with your Keboola account instead of hunting down a
Storage API token. Works on any stack this hub allows, and ends in the same
place a pasted token does — a credential held in this browser tab only.</p>
</div>
</header>

<section id="step-stack">
<h2 class="label">where</h2>
<div class="card login-card">
<label for="stack">Stack</label>
<select id="stack" name="stack">{options}</select>
<div id="custom-wrap" hidden>
<label for="custom">Stack URL</label>
<input type="text" id="custom" name="custom" spellcheck="false"
  placeholder="https://connection.keboola.com">
</div>
<div class="methods">
<button type="button" class="method" id="m-pkce"{pkce_attrs}>
<span class="method-mark">&rarr;</span>
<span class="method-body">
<span class="method-name">Sign in with your browser</span>
<span class="method-note">{html.escape(pkce_note)}</span>
</span>
</button>
<button type="button" class="method" id="m-device">
<span class="method-mark">#</span>
<span class="method-body">
<span class="method-name">Sign in with a code</span>
<span class="method-note">Approve a short code in your Keboola tab. Works
from anywhere, including a browser on another device.</span>
</span>
</button>
</div>
<p class="err" id="login-error" hidden></p>
<p class="hint">Already have a Storage API token? <a href="{base}/admin">Paste
it into the studio</a> instead — both end up in the same place.</p>
</div>
</section>

<section id="step-device" hidden>
<h2 class="label">approve</h2>
<div class="card login-card">
<p>Enter this code in your Keboola tab:</p>
<div class="usercode" id="usercode"></div>
<p><a class="btn btn-primary btn-wide" id="verify-link" target="_blank"
  rel="noopener">Open the approval page</a></p>
<p class="hint">If that tab did not open, go to
<code id="verify-plain"></code> and enter the code yourself.</p>
<p class="poll" id="poll-note">waiting for approval…</p>
<button type="button" class="btn" id="device-cancel">Cancel</button>
</div>
</section>

<section id="step-project" hidden>
<h2 class="label">which project</h2>
<div class="card login-card">
<p class="whoami" id="whoami"></p>
<p>Artifacts are owned by a project, so pick the one you are publishing as.</p>
<p class="hint">What this sign-in can reach was settled on Keboola's own
screen a moment ago. This step only chooses which of those projects your
calls act as — change it any time by signing in again.</p>
<ul class="plist" id="projects"></ul>
<p class="empty" id="projects-empty" hidden></p>
<div id="manual" hidden>
<label for="project-id">Project id</label>
<input type="text" id="project-id" inputmode="numeric" spellcheck="false"
  placeholder="1234">
<button type="button" class="btn btn-primary btn-wide" id="project-go">Continue</button>
</div>
</div>
</section>

<footer>
<span>kbc-artifact-hub v{version}</span>
<span class="spacer"></span>
<a href="{base}/">hub home</a>
<a href="{base}/admin">/admin</a>
<a href="{repo}">source</a>
</footer>
</main>
<script>window.HUB_BASE = "{base}";
window.HUB_PKCE = {str(pkce_available).lower()};
window.HUB_LOGIN_RESULT = {result_json};</script>
<script>"""

    return _page(
        "Artifact Hub · Sign in",
        _CONTROLS_CSS + _ADMIN_CSS + _LOGIN_CSS,
        body + _SESSION_JS + "</script>\n<script>" + _LOGIN_JS + "</script>",
    )


def _login_result_json(result: dict | None) -> str:
    """Serialize a PKCE outcome for the page, safe to inline in a script.

    ``<`` is escaped so no value — a project name included — can close the
    script element it sits in. The result may carry the visitor's own session
    tokens; the response that embeds it is ``no-store``, and it goes nowhere
    but the tab that started the sign-in.
    """
    if result is None:
        return "null"
    return json.dumps(result).replace("<", "\\u003c")
