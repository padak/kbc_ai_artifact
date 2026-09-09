"""Regression tests for the v0.10.0 review's release and single-writer findings.

Three findings share this module because they share one theme: a rule the
project had written down but nothing checked.

* ``SUP-100-001`` — release integrity controls were advisory. The workflow
  assertions below pin the parts of that chain that live in the repository:
  every third-party action referenced by a full commit SHA, CI running the
  suite/compile/audit/doc gates as separately named jobs, and the privileged
  release job re-running all of them *plus* proving the tagged commit is on
  ``main``. The parts that live in GitHub settings (required status checks,
  the ``v*`` tag ruleset, required signatures) cannot be asserted from here;
  the job ids in :data:`REQUIRED_CI_JOBS` are the names those settings refer
  to, so renaming one here without renaming it there silently drops a gate.
* ``DOC-100-001`` — the README's production deploy command pinned a tag one
  release behind the code it documented. The parity test compares the three
  places a release version is written down against each other, so any future
  bump that misses one fails instead of shipping operators the old release.
* ``ARCH-100-001`` — "exactly one instance, ever" was a sentence in CLAUDE.md
  that nothing enforced. Two mechanisms are covered: the exclusive startup
  lock (which stops a second process on the same disk) and the state sidecar's
  foreign-writer latch (which catches the case the lock cannot see, two
  processes on two disks sharing one Storage project).
"""

from __future__ import annotations

import fcntl
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

import src.main as main
from src.kbc import InMemoryFilesBackend
from src.statedb import (
    SNAPSHOT_FILE_NAME,
    TAG_STATE,
    ForeignWriterError,
    StateDB,
    foreign_writer_detected,
    note_foreign_writer,
)
from tests.test_api import api  # noqa: F401 - the fixture is used by name

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

#: CI job ids that are meant to be *required* status checks on ``main``. They
#: are the literal strings the branch protection rule names, so this list is
#: the contract between the workflow file and the GitHub setting.
REQUIRED_CI_JOBS = ("tests", "compile", "audit", "docs-parity")

#: ``uses: owner/repo@<40 hex>`` followed by a ``# vX.Y.Z`` version comment.
_PINNED_USES = re.compile(
    r"^\s*(?:-\s+)?uses:\s+(?P<action>[\w.-]+/[\w.-]+)@(?P<sha>[0-9a-f]{40})"
    r"\s+#\s*v(?P<version>\d+\.\d+(?:\.\d+)?)\s*$"
)
_ANY_USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>\S+)")


def _workflow_files() -> list[Path]:
    files = sorted(WORKFLOWS.glob("*.yml"))
    assert files, "no workflow files found"
    return files


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _run_script(job: dict) -> str:
    """Every ``run:`` block of one job, concatenated."""
    return "\n".join(step.get("run", "") for step in job["steps"])


class TestActionsArePinnedToCommits:
    """SUP-100-001: a mutable major tag is a third party's write access to us."""

    def test_every_uses_is_a_full_commit_sha_with_a_version_comment(self) -> None:
        unpinned: list[str] = []
        for path in _workflow_files():
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not _ANY_USES.match(line):
                    continue
                if not _PINNED_USES.match(line):
                    unpinned.append(f"{path.name}:{number}: {line.strip()}")
        assert not unpinned, (
            "every `uses:` must be pinned to a full 40-character commit SHA "
            "with a trailing `# vX.Y.Z` comment: " + "; ".join(unpinned)
        )

    def test_the_same_action_is_pinned_to_one_sha_everywhere(self) -> None:
        """Two workflows disagreeing about a SHA means one was updated alone."""
        seen: dict[str, set[str]] = {}
        for path in _workflow_files():
            for line in path.read_text(encoding="utf-8").splitlines():
                match = _PINNED_USES.match(line)
                if match:
                    seen.setdefault(match["action"], set()).add(
                        f"{match['sha']} (v{match['version']})"
                    )
        drifted = {a: sorted(s) for a, s in seen.items() if len(s) > 1}
        assert not drifted, f"same action pinned to different commits: {drifted}"


