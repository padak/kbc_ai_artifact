"""Artifact builder.

Turns publish inputs (raw HTML, Markdown or a public git repository) into one
final, self-contained HTML document.

Three entry points mirror the three publish input shapes:

* :func:`build_from_html` — near pass-through: a page saved from Claude's
  artifact viewer is rebuilt as a standalone document, anything else is used
  byte for byte, and only the title is derived.
* :func:`build_from_markdown` — markdown-it-py rendering wrapped in
  :data:`PAGE_TEMPLATE` (tables, task lists, anchors, mermaid, highlight.js).
* :func:`build_from_git` — shallow clone and entry-file selection, then
  whatever the chosen entry's shape already gets on its own: the same Markdown
  rendering for a Markdown entry, the same rebuild-then-pass-through for an
  HTML one, plus inlining of relative images as ``data:`` URIs.

Every failure that is the caller's fault raises :class:`BuildError`; its message
is user-facing and is mapped to HTTP 422 by the API layer.

The only outbound network access in this module is the ``git clone``
subprocess. Everything else is local.

Private repositories are supported by passing a transient ``git_token`` (and
optionally ``git_username``) to :func:`build_from_git`. The credentials are
handed to git through a short-lived ``GIT_ASKPASS`` helper (token in a curated
subprocess environment, never in ``argv`` and never in the clone URL) for the
duration of that one subprocess call and nothing else: the *unauthenticated*
URL is what gets logged, recorded and echoed back, and every string that can
reach a :class:`BuildError` message goes through :func:`_scrub` with the token
as an extra literal to redact (defence in depth).

Security note on rendered Markdown/HTML: the Markdown renderer intentionally
keeps ``html: True`` so authors can embed rich raw HTML (and thus ``<script>``)
in "markdown" artifacts, and an HTML artifact keeps whatever markup its author
wrote. This is *by design* for rich content; the security boundary that stops
artifact script from touching the hub origin is the sandboxed iframe applied by
the serving layer (``src/main.py`` / ``src/pages.py``), NOT sanitisation here.
Do not rely on this module to neutralise artifact markup — the frame-runtime
rebuild below removes one specific *foreign* wrapper, it is not a filter.
"""

from __future__ import annotations

import base64
import html as html_lib
import ipaddress
import logging
import mimetypes
import os
import re
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import quote, unquote, urlparse, urlunparse

from markdown_it import MarkdownIt
from mdit_py_plugins.anchors import anchors_plugin
from mdit_py_plugins.front_matter import front_matter_plugin
from mdit_py_plugins.tasklists import tasklists_plugin

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.config import Settings

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

DEFAULT_TITLE = "Untitled artifact"

#: Suffixes accepted as an explicit git entry file.
HTML_SUFFIXES: frozenset[str] = frozenset({".html", ".htm"})
MARKDOWN_SUFFIXES: frozenset[str] = frozenset({".md", ".markdown"})
ALLOWED_ENTRY_SUFFIXES: frozenset[str] = HTML_SUFFIXES | MARKDOWN_SUFFIXES

#: Default entry-file lookup order inside a repository directory (lowercased).
INDEX_CANDIDATES: tuple[str, ...] = ("index.html", "index.htm")
README_CANDIDATES: tuple[str, ...] = ("readme.md", "readme.markdown")

#: Anchors are generated for h1..h3 only — deeper headings rarely need links.
ANCHOR_MAX_LEVEL = 3

DEFAULT_MIME_TYPE = "application/octet-stream"

#: Username used when a git token is supplied without one. Works for GitHub
#: personal access tokens and GitLab deploy tokens alike.
DEFAULT_GIT_USERNAME = "x-access-token"

#: What a redacted secret is replaced with in anything shown to a caller.
REDACTED = "***"

#: Comments Claude's artifact viewer wraps its injected bootstrap script in. A
#: page saved from that viewer arrives with a ``<head>`` the author never wrote:
#: the opening comment, ~13 KB of inline script that sets
#: ``window.__FRAME_PREAMBLE``, the closing comment, and a style reset that
#: forces a cream background and a 14px system font — with the whole authored
#: document, its own ``<title>`` and ``<style>`` included, pushed down into
#: ``<body>``. That runtime only functions inside claude.ai, so republishing the
#: file as it stands hosts dead script *and* lets the reset fight the CSS the
#: author wrote. :func:`_unwrap_frame_runtime` rebuilds the authored document
#: instead. The opening comment identifies the wrapper; the closing one, when
#: present, says where the script ends.
FRAME_RUNTIME_MARKER = "<!-- frame-runtime -->"
FRAME_RUNTIME_END_MARKER = "<!-- /frame-runtime -->"

#: Schemes that are never inlined as data URIs.
EXTERNAL_URL_PREFIXES: tuple[str, ...] = (
    "http://",
    "https://",
    "data:",
    "//",
    "mailto:",
    "cid:",
)

# CDN dependencies for rendered Markdown pages.
#
# These are pinned to *exact* patch versions rather than a floating major
# (``@11``) so a rendered artifact always pulls the reviewed bytes and a silent
# upstream mutation cannot change the code that runs in it. Subresource
# Integrity (SRI) is not applied: it is impractical for the ESM ``import`` of
# mermaid (the module in turn imports further chunks the top-level hash cannot
# cover), and the *actual* security boundary for artifact pages is the sandboxed
# iframe the serving layer wraps them in (see the module docstring) — not SRI or
# a per-page CSP. Bump these deliberately and re-review when upgrading.
HLJS_VERSION = "11.9.0"
MERMAID_VERSION = "11.4.1"
HLJS_CSS_LIGHT = (
    f"https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@{HLJS_VERSION}"
    "/build/styles/github.min.css"
)
HLJS_CSS_DARK = (
    f"https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@{HLJS_VERSION}"
    "/build/styles/github-dark.min.css"
)
HLJS_JS = (
    f"https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@{HLJS_VERSION}"
    "/build/highlight.min.js"
)
MERMAID_ESM = (
    f"https://cdn.jsdelivr.net/npm/mermaid@{MERMAID_VERSION}"
    "/dist/mermaid.esm.min.mjs"
)

