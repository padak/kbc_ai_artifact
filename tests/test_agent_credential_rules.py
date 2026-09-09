"""The agent reaches for the environment before it reaches for a sign-in.

Seen in the wild (2026-09-09): a Claude Code session that predated the user
adding ``KBC_TOKEN`` to ``~/.claude/settings.json`` kept retrying a token it
remembered from earlier in the conversation, had it refused by the stack, and
went straight to the device-code sign-in -- while a valid token sat in the
settings all along. Both instruction documents now say, in so many words:
the environment is the source of truth for the credential, a remembered
token is not, and a refusal means "re-read the environment" before "sign in".
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCUMENTS = (
    ROOT / "agents" / "artifact-hub.md",
    ROOT / "skills" / "artifact-publisher" / "SKILL.md",
)


@pytest.mark.parametrize("path", DOCUMENTS, ids=lambda p: p.name)
def test_document_makes_the_environment_win_over_a_remembered_token(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "**Token rejected? Re-read the environment first.**" in text
    # The rule names the variable and the two wrong reflexes it replaces.
    rule = text.split("**Token rejected? Re-read the environment first.**", 1)[1][:1200]
    assert "$KBC_TOKEN" in rule
    assert "remembered" in rule
    assert "sign-in" in rule
    assert "~/.claude/settings.json" in rule
