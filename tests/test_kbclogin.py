"""Tests for src.kbclogin: the device-code and PKCE sign-in flows.

Every stack call is stubbed with ``respx``; no live Keboola endpoint is
touched. The wire shapes asserted here are the ones Connection documents in
``openapi/auth.json`` (``CliTokenResponse``, ``CliAuthErrorResponse``,
``DeviceAuthorizationCreateResponse``, ``TokenIntrospectResponse``).
"""

import base64
import hashlib

import httpx
import pytest
import respx

from src.kbclogin import (
    LoginPending,
    LoginRejected,
    LoginUnavailable,
    PkceRegistry,
    authorize_url,
    exchange_pkce,
    introspect,
    is_bearer_credential,
    new_pkce_verifier,
    new_state,
    pkce_challenge,
    poll_device,
    refresh_credential,
    revoke,
    start_device,
)

STACK = "https://connection.keboola.com"
CLIENT = "kbc-artifact-hub-tests"
TIMEOUT = 5

DEVICE_URL = f"{STACK}/v1/auth/device"
DEVICE_TOKEN_URL = f"{STACK}/v1/auth/device/token"
PKCE_TOKEN_URL = f"{STACK}/v1/auth/pkce/token"
INTROSPECT_URL = f"{STACK}/v1/auth/token/introspect"
REFRESH_URL = f"{STACK}/v1/auth/token/refresh"
REVOKE_URL = f"{STACK}/v1/auth/token/revoke"

TOKEN_BODY = {
    "accessToken": "kbc_at_abc_secret",
    "refreshToken": "kbc_rt_abc_secret",
    "tokenType": "Bearer",
    "expiresIn": 3600,
    "sessionId": "abc",
    "user": {"id": 42, "email": "someone@keboola.com", "name": "Someone"},
}


def _cli_error(error: str, interval: int | None = None) -> dict:
    """A ``CliAuthErrorResponse`` body carrying one RFC-style error token."""
    params: dict = {"error": error}
    if interval is not None:
        params["interval"] = interval
    return {
        "error": "cliAuth",
        "exceptionId": "x",
        "code": "y",
        "uuid": "z",
        "params": params,
        "exceptionDetailUrl": "https://example.com",
    }


class TestBearerDetection:
    def test_session_token_is_a_bearer(self):
        assert is_bearer_credential("kbc_at_abc_secret")

    def test_personal_access_token_is_a_bearer(self):
        assert is_bearer_credential("kbc_pat_abc_secret")

    def test_storage_token_is_not(self):
        assert not is_bearer_credential("1234-abcdef")