# Chart.js is not used by the Markdown template; design-system starters and
# style guides (src/designkit.py) load it when a bundle declares
# ``charts.library == "chart.js"``. Same exact-patch pinning, same deliberate
# no-SRI stance as above: the boundary is the sandbox those documents run in.
CHARTJS_VERSION = "4.4.1"
CHARTJS_JS = f"https://cdn.jsdelivr.net/npm/chart.js@{CHARTJS_VERSION}/dist/chart.umd.min.js"

#: Placeholders substituted into PAGE_TEMPLATE (plain replace — the template
#: contains CSS/JS braces, so str.format() is not usable here).
_TITLE_SLOT = "{{ARTIFACT_TITLE}}"
_BODY_SLOT = "{{ARTIFACT_BODY}}"

# --------------------------------------------------------------------------
# Regular expressions
# --------------------------------------------------------------------------

_TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_H1_TAG_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_FRONT_MATTER_TITLE_RE = re.compile(
    r"^[ \t]*title[ \t]*:[ \t]*(.+?)[ \t]*$", re.IGNORECASE | re.MULTILINE
)
_MD_H1_RE = re.compile(r"^[ \t]{0,3}#[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_IMG_SRC_RE = re.compile(
    r"(<img\b[^>]*?\bsrc\s*=\s*)(\"|')(.*?)\2", re.IGNORECASE | re.DOTALL
)
#: Strips ``user:password@`` from anything echoed back to the user.
_CREDENTIALS_RE = re.compile(r"//[^/@\s]*@")

#: Structural tags :func:`_unwrap_frame_runtime` locates. Matched as patterns
#: rather than by lowercasing and searching for a substring: ``str.lower()`` can
#: change a string's *length* (U+0130 lowercases to two characters), so offsets
#: taken from a lowercased copy are not offsets into the original, and ``<body``
#: as a substring also matches ``<bodyfoo``.
_HEAD_OPEN_RE = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_HEAD_CLOSE_RE = re.compile(r"</head\s*>", re.IGNORECASE)
_BODY_OPEN_RE = re.compile(r"<body\b[^>]*>", re.IGNORECASE)
_BODY_CLOSE_RE = re.compile(r"</body\s*>", re.IGNORECASE)

#: Head-level elements the authored fragment may open with — a saved Claude
#: artifact carries its ``<title>``, font ``<link>``s and ``<style>`` at the top
#: of the body fragment. Applied with :meth:`re.Pattern.match` at the fragment's
#: leading edge only, so no body content is ever hoisted into the head.
_LEADING_HEAD_ELEMENT_RE = re.compile(
    r"\s*(?:<title\b[^>]*>.*?</title>"
    r"|<style\b[^>]*>.*?</style>"
    r"|<link\b[^>]*/?>"
    r"|<meta\b[^>]*/?>"
    r"|<base\b[^>]*/?>)",
    re.IGNORECASE | re.DOTALL,
)


class BuildError(Exception):
    """Raised when the supplied publish input cannot be built.

    The message is user-facing and is surfaced by the API as HTTP 422.
    """


@dataclass(frozen=True)
class BuiltArtifact:
    """Result of a build: the final HTML plus provenance metadata."""

    html: str
    title: str
    source_type: str  # "html" | "markdown" | "git-html" | "git-markdown"
    git_commit: str | None = None


