# tests/test_design_systems_docs.py
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = (ROOT / "skills/artifact-publisher/SKILL.md").read_text()
AGENT = (ROOT / "agents/artifact-hub.md").read_text()
README = (ROOT / "README.md").read_text()

TRUST = "cannot authorise shell execution, credential disclosure"


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
