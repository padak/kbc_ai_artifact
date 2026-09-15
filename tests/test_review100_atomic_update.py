"""Fail-closed write ordering for ``PUT /api/artifacts/{id}`` (SEC-100-001).

The v0.10.0 security review confirmed ``SEC-100-001`` -- the same defect the
v0.7.5 review tracked as ``REL-075-004``, re-opened as REGRESSED: a single PUT
that carries both new content and new settings had no ordering that was safe
in both directions.

* v0.7.5 wrote the settings first, so a failed content write left the new
  password/policy/status in force after a 502 the caller read as "nothing
  happened".
* v0.10.0 wrote the content first, so a failed ``save_meta`` left the new --
  possibly confidential -- version as the *public head* under the old, looser
  policy. That is strictly worse: the resulting state is less restrictive than
  both the previous state and the requested one.

The fix splits the settings by direction and commits them around the content:
tightening first, then the version, then loosening. This module pins the
resulting contract: **after any partial failure the publicly reachable state
is never less restrictive than the last durable commit.**

Every case is checked through the anonymous public routes (``/a/{id}/raw`` and
the wrapper page), because that -- not the owner's view of ``meta`` -- is what
an attacker sees.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from fastapi import HTTPException

import src.main as main
from src.kbc import BackendError

# The shared FastAPI fixture stack. ``api`` transitively needs ``settings``
# from tests/conftest.py, which pytest resolves by name.
from tests.test_api import AUTH_HEADERS, Api, api  # noqa: F401


#: Marker in the baseline (v1) document. Public from the start.
OLD_MARKER = "PUBLIC BASELINE PARAGRAPH"
#: Marker in the replacement (v2) document, which every failing case must keep
#: away from an anonymous reader unless it is live *and* ungated.
NEW_MARKER = "CONFIDENTIAL REPLACEMENT PARAGRAPH"

BASELINE_MARKDOWN = f"# Baseline\n\n{OLD_MARKER}\n"
REPLACEMENT_MARKDOWN = f"# Replacement\n\n{NEW_MARKER}\n"


# --------------------------------------------------------------------------
# Failure injection
# --------------------------------------------------------------------------


class FailingSaveMeta:
    """Wrap ``store.save_meta`` and raise on the ``fail_on``-th call.

    Records the meta object handed to every call, so a test can assert *what*
    each durable commit carried and not merely how many there were. The route
    hands a fresh ``dataclasses.replace`` copy to each save, so the recorded
    objects stay distinct snapshots.
    """

    def __init__(self, store: Any, fail_on: int | None) -> None:
        self._real = store.save_meta
        self._fail_on = fail_on
        self.calls: list[Any] = []

    def __call__(self, meta: Any) -> None:
        self.calls.append(meta)
        if self._fail_on is not None and len(self.calls) == self._fail_on:
            raise BackendError("simulated meta write failure")
        return self._real(meta)


@pytest.fixture(autouse=True)
def _restore_module_patches():
    """Put ``src.main._store_canonical`` back, whatever a test did to it.

    The injections below are installed by hand rather than through
    ``monkeypatch``, whose ``undo()`` would also roll back the ``api``
    fixture's own patches (``verify_token`` among them, so every later
    authenticated call would 401), and these tests need the app to keep working
    *after* the injected failure. Instance attributes on the store die with the
    per-test store; this module-level one needs putting back explicitly.
    """
    original = main._store_canonical
    yield
    main._store_canonical = original


def _install_save_meta(fail_on: int | None) -> FailingSaveMeta:
    store = main.app.state.store
    spy = FailingSaveMeta(store, fail_on)
    store.save_meta = spy
    return spy


def _break_add_version() -> None:
    def boom(_envelope):
        raise BackendError("simulated version write failure")

    main.app.state.store.add_version_next = boom


def _break_canonical() -> None:
    """Fail the way the real helper fails: a 502 HTTPException, not a 500."""

    def boom(*_args, **_kwargs):
        raise HTTPException(
            status_code=502,
            detail="could not store canonical copy in your project",
        )

    main._store_canonical = boom


def _stop_injecting() -> None:
    """Drop every injected failure, leaving the fixture's own patches alone."""
    store = main.app.state.store
    store.__dict__.pop("save_meta", None)
    store.__dict__.pop("add_version_next", None)
    main._store_canonical = _REAL_STORE_CANONICAL


