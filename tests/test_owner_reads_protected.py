"""The owning project reads its own password-protected artifact without the password.

The reader password is protection for *shared-link* access: whoever holds the
link needs the password too. It was never a boundary against the owning
project -- the owner can ``clear_password`` at will and keeps the canonical
copy in their own Storage -- yet ``reader_allowed`` knew only the unlock
cookie and ``X-Artifact-Password``, so the owner-only admin studio, which
opens an artifact through ``GET /a/{id}/versions`` with the owner's headers,
got 401 and could not manage a protected artifact at all.

These tests pin the rule agreed in the design review (GPT Astra, 2026-09-08):

- a *verified* credential of the owning project satisfies the gate, on every
  reader route, exports included; other projects, proposal authors and guest
  invitations do not;
- the owner check runs after the cookie and *before* the password path, so a
  wrong or stale password header costs the owner no PBKDF2, records no
  failure and an exhausted password budget cannot lock the owner out;
- the caller is resolved at most once per request and reused by the proposal
  visibility check;
- an unverifiable credential (bad token, unreachable stack) is "no identity":
  the request falls through to the password path and ends in the route's
  ordinary locked answer, never a 5xx;
- the bypass mints no unlock cookie.
"""

from __future__ import annotations

import src.main as main
from src.auth import StackUnreachableError
from tests.test_api import (
    AUTH_HEADERS,
    OTHER_AUTH_HEADERS,
    SESSION_HEADERS,
    Api,
    _guest_headers,
    _invite,
    _low_unlock_limit,
    _publish_markdown,
    api,  # noqa: F401 - the fixture this module runs on
)

PASSWORD = "hunter22"

#: Every reader route behind the gate, with the status a locked caller gets.
READER_ROUTES = (
    ("", 401),
    ("/v/1", 401),
    ("/raw", 401),
    ("/source", 401),
    ("/versions", 401),
    ("/comments", 401),
    ("/export/markdown", 401),
    ("/export/vault", 401),
)