# --------------------------------------------------------------------------
# Page template
# --------------------------------------------------------------------------

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ARTIFACT_TITLE}}</title>
<link rel="stylesheet" href="__HLJS_CSS_LIGHT__" media="(prefers-color-scheme: light)">
<link rel="stylesheet" href="__HLJS_CSS_DARK__" media="(prefers-color-scheme: dark)">
<style>
:root {
  --bg: #ffffff;
  --fg: #1f2328;
  --muted: #656d76;
  --border: #d0d7de;
  --subtle-bg: #f6f8fa;
  --accent: #0969da;
  --quote-border: #d0d7de;
  --table-header-bg: #f6f8fa;
  --table-hover-bg: #f0f3f6;
  --shadow: rgba(31, 35, 40, 0.08);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117;
    --fg: #e6edf3;
    --muted: #9198a1;
    --border: #30363d;
    --subtle-bg: #161b22;
    --accent: #4493f8;
    --quote-border: #3d444d;
    --table-header-bg: #161b22;
    --table-hover-bg: #1c2128;
    --shadow: rgba(0, 0, 0, 0.4);
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  padding: 2.5rem 1.25rem 5rem;
  background: var(--bg);
  color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue",
    Arial, "Noto Sans", sans-serif, "Apple Color Emoji", "Segoe UI Emoji";
  font-size: 17px;
  line-height: 1.7;
  -moz-osx-font-smoothing: grayscale;
  -webkit-font-smoothing: antialiased;
}
main {
  max-width: 52rem;
  margin: 0 auto;
}
h1, h2, h3, h4, h5, h6 {
  line-height: 1.25;
  margin: 2.2rem 0 1rem;
  font-weight: 600;
}
h1 { font-size: 2.1rem; letter-spacing: -0.02em; }
h2 {
  font-size: 1.55rem;
  padding-bottom: 0.3rem;
  border-bottom: 1px solid var(--border);
}
h3 { font-size: 1.25rem; }
h4 { font-size: 1.05rem; }
h5, h6 { font-size: 1rem; color: var(--muted); }
h1:first-child, h2:first-child, h3:first-child { margin-top: 0; }
p { margin: 0 0 1.1rem; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
h1 a.header-anchor, h2 a.header-anchor, h3 a.header-anchor {
  color: var(--muted);
  opacity: 0;
  font-weight: 400;
  text-decoration: none;
}
h1:hover a.header-anchor, h2:hover a.header-anchor, h3:hover a.header-anchor {
  opacity: 1;
}
ul, ol { padding-left: 1.6rem; margin: 0 0 1.1rem; }
li { margin: 0.25rem 0; }
li > p { margin-bottom: 0.4rem; }
hr {
  border: none;
  border-top: 1px solid var(--border);
  margin: 2.5rem 0;
}
blockquote {
  margin: 0 0 1.1rem;
  padding: 0.1rem 1.1rem;
  border-left: 4px solid var(--quote-border);
  color: var(--muted);
}
blockquote > :last-child { margin-bottom: 0; }
code, kbd, samp {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
    "Liberation Mono", monospace;
  font-size: 0.88em;
}
:not(pre) > code {
  background: var(--subtle-bg);
  border: 1px solid var(--border);
  border-radius: 5px;
  padding: 0.12em 0.36em;
}
pre {
  background: var(--subtle-bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0.9rem 1rem;
  margin: 0 0 1.3rem;
  overflow-x: auto;
  line-height: 1.55;
}
pre code {
  background: none;
  border: none;
  padding: 0;
  display: block;
}
pre.mermaid {
  background: none;
  border: none;
  padding: 0.5rem 0;
  text-align: center;
  overflow-x: auto;
}
table {
  border-collapse: collapse;
  width: 100%;
  margin: 0 0 1.4rem;
  display: block;
  overflow-x: auto;
  font-size: 0.95em;
}
th, td {
  border: 1px solid var(--border);
  padding: 0.5rem 0.75rem;
  text-align: left;
  vertical-align: top;
}
thead th { background: var(--table-header-bg); font-weight: 600; }
tbody tr:hover { background: var(--table-hover-bg); }
img { max-width: 100%; height: auto; }
figure { margin: 0 0 1.3rem; }
.contains-task-list, ul.contains-task-list { list-style: none; padding-left: 0.4rem; }
.task-list-item { list-style: none; }
.task-list-item input[type="checkbox"] {
  margin: 0 0.5rem 0 0;
  vertical-align: middle;
  width: 0.95em;
  height: 0.95em;
  accent-color: var(--accent);
}
.footnotes { font-size: 0.9em; color: var(--muted); }
</style>
</head>
<body>
<main>
{{ARTIFACT_BODY}}
</main>
<script src="__HLJS_JS__"></script>
<script>
  if (window.hljs) { hljs.highlightAll(); }
</script>
<script type="module">
import mermaid from "__MERMAID_ESM__";
mermaid.initialize({startOnLoad: true, theme: window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "default"});
</script>
</body>
</html>
"""

PAGE_TEMPLATE = (
    PAGE_TEMPLATE.replace("__HLJS_CSS_LIGHT__", HLJS_CSS_LIGHT)
    .replace("__HLJS_CSS_DARK__", HLJS_CSS_DARK)
    .replace("__HLJS_JS__", HLJS_JS)
    .replace("__MERMAID_ESM__", MERMAID_ESM)
)


# --------------------------------------------------------------------------
# Markdown rendering
# --------------------------------------------------------------------------


def _linkify_available() -> bool:
    """Whether markdown-it's optional ``linkify-it-py`` backend is installed.

    Enabling the ``linkify`` rule without it raises at render time, so the rule
    is only switched on when the package is importable. The absence is logged
    loudly rather than passing silently.
    """
    try:
        import linkify_it  # noqa: F401
    except ImportError:
        logger.warning(
            "linkify-it-py is not installed; bare URLs in Markdown will not be "
            "auto-linked. Install markdown-it-py[linkify] to enable it."
        )
        return False
    return True


def _make_markdown() -> MarkdownIt:
    """Build the configured MarkdownIt instance used for every render."""
    linkify = _linkify_available()
    md = MarkdownIt(
        "commonmark",
        {"html": True, "linkify": linkify, "typographer": True},
    ).enable(["table", "strikethrough"] + (["linkify"] if linkify else []))
    md.use(front_matter_plugin)
    md.use(tasklists_plugin, enabled=True)
    md.use(anchors_plugin, max_level=ANCHOR_MAX_LEVEL)
    _install_mermaid_fence(md)
    return md


def _install_mermaid_fence(md: MarkdownIt) -> None:
    """Render ```mermaid fences as ``<pre class="mermaid">`` blocks.

    Every other fence keeps markdown-it's default rendering so that
    highlight.js still sees ``<pre><code class="language-X">``.
    """
    default_fence: Callable[..., str] | None = md.renderer.rules.get("fence")

    def fence(tokens: Any, idx: int, options: Any, env: Any, *args: Any) -> str:
        token = tokens[idx]
        info = token.info.strip().split(maxsplit=1)[0].lower() if token.info else ""
        if info == "mermaid":
            return f'<pre class="mermaid">{html_lib.escape(token.content)}</pre>\n'
        if default_fence is not None:
            return default_fence(tokens, idx, options, env, *args)
        return md.renderer.renderToken(tokens, idx, options, env)

    md.renderer.rules["fence"] = fence


def _render_markdown_body(md_text: str) -> str:
    """Render Markdown source into an HTML fragment."""
    return _make_markdown().render(md_text)


def _render_page(body_html: str, title: str) -> str:
    """Wrap a rendered fragment in the full standalone HTML page."""
    return PAGE_TEMPLATE.replace(_TITLE_SLOT, html_lib.escape(title)).replace(
        _BODY_SLOT, body_html
    )


# --------------------------------------------------------------------------
# Title extraction
# --------------------------------------------------------------------------


def _clean(value: str | None) -> str | None:
    """Collapse whitespace; return None for empty results."""
    if value is None:
        return None
    collapsed = " ".join(value.split())
    return collapsed or None


def _strip_tags(value: str) -> str:
    return html_lib.unescape(_TAG_RE.sub("", value))


def _title_from_html(html: str) -> str | None:
    match = _TITLE_TAG_RE.search(html)
    if match:
        found = _clean(html_lib.unescape(match.group(1)))
        if found:
            return found
    match = _H1_TAG_RE.search(html)
    if match:
        return _clean(_strip_tags(match.group(1)))
    return None


def _front_matter_block(md_text: str) -> str | None:
    match = _FRONT_MATTER_RE.match(md_text)
    return match.group(1) if match else None


def _title_from_front_matter(md_text: str) -> str | None:
    """Pull ``title:`` out of a leading YAML front-matter block.

    Deliberately a minimal regex parse — the project does not carry a YAML
    dependency just to read one field.
    """
    block = _front_matter_block(md_text)
    if block is None:
        return None
    match = _FRONT_MATTER_TITLE_RE.search(block)
    if not match:
        return None
    raw = match.group(1).strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1]
    return _clean(raw)


def _title_from_markdown(md_text: str) -> str | None:
    front_matter = _title_from_front_matter(md_text)
    if front_matter:
        return front_matter
    body = md_text
    block = _front_matter_block(md_text)
    if block is not None:
        body = md_text[_FRONT_MATTER_RE.match(md_text).end() :]  # type: ignore[union-attr]
    match = _MD_H1_RE.search(body)
    if match:
        return _clean(_strip_tags(match.group(1)))
    return None


# --------------------------------------------------------------------------
# Claude frame-runtime unwrapping
# --------------------------------------------------------------------------

#: The shell a rebuilt document is reassembled into: a real ``<head>`` carrying
#: the charset and viewport the wrapper used to supply, then whatever head-level
#: elements the authored fragment opened with, then the authored body. Kept as
#: fixed parts to concatenate rather than a template with placeholders (cf.
#: :data:`PAGE_TEMPLATE`), because what goes between them is author content and
#: must never be able to name a slot of ours.
_STANDALONE_OPEN = (
    "<!doctype html>\n"
    '<html lang="en">\n'
    "<head>\n"
    '<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
)
_STANDALONE_HEAD_CLOSE = "\n</head>\n<body>\n"
_STANDALONE_CLOSE = "\n</body>\n</html>\n"


def _unwrap_frame_runtime(html: str) -> tuple[str, bool]:
    """Rebuild a page saved from Claude's artifact viewer as a standalone one.

    Returns ``(html, rebuilt)``. Everything the wrapper injected — the
    bootstrap script and the style reset that fights the author's own CSS — is
    dropped, the head-level elements the authored fragment opens with are
    hoisted back into a real ``<head>``, and the rest becomes the body. See
    :data:`FRAME_RUNTIME_MARKER` for what the wrapper looks like.

    Fails safe in both directions, because this runs over *every* HTML publish:
    an input without the wrapper, one that only mentions the marker in its own
    prose, and one whose wrapper is truncated or otherwise malformed are all
    returned byte for byte, ``rebuilt`` False.
    """
    marker = html.find(FRAME_RUNTIME_MARKER)
    if marker == -1:
        return html, False

    # The marker has to sit inside an explicit <head> ... </head>, which is
    # where the wrapper puts it. A page that writes *about* the frame runtime
    # mentions it in prose instead, and rebuilding that throws away content its
    # author meant to keep. Anchoring to the enclosing head rather than merely
    # rejecting a </head> before the marker also covers the implicit-head
    # shape, where markup that was inert inside a script string would otherwise
    # be promoted to live body text.
    head_open = _HEAD_OPEN_RE.search(html)
    if head_open is None or head_open.end() > marker:
        return html, False
    if _HEAD_CLOSE_RE.search(html, head_open.end(), marker) is not None:
        return html, False

    # Look for the end of the injected head past the bootstrap script whenever
    # the closing comment says where the script ends: those ~13 KB are opaque
    # JavaScript, and a tag-shaped string inside them is not markup. A wrapper
    # without the closing comment falls back to scanning from the marker.
    end_marker = html.find(FRAME_RUNTIME_END_MARKER, marker)
    head_close = _HEAD_CLOSE_RE.search(html, end_marker if end_marker != -1 else marker)
    if head_close is None:
        return html, False
    body_open = _BODY_OPEN_RE.search(html, head_close.end())
    if body_open is None:
        return html, False

    fragment = html[body_open.end() :]
    body_closes = list(_BODY_CLOSE_RE.finditer(fragment))
    if body_closes:
        # The last one is the document's; an earlier one is inside author
        # content (a code sample, a script) and closes nothing.
        fragment = fragment[: body_closes[-1].start()]
    fragment = fragment.strip()
    if not fragment:
        return html, False

    head_parts: list[str] = []
    cursor = 0
    while (match := _LEADING_HEAD_ELEMENT_RE.match(fragment, cursor)) is not None:
        head_parts.append(match.group(0).strip())
        cursor = match.end()

    head = "\n".join(head_parts)
    body = fragment[cursor:].strip()
    return (
        _STANDALONE_OPEN + head + _STANDALONE_HEAD_CLOSE + body + _STANDALONE_CLOSE,
        True,
    )


def _standalone_html(html: str, title: str | None) -> tuple[str, str]:
    """Rebuild a saved Claude artifact and resolve its title. ``(html, title)``.

    Everything an HTML publish does to its input, kept in one place because
    there are two ways in: a raw ``html`` body (:func:`build_from_html`) and an
    HTML entry file in a cloned repository (:func:`build_from_git`). The same
    bytes have to yield the same document and the same title whichever door
    they come through — a wrapper stripped on one path only is a wrapper the
    other path still publishes.
    """
    document, rebuilt = _unwrap_frame_runtime(html)
    if rebuilt:
        logger.info(
            "Rebuilt a saved Claude artifact as a standalone document "
            "(%d -> %d characters)",
            len(html),
            len(document),
        )
    # Deliberately after the rebuild, never before it: in a wrapped page the
    # authored <title> sits in the body, behind ~13 KB of bootstrap script that
    # any title-shaped string inside would win the search from.
    resolved = _clean(title) or _title_from_html(document) or DEFAULT_TITLE
    return document, resolved


# --------------------------------------------------------------------------
# Public builders: html / markdown
# --------------------------------------------------------------------------


def build_from_html(html: str, title: str | None = None) -> BuiltArtifact:
    """Serve raw HTML as-is, deriving a title when none was supplied.

    "As-is" has one exception: a page saved from Claude's artifact viewer is
    rebuilt as a standalone document first (:func:`_unwrap_frame_runtime`).
    Every other input is published byte for byte.

    Title precedence: explicit argument, ``<title>``, first ``<h1>``, then
    :data:`DEFAULT_TITLE`.
    """
    document, resolved = _standalone_html(html, title)
    logger.debug("Built html artifact (title=%r, %d bytes)", resolved, len(document))
    return BuiltArtifact(html=document, title=resolved, source_type="html")


def build_from_markdown(md: str, title: str | None = None) -> BuiltArtifact:
    """Render Markdown into a full standalone HTML page.

    Title precedence: explicit argument, front-matter ``title:``, first
    ``# heading``, then :data:`DEFAULT_TITLE`.
    """
    resolved = _clean(title) or _title_from_markdown(md) or DEFAULT_TITLE
    body = _render_markdown_body(md)
    page = _render_page(body, resolved)
    logger.debug("Built markdown artifact (title=%r, %d bytes)", resolved, len(page))
    return BuiltArtifact(html=page, title=resolved, source_type="markdown")


# --------------------------------------------------------------------------
# Git helpers
# --------------------------------------------------------------------------


def _scrub(text: str, secret: str | None = None) -> str:
    """Remove any embedded credentials before echoing text back to a user.

    Two independent passes, deliberately overlapping:

    1. the ``//user:pass@host`` form is rewritten to ``//host`` — this catches
       credentials in any URL git echoes back, whatever the secret was;
    2. when ``secret`` is given, both its literal and its percent-encoded form
       are replaced with :data:`REDACTED`, so a token that leaked in some shape
       the regex does not model still never reaches the caller.
    """
    cleaned = _CREDENTIALS_RE.sub("//", text)
    if secret:
        for form in (secret, quote(secret, safe="")):
            cleaned = cleaned.replace(form, REDACTED)
    return cleaned


def _last_line(text: str, secret: str | None = None) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return _scrub(lines[-1], secret) if lines else ""


def _authed_clone_url(git_url: str, username: str | None, token: str) -> str:
    """Return ``git_url`` with credentials embedded in its userinfo.

    The clone itself no longer uses this form — the token is delivered via
    ``GIT_ASKPASS`` so it never enters ``argv`` or the URL (see :func:`_clone`).
    This helper is retained to exercise :func:`_scrub`'s defence-in-depth: if a
    credentialed URL ever surfaces (e.g. echoed by git from ``.git/config`` or a
    redirect), the scrubber must still strip it before it reaches a log or an
    error. Both parts are percent-encoded with ``safe=""`` so a token containing
    ``@``, ``:`` or ``/`` cannot break out of the userinfo section; any userinfo
    already present in ``git_url`` is replaced.
    """
    parsed = urlparse(git_url)
    host = parsed.netloc.rsplit("@", 1)[-1]
    user = quote(username or DEFAULT_GIT_USERNAME, safe="")
    secret = quote(token, safe="")
    return urlunparse(parsed._replace(netloc=f"{user}:{secret}@{host}"))


def _validate_git_url(git_url: str) -> str:
    """Accept only https URLs with a hostname."""
    try:
        parsed = urlparse(git_url.strip())
    except ValueError as exc:  # pragma: no cover - urlparse rarely raises
        raise BuildError(f"Invalid git URL: {exc}") from exc
    if parsed.scheme != "https" or not parsed.hostname:
        raise BuildError(
            "git_url must be an https URL with a hostname, "
            "for example https://github.com/owner/repo.git"
        )
    return git_url.strip()


#: Host names that must never be cloned — cloud metadata / internal service
#: names. ``*.internal`` (GCP/AWS internal DNS) is matched by suffix separately.
_BLOCKED_HOSTNAMES: frozenset[str] = frozenset(
    {"metadata", "metadata.google.internal", "localhost"}
)


def _resolve_host_ips(hostname: str) -> list[str]:
    """Resolve ``hostname`` to the list of IP strings it maps to.

    Isolated so tests can inject a fake resolver instead of doing real DNS.
    """
    infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


def _ip_is_blocked(ip: str) -> bool:
    """True when ``ip`` is private, loopback, link-local, reserved, etc."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # unparseable — refuse rather than risk it
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def _check_git_host(
    git_url: str,
    *,
    allow_private: bool,
    resolver: Callable[[str], list[str]] | None = None,
) -> None:
    """SSRF guard: reject a git_url whose host is internal or resolves private.

    Applied *before* any clone. Literal-IP hosts in a blocked range and host
    names that resolve to one (private/loopback/link-local/ULA/reserved, plus
    cloud-metadata endpoints such as 169.254.169.254 and ``*.internal``) are
    refused unless ``allow_private`` is set (self-hosting opt-in). This checks
    the addresses at validation time; git re-resolves at clone time, so a
    determined DNS-rebinding attacker retains a narrow TOCTOU window — the
    residual risk accepted for this network-local guard.
    """
    if allow_private:
        return
    if resolver is None:
        resolver = _resolve_host_ips
    hostname = (urlparse(git_url).hostname or "").strip().rstrip(".").lower()
    if not hostname:
        raise BuildError("git_url must have a hostname.")
    if hostname in _BLOCKED_HOSTNAMES or hostname.endswith(".internal"):
        raise BuildError(
            "git_url host is not permitted: internal/metadata hostnames are "
            "blocked."
        )
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        candidates = [hostname]
    else:
        try:
            candidates = resolver(hostname)
        except OSError as exc:
            raise BuildError(
                f"Could not resolve git_url host {hostname!r}."
            ) from exc
        if not candidates:
            raise BuildError(f"Could not resolve git_url host {hostname!r}.")
    for ip in candidates:
        if _ip_is_blocked(ip):
            raise BuildError(
                "git_url resolves to a private, loopback, link-local, or "
                "reserved address, which is not allowed. Set "
                "HUB_GIT_ALLOW_PRIVATE_HOSTS=1 for trusted self-hosting."
            )


#: Environment variables git may read that we deliberately forward. Everything
#: else in ``os.environ`` (notably the app's own HUB_* secrets) is withheld so a
#: git config/credential helper or any subprocess git spawns cannot read it.
_GIT_ENV_PASSTHROUGH: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL")

#: askpass helper: git calls this once for the username and once for the
#: password. It returns the value from the (curated) environment so the token
#: is never placed in ``argv``. ``$1`` is the human prompt ("Username for ...",
#: "Password for ...").
_ASKPASS_SCRIPT = (
    "#!/bin/sh\n"
    'case "$1" in\n'
    '  Username*|username*) printf %s "$GIT_ASKPASS_USER" ;;\n'
    '  *) printf %s "$GIT_ASKPASS_PASS" ;;\n'
    "esac\n"
)


def _git_base_env() -> dict[str, str]:
    """Minimal, curated environment for every git subprocess.

    Only a handful of harmless vars are forwarded (never the app's HUB_*
    secrets). ``GIT_TERMINAL_PROMPT=0`` makes git fail immediately on private
    or missing repositories instead of blocking on an interactive prompt.
    """
    env = {"GIT_TERMINAL_PROMPT": "0"}
    for key in _GIT_ENV_PASSTHROUGH:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    return env


def _run_git(
    args: list[str],
    timeout_s: int,
    *,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a git command, capturing output; never raises on non-zero exit.

    The subprocess only ever sees :func:`_git_base_env` plus the optional
    ``env_extra`` (the transient git-auth vars) — the full process environment
    is deliberately *not* inherited.
    """
    env = _git_base_env()
    if env_extra:
        env.update(env_extra)
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            args,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env=env,
        )
    except FileNotFoundError as exc:
        raise BuildError("git is not available on this server.") from exc
    except subprocess.TimeoutExpired as exc:
        raise BuildError(
            f"git operation timed out after {timeout_s}s. "
            "Try a smaller repository or a specific branch."
        ) from exc


#: Lengths of a git object id in hex: SHA-1 and SHA-256 respectively. A ref of
#: exactly one of these lengths and entirely hexadecimal is an object id, not a
#: branch someone happened to name that way.
_OBJECT_ID_LENGTHS = (40, 64)


def _validate_git_ref(ref: str | None) -> str | None:
    """Return ``ref`` unchanged, or raise if it is a commit id.

    The clone runs ``git clone --depth 1 --branch <ref>``, and ``--branch``
    resolves a branch or a tag only -- an object id there fails inside git.
    The public contract used to say "branch, tag or commit", so a caller
    pinning an immutable source got an opaque subprocess failure rather than
    an answer about what it asked for. The contract now says branch or tag,
    and this turns the one case it excludes into a message that says which
    ref was rejected and why.

    Only a full object id is refused. A shorter hexadecimal string is a
    perfectly ordinary branch name, and refusing those would reject valid
    input to describe an unsupported feature.
    """
    if not ref:
        return ref
    candidate = ref.strip()
    if len(candidate) in _OBJECT_ID_LENGTHS and all(
        character in "0123456789abcdefABCDEF" for character in candidate
    ):
        raise BuildError(
            f"git_ref {ref!r} looks like a commit id; this service checks out "
            "a branch or a tag. Use a tag to pin an immutable source."
        )
    return ref


def _clone(
    git_url: str,
    ref: str | None,
    dest: Path,
    timeout_s: int,
    *,
    username: str | None = None,
    token: str | None = None,
    blob_limit_bytes: int | None = None,
) -> None:
    """Shallow-clone ``git_url`` (optionally at ``ref``) into ``dest``.

    ``git_url`` is always the *clean*, unauthenticated URL — it is what lands in
    ``argv`` and in every message raised from here. When ``token`` is set the
    credential is delivered out-of-band through a short-lived ``GIT_ASKPASS``
    helper (:data:`_ASKPASS_SCRIPT`) reading a curated environment, so the token
    never appears in ``argv``, process listings, or the persisted URL.
    """
    # SEC-100-005: disable git's default of following an HTTP redirect. Without
    # this, `_check_git_host` validates only the *original* hostname and git
    # itself would silently follow a redirect to an unvalidated destination
    # (including a private/internal address) during the clone -- the SSRF
    # guard would never see the real target. The `-c` override must precede
    # the subcommand to apply as a one-off config for this invocation only.
    args = ["git", "-c", "http.followRedirects=false", "clone", "--depth", "1"]
    if blob_limit_bytes and blob_limit_bytes > 0:
        # Partial clone: ask the server to withhold blobs larger than the hard
        # repo cap. Servers without partial-clone support merely warn and send
        # everything, so this never breaks a clone. It only reduces blast
        # radius — git still materialises HEAD blobs during checkout, and the
        # whole shallow tree lands on disk before the size check runs. Residual
        # risk is therefore bounded by the clone *timeout* (the only true
        # during-download limit) and caught by the post-clone size check, which
        # now includes the .git directory (see ``_repo_size_bytes``).
        args.append(f"--filter=blob:limit={blob_limit_bytes}")
    if ref:
        args += ["--branch", ref]
    args += [git_url, str(dest)]

    if token:
        # The token lives only in a curated env handed to a 0700 askpass helper,
        # torn down in the ``with`` finally. It is never an argv element.
        with tempfile.TemporaryDirectory(prefix="artifact-askpass-") as helper_dir:
            askpass = Path(helper_dir) / "askpass.sh"
            askpass.write_text(_ASKPASS_SCRIPT, encoding="utf-8")
            askpass.chmod(0o700)
            env_extra = {
                "GIT_ASKPASS": str(askpass),
                "GIT_ASKPASS_USER": username or DEFAULT_GIT_USERNAME,
                "GIT_ASKPASS_PASS": token,
            }
            result = _run_git(args, timeout_s, env_extra=env_extra)
    else:
        result = _run_git(args, timeout_s)
    if result.returncode == 0:
        return
    reason = _last_line(result.stderr, token) or _last_line(result.stdout, token)
    lowered = (result.stderr or "").lower()
    detail = f" git said: {reason}" if reason else ""
    if "could not read username" in lowered or "authentication failed" in lowered:
        hint = (
            "The supplied git_token was rejected, or git_username is wrong for "
            "this host."
            if token
            else "Pass git_token (and optionally git_username) to publish from "
            "a private repository."
        )
        raise BuildError(
            f"Could not clone the repository: it is private or does not exist. {hint}"
        )
    if re.search(r"returned error: 3\d\d", lowered):
        # SEC-100-005: with -c http.followRedirects=false above, git turns a
        # blocked redirect into an ordinary HTTP-status failure -- git's own
        # stderr never uses the word "redirect" -- so this is the case where
        # the remote tried to send the clone somewhere _check_git_host never
        # validated. Name the real cause instead of falling through to the
        # generic message below; `reason`/`detail` are already scrubbed via
        # `_last_line` -> `_scrub`, so no credential or internal detail leaks.
        raise BuildError(
            "Could not clone the repository: the remote responded with an "
            f"HTTP redirect, which this service does not follow for security "
            f"reasons.{detail}"
        )
    if ref:
        raise BuildError(
            f"Could not clone the repository at ref {ref!r}. Only branch and tag "
            f"names are supported (not commit SHAs).{detail}"
        )
    raise BuildError(f"Could not clone the repository.{detail}")


def _repo_size_bytes(root: Path) -> int:
    """Sum file sizes under ``root``, *including* the ``.git`` directory.

    The ``.git`` pack is counted deliberately: it is where a shallow clone's
    downloaded objects land, so counting it turns the post-clone check into a
    real hard stop against a repository with a huge history/pack — not just a
    huge working tree. Symlinks are never followed (their tiny link size is
    ignored); containment of the entry/image files is enforced elsewhere.
    """
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames:
            candidate = Path(dirpath) / name
            try:
                if candidate.is_symlink():
                    continue
                total += candidate.stat().st_size
            except OSError:  # pragma: no cover - broken entries are ignored
                continue
    return total


def _head_commit(root: Path, timeout_s: int, secret: str | None = None) -> str | None:
    result = _run_git(["git", "-C", str(root), "rev-parse", "HEAD"], timeout_s)
    if result.returncode != 0:
        # ``secret`` is passed through purely defensively: rev-parse never
        # touches the remote, but a cloned .git/config does hold the authed
        # remote URL, so nothing derived from git output is logged unscrubbed.
        logger.warning("rev-parse HEAD failed: %s", _last_line(result.stderr, secret))
        return None
    return result.stdout.strip() or None


def _is_inside(root: Path, candidate: Path) -> bool:
    """True when ``candidate`` really lives under ``root`` (symlinks resolved)."""
    root_real = os.path.realpath(root)
    candidate_real = os.path.realpath(candidate)
    return candidate_real == root_real or candidate_real.startswith(
        root_real + os.sep
    )


def _find_named(directory: Path, names: tuple[str, ...]) -> Path | None:
    """Case-insensitive lookup of the first matching file name."""
    try:
        entries = sorted(directory.iterdir(), key=lambda p: p.name)
    except OSError:
        return None
    lowered = {entry.name.lower(): entry for entry in entries if entry.is_file()}
    for name in names:
        found = lowered.get(name)
        if found is not None:
            return found
    return None


def _default_entry(directory: Path, where: str) -> Path:
    """Apply the default entry-file rules inside ``directory``."""
    found = _find_named(directory, INDEX_CANDIDATES)
    if found is not None:
        return found
    found = _find_named(directory, README_CANDIDATES)
    if found is not None:
        return found
    html_files = sorted(
        (
            entry
            for entry in directory.iterdir()
            if entry.is_file() and entry.suffix.lower() in HTML_SUFFIXES
        ),
        key=lambda p: p.name,
    )
    if len(html_files) == 1:
        return html_files[0]
    raise BuildError(
        f"No entry file found in {where}. Tried index.html, README.md, and a "
        "single *.html file"
        + (
            f" (found {len(html_files)} .html files — set git_path to pick one)."
            if html_files
            else ". Set git_path to point at the file to publish."
        )
    )


def _resolve_entry(repo_root: Path, path: str | None) -> Path:
    """Pick the file to build from, honouring an explicit ``git_path``.

    Whatever branch is taken (explicit path, an explicit directory's default,
    or the repository-root default), the final entry is verified to resolve
    *inside* ``repo_root`` with symlinks followed — a repo shipping an
    ``index.html`` / ``README.md`` symlink that points at ``/etc/passwd`` is
    rejected rather than read.
    """
    entry = _pick_entry(repo_root, path)
    if not _is_inside(repo_root, entry):
        raise BuildError(
            "The selected entry file resolves outside the repository "
            "(symlink escape) and cannot be published."
        )
    return entry


def _pick_entry(repo_root: Path, path: str | None) -> Path:
    """Select the entry file without the final containment check."""
    if not path:
        return _default_entry(repo_root, "the repository root")

    cleaned = path.strip().lstrip("/")
    if not cleaned:
        return _default_entry(repo_root, "the repository root")

    candidate = repo_root / cleaned
    if not _is_inside(repo_root, candidate):
        raise BuildError(f"git_path {path!r} points outside the repository.")
    if not candidate.exists():
        raise BuildError(f"git_path {path!r} does not exist in the repository.")
    if candidate.is_dir():
        return _default_entry(candidate, f"directory {cleaned!r}")
    if candidate.suffix.lower() not in ALLOWED_ENTRY_SUFFIXES:
        raise BuildError(
            f"git_path {path!r} must point at an .html, .htm, .md or .markdown "
            "file."
        )
    return candidate


def _read_text(entry: Path) -> str:
    try:
        return entry.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise BuildError(f"Could not read {entry.name}: {exc.strerror}") from exc


# --------------------------------------------------------------------------
# Image inlining
# --------------------------------------------------------------------------


def _inline_images(
    html: str,
    base_dir: Path,
    repo_root: Path,
    max_image_bytes: int,
    max_total_bytes: int,
) -> str:
    """Replace relative ``<img src=...>`` references with ``data:`` URIs.

    References that are absolute URLs, missing, outside the repository, or that
    would breach a size cap are left untouched — inlining is best-effort and
    never fails the build.
    """
    budget = {"used": 0}

    def replace(match: re.Match[str]) -> str:
        prefix, quote, src = match.group(1), match.group(2), match.group(3)
        raw = src.strip()
        if not raw or raw.lower().startswith(EXTERNAL_URL_PREFIXES):
            return match.group(0)

        relative = unquote(raw.split("#", 1)[0].split("?", 1)[0])
        if not relative:
            return match.group(0)

        target = (
            (repo_root / relative.lstrip("/"))
            if relative.startswith("/")
            else (base_dir / relative)
        )
        if not _is_inside(repo_root, target):
            logger.debug("Skipping image outside repository: %r", relative)
            return match.group(0)
        if not target.is_file():
            return match.group(0)

        try:
            size = target.stat().st_size
        except OSError:
            return match.group(0)
        if size > max_image_bytes:
            logger.info("Image %r exceeds per-image cap, left as link", relative)
            return match.group(0)
        if budget["used"] + size > max_total_bytes:
            logger.info("Inline image budget exhausted, %r left as link", relative)
            return match.group(0)

        try:
            payload = target.read_bytes()
        except OSError:
            return match.group(0)

        mime = mimetypes.guess_type(target.name)[0] or DEFAULT_MIME_TYPE
        encoded = base64.b64encode(payload).decode("ascii")
        budget["used"] += size
        return f"{prefix}{quote}data:{mime};base64,{encoded}{quote}"

    return _IMG_SRC_RE.sub(replace, html)


# --------------------------------------------------------------------------
# Public builder: git
# --------------------------------------------------------------------------


def build_from_git(
    git_url: str,
    ref: str | None,
    path: str | None,
    title: str | None,
    settings: "Settings",
    *,
    git_username: str | None = None,
    git_token: str | None = None,
) -> BuiltArtifact:
    """Shallow-clone an https repository and build its entry document.

    Entry selection: explicit ``path`` (file or directory), otherwise
    ``index.html`` → ``README.md`` → a single root-level ``*.html``. Markdown
    entries go through the same rendering as :func:`build_from_markdown`; HTML
    entries go through the same near pass-through as :func:`build_from_html`,
    a page saved from Claude's artifact viewer rebuilt as a standalone document
    included, and are otherwise used verbatim. Relative images are inlined as
    ``data:`` URIs afterwards.

    ``git_token`` (with the optional ``git_username``, defaulting to
    :data:`DEFAULT_GIT_USERNAME`) makes private repositories reachable. It is
    used for the clone subprocess only: the unauthenticated ``git_url`` is what
    is logged and reported, and it is the caller's job never to persist the
    token.
    """
    url = _validate_git_url(git_url)
    ref = _validate_git_ref(ref)
    _check_git_host(url, allow_private=settings.git_allow_private_hosts)

    with tempfile.TemporaryDirectory(prefix="artifact-git-") as tmp:
        dest = Path(tmp) / "repo"
        logger.info(
            "Cloning %s (ref=%r, authenticated=%s)", _scrub(url), ref, bool(git_token)
        )
        # Re-resolve and re-validate immediately before the clone subprocess to
        # narrow the DNS-rebinding TOCTOU window: the host passed _check_git_host
        # above, but DNS can rebind to an internal/metadata address between that
        # check and git's own resolution. This shrinks the window to the gap
        # between here and git's connect; it cannot fully close it, because git
        # (libcurl) resolves the hostname independently and true IP pinning is
        # out of scope. Any blocked address here rejects with BuildError.
        #
        # SEC-075-006 (accepted residual risk): this hostname re-validation is
        # a best-effort application-layer check, not a guarantee. It cannot be
        # closed here with the current client (git/libcurl resolves on its own
        # at connect time, after this function returns), and the same gap
        # applies to outbound webhook delivery (SEC-075-005, see
        # src/webhooks.py). The reliable control is operational: an egress
        # policy/proxy in front of this container that denies loopback,
        # RFC1918/ULA, link-local, cluster and cloud-metadata ranges outright,
        # regardless of what hostname validation concluded. See the "Network
        # egress" section of README.md for the operator-facing statement of
        # this control.
        _check_git_host(url, allow_private=settings.git_allow_private_hosts)
        _clone(
            url,
            ref,
            dest,
            settings.git_clone_timeout_s,
            username=git_username,
            token=git_token,
            blob_limit_bytes=settings.git_max_repo_bytes,
        )

        size = _repo_size_bytes(dest)
        if size > settings.git_max_repo_bytes:
            raise BuildError(
                f"Repository is too large ({size // (1024 * 1024)} MB); the limit "
                f"is {settings.git_max_repo_bytes // (1024 * 1024)} MB."
            )

        commit = _head_commit(dest, settings.git_clone_timeout_s, git_token)
        entry = _resolve_entry(dest, path)
        suffix = entry.suffix.lower()
        source = _read_text(entry)

        if suffix in MARKDOWN_SUFFIXES:
            resolved = _clean(title) or _title_from_markdown(source) or DEFAULT_TITLE
            page = _render_page(_render_markdown_body(source), resolved)
            source_type = "git-markdown"
        else:
            # Exactly what a raw `html` publish of these bytes would produce,
            # the saved-artifact rebuild included: an entry file committed to a
            # repository is the same document, and which door it arrived
            # through is not a reason to store it differently.
            page, resolved = _standalone_html(source, title)
            source_type = "git-html"

        # Strictly after the entry has been turned into a document. A Markdown
        # entry has no `src` attributes to rewrite until it is rendered, and an
        # HTML one must be rebuilt first: a wrapper's injected head is ~13 KB of
        # opaque bootstrap script, so an <img>-shaped string inside it is not
        # markup to resolve against the repository, and every byte of the inline
        # budget spent on content the rebuild then discards is a byte the
        # author's own images no longer have. The reverse order needs no
        # defending in turn — a data: URI is base64, which contains no `<`, so
        # inlining can never manufacture the structural tags the rebuild looks
        # for.
        page = _inline_images(
            page,
            base_dir=entry.parent,
            repo_root=dest,
            max_image_bytes=settings.max_inline_image_bytes,
            max_total_bytes=settings.max_inline_total_bytes,
        )

    encoded_size = len(page.encode("utf-8"))
    if encoded_size > settings.max_html_bytes:
        raise BuildError(
            f"Built HTML is too large ({encoded_size // (1024 * 1024)} MB); the "
            f"limit is {settings.max_html_bytes // (1024 * 1024)} MB."
        )

    logger.info(
        "Built %s artifact from %s (entry=%s, commit=%s, %d bytes)",
        source_type,
        _scrub(url),
        entry.name,
        commit,
        encoded_size,
    )
    return BuiltArtifact(
        html=page,
        title=resolved,
        source_type=source_type,
        git_commit=commit,
    )
