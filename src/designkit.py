"""Documents derived from a design-system bundle: the starter skeleton and the
style-guide page body. Pure functions of (bundle, token sets); no I/O.

Both outputs are *user content*: they are only ever served through
``main._sandboxed_html`` or inside the sandboxed ``srcdoc`` iframe. Nothing
here is safe to place on the hub's own origin.

This module relies on upstream validation (``src.designs.validate_bundle``,
Track 2) for the shape and safety of ``bundle["tokens"]``, ``bundle["roles"]``
and friends — it does not re-validate those. The one thing it *does* check
itself, defensively, is the boundary of the single unescaped ``<style>``
block ``starter_html`` builds: component ``css`` (and the tokens/role CSS,
for good measure) is scanned for a case-insensitive ``</style`` before being
spliced in, and refused with ``ValueError`` if found, even though the
upstream validator is expected to reject such a component at submit time
(422) already — belt and suspenders around the one place raw text is placed
inside an unescaped HTML block.
"""
from __future__ import annotations

import html
import json
import re
from urllib.parse import urlsplit

from src.tokens import TokenSet, alias_target, concrete_value, to_css, variable_name

TITLE_SLOT = "{{TITLE}}"
BODY_SLOT = "{{BODY}}"

#: Case-insensitive, whitespace-tolerant match for a closing </style> tag —
#: the one sequence that would let component/role/token CSS break out of the
#: starter's single unescaped <style> block.
_STYLE_BREAKOUT_RE = re.compile(r"</\s*style", re.IGNORECASE)


def _refuse_style_breakout(css_text: str, *, where: str) -> None:
    if _STYLE_BREAKOUT_RE.search(css_text):
        raise ValueError(f"component css may not contain '</style' ({where})")

_ROLE_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("background", "text", "font_body"),
        "body{{background:var({background});color:var({text});font-family:var({font_body})}}",
    ),
    (("font_heading",), "h1,h2,h3,h4,h5,h6{{font-family:var({font_heading})}}"),
    (("font_mono",), "code,pre,kbd{{font-family:var({font_mono})}}"),
    (("accent",), "a{{color:var({accent})}}"),
    (
        ("surface", "border", "radius"),
        ".ds-surface{{background:var({surface});border:1px solid var({border});border-radius:var({radius})}}",
    ),
    (("muted",), ".ds-muted{{color:var({muted})}}"),
)

#: The one role whose value is a list rather than a single alias, and whose
#: variables are therefore numbered (``--ds-chart-1`` …) rather than named.
PALETTE_ROLE = "chart_palette"
#: The stable prefix every role alias carries. Token variable names differ
#: from system to system (``--color-bg`` here, ``--color-page`` there); these
#: do not, which is what lets one document be re-skinned by pointing at
#: another system's ``/ds/{id}/css``.
ROLE_VAR_PREFIX = "--ds-"


def role_variable_names(bundle: dict) -> dict[str, str]:
    """Map each declared role to the stable ``--ds-*`` variable it is aliased to.

    Scalar roles keep their own name with ``_`` turned into ``-``
    (``font_body`` -> ``--ds-font-body``). ``chart_palette`` expands to one
    numbered entry per colour (``chart_palette_1`` -> ``--ds-chart-1``) plus
    ``chart_palette_count`` -> ``--ds-chart-count``, so a template can read
    how many there are without counting. Roles the bundle does not declare
    are absent: nothing here is invented.
    """
    names: dict[str, str] = {}
    for role, ref in (bundle.get("roles") or {}).items():
        if role == PALETTE_ROLE or isinstance(ref, list):
            entries = ref if isinstance(ref, list) else []
            for index in range(1, len(entries) + 1):
                names[f"{role}_{index}"] = f"{ROLE_VAR_PREFIX}chart-{index}"
            if entries:
                names[f"{role}_count"] = f"{ROLE_VAR_PREFIX}chart-count"
        else:
            names[role] = ROLE_VAR_PREFIX + role.replace("_", "-")
    return names


def role_css_vars(bundle: dict, base: TokenSet) -> str:
    """One ``:root`` block aliasing every declared role to its token variable.

    Each alias is ``var(<target>)`` rather than a concrete value, so it keeps
    following the mode: when the dark block redefines ``--color-bg``,
    ``--ds-background`` follows without a second alias block. Returns ``""``
    when the bundle declares no roles, so no empty rule is emitted.
    """
    roles = bundle.get("roles") or {}
    pairs: list[str] = []
    for role, ref in roles.items():
        if role == PALETTE_ROLE or isinstance(ref, list):
            entries = ref if isinstance(ref, list) else []
            for index, item in enumerate(entries, start=1):
                pairs.append(f"{ROLE_VAR_PREFIX}chart-{index}:var({_var(base, item)})")
            if entries:
                pairs.append(f"{ROLE_VAR_PREFIX}chart-count:{len(entries)}")
        else:
            name = ROLE_VAR_PREFIX + role.replace("_", "-")
            pairs.append(f"{name}:var({_var(base, ref)})")
    if not pairs:
        return ""
    return ":root{" + ";".join(pairs) + "}"