class TestContinuousIntegrationGates:
    """SUP-100-001: the checks CLAUDE.md mandates have to actually run."""

    def test_ci_runs_on_pull_requests_and_pushes_to_main(self) -> None:
        # PyYAML parses the bare key `on:` as the boolean True.
        triggers = _load("ci.yml")[True]
        assert "pull_request" in triggers
        assert triggers["push"]["branches"] == ["main"]

    def test_ci_declares_every_required_job(self) -> None:
        jobs = _load("ci.yml")["jobs"]
        assert set(REQUIRED_CI_JOBS) <= set(jobs), (
            "these job ids are named by the branch protection rule as required "
            f"status checks: {REQUIRED_CI_JOBS}"
        )

    def test_ci_jobs_run_the_checks_they_are_named_for(self) -> None:
        jobs = _load("ci.yml")["jobs"]
        assert "pytest tests/" in _run_script(jobs["tests"])
        assert "py_compile src/*.py" in _run_script(jobs["compile"])
        audit = _run_script(jobs["audit"])
        assert "uv export --no-dev" in audit
        # `uv run pip-audit`, not `uvx pip-audit`: the auditor comes from the
        # lock file rather than from whatever the index serves that minute.
        assert "uv run pip-audit" in audit
        assert "uvx pip-audit" not in audit
        assert (
            "tests/test_review100_release_controls.py"
            in _run_script(jobs["docs-parity"])
        )

    def test_ci_grants_no_write_permission(self) -> None:
        workflow = _load("ci.yml")
        assert workflow["permissions"] == {}
        for name, job in workflow["jobs"].items():
            assert job["permissions"] == {"contents": "read"}, name


class TestReleaseJobRerunsEveryGate:
    """SUP-100-001: CI passing on some other commit says nothing about this tag."""

    def test_release_fires_only_on_version_tags(self) -> None:
        triggers = _load("release.yml")[True]
        assert triggers["push"]["tags"] == ["v*"]
        assert "branches" not in triggers["push"]

    def test_release_proves_the_tagged_commit_is_on_main(self) -> None:
        script = _run_script(_load("release.yml")["jobs"]["release"])
        assert "git fetch" in script and "origin/main" in script
        assert 'merge-base --is-ancestor "$GITHUB_SHA" origin/main' in script

    def test_release_needs_full_history_for_that_proof(self) -> None:
        """A shallow clone has no merge-base to check, so it would pass blindly."""
        checkout = [
            step
            for step in _load("release.yml")["jobs"]["release"]["steps"]
            if str(step.get("uses", "")).startswith("actions/checkout@")
        ]
        assert len(checkout) == 1
        assert checkout[0]["with"]["fetch-depth"] == 0

    def test_release_reruns_tests_compile_and_the_production_audit(self) -> None:
        script = _run_script(_load("release.yml")["jobs"]["release"])
        assert "pytest tests/ -q" in script
        assert "py_compile src/*.py" in script
        assert "uv export --no-dev" in script
        assert "uv run pip-audit" in script

    def test_release_keeps_the_version_parity_gate_and_the_attestation(self) -> None:
        job = _load("release.yml")["jobs"]["release"]
        script = _run_script(job)
        assert 'test "$TAG" = "$PKG" -a "$TAG" = "$LOG"' in script
        assert any(
            str(step.get("uses", "")).startswith("actions/attest-build-provenance@")
            for step in job["steps"]
        )

    def test_release_permissions_are_explicit_and_minimal(self) -> None:
        workflow = _load("release.yml")
        assert workflow["permissions"] == {}, "deny by default at the top level"
        assert workflow["jobs"]["release"]["permissions"] == {
            "contents": "write",
            "id-token": "write",
            "attestations": "write",
        }