class TestPkcePrimitives:
    def test_verifier_is_43_url_safe_characters(self):
        verifier = new_pkce_verifier()
        assert len(verifier) == 43
        assert verifier.strip("ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                              "abcdefghijklmnopqrstuvwxyz0123456789-_") == ""

    def test_state_is_inside_the_stacks_length_window(self):
        state = new_state()
        assert 22 <= len(state) <= 512

    def test_challenge_is_the_s256_of_the_verifier(self):
        verifier = new_pkce_verifier()
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        assert pkce_challenge(verifier) == expected

    def test_two_verifiers_differ(self):
        assert new_pkce_verifier() != new_pkce_verifier()

    def test_authorize_url_carries_every_required_parameter(self):
        url = authorize_url(STACK, CLIENT, "http://127.0.0.1:8050/login/callback",
                            "c" * 43, "s" * 32)
        parsed = httpx.URL(url)
        assert parsed.path == "/admin/auth/pkce/authorize"
        assert parsed.params["responseType"] == "code"
        assert parsed.params["codeChallengeMethod"] == "S256"
        assert parsed.params["clientId"] == CLIENT
        assert parsed.params["redirectUri"] == "http://127.0.0.1:8050/login/callback"

    def test_the_stacks_project_picker_is_asked_for_by_default(self):
        """The credential ends up in a browser tab, so it should be the small one.

        Only the stack can narrow what a session reaches; this hub's own
        project step just chooses which reachable project a call acts as.
        """
        url = authorize_url(STACK, CLIENT, "http://127.0.0.1:8050/login/callback",
                            "c" * 43, "s" * 32)
        assert httpx.URL(url).params["projectScope"] == "selected"

    def test_an_unrestricted_session_has_to_be_asked_for(self):
        url = authorize_url(STACK, CLIENT, "http://127.0.0.1:8050/login/callback",
                            "c" * 43, "s" * 32, pick_project=False)
        assert httpx.URL(url).params["projectScope"] == "all"


class TestStartDevice:
    @respx.mock
    def test_returns_the_codes_and_polling_interval(self):
        route = respx.post(DEVICE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "deviceCode": "kbc_dc_secret",
                    "userCode": "ABCD-EFGH",
                    "verificationUri": f"{STACK}/admin/auth/device",
                    "verificationUriComplete": (
                        f"{STACK}/admin/auth/device?userCode=ABCD-EFGH"
                    ),
                    "expiresIn": 900,
                    "interval": 5,
                },
            )
        )
        start = start_device(STACK, CLIENT, TIMEOUT)
        assert start.user_code == "ABCD-EFGH"
        assert start.interval == 5
        assert route.calls.last.request.read() == (
            b'{"clientId":"kbc-artifact-hub-tests",'
            b'"scope":{"credentialType":"session"}}'
        )

    @respx.mock
    def test_a_stack_without_the_feature_is_unavailable_not_rejected(self):
        respx.post(DEVICE_URL).mock(return_value=httpx.Response(404))
        with pytest.raises(LoginUnavailable):
            start_device(STACK, CLIENT, TIMEOUT)

    @respx.mock
    def test_an_unreachable_stack_is_unavailable(self):
        respx.post(DEVICE_URL).mock(side_effect=httpx.ConnectError("no route"))
        with pytest.raises(LoginUnavailable):
            start_device(STACK, CLIENT, TIMEOUT)

    @respx.mock
    def test_a_rate_limited_start_is_rejected_with_a_readable_message(self):
        respx.post(DEVICE_URL).mock(
            return_value=httpx.Response(429, json=_cli_error("rate_limited"))
        )
        with pytest.raises(LoginRejected) as excinfo:
            start_device(STACK, CLIENT, TIMEOUT)
        assert "rate-limiting" in str(excinfo.value)

    @respx.mock
    def test_a_body_missing_the_codes_is_unavailable(self):
        respx.post(DEVICE_URL).mock(return_value=httpx.Response(200, json={"a": 1}))
        with pytest.raises(LoginUnavailable):
            start_device(STACK, CLIENT, TIMEOUT)


