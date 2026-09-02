"""Tests for src.auth: stack resolution and Storage token verification."""

import httpx
import pytest
import respx

from src.auth import (
    AuthError,
    StackError,
    StackUnreachableError,
    resolve_stack,
    storage_headers,
    verify_token,
)

VERIFY_URL = "https://connection.keboola.com/v2/storage/tokens/verify"


class TestResolveStack:
    def test_alias_us(self):
        assert resolve_stack("us") == "https://connection.keboola.com"

    def test_alias_eu(self):
        assert resolve_stack("eu") == "https://connection.eu-central-1.keboola.com"

    def test_alias_gcp_eu(self):
        assert (
            resolve_stack("gcp-eu") == "https://connection.europe-west3.gcp.keboola.com"
        )

    def test_alias_is_case_insensitive(self):
        assert resolve_stack("US") == "https://connection.keboola.com"

    def test_full_https_url_normalizes_and_strips_path(self):
        assert (
            resolve_stack("https://connection.keboola.com/v2/storage/")
            == "https://connection.keboola.com"
        )

    def test_trailing_slash_stripped(self):
        assert (
            resolve_stack("https://connection.keboola.com/")
            == "https://connection.keboola.com"
        )

    def test_http_rejected(self):
        with pytest.raises(StackError):
            resolve_stack("http://connection.keboola.com")

    def test_non_keboola_host_rejected(self):
        with pytest.raises(StackError):
            resolve_stack("https://evil.example.com")

    def test_extra_stacks_allows_explicit_url(self):
        extra = ("https://custom.example.com",)
        assert (
            resolve_stack("https://custom.example.com", extra_stacks=extra)
            == "https://custom.example.com"
        )

    def test_extra_stacks_does_not_allow_unlisted_hosts(self):
        extra = ("https://custom.example.com",)
        with pytest.raises(StackError):
            resolve_stack("https://other.example.com", extra_stacks=extra)

    def test_empty_raises(self):
        with pytest.raises(StackError):
            resolve_stack("")

    def test_whitespace_only_raises(self):
        with pytest.raises(StackError):
            resolve_stack("   ")


class TestVerifyToken:
    def test_success_returns_owner(self):
        with respx.mock as mock:
            mock.get(VERIFY_URL).mock(
                return_value=httpx.Response(
                    200, json={"owner": {"id": 123, "name": "Proj"}}
                )
            )
            owner = verify_token("https://connection.keboola.com", "some-token")

        assert owner.project_id == 123
        assert owner.project_name == "Proj"
        assert owner.key == "123@connection.keboola.com"

    def test_401_raises_autherror(self):
        with respx.mock as mock:
            mock.get(VERIFY_URL).mock(return_value=httpx.Response(401))
            with pytest.raises(AuthError):
                verify_token("https://connection.keboola.com", "bad-token")

    def test_403_raises_autherror(self):
        with respx.mock as mock:
            mock.get(VERIFY_URL).mock(return_value=httpx.Response(403))
            with pytest.raises(AuthError):
                verify_token("https://connection.keboola.com", "bad-token")

    def test_500_raises_stack_unreachable(self):
        with respx.mock as mock:
            mock.get(VERIFY_URL).mock(return_value=httpx.Response(500))
            with pytest.raises(StackUnreachableError):
                verify_token("https://connection.keboola.com", "some-token")

    def test_connect_error_raises_stack_unreachable(self):
        with respx.mock as mock:
            mock.get(VERIFY_URL).mock(side_effect=httpx.ConnectError("boom"))
            with pytest.raises(StackUnreachableError):
                verify_token("https://connection.keboola.com", "some-token")

    def test_missing_token_raises_autherror_without_network(self):
        with respx.mock:
            # No route is registered for VERIFY_URL. If verify_token attempted
            # a network call before checking the token, respx would raise its
            # own "not mocked" error instead of AuthError, failing this test.
            with pytest.raises(AuthError):
                verify_token("https://connection.keboola.com", "")

    def test_owner_missing_id_raises_autherror(self):
        with respx.mock as mock:
            mock.get(VERIFY_URL).mock(return_value=httpx.Response(200, json={"owner": {}}))
            with pytest.raises(AuthError):
                verify_token("https://connection.keboola.com", "some-token")

    def test_owner_as_a_list_raises_autherror(self):
        """A 200 with a schema-drifting body must not crash the request."""
        with respx.mock as mock:
            mock.get(VERIFY_URL).mock(
                return_value=httpx.Response(200, json={"owner": [{"id": 1}]})
            )
            with pytest.raises(AuthError):
                verify_token("https://connection.keboola.com", "some-token")

    def test_owner_id_non_numeric_string_raises_autherror(self):
        with respx.mock as mock:
            mock.get(VERIFY_URL).mock(
                return_value=httpx.Response(200, json={"owner": {"id": "abc"}})
            )
            with pytest.raises(AuthError):
                verify_token("https://connection.keboola.com", "some-token")

    def test_owner_id_as_an_int_string_is_accepted(self):
        """Some stacks serialize the project id as a decimal string."""
        with respx.mock as mock:
            mock.get(VERIFY_URL).mock(
                return_value=httpx.Response(
                    200, json={"owner": {"id": "123", "name": "Proj"}}
                )
            )
            owner = verify_token("https://connection.keboola.com", "some-token")
        assert owner.project_id == 123

    def test_owner_id_boolean_raises_autherror(self):
        with respx.mock as mock:
            mock.get(VERIFY_URL).mock(
                return_value=httpx.Response(200, json={"owner": {"id": True}})
            )
            with pytest.raises(AuthError):
                verify_token("https://connection.keboola.com", "some-token")

    def test_json_null_body_raises_autherror(self):
        with respx.mock as mock:
            mock.get(VERIFY_URL).mock(
                return_value=httpx.Response(
                    200, content=b"null", headers={"content-type": "application/json"}
                )
            )
            with pytest.raises(AuthError):
                verify_token("https://connection.keboola.com", "some-token")

    def test_non_json_body_raises_stack_unreachable(self):
        """A captive portal answering 200 with HTML is an upstream problem."""
        with respx.mock as mock:
            mock.get(VERIFY_URL).mock(
                return_value=httpx.Response(
                    200, text="<html>login here</html>",
                    headers={"content-type": "text/html"},
                )
            )
            with pytest.raises(StackUnreachableError):
                verify_token("https://connection.keboola.com", "some-token")

    def test_non_scalar_owner_name_raises_autherror(self):
        with respx.mock as mock:
            mock.get(VERIFY_URL).mock(
                return_value=httpx.Response(
                    200, json={"owner": {"id": 1, "name": {"en": "Proj"}}}
                )
            )
            with pytest.raises(AuthError):
                verify_token("https://connection.keboola.com", "some-token")

    def test_missing_owner_name_defaults_to_empty(self):
        with respx.mock as mock:
            mock.get(VERIFY_URL).mock(
                return_value=httpx.Response(200, json={"owner": {"id": 7}})
            )
            owner = verify_token("https://connection.keboola.com", "some-token")
        assert owner.project_id == 7
        assert owner.project_name == ""


