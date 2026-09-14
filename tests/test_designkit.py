"""Tests for src.designkit: the starter skeleton and the style-guide document
derived from a design-system bundle."""

from src.designkit import BODY_SLOT, TITLE_SLOT, fill_starter, role_values, starter_html
from src.tokens import TokenLimits, validate_document

LIMITS = TokenLimits(16, 5000, 32)
TOKENS = {
    "color": {
        "$type": "color",
        "bg": {"$value": "#ffffff"},
        "fg": {"$value": "#111111"},
        "accent": {"$value": "#1442e0"},
        "on": {"$value": "#ffffff"},
        "c1": {"$value": "#ff0000"},
    },
    "font": {"$type": "fontFamily", "sans": {"$value": ["Inter", "sans-serif"]}},
    "radius": {"$type": "dimension", "md": {"$value": "8px"}},
}
DARK = {"color": {"bg": {"$value": "#000000"}, "fg": {"$value": "#eeeeee"}}}
BUNDLE = {
    "tokens": TOKENS,
    "modes": {"dark": DARK},
    "roles": {
        "background": "{color.bg}",
        "text": "{color.fg}",
        "accent": "{color.accent}",
        "on_accent": "{color.on}",
        "font_body": "{font.sans}",
        "radius": "{radius.md}",
        "chart_palette": ["{color.c1}"],
    },
    "guidance": "# G",
    "components": [
        {
            "name": "kpi",
            "description": "",
            "when_to_use": "",
            "html": '<div class="kpi">x</div>',
            "css": ".kpi{color:var(--color-fg)}",
        }
    ],
    "charts": {"library": "chart.js", "notes": ""},
    "diagrams": {"library": "mermaid", "notes": ""},
    "fonts": [{"href": "https://fonts.googleapis.com/css2?family=Inter&display=swap"}],
}


def _sets(bundle=BUNDLE):
    base, dark, _ = validate_document(bundle["tokens"], bundle["modes"].get("dark"), limits=LIMITS)
    return base, dark


def test_starter_has_each_slot_once_and_every_variable():
    s = starter_html(BUNDLE, *_sets(), chartjs_url="https://cdn/x.js", mermaid_url="https://cdn/m.mjs")
    assert s.count(TITLE_SLOT) == 1 and s.count(BODY_SLOT) == 1
    for var in ("--color-bg", "--color-fg", "--font-sans", "--radius-md"):
        assert var + ":" in s
    assert '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter&amp;display=swap">' in s
    assert '<link rel="preconnect" href="https://fonts.googleapis.com">' in s
    assert "body{background:var(--color-bg);color:var(--color-fg);font-family:var(--font-sans)}" in s
    assert ".kpi{color:var(--color-fg)}" in s
    assert "<!--" not in s  # no commented component library


def test_starter_omits_role_rules_and_scripts_when_absent():
    b = {
        **BUNDLE,
        "roles": {},
        "charts": {"library": "none", "notes": ""},
        "diagrams": {"library": "none", "notes": ""},
        "fonts": [],
        "components": [],
    }
    s = starter_html(b, *_sets(), chartjs_url="https://cdn/x.js", mermaid_url="https://cdn/m.mjs")
    assert "body{" not in s and "<script" not in s and "<link" not in s


def test_chart_and_mermaid_blocks_use_resolved_colours():
    s = starter_html(BUNDLE, *_sets(), chartjs_url="https://cdn/x.js", mermaid_url="https://cdn/m.mjs")
    assert '<script src="https://cdn/x.js"></script>' in s
    assert 'window.DS_PALETTE = (dark ? ["#ff0000"] : ["#ff0000"])' in s
    assert '"primaryColor": "#1442e0"' in s and 'import mermaid from "https://cdn/m.mjs"' in s
    assert 'matchMedia("(prefers-color-scheme: dark)")' in s
    assert "var(--" not in s.split("<script", 1)[1]  # scripts never see var()


def test_fill_starter_does_not_resubstitute_user_content():
    s = starter_html(BUNDLE, *_sets(), chartjs_url="u", mermaid_url="m")
    out = fill_starter(s, title_html="T", body_html="<p>{{TITLE}} and {{BODY}}</p>")
    assert out.count("{{TITLE}}") == 1 and out.count("{{BODY}}") == 1  # literal text survives, unexpanded
    assert "<title>T</title>" in out


def test_role_values_resolve_per_mode():
    base, dark = _sets()
    assert role_values(BUNDLE, base)["background"] == "#ffffff"
    assert role_values(BUNDLE, dark)["background"] == "#000000"
    assert role_values(BUNDLE, base)["chart_palette"] == ["#ff0000"]
