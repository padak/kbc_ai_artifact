"""The shipped sample design systems must stay valid and complete.

``examples/design-systems/*.json`` are registered on a live hub by
``scripts/register_design_systems.py``; a file that no longer validates would
only be discovered at release time, against production. These tests run the
same validator the route runs, plus the editorial floor the samples promise:
ten known slugs, three or more components, a dark mode, a ``chart_palette``
role and a guidance brief that is actually a brief.

They also pin the *direction* of the two modes. Spec Key decision 8 makes the
base token document the light look and ``modes.dark`` the dark one, so a
dark-first system (``board-deck``, ``data-dashboard``) must still carry a real
light base — otherwise ``data-theme="light"`` renders near-black. That is a
palette mistake no schema validator can catch, so it is asserted here as
relative luminance.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config import Settings, load_settings
from src.designs import SLUG_RE, validate_bundle
from src.tokens import TokenLimits, alias_target, concrete_value, validate_document

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples" / "design-systems"

EXPECTED_SLUGS = {
    "academic-paper", "board-deck", "data-dashboard", "end-user-guide",
    "exec-report", "incident-postmortem", "keboola-website", "oldschool-memo",
    "software-manual", "tech-docs",
}

#: The shortest guidance that can plausibly carry the documented outline.
MIN_GUIDANCE_CHARS = 800
#: A light background is well above mid-grey...
MIN_LIGHT_LUMINANCE = 0.5
#: ...and a dark one is well below it, with a gap so neither is a near-miss.
MAX_DARK_LUMINANCE = 0.35


def _srgb_luminance(hex_colour: str) -> float:
    """WCAG relative luminance of a ``#rgb``/``#rrggbb`` sRGB colour, 0..1."""
    raw = hex_colour.strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(c * 2 for c in raw)
    assert len(raw) >= 6, f"not a hex colour: {hex_colour!r}"
    channels = []
    for offset in (0, 2, 4):
        value = int(raw[offset:offset + 2], 16) / 255
        channels.append(value / 12.92 if value <= 0.04045
                        else ((value + 0.055) / 1.055) ** 2.4)
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _role_colour(bundle: dict, role: str, *, dark: bool) -> str:
    """The concrete colour ``role`` resolves to in the base or the dark mode."""
    limits = TokenLimits(16, 5000, 32)
    base, dark_set, _ = validate_document(
        bundle["tokens"], (bundle.get("modes") or {}).get("dark"), limits=limits)
    tokens = dark_set if dark else base
    assert tokens is not None, "bundle has no dark mode"
    return concrete_value(tokens, alias_target(bundle["roles"][role]))