#: Captured at import, before any test has a chance to replace it.
_REAL_STORE_CANONICAL = main._store_canonical


# --------------------------------------------------------------------------
# Public-state helpers
# --------------------------------------------------------------------------


def _publish(api: Api, **extra: Any) -> str:
    payload: dict[str, Any] = {"markdown": BASELINE_MARKDOWN}
    payload.update(extra)
    resp = api.client.post("/api/artifacts", json=payload, headers=AUTH_HEADERS)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _update(api: Api, artifact_id: str, **fields: Any):
    return api.client.put(
        f"/api/artifacts/{artifact_id}", json=fields, headers=AUTH_HEADERS
    )


def _assert_gated(api: Api, artifact_id: str) -> None:
    """A password is in force: no anonymous route may serve either document."""
    raw = api.client.get(f"/a/{artifact_id}/raw")
    page = api.client.get(f"/a/{artifact_id}")
    assert raw.status_code == 401, raw.text
    assert NEW_MARKER not in raw.text
    assert OLD_MARKER not in raw.text
    assert NEW_MARKER not in page.text
    assert OLD_MARKER not in page.text


def _assert_serves(api: Api, artifact_id: str, marker: str, *, absent: str) -> None:
    """No password: exactly ``marker`` is anonymously reachable."""
    raw = api.client.get(f"/a/{artifact_id}/raw")
    assert raw.status_code == 200, raw.text
    assert marker in raw.text
    assert absent not in raw.text
    page = api.client.get(f"/a/{artifact_id}")
    assert page.status_code == 200, page.text


# --------------------------------------------------------------------------
# The review probe, ported to assert the fixed behaviour
# --------------------------------------------------------------------------


def test_failed_passworded_content_update_does_not_expose_the_new_head(
    api: Api
) -> None:
    """SEC-100-001 probe, inverted to the behaviour now required.

    The review's ``test_failed_passworded_content_update_exposes_new_head``
    asserted the bug: 502, head at v2, ``password_set=False`` and an anonymous
    ``/raw`` returning the confidential marker with 200. With the password
    committed *before* the content, a dead ``save_meta`` never gets as far as
    publishing anything.
    """
    artifact_id = _publish(api)
    _install_save_meta(fail_on=1)

    update = _update(
        api,
        artifact_id,
        markdown=REPLACEMENT_MARKDOWN,
        password="new-reader-password",
    )
    _stop_injecting()

    store = main.app.state.store
    meta = store.get_meta(artifact_id)
    head = store.get_head(artifact_id)

    assert update.status_code == 502, update.text
    assert head.version == 1
    assert meta.password is None
    _assert_serves(api, artifact_id, OLD_MARKER, absent=NEW_MARKER)


# --------------------------------------------------------------------------
# Failure injection at every persistence step, for every settings direction
# --------------------------------------------------------------------------

#: name -> (publish kwargs, update settings, direction the route must derive)
CASES: dict[str, tuple[dict[str, Any], dict[str, Any], str]] = {
    "content_and_password": ({}, {"password": "reader-pw"}, "tighten"),
    "content_and_clear_password": (
        {"password": "old-pw"},
        {"clear_password": True},
        "loosen",
    ),
    "content_and_policy_tighten": (
        {"accept_versions": True},
        {"accept_versions_mode": "off"},
        "tighten",
    ),
    "content_and_policy_loosen": ({}, {"accept_versions_mode": "anyone"}, "loosen"),
    "content_and_status_final": ({}, {"status": "final"}, "tighten"),
    "content_only": ({}, {}, "none"),
}