class TestPollDevice:
    @respx.mock
    def test_pending_carries_the_interval_to_wait(self):
        respx.post(DEVICE_TOKEN_URL).mock(
            return_value=httpx.Response(
                400, json=_cli_error("authorization_pending", interval=5)
            )
        )
        with pytest.raises(LoginPending) as excinfo:
            poll_device(STACK, CLIENT, "kbc_dc_secret", TIMEOUT)
        assert excinfo.value.interval == 5
        assert excinfo.value.slow_down is False

    @respx.mock
    def test_slow_down_is_pending_with_a_flag(self):
        respx.post(DEVICE_TOKEN_URL).mock(
            return_value=httpx.Response(400, json=_cli_error("slow_down", interval=10))
        )
        with pytest.raises(LoginPending) as excinfo:
            poll_device(STACK, CLIENT, "kbc_dc_secret", TIMEOUT)
        assert excinfo.value.slow_down is True
        assert excinfo.value.interval == 10

    @respx.mock
    def test_approval_returns_the_session(self):
        respx.post(DEVICE_TOKEN_URL).mock(
            return_value=httpx.Response(200, json=TOKEN_BODY)
        )
        credential = poll_device(STACK, CLIENT, "kbc_dc_secret", TIMEOUT)
        assert credential.access_token == "kbc_at_abc_secret"
        assert credential.refresh_token == "kbc_rt_abc_secret"
        assert credential.expires_in == 3600
        assert credential.user_email == "someone@keboola.com"
        assert credential.stack_url == STACK

    @respx.mock
    def test_a_declined_sign_in_is_terminal(self):
        respx.post(DEVICE_TOKEN_URL).mock(
            return_value=httpx.Response(400, json=_cli_error("access_denied"))
        )
        with pytest.raises(LoginRejected) as excinfo:
            poll_device(STACK, CLIENT, "kbc_dc_secret", TIMEOUT)
        assert excinfo.value.error == "access_denied"
        assert "declined" in str(excinfo.value)

    @respx.mock
    def test_an_expired_code_is_terminal(self):
        respx.post(DEVICE_TOKEN_URL).mock(
            return_value=httpx.Response(400, json=_cli_error("expired_token"))
        )
        with pytest.raises(LoginRejected) as excinfo:
            poll_device(STACK, CLIENT, "kbc_dc_secret", TIMEOUT)
        assert "expired" in str(excinfo.value)

    @respx.mock
    def test_a_session_without_a_token_is_rejected(self):
        respx.post(DEVICE_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"sessionId": "abc"})
        )
        with pytest.raises(LoginRejected):
            poll_device(STACK, CLIENT, "kbc_dc_secret", TIMEOUT)

    @respx.mock
    def test_an_unknown_error_token_never_echoes_the_stacks_text(self):
        respx.post(DEVICE_TOKEN_URL).mock(
            return_value=httpx.Response(
                400, json=_cli_error("<script>alert(1)</script>")
            )
        )
        with pytest.raises(LoginRejected) as excinfo:
            poll_device(STACK, CLIENT, "kbc_dc_secret", TIMEOUT)
        assert "script" not in str(excinfo.value)


class TestExchangePkce:
    @respx.mock
    def test_sends_the_verifier_and_returns_the_session(self):
        route = respx.post(PKCE_TOKEN_URL).mock(
            return_value=httpx.Response(200, json=TOKEN_BODY)
        )
        credential = exchange_pkce(
            STACK,
            CLIENT,
            "the-code",
            "http://127.0.0.1:8050/login/callback",
            "s" * 32,
            "v" * 43,
            TIMEOUT,
        )
        assert credential.session_id == "abc"
        assert b'"codeVerifier":"' + b"v" * 43 + b'"' in route.calls.last.request.read()

    @respx.mock
    def test_a_replayed_code_is_rejected(self):
        respx.post(PKCE_TOKEN_URL).mock(
            return_value=httpx.Response(400, json=_cli_error("invalid_grant"))
        )
        with pytest.raises(LoginRejected):
            exchange_pkce(
                STACK, CLIENT, "used", "http://127.0.0.1:8050/login/callback",
                "s" * 32, "v" * 43, TIMEOUT,
            )