class TestVerifyTokenClaims:
    """SEC-075-011: the token-level claims the destructive-token policy reads.

    Every one of them is optional and must degrade to the least-privileged
    value. A stack that omits a claim, renames it, or answers with a type
    nobody expected has to leave the caller an ordinary token — never fail
    their request, and never promote them.
    """

    def _verify(self, body: dict):
        with respx.mock as mock:
            mock.get(VERIFY_URL).mock(return_value=httpx.Response(200, json=body))
            return verify_token("https://connection.keboola.com", "some-token")

    def test_full_claim_set_is_parsed(self):
        owner = self._verify(
            {
                "id": "9876",
                "description": "CI publisher",
                "isMasterToken": False,
                "canManageBuckets": True,
                "canManageTokens": True,
                "canPurgeTrash": True,
                "canReadAllFileUploads": True,
                "admin": {"role": "admin", "id": 42},
                "owner": {"id": 123, "name": "Proj"},
            }
        )
        assert owner.token_id == "9876"
        assert owner.is_master_token is False
        assert owner.admin_role == "admin"
        assert owner.can_purge_trash is True
        assert owner.can_manage_tokens is True
        assert owner.is_project_admin is True

    def test_a_body_without_claims_is_least_privileged(self):
        owner = self._verify({"owner": {"id": 123, "name": "Proj"}})
        assert owner.token_id is None
        assert owner.is_master_token is False
        assert owner.admin_role is None
        assert owner.can_purge_trash is False
        assert owner.can_manage_tokens is False
        assert owner.is_project_admin is False

    def test_master_token_flag_is_parsed(self):
        owner = self._verify(
            {"isMasterToken": True, "owner": {"id": 1, "name": "P"}}
        )
        assert owner.is_master_token is True
        assert owner.is_project_admin is True

    def test_a_numeric_token_id_is_normalized_to_a_string(self):
        """HUB_DESTRUCTIVE_TOKEN_IDS is a list of strings, so the id must be one."""
        owner = self._verify({"id": 9876, "owner": {"id": 1, "name": "P"}})
        assert owner.token_id == "9876"

    @pytest.mark.parametrize(
        "raw", [None, True, False, "", "   ", [], {}, {"id": 1}, 1.5]
    )
    def test_an_unusable_token_id_becomes_none(self, raw):
        owner = self._verify({"id": raw, "owner": {"id": 1, "name": "P"}})
        assert owner.token_id is None

    @pytest.mark.parametrize(
        "raw", ["true", "True", 1, 0, None, [], {}, "yes", "1"]
    )
    def test_only_a_real_json_true_grants_a_boolean_claim(self, raw):
        """A stringly-typed 'true' is an unverified shape, so it grants nothing."""
        owner = self._verify(
            {
                "isMasterToken": raw,
                "canPurgeTrash": raw,
                "canManageTokens": raw,
                "owner": {"id": 1, "name": "P"},
            }
        )
        assert owner.is_master_token is False
        assert owner.can_purge_trash is False
        assert owner.can_manage_tokens is False
        assert owner.is_project_admin is False

    @pytest.mark.parametrize("role", ["guest", "readOnly", "share"])
    def test_a_non_admin_role_is_kept_but_grants_nothing(self, role):
        owner = self._verify(
            {"admin": {"role": role}, "owner": {"id": 1, "name": "P"}}
        )
        assert owner.admin_role == role
        assert owner.is_project_admin is False

    def test_an_unknown_role_is_reported_and_grants_nothing(self):
        owner = self._verify(
            {"admin": {"role": "superuser"}, "owner": {"id": 1, "name": "P"}}
        )
        assert owner.admin_role == "superuser"
        assert owner.is_project_admin is False

    @pytest.mark.parametrize(
        "admin",
        [None, "admin", ["admin"], {}, {"role": None}, {"role": 1}, {"role": "  "}],
    )
    def test_an_unusable_admin_object_becomes_no_role(self, admin):
        """Including the string "admin" in the admin *slot* — shape matters."""
        owner = self._verify({"admin": admin, "owner": {"id": 1, "name": "P"}})
        assert owner.admin_role is None
        assert owner.is_project_admin is False

    def test_malformed_claims_never_fail_a_valid_identity(self):
        """The project identity rules are unchanged: claims cannot 401 anyone."""
        owner = self._verify(
            {
                "id": {"nested": "nonsense"},
                "isMasterToken": "perhaps",
                "admin": 7,
                "canPurgeTrash": [],
                "canManageTokens": {},
                "owner": {"id": 123, "name": "Proj"},
            }
        )
        assert owner.project_id == 123
        assert owner.project_name == "Proj"
        assert owner.is_project_admin is False