def _var(ts: TokenSet, alias: str) -> str:
    return variable_name(alias_target(alias))


def role_values(bundle: dict, ts: TokenSet) -> dict[str, str | list[str]]:
    out: dict[str, str | list[str]] = {}
    for role, ref in (bundle.get("roles") or {}).items():
        if isinstance(ref, list):
            out[role] = [concrete_value(ts, alias_target(r)) for r in ref]
        else:
            out[role] = concrete_value(ts, alias_target(ref))
    return out


def _role_css(bundle: dict, base: TokenSet) -> str:
    """The starter's own role rules, written against the ``--ds-*`` aliases.

    They deliberately do *not* name this system's token variables: a document
    built from this starter re-skins by swapping in another system's CSS,
    which only works while every rule goes through the stable alias layer
    :func:`role_css_vars` defines.
    """
    roles = bundle.get("roles") or {}
    names = role_variable_names(bundle)
    rules = []
    for needed, template in _ROLE_RULES:
        if all(r in roles for r in needed):
            rules.append(template.format(**{r: names[r] for r in needed}))
    return "\n".join(rules)


def _font_links(bundle: dict) -> str:
    hosts: list[str] = []
    lines = []
    for item in bundle.get("fonts") or []:
        host = urlsplit(item["href"]).hostname or ""
        if host and host not in hosts:
            hosts.append(host)
    for host in hosts:
        lines.append(f'<link rel="preconnect" href="https://{html.escape(host, quote=True)}">')
    for item in bundle.get("fonts") or []:
        lines.append(f'<link rel="stylesheet" href="{html.escape(item["href"], quote=True)}">')
    return "\n".join(lines)


def _chart_script(bundle: dict, base: TokenSet, dark: TokenSet | None, url: str) -> str:
    light = role_values(bundle, base)
    darkv = role_values(bundle, dark) if dark is not None else light

    def pick(d, key, default):
        return d.get(key, default)

    cfg = {
        "light": {
            "font": pick(light, "font_body", ""),
            "color": pick(light, "text", ""),
            "border": pick(light, "border", ""),
            "palette": pick(light, "chart_palette", []),
        },
        "dark": {
            "font": pick(darkv, "font_body", ""),
            "color": pick(darkv, "text", ""),
            "border": pick(darkv, "border", ""),
            "palette": pick(darkv, "chart_palette", []),
        },
    }
    light_json, dark_json = json.dumps(cfg["light"]), json.dumps(cfg["dark"])
    palette_json = json.dumps(cfg["dark"]["palette"])
    light_palette_json = json.dumps(cfg["light"]["palette"])
    return (
        f'<script src="{html.escape(url, quote=True)}"></script>\n'
        "<script>\n(function(){\n"
        '  var dark = window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches;\n'
        f"  var L = {light_json}, D = {dark_json}; var c = dark ? D : L;\n"
        "  if (window.Chart) {\n"
        "    if (c.font) Chart.defaults.font.family = c.font;\n"
        "    if (c.color) Chart.defaults.color = c.color;\n"
        "    if (c.border) Chart.defaults.borderColor = c.border;\n"
        "  }\n"
        f"  window.DS_PALETTE = (dark ? {palette_json} : {light_palette_json});\n"
        "})();\n</script>"
    )


_MERMAID_MAP = (
    ("primaryColor", "accent"),
    ("primaryTextColor", "on_accent"),
    ("lineColor", "border"),
    ("background", "background"),
    ("fontFamily", "font_body"),
)


def _mermaid_script(bundle: dict, base: TokenSet, dark: TokenSet | None, url: str) -> str:
    def theme(ts):
        vals = role_values(bundle, ts)
        return {k: vals[r] for k, r in _MERMAID_MAP if r in vals}

    light_json = json.dumps(theme(base), indent=2)
    dark_json = json.dumps(theme(dark) if dark is not None else theme(base), indent=2)
    return (
        '<script type="module">\n'
        f'import mermaid from "{html.escape(url, quote=True)}";\n'
        'const dark = window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches;\n'
        f"const themeVariables = dark ? {dark_json} : {light_json};\n"
        'mermaid.initialize({ startOnLoad: true, theme: "base", themeVariables });\n'
        "</script>"
    )


