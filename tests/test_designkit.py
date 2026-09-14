"""Tests for src.designkit: the starter skeleton and the style-guide document
derived from a design-system bundle."""

import pytest

from src.designkit import BODY_SLOT, TITLE_SLOT, fill_starter, role_values, starter_html, style_guide_html
from src.tokens import TokenLimits, to_css, validate_document

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


def test_component_css_style_breakout_is_refused():
    """A component whose css contains '</style' (any case, optional whitespace) would
    break out of the starter's single unescaped <style> block and inject markup.
    Track 2's bundle validator is expected to reject this at submit time (422), but
    designkit refuses defensively too, as the last line before the unescaped <style>."""
    evil = {
        **BUNDLE,
        "components": [
            {
                "name": "evil-widget",
                "description": "",
                "when_to_use": "",
                "html": "<div></div>",
                "css": ".x{}</style><script>1</script>",
            }
        ],
    }
    with pytest.raises(ValueError, match="evil-widget"):
        starter_html(evil, *_sets(evil), chartjs_url="u", mermaid_url="m")


def test_component_css_style_breakout_is_refused_case_and_whitespace_insensitive():
    evil = {
        **BUNDLE,
        "components": [
            {
                "name": "evil2",
                "description": "",
                "when_to_use": "",
                "html": "<div></div>",
                "css": ".x{}</\t\tSTYLE  ><script>1</script>",
            }
        ],
    }
    with pytest.raises(ValueError, match="evil2"):
        starter_html(evil, *_sets(evil), chartjs_url="u", mermaid_url="m")


def test_chart_and_mermaid_scripts_fall_back_to_light_without_dark_mode():
    """No dark mode declared at all (validate_document called with overrides=None):
    both branches of the ``dark ?`` expressions in the chart and mermaid scripts must
    fall back to the light values, and the emitted stylesheet must carry no @media
    block (there is nothing to diverge to)."""
    base, dark, _ = validate_document(BUNDLE["tokens"], None, limits=LIMITS)
    assert dark is None
    s = starter_html(BUNDLE, base, dark, chartjs_url="https://cdn/x.js", mermaid_url="https://cdn/m.mjs")
    assert 'window.DS_PALETTE = (dark ? ["#ff0000"] : ["#ff0000"])' in s
    assert '"primaryColor": "#1442e0"' in s and 'import mermaid from "https://cdn/m.mjs"' in s
    scripts = s.split("<script", 1)[1]
    assert "var(--" not in scripts
    css = to_css(base, dark, mode="all")
    assert "@media" not in css


META = {"name": "Corp <b>", "slug": "corp", "owner": {"project_name": "Mkt & co", "project_id": 1, "stack_host": "h"}}
VERSION = {
    "version": 2,
    "note": "<i>note</i>",
    "created_at": "2026-09-15T00:00:00Z",
    "warnings": [{"path": "/tokens/x", "message": "<w>"}],
}


def _guide(bundle=BUNDLE):
    return style_guide_html(
        META,
        VERSION,
        bundle,
        *_sets(bundle),
        chartjs_url="u",
        mermaid_url="m",
        render_markdown=lambda md: "<h1>G</h1>",
    )


def test_guide_escapes_meta_and_lists_sections():
    g = _guide()
    assert "Corp &lt;b&gt;" in g and "Mkt &amp; co" in g and "&lt;i&gt;note&lt;/i&gt;" in g and "&lt;w&gt;" in g
    for section in ("Palette", "Typography", "Scale", "Components", "Charts", "Diagrams", "Guidance", "Warnings"):
        assert f"<h2>{section}</h2>" in g
    assert "--color-accent" in g and "#1442e0" in g  # swatch shows variable and value
    assert '<div class="kpi">x</div>' in g  # live component
    assert "&lt;div class=&quot;kpi&quot;&gt;x&lt;/div&gt;" in g  # and its escaped source
    assert "<h1>G</h1>" in g
    assert "data-theme" in g and "toggle" in g.lower()


def test_guide_skips_chart_and_diagram_sections_when_not_declared():
    b = {**BUNDLE, "charts": {"library": "none", "notes": ""}, "diagrams": {"library": "none", "notes": ""}}
    g = _guide(b)
    assert "<h2>Charts</h2>" not in g and "<h2>Diagrams</h2>" not in g


def test_guide_is_built_on_the_starter():
    g = _guide()
    assert g.startswith("<!doctype html>") and TITLE_SLOT not in g and BODY_SLOT not in g


def test_design_system_page_is_hub_chrome_around_a_sandboxed_iframe():
    from src import pages

    proj = {
        "id": "ds_abc",
        "slug": "corp",
        "name": "Corp <x>",
        "description": "d",
        "head_version": 2,
        "owner": {"project_name": "P", "project_id": 1, "stack_host": "h"},
        "urls": {},
    }
    rows = [
        {"version": 1, "created_at": "2026-09-14T00:00:00Z", "note": ""},
        {"version": 2, "created_at": "2026-09-15T00:00:00Z", "note": "n"},
    ]
    out = pages.design_system_page("https://hub", proj, rows, 2, "<html>guide</html>", "0.16.0")
    assert "Corp &lt;x&gt;" in out
    assert 'sandbox="allow-scripts allow-popups allow-forms allow-downloads"' in out and "allow-same-origin" not in out
    assert 'srcdoc="&lt;html&gt;guide&lt;/html&gt;"' in out
    assert 'href="https://hub/ds/ds_abc?v=1"' in out
    assert "https://hub/ds/ds_abc/bundle?v=2" in out and "https://hub/ds/ds_abc/starter?v=2" in out
    assert "hubSession" not in out