def _paths() -> list[Path]:
    return sorted(EXAMPLES_DIR.glob("*.json"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def settings() -> Settings:
    # The limits the deployed hub actually enforces; conftest supplies the
    # required env vars, so this is the same Settings the routes get.
    return load_settings()


def test_the_ten_slugs_are_exactly_the_shipped_set():
    slugs = [_load(p)["slug"] for p in _paths()]
    assert len(slugs) == len(set(slugs)), "duplicate slug across example files"
    assert set(slugs) == EXPECTED_SLUGS


def test_every_file_is_named_after_its_slug():
    for path in _paths():
        assert _load(path)["slug"] == path.stem


@pytest.mark.parametrize("path", _paths(), ids=lambda p: p.stem)
def test_example_is_a_valid_registration_body(path: Path, settings: Settings):
    body = _load(path)
    assert SLUG_RE.match(body["slug"]), body["slug"]
    assert 1 <= len(body["name"]) <= settings.ds_max_name_chars
    assert len(body["description"]) <= settings.ds_max_description_chars
    assert len(body["note"]) <= settings.ds_max_note_chars
    assert set(body) == {"slug", "name", "description", "note", "bundle"}

    normalised, warnings = validate_bundle(body["bundle"], settings=settings)
    assert warnings == [], f"{path.stem} carries non-fatal findings: {warnings}"

    assert "dark" in (normalised.get("modes") or {}), "no dark mode"
    roles = normalised.get("roles") or {}
    assert "chart_palette" in roles and roles["chart_palette"]
    for required in ("background", "surface", "text", "accent", "font_body",
                     "font_heading", "font_mono"):
        assert required in roles, f"{path.stem} is missing role {required}"

    components = normalised.get("components") or []
    assert len(components) >= 3, f"{path.stem} has only {len(components)} component(s)"
    assert len({c["name"] for c in components}) == len(components)
    for component in components:
        assert component["html"].strip()
        assert "var(--" in component["css"], f"{component['name']} hard-codes its look"

    assert len(normalised["guidance"]) >= MIN_GUIDANCE_CHARS


@pytest.mark.parametrize("path", _paths(), ids=lambda p: p.stem)
def test_guidance_follows_the_brief_outline(path: Path):
    guidance = _load(path)["bundle"]["guidance"].lower()
    for heading in ("principles", "layout", "typography", "colour", "charts",
                    "diagrams", "components", "do"):
        assert heading in guidance, f"{path.stem} guidance has no '{heading}' section"


@pytest.mark.parametrize("path", _paths(), ids=lambda p: p.stem)
def test_fonts_are_google_fonts_css2_links_only(path: Path):
    for font in _load(path)["bundle"].get("fonts") or []:
        assert font["href"].startswith("https://fonts.googleapis.com/css2?"), font


@pytest.mark.parametrize("path", _paths(), ids=lambda p: p.stem)
def test_base_is_the_light_look_and_dark_mode_is_dark(path: Path):
    """Key decision 8: the base document is light, ``modes.dark`` is dark.

    A dark-first system may *prefer* its dark mode, but inverting the two
    leaves ``:root[data-theme="light"]`` rendering near-black — the one failure
    mode a schema check cannot see.
    """
    bundle = _load(path)["bundle"]
    light_bg = _srgb_luminance(_role_colour(bundle, "background", dark=False))
    dark_bg = _srgb_luminance(_role_colour(bundle, "background", dark=True))
    assert light_bg > MIN_LIGHT_LUMINANCE, (
        f"{path.stem}: base background luminance {light_bg:.3f} is not a light look")
    assert dark_bg < MAX_DARK_LUMINANCE, (
        f"{path.stem}: dark-mode background luminance {dark_bg:.3f} is not dark")

    light_text = _srgb_luminance(_role_colour(bundle, "text", dark=False))
    assert light_text < MIN_LIGHT_LUMINANCE, (
        f"{path.stem}: base text luminance {light_text:.3f} is not dark on a light base")


@pytest.mark.parametrize("path", _paths(), ids=lambda p: p.stem)
def test_starter_exposes_the_stable_role_variables(path: Path):
    """Every sample must be swappable: its starter emits the ``--ds-*`` layer.

    A document styled only against these aliases re-skins by pointing at
    another system's ``/ds/{id}/css``. A sample whose starter lacked them
    would silently break that promise for the switcher demo.
    """
    from src.designkit import starter_html

    bundle = _load(path)["bundle"]
    limits = TokenLimits(16, 5000, 32)
    base, dark, _ = validate_document(
        bundle["tokens"], (bundle.get("modes") or {}).get("dark"), limits=limits)
    starter = starter_html(
        bundle, base, dark,
        chartjs_url="https://example.invalid/chart.js",
        mermaid_url="https://example.invalid/mermaid.mjs",
    )
    for variable in ("--ds-background", "--ds-accent"):
        assert f"{variable}:var(--" in starter, f"{path.stem} starter has no {variable}"


def test_data_dashboard_chart_panel_draws_something():
    """A style guide renders a component's HTML as-is and runs no chart code.

    The panel used to ship a bare ``<canvas>``, which reads as a broken
    component in the gallery: nothing on the page ever draws into it. It now
    carries a static inline SVG sample instead, and its ``when_to_use`` says
    a real chart replaces that SVG with a chart.js canvas.
    """
    bundle = _load(EXAMPLES_DIR / "data-dashboard.json")["bundle"]
    (panel,) = [c for c in bundle["components"] if c["name"] == "chart-panel"]
    assert "<canvas" not in panel["html"], "chart-panel still ships an undrawn canvas"
    assert "<svg" in panel["html"]
    assert "Requests per minute" in panel["html"]
    assert "last 6 hours" in panel["html"]
    assert "#" not in panel["html"], "sample bars must use var(--…), not hex"
    assert "var(--color-chart-" in panel["html"]
    assert "chart.js" in panel["when_to_use"]