class TestReleaseVersionParity:
    """DOC-100-001: the README deploy example must not drift behind the code.

    The three sources are compared against each other rather than against a
    literal, so a version bump only has to be made consistently — it does not
    have to be made here as well.
    """

    @staticmethod
    def _readme_deploy_tag() -> str:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        tags = re.findall(r"^\s*--git-branch\s+(v\d+\.\d+\.\d+)\s*\\?$", text, re.M)
        assert len(tags) == 1, (
            "expected exactly one pinned `--git-branch vX.Y.Z` in the README "
            f"production deploy example, found {tags}"
        )
        return tags[0]

    @staticmethod
    def _pyproject_tag() -> str:
        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))
        return "v" + data["project"]["version"]

    @staticmethod
    def _changelog_tag() -> str:
        text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        match = re.search(r"^## (\d+\.\d+\.\d+)", text, re.M)
        assert match is not None, "no `## X.Y.Z` heading in CHANGELOG.md"
        return "v" + match.group(1)

    @staticmethod
    def _plugin_tag() -> str:
        import json

        manifest = json.loads((REPO_ROOT / ".claude-plugin" / "plugin.json").read_text())
        return "v" + str(manifest["version"])

    @staticmethod
    def _marketplace_tag() -> str:
        import json

        manifest = json.loads(
            (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text()
        )
        (entry,) = manifest["plugins"]
        return "v" + str(entry["version"])

    def test_version_parity_of_readme_pyproject_and_changelog(self) -> None:
        readme = self._readme_deploy_tag()
        pyproject = self._pyproject_tag()
        changelog = self._changelog_tag()
        plugin = self._plugin_tag()
        marketplace = self._marketplace_tag()
        assert readme == pyproject == changelog == plugin == marketplace, (
            "the release tag in README's production deploy command, "
            "[project].version in pyproject.toml, the newest CHANGELOG.md "
            "heading and the version in both Claude Code plugin manifests "
            f"must agree: README={readme} pyproject={pyproject} "
            f"changelog={changelog} plugin.json={plugin} "
            f"marketplace.json={marketplace}. A stale README deploys code "
            "older than the release it claims to install; a stale manifest "
            "means no installed plugin ever picks the release up."
        )

    def test_version_parity_sources_are_each_found_exactly_once(self) -> None:
        """Guards the test itself: a silently unmatched regex would pass above."""
        for value in (
            self._readme_deploy_tag(),
            self._pyproject_tag(),
            self._changelog_tag(),
            self._plugin_tag(),
            self._marketplace_tag(),
        ):
            assert re.fullmatch(r"v\d+\.\d+\.\d+", value), value


class TestStartupInstanceLock:
    """ARCH-100-001: two writers must fail loudly at startup, not silently later."""

    def test_the_lock_is_held_after_acquisition(self, tmp_path: Path) -> None:
        handle = main.acquire_instance_lock(tmp_path / "instance.lock")
        try:
            assert (tmp_path / "instance.lock").exists()
            assert "pid=" in (tmp_path / "instance.lock").read_text()
        finally:
            main.release_instance_lock(handle)

    def test_a_second_acquisition_in_this_process_is_refused(
        self, tmp_path: Path
    ) -> None:
        """`--workers 2` is two open file descriptions, and flock is per-fd."""
        path = tmp_path / "instance.lock"
        first = main.acquire_instance_lock(path)
        try:
            with pytest.raises(main.SingleInstanceError) as excinfo:
                main.acquire_instance_lock(path)
            assert "Exactly one instance" in str(excinfo.value)
            assert "--workers" in str(excinfo.value)
        finally:
            main.release_instance_lock(first)

    def test_a_second_process_is_refused(self, tmp_path: Path) -> None:
        """The lock is a real OS lock, so a separate process sees it too."""
        path = tmp_path / "instance.lock"
        held = main.acquire_instance_lock(path)
        try:
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import fcntl, sys\n"
                    "handle = open(sys.argv[1], 'a+')\n"
                    "try:\n"
                    "    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                    "except OSError:\n"
                    "    print('refused')\n"
                    "else:\n"
                    "    print('acquired')\n",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert probe.stdout.strip() == "refused", probe
        finally:
            main.release_instance_lock(held)

    def test_the_lock_is_reusable_after_release(self, tmp_path: Path) -> None:
        """A clean shutdown must not leave the next container locked out."""
        path = tmp_path / "instance.lock"
        main.release_instance_lock(main.acquire_instance_lock(path))
        second = main.acquire_instance_lock(path)
        main.release_instance_lock(second)

    def test_releasing_nothing_is_harmless(self) -> None:
        main.release_instance_lock(None)

    def test_a_running_app_holds_the_lock_for_its_cache_dir(self, api) -> None:
        """The lifespan takes it; a second boot on the same disk would fail."""
        assert main.app.state.instance_lock is not None
        lock_path = (
            api.settings.cache_dir / api.settings.instance_lock_filename
        )
        with pytest.raises(main.SingleInstanceError):
            main.acquire_instance_lock(lock_path)


class TestForeignWriterLatch:
    """ARCH-100-001: the sidecar catches what the file lock cannot see."""

    @staticmethod
    def _started_db(backend: InMemoryFilesBackend, tmp_path: Path) -> StateDB:
        db = StateDB(backend, tmp_path / "state.sqlite3", 0, 10 * 1024 * 1024)
        db.start()
        return db

    def test_a_foreign_snapshot_latches_the_process(
        self, backend: InMemoryFilesBackend, tmp_path: Path
    ) -> None:
        db = self._started_db(backend, tmp_path)
        try:
            db.bump("submissions", "abc", "2026-09-01")
            assert db.snapshot_now() is True
            assert foreign_writer_detected() is None

            # A second hub, on its own disk, writes the shared Storage project.
            backend.upload(
                SNAPSHOT_FILE_NAME, b"SQLite format 3\0foreign", [TAG_STATE]
            )
            db.bump("submissions", "abc", "2026-09-01")
            db.snapshot_now()

            detail = foreign_writer_detected()
            assert detail is not None
            assert "another instance" in detail
            assert "Exactly one instance" in detail
        finally:
            db.stop()

    def test_the_foreign_snapshot_is_not_deleted(
        self, backend: InMemoryFilesBackend, tmp_path: Path
    ) -> None:
        """Retiring it is exactly the data loss the detector exists to prevent."""
        db = self._started_db(backend, tmp_path)
        try:
            db.bump("submissions", "abc", "2026-09-01")
            assert db.snapshot_now() is True
            foreign_id = backend.upload(
                SNAPSHOT_FILE_NAME, b"SQLite format 3\0foreign", [TAG_STATE]
            )
            db.bump("submissions", "abc", "2026-09-01")
            db.snapshot_now()

            surviving = {info.id for info in backend.search_by_tag(TAG_STATE)}
            assert foreign_id in surviving
        finally:
            db.stop()

    def test_writes_are_refused_once_latched(
        self, backend: InMemoryFilesBackend, tmp_path: Path
    ) -> None:
        db = self._started_db(backend, tmp_path)
        try:
            note_foreign_writer("another instance is writing this hub's state")
            with pytest.raises(ForeignWriterError):
                db.bump("submissions", "abc", "2026-09-01")
            with pytest.raises(ForeignWriterError):
                db.record_view("abc", "2026-09-01", "page")
            with pytest.raises(ForeignWriterError):
                db.forget_artifact("abc")
            with pytest.raises(ForeignWriterError):
                db.prune_counters_before("2026-09-01")
            # Reads of the counter pair are refused too, so a limit is never
            # compared against a tally its increments no longer reach.
            with pytest.raises(ForeignWriterError):
                db.count("submissions", "abc", "2026-09-01")
        finally:
            db.stop()

    def test_snapshots_stop_once_latched(
        self, backend: InMemoryFilesBackend, tmp_path: Path
    ) -> None:
        db = self._started_db(backend, tmp_path)
        try:
            db.bump("submissions", "abc", "2026-09-01")
            assert db.snapshot_now() is True
            before = {info.id for info in backend.search_by_tag(TAG_STATE)}

            note_foreign_writer("another instance is writing this hub's state")
            assert db.snapshot_now() is False
            assert {info.id for info in backend.search_by_tag(TAG_STATE)} == before
        finally:
            db.stop()

    def test_analytics_reads_still_work_once_latched(
        self, backend: InMemoryFilesBackend, tmp_path: Path
    ) -> None:
        """views() is exempt: stale numbers beat a 500 on the owner's page."""
        db = self._started_db(backend, tmp_path)
        try:
            db.record_view("abc", "2026-09-01", "page")
            note_foreign_writer("another instance is writing this hub's state")
            assert db.views("abc")["total"] == 1
        finally:
            db.stop()

    def test_stopping_a_latched_database_does_not_raise(
        self, backend: InMemoryFilesBackend, tmp_path: Path
    ) -> None:
        db = self._started_db(backend, tmp_path)
        db.bump("submissions", "abc", "2026-09-01")
        note_foreign_writer("another instance is writing this hub's state")
        db.stop()  # must not raise: shutdown has to complete


class TestReadinessOnForeignWriter:
    """ARCH-100-001: the broken invariant has to be visible over HTTP."""

    def test_health_is_200_normally(self, api) -> None:
        assert api.client.get("/health").status_code == 200

    def test_health_is_503_after_a_foreign_writer_is_detected(self, api) -> None:
        database = main.app.state.statedb
        database.bump("submissions", "abc", "2026-09-01")
        assert database.snapshot_now() is True
        api.backend.upload(
            SNAPSHOT_FILE_NAME, b"SQLite format 3\0foreign", [TAG_STATE]
        )
        database.bump("submissions", "abc", "2026-09-01")
        database.snapshot_now()

        resp = api.client.get("/health")
        assert resp.status_code == 503, resp.text
        body = resp.json()
        assert body["status"] == "unready"
        assert "Exactly one instance" in body["detail"]

    def test_the_platform_startup_probe_stays_200(self, api) -> None:
        """CLAUDE.md rule 3: failing POST / only restarts into the same conflict."""
        note_foreign_writer("another instance is writing this hub's state")
        resp = api.client.post("/")
        assert resp.status_code == 200
        assert resp.text == "OK"

    def test_a_counter_degrades_instead_of_erroring_a_request(self, api) -> None:
        """main.py already treats a sidecar failure as "use the fallback tally"."""
        note_foreign_writer("another instance is writing this hub's state")
        resp = api.client.post(
            "/api/artifacts",
            json={"markdown": "# still serving"},
            headers={"X-StorageApi-Token": "good-token", "X-Kbc-Stack": "us"},
        )
        assert resp.status_code == 201, resp.text


def test_fcntl_flock_conflicts_across_open_file_descriptions() -> None:
    """The platform assumption the startup lock rests on, asserted directly.

    ``flock`` associates the lock with the open file description rather than
    with the process, which is what makes a second ``open()`` conflict even
    inside one interpreter. If a future platform changed that, every test above
    would still pass while the lock protected nothing.
    """
    path = Path(__file__).with_suffix(".flockprobe")
    first = path.open("a+")
    second = path.open("a+")
    try:
        fcntl.flock(first.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(OSError):
            fcntl.flock(second.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        fcntl.flock(first.fileno(), fcntl.LOCK_UN)
        first.close()
        second.close()
        path.unlink(missing_ok=True)