class TestIntrospect:
    @respx.mock
    def test_lists_the_projects_the_session_reaches(self):
        route = respx.get(INTROSPECT_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "sessionId": "abc",
                    "user": {"id": 42, "email": "a@b.c", "name": "A"},
                    "grantType": "device_code",
                    "sudoVerified": False,
                    "createdAt": "2026-09-01T00:00:00+00:00",
                    "expiresAt": "2026-09-01T01:00:00+00:00",
                    "projects": [
                        {"id": 123, "name": "Test", "role": "admin"},
                        {"id": 999, "name": "Other", "role": "guest"},
                    ],
                },
            )
        )
        info = introspect(STACK, "kbc_at_abc_secret", TIMEOUT)
        assert [p.id for p in info.projects] == [123, 999]
        assert info.user_email == "a@b.c"
        assert (
            route.calls.last.request.headers["authorization"]
            == "Bearer kbc_at_abc_secret"
        )

    @respx.mock
    def test_a_project_the_admin_no_longer_reaches_is_dropped(self):
        respx.get(INTROSPECT_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "sessionId": "abc",
                    "user": {"id": 42, "email": "a@b.c", "name": "A"},
                    "projects": [
                        {"id": 1, "name": None, "role": None},
                        {"id": 2, "name": "Live", "role": "admin"},
                    ],
                },
            )
        )
        info = introspect(STACK, "kbc_at_abc_secret", TIMEOUT)
        assert [p.id for p in info.projects] == [2]

    @respx.mock
    def test_an_expired_session_is_rejected(self):
        respx.get(INTROSPECT_URL).mock(
            return_value=httpx.Response(401, json={"error": "invalid_token"})
        )
        with pytest.raises(LoginRejected):
            introspect(STACK, "kbc_at_abc_secret", TIMEOUT)

    @respx.mock
    def test_the_token_never_leaks_into_a_transport_error(self):
        respx.get(INTROSPECT_URL).mock(
            side_effect=httpx.ConnectError("failed for kbc_at_abc_secret")
        )
        with pytest.raises(LoginUnavailable) as excinfo:
            introspect(STACK, "kbc_at_abc_secret", TIMEOUT)
        assert "kbc_at_abc_secret" not in str(excinfo.value)


class TestRefreshAndRevoke:
    @respx.mock
    def test_refresh_returns_a_new_pair(self):
        respx.post(REFRESH_URL).mock(
            return_value=httpx.Response(
                200, json={**TOKEN_BODY, "accessToken": "kbc_at_next_secret"}
            )
        )
        credential = refresh_credential(STACK, "kbc_rt_abc_secret", TIMEOUT)
        assert credential.access_token == "kbc_at_next_secret"

    @respx.mock
    def test_a_spent_refresh_token_is_rejected(self):
        respx.post(REFRESH_URL).mock(return_value=httpx.Response(401, json={}))
        with pytest.raises(LoginRejected):
            refresh_credential(STACK, "kbc_rt_abc_secret", TIMEOUT)

    @respx.mock
    def test_revoke_posts_the_token(self):
        route = respx.post(REVOKE_URL).mock(return_value=httpx.Response(200, json={}))
        revoke(STACK, "kbc_rt_abc_secret", TIMEOUT)
        assert route.called

    @respx.mock
    def test_a_refused_revocation_is_swallowed(self):
        respx.post(REVOKE_URL).mock(return_value=httpx.Response(401, json={}))
        revoke(STACK, "kbc_rt_abc_secret", TIMEOUT)


class TestPkceRegistry:
    def test_an_entry_round_trips_once(self):
        registry = PkceRegistry(ttl_s=60, max_pending=4)
        pending = registry.start(STACK, "http://127.0.0.1:8050/login/callback")
        assert registry.take(pending.state) is not None
        assert registry.take(pending.state) is None

    def test_an_unknown_state_is_not_found(self):
        registry = PkceRegistry(ttl_s=60, max_pending=4)
        registry.start(STACK, "http://127.0.0.1:8050/login/callback")
        assert registry.take("never-issued") is None

    def test_entries_expire(self, monkeypatch):
        clock = {"now": 1000.0}
        monkeypatch.setattr("src.kbclogin.time.monotonic", lambda: clock["now"])
        registry = PkceRegistry(ttl_s=60, max_pending=4)
        pending = registry.start(STACK, "http://127.0.0.1:8050/login/callback")
        clock["now"] += 61
        assert registry.take(pending.state) is None

    def test_the_oldest_pending_login_is_evicted_first(self, monkeypatch):
        clock = {"now": 1000.0}
        monkeypatch.setattr("src.kbclogin.time.monotonic", lambda: clock["now"])
        registry = PkceRegistry(ttl_s=600, max_pending=2)
        first = registry.start(STACK, "http://127.0.0.1:8050/login/callback")
        clock["now"] += 1
        second = registry.start(STACK, "http://127.0.0.1:8050/login/callback")
        clock["now"] += 1
        third = registry.start(STACK, "http://127.0.0.1:8050/login/callback")
        assert registry.take(first.state) is None
        assert registry.take(second.state) is not None
        assert registry.take(third.state) is not None

    def test_every_start_gets_its_own_verifier(self):
        registry = PkceRegistry(ttl_s=60, max_pending=4)
        one = registry.start(STACK, "http://127.0.0.1:8050/login/callback")
        two = registry.start(STACK, "http://127.0.0.1:8050/login/callback")
        assert one.verifier != two.verifier
        assert one.state != two.state


