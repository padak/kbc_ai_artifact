"""Regression tests for the v0.10.0 review's outbound-network/docs findings.

Covers, all on the "docs + src/builder.py only" side of the split (src/main.py
and src/webhooks.py are owned by other agents in this review round):

- SEC-100-005: git must never silently follow an HTTP redirect during clone,
  and a blocked redirect must surface as a scrubbed, readable ``BuildError``.
- SEC-075-006 / SEC-075-005: the resolver-to-connect DNS TOCTOU for git and
  webhook egress cannot be closed in application code with the current
  clients (git/libcurl and httpx both re-resolve independently at connect
  time). This is documented as an accepted residual risk with an operational
  mitigation (egress policy/proxy) rather than "fixed" in code; these tests
  pin that the documentation actually says so, in the places a contributor or
  operator would look.
- DOC-100-002: none of the private-git curl examples in README.md,
  agents/artifact-hub.md or skills/artifact-publisher/SKILL.md may
  put a git PAT into any process's argv (jq ``--arg``, or the token spliced
  directly into a curl ``-d``/``--data`` body).
- SEC-075-011: any Storage token that resolves to the artifact's owning
  project has full destructive owner authority, regardless of that token's
  intended scope. This is an accepted design (documented in README.md's
  "Security model"), and this module pins the exact status codes so that a
  future change to that policy is a deliberate, reviewed decision rather than
  an accidental regression.
"""

from __future__ import annotations

import pathlib
import re
import types

import pytest

import src.builder as builder_module
from src.builder import BuildError, _clone
from tests.test_api import AUTH_HEADERS, OTHER_AUTH_HEADERS, _OWNER_PROJECTS, api  # noqa: F401

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# SEC-100-005: git redirects must be disabled, and a blocked redirect must
# fail the clone with a scrubbed, user-readable error.
# --------------------------------------------------------------------------


