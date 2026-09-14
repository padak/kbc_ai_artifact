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
