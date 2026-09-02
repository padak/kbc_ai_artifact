"""Regression tests for the v0.12.0 review of the sign-in feature.

One module per finding the review raised, in the order they were reported.
Nothing here touches a real Keboola stack: the two stack-facing groups drive
:mod:`respx`, and the browser-side group runs the page's own JavaScript under
``node`` (skipped when node is absent, exactly like
``test_api.test_every_inline_script_parses``).

The findings pinned here:

* **X-Storage-Project is ignored with a Storage API token.** It was compared
  against the resolved project for *either* credential, so a caller who kept
  the header exported for a sign-in and then used a Storage token of another
  project got a 401 the docs said could not happen.
* **Sign-out has a rate-limit budget of its own.** It shared the ``login``
  bucket with starting and renewing, so a spent sign-in budget turned every
  sign-out into a silent no-op while a live session stayed on the stack.
* **Sign-in budgets are counted in this process, once per attempt.** They were
  read-then-bumped (against the accounting contract every other budget here
  follows) through the state sidecar, which meant a ~180-poll device sign-in
  dirtied the StateDB 180 times for rows nothing will ever read again.
* **A PKCE registry always holds at least one login.** ``max_pending=0``
  turned the eviction loop into ``min()`` over an empty dict.
* **Any 2xx from a stack's auth API is a success.** A 204 from
  ``token/revoke`` was reported as a failed revocation.
* **A half-built Storage client is never cached** (``src/kbc.py``).
* **One shared session module.** ``/admin``, the review page and ``/login``
  each had their own copy of reading, renewing and revoking the credential;
  the copies had started to drift, and the two fixes below would otherwise
  have had to land twice.
* **Renewal is deduplicated and merges.** Two concurrent 401s each renewed,
  and the stack spends a refresh token on the first exchange, so the second
  was told a live session was dead. A renewal also rebuilt the record from a
  fixed field list, silently dropping anything else it held.
* **A password-gate 401 is not a session failure.** ``api()`` renewed on any
  401 before reading the body, so every write to a locked artifact spent a
  refresh token to be answered with the same 401.
* **A device poll survives a transient failure.** Any error abandoned a device
  code that was valid for another quarter of an hour, and ``slow_down`` did
  not lengthen the interval.
* **A throttled stack is not a refusal.** A 429 from a stack was translated
  into the same 400 a decline gets, so the fix above could not tell "ask
  again" from "start over" — it now answers 429.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import pathlib
import shutil
import subprocess
import tempfile

import httpx
import pytest
import respx

import src.auth as auth_module
import src.main as main
import src.pages as pages
from src.auth import AuthError, verify_token
from src.kbc import BackendError, KbcFilesBackend
from src.kbclogin import PkceRegistry, revoke
from tests.test_api import (
    AUTH_HEADERS,
    Api,
    _STACK,
    _publish_markdown,
    api,  # noqa: F401 -- pytest fixture, re-exported for this module's tests
)

#: What a stack answers for the fixture's Storage token: project 123.
_VERIFY_BODY = {
    "id": "tok-good",
    "isMasterToken": True,
    "owner": {"id": 123, "name": "Test"},
}


# --------------------------------------------------------------------------
# X-Storage-Project belongs to a bearer, and only to a bearer
# --------------------------------------------------------------------------


class TestTheProjectHeaderIsIgnoredWithAStorageToken:
    """A Storage token names its own project; the header cannot contradict it.

    A bearer is different: the stack exchanges it for the admin's own Storage
    token *of the project the request named*, so a different project coming
    back means the call was routed somewhere else than the caller said, and
    that must be refused rather than recorded as an identity nobody asked for.
    """

    @staticmethod
    def _verified(token: str, project_id: int | None):
        with respx.mock as mock:
            mock.get(f"{_STACK}/v2/storage/tokens/verify").mock(
                return_value=httpx.Response(200, json=_VERIFY_BODY)
            )
            return verify_token(_STACK, token, project_id=project_id)

    def test_a_storage_token_wins_over_a_mismatched_header(self) -> None:
        owner = self._verified("good-token", 999)
        assert owner.project_id == 123

    def test_a_storage_token_with_a_matching_header_is_unaffected(self) -> None:
        assert self._verified("good-token", 123).project_id == 123

    def test_a_storage_token_with_no_header_is_unaffected(self) -> None:
        assert self._verified("good-token", None).project_id == 123

    def test_a_bearer_resolving_elsewhere_is_still_refused(self) -> None:
        with pytest.raises(AuthError) as excinfo:
            self._verified("kbc_at_session_secret", 999)
        assert "presented as project 999" in str(excinfo.value)

    def test_the_route_accepts_a_storage_token_beside_a_stale_project_header(
        self, api: Api, monkeypatch
    ) -> None:
        """The documented `hub` wrapper leaves KBC_PROJECT exported.

        Reaching the real ``verify_token`` rather than the fixture's stand-in
        is the point of this one: the check being pinned lives there.
        """
        monkeypatch.setattr(main, "verify_token", auth_module.verify_token)
        headers = dict(AUTH_HEADERS)
        headers["X-Storage-Project"] = "999"

        with respx.mock as mock:
            mock.get(f"{_STACK}/v2/storage/tokens/verify").mock(
                return_value=httpx.Response(200, json=_VERIFY_BODY)
            )
            resp = api.client.get("/api/artifacts", headers=headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["project_id"] == 123

    def test_the_openapi_storage_alternative_does_not_require_the_header(
        self, api: Api
    ) -> None:
        """A generated client would otherwise demand a value it cannot have."""
        schema = api.client.get("/openapi.json").json()
        security = schema["paths"]["/api/artifacts"]["get"]["security"]
        by_scheme = [set(option) for option in security]
        assert {"StorageApiToken", "StorageStack"} in by_scheme
        assert {"KeboolaBearer", "StorageStack", "StorageProject"} in by_scheme

    def test_the_documented_behaviour_says_ignored_in_both_places(
        self, api: Api
    ) -> None:
        schema = api.client.get("/openapi.json").json()
        scheme = schema["components"]["securitySchemes"]["StorageProject"]
        assert "ignored with a Storage token" in scheme["description"]
        context = api.client.get("/context").json()
        project = context["auth"]["headers"]["X-Storage-Project"]
        assert "ignored with a Storage token" in project


# --------------------------------------------------------------------------
# Sign-in budgets
# --------------------------------------------------------------------------


def _budget_of_one(api: Api, monkeypatch) -> None:
    """Shrink every sign-in budget to a single call per hour."""
    monkeypatch.setattr(
        main,
        "settings",
        dataclasses.replace(
            api.settings, max_logins_per_hour=1, max_login_polls_per_hour=1
        ),
    )


class TestSignOutHasItsOwnBudget:
    """A 429 on sign-out leaves a live credential on a stack.

    Every other sign-in route can answer 429 harmlessly — the person tries
    again. Sign-out cannot: the tab has already forgotten the token, so a
    refused revocation is a session nobody can end. It therefore gets a
    budget of its own rather than sharing the one that starting and renewing
    spend.
    """

    def test_a_spent_sign_in_budget_still_lets_a_session_be_revoked(
        self, api: Api, monkeypatch
    ) -> None:
        _budget_of_one(api, monkeypatch)
        with respx.mock as mock:
            mock.post(f"{_STACK}/v1/auth/device").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "deviceCode": "dev",
                        "userCode": "ABCD-EFGH",
                        "verificationUri": f"{_STACK}/admin/auth/device",
                        "verificationUriComplete": f"{_STACK}/admin/auth/device?x=1",
                        "expiresIn": 900,
                        "interval": 5,
                    },
                )
            )
            started = api.client.post("/login/device", json={"stack": "us"})
            spent = api.client.post(
                "/login/refresh", json={"stack": "us", "refresh_token": "kbc_rt_x"}
            )

            revoked = mock.post(f"{_STACK}/v1/auth/token/revoke").mock(
                return_value=httpx.Response(204)
            )
            signout = api.client.post(
                "/login/signout", json={"stack": "us", "token": "kbc_rt_x"}
            )

        assert started.status_code == 200, started.text
        assert spent.status_code == 429, "the sign-in budget was not spent"
        assert signout.status_code == 204, signout.text
        assert revoked.called, "sign-out was charged to the sign-in budget"

    def test_sign_out_is_still_bounded_on_its_own(
        self, api: Api, monkeypatch
    ) -> None:
        """It is an unauthenticated route that calls out to a stack."""
        _budget_of_one(api, monkeypatch)
        with respx.mock as mock:
            mock.post(f"{_STACK}/v1/auth/token/revoke").mock(
                return_value=httpx.Response(204)
            )
            first = api.client.post(
                "/login/signout", json={"stack": "us", "token": "kbc_rt_x"}
            )
            second = api.client.post(
                "/login/signout", json={"stack": "us", "token": "kbc_rt_y"}
            )

        assert first.status_code == 204
        assert second.status_code == 429

    def test_the_route_documents_the_one_answer_other_than_204(
        self, api: Api
    ) -> None:
        schema = api.client.get("/openapi.json").json()
        operation = schema["paths"]["/login/signout"]["post"]
        # 422 is FastAPI's own body-validation answer, documented for every
        # route that takes a body.
        assert set(operation["responses"]) == {"204", "422", "429"}
        assert "Always answers 204" not in operation["description"]


class TestSignInBudgetsAreCountedInThisProcess:
    """Hourly, per-address and disposable: the state sidecar is the wrong home.

    One device sign-in polls on the order of 180 times. Persisting that would
    dirty the StateDB on every poll and re-upload the whole snapshot every
    five minutes for rows nothing will ever read again — and losing the tally
    to a restart costs one address one hour's leniency.
    """

    @staticmethod
    def _sidecar_rows(api: Api) -> dict[str, int]:
        database = main.app.state.statedb
        bucket = main._utc_hour()
        return {
            scope: database.count(scope, "testclient", bucket)
            for scope in (
                main.COUNTER_LOGINS,
                main.COUNTER_LOGIN_POLLS,
                main.COUNTER_LOGIN_SIGNOUTS,
            )
        }

    def test_starting_polling_and_signing_out_leave_the_sidecar_alone(
        self, api: Api
    ) -> None:
        with respx.mock as mock:
            mock.post(f"{_STACK}/v1/auth/device/token").mock(
                return_value=httpx.Response(
                    400,
                    json={
                        "error": "cliAuth",
                        "params": {"error": "authorization_pending", "interval": 5},
                    },
                )
            )
            mock.post(f"{_STACK}/v1/auth/token/revoke").mock(
                return_value=httpx.Response(204)
            )
            for _ in range(3):
                polled = api.client.post(
                    "/login/device/token", json={"stack": "us", "device_code": "d"}
                )
                assert polled.json()["status"] == "pending"
            api.client.post(
                "/login/signout", json={"stack": "us", "token": "kbc_rt_x"}
            )

        assert self._sidecar_rows(api) == {
            main.COUNTER_LOGINS: 0,
            main.COUNTER_LOGIN_POLLS: 0,
            main.COUNTER_LOGIN_SIGNOUTS: 0,
        }
        counted = main._fallback_counts[
            (main.COUNTER_LOGIN_POLLS, "testclient", main._utc_hour())
        ]
        assert counted == 3, "polls were not counted anywhere"

    def test_every_attempt_is_charged_including_the_refused_ones(
        self, api: Api, monkeypatch
    ) -> None:
        """The accounting contract `_claim_slot` documents: the bump is first.

        A caller hammering a spent budget keeps being counted, which is what
        makes the window self-limiting rather than a retry loop.
        """
        _budget_of_one(api, monkeypatch)
        for _ in range(4):
            api.client.post(
                "/login/refresh", json={"stack": "us", "refresh_token": "kbc_rt_x"}
            )
        counted = main._fallback_counts[
            (main.COUNTER_LOGINS, "testclient", main._utc_hour())
        ]
        assert counted == 4


# --------------------------------------------------------------------------
# Registry and transport edges
# --------------------------------------------------------------------------


class TestPkceRegistryAlwaysHoldsOne:
    """A registry that cannot hold the login it just minted is not "off".

    Zero means "no limit" or "disabled" for other settings, so it reaches
    here; the eviction loop then ran forever over an empty dict. PKCE is
    turned off by not being on a loopback origin, never by this.
    """

    def test_a_zero_ceiling_still_starts_a_login(self) -> None:
        registry = PkceRegistry(ttl_s=600, max_pending=0)
        pending = registry.start(_STACK, "http://127.0.0.1:8050/login/callback")
        assert registry.take(pending.state) is not None

    def test_a_ceiling_of_one_keeps_the_newest_login(self) -> None:
        registry = PkceRegistry(ttl_s=600, max_pending=1)
        first = registry.start(_STACK, "http://127.0.0.1:8050/login/callback")
        second = registry.start(_STACK, "http://127.0.0.1:8050/login/callback")
        assert registry.take(first.state) is None
        assert registry.take(second.state) is not None


class TestAnyTwoHundredIsASuccess:
    """``token/revoke`` may answer 204, which carries no body to parse."""

    def test_a_204_revocation_is_not_reported_as_a_failure(self, caplog) -> None:
        with respx.mock as mock:
            mock.post(f"{_STACK}/v1/auth/token/revoke").mock(
                return_value=httpx.Response(204)
            )
            with caplog.at_level(logging.INFO):
                revoke(_STACK, "kbc_rt_x", 5)
        assert [r.message for r in caplog.records if "revocation" in r.message] == []
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_a_refused_revocation_is_still_logged_and_swallowed(
        self, caplog
    ) -> None:
        with respx.mock as mock:
            mock.post(f"{_STACK}/v1/auth/token/revoke").mock(
                return_value=httpx.Response(400, json={"error": "invalid_grant"})
            )
            with caplog.at_level(logging.INFO):
                revoke(_STACK, "kbc_rt_x", 5)
        assert any("did not take" in r.message for r in caplog.records)


class TestAHalfBuiltStorageClientIsNotCached:
    """``_apply_auth`` is what moves a bearer out of ``X-StorageApi-Token``.

    Caching a client before it ran would leave a reused backend sending the
    bearer in a Storage token's header for the rest of its life.
    """

    def test_a_failed_auth_leaves_nothing_behind_to_reuse(self) -> None:
        backend = KbcFilesBackend(_STACK, "kbc_at_abc_secret")
        for _ in range(2):
            with pytest.raises(BackendError):
                backend._files()
            assert backend._client is None


# --------------------------------------------------------------------------
# The shared session module
# --------------------------------------------------------------------------


class TestOneSessionModuleForEveryPage:
    """Three pages hold the same record; one place decides what to do with it."""

    @staticmethod
    def _pages(api: Api) -> dict[str, str]:
        artifact_id = _publish_markdown(api, "# Reviewed")
        return {
            "/admin": api.client.get("/admin").text,
            "/login": api.client.get("/login").text,
            "/review": api.client.get(f"/a/{artifact_id}/review").text,
        }

    def test_every_page_that_holds_a_credential_ships_the_module(
        self, api: Api
    ) -> None:
        for name, text in self._pages(api).items():
            assert text.count("window.hubSession = {") == 1, name
            assert "var SESSION = window.hubSession;" in text, name

    def test_no_page_talks_to_the_session_routes_on_its_own(self, api: Api) -> None:
        """One caller per route, and it is the module.

        ``/login`` is the exception on ``/login/device*``, which is its own
        flow and nobody else's.
        """
        for name, text in self._pages(api).items():
            assert text.count('"/login/refresh"') == 1, name
            assert text.count('"/login/signout"') == 1, name

    def test_the_module_is_the_only_reader_of_the_storage_entry(
        self, api: Api
    ) -> None:
        for name, text in self._pages(api).items():
            assert text.count('var AUTH_KEY = "hub_admin_auth";') == 1, name


#: Drives the page's own session module under node. ``__SESSION__`` is
#: replaced with :data:`src.pages._SESSION_JS` and the result is one file, so
#: the module runs exactly as the browser runs it.
_SESSION_HARNESS = """
'use strict';