def starter_html(bundle: dict, base: TokenSet, dark: TokenSet | None, *, chartjs_url: str, mermaid_url: str) -> str:
    tokens_css = to_css(base, dark, mode="all")
    alias_css = role_css_vars(bundle, base)
    role_css = _role_css(bundle, base)
    _refuse_style_breakout(tokens_css, where="tokens css")
    _refuse_style_breakout(alias_css, where="role variables css")
    _refuse_style_breakout(role_css, where="role css")
    css_parts = [tokens_css, alias_css, role_css]
    for component in bundle.get("components") or []:
        comp_css = component.get("css")
        if not comp_css:
            continue
        _refuse_style_breakout(comp_css, where=f"component {component.get('name')!r}")
        css_parts.append(comp_css)
    css = "\n".join(p for p in css_parts if p)
    head = _font_links(bundle)
    scripts = []
    if (bundle.get("charts") or {}).get("library") == "chart.js":
        scripts.append(_chart_script(bundle, base, dark, chartjs_url))
    if (bundle.get("diagrams") or {}).get("library") == "mermaid":
        scripts.append(_mermaid_script(bundle, base, dark, mermaid_url))
    head_block = (head + "\n") if head else ""
    scripts_block = ("\n" + "\n".join(scripts)) if scripts else ""
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{TITLE_SLOT}</title>\n{head_block}<style>\n{css}\n</style>\n</head>\n<body>\n"
        f"{BODY_SLOT}\n{scripts_block}</body>\n</html>\n"
    )


def fill_starter(starter: str, *, title_html: str, body_html: str) -> str:
    """Substitute both slots exactly once each; user content is never rescanned."""
    before_title, rest = starter.split(TITLE_SLOT, 1)
    between, after_body = rest.split(BODY_SLOT, 1)
    return before_title + title_html + between + body_html + after_body


# --- style guide -----------------------------------------------------------

_GUIDE_CSS = """
.sg-wrap{max-width:64rem;margin:0 auto;padding:2rem 1.25rem}
.sg-head{display:flex;flex-wrap:wrap;gap:.75rem;align-items:baseline;justify-content:space-between}
.sg-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(11rem,1fr));gap:.75rem}
.sg-swatch{border:1px solid rgba(127,127,127,.35);border-radius:.5rem;overflow:hidden}
.sg-swatch i{display:block;height:3.5rem}
.sg-swatch small{display:block;padding:.4rem .5rem;font:.75rem/1.3 monospace;word-break:break-all}
.sg-bar{height:.5rem;background:currentColor;opacity:.5;margin:.25rem 0}
.sg-card{border:1px solid rgba(127,127,127,.35);border-radius:.5rem;padding:1rem;margin:1rem 0}
.sg-card pre{overflow:auto;font-size:.8rem}
.sg-toggle{cursor:pointer;padding:.35rem .7rem;border:1px solid currentColor;border-radius:.4rem;background:transparent;color:inherit}
"""

_GUIDE_TOGGLE_JS = """<script>
(function(){var b=document.getElementById("sg-toggle"),h=document.documentElement;
b.addEventListener("click",function(){var cur=h.getAttribute("data-theme");
h.setAttribute("data-theme",cur==="dark"?"light":"dark");});})();
</script>"""

# Runs after the starter's own chart script (placed after {{BODY}}) has set
# window.DS_PALETTE, so the sample waits for the window "load" event rather
# than assuming script order.
_SAMPLE_CHART_JS = """<canvas id="sg-bar" height="120"></canvas><canvas id="sg-line" height="120"></canvas>
<script>
window.addEventListener("load", function(){
if(!window.Chart)return;var p=window.DS_PALETTE||[];
new Chart(document.getElementById("sg-bar"),{type:"bar",data:{labels:["Q1","Q2","Q3","Q4"],
 datasets:[{label:"Runs",data:[120,184,150,210],backgroundColor:p[0]},{label:"Errors",data:[8,5,9,4],backgroundColor:p[1]||p[0]}]}});
new Chart(document.getElementById("sg-line"),{type:"line",data:{labels:["Jan","Feb","Mar","Apr","May"],
 datasets:[{label:"Volume",data:[3,5,4,7,6],borderColor:p[0],tension:.3}]}});
});
</script>"""

