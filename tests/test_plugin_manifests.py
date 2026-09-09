"""The repository is a Claude Code marketplace carrying one plugin.

``claude plugin marketplace add padak/kbc_ai_artifact`` followed by
``claude plugin install artifact-hub@kbc-artifact-hub`` installs the subagent
and the skill, and Claude Code refreshes both in the background whenever the
plugin's version moves. That last part is why the version in both manifests
must equal ``[project].version``: a release that forgets the manifests is a
release nobody's plugin ever sees.

Layout is the plugin loader's default, verified against the CLI (2.1.x):
``agents/<name>.md`` and ``skills/<name>/SKILL.md`` at the plugin root. A
custom ``agents`` path in plugin.json and a symlink in ``agents/`` were both
tried and neither yielded an agent, so the agent definition *lives* at
``agents/artifact-hub.md`` and everything else (``/agent``, the release
asset) reads it from there.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import src.main as main

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_JSON = ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE_JSON = ROOT / ".claude-plugin" / "marketplace.json"
AGENT_FILE = ROOT / "agents" / "artifact-hub.md"
SKILL_DIR = ROOT / "skills" / "artifact-publisher"

PLUGIN_NAME = "artifact-hub"
MARKETPLACE_NAME = "kbc-artifact-hub"


def _project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def test_plugin_manifest_names_the_plugin_and_tracks_the_project_version() -> None:
    manifest = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))
    assert manifest["name"] == PLUGIN_NAME
    assert manifest["version"] == _project_version()
    assert manifest["description"]
    assert manifest["repository"] == main.GITHUB_REPO_URL
    for skill in manifest["skills"]:
        assert (ROOT / skill / "SKILL.md").is_file(), skill
    assert "./skills/artifact-publisher" in manifest["skills"]
    # The agent is discovered from the default agents/ directory; a custom
    # path here is silently ignored by the loader (see the module docstring).
    assert "agents" not in manifest


def test_marketplace_manifest_lists_exactly_this_plugin() -> None:
    manifest = json.loads(MARKETPLACE_JSON.read_text(encoding="utf-8"))
    assert manifest["name"] == MARKETPLACE_NAME
    assert manifest["description"]
    assert manifest["owner"]["name"]
    (entry,) = manifest["plugins"]
    assert entry["name"] == PLUGIN_NAME
    assert entry["source"] == "./"
    assert entry["version"] == _project_version()
    assert entry["description"]


def test_agent_definition_lives_where_the_plugin_loader_looks() -> None:
    assert AGENT_FILE.is_file()
    assert main.AGENT_PATH == AGENT_FILE
    head = AGENT_FILE.read_text(encoding="utf-8").split("---", 2)[1]
    assert re.search(r"^name: artifact-hub$", head, re.M)
    assert not (ROOT / "skills" / "artifact-hub-agent").exists()


def test_skill_stays_where_both_the_plugin_and_the_hub_read_it() -> None:
    assert main.SKILL_PATH == SKILL_DIR / "SKILL.md"


def test_release_workflow_ships_the_agent_from_its_new_home() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "cp agents/artifact-hub.md dist/AGENT.md" in workflow
    assert "skills/artifact-hub-agent" not in workflow
    # The release gate refuses a tag whose plugin manifests lag behind.
    assert ".claude-plugin/plugin.json" in workflow
    assert ".claude-plugin/marketplace.json" in workflow


def test_readme_documents_the_plugin_install() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "claude plugin marketplace add padak/kbc_ai_artifact" in readme
    assert f"claude plugin install {PLUGIN_NAME}@{MARKETPLACE_NAME}" in readme