STEPS = ("meta1", "meta2", "version", "canonical")


@pytest.mark.parametrize("case_name", sorted(CASES))
@pytest.mark.parametrize("step", STEPS)
def test_partial_failure_never_loosens_public_state(
    api: Api, case_name: str, step: str
) -> None:
    """The core contract, across every persistence step and settings mix.

    A single PUT holds at most three durable commits -- tightening settings,
    the version, loosening settings -- and this walks a simulated backend
    failure through each of them.
    """
    publish_kwargs, update_settings, direction = CASES[case_name]
    artifact_id = _publish(api, **publish_kwargs)
    protected = "password" in publish_kwargs

    spy = None
    if step in ("meta1", "meta2"):
        spy = _install_save_meta(fail_on=1 if step == "meta1" else 2)
    elif step == "version":
        _break_add_version()
    else:
        _break_canonical()

    resp = _update(api, artifact_id, markdown=REPLACEMENT_MARKDOWN, **update_settings)
    _stop_injecting()

    store = main.app.state.store
    meta = store.get_meta(artifact_id)
    head = store.get_head(artifact_id)

    if step == "meta2":
        # None of these cases needs a second settings commit: a pure
        # tightening or a pure loosening is one save either way. The request
        # therefore has to succeed, which also pins "no redundant write".
        assert resp.status_code == 200, resp.text
        assert head.version == 2
        assert len(spy.calls) == 1
        if case_name == "content_and_password":
            _assert_gated(api, artifact_id)
        else:
            _assert_serves(api, artifact_id, NEW_MARKER, absent=OLD_MARKER)
        return

    assert resp.status_code == 502, resp.text
    detail = resp.json().get("detail", "")

    if step == "meta1" and direction != "tighten":
        # The only settings commit a loosening (or settings-free) update makes
        # is the one *after* the content, so the version is legitimately live.
        # What must not have happened is the widening.
        assert head.version == 2
        assert "published" in detail, detail
        if direction == "loosen":
            assert "not applied" in detail, detail
        if protected:
            # clear_password did not take: the gate is still up over the new
            # content, which is the whole point.
            assert meta.password is not None
            _assert_gated(api, artifact_id)
        else:
            assert meta.accept_versions_mode == "off"
            _assert_serves(api, artifact_id, NEW_MARKER, absent=OLD_MARKER)
        return

    # Every remaining combination must leave the artifact exactly as it was.
    assert head.version == 1
    assert (meta.password is not None) is protected
    assert meta.status == "draft"
    if protected:
        _assert_gated(api, artifact_id)
    else:
        _assert_serves(api, artifact_id, OLD_MARKER, absent=NEW_MARKER)

    if direction == "tighten" and step in ("version", "canonical"):
        # The tightening commit really happened and really was undone, and the
        # 502 has to say which of the two it is rather than "storage failed".
        assert "in force" in detail, detail


# --------------------------------------------------------------------------
# A request that tightens *and* loosens at once
# --------------------------------------------------------------------------


def test_mixed_direction_update_commits_tightening_before_the_content(
    api: Api
) -> None:
    """Only a request that does both needs two settings commits, in order."""
    artifact_id = _publish(api, password="old-pw")
    spy = _install_save_meta(fail_on=None)

    resp = _update(
        api,
        artifact_id,
        markdown=REPLACEMENT_MARKDOWN,
        clear_password=True,
        status="final",
    )
    _stop_injecting()

    assert resp.status_code == 200, resp.text
    assert len(spy.calls) == 2, "tightening and loosening must be separate commits"
    first, second = spy.calls
    # The pre-content commit carries the freeze and keeps the password: it is
    # the strictly more restrictive of the two states.
    assert first.status == "final"
    assert first.password is not None
    assert second.status == "final"
    assert second.password is None