_SAMPLE_MERMAID = '<pre class="mermaid">flowchart LR\n  A[Source] --> B[Transform] --> C[Report]</pre>'


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def style_guide_html(
    meta: dict,
    version: dict,
    bundle: dict,
    base: TokenSet,
    dark: TokenSet | None,
    *,
    chartjs_url: str,
    mermaid_url: str,
    render_markdown,
) -> str:
    from src.tokens import css_value

    parts: list[str] = ['<div class="sg-wrap">']
    owner = meta.get("owner") or {}
    parts.append(
        '<div class="sg-head"><div>'
        f"<h1>{_esc(meta.get('name'))}</h1>"
        f"<p><code>{_esc(meta.get('slug'))}</code> · version {_esc(version.get('version'))} · "
        f"{_esc(owner.get('project_name'))} · {_esc(version.get('created_at'))}</p>"
        + (f"<p>{_esc(version.get('note'))}</p>" if version.get("note") else "")
        + '</div><button id="sg-toggle" class="sg-toggle" type="button">Toggle light/dark</button></div>'
    )
    # Palette
    swatches = []
    for tok in base:
        if tok.type == "color":
            pairs = css_value(tok, base)
            if not pairs:
                continue
            name, value = pairs[0]
            swatches.append(
                f'<div class="sg-swatch"><i style="background:var({name})"></i>'
                f"<small>{_esc('.'.join(tok.path))}<br>{_esc(name)}<br>{_esc(value)}</small></div>"
            )
    parts.append('<h2>Palette</h2><div class="sg-grid">' + "".join(swatches) + "</div>")
    # Typography
    specimens = []
    for tok in base:
        if tok.type == "fontFamily":
            specimens.append(
                f'<p style="font-family:var({variable_name(tok.path)})">'
                f"{_esc('.'.join(tok.path))} — The quick brown fox jumps over the lazy dog</p>"
            )
        elif tok.type == "typography":
            v = variable_name(tok.path)
            specimens.append(
                f'<p style="font-family:var({v}-font-family);font-size:var({v}-font-size);'
                f'font-weight:var({v}-font-weight);line-height:var({v}-line-height);'
                f'letter-spacing:var({v}-letter-spacing)">{_esc(".".join(tok.path))} — Aa Bb Cc 123</p>'
            )
    parts.append("<h2>Typography</h2>" + ("".join(specimens) or "<p>No font tokens.</p>"))
    # Scale
    bars = [
        f'<div><small>{_esc(".".join(t.path))}</small>'
        f'<div class="sg-bar" style="width:var({variable_name(t.path)})"></div></div>'
        for t in base
        if t.type == "dimension"
    ]
    parts.append("<h2>Scale</h2>" + ("".join(bars) or "<p>No dimension tokens.</p>"))
    # Components
    cards = []
    for c in bundle.get("components") or []:
        cards.append(
            '<div class="sg-card">'
            f"<h3>{_esc(c['name'])}</h3><p>{_esc(c.get('description', ''))}</p>"
            + (f"<p><em>When to use:</em> {_esc(c['when_to_use'])}</p>" if c.get("when_to_use") else "")
            + f"<div>{c['html']}</div><pre>{_esc(c['html'])}</pre></div>"
        )
    parts.append("<h2>Components</h2>" + ("".join(cards) or "<p>No components.</p>"))
    if (bundle.get("charts") or {}).get("library") == "chart.js":
        notes = bundle["charts"].get("notes")
        parts.append("<h2>Charts</h2>" + (f"<p>{_esc(notes)}</p>" if notes else "") + _SAMPLE_CHART_JS)
    if (bundle.get("diagrams") or {}).get("library") == "mermaid":
        notes = bundle["diagrams"].get("notes")
        parts.append("<h2>Diagrams</h2>" + (f"<p>{_esc(notes)}</p>" if notes else "") + _SAMPLE_MERMAID)
    parts.append("<h2>Guidance</h2>" + render_markdown(bundle.get("guidance", "")))
    warns = version.get("warnings") or []
    if warns:
        items = "".join(f"<li><code>{_esc(w['path'])}</code> {_esc(w['message'])}</li>" for w in warns)
        parts.append("<h2>Warnings</h2>" + "<ul>" + items + "</ul>")
    else:
        parts.append("<h2>Warnings</h2><p>None.</p>")
    parts.append("</div>" + _GUIDE_TOGGLE_JS)
    body = "\n".join(parts)
    starter = starter_html(bundle, base, dark, chartjs_url=chartjs_url, mermaid_url=mermaid_url)
    starter = starter.replace("</style>", _GUIDE_CSS + "</style>", 1)
    return fill_starter(starter, title_html=_esc(meta.get("name")), body_html=body)