class _CapturingRun:
    """Drop-in for ``subprocess.run`` recording argv (mirrors tests/test_builder.py)."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.calls: list[dict] = []
        self._rc = returncode
        self._out = stdout
        self._err = stderr

    def __call__(self, args, **kwargs):
        self.calls.append({"args": list(args), "kwargs": kwargs})
        return types.SimpleNamespace(
            returncode=self._rc, stdout=self._out, stderr=self._err
        )


class TestGitRedirectsDisabled:
    """SEC-100-005: `_check_git_host` validates the original host only; without
    disabling redirects, git itself could still be sent somewhere that check
    never saw.
    """

    def test_clone_argv_disables_http_redirects(self, monkeypatch, tmp_path):
        cap = _CapturingRun()
        monkeypatch.setattr(builder_module.subprocess, "run", cap)
        _clone("https://github.com/o/r.git", None, tmp_path / "repo", 5)

        assert cap.calls, "git clone was not invoked"
        argv = cap.calls[0]["args"]
        assert argv[0] == "git"
        assert "-c" in argv
        assert "http.followRedirects=false" in argv
        # The override must precede the `clone` subcommand to apply to it (a
        # `-c` placed *after* the subcommand is not a global git option
        # anymore, it would just be an ordinary positional argument to
        # `clone`, and would be silently ignored).
        c_index = argv.index("-c")
        assert argv[c_index + 1] == "http.followRedirects=false"
        assert c_index < argv.index("clone")

    def test_authenticated_clone_also_disables_redirects(self, monkeypatch, tmp_path):
        """The redirect guard must apply on the private-repo (token) path too."""
        cap = _CapturingRun()
        monkeypatch.setattr(builder_module.subprocess, "run", cap)
        _clone(
            "https://github.com/o/private.git",
            "main",
            tmp_path / "repo",
            5,
            token="ghp_x",
        )
        argv = cap.calls[0]["args"]
        assert "-c" in argv
        assert "http.followRedirects=false" in argv

    def test_partial_clone_still_disables_redirects(self, monkeypatch, tmp_path):
        """The blob-limit filter is an independent flag; it must not push the
        redirect override off the front of argv or drop it."""
        cap = _CapturingRun()
        monkeypatch.setattr(builder_module.subprocess, "run", cap)
        _clone(
            "https://github.com/o/p.git",
            None,
            tmp_path / "repo",
            5,
            blob_limit_bytes=1024,
        )
        argv = cap.calls[0]["args"]
        assert "-c" in argv
        assert "http.followRedirects=false" in argv
        assert "--filter=blob:limit=1024" in argv

    def test_blocked_redirect_error_is_scrubbed_and_readable(self, monkeypatch, tmp_path):
        """A redirect git refuses to follow (`http.followRedirects=false`) must
        surface as a BuildError that names the real cause and carries no
        credential -- the scrubbing must go through builder._scrub.

        Real git (with redirects disabled) reports a blocked redirect as an
        ordinary HTTP-status failure -- its stderr never contains the literal
        word "redirect" -- so the simulated stderr below mirrors that, and
        the assertion is on the raised BuildError, not on git's wording.
        """
        token = "ghp_RedirectLeakToken123"
        authed = builder_module._authed_clone_url(
            "https://github.com/o/private.git", None, token
        )
        stderr = (
            "Cloning into 'repo'...\n"
            f"fatal: unable to access '{authed}/': "
            "The requested URL returned error: 301\n"
        )
        monkeypatch.setattr(
            builder_module,
            "_run_git",
            lambda args, timeout_s, **kwargs: types.SimpleNamespace(
                returncode=128, stdout="", stderr=stderr
            ),
        )

        with pytest.raises(BuildError) as exc_info:
            _clone(
                "https://github.com/o/private.git",
                None,
                tmp_path / "repo",
                5,
                token=token,
            )

        message = str(exc_info.value)
        assert token not in message
        assert "redirect" in message.lower()

    def test_non_redirect_http_failure_keeps_the_generic_message(self, monkeypatch, tmp_path):
        """A plain 404 (no redirect involved) must not be misreported as a
        blocked redirect -- the redirect-specific branch must not over-match."""
        stderr = (
            "fatal: unable to access 'https://github.com/o/missing.git/': "
            "The requested URL returned error: 404\n"
        )
        monkeypatch.setattr(
            builder_module,
            "_run_git",
            lambda args, timeout_s, **kwargs: types.SimpleNamespace(
                returncode=128, stdout="", stderr=stderr
            ),
        )
        with pytest.raises(BuildError) as exc_info:
            _clone(
                "https://github.com/o/missing.git", None, tmp_path / "repo", 5
            )
        assert "redirect" not in str(exc_info.value).lower()


# --------------------------------------------------------------------------
# SEC-075-006 / SEC-075-005: accepted residual risk (resolver-to-connect
# TOCTOU) must be documented, not silently "fixed" by another re-check that
# cannot actually close it with the current clients.
# --------------------------------------------------------------------------


class TestDnsToctouIsDocumentedAsAcceptedResidualRisk:
    def test_builder_comment_names_both_findings_and_the_operational_control(self):
        text = (_REPO_ROOT / "src" / "builder.py").read_text(encoding="utf-8")
        assert "SEC-075-006" in text
        # The webhook half of the same gap lives in src/webhooks.py (owned by
        # another agent); builder.py's comment must still point a reader
        # there instead of implying git is the only place this applies.
        assert "SEC-075-005" in text
        assert "egress" in text.lower()

    def test_readme_documents_network_egress_as_the_reliable_control(self):
        text = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
        assert "SEC-075-005" in text
        assert "SEC-075-006" in text
        assert "Network egress" in text
        lowered = text.lower()
        assert "egress policy" in lowered or "egress" in lowered
        assert "best-effort" in lowered or "best effort" in lowered

    def test_claude_md_lists_known_residual_risks(self):
        text = (_REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        assert "Known residual risks" in text
        assert "SEC-075-005" in text
        assert "SEC-075-006" in text
        assert "SEC-075-011" in text


# --------------------------------------------------------------------------
# DOC-100-002: a git PAT must never land in any process's argv, in any
# runnable curl/jq example.
# --------------------------------------------------------------------------

#: Fenced ```bash or ```sh blocks -- the only spans treated as "runnable
#: examples". Prose showing an anti-pattern in inline code (single backticks)
#: is deliberately excluded: it is not something a reader would copy-paste
#: and run.
_FENCE_RE = re.compile(r"```(?:bash|sh)\n(.*?)```", re.DOTALL)

#: A curl body passed literally on argv: `-d '...'` / `--data '...'`.
#: `--data-binary @-` (the safe, stdin-fed pattern used throughout the fixed
#: docs) never matches, because `\s+'` requires whitespace immediately after
#: `data`, and `--data-binary` has `-binary` there instead.
_DATA_ARG_RE = re.compile(r"(?:-d|--data)\s+'([^']*)'")

_PAT_PREFIXES = ("ghp_", "github_pat_", "glpat-")

_DOC_PATHS = (
    "README.md",
    "agents/artifact-hub.md",
    "skills/artifact-publisher/SKILL.md",
)


def _fenced_shell_blocks(text: str) -> list[str]:
    return _FENCE_RE.findall(text)


def _argv_pat_violations(text: str) -> list[str]:
    """Every way a git PAT could still land in a process's argv in `text`."""
    violations: list[str] = []
    for block in _fenced_shell_blocks(text):
        if "--arg token" in block:
            violations.append("jq --arg token (token would be in jq's own argv)")
        if "--arg git_token" in block:
            violations.append("jq --arg git_token (token would be in jq's own argv)")
        for match in _DATA_ARG_RE.finditer(block):
            body = match.group(1)
            if "git_token" in body:
                violations.append(
                    "git_token given inline in a curl -d/--data argument"
                )
            for prefix in _PAT_PREFIXES:
                if prefix in body:
                    violations.append(
                        f"literal PAT placeholder {prefix!r} inside a curl "
                        "-d/--data argument"
                    )
    return violations