class TestStorageHeaders:
    """Which authentication scheme each credential shape travels under."""

    def test_a_storage_token_uses_the_storage_header(self):
        assert storage_headers("1234-abcdef", None) == {
            "X-StorageApi-Token": "1234-abcdef"
        }

    def test_a_project_id_is_irrelevant_to_a_storage_token(self):
        assert storage_headers("1234-abcdef", 123) == {
            "X-StorageApi-Token": "1234-abcdef"
        }

    def test_a_session_bearer_names_its_project(self):
        assert storage_headers("kbc_at_abc_secret", 123) == {
            "Authorization": "Bearer kbc_at_abc_secret",
            "X-KBC-ProjectId": "123",
        }

    def test_a_personal_access_token_authenticates_the_same_way(self):
        assert storage_headers("kbc_pat_abc_secret", 7) == {
            "Authorization": "Bearer kbc_pat_abc_secret",
            "X-KBC-ProjectId": "7",
        }

    def test_a_bearer_without_a_project_is_an_incomplete_credential(self):
        with pytest.raises(AuthError) as excinfo:
            storage_headers("kbc_at_abc_secret", None)
        assert "X-Storage-Project" in str(excinfo.value)


class TestVerifyBearer:
    """A signed-in session verifies through the project it names."""

    def test_the_project_it_names_becomes_the_owner(self):
        with respx.mock as mock:
            route = mock.get(VERIFY_URL).mock(
                return_value=httpx.Response(
                    200, json={"owner": {"id": 123, "name": "Proj"}}
                )
            )
            owner = verify_token(
                "https://connection.keboola.com",
                "kbc_at_abc_secret",
                project_id=123,
            )
        request = route.calls.last.request
        assert request.headers["authorization"] == "Bearer kbc_at_abc_secret"
        assert request.headers["x-kbc-projectid"] == "123"
        assert "x-storageapi-token" not in request.headers
        assert owner.key == "123@connection.keboola.com"

    def test_a_bearer_without_a_project_never_reaches_the_stack(self):
        with respx.mock as mock:
            route = mock.get(VERIFY_URL).mock(return_value=httpx.Response(200))
            with pytest.raises(AuthError):
                verify_token("https://connection.keboola.com", "kbc_at_abc_secret")
        assert not route.called

    def test_a_project_the_session_cannot_reach_is_refused(self):
        with respx.mock as mock:
            mock.get(VERIFY_URL).mock(return_value=httpx.Response(403))
            with pytest.raises(AuthError) as excinfo:
                verify_token(
                    "https://connection.keboola.com",
                    "kbc_at_abc_secret",
                    project_id=42,
                )
        assert "42" in str(excinfo.value)

    def test_an_owner_other_than_the_named_project_is_refused(self):
        """The stack resolving a different project means the call was misrouted."""
        with respx.mock as mock:
            mock.get(VERIFY_URL).mock(
                return_value=httpx.Response(
                    200, json={"owner": {"id": 999, "name": "Elsewhere"}}
                )
            )
            with pytest.raises(AuthError) as excinfo:
                verify_token(
                    "https://connection.keboola.com",
                    "kbc_at_abc_secret",
                    project_id=123,
                )
        assert "999" in str(excinfo.value)
