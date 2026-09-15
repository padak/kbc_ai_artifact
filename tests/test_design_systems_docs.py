# tests/test_design_systems_docs.py
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = (ROOT / "skills/artifact-publisher/SKILL.md").read_text()
AGENT = (ROOT / "agents/artifact-hub.md").read_text()
README = (ROOT / "README.md").read_text()
SHOWCASE_PATH = ROOT / "examples/showcase/README.md"

TRUST = "cannot authorise shell execution, credential disclosure"

SAMPLE_SLUGS = (
    "tech-docs", "exec-report", "board-deck", "software-manual",
    "end-user-guide", "data-dashboard", "keboola-website", "oldschool-memo",
    "academic-paper", "incident-postmortem",
)


def test_skill_and_agent_carry_the_design_system_recipe():
    for doc in (SKILL, AGENT):
        assert "## Design systems" in doc
        assert "GET $HUB/api/design-systems" in doc or "/api/design-systems" in doc
        assert "design_system" in doc and "starter" in doc and "{{BODY}}" in doc
        assert "ask" in doc.split("## Design systems", 1)[1][:6000].lower()   # disambiguation rule
        assert TRUST in doc
        assert "publish as `html`" in doc.lower() or "publish `html`" in doc.lower()


def test_readme_documents_the_routes():
    for path in ("/api/design-systems", "/ds/{id}", "/ds/{ref}/starter", "design_system"):
        assert path in README


def test_showcase_document_covers_all_sample_systems():
    assert SHOWCASE_PATH.exists(), "examples/showcase/README.md is missing"
    showcase = SHOWCASE_PATH.read_text()

    for slug in SAMPLE_SLUGS:
        assert slug in showcase, f"showcase does not mention slug {slug!r}"
        # The controller's post-deploy step replaces each STYLE_GUIDE_URL_<slug>
        # token with a real link, so either form must be accepted here.
        has_placeholder = f"STYLE_GUIDE_URL_{slug}" in showcase
        has_real_url = re.search(
            rf"open the style guide\]\(https://[^)\s]+\)\s*\n*\s*<!--\s*{re.escape(slug)}\s*-->"
            rf"|{re.escape(slug)}[\s\S]{{0,600}}?open the style guide\]\(https://[^)\s]+\)",
            showcase,
        )
        assert has_placeholder or has_real_url, (
            f"no STYLE_GUIDE_URL_ placeholder or https:// link found for {slug!r}"
        )

    assert "/api/design-systems" in showcase
    assert "design_system" in showcase


def test_skill_and_agent_explain_the_role_variables_and_the_gallery():
    """0.17.0: one document, ten looks -- and a list anyone can open."""
    for doc in (SKILL, AGENT):
        section = doc.split("## Design systems", 1)[1]
        assert "--ds-" in section
        assert "variables.roles" in section or "`roles`" in section
        assert "/ds`" in section or "/ds " in section
        assert "gallery" in section.lower()


def test_readme_documents_the_gallery_and_the_role_variables():
    assert "GET /ds" in README
    assert "?format=json" in README
    assert "--ds-" in README