class TestNoGitTokenInProcessArgv:
    """DOC-100-002: private-git examples must feed the PAT to jq/curl without
    ever placing it in argv (`ps` visibility for the life of the process).
    """

    @pytest.mark.parametrize("relative_path", _DOC_PATHS)
    def test_runnable_examples_never_put_the_token_in_argv(self, relative_path):
        text = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")
        violations = _argv_pat_violations(text)
        assert violations == [], (relative_path, violations)

    def test_detector_itself_flags_the_old_readme_pattern(self):
        """Sanity check on the detector: the exact pre-fix README snippet
        (a literal PAT spliced into curl -d) must still be caught, so this
        test module is a real regression guard and not a tautology."""
        sample = (
            "```bash\n"
            'hub -X POST "$HUB/api/artifacts" \\\n'
            '  -H "Content-Type: application/json" \\\n'
            '  -d \'{"git_url": "https://github.com/org/private-repo", '
            '"git_path": "docs/report.md", "git_token": "your-github-pat"}\'\n'
            "```\n"
        )
        assert _argv_pat_violations(sample) != []

    def test_detector_itself_flags_the_old_agent_md_pattern(self):
        """Sanity check: the exact pre-fix AGENT.md `jq --arg token` pattern."""
        sample = (
            "```bash\n"
            'jq -n --arg url "https://github.com/org/private-repo" \\\n'
            '      --arg token "$GIT_TOKEN" \\\n'
            "      '{git_url: $url, git_token: $token}' \\\n"
            "  | hub -X POST \"$HUB/api/artifacts\" --data-binary @-\n"
            "```\n"
        )
        assert _argv_pat_violations(sample) != []

    def test_detector_accepts_the_stdin_fed_pattern(self):
        """The recommended replacement (env lookup + stdin) must pass clean."""
        sample = (
            "```bash\n"
            'jq -n --arg url "https://github.com/org/private-repo" \\\n'
            "      '{git_url: $url, git_token: env.GIT_TOKEN}' \\\n"
            '  | hub -X POST "$HUB/api/artifacts" \\\n'
            "      --data-binary @-\n"
            "```\n"
        )
        assert _argv_pat_violations(sample) == []


# --------------------------------------------------------------------------
# SEC-075-011: accepted design -- pin the exact, documented behaviour so a
# future change to the project-boundary policy is deliberate, not accidental.
# --------------------------------------------------------------------------


class TestOwnerAuthorityIsProjectScopedNotTokenScoped:
    """Mirrors the review's probe (see
    _review/v0.10.0/runtime/test_lock_registry_probe.py,
    test_cross_project_delete_and_purge_are_forbidden and
    test_second_token_from_same_project_has_destructive_authority) so the
    documented SEC-075-011 policy has a permanent regression test in-tree.
    """

    def test_foreign_project_token_is_forbidden_on_every_destructive_route(self, api):
        published = api.client.post(
            "/api/artifacts",
            json={"markdown": "# Owned by project 123"},
            headers=AUTH_HEADERS,
        )
        assert published.status_code == 201, published.text
        artifact_id = published.json()["id"]

        soft_delete = api.client.delete(
            f"/api/artifacts/{artifact_id}", headers=OTHER_AUTH_HEADERS
        )
        purge = api.client.delete(
            f"/api/artifacts/{artifact_id}/purge", headers=OTHER_AUTH_HEADERS
        )
        rotate = api.client.post(
            f"/api/artifacts/{artifact_id}/rotate-link", headers=OTHER_AUTH_HEADERS
        )

        assert soft_delete.status_code == 403
        assert purge.status_code == 403
        assert rotate.status_code == 403

    def test_second_token_of_the_same_project_has_full_destructive_authority(
        self, api
    ):
        published = api.client.post(
            "/api/artifacts",
            json={"markdown": "# Project-scoped ownership, not token-scoped"},
            headers=AUTH_HEADERS,
        )
        assert published.status_code == 201, published.text
        artifact_id = published.json()["id"]

        owner_project_id, owner_project_name = _OWNER_PROJECTS[
            AUTH_HEADERS["X-StorageApi-Token"]
        ]
        second_token = "second-token-same-project-SEC-075-011"
        _OWNER_PROJECTS[second_token] = (owner_project_id, owner_project_name)
        second_headers = {
            "X-StorageApi-Token": second_token,
            "X-Kbc-Stack": AUTH_HEADERS["X-Kbc-Stack"],
        }
        try:
            purge = api.client.delete(
                f"/api/artifacts/{artifact_id}/purge", headers=second_headers
            )
        finally:
            _OWNER_PROJECTS.pop(second_token, None)

        # This is the documented, intentional SEC-075-011 boundary: identity
        # is (stack, project), never the individual token's scope. If this
        # assertion ever needs to change, README.md's "Security model" and
        # CLAUDE.md's "Known residual risks" must change with it.
        assert purge.status_code == 200