def test_failed_loosening_leaves_the_tightened_settings_in_force(
    api: Api
) -> None:
    """(c) fails after (b): content is live, but only under the tight state."""
    artifact_id = _publish(api, password="old-pw")
    _install_save_meta(fail_on=2)

    resp = _update(
        api,
        artifact_id,
        markdown=REPLACEMENT_MARKDOWN,
        clear_password=True,
        status="final",
    )
    _stop_injecting()

    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert "published" in detail and "not applied" in detail, detail

    store = main.app.state.store
    meta = store.get_meta(artifact_id)
    assert store.get_head(artifact_id).version == 2
    assert meta.status == "final"
    assert meta.password is not None, "clear_password must not have taken"
    _assert_gated(api, artifact_id)


def test_failed_content_rolls_back_the_tightening_half_only(
    api: Api
) -> None:
    """(b) fails after (a): the tightening is undone, the loosening never ran."""
    artifact_id = _publish(api, password="old-pw")
    spy = _install_save_meta(fail_on=None)
    _break_add_version()

    resp = _update(
        api,
        artifact_id,
        markdown=REPLACEMENT_MARKDOWN,
        clear_password=True,
        status="final",
    )
    _stop_injecting()

    assert resp.status_code == 502, resp.text
    store = main.app.state.store
    meta = store.get_meta(artifact_id)
    assert store.get_head(artifact_id).version == 1
    assert meta.status == "draft", "the freeze was not rolled back"
    assert meta.password is not None
    # Commit (a), then the rollback of (a). The loosening commit is never
    # reached, so clear_password cannot leak out of a failed request.
    assert len(spy.calls) == 2
    assert spy.calls[0].status == "final"
    assert spy.calls[1].status == "draft"
    assert spy.calls[1].password is not None
    _assert_gated(api, artifact_id)


def test_unrollable_tightening_is_reported_as_still_in_force(
    api: Api
) -> None:
    """When the rollback itself fails, the 502 must say so rather than lie.

    Retained tightening is an acceptable outcome -- it is more restrictive than
    both the old and the requested state -- but the owner has to be told, or
    they will believe a failed request changed nothing.
    """
    artifact_id = _publish(api)
    # Commit (a) succeeds; the rollback attempt after (b) fails is the second
    # save and is refused, so the freeze survives.
    _install_save_meta(fail_on=2)
    _break_add_version()

    resp = _update(api, artifact_id, markdown=REPLACEMENT_MARKDOWN, status="final")
    _stop_injecting()

    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert "could not be rolled back" in detail, detail
    assert "in force" in detail, detail

    store = main.app.state.store
    assert store.get_head(artifact_id).version == 1
    assert store.get_meta(artifact_id).status == "final"
    # More restrictive than before, never less: the old content still serves,
    # the new one was never published.
    _assert_serves(api, artifact_id, OLD_MARKER, absent=NEW_MARKER)


def test_reopening_a_final_artifact_with_content_stays_frozen_if_it_fails(
    api: Api
) -> None:
    """The frozen escape hatch is a loosening, so it commits last.

    ``PUT {"status": "draft", "markdown": ...}`` on a final artifact is the one
    documented way to reopen and re-publish in a single call. Un-finalizing
    widens who may contribute, so it lands after the version: a failure there
    leaves the new content live under the *freeze*, never a reopened document
    the owner was told had failed.
    """
    artifact_id = _publish(api)
    assert _update(api, artifact_id, status="final").status_code == 200
    _install_save_meta(fail_on=1)

    resp = _update(api, artifact_id, markdown=REPLACEMENT_MARKDOWN, status="draft")
    _stop_injecting()

    assert resp.status_code == 502, resp.text
    assert "not applied" in resp.json()["detail"], resp.text
    store = main.app.state.store
    assert store.get_head(artifact_id).version == 2
    assert store.get_meta(artifact_id).status == "final", "the freeze was lifted"
    # A frozen artifact still refuses contributions from everyone, so the
    # failed reopen really did leave the tighter state in force.
    assert _update(api, artifact_id, markdown="# Third").status_code == 409