class TestFailureLogging:
    """An operator must be able to tell a broken stack from a declining user.

    Without a log line here the only trace of a refused sign-in is a bare 400
    in the access log, which is the same whether one person declined in their
    browser or the stack has stopped accepting sign-ins for everyone.
    """

    @respx.mock
    def test_a_stack_without_the_feature_is_logged(self, caplog):
        respx.post(DEVICE_URL).mock(return_value=httpx.Response(404))
        with caplog.at_level("WARNING", logger="src.kbclogin"):
            with pytest.raises(LoginUnavailable):
                start_device(STACK, CLIENT, TIMEOUT)
        assert any("404" in r.getMessage() for r in caplog.records)

    @respx.mock
    def test_a_stack_side_failure_is_a_warning_naming_the_error_token(self, caplog):
        respx.post(DEVICE_TOKEN_URL).mock(
            return_value=httpx.Response(400, json=_cli_error("invalid_client"))
        )
        with caplog.at_level("INFO", logger="src.kbclogin"):
            with pytest.raises(LoginRejected):
                poll_device(STACK, CLIENT, "kbc_dc_secret", TIMEOUT)
        record = caplog.records[-1]
        assert record.levelname == "WARNING"
        assert "invalid_client" in record.getMessage()

    @respx.mock
    def test_a_person_declining_is_only_informational(self, caplog):
        """A wall of WARNINGs has to mean something is wrong, so this is not one."""
        respx.post(DEVICE_TOKEN_URL).mock(
            return_value=httpx.Response(400, json=_cli_error("access_denied"))
        )
        with caplog.at_level("INFO", logger="src.kbclogin"):
            with pytest.raises(LoginRejected):
                poll_device(STACK, CLIENT, "kbc_dc_secret", TIMEOUT)
        assert caplog.records[-1].levelname == "INFO"

    @respx.mock
    def test_a_pending_poll_says_nothing(self, caplog):
        """It repeats every few seconds for the whole of every sign-in."""
        respx.post(DEVICE_TOKEN_URL).mock(
            return_value=httpx.Response(
                400, json=_cli_error("authorization_pending", interval=5)
            )
        )
        with caplog.at_level("INFO", logger="src.kbclogin"):
            with pytest.raises(LoginPending):
                poll_device(STACK, CLIENT, "kbc_dc_secret", TIMEOUT)
        assert caplog.records == []

    @respx.mock
    def test_an_intermediarys_error_text_is_bounded_in_the_log(self, caplog):
        """The token is parsed out of a body nobody here has vetted."""
        respx.post(DEVICE_TOKEN_URL).mock(
            return_value=httpx.Response(400, json=_cli_error("x" * 500))
        )
        with caplog.at_level("INFO", logger="src.kbclogin"):
            with pytest.raises(LoginRejected):
                poll_device(STACK, CLIENT, "kbc_dc_secret", TIMEOUT)
        record = caplog.records[-1]
        assert "x" * 500 not in record.getMessage()
        assert "x" * 64 in record.getMessage()

    @respx.mock
    def test_a_refused_sign_in_never_logs_the_credential(self, caplog):
        respx.get(INTROSPECT_URL).mock(return_value=httpx.Response(401, json={}))
        with caplog.at_level("INFO", logger="src.kbclogin"):
            with pytest.raises(LoginRejected):
                introspect(STACK, "kbc_at_abc_secret", TIMEOUT)
        for record in caplog.records:
            assert "kbc_at_abc_secret" not in record.getMessage()
