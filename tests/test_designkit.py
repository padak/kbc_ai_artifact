"""Tests for src.designkit: the starter skeleton and the style-guide document
derived from a design-system bundle."""

import pytest

from src.designkit import (
    BODY_SLOT,
    TITLE_SLOT,
    SAFE_CSS_COLOR_RE,
    fill_starter,
    is_safe_css_color,
    role_css_vars,
    role_values,
    role_variable_names,
    starter_html,
    style_guide_html,
)
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
    assert "body{background:var(--ds-background);color:var(--ds-text);font-family:var(--ds-font-body)}" in s
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
    assert "--ds-" not in s


# --- role variables (0.17.0) ----------------------------------------------


def test_role_variable_names_covers_declared_roles_only():
    names = role_variable_names(BUNDLE)
    assert names["background"] == "--ds-background"
    assert names["text"] == "--ds-text"
    assert names["accent"] == "--ds-accent"
    assert names["on_accent"] == "--ds-on-accent"
    assert names["font_body"] == "--ds-font-body"
    assert names["radius"] == "--ds-radius"
    # chart_palette expands to one alias per entry, plus the count
    assert names["chart_palette_1"] == "--ds-chart-1"
    assert names["chart_palette_count"] == "--ds-chart-count"
    # nothing is invented for a role the bundle never declared
    assert "surface" not in names and "muted" not in names and "border" not in names
    assert role_variable_names({"roles": {}}) == {}
    assert role_variable_names({}) == {}


def test_role_css_vars_aliases_the_mode_following_targets():
    base, _ = _sets()
    css = role_css_vars(BUNDLE, base)
    assert css.startswith(":root{") and css.endswith("}")
    assert css.count(":root{") == 1
    for pair in (
        "--ds-background:var(--color-bg)",
        "--ds-text:var(--color-fg)",
        "--ds-accent:var(--color-accent)",
        "--ds-on-accent:var(--color-on)",
        "--ds-font-body:var(--font-sans)",
        "--ds-radius:var(--radius-md)",
        "--ds-chart-1:var(--color-c1)",
        "--ds-chart-count:1",
    ):
        assert pair in css, pair
    assert "--ds-surface" not in css


def test_role_css_vars_is_empty_when_no_roles_are_declared():
    base, _ = _sets()
    assert role_css_vars({"roles": {}}, base) == ""
    assert role_css_vars({}, base) == ""


def test_starter_carries_the_alias_block_and_uses_it_in_role_rules():
    s = starter_html(BUNDLE, *_sets(), chartjs_url="https://cdn/x.js", mermaid_url="https://cdn/m.mjs")
    assert ":root{--ds-background:var(--color-bg)" in s
    assert "a{color:var(--ds-accent)}" in s
    assert "code,pre,kbd{font-family:var(--ds-font-mono)}" not in s  # role not declared
    # the alias block sits inside the one <style> block, after the token CSS
    style = s.split("<style>", 1)[1].split("</style>", 1)[0]
    assert "--color-bg:" in style
    assert style.index("--color-bg:") < style.index("--ds-background:")


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


# --- colour safety (fix round 1) -------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "#fff",
        "#FFF",
        "#ffffff",
        "#11223344",
        "rgb(1,2,3)",
        "rgb( 12 , 34 , 56 )",
        "rgb(18 52 86)",
        "rgb(18 52 86 / 0.5)",
        "rgba(1,2,3,0.25)",
        "rgba(1, 2, 3, 1)",
        "rebeccapurple",
        "Red",
    ],
)
def test_is_safe_css_color_accepts_real_colours(value):
    assert is_safe_css_color(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "#12",
        "#1234567",
        "#zzzzzz",
        "red;position:fixed",
        "position:fixed;top:0;left:0;width:100vw;background:url(https://attacker.example/x)",
        "url(https://attacker.example/x)",
        "var(--color-bg)",
        "rgb(1,2,3);background:url(https://attacker.example/p)",
        "expression(alert(1))",
        "a" * 21,
        "#fff /*",
        "\\75 rl(x)",
        None,
        123,
    ],
)
def test_is_safe_css_color_rejects_everything_else(value):
    assert is_safe_css_color(value) is False


def test_safe_css_color_re_is_exported_for_reuse():
    assert SAFE_CSS_COLOR_RE.match("#abc")
    assert not SAFE_CSS_COLOR_RE.match("#abc;x:y")