# --------------------------------------------------------------------------
# Not an API change
# --------------------------------------------------------------------------


def test_successful_mixed_update_returns_the_unchanged_response_body(
    api: Api,
) -> None:
    """A request that fully succeeds must look exactly as it did before.

    The split is a persistence-ordering change, not a contract change: same
    status code, same keys, same values, same single new version.
    """
    artifact_id = _publish(api, password="old-pw", accept_versions=True)

    resp = _update(
        api,
        artifact_id,
        markdown=REPLACEMENT_MARKDOWN,
        title="Replacement",
        password="new-pw",
        accept_versions_mode="anyone",
        comments_mode="off",
        status="final",
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["id"] == artifact_id
    assert body["share_id"] == artifact_id
    assert body["title"] == "Replacement"
    assert body["protected"] is True
    assert body["accept_versions"] is True
    assert body["accept_versions_mode"] == "anyone"
    assert body["contributors"] == []
    assert body["comments_mode"] == "off"
    assert body["artifact_status"] == "final"
    assert body["webhooks"] == []
    assert body["version"] == 2
    assert body["status"] == "live"
    assert body["head_version"] == 2
    assert body["owner_project_id"] == 123
    assert body["canonical_file_id"] is not None
    assert body["url"].endswith(f"/a/{artifact_id}")
    # Exactly the key set the endpoint returned before the reordering.
    assert set(body) == {
        "id",
        "share_id",
        "title",
        "protected",
        "accept_versions",
        "accept_versions_mode",
        "contributors",
        "comments_mode",
        "artifact_status",
        "webhooks",
        "version",
        "status",
        "head_version",
        "owner_project_id",
        "canonical_file_id",
        # Added with hosted design systems: the provenance this version
        # claims, or null. Still not a contract change to the ordering.
        "design_system",
        # Added with the reader menu (0.18.0): whether the hub's corner menu
        # is shown on this artifact's page. Owner-facing chrome, not ordering.
        "reader_menu",
        *main.artifact_urls("https://testserver", artifact_id),
    }


def test_settings_only_update_still_makes_a_single_commit(
    api: Api
) -> None:
    """No content in the request means no ordering problem, and one save."""
    artifact_id = _publish(api)
    spy = _install_save_meta(fail_on=None)

    resp = _update(
        api, artifact_id, password="reader-pw", accept_versions_mode="anyone"
    )
    _stop_injecting()

    assert resp.status_code == 200, resp.text
    assert len(spy.calls) == 1
    meta = main.app.state.store.get_meta(artifact_id)
    assert meta.password is not None
    assert meta.accept_versions_mode == "anyone"
    _assert_gated(api, artifact_id)


def test_content_only_update_that_cannot_record_its_timestamp_says_so(
    api: Api
) -> None:
    """The content is live and access untouched; the message must not imply a
    settings change was lost."""
    artifact_id = _publish(api)
    _install_save_meta(fail_on=1)

    resp = _update(api, artifact_id, markdown=REPLACEMENT_MARKDOWN)
    _stop_injecting()

    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert "access is unchanged" in detail, detail
    assert main.app.state.store.get_head(artifact_id).version == 2
    _assert_serves(api, artifact_id, NEW_MARKER, absent=OLD_MARKER)


# --------------------------------------------------------------------------
# The classification itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field_name", "before", "after", "tightening"),
    [
        # A gate going up, or an existing credential being replaced, never
        # admits anyone new. Only removing the gate does.
        ("password", None, {"hash": "x"}, True),
        ("password", {"hash": "x"}, {"hash": "y"}, True),
        ("password", {"hash": "x"}, None, False),
        ("password", None, None, True),
        # off < allowlist < anyone.
        ("accept_versions_mode", "anyone", "off", True),
        ("accept_versions_mode", "anyone", "allowlist", True),
        ("accept_versions_mode", "allowlist", "off", True),
        ("accept_versions_mode", "off", "anyone", False),
        ("accept_versions_mode", "allowlist", "anyone", False),
        ("comments_mode", "anyone", "off", True),
        ("comments_mode", "allowlist", "off", True),
        ("comments_mode", "off", "allowlist", False),
        ("comments_mode", "off", "anyone", False),
        # Freezing narrows; reopening widens.
        ("status", "draft", "final", True),
        ("status", "final", "draft", False),
        ("status", "final", "trashed", True),
        # A list that only loses entries is a narrowing; one that gains any is
        # not, however many it also drops.
        ("contributors", ["a", "b"], ["a"], True),
        ("contributors", ["a", "b"], [], True),
        ("contributors", ["a"], ["a", "b"], False),
        ("contributors", ["a"], ["b"], False),
        ("webhooks", ["https://a/x", "https://b/y"], ["https://a/x"], True),
        ("webhooks", ["https://a/x"], ["https://a/x", "https://b/y"], False),
        # Fail-closed default for anything not enumerated.
        ("something_new", "old", "new", True),
    ],
)
def test_settings_direction_classification(
    field_name: str, before: Any, after: Any, tightening: bool
) -> None:
    """SEC-100-001: each setting's direction is a deliberate, pinned decision.

    Getting one of these backwards is exactly the fail-open the finding is
    about, so the table is asserted directly and not only through the route.
    """
    assert main._is_tightening(field_name, before, after) is tightening