def _counting(monkeypatch, name: str) -> dict[str, int]:
    """Wrap ``main.<name>`` so a test can assert how often it ran."""
    calls = {"n": 0}
    original = getattr(main, name)

    def counted(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(main, name, counted)
    return calls


def test_owner_token_reads_every_protected_route_without_the_password(
    api: Api,
) -> None:
    artifact_id = _publish_markdown(api, "# Secret", password=PASSWORD)
    for suffix, _locked in READER_ROUTES:
        resp = api.client.get(f"/a/{artifact_id}{suffix}", headers=AUTH_HEADERS)
        assert resp.status_code == 200, suffix


def test_owner_session_reads_the_protected_artifact_too(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Secret", password=PASSWORD)
    resp = api.client.get(f"/a/{artifact_id}/versions", headers=SESSION_HEADERS)
    assert resp.status_code == 200


def test_another_project_does_not_bypass(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Secret", password=PASSWORD)
    for suffix, locked in READER_ROUTES:
        resp = api.client.get(
            f"/a/{artifact_id}{suffix}", headers=OTHER_AUTH_HEADERS
        )
        assert resp.status_code == locked, suffix


def test_a_guest_invitation_does_not_bypass(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Secret", password=PASSWORD)
    invited = _invite(api, artifact_id).json()
    guest = _guest_headers(invited["review_url"])
    assert api.client.get(f"/a/{artifact_id}/comments", headers=guest).status_code == 401
    # The invitation plus the password still works, exactly as before.
    unlocked = api.client.get(
        f"/a/{artifact_id}/comments",
        headers={**guest, "X-Artifact-Password": PASSWORD},
    )
    assert unlocked.status_code == 200


def test_owner_with_a_wrong_password_header_costs_no_pbkdf2_and_no_failure(
    api: Api, monkeypatch
) -> None:
    artifact_id = _publish_markdown(api, "# Secret", password=PASSWORD)
    checks = _counting(monkeypatch, "check_password")
    failures = _counting(monkeypatch, "_record_unlock_failure")
    resp = api.client.get(
        f"/a/{artifact_id}/raw",
        headers={**AUTH_HEADERS, "X-Artifact-Password": "stale"},
    )
    assert resp.status_code == 200
    assert checks["n"] == 0
    assert failures["n"] == 0


def test_an_exhausted_password_budget_cannot_lock_the_owner_out(
    api: Api, monkeypatch
) -> None:
    artifact_id = _publish_markdown(api, "# Secret", password=PASSWORD)
    _low_unlock_limit(api, monkeypatch, limit=1)
    assert (
        api.client.get(
            f"/a/{artifact_id}/raw", headers={"X-Artifact-Password": "nope"}
        ).status_code
        == 401
    )
    # Budget gone: the anonymous path is now 429 even with a stale header...
    assert (
        api.client.get(
            f"/a/{artifact_id}/raw", headers={"X-Artifact-Password": "nope"}
        ).status_code
        == 429
    )
    # ...but the owner is checked before the throttle is ever consulted.
    resp = api.client.get(
        f"/a/{artifact_id}/raw",
        headers={**AUTH_HEADERS, "X-Artifact-Password": "nope"},
    )
    assert resp.status_code == 200


def test_unreachable_stack_falls_through_to_the_locked_answer_not_a_5xx(
    api: Api, monkeypatch
) -> None:
    artifact_id = _publish_markdown(api, "# Secret", password=PASSWORD)

    def unreachable(*args, **kwargs):
        raise StackUnreachableError("stack down")

    monkeypatch.setattr(main, "verify_token", unreachable)
    locked = api.client.get(f"/a/{artifact_id}/raw", headers=AUTH_HEADERS)
    assert locked.status_code == 401
    assert locked.json()["error"] == "password required"
    page = api.client.get(f"/a/{artifact_id}", headers=AUTH_HEADERS)
    assert page.status_code == 401
    assert "password" in page.text.lower()
    # The password still works while the stack is down.
    ok = api.client.get(
        f"/a/{artifact_id}/raw",
        headers={**AUTH_HEADERS, "X-Artifact-Password": PASSWORD},
    )
    assert ok.status_code == 200


def test_gate_never_verifies_when_the_artifact_is_not_protected(
    api: Api, monkeypatch
) -> None:
    artifact_id = _publish_markdown(api, "# Open")
    verifies = _counting(monkeypatch, "verify_token")
    assert api.client.get(f"/a/{artifact_id}/raw", headers=AUTH_HEADERS).status_code == 200
    assert verifies["n"] == 0


def test_gate_never_verifies_when_the_unlock_cookie_is_valid(
    api: Api, monkeypatch
) -> None:
    artifact_id = _publish_markdown(api, "# Secret", password=PASSWORD)
    unlock = api.client.post(
        f"/a/{artifact_id}/unlock", data={"password": PASSWORD}, follow_redirects=False
    )
    assert unlock.status_code == 303
    verifies = _counting(monkeypatch, "verify_token")
    assert api.client.get(f"/a/{artifact_id}/raw", headers=AUTH_HEADERS).status_code == 200
    assert verifies["n"] == 0


def test_the_caller_is_verified_once_per_request(api: Api, monkeypatch) -> None:
    """The gate and the proposal-visibility check share one verification."""
    artifact_id = _publish_markdown(api, "# Secret", password=PASSWORD)
    verifies = _counting(monkeypatch, "verify_token")
    # /a/{id}/v/1 runs the gate and then may_see(); /versions runs the gate and
    # resolves the caller for proposal rows.
    assert api.client.get(f"/a/{artifact_id}/v/1", headers=AUTH_HEADERS).status_code == 200
    assert verifies["n"] == 1
    assert api.client.get(f"/a/{artifact_id}/versions", headers=AUTH_HEADERS).status_code == 200
    assert verifies["n"] == 2


def test_owner_bypass_mints_no_unlock_cookie(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Secret", password=PASSWORD)
    resp = api.client.get(f"/a/{artifact_id}", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    assert "set-cookie" not in resp.headers
    # A plain browser visit by the same owner, without headers, is still locked.
    assert api.client.get(f"/a/{artifact_id}").status_code == 401


def test_password_required_hint_names_the_owner_route_in(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Secret", password=PASSWORD)
    body = api.client.get(f"/a/{artifact_id}/raw").json()
    assert body["error"] == "password required"
    assert "X-Artifact-Password" in body["hint"]
    assert "owning" in body["hint"]
