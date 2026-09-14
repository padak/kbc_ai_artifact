"""Documents derived from a design-system bundle: the starter skeleton and the
style-guide page body. Pure functions of (bundle, token sets); no I/O.

Both outputs are *user content*: they are only ever served through
``main._sandboxed_html`` or inside the sandboxed ``srcdoc`` iframe. Nothing
here is safe to place on the hub's own origin.
"""
from __future__ import annotations

import html
import json
from urllib.parse import urlsplit

from src.tokens import TokenSet, alias_target, concrete_value, to_css, variable_name

TITLE_SLOT = "{{TITLE}}"
BODY_SLOT = "{{BODY}}"

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
    roles = bundle.get("roles") or {}
    rules = []
    for needed, template in _ROLE_RULES:
        if all(r in roles for r in needed):
            rules.append(template.format(**{r: _var(base, roles[r]) for r in needed}))
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
    css_parts = [to_css(base, dark, mode="all"), _role_css(bundle, base)]
    css_parts.extend(c["css"] for c in bundle.get("components") or [] if c.get("css"))
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