def test_every_setting_the_route_can_change_is_classified(api: Api) -> None:
    """No access-relevant field may slip past ``_ACCESS_SETTINGS`` unnoticed.

    Drives a PUT that touches every settings field the endpoint accepts and
    diffs the stored meta record: anything that moved and is not on the list
    would be committed in the loosening step by default, i.e. silently exempt
    from the ordering guarantee.
    """
    artifact_id = _publish(api)
    store = main.app.state.store
    before = dataclasses.asdict(store.get_meta(artifact_id))

    resp = _update(
        api,
        artifact_id,
        password="reader-pw",
        accept_versions_mode="allowlist",
        contributors=["999@connection.keboola.com"],
        comments_mode="off",
        status="final",
    )
    assert resp.status_code == 200, resp.text
    after = dataclasses.asdict(store.get_meta(artifact_id))

    changed = {name for name in before if before[name] != after[name]}
    # "updated_at" grants nobody anything and "accept_versions" is a derived
    # read of "accept_versions_mode" (see ArtifactMeta), so neither belongs on
    # the list; everything else that moved must be on it.
    changed -= {"updated_at", "accept_versions"}
    unclassified = sorted(changed - set(main._ACCESS_SETTINGS))
    assert not unclassified, f"unclassified access-relevant settings: {unclassified}"
    # "webhooks" is the one settable field this request cannot exercise (its
    # validation resolves DNS), so its membership is asserted directly.
    assert "webhooks" in main._ACCESS_SETTINGS


def test_a_tightening_put_still_carries_a_non_access_setting(api: Api) -> None:
    """Content + tightening + reader_menu: the flag must not be dropped.

    A request with a tightening change and no loosening one commits only the
    ``_tightening_half`` copy, which is built from ``_ACCESS_SETTINGS``.
    ``reader_menu`` is deliberately not an access setting (it changes what the
    wrapper page shows, never who may read or write), so it has to be carried
    across explicitly — otherwise the owner gets a 200 for a change that was
    silently thrown away.
    """
    artifact_id = _publish(api)

    resp = _update(
        api,
        artifact_id,
        markdown=REPLACEMENT_MARKDOWN,
        password="new-pw",
        reader_menu=False,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["reader_menu"] is False

    persisted = api.client.app.state.store.get_meta(artifact_id)
    assert persisted is not None
    assert persisted.reader_menu is False
    # The tightening itself still took, and nothing widened.
    assert persisted.password is not None
