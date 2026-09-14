"""The shipped sample design systems must stay valid and complete.

``examples/design-systems/*.json`` are registered on a live hub by
``scripts/register_design_systems.py``; a file that no longer validates would
only be discovered at release time, against production. These tests run the
same validator the route runs, plus the editorial floor the samples promise:
ten known slugs, three or more components, a dark mode, a ``chart_palette``
role and a guidance brief that is actually a brief.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config import Settings
from src.designs import SLUG_RE, validate_bundle

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples" / "design-systems"

EXPECTED_SLUGS = {
    "academic-paper", "board-deck", "data-dashboard", "end-user-guide",
    "exec-report", "incident-postmortem", "keboola-website", "oldschool-memo",
    "software-manual", "tech-docs",
}

#: The shortest guidance that can plausibly carry the documented outline.
MIN_GUIDANCE_CHARS = 800
#: Spec defaults for the metadata fields, asserted literally so the samples stay
#: registrable on a hub that has not raised any HUB_DS_* limit.
MAX_NAME_CHARS = 80
MAX_TEXT_CHARS = 500


def _paths() -> list[Path]:
    return sorted(EXAMPLES_DIR.glob("*.json"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def settings() -> Settings:
    # validate_bundle reads limits only; the three required fields are unused.
    return Settings(hub_storage_token="", hub_stack_url="", secret_key="")


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
    assert 1 <= len(body["name"]) <= MAX_NAME_CHARS
    assert len(body["description"]) <= MAX_TEXT_CHARS
    assert len(body["note"]) <= MAX_TEXT_CHARS
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