var store = {};
var fetches = [];
var settle = null;

global.window = {
  HUB_BASE: "https://hub.example/",
  sessionStorage: {
    getItem: function (key) { return key in store ? store[key] : null; },
    setItem: function (key, value) { store[key] = String(value); },
    removeItem: function (key) { delete store[key]; }
  }
};

/* Every refresh hangs until the harness settles it, which is what lets two
   concurrent renewals overlap in the first place. */
global.fetch = function (url, init) {
  fetches.push(String(url));
  return new Promise(function (resolve) { settle = resolve; });
};

__SESSION__

var S = global.window.hubSession;
var out = {};

function refreshes() {
  return fetches.filter(function (u) { return u.indexOf("/login/refresh") !== -1; })
    .length;
}

(async function () {
  /* read(): normalized, and whatever else the record holds is kept. */
  store["hub_admin_auth"] = JSON.stringify({
    token: "kbc_at_old", stack: "https://connection.keboola.com",
    project: 7, refresh: "kbc_rt_old", note: "from a newer /login"
  });
  var loaded = S.read();
  out.loadedNote = loaded.note;
  out.loadedProject = loaded.project;

  store["hub_admin_auth"] = JSON.stringify({ token: "1234-abc", stack: "st" });
  out.storageProject = S.read().project;
  out.storageRefresh = S.read().refresh;

  store["hub_admin_auth"] = JSON.stringify({ token: "", stack: "st" });
  out.emptyToken = S.read();

  /* headers(): each credential in the header its kind belongs in. */
  out.bearer = S.headers({ token: "kbc_at_x", stack: "st", project: 7 }, {});
  out.pat = S.headers({ token: "kbc_pat_x", stack: "st", project: 7 }, {});
  out.storage = S.headers({ token: "1234-abc", stack: "st", project: null }, {});

  /* renew(): one exchange, shared by every caller. */
  var live = {
    token: "kbc_at_old", stack: "st", project: 7,
    refresh: "kbc_rt_old", note: "keep me"
  };
  var a = S.renew(live);
  var b = S.renew(live);
  out.sharedPromise = a === b;
  settle({
    ok: true,
    json: async function () {
      return { credential: {
        access_token: "kbc_at_new", refresh_token: "kbc_rt_new"
      } };
    }
  });
  var renewed = await a;
  out.sameResult = renewed === (await b);
  out.refreshCalls = refreshes();
  out.renewedToken = renewed.token;
  out.renewedRefresh = renewed.refresh;
  out.renewedNote = renewed.note;
  out.renewedProject = renewed.project;
  out.persistedToken = JSON.parse(store["hub_admin_auth"]).token;

  /* A later renewal is a new exchange, and a refusal is null rather than a
     throw: the caller reports the failure it already had. */
  var again = S.renew(renewed);
  out.newExchange = again !== a;
  settle({ ok: false });
  out.refused = await again;
  out.refreshCallsAfter = refreshes();

  out.nothingToRenew = await S.renew({ token: "1234-abc", stack: "st" });

  /* Which failures say something about the credential, and which do not. */
  out.rejected401 = S.rejected({ status: 401 });
  out.rejected403 = S.rejected({ status: 403 });
  out.rejected502 = S.rejected({ status: 502 });
  out.lockedGate = S.locked({ status: 401, payload: { error: "password required" } });
  out.lockedOther = S.locked({ status: 401, payload: { error: "token rejected" } });
  out.lockedBody = S.lockedBody({ error: "password required" });
  out.lockedNoBody = S.lockedBody(null);

  console.log(JSON.stringify(out));
})().catch(function (err) {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def session_module() -> dict:
    """The session module's behaviour, as observed by running it in node."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available to run the inline scripts")
    script = _SESSION_HARNESS.replace("__SESSION__", pages._SESSION_JS)
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "session-harness.js"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run(
            [node, str(path)], capture_output=True, text=True, timeout=30
        )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


class TestTheSessionModuleBehaviour:
    def test_a_record_keeps_fields_this_version_does_not_know(
        self, session_module: dict
    ) -> None:
        assert session_module["loadedNote"] == "from a newer /login"
        assert session_module["loadedProject"] == 7

    def test_a_storage_token_record_is_normalized(
        self, session_module: dict
    ) -> None:
        assert session_module["storageProject"] is None
        assert session_module["storageRefresh"] is None

    def test_a_record_without_a_token_is_no_record(
        self, session_module: dict
    ) -> None:
        assert session_module["emptyToken"] is None

    def test_each_credential_travels_in_its_own_header(
        self, session_module: dict
    ) -> None:
        assert session_module["bearer"] == {
            "Authorization": "Bearer kbc_at_x",
            "X-Storage-Stack": "st",
            "X-Storage-Project": "7",
        }
        assert session_module["pat"]["Authorization"] == "Bearer kbc_pat_x"
        assert session_module["storage"] == {
            "X-StorageApi-Token": "1234-abc",
            "X-Storage-Stack": "st",
        }

    def test_concurrent_renewals_share_one_exchange(
        self, session_module: dict
    ) -> None:
        """The stack spends the refresh token on the first exchange.

        Two independent renewals meant the second was handed a token already
        gone, reported a live session as dead, and spent a sign-in slot doing
        it.
        """
        assert session_module["sharedPromise"] is True
        assert session_module["sameResult"] is True
        assert session_module["refreshCalls"] == 1

    def test_a_renewal_merges_rather_than_rebuilds(
        self, session_module: dict
    ) -> None:
        assert session_module["renewedToken"] == "kbc_at_new"
        assert session_module["renewedRefresh"] == "kbc_rt_new"
        assert session_module["renewedProject"] == 7
        assert session_module["renewedNote"] == "keep me"
        assert session_module["persistedToken"] == "kbc_at_new"

    def test_a_later_renewal_is_a_fresh_exchange(
        self, session_module: dict
    ) -> None:
        assert session_module["newExchange"] is True
        assert session_module["refreshCallsAfter"] == 2

    def test_a_refused_renewal_answers_null(self, session_module: dict) -> None:
        assert session_module["refused"] is None

    def test_a_storage_token_has_nothing_to_renew(
        self, session_module: dict
    ) -> None:
        assert session_module["nothingToRenew"] is None
        assert session_module["refreshCallsAfter"] == 2

    def test_only_the_credential_being_refused_is_about_the_credential(
        self, session_module: dict
    ) -> None:
        assert session_module["rejected401"] is True
        assert session_module["rejected403"] is True
        assert session_module["rejected502"] is False

    def test_the_reader_gate_is_told_apart_from_a_dead_session(
        self, session_module: dict
    ) -> None:
        assert session_module["lockedGate"] is True
        assert session_module["lockedOther"] is False
        assert session_module["lockedBody"] is True
        assert session_module["lockedNoBody"] is False


class TestAPasswordGate401DoesNotRenewASession:
    """`_comment_gate` answers 401 to a signed-in owner as well.

    Renewing over it spends a refresh token, and a sign-in slot, to be
    answered with exactly the same 401. Both fetch wrappers therefore read the
    body before deciding, which is why the retry guard reads that way.
    """

    def test_both_pages_read_the_body_before_renewing(self, api: Api) -> None:
        for source in (pages._ADMIN_JS, pages._REVIEW_JS):
            assert "!SESSION.lockedBody(out.data)" in source
            # The old shape: renew on the bare status, body unread.
            assert "resp.status === 401 && await renewSession()" not in source


# --------------------------------------------------------------------------
# Device polling
# --------------------------------------------------------------------------


class TestAThrottledStackIsNotARefusal:
    """The page tells the two apart by the status, so the hub has to.

    A stack that rate-limits a poll used to reach the browser as the same 400
    a decline gets, and the page then threw away a device code the stack was
    still willing to honour.
    """

    @staticmethod
    def _throttled(path: str) -> httpx.Response:
        return httpx.Response(
            429, json={"error": "cliAuth", "params": {"error": "rate_limited"}}
        )

    def test_a_throttled_poll_answers_429(self, api: Api) -> None:
        with respx.mock as mock:
            mock.post(f"{_STACK}/v1/auth/device/token").mock(
                return_value=self._throttled("device/token")
            )
            resp = api.client.post(
                "/login/device/token", json={"stack": "us", "device_code": "d"}
            )
        assert resp.status_code == 429, resp.text
        assert "rate-limiting" in resp.json()["detail"]

    def test_a_throttled_start_answers_429(self, api: Api) -> None:
        with respx.mock as mock:
            mock.post(f"{_STACK}/v1/auth/device").mock(
                return_value=self._throttled("device")
            )
            resp = api.client.post("/login/device", json={"stack": "us"})
        assert resp.status_code == 429, resp.text

    def test_a_throttled_renewal_answers_429(self, api: Api) -> None:
        with respx.mock as mock:
            mock.post(f"{_STACK}/v1/auth/token/refresh").mock(
                return_value=self._throttled("token/refresh")
            )
            resp = api.client.post(
                "/login/refresh", json={"stack": "us", "refresh_token": "kbc_rt_x"}
            )
        assert resp.status_code == 429, resp.text

    def test_a_declined_poll_is_still_the_terminal_400(self, api: Api) -> None:
        with respx.mock as mock:
            mock.post(f"{_STACK}/v1/auth/device/token").mock(
                return_value=httpx.Response(
                    400,
                    json={"error": "cliAuth", "params": {"error": "access_denied"}},
                )
            )
            resp = api.client.post(
                "/login/device/token", json={"stack": "us", "device_code": "d"}
            )
        assert resp.status_code == 400, resp.text
        assert "declined" in resp.json()["detail"]

    def test_both_answers_are_documented_on_the_poll_route(self, api: Api) -> None:
        schema = api.client.get("/openapi.json").json()
        responses = schema["paths"]["/login/device/token"]["post"]["responses"]
        assert "429" in responses
        assert "still valid" in responses["429"]["description"]


class TestADevicePollSurvivesATransientFailure:
    """The device *code* expires, not the request that asked about it.

    A network blip, this hub's own 429 or a stack that timed out all left a
    code valid for another quarter of an hour, and the page threw it away and
    made the person start over with a second code and a second approval tab.
    """

    def test_only_a_terminal_refusal_resets_the_flow(self) -> None:
        source = pages._LOGIN_JS
        assert "if (err && err.status === 400) {" in source
        assert "failure.status = resp.status;" in source

    def test_a_transient_failure_reschedules_with_backoff(self) -> None:
        source = pages._LOGIN_JS
        assert "schedulePoll(start, Math.min(interval * 2, MAX_POLL_INTERVAL_S))" in source
        assert "waiting for approval (retrying)" in source

    def test_slow_down_actually_lengthens_the_interval(self) -> None:
        """The hub forwards interval 0 whenever the stack omits one.

        RFC 8628 says slow_down means "add five seconds"; without the step it
        was nothing but a change of label.
        """
        source = pages._LOGIN_JS
        assert "var SLOW_DOWN_STEP_S = 5;" in source
        assert (
            "next = Math.max(next, interval + SLOW_DOWN_STEP_S)" in source
        )

    def test_the_approval_url_has_to_be_https(self) -> None:
        """It is the one stack-supplied value that reaches href and window.open."""
        source = pages._LOGIN_JS
        assert "function approvalUrl(raw)" in source
        assert "/^https:\\/\\//.test(String(raw" in source
        assert 'window.open(approval, "_blank", "noopener")' in source
