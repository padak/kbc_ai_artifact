"""FastAPI-layer tests for the KBC Artifact Hub.

The whole HTTP surface of ``src.main`` is exercised through a single
``api`` fixture built on top of ``fastapi.testclient.TestClient``:

- the hub's serving Storage backend is a fresh ``InMemoryFilesBackend``
  (exposed via ``api.backend`` so tests can inspect Storage state directly,
  e.g. to simulate a container restart by hydrating a second store over the
  same backend),
- ``src.main.verify_token`` is monkeypatched so ``"good-token"`` verifies as
  project 123 and ``"other-token"`` as project 999; a ``kbc_at_*``/``kbc_pat_*``
  bearer from ``_SESSION_TOKENS`` verifies as whatever project the request
  named; any other token raises ``AuthError``,
- the canonical-copy upload (``KbcFilesBackend`` constructed with the
  *caller's* token inside ``src.main._store_canonical``) is intercepted at
  the same seam (``src.main.KbcFilesBackend``) and, for any stack/token pair
  that is not the hub's own, records the call into ``api.canonical_calls``
  and returns a fake incrementing file id instead of touching real Storage,
- the TestClient's ``base_url`` is ``https://testserver`` so ``Secure``
  unlock cookies round-trip.

The fixture is function-scoped (defined at module level in this file, not in
``conftest.py``) so every test gets an isolated backend, cache dir and call
log.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import html
import io
import json
import logging
import pathlib
import re
import shutil
import subprocess
import tempfile
import zipfile
from typing import Any, NamedTuple

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import src.main as main
import src.pages as pages
from src.auth import STACK_ALIASES, AuthError, Owner, storage_headers
from src.kbclogin import is_bearer_credential
from src.builder import BuiltArtifact
import src.comments as comments_module
from src.comments import CommentStore
from src.config import load_settings
from src.kbc import BackendError, InMemoryFilesBackend
from src.security import (
    KEY_LABEL_UNLOCK_COOKIE,
    KEY_LABEL_WEBHOOK,
    CookieSigner,
    derive_key,
)
from src.statedb import StateDB
from src.webhooks import receiver_signing_key
from src.store import ArtifactStore

AUTH_HEADERS = {"X-StorageApi-Token": "good-token", "X-Kbc-Stack": "us"}
OTHER_AUTH_HEADERS = {"X-StorageApi-Token": "other-token", "X-Kbc-Stack": "us"}

#: token -> (project_id, project_name) recognized by the fake verify_token.
_OWNER_PROJECTS: dict[str, tuple[int, str]] = {
    "good-token": (123, "Test"),
    "other-token": (999, "Other"),
}

#: token -> extra ``Owner`` claim keyword arguments, mirroring the token-level
#: fields a real ``/v2/storage/tokens/verify`` body carries (SEC-075-011).
#: A token absent from this mapping gets the least-privileged defaults, which
#: is what every test written before the destructive-token policy expects. The
#: two standard tokens are master tokens, so the whole existing suite keeps
#: full authority under any HUB_DESTRUCTIVE_TOKEN_POLICY.
_TOKEN_CLAIMS: dict[str, dict[str, Any]] = {
    "good-token": {"token_id": "tok-good", "is_master_token": True},
    "other-token": {"token_id": "tok-other", "is_master_token": True},
}

#: Signed-in credentials the fake stack accepts. Unlike a Storage token these
#: name no project of their own — the request does, via X-Storage-Project.
#:
#: Their claims mirror what a stack really answers: a bearer is exchanged for
#: the admin's *own* Storage token in the named project, so verify describes
#: that token — a project administrator's, hence ``admin_role="admin"`` rather
#: than a master token.
_SESSION_TOKENS: dict[str, dict[str, Any]] = {
    "kbc_at_session_secret": {"token_id": "tok-session", "admin_role": "admin"},
    "kbc_pat_personal_secret": {"token_id": "tok-pat", "admin_role": "admin"},
}


class Api(NamedTuple):
    client: TestClient
    backend: InMemoryFilesBackend
    canonical_calls: list[dict[str, Any]]
    settings: Any


@pytest.fixture
def api(tmp_path, settings, monkeypatch):
    """A TestClient over ``src.main.app`` wired to fakes, per the module docstring."""
    test_settings = dataclasses.replace(
        settings,
        cache_dir=tmp_path / "cache",
        # The state sidecar is built by the lifespan just like in production;
        # 0 means "no background snapshot thread", so the tests drive it (and
        # its Storage writes) entirely themselves.
        state_snapshot_interval_s=0,
    )
    monkeypatch.setattr(main, "settings", test_settings)

    backend = InMemoryFilesBackend()
    canonical_calls: list[dict[str, Any]] = []
    counter = {"n": 9000}

    class _CanonicalBackend:
        """Fake KbcFilesBackend used only for the author's canonical copy."""

        def __init__(
            self, stack_url: str, token: str, project_id: int | None = None
        ) -> None:
            self.stack_url = stack_url
            self.token = token
            self.project_id = project_id

        def upload(self, name: str, content: bytes, tags: list[str]) -> int:
            counter["n"] += 1
            file_id = counter["n"]
            canonical_calls.append(
                {
                    "stack_url": self.stack_url,
                    "token": self.token,
                    "project_id": self.project_id,
                    "name": name,
                    "content": content,
                    "tags": list(tags),
                    "file_id": file_id,
                }
            )
            return file_id

        def delete(self, file_id: int) -> None:
            canonical_calls.append(
                {"stack_url": self.stack_url, "token": self.token, "deleted": file_id}
            )

    def fake_kbc_files_backend(
        stack_url: str, token: str, project_id: int | None = None
    ):
        """Route hub-project calls to the shared in-memory backend, else fake."""
        if (
            stack_url == test_settings.hub_stack_url
            and token == test_settings.hub_storage_token
        ):
            return backend
        return _CanonicalBackend(stack_url, token, project_id)

    def fake_verify_token(
        stack_url: str,
        token: str,
        timeout_s: int = 15,
        project_id: int | None = None,
    ) -> Owner:
        """Stand in for the stack, for both credential shapes.

        A Storage token is looked up in the fixture's table; a signed-in
        bearer names its project on the request instead, exactly as the real
        stack resolves ``X-KBC-ProjectId`` into an owner.
        """
        if is_bearer_credential(token):
            # Delegate the "which headers does this credential travel under"
            # question to the real code, so the fixture cannot drift from the
            # message a caller actually gets for an incomplete credential.
            storage_headers(token, project_id)
            claims = _SESSION_TOKENS.get(token)
            if claims is None:
                raise AuthError("session rejected by the stack")
            return Owner(
                stack_url=stack_url,
                project_id=project_id,
                project_name=f"project {project_id}",
                **claims,
            )
        project = _OWNER_PROJECTS.get(token)
        if project is None:
            raise AuthError("Storage token rejected by the stack")
        project_id, project_name = project
        return Owner(
            stack_url=stack_url,
            project_id=project_id,
            project_name=project_name,
            **_TOKEN_CLAIMS.get(token, {}),
        )

    monkeypatch.setattr(main, "KbcFilesBackend", fake_kbc_files_backend)
    monkeypatch.setattr(main, "verify_token", fake_verify_token)
    # Rate-limit counters live in the state sidecar, which the lifespan builds
    # fresh per test over this test's own in-memory backend and tmp cache dir —
    # so no tally leaks between tests. The module-level fallback dict is only
    # reached when no sidecar is attached; clear it anyway, since a test that
    # detaches the sidecar would otherwise inherit another test's tally.
    main._fallback_counts.clear()

    with TestClient(main.app, base_url="https://testserver") as client:
        yield Api(
            client=client,
            backend=backend,
            canonical_calls=canonical_calls,
            settings=test_settings,
        )


def _publish_markdown(
    api: Api,
    markdown: str,
    *,
    title: str | None = None,
    password: str | None = None,
    accept_versions: bool | None = None,
    headers: dict[str, str] = AUTH_HEADERS,
) -> str:
    """Publish a markdown artifact and return its id, asserting success."""
    payload: dict[str, Any] = {"markdown": markdown}
    if title is not None:
        payload["title"] = title
    if password is not None:
        payload["password"] = password
    if accept_versions is not None:
        payload["accept_versions"] = accept_versions
    resp = api.client.post("/api/artifacts", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _submit_version(
    api: Api,
    artifact_id: str,
    markdown: str,
    *,
    note: str | None = None,
    headers: dict[str, str] = AUTH_HEADERS,
):
    """POST one version; returns the raw response so tests can assert status."""
    payload: dict[str, Any] = {"markdown": markdown}
    if note is not None:
        payload["note"] = note
    return api.client.post(
        f"/api/artifacts/{artifact_id}/versions", json=payload, headers=headers
    )


def _assert_no_sensitive_keys(obj: Any) -> None:
    """Recursively assert no 'owner' or 'password' key appears anywhere."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            assert key not in ("owner", "password"), f"found sensitive key {key!r}"
            _assert_no_sensitive_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            _assert_no_sensitive_keys(item)


# --------------------------------------------------------------------------
# Platform / discovery endpoints
# --------------------------------------------------------------------------


def test_platform_probe_returns_ok(api: Api) -> None:
    resp = api.client.post("/")
    assert resp.status_code == 200
    assert resp.text == "OK"


def test_health_shape(api: Api) -> None:
    resp = api.client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "status",
        "version",
        "artifacts",
        "design_systems",
        "hydrated",
    }
    assert body["status"] == "ok"
    assert body["version"] == main.SERVICE_VERSION
    assert body["version"]
    assert isinstance(body["artifacts"], int)
    assert isinstance(body["design_systems"], int)
    assert isinstance(body["hydrated"], bool)
    assert body["hydrated"] is True
    assert body["artifacts"] == 0


def test_context_lists_all_endpoints_and_stack_aliases(api: Api) -> None:
    resp = api.client.get("/context")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == main.SERVICE_VERSION
    assert body["repository"] == main.GITHUB_REPO_URL
    assert body["auth"]["stack_aliases"] == dict(STACK_ALIASES)
    paths = {(e["method"], e["path"]) for e in body["endpoints"]}
    expected = {
        ("GET", "/"),
        ("POST", "/"),
        ("GET", "/health"),
        ("GET", "/health/headers"),
        ("GET", "/context"),
        ("GET", "/skill"),
        ("GET", "/llms.txt"),
        ("GET", "/login"),
        ("POST", "/login/device"),
        ("POST", "/login/device/token"),
        ("GET", "/login/pkce/start"),
        ("GET", "/login/callback"),
        ("POST", "/login/refresh"),
        ("POST", "/login/signout"),
        ("GET", "/agent"),
        ("GET", "/changelog"),
        ("GET", "/changelog.md"),
        ("GET", "/admin"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
        ("GET", "/a/{id}"),
        ("POST", "/a/{id}/unlock"),
        ("GET", "/a/{id}/raw"),
        ("GET", "/a/{id}/source"),
        ("GET", "/a/{id}/meta"),
        ("GET", "/a/{id}/live"),
        ("GET", "/a/{id}/v/{n}"),
        ("GET", "/a/{id}/versions"),
        ("GET", "/a/{id}/diff/{a}..{b}"),
        ("GET", "/a/{id}/comments"),
        ("GET", "/a/{id}/guest"),
        ("GET", "/a/{id}/review"),
        ("GET", "/a/{id}/export/markdown"),
        ("GET", "/a/{id}/export/vault"),
        ("POST", "/api/artifacts"),
        ("PUT", "/api/artifacts/{id}"),
        ("GET", "/api/artifacts"),
        ("DELETE", "/api/artifacts/{id}"),
        ("POST", "/api/artifacts/{id}/restore"),
        ("DELETE", "/api/artifacts/{id}/purge"),
        ("GET", "/api/artifacts/{id}/webhooks"),
        ("POST", "/api/artifacts/{id}/webhooks/{receiver_id}/rotate-key"),
        ("POST", "/api/artifacts/{id}/rotate-link"),
        ("GET", "/api/artifacts/{id}/stats"),
        ("POST", "/api/artifacts/{id}/invitations"),
        ("GET", "/api/artifacts/{id}/invitations"),
        ("DELETE", "/api/artifacts/{id}/invitations/{iid}"),
        ("POST", "/api/artifacts/{id}/versions"),
        ("POST", "/api/artifacts/{id}/versions/{n}/promote"),
        ("DELETE", "/api/artifacts/{id}/versions/{n}"),
        ("PUT", "/api/artifacts/{id}/head"),
        ("POST", "/api/artifacts/{id}/comments"),
        ("POST", "/api/artifacts/{id}/comments/{tid}/replies"),
        ("POST", "/api/artifacts/{id}/comments/{tid}/resolve"),
        ("DELETE", "/api/artifacts/{id}/comments/{tid}"),
        ("GET", "/api/design-systems"),
        ("POST", "/api/design-systems"),
        ("GET", "/api/design-systems/{ref}"),
        ("PUT", "/api/design-systems/{ref}"),
        ("POST", "/api/design-systems/{ref}/versions"),
        ("POST", "/api/design-systems/{ref}/fork"),
        ("DELETE", "/api/design-systems/{ref}/versions/{n}"),
        ("DELETE", "/api/design-systems/{ref}"),
        ("GET", "/ds"),
        ("GET", "/ds?format=json"),
        ("GET", "/ds/{ref}"),
        ("GET", "/ds/{ref}/versions"),
        ("GET", "/ds/{ref}/bundle"),
        ("GET", "/ds/{ref}/tokens"),
        ("GET", "/ds/{ref}/css"),
        ("GET", "/ds/{ref}/starter"),
        ("GET", "/ds/{ref}/guidance"),
    }
    assert paths == expected
    assert len(body["endpoints"]) == len(expected)


def test_skill_returns_markdown(api: Api) -> None:
    resp = api.client.get("/skill")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.text.strip() != ""


def test_agent_serves_the_agent_definition_file(api: Api) -> None:
    """/agent must serve agents/artifact-hub.md verbatim.

    The expectation is read from disk rather than hard-coded so the test keeps
    passing while the definition itself is rewritten.
    """
    expected = main.AGENT_PATH.read_text(encoding="utf-8")
    resp = api.client.get("/agent")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.text == expected
    # Echoing the first line proves it is *that* file, not some other markdown.
    assert resp.text.splitlines()[0] == expected.splitlines()[0]


def test_context_documents_the_agent_and_admin_endpoints(api: Api) -> None:
    body = api.client.get("/context").json()
    by_path = {e["path"]: e for e in body["endpoints"]}

    agent = by_path["/agent"]
    assert agent["method"] == "GET"
    assert agent["auth"] == "none"
    assert "subagent" in agent["purpose"].lower()
    assert "~/.claude/agents" in agent["purpose"]

    admin = by_path["/admin"]
    assert admin["method"] == "GET"
    assert "sessionStorage" in admin["purpose"]


def test_changelog_renders_the_repository_file_as_html(api: Api) -> None:
    resp = api.client.get("/changelog")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "<h1" in resp.text
    assert "Changelog" in resp.text


def test_changelog_md_serves_the_raw_file(api: Api) -> None:
    """Echoes the file's own first line so this survives a rewritten placeholder."""
    expected_first_line = main.CHANGELOG_PATH.read_text(
        encoding="utf-8"
    ).splitlines()[0]
    resp = api.client.get("/changelog.md")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.text.splitlines()[0] == expected_first_line


# --------------------------------------------------------------------------
# Interactive API docs
# --------------------------------------------------------------------------


def test_docs_serves_swagger_ui(api: Api) -> None:
    resp = api.client.get("/docs")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "swagger" in resp.text.lower()


def test_openapi_json_has_expected_paths_and_security_schemes(api: Api) -> None:
    resp = api.client.get("/openapi.json")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    schema = resp.json()

    assert "openapi" in schema
    assert "/api/artifacts" in schema["paths"]
    assert "/a/{artifact_id}" in schema["paths"]

    security_schemes = schema["components"]["securitySchemes"]
    scheme_headers = {s["name"] for s in security_schemes.values() if "name" in s}
    assert "X-StorageApi-Token" in scheme_headers
    assert "X-Storage-Stack" in scheme_headers
    assert "X-Storage-Project" in scheme_headers
    # The bearer spelling is an http scheme, not a named header.
    assert security_schemes["KeboolaBearer"] == {
        "type": "http",
        "scheme": "bearer",
        "description": security_schemes["KeboolaBearer"]["description"],
    }


def test_openapi_json_never_leaks_the_hub_storage_token(api: Api) -> None:
    resp = api.client.get("/openapi.json")
    assert resp.status_code == 200
    assert api.settings.hub_storage_token not in resp.text


# --------------------------------------------------------------------------
# OpenAPI documentation audit
#
# These tests are the standing guarantee that /docs stays usable: every
# operation explains itself, and every parameter a caller can send is
# described in the schema. They are mechanical on purpose — a new route or a
# new parameter that skips its documentation fails the suite.
# --------------------------------------------------------------------------

#: The tags declared on the app; every operation must carry exactly one.
_TAGS = {
    "public",
    "artifacts",
    "versions",
    "comments",
    "design systems",
    "service",
}


def _operations(schema: dict) -> list[tuple[str, str, dict]]:
    """Every (method, path, operation) in the generated document."""
    out: list[tuple[str, str, dict]] = []
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            if isinstance(operation, dict):
                out.append((method.upper(), path, operation))
    return out


def test_openapi_every_operation_has_summary_description_and_one_tag(
    api: Api,
) -> None:
    schema = api.client.get("/openapi.json").json()
    operations = _operations(schema)
    assert operations, "no operations in the OpenAPI document"
    for method, path, operation in operations:
        where = f"{method} {path}"
        assert operation.get("summary", "").strip(), f"{where}: empty summary"
        assert (
            operation.get("description", "").strip()
        ), f"{where}: empty description"
        tags = operation.get("tags") or []
        assert len(tags) == 1, f"{where}: expected exactly one tag, got {tags}"
        assert tags[0] in _TAGS, f"{where}: unknown tag {tags[0]!r}"


def test_openapi_every_parameter_is_described(api: Api) -> None:
    schema = api.client.get("/openapi.json").json()
    seen = 0
    for method, path, operation in _operations(schema):
        for parameter in operation.get("parameters", []):
            seen += 1
            where = f"{method} {path} [{parameter.get('in')} {parameter.get('name')}]"
            description = parameter.get("description") or parameter.get(
                "schema", {}
            ).get("description")
            assert description and description.strip(), f"{where}: no description"
    assert seen, "no parameters found; the audit would be vacuous"


def test_openapi_every_operation_documents_its_responses(api: Api) -> None:
    schema = api.client.get("/openapi.json").json()
    for method, path, operation in _operations(schema):
        responses = operation.get("responses") or {}
        assert responses, f"{method} {path}: no responses documented"
        for code, response in responses.items():
            where = f"{method} {path} -> {code}"
            assert (
                response.get("description", "").strip()
            ), f"{where}: response without a description"


def test_openapi_api_operations_carry_the_security_schemes(api: Api) -> None:
    """/api/* is header-authenticated; the public read path must not claim to be."""
    schema = api.client.get("/openapi.json").json()
    for method, path, operation in _operations(schema):
        if path.startswith("/api/"):
            security = operation.get("security")
            assert security, f"{method} {path}: missing security requirement"
            # Two alternatives, either of which authenticates on its own: the
            # header pair, or the standard bearer spelling plus the project it
            # acts as. The project header belongs to the bearer alternative
            # alone: a Storage token names its own project and the header is
            # ignored for it.
            alternatives = [set(option) for option in security]
            assert alternatives == [
                {"StorageApiToken", "StorageStack"},
                {"KeboolaBearer", "StorageStack", "StorageProject"},
            ], f"{method} {path}: unexpected security {alternatives}"
        else:
            assert (
                "security" not in operation
            ), f"{method} {path}: public route must not require credentials"


def _parameter(schema: dict, path: str, method: str, name: str) -> dict:
    for parameter in schema["paths"][path][method].get("parameters", []):
        if parameter["name"] == name:
            return parameter
    raise AssertionError(f"{method.upper()} {path} does not document {name!r}")


def test_openapi_documents_the_versions_format_query_parameter(api: Api) -> None:
    schema = api.client.get("/openapi.json").json()
    parameter = _parameter(schema, "/a/{artifact_id}/versions", "get", "format")
    assert parameter["in"] == "query"
    assert parameter["required"] is False
    assert parameter["schema"]["default"] == "json"
    assert parameter["schema"]["enum"] == ["json", "html"]
    assert "html" in parameter["description"]


def test_openapi_distinguishes_version_status_from_document_status(api: Api) -> None:
    """/docs must say which "status" is which on every artifact-describing route."""
    schema = api.client.get("/openapi.json").json()
    for path in (
        "/a/{artifact_id}/meta",
        "/a/{artifact_id}/versions",
        "/a/{artifact_id}/comments",
        "/api/artifacts",
    ):
        description = schema["paths"][path]["get"]["description"]
        assert "document_status" in description, path
        assert "proposed" in description, path


def test_openapi_documents_the_diff_format_query_parameter(api: Api) -> None:
    schema = api.client.get("/openapi.json").json()
    parameter = _parameter(schema, "/a/{artifact_id}/diff/{spec}", "get", "format")
    assert parameter["in"] == "query"
    assert parameter["schema"]["default"] == "html"
    assert parameter["schema"]["enum"] == ["html", "unified", "json", "visual"]
    assert "unified" in parameter["description"]


def test_openapi_documents_the_path_parameters(api: Api) -> None:
    schema = api.client.get("/openapi.json").json()

    spec = _parameter(schema, "/a/{artifact_id}/diff/{spec}", "get", "spec")
    assert spec["in"] == "path"
    assert "OLD..NEW" in spec["description"]

    artifact_id = _parameter(schema, "/a/{artifact_id}", "get", "artifact_id")
    assert "capability" in artifact_id["description"]

    version = _parameter(
        schema, "/a/{artifact_id}/v/{version}", "get", "version"
    )
    assert "version" in version["description"].lower()


def test_openapi_declares_non_json_response_content_types(api: Api) -> None:
    """HTML pages, markdown and the unified diff must not look like JSON."""
    schema = api.client.get("/openapi.json").json()

    def content(path: str, method: str, code: str) -> set[str]:
        return set(
            schema["paths"][path][method]["responses"][code].get("content", {})
        )

    assert "text/html" in content("/", "get", "200")
    assert "text/html" in content("/admin", "get", "200")
    assert "text/plain" in content("/", "post", "200")
    assert any(
        media.startswith("text/markdown") for media in content("/skill", "get", "200")
    )
    assert any(
        media.startswith("text/markdown") for media in content("/agent", "get", "200")
    )
    assert "text/html" in content("/a/{artifact_id}", "get", "200")
    diff_content = content("/a/{artifact_id}/diff/{spec}", "get", "200")
    assert {"text/html", "text/plain", "application/json"} <= diff_content


def test_openapi_request_bodies_carry_examples_and_field_descriptions(
    api: Api,
) -> None:
    schema = api.client.get("/openapi.json").json()
    schemas = schema["components"]["schemas"]
    for name in (
        "PublishBody",
        "UpdateBody",
        "VersionBody",
        "HeadBody",
        "CommentBody",
        "ReplyBody",
        "ResolveBody",
    ):
        model = schemas[name]
        assert model.get("examples"), f"{name}: no request-body example"
        for field, definition in model["properties"].items():
            assert definition.get(
                "description", ""
            ).strip(), f"{name}.{field}: no description"


def test_openapi_unlock_form_field_is_documented(api: Api) -> None:
    schema = api.client.get("/openapi.json").json()
    body = schema["paths"]["/a/{artifact_id}/unlock"]["post"]["requestBody"]
    assert "application/x-www-form-urlencoded" in body["content"]
    form_schema_ref = body["content"]["application/x-www-form-urlencoded"][
        "schema"
    ]["$ref"].rsplit("/", 1)[-1]
    form_schema = schema["components"]["schemas"][form_schema_ref]
    assert form_schema["properties"]["password"]["description"].strip()


# --------------------------------------------------------------------------
# Public origin behind the platform proxy
# --------------------------------------------------------------------------

PROXY_HEADERS = {
    "X-Forwarded-Host": "artifact-hub.example.com",
    "X-Forwarded-Proto": "https",
}


@pytest.fixture
def trusted_proxy(api: Api, monkeypatch) -> Api:
    """``api`` with HUB_TRUST_FORWARDED_HEADERS on.

    Forwarded headers are ignored by default (any client can forge them), so
    every test that expects them to name the public origin has to opt in the
    way a proxied deployment does.
    """
    monkeypatch.setattr(
        main,
        "settings",
        dataclasses.replace(api.settings, trust_forwarded_headers=True),
    )
    return api


def test_forwarded_headers_are_ignored_unless_trusted(api: Api) -> None:
    """Default deployment: a forged X-Forwarded-Host must not be honored."""
    artifact_id = _publish_markdown(api, "# Hello")
    resp = api.client.get(
        f"/a/{artifact_id}/", headers=PROXY_HEADERS, follow_redirects=False
    )
    assert resp.status_code == 307
    assert resp.headers["location"] == f"https://testserver/a/{artifact_id}"

    published = api.client.post(
        "/api/artifacts",
        json={"markdown": "# Hello"},
        headers={**AUTH_HEADERS, **PROXY_HEADERS},
    )
    assert published.status_code == 201
    body = published.json()
    assert body["url"] == f"https://testserver/a/{body['id']}"
    assert "artifact-hub.example.com" not in published.text


def test_trailing_slash_redirect_uses_the_forwarded_origin(
    trusted_proxy: Api,
) -> None:
    """The 307 must not leak the internal cluster hostname (see issue)."""
    api = trusted_proxy
    artifact_id = _publish_markdown(api, "# Hello")
    resp = api.client.get(
        f"/a/{artifact_id}/", headers=PROXY_HEADERS, follow_redirects=False
    )
    assert resp.status_code == 307
    location = resp.headers["location"]
    assert location.startswith(f"https://artifact-hub.example.com/a/{artifact_id}")
    assert "cluster.local" not in location
    assert "testserver" not in location


def test_api_trailing_slash_redirect_uses_the_forwarded_origin(
    trusted_proxy: Api,
) -> None:
    api = trusted_proxy
    resp = api.client.get(
        "/api/artifacts/",
        headers={**AUTH_HEADERS, **PROXY_HEADERS},
        follow_redirects=False,
    )
    assert resp.status_code == 307
    location = resp.headers["location"]
    assert location.startswith("https://artifact-hub.example.com/api/artifacts")
    assert "cluster.local" not in location
    assert "testserver" not in location


def test_public_base_url_wins_over_forwarded_headers_in_redirects(
    api: Api, monkeypatch
) -> None:
    monkeypatch.setattr(
        main,
        "settings",
        dataclasses.replace(
            api.settings,
            public_base_url="https://hub.example.org",
            trust_forwarded_headers=True,
        ),
    )
    artifact_id = _publish_markdown(api, "# Hello")
    resp = api.client.get(
        f"/a/{artifact_id}/", headers=PROXY_HEADERS, follow_redirects=False
    )
    assert resp.status_code == 307
    assert resp.headers["location"].startswith(
        f"https://hub.example.org/a/{artifact_id}"
    )


def test_redirect_without_forwarded_headers_is_unchanged(api: Api) -> None:
    """Regression: no proxy headers, no public base URL — behavior as before."""
    artifact_id = _publish_markdown(api, "# Hello")
    resp = api.client.get(f"/a/{artifact_id}/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == f"https://testserver/a/{artifact_id}"


def test_malformed_forwarded_host_is_ignored(trusted_proxy: Api) -> None:
    """A broken forwarded value must never become the redirect target."""
    api = trusted_proxy
    artifact_id = _publish_markdown(api, "# Hello")
    resp = api.client.get(
        f"/a/{artifact_id}/",
        headers={"X-Forwarded-Host": "  ", "X-Forwarded-Proto": "  "},
        follow_redirects=False,
    )
    assert resp.status_code == 307
    assert resp.headers["location"] == f"https://testserver/a/{artifact_id}"


def test_payload_urls_still_use_the_forwarded_host(trusted_proxy: Api) -> None:
    """Regression: the JSON URLs that were already correct stay correct."""
    api = trusted_proxy
    resp = api.client.post(
        "/api/artifacts",
        json={"markdown": "# Hello"},
        headers={**AUTH_HEADERS, **PROXY_HEADERS},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["url"] == f"https://artifact-hub.example.com/a/{body['id']}"
    assert body["versions_url"] == f"{body['url']}/versions"


# --------------------------------------------------------------------------
# Publish
# --------------------------------------------------------------------------


def test_publish_markdown_returns_full_shape_and_records_canonical_upload(
    api: Api,
) -> None:
    resp = api.client.post(
        "/api/artifacts",
        json={"markdown": "# Hello\n\nWorld", "title": "Hello doc"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 201
    body = resp.json()
    for key in ("id", "url", "raw_url", "source_url", "meta_url"):
        assert key in body and body[key]
    artifact_id = body["id"]
    assert body["url"] == f"https://testserver/a/{artifact_id}"
    assert body["raw_url"] == f"{body['url']}/raw"
    assert body["source_url"] == f"{body['url']}/source"
    assert body["meta_url"] == f"{body['url']}/meta"

    assert len(api.canonical_calls) == 1
    call = api.canonical_calls[0]
    assert call["tags"] == ["kbc-artifact", f"artifact-id-{artifact_id}"]
    assert call["file_id"] == body["canonical_file_id"]


def test_read_artifact_renders_html_with_no_index_headers(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title\n\nBody text")
    resp = api.client.get(f"/a/{artifact_id}")
    assert resp.status_code == 200
    assert resp.headers["x-robots-tag"] == "noindex, nofollow"
    assert resp.headers["cache-control"] == "no-cache"
    assert "Body text" in resp.text


def test_read_artifact_sandboxes_the_document_in_an_iframe(api: Api) -> None:
    """The artifact must never execute on the hub's own origin (sec-web)."""
    artifact_id = _publish_markdown(api, "# Title\n\nBody text")
    raw = api.client.get(f"/a/{artifact_id}/raw")
    page = api.client.get(f"/a/{artifact_id}")
    assert page.status_code == 200

    # A sandbox without allow-same-origin: the document gets an opaque origin.
    assert 'sandbox="allow-scripts allow-popups allow-forms allow-downloads"' in (
        page.text
    )
    assert "allow-same-origin" not in page.text
    assert page.headers["content-security-policy"] == "frame-ancestors 'self'"

    # The whole document is inside srcdoc, html-escaped — with the shell's own
    # scroll reporter appended to it, which is the only way a frame that has no
    # allow-same-origin can tell the wrapper whether the reader has scrolled.
    embedded = pages._inject_before_body_end(raw.text, pages._SRCDOC_JS)
    assert f'srcdoc="{html.escape(embedded, quote=True)}"' in page.text
    # ...so none of the artifact's own markup is live at top level.
    assert "<h1" not in page.text
    assert "&lt;h1" in page.text
    # The scripts that *are* live at top level are the shell's own, and they
    # gain the frame no capability: the sandbox attribute is unchanged above.
    assert "window.AHLive" in page.text


def test_read_version_is_sandboxed_too(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Only version")
    page = api.client.get(f"/a/{artifact_id}/v/1")
    assert page.status_code == 200
    assert "sandbox=" in page.text
    assert "allow-same-origin" not in page.text
    assert "<h1" not in page.text
    assert "&lt;h1" in page.text


def test_raw_is_unchanged_byte_exact_html(api: Api) -> None:
    """/raw stays the artifact itself: machines get the bytes, not a wrapper."""
    artifact_id = _publish_markdown(api, "# Title\n\nRaw check")
    envelope = main.app.state.store.get_head(artifact_id)
    assert envelope is not None
    raw = api.client.get(f"/a/{artifact_id}/raw")
    assert raw.status_code == 200
    assert raw.text == envelope.html
    assert "srcdoc" not in raw.text


def test_source_returns_original_markdown(api: Api) -> None:
    md = "# Hi\n\nSome *text* here."
    artifact_id = _publish_markdown(api, md)
    resp = api.client.get(f"/a/{artifact_id}/source")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.text == md


def test_raw_carries_opaque_origin_sandbox_csp(api: Api) -> None:
    """Top-level publisher HTML must land in an opaque origin, not the hub's."""
    artifact_id = _publish_markdown(api, "# Sandboxed")
    raw = api.client.get(f"/a/{artifact_id}/raw")
    assert raw.status_code == 200
    csp = raw.headers["content-security-policy"]
    assert "sandbox" in csp
    assert "allow-scripts" in csp
    # allow-same-origin would hand the document the hub origin back and undo
    # the isolation entirely.
    assert "allow-same-origin" not in csp
    assert raw.headers["x-content-type-options"] == "nosniff"


def test_source_html_branch_carries_sandbox_csp(api: Api) -> None:
    html_doc = "<html><body><h1>Hi</h1></body></html>"
    resp = api.client.post(
        "/api/artifacts", json={"html": html_doc}, headers=AUTH_HEADERS
    )
    assert resp.status_code == 201
    artifact_id = resp.json()["id"]
    source = api.client.get(f"/a/{artifact_id}/source")
    assert source.status_code == 200
    csp = source.headers["content-security-policy"]
    assert "sandbox" in csp
    assert "allow-same-origin" not in csp
    assert source.headers["x-content-type-options"] == "nosniff"


def test_source_markdown_branch_has_no_sandbox_csp(api: Api) -> None:
    """Markdown is inert text — it gets no sandbox CSP and stays text/markdown."""
    artifact_id = _publish_markdown(api, "# Plain\n\ntext")
    resp = api.client.get(f"/a/{artifact_id}/source")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "content-security-policy" not in resp.headers


def test_sandbox_headers_do_not_alter_raw_bytes(api: Api) -> None:
    """Headers only: the body stays byte-identical to what was published."""
    html_doc = "<html><body><h1>Exact</h1><script>void 0;</script></body></html>"
    resp = api.client.post(
        "/api/artifacts", json={"html": html_doc}, headers=AUTH_HEADERS
    )
    assert resp.status_code == 201
    artifact_id = resp.json()["id"]
    raw = api.client.get(f"/a/{artifact_id}/raw")
    assert raw.status_code == 200
    assert raw.content == html_doc.encode()
    source = api.client.get(f"/a/{artifact_id}/source")
    assert source.content == html_doc.encode()


def test_meta_public_shape_has_no_owner_or_password(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Hi there")
    resp = api.client.get(f"/a/{artifact_id}/meta")
    assert resp.status_code == 200
    body = resp.json()
    _assert_no_sensitive_keys(body)
    for key in (
        "id",
        "title",
        "source_type",
        "created_at",
        "updated_at",
        "version",
        "protected",
        "size_bytes",
        "url",
        "raw_url",
        "source_url",
        "meta_url",
    ):
        assert key in body
    assert body["protected"] is False


def test_publish_html_source_matches_submission_exactly(api: Api) -> None:
    html_doc = "<html><body><h1>Hi</h1></body></html>"
    resp = api.client.post(
        "/api/artifacts", json={"html": html_doc}, headers=AUTH_HEADERS
    )
    assert resp.status_code == 201
    artifact_id = resp.json()["id"]
    source = api.client.get(f"/a/{artifact_id}/source")
    assert source.status_code == 200
    assert source.text == html_doc


def test_publish_both_html_and_markdown_is_422(api: Api) -> None:
    resp = api.client.post(
        "/api/artifacts",
        json={"html": "<p>x</p>", "markdown": "# x"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422


def test_publish_neither_content_field_is_422(api: Api) -> None:
    resp = api.client.post(
        "/api/artifacts", json={"title": "Empty"}, headers=AUTH_HEADERS
    )
    assert resp.status_code == 422


def test_publish_oversized_html_is_413(api: Api, monkeypatch) -> None:
    tiny_settings = dataclasses.replace(api.settings, max_html_bytes=10)
    monkeypatch.setattr(main, "settings", tiny_settings)
    resp = api.client.post(
        "/api/artifacts",
        json={"html": "<html>" * 5},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 413


def test_publish_missing_token_header_is_401(api: Api) -> None:
    resp = api.client.post(
        "/api/artifacts",
        json={"html": "<p>hi</p>"},
        headers={"X-Kbc-Stack": "us"},
    )
    assert resp.status_code == 401


def test_publish_bad_token_is_401(api: Api) -> None:
    resp = api.client.post(
        "/api/artifacts",
        json={"html": "<p>hi</p>"},
        headers={"X-StorageApi-Token": "totally-wrong", "X-Kbc-Stack": "us"},
    )
    assert resp.status_code == 401


def test_publish_bad_stack_header_is_400(api: Api) -> None:
    resp = api.client.post(
        "/api/artifacts",
        json={"html": "<p>hi</p>"},
        headers={"X-StorageApi-Token": "good-token", "X-Kbc-Stack": "not-a-stack"},
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# Private-repository publishing (transient git credentials)
# --------------------------------------------------------------------------

#: Token used by the private-repo tests; distinctive so a substring search for
#: it across a whole serialized response/envelope is meaningful.
GIT_TOKEN = "ghp_TestOnlyPrivateRepoToken0001"


def test_publish_git_token_without_git_url_is_422(api: Api) -> None:
    resp = api.client.post(
        "/api/artifacts", json={"git_token": GIT_TOKEN}, headers=AUTH_HEADERS
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "git_url" in detail and "git_token" in detail


def test_publish_git_username_with_markdown_is_422(api: Api) -> None:
    resp = api.client.post(
        "/api/artifacts",
        json={"markdown": "# Hi", "git_username": "someone"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422
    assert "git_url" in resp.json()["detail"]


def test_a_failed_revocation_does_not_look_revoked(api: Api, monkeypatch) -> None:
    """A revocation that could not be persisted must not appear to have taken.

    store.get_meta answers from cache, so setting revoked on the invitation
    before save_meta succeeds marks it revoked on this replica only. The
    owner is told 502, so they know to retry -- but a later list on this
    same replica shows it revoked, and a restart rehydrates from Storage and
    brings the guest back. A revocation that silently undoes itself is worse
    than one that plainly failed.
    """
    artifact_id = _publish_markdown(api, "# One")
    minted = api.client.post(
        f"/api/artifacts/{artifact_id}/invitations",
        json={"name": "Jana"},
        headers=AUTH_HEADERS,
    )
    assert minted.status_code == 201, minted.text
    invitation_id = minted.json()["invitation_id"]

    store = main.app.state.store
    working_save = store.save_meta

    def failing_save(_meta):
        raise BackendError("storage is down")

    # Restored by hand rather than with monkeypatch.undo(), which would also
    # roll back the api fixture's own patches (verify_token among them).
    store.save_meta = failing_save
    try:
        refused = api.client.delete(
            f"/api/artifacts/{artifact_id}/invitations/{invitation_id}",
            headers=AUTH_HEADERS,
        )
        assert refused.status_code == 502, refused.text
    finally:
        store.save_meta = working_save

    listed = api.client.get(
        f"/api/artifacts/{artifact_id}/invitations", headers=AUTH_HEADERS
    ).json()["invitations"]
    assert listed[0]["revoked"] is False


def test_a_successful_revocation_is_visible_immediately(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    invitation_id = api.client.post(
        f"/api/artifacts/{artifact_id}/invitations",
        json={"name": "Jana"},
        headers=AUTH_HEADERS,
    ).json()["invitation_id"]

    assert (
        api.client.delete(
            f"/api/artifacts/{artifact_id}/invitations/{invitation_id}",
            headers=AUTH_HEADERS,
        ).status_code
        == 200
    )
    listed = api.client.get(
        f"/api/artifacts/{artifact_id}/invitations", headers=AUTH_HEADERS
    ).json()["invitations"]
    assert listed[0]["revoked"] is True


class TestFrozenDocumentsFreezeModeration:
    """A frozen document blocked new comments but not the state of old ones.

    "Final" and "trashed" are advertised as freezing contributions, yet
    resolve, reopen and an author's withdrawal all still rewrote the
    discussion afterwards -- so the frozen record was not actually fixed.
    """

    def test_resolving_a_thread_on_a_final_document_is_409(self, api: Api) -> None:
        artifact_id = _publish_markdown(api, "# One")
        thread_id = _comment(api, artifact_id, exact="One").json()["id"]
        assert _policy(api, artifact_id, status="final").status_code == 200

        resp = api.client.post(
            f"/api/artifacts/{artifact_id}/comments/{thread_id}/resolve",
            json={"resolved": True},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"] == "document is final"

    def test_reopening_a_thread_on_a_final_document_is_409(self, api: Api) -> None:
        artifact_id = _publish_markdown(api, "# One")
        thread_id = _comment(api, artifact_id, exact="One").json()["id"]
        assert (
            api.client.post(
                f"/api/artifacts/{artifact_id}/comments/{thread_id}/resolve",
                json={"resolved": True},
                headers=AUTH_HEADERS,
            ).status_code
            == 200
        )
        assert _policy(api, artifact_id, status="final").status_code == 200

        resp = api.client.post(
            f"/api/artifacts/{artifact_id}/comments/{thread_id}/resolve",
            json={"resolved": False},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 409, resp.text

    def test_resolving_on_a_trashed_document_says_trashed(self, api: Api) -> None:
        artifact_id = _publish_markdown(api, "# One")
        thread_id = _comment(api, artifact_id, exact="One").json()["id"]
        assert (
            api.client.delete(
                f"/api/artifacts/{artifact_id}", headers=AUTH_HEADERS
            ).status_code
            == 200
        )

        resp = api.client.post(
            f"/api/artifacts/{artifact_id}/comments/{thread_id}/resolve",
            json={"resolved": True},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"] == "document is trashed"

    def test_the_owner_can_still_delete_a_thread_on_a_final_document(
        self, api: Api
    ) -> None:
        """Deliberately not frozen: removal must stay possible.

        Freezing this would mean a comment that has to come off a finished
        document -- a leaked secret, someone's personal data -- never could,
        short of destroying the whole document.
        """
        artifact_id = _publish_markdown(api, "# One")
        thread_id = _comment(api, artifact_id, exact="One").json()["id"]
        assert _policy(api, artifact_id, status="final").status_code == 200

        resp = api.client.delete(
            f"/api/artifacts/{artifact_id}/comments/{thread_id}",
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200, resp.text

    def test_an_author_cannot_withdraw_their_thread_once_frozen(
        self, api: Api
    ) -> None:
        """Withdrawal is a contribution-shaped change; owner moderation is not."""
        artifact_id = _publish_markdown(api, "# One", accept_versions=True)
        authored = _comment(api, artifact_id, exact="One", headers=OTHER_AUTH_HEADERS)
        assert authored.status_code == 201, authored.text
        thread_id = authored.json()["id"]
        assert _policy(api, artifact_id, status="final").status_code == 200

        resp = api.client.delete(
            f"/api/artifacts/{artifact_id}/comments/{thread_id}",
            headers=OTHER_AUTH_HEADERS,
        )
        assert resp.status_code == 409, resp.text

    def test_a_draft_document_still_allows_all_of_it(self, api: Api) -> None:
        artifact_id = _publish_markdown(api, "# One", accept_versions=True)
        authored = _comment(api, artifact_id, exact="One", headers=OTHER_AUTH_HEADERS)
        thread_id = authored.json()["id"]
        assert (
            api.client.post(
                f"/api/artifacts/{artifact_id}/comments/{thread_id}/resolve",
                json={"resolved": True},
                headers=AUTH_HEADERS,
            ).status_code
            == 200
        )
        assert (
            api.client.delete(
                f"/api/artifacts/{artifact_id}/comments/{thread_id}",
                headers=OTHER_AUTH_HEADERS,
            ).status_code
            == 200
        )


def test_a_rejected_reply_does_not_linger_in_the_thread(
    api: Api, monkeypatch
) -> None:
    """A write that was refused must leave no trace in what readers see.

    CommentStore.get returns the *cached* thread object, so appending a reply
    to it before the write succeeds mutates what every later read on this
    replica returns -- a reply that was refused would still be shown, and
    would be persisted by whatever wrote the thread next.
    """
    monkeypatch.setattr(comments_module, "MAX_REPLIES_PER_THREAD", 1)
    artifact_id = _publish_markdown(api, "# One")
    created = _comment(api, artifact_id, exact="One")
    assert created.status_code == 201
    thread_id = created.json()["id"]

    accepted = api.client.post(
        f"/api/artifacts/{artifact_id}/comments/{thread_id}/replies",
        json={"body": "first reply"},
        headers=AUTH_HEADERS,
    )
    assert accepted.status_code == 201, accepted.text

    refused = api.client.post(
        f"/api/artifacts/{artifact_id}/comments/{thread_id}/replies",
        json={"body": "one too many"},
        headers=AUTH_HEADERS,
    )
    assert refused.status_code == 422, refused.text

    threads = api.client.get(f"/a/{artifact_id}/comments").json()["threads"]
    bodies = [reply["body"] for reply in threads[0]["replies"]]
    assert bodies == ["first reply"]


def test_a_rejected_resolve_leaves_the_thread_open(api: Api, monkeypatch) -> None:
    """Same rule for resolve: a refused write must not change what readers see."""
    artifact_id = _publish_markdown(api, "# One")
    created = _comment(api, artifact_id, exact="One")
    assert created.status_code == 201
    thread_id = created.json()["id"]

    # Any write is over budget now, so the resolve is refused at the store.
    monkeypatch.setattr(comments_module, "MAX_THREAD_BYTES", 1)
    refused = api.client.post(
        f"/api/artifacts/{artifact_id}/comments/{thread_id}/resolve",
        json={"resolved": True},
        headers=AUTH_HEADERS,
    )
    assert refused.status_code == 422, refused.text

    monkeypatch.undo()
    threads = api.client.get(f"/a/{artifact_id}/comments").json()["threads"]
    assert threads[0]["resolved"] is False


def test_publish_with_a_commit_id_git_ref_is_422_with_a_usable_message(
    api: Api,
) -> None:
    """The contract says branch or tag, so a commit id gets a clear refusal.

    Checked before the clone -- and before any DNS -- so the caller learns
    what was wrong with its request instead of an opaque git failure.
    """
    resp = api.client.post(
        "/api/artifacts",
        json={
            "git_url": "https://github.com/org/repo",
            "git_ref": "0" * 40,
        },
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "commit id" in detail
    assert "branch or a tag" in detail


def test_openapi_does_not_promise_commit_ids_for_git_ref(api: Api) -> None:
    schema = api.client.get("/openapi.json").json()
    for name in ("PublishBody", "UpdateBody"):
        body = schema["components"]["schemas"].get(name)
        if body is None or "git_ref" not in body.get("properties", {}):
            continue
        description = body["properties"]["git_ref"]["description"]
        assert "branch or tag" in description or "branch or a tag" in description
        assert "tag or commit" not in description


def test_put_git_token_without_git_url_is_422(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Original")
    resp = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"markdown": "# Updated", "git_token": GIT_TOKEN},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422
    assert "git_url" in resp.json()["detail"]


def _stub_build_from_git(monkeypatch) -> dict[str, Any]:
    """Replace the git builder with a capture stub; returns the captured kwargs.

    A real clone is not available in tests, so the seam under test is the
    hand-off from the API layer into the builder: the token must arrive there,
    and must appear nowhere else afterwards.
    """
    captured: dict[str, Any] = {}

    def fake_build_from_git(
        git_url, ref, path, title, settings, *, git_username=None, git_token=None
    ):
        captured.update(
            git_url=git_url,
            ref=ref,
            path=path,
            title=title,
            git_username=git_username,
            git_token=git_token,
        )
        return BuiltArtifact(
            html="<html><body><h1>From a private repo</h1></body></html>",
            title="From a private repo",
            source_type="git-html",
            git_commit="0123456789abcdef0123456789abcdef01234567",
        )

    monkeypatch.setattr(main.builder, "build_from_git", fake_build_from_git)
    return captured


def test_publish_private_git_passes_credentials_to_the_builder(
    api: Api, monkeypatch
) -> None:
    captured = _stub_build_from_git(monkeypatch)
    resp = api.client.post(
        "/api/artifacts",
        json={
            "git_url": "https://github.com/org/private-repo",
            "git_ref": "main",
            "git_path": "docs/report.html",
            "git_username": "deploy-user",
            "git_token": GIT_TOKEN,
        },
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    assert captured["git_token"] == GIT_TOKEN
    assert captured["git_username"] == "deploy-user"
    assert captured["git_url"] == "https://github.com/org/private-repo"
    assert captured["ref"] == "main"
    assert captured["path"] == "docs/report.html"


def test_publish_private_git_never_stores_or_echoes_the_token(
    api: Api, monkeypatch
) -> None:
    _stub_build_from_git(monkeypatch)
    resp = api.client.post(
        "/api/artifacts",
        json={
            "git_url": "https://github.com/org/private-repo",
            "git_username": "deploy-user",
            "git_token": GIT_TOKEN,
        },
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    assert GIT_TOKEN not in resp.text

    artifact_id = resp.json()["id"]
    envelope = main.app.state.store.get_head(artifact_id)
    assert envelope is not None

    serialized = envelope.to_json().decode("utf-8")
    assert GIT_TOKEN not in serialized
    assert "deploy-user" not in serialized
    assert "git_token" not in serialized
    assert envelope.source["git"] == {
        "url": "https://github.com/org/private-repo",
        "ref": None,
        "path": None,
        "commit": "0123456789abcdef0123456789abcdef01234567",
        # Records *that* a credential was needed, never the credential.
        "private": True,
    }

    # The same for every public read surface of the artifact.
    for path in (
        f"/a/{artifact_id}",
        f"/a/{artifact_id}/raw",
        f"/a/{artifact_id}/meta",
    ):
        read = api.client.get(path)
        assert GIT_TOKEN not in read.text, path

    # ...and for the canonical copy uploaded into the author's own project.
    assert api.canonical_calls
    assert GIT_TOKEN.encode() not in api.canonical_calls[-1]["content"]


# --------------------------------------------------------------------------
# Password protection
# --------------------------------------------------------------------------


def test_password_protected_read_paths(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Secret", password="hunter2")

    page = api.client.get(f"/a/{artifact_id}")
    assert page.status_code == 401
    assert "password" in page.text.lower()
    assert page.headers["content-type"].startswith("text/html")

    raw_no_password = api.client.get(f"/a/{artifact_id}/raw")
    assert raw_no_password.status_code == 401
    assert raw_no_password.json()["error"] == "password required"

    raw_wrong = api.client.get(
        f"/a/{artifact_id}/raw", headers={"X-Artifact-Password": "wrong"}
    )
    assert raw_wrong.status_code == 401

    raw_right = api.client.get(
        f"/a/{artifact_id}/raw", headers={"X-Artifact-Password": "hunter2"}
    )
    assert raw_right.status_code == 200

    meta = api.client.get(f"/a/{artifact_id}/meta")
    assert meta.status_code == 200
    assert meta.json()["protected"] is True


def test_unlock_form_wrong_then_right_password_sets_cookie(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Secret content", password="hunter2")

    wrong = api.client.post(
        f"/a/{artifact_id}/unlock",
        data={"password": "nope"},
        follow_redirects=False,
    )
    assert wrong.status_code == 401
    assert "Wrong password" in wrong.text

    right = api.client.post(
        f"/a/{artifact_id}/unlock",
        data={"password": "hunter2"},
        follow_redirects=False,
    )
    assert right.status_code == 303
    assert right.headers["location"] == f"/a/{artifact_id}"

    set_cookie = right.headers.get("set-cookie", "")
    assert f"art_{artifact_id}=" in set_cookie
    assert f"Path=/a/{artifact_id}" in set_cookie
    assert "HttpOnly" in set_cookie

    followup = api.client.get(f"/a/{artifact_id}")
    assert followup.status_code == 200


# --------------------------------------------------------------------------
# Update (PUT)
# --------------------------------------------------------------------------


def test_put_update_by_owner_bumps_version_and_changes_content(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Version One")
    resp = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"markdown": "# Version Two"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == 2

    raw = api.client.get(f"/a/{artifact_id}/raw")
    assert "Version Two" in raw.text
    assert "Version One" not in raw.text


def test_put_by_foreign_project_is_403(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Owner content")
    resp = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"title": "hijack"},
        headers=OTHER_AUTH_HEADERS,
    )
    assert resp.status_code == 403


def test_put_unknown_id_is_404(api: Api) -> None:
    resp = api.client.put(
        "/api/artifacts/does-not-exist", json={"title": "x"}, headers=AUTH_HEADERS
    )
    assert resp.status_code == 404


def test_put_clear_password_removes_protection(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Protected", password="secret")
    assert api.client.get(f"/a/{artifact_id}").status_code == 401

    put_resp = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"clear_password": True},
        headers=AUTH_HEADERS,
    )
    assert put_resp.status_code == 200
    assert put_resp.json()["protected"] is False

    resp = api.client.get(f"/a/{artifact_id}")
    assert resp.status_code == 200


# --------------------------------------------------------------------------
# List
# --------------------------------------------------------------------------


def test_list_artifacts_only_own_project_with_urls_merged(api: Api) -> None:
    own_id = _publish_markdown(api, "# Mine", headers=AUTH_HEADERS)
    foreign_id = _publish_markdown(api, "# Not mine", headers=OTHER_AUTH_HEADERS)

    resp = api.client.get("/api/artifacts", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["project_id"] == 123
    ids = {a["id"] for a in body["artifacts"]}
    assert own_id in ids
    assert foreign_id not in ids

    mine = next(a for a in body["artifacts"] if a["id"] == own_id)
    assert mine["url"] == f"https://testserver/a/{own_id}"
    assert mine["raw_url"] == f"{mine['url']}/raw"
    assert mine["source_url"] == f"{mine['url']}/source"
    assert mine["meta_url"] == f"{mine['url']}/meta"


# --------------------------------------------------------------------------
# Delete (soft) and purge (permanent)
# --------------------------------------------------------------------------


def test_delete_unknown_is_404(api: Api) -> None:
    resp = api.client.delete("/api/artifacts/does-not-exist", headers=AUTH_HEADERS)
    assert resp.status_code == 404


def test_delete_foreign_is_403(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Mine")
    resp = api.client.delete(f"/api/artifacts/{artifact_id}", headers=OTHER_AUTH_HEADERS)
    assert resp.status_code == 403


def test_delete_own_takes_the_public_link_down(api: Api) -> None:
    """DELETE is now a soft delete: the link dies, the content does not."""
    artifact_id = _publish_markdown(api, "# Mine")
    resp = api.client.delete(f"/api/artifacts/{artifact_id}", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["trashed"] is True
    assert body["id"] == artifact_id
    assert "restore" in body["restore_hint"]

    assert api.client.get(f"/a/{artifact_id}").status_code == 404
    # The Storage files are all still there — that is what makes it reversible.
    assert main.app.state.store.get_meta(artifact_id) is not None


def test_purge_own_removes_serving_copy(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Mine")
    resp = api.client.delete(
        f"/api/artifacts/{artifact_id}/purge", headers=AUTH_HEADERS
    )
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    assert resp.json()["purged"] is True

    assert api.client.get(f"/a/{artifact_id}").status_code == 404
    assert main.app.state.store.get_meta(artifact_id) is None


def test_purge_unknown_is_404_and_foreign_is_403(api: Api) -> None:
    assert (
        api.client.delete(
            "/api/artifacts/does-not-exist/purge", headers=AUTH_HEADERS
        ).status_code
        == 404
    )
    artifact_id = _publish_markdown(api, "# Mine")
    assert (
        api.client.delete(
            f"/api/artifacts/{artifact_id}/purge", headers=OTHER_AUTH_HEADERS
        ).status_code
        == 403
    )


# --------------------------------------------------------------------------
# Restart survival
# --------------------------------------------------------------------------


def test_restart_survival_second_store_over_same_backend(api: Api, tmp_path) -> None:
    """A fresh ArtifactStore over the same backend must serve what was published.

    This simulates a container restart: the process is stateless except for
    Storage, so hydrating a brand-new store over the same backend must find
    everything a previous process instance published.
    """
    artifact_id = _publish_markdown(api, "# Survives restart")

    second_store = ArtifactStore(
        backend=api.backend,
        cache_dir=tmp_path / "restart-cache",
        cache_max_entries=api.settings.cache_max_entries,
        max_versions=api.settings.max_versions,
    )
    count = second_store.hydrate()
    assert count >= 1

    envelope = second_store.get_head(artifact_id)
    assert envelope is not None
    assert "Survives restart" in envelope.html
    assert second_store.get_meta(artifact_id) is not None


# --------------------------------------------------------------------------
# Shell pages
# --------------------------------------------------------------------------


def test_landing_page_advertises_repo_and_version(api: Api) -> None:
    resp = api.client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert main.GITHUB_REPO_URL in resp.text
    assert main.SERVICE_VERSION in resp.text
    assert "kbc-artifact-hub" in resp.text


def test_landing_page_links_the_admin_studio_and_the_agent_definition(
    api: Api,
) -> None:
    text = api.client.get("/").text
    assert "/admin" in text
    assert "/agent" in text
    assert "Admin studio" in text
    # The install one-liner from the "for agents" section.
    assert "~/.claude/agents/artifact-hub.md" in text


def test_admin_page_is_html_and_self_describing(api: Api) -> None:
    resp = api.client.get("/admin")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Admin studio" in resp.text


def test_admin_page_keeps_credentials_in_the_tab_only(api: Api) -> None:
    """The studio must never ship a token, and must never reach for localStorage.

    The page is public, so a leaked hub token would be catastrophic; and the
    whole security story rests on the visitor's own token living in
    sessionStorage (per-tab, cleared on close) rather than anywhere durable.
    Both are asserted mechanically so a later edit cannot quietly regress them.
    """
    text = api.client.get("/admin").text
    assert api.settings.hub_storage_token not in text
    assert "good-token" not in text
    assert "localStorage" not in text
    assert "sessionStorage" in text
    assert "hub_admin_auth" in text
    # The promise made to the visitor on the sign-in card.
    assert "the credential stays in this browser tab" in text


def test_admin_page_offers_every_stack_alias_and_a_custom_url(api: Api) -> None:
    text = api.client.get("/admin").text
    for alias in ("us", "gcp-us", "eu", "azure-eu", "gcp-eu"):
        assert f'<option value="{alias}">' in text
    assert '<option value="__custom__">' in text


# --------------------------------------------------------------------------
# Versioning: owner updates
# --------------------------------------------------------------------------


def test_put_by_owner_creates_version_two_and_both_are_listed(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    put = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"markdown": "# Two"},
        headers=AUTH_HEADERS,
    )
    assert put.status_code == 200, put.text
    assert put.json()["version"] == 2
    assert put.json()["head_version"] == 2

    listing = api.client.get(f"/a/{artifact_id}/versions")
    assert listing.status_code == 200
    body = listing.json()
    assert body["head_version"] == 2
    assert [v["version"] for v in body["versions"]] == [2, 1]
    assert body["versions"][0]["is_head"] is True
    assert body["versions"][1]["is_head"] is False
    assert body["versions"][0]["url"] == f"https://testserver/a/{artifact_id}/v/2"

    assert "Two" in api.client.get(f"/a/{artifact_id}/v/2").text
    assert "One" in api.client.get(f"/a/{artifact_id}/v/1").text


def test_put_title_without_content_is_422(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    resp = api.client.put(
        f"/api/artifacts/{artifact_id}", json={"title": "Renamed"}, headers=AUTH_HEADERS
    )
    assert resp.status_code == 422
    assert "title" in resp.json()["detail"]


def test_owner_submitted_version_is_live_immediately(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    resp = _submit_version(api, artifact_id, "# Owner two", note="second cut")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["version"] == 2
    assert body["status"] == "live"
    assert body["note"] == "second cut"
    assert body["url"] == f"https://testserver/a/{artifact_id}/v/2"
    assert "Owner two" in api.client.get(f"/a/{artifact_id}").text


# --------------------------------------------------------------------------
# Versioning: moderated proposals
# --------------------------------------------------------------------------


def test_foreign_version_without_accept_versions_is_403(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Closed")
    resp = _submit_version(
        api, artifact_id, "# Hijack", headers=OTHER_AUTH_HEADERS
    )
    assert resp.status_code == 403
    assert "accept_versions" in resp.json()["detail"]


def test_foreign_proposal_is_private_until_promoted(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Open one", accept_versions=True)

    proposal = _submit_version(
        api,
        artifact_id,
        "# Contributed two",
        note="typo fix",
        headers=OTHER_AUTH_HEADERS,
    )
    assert proposal.status_code == 201, proposal.text
    assert proposal.json()["status"] == "proposed"
    assert proposal.json()["version"] == 2

    # The head is untouched: /a/{id} still serves version 1.
    head = api.client.get(f"/a/{artifact_id}")
    assert head.status_code == 200
    assert "Open one" in head.text
    assert "Contributed two" not in head.text

    # The proposal's *content* is private...
    anonymous = api.client.get(f"/a/{artifact_id}/v/2")
    assert anonymous.status_code == 403
    assert "proposed" in anonymous.json()["error"]

    # ...to everyone but its author and the artifact owner.
    author = api.client.get(f"/a/{artifact_id}/v/2", headers=OTHER_AUTH_HEADERS)
    assert author.status_code == 200
    assert "Contributed two" in author.text

    owner = api.client.get(f"/a/{artifact_id}/v/2", headers=AUTH_HEADERS)
    assert owner.status_code == 200

    # Its *metadata* is listed for anyone holding the capability URL.
    listing = api.client.get(f"/a/{artifact_id}/versions").json()
    proposed = next(v for v in listing["versions"] if v["version"] == 2)
    assert proposed["status"] == "proposed"
    assert proposed["note"] == "typo fix"
    assert proposed["author"]["project_id"] == 999
    assert listing["accept_versions"] is True


def test_promote_is_owner_only_and_moves_the_head(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Open one", accept_versions=True)
    assert (
        _submit_version(
            api, artifact_id, "# Promoted two", headers=OTHER_AUTH_HEADERS
        ).status_code
        == 201
    )

    foreign = api.client.post(
        f"/api/artifacts/{artifact_id}/versions/2/promote",
        headers=OTHER_AUTH_HEADERS,
    )
    assert foreign.status_code == 403

    promoted = api.client.post(
        f"/api/artifacts/{artifact_id}/versions/2/promote", headers=AUTH_HEADERS
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["status"] == "live"
    assert promoted.json()["head_version"] == 2

    head = api.client.get(f"/a/{artifact_id}")
    assert head.status_code == 200
    assert "Promoted two" in head.text

    again = api.client.post(
        f"/api/artifacts/{artifact_id}/versions/2/promote", headers=AUTH_HEADERS
    )
    assert again.status_code == 409

    unknown = api.client.post(
        f"/api/artifacts/{artifact_id}/versions/77/promote", headers=AUTH_HEADERS
    )
    assert unknown.status_code == 404


def test_contributor_withdraws_own_proposal(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Open one", accept_versions=True)
    assert (
        _submit_version(
            api, artifact_id, "# Withdrawn", headers=OTHER_AUTH_HEADERS
        ).status_code
        == 201
    )

    withdrawn = api.client.delete(
        f"/api/artifacts/{artifact_id}/versions/2", headers=OTHER_AUTH_HEADERS
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["deleted"] is True

    listing = api.client.get(f"/a/{artifact_id}/versions").json()
    assert [v["version"] for v in listing["versions"]] == [1]


def test_contributor_cannot_delete_someone_elses_version(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Open one", accept_versions=True)
    resp = api.client.delete(
        f"/api/artifacts/{artifact_id}/versions/1", headers=OTHER_AUTH_HEADERS
    )
    assert resp.status_code == 403


def test_owner_cannot_delete_the_only_live_version(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Only one")
    resp = api.client.delete(
        f"/api/artifacts/{artifact_id}/versions/1", headers=AUTH_HEADERS
    )
    assert resp.status_code == 409
    assert "only live version" in resp.json()["error"]
    assert api.client.get(f"/a/{artifact_id}").status_code == 200


def test_version_submissions_are_rate_limited_per_day(api: Api, monkeypatch) -> None:
    artifact_id = _publish_markdown(api, "# One")
    monkeypatch.setattr(
        main,
        "settings",
        dataclasses.replace(api.settings, max_versions_per_day=2),
    )

    assert _submit_version(api, artifact_id, "# Two").status_code == 201
    assert _submit_version(api, artifact_id, "# Three").status_code == 201

    limited = _submit_version(api, artifact_id, "# Four")
    assert limited.status_code == 429
    assert limited.json()["limit"] == 2


# --------------------------------------------------------------------------
# Diffs
# --------------------------------------------------------------------------


def _publish_and_update(api: Api) -> str:
    artifact_id = _publish_markdown(api, "# Report\n\nOld line\n")
    resp = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"markdown": "# Report\n\nNew line\n"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    return artifact_id


def test_diff_json_reports_stats(api: Api) -> None:
    artifact_id = _publish_and_update(api)
    resp = api.client.get(f"/a/{artifact_id}/diff/1..2?format=json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["from"] == 1 and body["to"] == 2
    assert body["kind"] == "markdown"
    assert body["stats"]["added"] >= 1
    assert body["stats"]["removed"] >= 1
    assert "New line" in body["unified"]


def test_diff_html_and_unified_render(api: Api) -> None:
    artifact_id = _publish_and_update(api)

    html_diff = api.client.get(f"/a/{artifact_id}/diff/1..2")
    assert html_diff.status_code == 200
    assert html_diff.headers["content-type"].startswith("text/html")
    assert "New line" in html_diff.text

    unified = api.client.get(f"/a/{artifact_id}/diff/1..2?format=unified")
    assert unified.status_code == 200
    assert unified.headers["content-type"].startswith("text/plain")
    assert "+New line" in unified.text


def test_diff_unknown_format_is_400(api: Api) -> None:
    artifact_id = _publish_and_update(api)
    resp = api.client.get(f"/a/{artifact_id}/diff/1..2?format=pdf")
    assert resp.status_code == 400
    assert "format" in resp.json()["error"].lower()


def test_diff_malformed_spec_is_400_and_missing_version_is_404(api: Api) -> None:
    artifact_id = _publish_and_update(api)
    assert api.client.get(f"/a/{artifact_id}/diff/1-2").status_code == 400
    assert api.client.get(f"/a/{artifact_id}/diff/1..9").status_code == 404


def test_changelog_documents_every_released_version(api: Api) -> None:
    """The changelog is how a reader learns what changed, security included.

    A gap in it is invisible: nothing else in the service reports that a
    release happened. Requiring the newest section to name the running
    version turns "we forgot to write the entry" into a failing test rather
    than a silent omission.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    # A heading may cover a range of patch releases ("0.7.4–0.7.5"); every
    # version it names counts as documented.
    documented: list[str] = []
    for heading in re.findall(r"^## ([0-9][0-9.–-]*)", text, flags=re.MULTILINE):
        documented.extend(re.findall(r"\d+\.\d+\.\d+", heading))

    assert documented, "the changelog has no version sections at all"
    assert documented[0] == main.SERVICE_VERSION, (
        f"newest changelog section is {documented[0]}, "
        f"but this build reports {main.SERVICE_VERSION}"
    )
    for released in ("0.7.2", "0.7.3", "0.7.4", "0.7.5", "0.8.0", "0.9.0"):
        assert released in documented, released
    assert len(documented) == len(set(documented)), "a version is documented twice"


def test_served_docs_do_not_claim_proposals_are_never_pruned(api: Api) -> None:
    """Retention text has to match ArtifactStore._prune_proposals.

    "Proposals are never pruned" was true while HUB_MAX_VERSIONS was the only
    retention rule. HUB_MAX_PROPOSED_VERSIONS added a second one, and an agent
    that believes the old sentence will assume a pending proposal waits
    forever.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    served = {
        "/agent": api.client.get("/agent").text,
        "/skill": api.client.get("/skill").text,
        "README.md": (root / "README.md").read_text(encoding="utf-8"),
    }
    for name, text in served.items():
        assert "roposals are never pruned" not in text, name
        assert "HUB_MAX_PROPOSED_VERSIONS" in text, name


def test_openapi_documents_the_locked_review_shell_as_200(api: Api) -> None:
    """The review route deliberately serves a credential-free shell while locked.

    Declaring a 401 unlock form there makes generated clients and operators
    expect a status and a flow the route never produces.
    """
    schema = api.client.get("/openapi.json").json()
    review = schema["paths"]["/a/{artifact_id}/review"]["get"]
    assert "401" not in review["responses"]
    description = review["description"]
    # It must say what the route does -- answer 200 while locked -- rather
    # than inherit the shared note that promises a redirect to the standalone
    # unlock form.
    assert "still answers 200" in description
    assert "browsers use the unlock form at POST" not in description
    assert "shell" in review["responses"]["200"]["description"].lower()


def test_locked_review_really_answers_200(api: Api) -> None:
    """The behaviour the OpenAPI above is describing."""
    artifact_id = _publish_markdown(api, "# Report\n")
    resp = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"password": "hunter2"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert api.client.get(f"/a/{artifact_id}/review").status_code == 200
    assert api.client.get(f"/a/{artifact_id}").status_code == 401


def test_every_response_carries_a_no_referrer_policy(api: Api) -> None:
    """Hub pages load Google Fonts, so the URL must not travel with the request.

    A capability URL *is* the credential. Under a browser's default policy a
    cross-origin stylesheet request carries the referring page's origin, and
    a same-origin navigation carries the full URL -- neither of which a
    third party or a linked-to site is entitled to learn.
    """
    artifact_id = _publish_markdown(api, "# Report\n")
    for path in ("/", "/admin", f"/a/{artifact_id}", f"/a/{artifact_id}/review"):
        resp = api.client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers.get("referrer-policy") == "no-referrer", path


def test_diff_spec_must_name_an_older_and_a_newer_version(api: Api) -> None:
    """The operands are named older..newer, so the route has to enforce it.

    2..1 would otherwise render additions as removals under labels claiming
    the opposite, and a review reading those stats draws the reverse
    conclusion about what changed.
    """
    artifact_id = _publish_and_update(api)

    reversed_spec = api.client.get(f"/a/{artifact_id}/diff/2..1")
    assert reversed_spec.status_code == 400
    assert "older" in reversed_spec.json()["detail"].lower()

    same = api.client.get(f"/a/{artifact_id}/diff/2..2")
    assert same.status_code == 400

    # The check must not depend on the versions existing: ordering is a
    # property of the spec itself, so it is answered before any lookup.
    assert api.client.get(f"/a/{artifact_id}/diff/9..8").status_code == 400


# --------------------------------------------------------------------------
# Head pointer
# --------------------------------------------------------------------------


def test_pinning_the_head_freezes_what_is_served(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Pinned one")
    assert _submit_version(api, artifact_id, "# Latest two").status_code == 201
    assert "Latest two" in api.client.get(f"/a/{artifact_id}").text

    pinned = api.client.put(
        f"/api/artifacts/{artifact_id}/head",
        json={"mode": "pinned", "version": 1},
        headers=AUTH_HEADERS,
    )
    assert pinned.status_code == 200, pinned.text
    assert pinned.json() == {
        "id": artifact_id,
        "head_mode": "pinned",
        "head_version_served": 1,
    }

    served = api.client.get(f"/a/{artifact_id}")
    assert "Pinned one" in served.text
    assert "Latest two" not in served.text

    listing = api.client.get(f"/a/{artifact_id}/versions").json()
    assert listing["head_version"] == 1
    by_number = {v["version"]: v for v in listing["versions"]}
    assert by_number[1]["is_head"] is True
    assert by_number[2]["is_head"] is False

    back = api.client.put(
        f"/api/artifacts/{artifact_id}/head",
        json={"mode": "latest"},
        headers=AUTH_HEADERS,
    )
    assert back.status_code == 200
    assert back.json()["head_version_served"] == 2


def test_head_rejects_unknown_mode_and_missing_version(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    bad_mode = api.client.put(
        f"/api/artifacts/{artifact_id}/head",
        json={"mode": "whatever"},
        headers=AUTH_HEADERS,
    )
    assert bad_mode.status_code == 422

    no_version = api.client.put(
        f"/api/artifacts/{artifact_id}/head",
        json={"mode": "pinned"},
        headers=AUTH_HEADERS,
    )
    assert no_version.status_code == 422

    missing = api.client.put(
        f"/api/artifacts/{artifact_id}/head",
        json={"mode": "pinned", "version": 42},
        headers=AUTH_HEADERS,
    )
    assert missing.status_code == 422

    foreign = api.client.put(
        f"/api/artifacts/{artifact_id}/head",
        json={"mode": "latest"},
        headers=OTHER_AUTH_HEADERS,
    )
    assert foreign.status_code == 403


def test_head_cannot_be_pinned_to_a_proposal(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Open one", accept_versions=True)
    assert (
        _submit_version(
            api, artifact_id, "# Proposed two", headers=OTHER_AUTH_HEADERS
        ).status_code
        == 201
    )
    resp = api.client.put(
        f"/api/artifacts/{artifact_id}/head",
        json={"mode": "pinned", "version": 2},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422
    assert "proposed" in resp.json()["detail"]


# --------------------------------------------------------------------------
# Version history page
# --------------------------------------------------------------------------


def test_versions_html_page_lists_every_version(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One", accept_versions=True)
    assert _submit_version(api, artifact_id, "# Two", note="second cut").status_code == 201

    resp = api.client.get(f"/a/{artifact_id}/versions?format=html")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["x-robots-tag"] == "noindex, nofollow"

    text = resp.text
    assert "Version history" in text
    assert ">v1<" in text and ">v2<" in text
    assert f"/a/{artifact_id}/v/1" in text
    assert f"/a/{artifact_id}/v/2" in text
    assert f"/a/{artifact_id}/diff/1..2" in text
    assert "second cut" in text
    # accept_versions is on, so the page teaches how to contribute.
    assert f"/api/artifacts/{artifact_id}/versions" in text


def test_versions_of_unknown_artifact_is_404(api: Api) -> None:
    assert api.client.get("/a/nope/versions").status_code == 404
    assert api.client.get("/a/nope/v/1").status_code == 404


def test_protected_artifact_gates_versions_and_diff(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Secret one", password="hunter2")
    assert _submit_version(api, artifact_id, "# Secret two").status_code == 201

    assert api.client.get(f"/a/{artifact_id}/versions").status_code == 401
    assert api.client.get(f"/a/{artifact_id}/diff/1..2").status_code == 401

    unlocked = {"X-Artifact-Password": "hunter2"}
    assert api.client.get(f"/a/{artifact_id}/versions", headers=unlocked).status_code == 200
    assert api.client.get(f"/a/{artifact_id}/diff/1..2", headers=unlocked).status_code == 200


# --------------------------------------------------------------------------
# Metadata with versions
# --------------------------------------------------------------------------


def test_meta_reports_version_counts(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One", accept_versions=True)
    assert (
        _submit_version(
            api, artifact_id, "# Two", headers=OTHER_AUTH_HEADERS
        ).status_code
        == 201
    )

    body = api.client.get(f"/a/{artifact_id}/meta").json()
    _assert_no_sensitive_keys(body)
    assert body["head_version"] == 1
    assert body["versions_count"] == 2
    assert body["proposed_count"] == 1
    assert body["accept_versions"] is True
    assert body["versions_url"] == f"https://testserver/a/{artifact_id}/versions"


# --------------------------------------------------------------------------
# Inline comments
# --------------------------------------------------------------------------

#: Owner keys of the two fake tokens, as src.auth.Owner.key builds them from
#: the "us" alias (https://connection.keboola.com).
OWNER_KEY = "123@connection.keboola.com"
OTHER_KEY = "999@connection.keboola.com"


def _comment(
    api: Api,
    artifact_id: str,
    *,
    version: int = 1,
    exact: str = "Body text",
    prefix: str = "",
    suffix: str = "",
    body: str = "Is this still true?",
    headers: dict[str, str] = AUTH_HEADERS,
):
    """POST one comment thread; returns the raw response."""
    return api.client.post(
        f"/api/artifacts/{artifact_id}/comments",
        json={
            "version": version,
            "exact": exact,
            "prefix": prefix,
            "suffix": suffix,
            "body": body,
        },
        headers=headers,
    )


def _policy(
    api: Api, artifact_id: str, headers: dict[str, str] = AUTH_HEADERS, **fields: Any
):
    """PUT artifact-level policy fields (comments_mode, status, ...)."""
    return api.client.put(
        f"/api/artifacts/{artifact_id}", json=fields, headers=headers
    )


def _assert_no_credentials(obj: Any) -> None:
    """Recursively assert no internal identity field is exposed publicly.

    ``stack_url`` would hand a reader the address of the author's stack and
    ``key`` the exact string an allowlist is matched against; the public
    projection reduces both to a bare stack hostname.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            assert key not in ("stack_url", "key"), f"found internal key {key!r}"
            _assert_no_credentials(value)
    elif isinstance(obj, list):
        for item in obj:
            _assert_no_credentials(item)


def test_comment_create_and_list_round_trips_the_selector(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title\n\nBody text here.")

    created = _comment(
        api,
        artifact_id,
        exact="Body text",
        prefix="Title\n\n",
        suffix=" here.",
        body="Which body?",
    )
    assert created.status_code == 201, created.text
    thread = created.json()
    assert thread["thread_id"] == thread["id"]
    assert thread["artifact_id"] == artifact_id
    assert thread["version"] == 1
    assert thread["selector"] == {
        "exact": "Body text",
        "prefix": "Title\n\n",
        "suffix": " here.",
    }
    assert thread["body"] == "Which body?"
    assert thread["resolved"] is False
    assert thread["replies"] == []
    assert thread["author"] == {
        "project_id": 123,
        "project_name": "Test",
        "stack_host": "connection.keboola.com",
    }
    _assert_no_credentials(thread)

    listing = api.client.get(f"/a/{artifact_id}/comments")
    assert listing.status_code == 200
    body = listing.json()
    assert body["id"] == artifact_id
    assert body["comments_mode"] == "anyone"
    assert body["status"] == "draft"
    assert [t["id"] for t in body["threads"]] == [thread["id"]]
    assert body["threads"][0]["selector"]["exact"] == "Body text"
    _assert_no_credentials(body)


def test_comments_are_listed_oldest_first(api: Api, monkeypatch) -> None:
    artifact_id = _publish_markdown(api, "# One\n\nTwo three")
    # Thread ids are random and the real clock has second precision, so the
    # ordering is only observable with distinct, controlled timestamps.
    stamps = iter(["2026-01-02T09:00:00+00:00", "2026-01-01T09:00:00+00:00"])
    monkeypatch.setattr(main, "_now", lambda: next(stamps))

    newer = _comment(api, artifact_id, exact="One").json()["id"]
    older = _comment(api, artifact_id, exact="Two").json()["id"]

    ids = [t["id"] for t in api.client.get(f"/a/{artifact_id}/comments").json()["threads"]]
    assert ids == [older, newer]


def test_comments_of_unknown_artifact_is_404(api: Api) -> None:
    assert api.client.get("/a/nope/comments").status_code == 404
    assert _comment(api, "nope").status_code == 404


def test_comments_mode_off_closes_the_artifact_to_other_projects(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Closed")
    assert _policy(api, artifact_id, comments_mode="off").status_code == 200

    stranger = _comment(api, artifact_id, headers=OTHER_AUTH_HEADERS)
    assert stranger.status_code == 403
    assert "closed" in stranger.json()["detail"]

    # Only "final" locks the owner out of their own artifact; "off" does not.
    assert _comment(api, artifact_id).status_code == 201


def test_comments_allowlist_admits_only_listed_projects(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Allowlisted")
    assert (
        _policy(
            api,
            artifact_id,
            comments_mode="allowlist",
            contributors=[OTHER_KEY],
        ).status_code
        == 200
    )

    contributor = _comment(api, artifact_id, headers=OTHER_AUTH_HEADERS)
    assert contributor.status_code == 201, contributor.text
    # The owner is never locked out of their own artifact.
    assert _comment(api, artifact_id).status_code == 201

    assert _policy(api, artifact_id, contributors=[]).status_code == 200
    stranger = _comment(api, artifact_id, headers=OTHER_AUTH_HEADERS)
    assert stranger.status_code == 403
    assert "allowlist" in stranger.json()["detail"]


def test_contributor_keys_are_validated(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Allowlisted")
    bad = _policy(api, artifact_id, contributors=["not-a-key"])
    assert bad.status_code == 422
    assert "project key" in bad.json()["detail"]

    too_many = _policy(
        api, artifact_id, contributors=[f"{n}@connection.keboola.com" for n in range(51)]
    )
    assert too_many.status_code == 422
    assert "too many contributors" in too_many.json()["detail"]


def test_accept_versions_and_mode_together_is_422(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    resp = _policy(api, artifact_id, accept_versions=True, accept_versions_mode="anyone")
    assert resp.status_code == 422
    assert "accept_versions_mode" in resp.json()["detail"]


def test_versions_allowlist_admits_only_listed_projects(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    assert (
        _policy(
            api,
            artifact_id,
            accept_versions_mode="allowlist",
            contributors=[OTHER_KEY],
        ).status_code
        == 200
    )
    allowed = _submit_version(api, artifact_id, "# Two", headers=OTHER_AUTH_HEADERS)
    assert allowed.status_code == 201, allowed.text
    assert allowed.json()["status"] == "proposed"

    assert _policy(api, artifact_id, contributors=[]).status_code == 200
    denied = _submit_version(api, artifact_id, "# Three", headers=OTHER_AUTH_HEADERS)
    assert denied.status_code == 403


def test_comment_on_unknown_version_is_422(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    resp = _comment(api, artifact_id, version=42)
    assert resp.status_code == 422
    assert "version 42" in resp.json()["detail"]


def test_comment_body_and_quote_validation_is_422(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")

    empty_body = _comment(api, artifact_id, body="   ")
    assert empty_body.status_code == 422
    assert "body" in empty_body.json()["detail"].lower()

    empty_quote = _comment(api, artifact_id, exact="   ")
    assert empty_quote.status_code == 422
    assert "selection" in empty_quote.json()["detail"].lower()

    long_quote = _comment(api, artifact_id, exact="x" * 2001)
    assert long_quote.status_code == 422
    # SEC-100-004 put the store's own ceiling on the field itself, so an
    # oversized quote is refused by request validation before it reaches the
    # comment store's "quoted selection is too long" check.
    assert "at most 2000 characters" in long_quote.text


def test_comments_are_rate_limited_per_day(api: Api, monkeypatch) -> None:
    artifact_id = _publish_markdown(api, "# One")
    monkeypatch.setattr(
        main, "settings", dataclasses.replace(api.settings, max_comments_per_day=2)
    )

    assert _comment(api, artifact_id, exact="One").status_code == 201
    thread_id = _comment(api, artifact_id, exact="One").json()["id"]

    limited = _comment(api, artifact_id, exact="One")
    assert limited.status_code == 429
    assert limited.json()["limit"] == 2

    # Replies share the same bucket.
    reply = api.client.post(
        f"/api/artifacts/{artifact_id}/comments/{thread_id}/replies",
        json={"body": "also blocked"},
        headers=AUTH_HEADERS,
    )
    assert reply.status_code == 429


def test_reply_appends_to_the_thread(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One", accept_versions=True)
    thread_id = _comment(api, artifact_id, exact="One").json()["id"]

    replied = api.client.post(
        f"/api/artifacts/{artifact_id}/comments/{thread_id}/replies",
        json={"body": "Answering."},
        headers=OTHER_AUTH_HEADERS,
    )
    assert replied.status_code == 201, replied.text
    thread = replied.json()
    assert len(thread["replies"]) == 1
    assert thread["replies"][0]["body"] == "Answering."
    assert thread["replies"][0]["author"]["project_id"] == 999
    _assert_no_credentials(thread)

    listed = api.client.get(f"/a/{artifact_id}/comments").json()["threads"][0]
    assert len(listed["replies"]) == 1


def test_reply_to_unknown_thread_is_404(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    resp = api.client.post(
        f"/api/artifacts/{artifact_id}/comments/nope/replies",
        json={"body": "hello"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 404


def test_empty_reply_body_is_422(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    thread_id = _comment(api, artifact_id, exact="One").json()["id"]
    resp = api.client.post(
        f"/api/artifacts/{artifact_id}/comments/{thread_id}/replies",
        json={"body": " "},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422


def test_resolve_then_conflict_then_reopen(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One", accept_versions=True)
    thread_id = _comment(
        api, artifact_id, exact="One", headers=OTHER_AUTH_HEADERS
    ).json()["id"]
    path = f"/api/artifacts/{artifact_id}/comments/{thread_id}/resolve"

    # The artifact owner may resolve a thread they did not write.
    resolved = api.client.post(path, headers=AUTH_HEADERS)
    assert resolved.status_code == 200, resolved.text
    thread = resolved.json()
    assert thread["resolved"] is True
    assert thread["resolved_by"]["project_id"] == 123
    _assert_no_credentials(thread)

    again = api.client.post(path, headers=AUTH_HEADERS)
    assert again.status_code == 409
    assert "already resolved" in again.json()["error"]

    # The thread's own author may reopen it.
    reopened = api.client.post(
        path, json={"resolved": False}, headers=OTHER_AUTH_HEADERS
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["resolved"] is False
    assert reopened.json()["resolved_by"] is None

    already_open = api.client.post(
        path, json={"resolved": False}, headers=AUTH_HEADERS
    )
    assert already_open.status_code == 409
    assert "already open" in already_open.json()["error"]


def test_resolve_by_an_unrelated_project_is_403(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    thread_id = _comment(api, artifact_id, exact="One").json()["id"]
    resp = api.client.post(
        f"/api/artifacts/{artifact_id}/comments/{thread_id}/resolve",
        headers=OTHER_AUTH_HEADERS,
    )
    assert resp.status_code == 403


def test_delete_thread_by_author_by_owner_and_by_a_stranger(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One\n\nTwo")

    # The author withdraws their own thread.
    own = _comment(
        api, artifact_id, exact="One", headers=OTHER_AUTH_HEADERS
    ).json()["id"]
    withdrawn = api.client.delete(
        f"/api/artifacts/{artifact_id}/comments/{own}", headers=OTHER_AUTH_HEADERS
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["deleted"] is True

    # The artifact owner moderates someone else's thread.
    foreign = _comment(
        api, artifact_id, exact="Two", headers=OTHER_AUTH_HEADERS
    ).json()["id"]
    moderated = api.client.delete(
        f"/api/artifacts/{artifact_id}/comments/{foreign}", headers=AUTH_HEADERS
    )
    assert moderated.status_code == 200

    # A project that is neither may not.
    owners_own = _comment(api, artifact_id, exact="One").json()["id"]
    stranger = api.client.delete(
        f"/api/artifacts/{artifact_id}/comments/{owners_own}",
        headers=OTHER_AUTH_HEADERS,
    )
    assert stranger.status_code == 403

    remaining = api.client.get(f"/a/{artifact_id}/comments").json()["threads"]
    assert [t["id"] for t in remaining] == [owners_own]


def test_delete_unknown_thread_is_404(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    resp = api.client.delete(
        f"/api/artifacts/{artifact_id}/comments/nope", headers=AUTH_HEADERS
    )
    assert resp.status_code == 404


def test_purging_an_artifact_removes_its_comment_threads(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    _comment(api, artifact_id, exact="One")
    _comment(api, artifact_id, exact="One")

    deleted = api.client.delete(
        f"/api/artifacts/{artifact_id}/purge", headers=AUTH_HEADERS
    )
    assert deleted.status_code == 200
    assert deleted.json()["comment_threads_deleted"] == 2

    assert api.client.get(f"/a/{artifact_id}/comments").status_code == 404
    # Nothing is left behind in Storage either.
    assert main.app.state.comments.list_for(artifact_id) == []


def test_trashing_an_artifact_keeps_its_comment_threads(api: Api) -> None:
    """A soft delete must not destroy the discussion it can be restored with."""
    artifact_id = _publish_markdown(api, "# One")
    _comment(api, artifact_id, exact="One")

    assert (
        api.client.delete(
            f"/api/artifacts/{artifact_id}", headers=AUTH_HEADERS
        ).status_code
        == 200
    )
    # Unreachable while trashed...
    assert api.client.get(f"/a/{artifact_id}/comments").status_code == 404
    # ...but still in Storage, and back after a restore.
    assert len(main.app.state.comments.list_for(artifact_id)) == 1
    assert (
        api.client.post(
            f"/api/artifacts/{artifact_id}/restore", headers=AUTH_HEADERS
        ).status_code
        == 200
    )
    threads = api.client.get(f"/a/{artifact_id}/comments").json()["threads"]
    assert len(threads) == 1


class _DeleteFailingBackend:
    """Delegates everything to an inner backend, but never deletes.

    Simulates Storage refusing to remove a file: the delete raises, the file
    stays, and anything that claimed "deleted" would be claiming erasure over
    content that is still readable.
    """

    def __init__(self, inner: InMemoryFilesBackend) -> None:
        self._inner = inner

    def upload(self, name: str, content: bytes, tags: list[str]) -> int:
        return self._inner.upload(name, content, tags)

    def search_by_tag(self, tag: str):
        return self._inner.search_by_tag(tag)

    def download(self, file_id: int) -> bytes:
        return self._inner.download(file_id)

    def delete(self, file_id: int) -> None:
        raise BackendError("simulated Storage failure")


def _break_comment_deletes(api: Api, monkeypatch, tmp_path) -> None:
    """Point the app's CommentStore at a backend whose deletes always fail.

    Only the comment store is swapped, so the artifact's own files still
    delete normally — exactly the partial-erasure shape the 502 exists for.
    """
    monkeypatch.setattr(
        main.app.state,
        "comments",
        CommentStore(
            _DeleteFailingBackend(api.backend),
            tmp_path / "broken-comment-cache",
            api.settings.cache_max_entries,
        ),
    )


def test_a_partially_failed_purge_can_be_retried_to_completion(
    api: Api, monkeypatch, tmp_path
) -> None:
    """The 502 says "retry the purge", so a retry has to be possible.

    Erasing the artifact's own metadata first destroys what _owner_only
    needs, so the advertised retry answered 404 and the comment files it was
    supposed to finish erasing stayed in Storage for good. Children go first
    now, and the record that authorizes the operation survives until the
    whole thing is done.
    """
    artifact_id = _publish_markdown(api, "# One")
    assert _comment(api, artifact_id, exact="One").status_code == 201
    working_comments = main.app.state.comments

    _break_comment_deletes(api, monkeypatch, tmp_path)
    first = api.client.delete(
        f"/api/artifacts/{artifact_id}/purge", headers=AUTH_HEADERS
    )
    assert first.status_code == 502, first.text

    # The artifact must still be there to authorize the retry the 502 asked
    # for -- a purge that erased it first could never be resumed.
    assert main.app.state.store.get_meta(artifact_id) is not None

    # Storage recovers; the retry finishes the job instead of 404ing.
    monkeypatch.setattr(main.app.state, "comments", working_comments)
    second = api.client.delete(
        f"/api/artifacts/{artifact_id}/purge", headers=AUTH_HEADERS
    )
    assert second.status_code == 200, second.text
    assert second.json()["purged"] is True

    assert main.app.state.store.get_meta(artifact_id) is None
    assert main.app.state.comments.list_for(artifact_id) == []
    assert api.client.get(f"/a/{artifact_id}").status_code == 404


def test_a_retry_after_a_partial_purge_still_refuses_a_foreign_owner(
    api: Api, monkeypatch, tmp_path
) -> None:
    """Keeping the meta record for the retry must not widen who may retry."""
    artifact_id = _publish_markdown(api, "# One")
    assert _comment(api, artifact_id, exact="One").status_code == 201
    _break_comment_deletes(api, monkeypatch, tmp_path)

    assert (
        api.client.delete(
            f"/api/artifacts/{artifact_id}/purge", headers=AUTH_HEADERS
        ).status_code
        == 502
    )
    assert (
        api.client.delete(
            f"/api/artifacts/{artifact_id}/purge", headers=OTHER_AUTH_HEADERS
        ).status_code
        == 403
    )


def test_purge_is_502_when_comment_threads_cannot_be_erased(
    api: Api, monkeypatch, tmp_path
) -> None:
    """Partial comment erasure must not be reported as a completed purge."""
    artifact_id = _publish_markdown(api, "# One")
    assert _comment(api, artifact_id, exact="One").status_code == 201
    _break_comment_deletes(api, monkeypatch, tmp_path)

    resp = api.client.delete(
        f"/api/artifacts/{artifact_id}/purge", headers=AUTH_HEADERS
    )
    assert resp.status_code == 502, resp.text
    body = resp.json()
    assert body["error"] == "comment threads not fully deleted"
    assert body["id"] == artifact_id
    assert body["comment_threads_deleted"] == 0
    assert body["comment_threads_failed"] == 1
    assert "deleted" not in body or body.get("deleted") is not True

    # The 502 is not cosmetic: the thread really is still in Storage.
    assert len(main.app.state.comments.list_for(artifact_id)) == 1


def test_deleting_a_comment_thread_is_502_when_its_files_remain(
    api: Api, monkeypatch, tmp_path
) -> None:
    """A failed backend delete is a 502, not a 404 and not a false success."""
    artifact_id = _publish_markdown(api, "# One")
    created = _comment(api, artifact_id, exact="One")
    assert created.status_code == 201
    thread_id = created.json()["thread_id"]
    _break_comment_deletes(api, monkeypatch, tmp_path)

    resp = api.client.delete(
        f"/api/artifacts/{artifact_id}/comments/{thread_id}", headers=AUTH_HEADERS
    )
    assert resp.status_code == 502, resp.text
    body = resp.json()
    assert body["error"] == "comment thread not fully deleted"
    assert body["thread_id"] == thread_id
    assert body["id"] == artifact_id

    # Still readable — which is why a 200 or a 404 would both have been lies.
    assert main.app.state.comments.get(artifact_id, thread_id) is not None
    threads = api.client.get(f"/a/{artifact_id}/comments").json()["threads"]
    assert [thread["id"] for thread in threads] == [thread_id]


# --------------------------------------------------------------------------
# Final status
# --------------------------------------------------------------------------


def test_final_status_freezes_versions_comments_and_content(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Draft", accept_versions=True)

    final = _policy(api, artifact_id, status="final")
    assert final.status_code == 200, final.text
    assert final.json()["artifact_status"] == "final"
    assert api.client.get(f"/a/{artifact_id}/comments").json()["status"] == "final"

    version = _submit_version(api, artifact_id, "# Frozen")
    assert version.status_code == 409
    assert version.json()["error"] == "document is final"

    foreign_version = _submit_version(
        api, artifact_id, "# Frozen", headers=OTHER_AUTH_HEADERS
    )
    assert foreign_version.status_code == 409

    comment = _comment(api, artifact_id, exact="Draft")
    assert comment.status_code == 409
    assert comment.json()["error"] == "document is final"

    content = _policy(api, artifact_id, markdown="# Frozen")
    assert content.status_code == 409

    # Artifact-level settings still work — that is how the owner reopens it.
    reopened = _policy(api, artifact_id, status="draft")
    assert reopened.status_code == 200
    assert reopened.json()["artifact_status"] == "draft"
    assert _submit_version(api, artifact_id, "# Thawed").status_code == 201
    assert _comment(api, artifact_id, exact="Thawed").status_code == 201


def test_final_artifact_can_be_reopened_in_the_same_call_as_new_content(
    api: Api,
) -> None:
    artifact_id = _publish_markdown(api, "# Draft")
    assert _policy(api, artifact_id, status="final").status_code == 200

    resp = _policy(api, artifact_id, status="draft", markdown="# Reopened")
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 2
    assert resp.json()["artifact_status"] == "draft"


def test_unknown_policy_values_are_422(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    assert _policy(api, artifact_id, status="archived").status_code == 422
    assert _policy(api, artifact_id, comments_mode="sometimes").status_code == 422
    assert _policy(api, artifact_id, accept_versions_mode="maybe").status_code == 422


def test_document_status_is_reported_on_every_public_payload(api: Api) -> None:
    """A draft artifact says so identically on /meta, /versions and /comments.

    The two notions of "status" used to be indistinguishable from the outside:
    /meta's 'status' is the head *version's* ('live'), while a document is
    'draft' or 'final'. 'document_status' is the one name that means the
    document everywhere.
    """
    artifact_id = _publish_markdown(api, "# Draft", accept_versions=True)

    meta = api.client.get(f"/a/{artifact_id}/meta").json()
    assert meta["document_status"] == "draft"
    assert meta["contributions_frozen"] is False
    assert meta["accept_versions"] is True
    assert meta["accept_versions_mode"] == "anyone"
    # Unchanged meaning: /meta's own 'status' is still the head VERSION's.
    assert meta["status"] == "live"

    versions = api.client.get(f"/a/{artifact_id}/versions").json()
    assert versions["document_status"] == "draft"
    assert versions["contributions_frozen"] is False
    assert versions["accept_versions"] is True
    assert versions["accept_versions_mode"] == "anyone"
    # Each row's 'status' remains the version's, not the document's.
    assert [row["status"] for row in versions["versions"]] == ["live"]

    comments = api.client.get(f"/a/{artifact_id}/comments").json()
    assert comments["document_status"] == "draft"
    # The pre-existing key keeps its (document) meaning, byte for byte.
    assert comments["status"] == "draft"


def test_finalised_artifact_reports_frozen_contributions_everywhere(api: Api) -> None:
    """Once final, every payload says 'final' and 'contributions_frozen'.

    'accept_versions' deliberately stays true — it is the owner's raw setting,
    not the effective state — which is exactly why the derived flag exists.
    """
    artifact_id = _publish_markdown(api, "# Draft", accept_versions=True)
    # A pending proposal keeps the head version 'live', so the version-level
    # and document-level statuses genuinely differ below.
    proposal = _submit_version(
        api, artifact_id, "# Proposed", headers=OTHER_AUTH_HEADERS
    )
    assert proposal.status_code == 201, proposal.text
    assert proposal.json()["status"] == "proposed"

    assert _policy(api, artifact_id, status="final").status_code == 200

    meta = api.client.get(f"/a/{artifact_id}/meta").json()
    assert meta["document_status"] == "final"
    assert meta["contributions_frozen"] is True
    assert meta["accept_versions"] is True
    assert meta["accept_versions_mode"] == "anyone"
    # The head VERSION is still live: 'status' did not change meaning.
    assert meta["status"] == "live"
    assert meta["proposed_count"] == 1

    versions = api.client.get(f"/a/{artifact_id}/versions").json()
    assert versions["document_status"] == "final"
    assert versions["contributions_frozen"] is True
    assert versions["accept_versions"] is True
    assert versions["accept_versions_mode"] == "anyone"
    assert sorted(row["status"] for row in versions["versions"]) == [
        "live",
        "proposed",
    ]

    comments = api.client.get(f"/a/{artifact_id}/comments").json()
    assert comments["document_status"] == "final"
    assert comments["status"] == "final"

    # And the freeze the flag advertises is real.
    assert _submit_version(api, artifact_id, "# Frozen").status_code == 409
    assert _comment(api, artifact_id, exact="Draft").status_code == 409


def test_owner_listing_carries_the_document_status_under_both_names(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Draft")
    assert _policy(api, artifact_id, status="final").status_code == 200

    rows = api.client.get("/api/artifacts", headers=AUTH_HEADERS).json()["artifacts"]
    row = next(item for item in rows if item["id"] == artifact_id)
    assert row["status"] == "final"
    assert row["document_status"] == "final"
    assert row["contributions_frozen"] is True


# --------------------------------------------------------------------------
# Exports
# --------------------------------------------------------------------------


def test_export_markdown_returns_the_source_as_an_attachment(api: Api) -> None:
    markdown = "# Q3 review\n\nShipped the new ingest path.\n"
    artifact_id = _publish_markdown(api, markdown, title="Q3 review")

    resp = api.client.get(f"/a/{artifact_id}/export/markdown")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.headers["content-disposition"] == (
        'attachment; filename="q3-review.md"'
    )
    assert resp.headers["x-artifact-markdown-source"] == "original"
    assert resp.text == markdown


def test_export_markdown_of_an_html_artifact_converts_to_markdown(api: Api) -> None:
    """The URL promises Markdown, so an HTML-authored artifact is converted."""
    document = (
        "<html><head><title>Notes</title>"
        "<style>b { color: #ff0000; }</style></head>"
        "<body><h1>Notes</h1><p>Ship <b>fast</b>.</p>"
        "<table><thead><tr><th>Metric</th><th>Q3</th></tr></thead>"
        "<tbody><tr><td>Runs</td><td>184</td></tr></tbody></table>"
        "</body></html>"
    )
    published = api.client.post(
        "/api/artifacts", json={"html": document, "title": "Notes"}, headers=AUTH_HEADERS
    )
    assert published.status_code == 201
    artifact_id = published.json()["id"]

    resp = api.client.get(f"/a/{artifact_id}/export/markdown")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.headers["content-disposition"] == 'attachment; filename="notes.md"'
    assert resp.headers["x-artifact-markdown-source"] == "converted"
    assert resp.text == (
        "# Notes\n\nShip **fast**.\n\n"
        "| Metric | Q3 |\n| --- | --- |\n| Runs | 184 |\n"
    )
    assert "#ff0000" not in resp.text

    # The document itself is untouched by any of this.
    assert api.client.get(f"/a/{artifact_id}/raw").text == document


def test_export_markdown_falls_back_to_html_when_conversion_fails(
    api: Api, monkeypatch
) -> None:
    """A converter failure degrades to the old behaviour instead of a 500."""
    document = "<html><head><title>Notes</title></head><body><p>Hi</p></body></html>"
    published = api.client.post(
        "/api/artifacts", json={"html": document, "title": "Notes"}, headers=AUTH_HEADERS
    )
    artifact_id = published.json()["id"]

    def _boom() -> None:
        raise RuntimeError("converter exploded")

    monkeypatch.setattr("src.export._converter", _boom, raising=True)
    resp = api.client.get(f"/a/{artifact_id}/export/markdown")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["content-disposition"] == 'attachment; filename="notes.html"'
    assert resp.headers["x-artifact-markdown-source"] == "none"
    assert resp.text == document


# --------------------------------------------------------------------------
# markdown_source: the author's own Markdown alongside a published HTML
# --------------------------------------------------------------------------

MS_HTML = (
    "<html><head><title>Board deck</title></head>"
    "<body><h1>Board deck</h1>"
    "<canvas id=\"chart\" aria-label=\"Revenue\"></canvas></body></html>"
)
MS_MARKDOWN = "# Board deck\n\nRevenue chart: see the deck.\n"


def _publish_html_with_markdown_source(api: Api) -> str:
    resp = api.client.post(
        "/api/artifacts",
        json={
            "html": MS_HTML,
            "markdown_source": MS_MARKDOWN,
            "title": "Board deck",
        },
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_markdown_source_is_accepted_with_html_and_stored_on_the_version(
    api: Api,
) -> None:
    artifact_id = _publish_html_with_markdown_source(api)
    envelope = main.app.state.store.get_head(artifact_id)
    assert envelope is not None
    assert envelope.source_type == "html"
    assert envelope.source["markdown"] == MS_MARKDOWN
    # Rendering is untouched: the served document is the submitted HTML.
    assert envelope.html == MS_HTML
    assert api.client.get(f"/a/{artifact_id}/raw").text == MS_HTML


def test_markdown_source_is_what_the_exports_serve(api: Api) -> None:
    artifact_id = _publish_html_with_markdown_source(api)

    export = api.client.get(f"/a/{artifact_id}/export/markdown")
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/markdown")
    assert export.headers["x-artifact-markdown-source"] == "original"
    assert export.text == MS_MARKDOWN

    source = api.client.get(f"/a/{artifact_id}/source")
    assert source.status_code == 200
    assert source.headers["content-type"].startswith("text/markdown")
    assert source.text == MS_MARKDOWN

    vault = api.client.get(f"/a/{artifact_id}/export/vault")
    with zipfile.ZipFile(io.BytesIO(vault.content)) as archive:
        document = archive.read("board-deck/document.md").decode("utf-8")
    assert document == MS_MARKDOWN
    assert "not exportable to Markdown" not in document


@pytest.mark.parametrize(
    "payload",
    [
        {"markdown": "# Hi\n", "markdown_source": MS_MARKDOWN},
        {"git_url": "https://github.com/octocat/Hello-World", "markdown_source": MS_MARKDOWN},
        {"markdown_source": MS_MARKDOWN},
    ],
    ids=["with-markdown", "with-git-url", "alone"],
)
def test_markdown_source_without_html_is_422(api: Api, payload: dict) -> None:
    resp = api.client.post("/api/artifacts", json=payload, headers=AUTH_HEADERS)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == (
        "'markdown_source' is only valid together with 'html'"
    )


def test_markdown_source_is_rejected_the_same_way_on_update_and_versions(
    api: Api,
) -> None:
    artifact_id = _publish_markdown(api, "# Hi\n", title="Hi")

    update = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"markdown": "# Hi again\n", "markdown_source": MS_MARKDOWN},
        headers=AUTH_HEADERS,
    )
    assert update.status_code == 422
    assert update.json()["detail"] == (
        "'markdown_source' is only valid together with 'html'"
    )

    version = api.client.post(
        f"/api/artifacts/{artifact_id}/versions",
        json={"markdown_source": MS_MARKDOWN},
        headers=AUTH_HEADERS,
    )
    assert version.status_code == 422
    assert version.json()["detail"] == (
        "'markdown_source' is only valid together with 'html'"
    )


def test_markdown_source_rides_along_with_a_new_html_version(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Hi\n", title="Hi")
    resp = api.client.post(
        f"/api/artifacts/{artifact_id}/versions",
        json={"html": MS_HTML, "markdown_source": MS_MARKDOWN, "title": "Board deck"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 201, resp.text

    export = api.client.get(f"/a/{artifact_id}/export/markdown")
    assert export.headers["x-artifact-markdown-source"] == "original"
    assert export.text == MS_MARKDOWN
    assert api.client.get(f"/a/{artifact_id}/raw").text == MS_HTML


def test_export_markdown_of_unknown_artifact_is_404(api: Api) -> None:
    assert api.client.get("/a/nope/export/markdown").status_code == 404
    assert api.client.get("/a/nope/export/vault").status_code == 404


def test_export_vault_is_a_zip_holding_the_whole_story(api: Api) -> None:
    artifact_id = _publish_markdown(
        api, "# Q3 review\n\nOld line\n", title="Q3 review", accept_versions=True
    )
    assert _submit_version(api, artifact_id, "# Q3 review\n\nNew line\n").status_code == 201
    assert (
        _submit_version(
            api, artifact_id, "# Proposal\n", headers=OTHER_AUTH_HEADERS
        ).status_code
        == 201
    )
    assert _comment(api, artifact_id, exact="Old line").status_code == 201

    resp = api.client.get(f"/a/{artifact_id}/export/vault")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert resp.headers["content-disposition"] == (
        'attachment; filename="q3-review-vault.zip"'
    )

    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        names = archive.namelist()
        assert "q3-review/INDEX.md" in names
        assert "q3-review/document.md" in names
        assert "q3-review/reasoning.md" in names
        # Live versions only: the anonymous export must not leak the
        # still-moderated proposal (v3).
        assert "q3-review/versions/v1.md" in names
        assert "q3-review/versions/v2.md" in names
        assert "q3-review/versions/v3.md" not in names
        assert any(name.startswith("q3-review/comments/") for name in names)
        assert "New line" in archive.read("q3-review/document.md").decode("utf-8")

    # The owner's authenticated download carries the proposal as well.
    owner_resp = api.client.get(
        f"/a/{artifact_id}/export/vault", headers=AUTH_HEADERS
    )
    with zipfile.ZipFile(io.BytesIO(owner_resp.content)) as archive:
        assert "q3-review/versions/v3.md" in archive.namelist()


def test_exports_honour_the_password_gate(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Secret\n\nBody", password="hunter2")

    assert api.client.get(f"/a/{artifact_id}/export/markdown").status_code == 401
    assert api.client.get(f"/a/{artifact_id}/export/vault").status_code == 401

    unlocked = {"X-Artifact-Password": "hunter2"}
    assert (
        api.client.get(
            f"/a/{artifact_id}/export/markdown", headers=unlocked
        ).status_code
        == 200
    )
    vault = api.client.get(f"/a/{artifact_id}/export/vault", headers=unlocked)
    assert vault.status_code == 200
    assert zipfile.ZipFile(io.BytesIO(vault.content)).namelist()


def test_comments_honour_the_password_gate(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Secret\n\nBody", password="hunter2")
    assert api.client.get(f"/a/{artifact_id}/comments").status_code == 401
    assert (
        api.client.get(
            f"/a/{artifact_id}/comments", headers={"X-Artifact-Password": "hunter2"}
        ).status_code
        == 200
    )


# --------------------------------------------------------------------------
# Review UI
# --------------------------------------------------------------------------


def test_review_page_sandboxes_the_artifact_and_keeps_the_token_in_the_tab(
    api: Api,
) -> None:
    """The security story of the review UI, asserted mechanically.

    The artifact renders in a srcdoc iframe *without* allow-same-origin, so its
    scripts run in an opaque origin; and the visitor's token lives in
    sessionStorage (shared with /admin), never in anything durable.
    """
    artifact_id = _publish_markdown(api, "# Reviewed\n\nBody text")
    resp = api.client.get(f"/a/{artifact_id}/review")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["x-robots-tag"] == "noindex, nofollow"

    text = resp.text
    assert 'sandbox="allow-scripts allow-popups"' in text
    sandboxes = re.findall(r'sandbox="([^"]*)"', text)
    assert sandboxes, "the page must render the artifact in a sandboxed iframe"
    for value in sandboxes:
        assert "allow-same-origin" not in value, value
    assert "localStorage" not in text
    assert "sessionStorage" in text
    assert "hub_admin_auth" in text
    assert api.settings.hub_storage_token not in text
    assert "good-token" not in text
    # The annotation script and the postMessage protocol it speaks.
    assert "ah-select" in text
    assert "ah-anchors" in text
    assert "ah-anchored" in text


def test_review_page_ships_a_whitespace_tolerant_anchor_fallback(api: Api) -> None:
    """The injected annotation script must carry both anchoring passes.

    The script runs in an opaque-origin iframe, so the only place the test can
    observe it is the review page's HTML. Exact matching stays the first-tried
    path (a browser-captured quote is a slice of the very string searched, and
    must keep landing at the offset it always had); the normalized pass is the
    fallback that makes an agent-written quote — one whose whitespace differs
    from the document's line breaks, indentation or non-breaking spaces — still
    anchor instead of silently reporting "quote not found on this version".
    """
    artifact_id = _publish_markdown(api, "# Reviewed\n\nBody text")
    text = api.client.get(f"/a/{artifact_id}/review").text

    # The exact scan, unchanged and tried first.
    assert "function locateExact" in text
    assert "text.indexOf(exact, from)" in text
    assert "if (at >= 0) { return { start: at, end: at + exact.length }; }" in text

    # The whitespace-tolerant fallback and its index map back to raw offsets.
    assert "function locateNormalized" in text
    assert "function collapseFlat" in text
    assert "function collapseText" in text
    # Non-breaking space is folded together with the ASCII whitespace class,
    # as a JS escape (never a literal U+00A0 smuggled through the Python
    # source, which would be invisible in both files).
    assert r"var WS = /[\s\u00a0]/;" in text
    assert r".replace(/[\s\u00a0]+/g" in text
    assert "\u00a0" not in text

    # A normalized match spans a different number of raw characters than the
    # quote does, so the range must come from the returned end offset rather
    # than from `at + spec.exact.length`.
    assert "rangeFor(flat, span.start, span.end)" in text
    assert "at + String(spec.exact).length" not in text


def test_comment_round_trips_a_quote_whose_whitespace_differs(api: Api) -> None:
    """An agent quoting across a source line break is stored and served as-is.

    The rendered document keeps the Markdown's own newline inside the
    paragraph, so the quote an agent naturally writes (one space) is not a
    substring of it. Anchoring that quote is the browser's job; the server's
    job — asserted here — is to accept it and hand it back byte-for-byte, so
    the client-side normalized pass has the original selector to work with.
    """
    artifact_id = _publish_markdown(
        api,
        "# Growth\n\nConversion plateaued at roughly 11%\nof signups in Q3.\n",
    )

    document = api.client.get(f"/a/{artifact_id}").text
    # Precondition: the rendered text really does break the phrase in two.
    assert "roughly 11%\nof signups" in document
    assert "roughly 11% of signups" not in document

    created = _comment(
        api,
        artifact_id,
        exact="plateaued at roughly 11% of signups",
        prefix="Conversion ",
        suffix=" in Q3.",
        body="Is 11% still the number?",
    )
    assert created.status_code == 201, created.text
    assert created.json()["selector"]["exact"] == (
        "plateaued at roughly 11% of signups"
    )

    threads = api.client.get(f"/a/{artifact_id}/comments").json()["threads"]
    assert len(threads) == 1
    assert threads[0]["selector"] == {
        "exact": "plateaued at roughly 11% of signups",
        "prefix": "Conversion ",
        "suffix": " in Q3.",
    }


def test_review_page_of_unknown_artifact_is_404(api: Api) -> None:
    assert api.client.get("/a/nope/review").status_code == 404


def test_review_shell_is_served_locked_while_its_data_stays_gated(api: Api) -> None:
    """The review shell is served even for a protected artifact.

    Answering with the standalone unlock form instead would navigate away and
    drop the URL fragment, which is where an invited guest's credential lives.
    The shell carries no artifact content and no credential, so serving it
    while locked reveals nothing; the data endpoints stay gated and the page's
    own unlock panel asks for the password in place.
    """
    artifact_id = _publish_markdown(api, "# Secret", password="hunter2")

    shell = api.client.get(f"/a/{artifact_id}/review")
    assert shell.status_code == 200
    # The review shell, not the standalone unlock page: the standalone form
    # navigates (it posts through a real <form action>), which is what would
    # discard a guest's URL fragment. The shell asks in place instead.
    assert 'action="/a/' not in shell.text
    assert "rv-lock-password" in shell.text
    assert "rv-doc" in shell.text
    assert "Secret" not in shell.text

    # The content itself is still refused until the password is supplied.
    assert api.client.get(f"/a/{artifact_id}/raw").status_code == 401
    assert api.client.get(f"/a/{artifact_id}/comments").status_code == 401

    unlocked = api.client.post(
        f"/a/{artifact_id}/unlock", data={"password": "hunter2"}, follow_redirects=False
    )
    assert unlocked.status_code == 303
    assert api.client.get(f"/a/{artifact_id}/raw").status_code == 200
    assert api.client.get(f"/a/{artifact_id}/review").status_code == 200


def test_review_page_ships_its_own_unlock_panel(api: Api) -> None:
    """The review page must be able to ask for the reader password itself.

    A guest invited to a password-protected document arrives on a link that
    has never shown them the standalone unlock form, and every read and every
    comment write answers 401 "password required". The page therefore carries
    its own unlock panel: a password field, a form-encoded POST to the
    existing /a/{id}/unlock endpoint, and a re-tried protected read that
    decides whether the password was accepted.
    """
    artifact_id = _publish_markdown(api, "# Reviewed\n\nBody text")
    text = api.client.get(f"/a/{artifact_id}/review").text

    # The panel itself.
    assert '<div class="rv-lock" id="rv-lock" hidden>' in text
    assert 'id="rv-lock-form"' in text
    assert 'type="password" id="rv-lock-password"' in text
    assert 'autocomplete="current-password"' in text
    assert 'id="rv-lock-btn"' in text
    assert 'id="rv-lock-error"' in text
    # An invitation grants a voice, not a key - and the panel says so instead
    # of letting the guest think their invitation is broken.
    assert 'id="rv-lock-guest"' in text
    assert "Your invitation is fine" in text

    # Locked state is told apart from every other failure: the gate's own 401
    # body, confirmed against /meta, which is public while /raw is not. The
    # test lives in the shared session module, which this page carries.
    assert 'data.error === "password required"' in text
    assert "SESSION.locked(err)" in text
    assert 'PATH + "/meta"' in text
    assert "data.protected" in text

    # What submitting the panel does.
    assert 'PATH + "/unlock"' in text
    assert '"Content-Type": "application/x-www-form-urlencoded"' in text
    assert 'body: "password=" + encodeURIComponent(value)' in text
    assert 'credentials: "same-origin"' in text
    # A 303 body is unreadable through fetch, so a protected read is re-tried
    # to find out whether the cookie took.
    assert 'await read("/comments")' in text
    assert '"That password was not accepted."' in text
    assert "Too many attempts" in text

    # A comment write goes to /api/..., which the unlock cookie (scoped to
    # /a/{id}) never reaches, so an unlocked tab re-states the password there.
    assert '"X-Artifact-Password"' in text
    # ...held in memory only: never storage, and never part of a URL. The one
    # place the typed value travels is the unlock POST body.
    assert re.search(r"Storage\s*\.\s*\w+\([^)]*readerPassword", text) is None
    assert text.count("encodeURIComponent(value)") == 1

    # A refused write parks itself and keeps what was typed, instead of
    # printing "password required" at somebody with nowhere to type one.
    assert "pendingWrite" in text
    assert "unlock it on the left" in text


def test_review_page_of_an_unprotected_artifact_is_unchanged(api: Api) -> None:
    """No unlock panel on screen, and no extra request to find that out."""
    artifact_id = _publish_markdown(api, "# Open\n\nBody text")
    text = api.client.get(f"/a/{artifact_id}/review").text

    # Every part of the lock is inert until a read is actually refused.
    for marker in ('id="rv-lock" hidden', 'id="rv-lock-guest" hidden',
                   'id="rv-lock-side" hidden'):
        assert marker in text, marker
    # /meta is fetched from one place, and only from behind the 401 check.
    assert text.count('PATH + "/meta"') == 1
    assert "if (!(await protectedArtifact()))" in text
    assert text.count('PATH + "/unlock"') == 1

    # ...and the page around it is the one that was always served.
    assert 'sandbox="allow-scripts allow-popups"' in text
    assert 'id="rv-composer"' in text
    assert 'id="rv-guest"' in text
    assert 'id="rv-signin-form"' in text
    assert "ah-select" in text
    assert "ah-anchors" in text
    assert "ah-anchored" in text


def test_unlock_then_guest_comment_is_the_flow_the_review_panel_drives(
    api: Api,
) -> None:
    """End to end, the exact sequence the review page's unlock panel performs.

    Both mechanisms appear here, because the page needs both. POST
    /a/{id}/unlock sets the signed cookie, which is path-scoped to /a/{id} and
    therefore rides on the protected *read* the panel re-tries - that re-try
    is how the JavaScript tells a right password from a wrong one, since it
    cannot read the 303. The cookie's path never covers
    /api/artifacts/{id}/comments, so the *write* that follows exercises the
    X-Artifact-Password header instead - which is why the page keeps the
    password it just unlocked with in memory for the life of the tab.
    """
    artifact_id = _publish_markdown(
        api, "# Secret\n\nBody text here.", password="hunter2"
    )
    review_url = _invite(api, artifact_id, "Jana").json()["review_url"]
    share_id = review_url.split("/a/", 1)[1].split("/", 1)[0]
    guest = _guest_headers(review_url)

    # 1. The dead end: invited, but document, discussion and write are shut.
    assert api.client.get(f"/a/{share_id}/raw").status_code == 401
    assert api.client.get(f"/a/{share_id}/comments").status_code == 401
    locked = _guest_comment(api, share_id, guest)
    assert locked.status_code == 401
    assert locked.json()["error"] == "password required"

    # ...while /meta - what the panel checks to know this is a lock and not a
    # bug - answers with no password at all.
    meta = api.client.get(f"/a/{share_id}/meta")
    assert meta.status_code == 200
    assert meta.json()["protected"] is True

    # 2. What the panel posts: the form-encoded 'password' field.
    unlocked = api.client.post(
        f"/a/{share_id}/unlock",
        data={"password": "hunter2"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert unlocked.status_code == 303

    # 3. The re-tried read now goes through on the cookie alone - and so does
    #    the invitation check, which was never the thing that was broken.
    assert api.client.get(f"/a/{share_id}/comments").status_code == 200
    assert api.client.get(f"/a/{share_id}/raw").status_code == 200
    named = api.client.get(f"/a/{share_id}/guest", headers=guest)
    assert named.status_code == 200
    assert named.json()["name"] == "Jana"

    # 4. The write still needs the password stated, because the cookie is
    #    scoped to /a/{id} and this path is not under it.
    assert _guest_comment(api, share_id, guest).status_code == 401
    created = _guest_comment(
        api, share_id, {**guest, "X-Artifact-Password": "hunter2"}
    )
    assert created.status_code == 201, created.text
    assert created.json()["author"] == {"kind": "guest", "name": "Jana"}


def test_versions_page_and_context_link_the_review_ui(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    page = api.client.get(f"/a/{artifact_id}/versions?format=html")
    assert f"/a/{artifact_id}/review" in page.text

    paths = {e["path"] for e in api.client.get("/context").json()["endpoints"]}
    assert "/a/{id}/review" in paths
    assert "/a/{id}/comments" in paths
    assert "/a/{id}/export/vault" in paths


def test_admin_studio_offers_a_review_link(api: Api) -> None:
    assert '"/review"' in api.client.get("/admin").text


def test_landing_page_lists_the_phase_three_read_endpoints(api: Api) -> None:
    text = api.client.get("/").text
    for path in ("/review", "/comments", "/export/markdown", "/export/vault"):
        assert path in text


# --------------------------------------------------------------------------
# Restart survival: comments
# --------------------------------------------------------------------------


def test_comment_threads_survive_a_restart(api: Api, tmp_path) -> None:
    """A fresh CommentStore over the same backend must find every thread."""
    artifact_id = _publish_markdown(api, "# One\n\nBody text")
    thread_id = _comment(api, artifact_id, exact="Body text").json()["id"]

    second = CommentStore(
        backend=api.backend,
        cache_dir=tmp_path / "restart-comments",
        cache_max_entries=api.settings.cache_max_entries,
    )
    assert second.hydrate() == 1
    threads = second.list_for(artifact_id)
    assert [t.id for t in threads] == [thread_id]
    assert threads[0].selector.exact == "Body text"


# --------------------------------------------------------------------------
# Hardening: unlock-cookie revocation and password-attempt throttling
# --------------------------------------------------------------------------


def test_unlock_cookies_are_signed_with_a_derived_key_not_the_raw_secret(
    api: Api,
) -> None:
    """The webhook key must not double as the unlock-cookie key (val-v070).

    A webhook receiver necessarily learns the key its signatures are keyed
    with. When that was the raw HUB_SECRET_KEY it was also the cookie key, so
    the receiver could mint an unlock cookie for any artifact.
    """
    artifact_id = _publish_markdown(api, "# Secret", password="hunter2")
    unlocked = api.client.post(
        f"/a/{artifact_id}/unlock",
        data={"password": "hunter2"},
        follow_redirects=False,
    )
    assert unlocked.status_code == 303
    meta = main.app.state.store.get_meta(artifact_id)
    cookie = api.client.cookies[main.unlock_cookie_name(meta)]
    scope = main.password_scope(meta)

    secret = api.settings.secret_key
    cookie_signer = CookieSigner(derive_key(secret, KEY_LABEL_UNLOCK_COOKIE))
    assert cookie_signer.check(artifact_id, cookie, 3600, scope) is True

    # Neither the raw secret nor the webhook key can verify or forge one.
    for foreign in (
        CookieSigner(secret),
        CookieSigner(derive_key(secret, KEY_LABEL_WEBHOOK)),
    ):
        assert foreign.check(artifact_id, cookie, 3600, scope) is False
        api.client.cookies.set(
            main.unlock_cookie_name(meta), foreign.make(artifact_id, scope)
        )
        assert api.client.get(f"/a/{artifact_id}").status_code == 401


def test_unlock_cookie_is_revoked_by_a_password_change(api: Api) -> None:
    """A cookie issued under the old password must stop working (sec-authn)."""
    artifact_id = _publish_markdown(api, "# Secret", password="hunter2")

    unlocked = api.client.post(
        f"/a/{artifact_id}/unlock",
        data={"password": "hunter2"},
        follow_redirects=False,
    )
    assert unlocked.status_code == 303
    assert api.client.get(f"/a/{artifact_id}").status_code == 200

    changed = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"password": "new-password"},
        headers=AUTH_HEADERS,
    )
    assert changed.status_code == 200

    # Same browser, same cookie — no longer valid for the new password.
    assert api.client.get(f"/a/{artifact_id}").status_code == 401
    # ...and the new password still unlocks normally.
    again = api.client.post(
        f"/a/{artifact_id}/unlock",
        data={"password": "new-password"},
        follow_redirects=False,
    )
    assert again.status_code == 303
    assert api.client.get(f"/a/{artifact_id}").status_code == 200


def test_unlock_cookie_is_revoked_by_clearing_the_password(api: Api) -> None:
    """Clearing then re-setting a password must not resurrect an old cookie."""
    artifact_id = _publish_markdown(api, "# Secret", password="hunter2")
    assert (
        api.client.post(
            f"/a/{artifact_id}/unlock",
            data={"password": "hunter2"},
            follow_redirects=False,
        ).status_code
        == 303
    )

    assert (
        api.client.put(
            f"/api/artifacts/{artifact_id}",
            json={"clear_password": True},
            headers=AUTH_HEADERS,
        ).status_code
        == 200
    )
    # No password at all: readable by anyone, cookie or not.
    assert api.client.get(f"/a/{artifact_id}").status_code == 200

    assert (
        api.client.put(
            f"/api/artifacts/{artifact_id}",
            json={"password": "hunter2"},
            headers=AUTH_HEADERS,
        ).status_code
        == 200
    )
    # The re-set password produces a different record, so the old cookie —
    # even for the very same password string — does not verify.
    assert api.client.get(f"/a/{artifact_id}").status_code == 401


def _low_unlock_limit(api: Api, monkeypatch, limit: int = 2) -> None:
    monkeypatch.setattr(
        main,
        "settings",
        dataclasses.replace(api.settings, max_unlock_attempts_per_hour=limit),
    )


def test_unlock_form_throttles_failed_attempts(api: Api, monkeypatch) -> None:
    artifact_id = _publish_markdown(api, "# Secret", password="hunter2")
    _low_unlock_limit(api, monkeypatch, limit=2)

    for _ in range(2):
        wrong = api.client.post(
            f"/a/{artifact_id}/unlock",
            data={"password": "nope"},
            follow_redirects=False,
        )
        assert wrong.status_code == 401

    blocked = api.client.post(
        f"/a/{artifact_id}/unlock",
        data={"password": "nope"},
        follow_redirects=False,
    )
    assert blocked.status_code == 429
    assert "Too many attempts" in blocked.text

    # The throttle covers the correct password too, once the budget is gone.
    correct = api.client.post(
        f"/a/{artifact_id}/unlock",
        data={"password": "hunter2"},
        follow_redirects=False,
    )
    assert correct.status_code == 429


def test_unlock_succeeds_while_the_budget_lasts(api: Api, monkeypatch) -> None:
    """Only failures count, so an honest reader is never throttled."""
    artifact_id = _publish_markdown(api, "# Secret", password="hunter2")
    _low_unlock_limit(api, monkeypatch, limit=2)

    assert (
        api.client.post(
            f"/a/{artifact_id}/unlock",
            data={"password": "nope"},
            follow_redirects=False,
        ).status_code
        == 401
    )
    for _ in range(5):
        ok = api.client.post(
            f"/a/{artifact_id}/unlock",
            data={"password": "hunter2"},
            follow_redirects=False,
        )
        assert ok.status_code == 303


def test_password_header_path_is_throttled_too(api: Api, monkeypatch) -> None:
    artifact_id = _publish_markdown(api, "# Secret", password="hunter2")
    _low_unlock_limit(api, monkeypatch, limit=2)

    for _ in range(2):
        assert (
            api.client.get(
                f"/a/{artifact_id}/raw", headers={"X-Artifact-Password": "wrong"}
            ).status_code
            == 401
        )

    blocked = api.client.get(
        f"/a/{artifact_id}/raw", headers={"X-Artifact-Password": "wrong"}
    )
    assert blocked.status_code == 429
    assert "too many wrong passwords" in blocked.json()["detail"]


def test_unlock_throttle_ignores_an_untrusted_forwarded_address(
    api: Api, monkeypatch
) -> None:
    """SEC-100-003: changing X-Real-IP no longer buys a fresh budget.

    The header is honoured only from a peer inside HUB_TRUSTED_PROXY_CIDRS,
    which is unset here (and in every default deployment), so all three
    attempts share the peer's bucket. The trusted-proxy case — where a
    forwarded address *does* get its own budget — lives in
    tests/test_review100_request_hygiene.py.
    """
    artifact_id = _publish_markdown(api, "# Secret", password="hunter2")
    _low_unlock_limit(api, monkeypatch, limit=1)

    assert (
        api.client.get(
            f"/a/{artifact_id}/raw",
            headers={"X-Artifact-Password": "wrong", "X-Real-IP": "10.0.0.1"},
        ).status_code
        == 401
    )
    assert (
        api.client.get(
            f"/a/{artifact_id}/raw",
            headers={"X-Artifact-Password": "wrong", "X-Real-IP": "10.0.0.1"},
        ).status_code
        == 429
    )
    # A second address does not reset it: the caller chose that header.
    assert (
        api.client.get(
            f"/a/{artifact_id}/raw",
            headers={"X-Artifact-Password": "wrong", "X-Real-IP": "10.0.0.2"},
        ).status_code
        == 429
    )


# --------------------------------------------------------------------------
# Hardening: configuration and logging
# --------------------------------------------------------------------------


def test_load_settings_rejects_a_short_secret_key(monkeypatch) -> None:
    monkeypatch.setenv("HUB_SECRET_KEY", "too-short")
    with pytest.raises(RuntimeError, match="HUB_SECRET_KEY"):
        load_settings()


def test_load_settings_accepts_a_long_secret_key(monkeypatch) -> None:
    monkeypatch.setenv("HUB_SECRET_KEY", "x" * 32)
    assert load_settings().secret_key == "x" * 32


def test_json_log_formatter_cannot_be_forged_by_a_log_message() -> None:
    """Newlines and quotes in user-controlled text must not forge a record."""
    hostile = 'evil", "level": "CRITICAL", "note": "x\nfake line'
    record = logging.LogRecord(
        name="src.main",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="published artifact %s",
        args=(hostile,),
        exc_info=None,
    )
    line = main.JSONFormatter().format(record)

    assert "\n" not in line
    parsed = json.loads(line)
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "src.main"
    assert parsed["message"] == f"published artifact {hostile}"


# --------------------------------------------------------------------------
# Hardening: git URL handling
# --------------------------------------------------------------------------


def test_publish_strips_userinfo_from_the_git_url(api: Api, monkeypatch) -> None:
    captured = _stub_build_from_git(monkeypatch)
    resp = api.client.post(
        "/api/artifacts",
        json={"git_url": "https://someone:s3cr3t@github.com/org/repo"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    assert "s3cr3t" not in resp.text

    # Stripped before the builder ever sees it...
    assert captured["git_url"] == "https://github.com/org/repo"

    artifact_id = resp.json()["id"]
    envelope = main.app.state.store.get_head(artifact_id)
    assert envelope is not None
    # ...before storage...
    assert envelope.source["git"]["url"] == "https://github.com/org/repo"
    assert "s3cr3t" not in envelope.to_json().decode("utf-8")
    # ...and in every public read surface.
    for path in (f"/a/{artifact_id}/meta", f"/a/{artifact_id}/versions"):
        read = api.client.get(path)
        assert "s3cr3t" not in read.text, path
        assert "someone" not in read.text, path


def test_update_and_version_strip_userinfo_from_the_git_url(
    api: Api, monkeypatch
) -> None:
    captured = _stub_build_from_git(monkeypatch)
    artifact_id = _publish_markdown(api, "# One")

    updated = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"git_url": "https://bot:tok3n@gitlab.com/org/repo"},
        headers=AUTH_HEADERS,
    )
    assert updated.status_code == 200, updated.text
    assert captured["git_url"] == "https://gitlab.com/org/repo"
    assert "tok3n" not in updated.text

    submitted = api.client.post(
        f"/api/artifacts/{artifact_id}/versions",
        json={"git_url": "https://bot:tok3n@gitlab.com/org/other"},
        headers=AUTH_HEADERS,
    )
    assert submitted.status_code == 201, submitted.text
    assert captured["git_url"] == "https://gitlab.com/org/other"
    assert "tok3n" not in submitted.text
    assert "tok3n" not in api.client.get(f"/a/{artifact_id}/versions").text


def test_git_url_query_and_fragment_are_stripped_everywhere(
    api: Api, monkeypatch
) -> None:
    """A secret hides in ?token= just as well as in the userinfo (val-v070)."""
    captured = _stub_build_from_git(monkeypatch)
    resp = api.client.post(
        "/api/artifacts",
        json={"git_url": "https://user:pass@github.com/org/repo?token=SECRET#frag"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 201, resp.text

    # Nothing credential-bearing reaches the builder, the clone, or storage.
    assert captured["git_url"] == "https://github.com/org/repo"

    artifact_id = resp.json()["id"]
    envelope = main.app.state.store.get_head(artifact_id)
    assert envelope is not None
    assert envelope.source["git"]["url"] == "https://github.com/org/repo"
    stored = envelope.to_json().decode("utf-8")
    assert "SECRET" not in stored
    assert "pass" not in stored

    # ...nor any response: the publish echo or the public git provenance.
    for text in (
        resp.text,
        api.client.get(f"/a/{artifact_id}/meta").text,
        api.client.get(f"/a/{artifact_id}/versions").text,
    ):
        assert "SECRET" not in text
        assert "pass" not in text
        assert "frag" not in text


def test_update_and_version_strip_the_git_url_query_and_fragment(
    api: Api, monkeypatch
) -> None:
    captured = _stub_build_from_git(monkeypatch)
    artifact_id = _publish_markdown(api, "# One")

    updated = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"git_url": "https://gitlab.com/org/repo?token=SECRET#frag"},
        headers=AUTH_HEADERS,
    )
    assert updated.status_code == 200, updated.text
    assert captured["git_url"] == "https://gitlab.com/org/repo"
    assert "SECRET" not in updated.text

    submitted = api.client.post(
        f"/api/artifacts/{artifact_id}/versions",
        json={"git_url": "https://gitlab.com/org/other?token=SECRET"},
        headers=AUTH_HEADERS,
    )
    assert submitted.status_code == 201, submitted.text
    assert captured["git_url"] == "https://gitlab.com/org/other"
    assert "SECRET" not in submitted.text
    assert "SECRET" not in api.client.get(f"/a/{artifact_id}/versions").text


def test_private_git_url_is_withheld_from_public_metadata(
    api: Api, monkeypatch
) -> None:
    """A private repo's address is not public provenance (sec-authz)."""
    _stub_build_from_git(monkeypatch)
    resp = api.client.post(
        "/api/artifacts",
        json={
            "git_url": "https://github.com/org/private-repo",
            "git_ref": "main",
            "git_token": GIT_TOKEN,
        },
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    artifact_id = resp.json()["id"]

    meta = api.client.get(f"/a/{artifact_id}/meta").json()
    assert meta["git"]["private"] is True
    assert "url" not in meta["git"]
    assert meta["git"]["ref"] == "main"
    assert meta["git"]["commit"]
    assert "private-repo" not in api.client.get(f"/a/{artifact_id}/meta").text

    listing = api.client.get(f"/a/{artifact_id}/versions")
    row = listing.json()["versions"][0]
    assert row["git"]["private"] is True
    assert "url" not in row["git"]
    assert "private-repo" not in listing.text


def test_public_git_url_is_still_reported(api: Api, monkeypatch) -> None:
    """Regression: only *private* provenance is redacted."""
    _stub_build_from_git(monkeypatch)
    resp = api.client.post(
        "/api/artifacts",
        json={"git_url": "https://github.com/org/public-repo"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    artifact_id = resp.json()["id"]

    meta = api.client.get(f"/a/{artifact_id}/meta").json()
    assert meta["git"]["url"] == "https://github.com/org/public-repo"
    assert "private" not in meta["git"]

    row = api.client.get(f"/a/{artifact_id}/versions").json()["versions"][0]
    assert row["git"]["url"] == "https://github.com/org/public-repo"


def test_owner_can_read_each_receivers_signing_key(hooked) -> None:
    """A receiver has to be configured with its own key, so the owner needs it.

    Delivered on its own endpoint rather than in every artifact response:
    the key is a credential, and the general owner view is returned on every
    publish and update, where it would end up in far more logs and
    transcripts than necessary.
    """
    api, posts, dispatcher = hooked
    artifact_id = _publish_markdown(api, "# One", accept_versions=True)
    other_hook = "https://hooks.test/second"
    assert _register_hook(api, artifact_id, [HOOK, other_hook]).status_code == 200

    listed = api.client.get(
        f"/api/artifacts/{artifact_id}/webhooks", headers=AUTH_HEADERS
    )
    assert listed.status_code == 200, listed.text
    receivers = {row["url"]: row["signing_key"] for row in listed.json()["webhooks"]}
    assert set(receivers) == {HOOK, other_hook}
    assert receivers[HOOK] != receivers[other_hook]
    # The hub-wide webhook key must not be what a receiver is handed.
    assert derive_key(api.settings.secret_key, KEY_LABEL_WEBHOOK) not in receivers.values()

    # The reported key is the one a delivery actually verifies under.
    assert (
        _submit_version(api, artifact_id, "# Two", headers=OTHER_AUTH_HEADERS).status_code
        == 201
    )
    assert dispatcher.drain() == 2
    for url, body, headers in posts:
        expected = hmac.new(
            receivers[url].encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        assert headers["X-Hub-Signature-256"] == f"sha256={expected}", url


def test_webhook_signing_keys_are_owner_only(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    assert (
        api.client.get(
            f"/api/artifacts/{artifact_id}/webhooks", headers=OTHER_AUTH_HEADERS
        ).status_code
        == 403
    )
    # No headers at all is a 400 here, not a 401: resolve_stack runs before
    # the token is verified, the same way it does on every owner-only route.
    assert api.client.get(f"/api/artifacts/{artifact_id}/webhooks").status_code == 400
    assert (
        api.client.get(
            f"/api/artifacts/{artifact_id}/webhooks",
            headers={"X-StorageApi-Token": "nope", "X-Kbc-Stack": "us"},
        ).status_code
        == 401
    )
    assert (
        api.client.get(
            "/api/artifacts/does-not-exist/webhooks", headers=AUTH_HEADERS
        ).status_code
        == 404
    )


@pytest.mark.parametrize("field", ["git_ref", "git_path"])
def test_publish_rejects_git_fields_without_a_git_url(api: Api, field: str) -> None:
    resp = api.client.post(
        "/api/artifacts",
        json={"markdown": "# Hi", field: "whatever"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422
    assert field in resp.json()["detail"]
    assert "git_url" in resp.json()["detail"]


@pytest.mark.parametrize("field", ["git_ref", "git_path"])
def test_update_rejects_git_fields_without_a_git_url(api: Api, field: str) -> None:
    artifact_id = _publish_markdown(api, "# One")
    resp = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={field: "whatever"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422
    assert field in resp.json()["detail"]


@pytest.mark.parametrize("field", ["git_ref", "git_path"])
def test_version_submission_rejects_git_fields_without_a_git_url(
    api: Api, field: str
) -> None:
    artifact_id = _publish_markdown(api, "# One")
    resp = api.client.post(
        f"/api/artifacts/{artifact_id}/versions",
        json={"markdown": "# Two", field: "whatever"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422
    assert field in resp.json()["detail"]


# --------------------------------------------------------------------------
# Hardening: write-path limits, ordering and partial failures
# --------------------------------------------------------------------------


def test_owner_content_updates_count_against_the_daily_budget(
    api: Api, monkeypatch
) -> None:
    """sec-abuse-limits: PUT with content is a submission like any other."""
    artifact_id = _publish_markdown(api, "# One")
    monkeypatch.setattr(
        main,
        "settings",
        dataclasses.replace(api.settings, max_versions_per_day=1),
    )

    first = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"markdown": "# Two"},
        headers=AUTH_HEADERS,
    )
    assert first.status_code == 200, first.text

    second = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"markdown": "# Three"},
        headers=AUTH_HEADERS,
    )
    assert second.status_code == 429
    assert second.json()["limit"] == 1

    # Nothing was persisted by the throttled call.
    assert "Three" not in api.client.get(f"/a/{artifact_id}/raw").text
    # A settings-only update is not a submission and still goes through.
    assert (
        api.client.put(
            f"/api/artifacts/{artifact_id}",
            json={"accept_versions": True},
            headers=AUTH_HEADERS,
        ).status_code
        == 200
    )


def test_update_does_not_apply_settings_when_the_build_fails(
    api: Api, monkeypatch
) -> None:
    """concept-error-handling: no half-applied update."""
    artifact_id = _publish_markdown(api, "# One")
    monkeypatch.setattr(
        main, "settings", dataclasses.replace(api.settings, max_html_bytes=10)
    )

    resp = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={
            "html": "<html><body>" + "x" * 500 + "</body></html>",
            "title": "t",
            # Settings carried by the *same* call, which used to be saved
            # before the build ran and therefore survived its failure.
            "accept_versions": True,
            "password": "should-not-stick",
            "comments_mode": "off",
        },
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 413

    meta = main.app.state.store.get_meta(artifact_id)
    assert meta is not None
    assert meta.accept_versions is False
    assert meta.comments_mode == "anyone"
    assert meta.password is None
    assert api.client.get(f"/a/{artifact_id}").status_code == 200


def test_publish_rolls_back_when_the_canonical_upload_fails(
    api: Api, monkeypatch
) -> None:
    """concept-error-handling: no artifact survives a failed canonical copy."""
    monkeypatch.setattr(main, "new_artifact_id", lambda: "rollback-probe-id")
    hub_backend = api.backend

    class _FailingCanonical:
        def upload(self, name: str, content: bytes, tags: list[str]) -> int:
            raise BackendError("the caller's project storage is down")

    def factory(stack_url: str, token: str, project_id: int | None = None):
        if (
            stack_url == api.settings.hub_stack_url
            and token == api.settings.hub_storage_token
        ):
            return hub_backend
        return _FailingCanonical()

    monkeypatch.setattr(main, "KbcFilesBackend", factory)

    resp = api.client.post(
        "/api/artifacts", json={"markdown": "# Doomed"}, headers=AUTH_HEADERS
    )
    assert resp.status_code == 502
    assert "canonical" in resp.json()["detail"]

    assert api.client.get("/a/rollback-probe-id").status_code == 404
    assert api.backend.search_by_tag("artifact-hub") == []
    assert main.app.state.store.count() == 0


def _break_backend_delete(api: Api, monkeypatch) -> None:
    def failing_delete(file_id: int) -> None:
        raise BackendError("storage refused the delete")

    monkeypatch.setattr(api.backend, "delete", failing_delete)


def test_purge_artifact_reports_partial_failure_as_502(
    api: Api, monkeypatch
) -> None:
    """Never claim 'deleted' over files that are still being served."""
    artifact_id = _publish_markdown(api, "# Mine")
    _break_backend_delete(api, monkeypatch)

    resp = api.client.delete(
        f"/api/artifacts/{artifact_id}/purge", headers=AUTH_HEADERS
    )
    assert resp.status_code == 502
    assert resp.json()["error"] == "artifact not fully deleted"
    assert "retry" in resp.json()["detail"].lower()

    # The artifact really is still there, which is exactly why 200 would lie.
    assert api.client.get(f"/a/{artifact_id}").status_code == 200


def test_delete_version_reports_partial_failure_as_502(
    api: Api, monkeypatch
) -> None:
    artifact_id = _publish_markdown(api, "# One")
    assert _submit_version(api, artifact_id, "# Two").status_code == 201
    _break_backend_delete(api, monkeypatch)

    resp = api.client.delete(
        f"/api/artifacts/{artifact_id}/versions/1", headers=AUTH_HEADERS
    )
    assert resp.status_code == 502
    assert resp.json()["error"] == "version not fully deleted"
    assert api.client.get(f"/a/{artifact_id}/v/1").status_code == 200


def test_delete_only_live_version_is_still_409(api: Api) -> None:
    """Regression: the policy refusal must not be confused with a 502."""
    artifact_id = _publish_markdown(api, "# Only one")
    resp = api.client.delete(
        f"/api/artifacts/{artifact_id}/versions/1", headers=AUTH_HEADERS
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "cannot delete the only live version"


def test_context_documents_the_health_headers_diagnostic(api: Api) -> None:
    by_path = {e["path"]: e for e in api.client.get("/context").json()["endpoints"]}
    entry = by_path["/health/headers"]
    assert entry["method"] == "GET"
    assert entry["auth"] == "none"
    assert "header" in entry["purpose"].lower()
    # It really exists, and OpenAPI knows about it too.
    assert api.client.get("/health/headers").status_code == 200
    assert "/health/headers" in api.client.get("/openapi.json").json()["paths"]


class TestVaultProposalPrivacy:
    """The public vault export must not leak moderated proposals."""

    def _seed(self, api):
        r = api.client.post(
            "/api/artifacts",
            json={"markdown": "# Doc\n\nbody\n", "accept_versions": True},
            headers=AUTH_HEADERS,
        )
        aid = r.json()["id"]
        r = api.client.post(
            f"/api/artifacts/{aid}/versions",
            json={"markdown": "# Doc\n\nproposed change\n", "note": "outside idea"},
            headers=OTHER_AUTH_HEADERS,
        )
        assert r.json()["status"] == "proposed"
        return aid

    def test_anonymous_vault_excludes_proposals(self, api):
        aid = self._seed(api)
        import io
        import zipfile

        payload = api.client.get(f"/a/{aid}/export/vault").content
        names = zipfile.ZipFile(io.BytesIO(payload)).namelist()
        assert any(n.endswith("versions/v1.md") for n in names)
        assert not any(n.endswith("versions/v2.md") for n in names)

    def test_owner_vault_includes_proposals(self, api):
        aid = self._seed(api)
        import io
        import zipfile

        payload = api.client.get(
            f"/a/{aid}/export/vault", headers=AUTH_HEADERS
        ).content
        names = zipfile.ZipFile(io.BytesIO(payload)).namelist()
        assert any(n.endswith("versions/v2.md") for n in names)


# --------------------------------------------------------------------------
# 0.7.0 — share ids and link rotation
#
# Every /a/{...} route addresses an artifact by its share id; the /api/* routes
# keep addressing it by the internal id. The two start out equal, so these
# tests rotate the link to force them apart before asserting anything.
# --------------------------------------------------------------------------


def _rotate(api: Api, artifact_id: str, headers: dict[str, str] = AUTH_HEADERS):
    return api.client.post(
        f"/api/artifacts/{artifact_id}/rotate-link", headers=headers
    )


def test_publish_reports_a_share_id_equal_to_the_id(api: Api) -> None:
    resp = api.client.post(
        "/api/artifacts", json={"markdown": "# One"}, headers=AUTH_HEADERS
    )
    body = resp.json()
    assert body["share_id"] == body["id"]
    assert body["url"].endswith(f"/a/{body['share_id']}")


def test_rotation_revokes_the_old_link_and_serves_the_new_one(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Rotate me")
    assert api.client.get(f"/a/{artifact_id}").status_code == 200

    rotated = _rotate(api, artifact_id)
    assert rotated.status_code == 200, rotated.text
    body = rotated.json()
    share_id = body["share_id"]
    assert share_id != artifact_id
    assert body["id"] == artifact_id
    assert body["previous_share_id"] == artifact_id
    assert "404" in body["warning"]
    assert body["url"] == f"https://testserver/a/{share_id}"

    # The new link works; the old one — and the bare artifact id — do not.
    assert api.client.get(f"/a/{share_id}").status_code == 200
    assert api.client.get(f"/a/{artifact_id}").status_code == 404
    for suffix in ("/raw", "/source", "/meta", "/versions", "/comments", "/review"):
        assert api.client.get(f"/a/{artifact_id}{suffix}").status_code == 404
        assert api.client.get(f"/a/{share_id}{suffix}").status_code == 200


def test_every_public_route_follows_the_rotated_share_id(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One", accept_versions=True)
    assert _submit_version(api, artifact_id, "# Two").status_code == 201
    thread_id = _comment(api, artifact_id, exact="One").json()["id"]
    share_id = _rotate(api, artifact_id).json()["share_id"]

    assert api.client.get(f"/a/{share_id}/v/2").status_code == 200
    assert api.client.get(f"/a/{share_id}/diff/1..2").status_code == 200
    assert api.client.get(f"/a/{share_id}/export/markdown").status_code == 200
    assert api.client.get(f"/a/{share_id}/export/vault").status_code == 200
    threads = api.client.get(f"/a/{share_id}/comments").json()["threads"]
    assert [t["id"] for t in threads] == [thread_id]

    # And none of them answer on the revoked identifier.
    assert api.client.get(f"/a/{artifact_id}/v/2").status_code == 404
    assert api.client.get(f"/a/{artifact_id}/diff/1..2").status_code == 404
    assert api.client.get(f"/a/{artifact_id}/export/vault").status_code == 404


def test_api_routes_still_address_the_internal_id_after_rotation(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One", accept_versions=True)
    share_id = _rotate(api, artifact_id).json()["share_id"]

    # Everything owner-facing keeps working on the id it was published under.
    assert _submit_version(api, artifact_id, "# Two").status_code == 201
    assert _comment(api, artifact_id, exact="One").status_code == 201
    assert (
        api.client.put(
            f"/api/artifacts/{artifact_id}",
            json={"accept_versions": False},
            headers=AUTH_HEADERS,
        ).status_code
        == 200
    )
    # ...and the share id is *not* an /api/* handle.
    assert (
        api.client.put(
            f"/api/artifacts/{share_id}",
            json={"accept_versions": False},
            headers=AUTH_HEADERS,
        ).status_code
        == 404
    )


def test_returned_urls_are_rebuilt_from_the_new_share_id(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    share_id = _rotate(api, artifact_id).json()["share_id"]
    root = f"https://testserver/a/{share_id}"

    listed = api.client.get("/api/artifacts", headers=AUTH_HEADERS).json()
    row = next(a for a in listed["artifacts"] if a["id"] == artifact_id)
    assert row["share_id"] == share_id
    assert row["url"] == root
    assert row["raw_url"] == f"{root}/raw"

    updated = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"markdown": "# Two"},
        headers=AUTH_HEADERS,
    ).json()
    assert updated["share_id"] == share_id
    assert updated["url"] == root

    submitted = _submit_version(api, artifact_id, "# Three").json()
    assert submitted["url"] == f"{root}/v/{submitted['version']}"

    # The public metadata and history name the artifact publicly, too.
    assert api.client.get(f"{root}/meta").json()["id"] == share_id
    versions = api.client.get(f"{root}/versions").json()
    assert versions["id"] == share_id
    assert versions["versions"][0]["url"].startswith(f"{root}/v/")


def test_rotate_link_is_owner_only_and_404s_for_unknown(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    assert _rotate(api, artifact_id, OTHER_AUTH_HEADERS).status_code == 403
    assert _rotate(api, "does-not-exist").status_code == 404


def test_rotation_moves_the_unlock_cookie_to_the_new_path(api: Api) -> None:
    """The cookie is path-scoped, so a rotated link asks for the password again."""
    artifact_id = _publish_markdown(api, "# Secret", password="hunter2")
    unlocked = api.client.post(
        f"/a/{artifact_id}/unlock",
        data={"password": "hunter2"},
        follow_redirects=False,
    )
    assert unlocked.status_code == 303
    assert api.client.get(f"/a/{artifact_id}").status_code == 200

    share_id = _rotate(api, artifact_id).json()["share_id"]
    # The old cookie's path no longer matches, so the gate is up again.
    assert api.client.get(f"/a/{share_id}").status_code == 401
    again = api.client.post(
        f"/a/{share_id}/unlock", data={"password": "hunter2"}, follow_redirects=False
    )
    assert again.status_code == 303
    assert again.headers["location"] == f"/a/{share_id}"
    assert api.client.get(f"/a/{share_id}").status_code == 200


# --------------------------------------------------------------------------
# 0.7.0 — trash, restore and purge
# --------------------------------------------------------------------------


def _trash(api: Api, artifact_id: str, headers: dict[str, str] = AUTH_HEADERS):
    return api.client.delete(f"/api/artifacts/{artifact_id}", headers=headers)


def _restore(api: Api, artifact_id: str, headers: dict[str, str] = AUTH_HEADERS):
    return api.client.post(f"/api/artifacts/{artifact_id}/restore", headers=headers)


def test_trash_restore_round_trip(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Round trip", accept_versions=True)

    assert _trash(api, artifact_id).status_code == 200
    assert api.client.get(f"/a/{artifact_id}").status_code == 404

    restored = _restore(api, artifact_id)
    assert restored.status_code == 200, restored.text
    assert restored.json()["restored"] is True
    assert restored.json()["artifact_status"] == "draft"
    assert restored.json()["url"] == f"https://testserver/a/{artifact_id}"

    assert api.client.get(f"/a/{artifact_id}").status_code == 200
    assert _submit_version(api, artifact_id, "# Two").status_code == 201


def test_restore_returns_to_the_status_it_was_trashed_from(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Done")
    assert _policy(api, artifact_id, status="final").status_code == 200

    assert _trash(api, artifact_id).status_code == 200
    assert _restore(api, artifact_id).json()["artifact_status"] == "final"
    # Still frozen, because that is the state it went into the trash in.
    assert _submit_version(api, artifact_id, "# Nope").status_code == 409


def test_owner_listing_shows_the_trash(api: Api) -> None:
    kept = _publish_markdown(api, "# Kept")
    binned = _publish_markdown(api, "# Binned")
    assert _trash(api, binned).status_code == 200

    rows = {
        row["id"]: row
        for row in api.client.get("/api/artifacts", headers=AUTH_HEADERS).json()[
            "artifacts"
        ]
    }
    assert rows[kept]["status"] == "draft"
    assert rows[kept]["trashed_at"] == ""
    assert rows[binned]["status"] == "trashed"
    assert rows[binned]["trashed_at"]


def test_trashed_artifact_freezes_versions_and_comments(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One", accept_versions=True)
    thread_id = _comment(api, artifact_id, exact="One").json()["id"]
    assert _trash(api, artifact_id).status_code == 200

    version = _submit_version(api, artifact_id, "# Two")
    assert version.status_code == 409
    assert version.json()["error"] == "document is trashed"
    assert "restore" in version.json()["detail"]

    comment = _comment(api, artifact_id, exact="One")
    assert comment.status_code == 409
    assert comment.json()["error"] == "document is trashed"

    reply = api.client.post(
        f"/api/artifacts/{artifact_id}/comments/{thread_id}/replies",
        json={"body": "frozen"},
        headers=AUTH_HEADERS,
    )
    assert reply.status_code == 409
    assert reply.json()["error"] == "document is trashed"

    content = _policy(api, artifact_id, markdown="# Two")
    assert content.status_code == 409
    assert content.json()["error"] == "document is trashed"


def test_trashed_artifact_cannot_be_thawed_through_the_status_field(api: Api) -> None:
    """Restoring is its own route, never a side effect of a settings PUT."""
    artifact_id = _publish_markdown(api, "# One")
    assert _trash(api, artifact_id).status_code == 200

    resp = _policy(api, artifact_id, status="draft")
    assert resp.status_code == 409
    assert "restore" in resp.json()["detail"]
    assert api.client.get(f"/a/{artifact_id}").status_code == 404


def test_status_trashed_is_not_settable(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    resp = _policy(api, artifact_id, status="trashed")
    assert resp.status_code == 422
    assert "draft" in resp.json()["detail"] and "final" in resp.json()["detail"]
    # Still perfectly readable — nothing was applied.
    assert api.client.get(f"/a/{artifact_id}").status_code == 200


def test_trashing_twice_is_a_successful_no_op(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    assert _trash(api, artifact_id).status_code == 200
    assert _trash(api, artifact_id).status_code == 200


def test_restore_of_a_live_artifact_is_409(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    resp = _restore(api, artifact_id)
    assert resp.status_code == 409
    assert resp.json()["error"] == "artifact is not in the trash"


def test_restore_is_owner_only_and_404s_for_unknown(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    assert _trash(api, artifact_id).status_code == 200
    assert _restore(api, artifact_id, OTHER_AUTH_HEADERS).status_code == 403
    assert _restore(api, "does-not-exist").status_code == 404


def test_trash_then_purge_erases_everything(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    _comment(api, artifact_id, exact="One")
    assert api.client.get(f"/a/{artifact_id}").status_code == 200
    stats_before = api.client.get(
        f"/api/artifacts/{artifact_id}/stats", headers=AUTH_HEADERS
    ).json()
    assert stats_before["total"] >= 1

    assert _trash(api, artifact_id).status_code == 200
    purged = api.client.delete(
        f"/api/artifacts/{artifact_id}/purge", headers=AUTH_HEADERS
    )
    assert purged.status_code == 200
    assert purged.json()["comment_threads_deleted"] == 1

    # Gone from every angle: content, threads, view statistics, owner listing.
    assert api.client.get(f"/a/{artifact_id}").status_code == 404
    assert main.app.state.comments.list_for(artifact_id) == []
    assert main.app.state.statedb.views(artifact_id)["total"] == 0
    assert (
        api.client.get(
            f"/api/artifacts/{artifact_id}/stats", headers=AUTH_HEADERS
        ).status_code
        == 404
    )
    listed = api.client.get("/api/artifacts", headers=AUTH_HEADERS).json()
    assert artifact_id not in {row["id"] for row in listed["artifacts"]}


# --------------------------------------------------------------------------
# 0.7.0 — base_version and the "outdated" flag
# --------------------------------------------------------------------------


def test_base_version_is_stored_and_echoed(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One", accept_versions=True)
    resp = api.client.post(
        f"/api/artifacts/{artifact_id}/versions",
        json={"markdown": "# Two", "base_version": 1},
        headers=OTHER_AUTH_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["base_version"] == 1

    rows = api.client.get(f"/a/{artifact_id}/versions").json()["versions"]
    proposal = next(row for row in rows if row["version"] == 2)
    assert proposal["base_version"] == 1


def test_base_version_defaults_to_none(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    assert _submit_version(api, artifact_id, "# Two").json()["base_version"] is None


def test_unknown_base_version_is_422(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    resp = api.client.post(
        f"/api/artifacts/{artifact_id}/versions",
        json={"markdown": "# Two", "base_version": 99},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422
    assert "base_version" in resp.json()["detail"]
    # Nothing was stored, so the history is untouched.
    assert len(api.client.get(f"/a/{artifact_id}/versions").json()["versions"]) == 1


def test_outdated_flag_tracks_the_head(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One", accept_versions=True)
    # A proposal written against the current head (v1) is not outdated.
    assert (
        api.client.post(
            f"/api/artifacts/{artifact_id}/versions",
            json={"markdown": "# Proposal", "base_version": 1},
            headers=OTHER_AUTH_HEADERS,
        ).status_code
        == 201
    )

    def proposal_row():
        rows = api.client.get(f"/a/{artifact_id}/versions").json()["versions"]
        return next(row for row in rows if row["version"] == 2)

    assert proposal_row()["outdated"] is False

    # The owner publishes v3, so the head moves and the proposal falls behind.
    assert _submit_version(api, artifact_id, "# Owner update").status_code == 201
    assert proposal_row()["outdated"] is True


def test_a_proposal_without_a_base_version_is_never_outdated(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One", accept_versions=True)
    assert (
        _submit_version(
            api, artifact_id, "# Proposal", headers=OTHER_AUTH_HEADERS
        ).status_code
        == 201
    )
    assert _submit_version(api, artifact_id, "# Owner update").status_code == 201

    rows = api.client.get(f"/a/{artifact_id}/versions").json()["versions"]
    proposal = next(row for row in rows if row["version"] == 2)
    assert proposal["base_version"] is None
    assert proposal["outdated"] is False


def test_live_versions_carry_no_outdated_flag(api: Api) -> None:
    """"outdated" answers a question only a pending proposal raises."""
    artifact_id = _publish_markdown(api, "# One")
    assert _submit_version(api, artifact_id, "# Two").status_code == 201
    rows = api.client.get(f"/a/{artifact_id}/versions").json()["versions"]
    assert rows
    assert all("outdated" not in row for row in rows)


# --------------------------------------------------------------------------
# 0.7.0 — view statistics
# --------------------------------------------------------------------------


def test_stats_count_each_surface(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Counted")
    assert _submit_version(api, artifact_id, "# Counted v2").status_code == 201

    api.client.get(f"/a/{artifact_id}")
    api.client.get(f"/a/{artifact_id}")
    api.client.get(f"/a/{artifact_id}/raw")
    api.client.get(f"/a/{artifact_id}/source")
    api.client.get(f"/a/{artifact_id}/v/1")

    resp = api.client.get(
        f"/api/artifacts/{artifact_id}/stats", headers=AUTH_HEADERS
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == artifact_id
    assert body["share_id"] == artifact_id
    assert body["by_kind"] == {"page": 2, "raw": 1, "source": 1, "version": 1}
    assert body["total"] == 5
    assert len(body["by_day"]) == 1
    assert body["by_day"][0]["count"] == 5


def test_stats_of_an_unread_artifact_are_zero(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Unread")
    body = api.client.get(
        f"/api/artifacts/{artifact_id}/stats", headers=AUTH_HEADERS
    ).json()
    assert body == {
        "total": 0,
        "by_day": [],
        "by_kind": {},
        "id": artifact_id,
        "share_id": artifact_id,
    }


def test_stats_report_the_current_share_id(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    share_id = _rotate(api, artifact_id).json()["share_id"]
    api.client.get(f"/a/{share_id}")

    body = api.client.get(
        f"/api/artifacts/{artifact_id}/stats", headers=AUTH_HEADERS
    ).json()
    assert body["id"] == artifact_id
    assert body["share_id"] == share_id
    # Views are counted against the internal id, so rotation does not reset them.
    assert body["total"] == 1


def test_stats_are_owner_only_and_404_for_unknown(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    assert (
        api.client.get(
            f"/api/artifacts/{artifact_id}/stats", headers=OTHER_AUTH_HEADERS
        ).status_code
        == 403
    )
    assert (
        api.client.get(
            "/api/artifacts/does-not-exist/stats", headers=AUTH_HEADERS
        ).status_code
        == 404
    )


def test_a_404_read_is_not_counted(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    api.client.get("/a/does-not-exist")
    assert api.client.get(f"/a/{artifact_id}/raw").status_code == 200
    body = api.client.get(
        f"/api/artifacts/{artifact_id}/stats", headers=AUTH_HEADERS
    ).json()
    assert body["by_kind"] == {"raw": 1}


def test_a_gated_read_is_not_counted(api: Api) -> None:
    """A 401 from the password gate never reaches the content, so it is no view."""
    artifact_id = _publish_markdown(api, "# Secret", password="hunter2")
    assert api.client.get(f"/a/{artifact_id}").status_code == 401
    assert (
        api.client.get(
            f"/a/{artifact_id}/raw", headers={"X-Artifact-Password": "hunter2"}
        ).status_code
        == 200
    )
    body = api.client.get(
        f"/api/artifacts/{artifact_id}/stats", headers=AUTH_HEADERS
    ).json()
    assert body["by_kind"] == {"raw": 1}


# --------------------------------------------------------------------------
# 0.7.0 — rate-limit counters live in the state sidecar
# --------------------------------------------------------------------------


def test_rate_limit_counters_land_in_the_state_database(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    assert _submit_version(api, artifact_id, "# Two").status_code == 201
    _comment(api, artifact_id, exact="One")

    day = main._utc_day()
    # The key convention statedb documents: internal artifact id first (so
    # forget_artifact can purge it), then the contributor's owner key.
    key = f"{artifact_id}:123@connection.keboola.com"
    database = main.app.state.statedb
    assert database.count("submissions", key, day) == 1, key
    assert database.count("comments", key, day) == 1


def test_submission_counters_survive_a_new_statedb_over_the_same_backend(
    api: Api, monkeypatch, tmp_path
) -> None:
    """The point of the sidecar: a redeploy must not hand out a fresh budget."""
    artifact_id = _publish_markdown(api, "# One")
    monkeypatch.setattr(
        main, "settings", dataclasses.replace(api.settings, max_versions_per_day=2)
    )
    assert _submit_version(api, artifact_id, "# Two").status_code == 201
    assert _submit_version(api, artifact_id, "# Three").status_code == 201
    assert _submit_version(api, artifact_id, "# Four").status_code == 429

    # Snapshot to Storage, then rebuild a sidecar from scratch the way a
    # restarted container would, and check the tally came back.
    assert main.app.state.statedb.snapshot_now() is True
    revived = StateDB(api.backend, tmp_path / "revived.sqlite3", 0, 0)
    revived.start()
    try:
        key = f"{artifact_id}:123@connection.keboola.com"
        assert revived.count("submissions", key, main._utc_day()) >= 3
    finally:
        revived.stop()


def test_unlock_failures_are_counted_in_the_state_database(
    api: Api, monkeypatch
) -> None:
    artifact_id = _publish_markdown(api, "# Secret", password="hunter2")
    _low_unlock_limit(api, monkeypatch, limit=2)

    for _ in range(2):
        assert (
            api.client.post(
                f"/a/{artifact_id}/unlock",
                data={"password": "nope"},
                follow_redirects=False,
            ).status_code
            == 401
        )

    database = main.app.state.statedb
    key = f"{artifact_id}:testclient"
    assert database.count("unlock_failures", key, main._utc_hour()) == 2


def test_counters_still_work_without_a_state_database(api: Api, monkeypatch) -> None:
    """A missing sidecar degrades to per-process counting, never to no limit."""
    artifact_id = _publish_markdown(api, "# One")
    monkeypatch.setattr(
        main, "settings", dataclasses.replace(api.settings, max_versions_per_day=1)
    )
    monkeypatch.delattr(main.app.state, "statedb", raising=False)
    main._fallback_counts.clear()

    assert _submit_version(api, artifact_id, "# Two").status_code == 201
    assert _submit_version(api, artifact_id, "# Three").status_code == 429
    main._fallback_counts.clear()


def test_a_broken_state_database_never_breaks_serving(api: Api, monkeypatch) -> None:
    """Analytics are best effort: a failing sidecar must not 500 a page load."""
    artifact_id = _publish_markdown(api, "# One")

    def boom(*args, **kwargs):
        raise RuntimeError("state database is on fire")

    monkeypatch.setattr(main.app.state.statedb, "record_view", boom)
    assert api.client.get(f"/a/{artifact_id}").status_code == 200
    assert api.client.get(f"/a/{artifact_id}/raw").status_code == 200


# --------------------------------------------------------------------------
# 0.7.0 — outbound webhooks
# --------------------------------------------------------------------------


@pytest.fixture
def hooked(api: Api, monkeypatch):
    """An api whose webhook validator accepts the test host and records posts.

    ``validate_webhook_url`` is patched at the seam ``src.main`` imported it
    through, so the real https/SSRF rules keep applying to everything except
    the one host these tests register. Deliveries are drained synchronously,
    exactly like tests/test_webhooks.py does.
    """
    real_validate = main.validate_webhook_url

    def fake_validate(url: str) -> str:
        # A localhost URL would be (correctly) refused by the SSRF guard, so
        # the test host bypasses resolution only for itself.
        if url.startswith("https://hooks.test/"):
            return url
        return real_validate(url)

    monkeypatch.setattr(main, "validate_webhook_url", fake_validate)

    posts: list[tuple[str, bytes, dict]] = []

    def record(url: str, body: bytes, headers: dict) -> int:
        posts.append((url, body, dict(headers)))
        return 200

    dispatcher = main.app.state.webhooks
    monkeypatch.setattr(dispatcher, "_post", record)
    # The delivery-time SSRF guard fails closed, and "hooks.test" does not
    # resolve, so it needs the same injected public answer tests/test_webhooks.py
    # uses. Without this the deliveries are (correctly) dropped before the POST.
    monkeypatch.setattr(dispatcher, "_resolver", lambda _hostname: ["93.184.216.34"])
    # The dispatcher's own thread would race drain(); stop it and drive
    # delivery from the test thread instead.
    dispatcher.stop()
    return api, posts, dispatcher


def _register_hook(api: Api, artifact_id: str, urls: list[str]):
    return api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"webhooks": urls},
        headers=AUTH_HEADERS,
    )


HOOK = "https://hooks.test/artifact"


def test_webhook_delivers_a_signed_event_for_a_submitted_version(hooked) -> None:
    api, posts, dispatcher = hooked
    artifact_id = _publish_markdown(api, "# One", accept_versions=True)

    registered = _register_hook(api, artifact_id, [HOOK])
    assert registered.status_code == 200, registered.text
    # The owner who set them gets them back in full.
    assert registered.json()["webhooks"] == [HOOK]

    assert (
        _submit_version(
            api, artifact_id, "# Two", headers=OTHER_AUTH_HEADERS
        ).status_code
        == 201
    )
    assert dispatcher.drain() == 1

    url, body, headers = posts[0]
    assert url == HOOK
    envelope = json.loads(body)
    assert envelope["event"] == "version.proposed"
    assert envelope["artifact_id"] == artifact_id
    assert envelope["created_at"]
    payload = envelope["payload"]
    assert payload["version"] == 2
    assert payload["actor"] == "Other"
    assert payload["url"] == f"https://testserver/a/{artifact_id}"
    # Signed with a key derived per receiver from the webhook-specific key —
    # never the raw master secret, which also backs the unlock cookies (see
    # security.derive_key), and never the hub-wide webhook key either, which
    # every receiver would then share (see test_owner_can_read_each_receivers
    # _signing_key).
    webhook_key = derive_key(api.settings.secret_key, KEY_LABEL_WEBHOOK)
    receiver_key = receiver_signing_key(webhook_key, artifact_id, HOOK)
    expected = hmac.new(
        receiver_key.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    assert headers["X-Hub-Signature-256"] == f"sha256={expected}"
    assert headers["X-Hub-Signature-256"] != f"sha256=" + hmac.new(
        webhook_key.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    # And nothing secret rode along.
    assert "token" not in body.decode("utf-8").lower()
    _assert_no_sensitive_keys(envelope)


@pytest.mark.parametrize(
    "action, kind",
    [
        ("promote", "version.promoted"),
        ("comment", "comment.created"),
        ("reply", "comment.replied"),
        ("finalize", "artifact.finalized"),
        ("trash", "artifact.trashed"),
        ("restore", "artifact.restored"),
        ("rotate", "link.rotated"),
        ("owner_version", "version.published"),
    ],
)
def test_webhook_event_kinds(hooked, action: str, kind: str) -> None:
    api, posts, dispatcher = hooked
    artifact_id = _publish_markdown(api, "# One", accept_versions=True)
    assert _register_hook(api, artifact_id, [HOOK]).status_code == 200
    dispatcher.drain()
    posts.clear()

    if action == "promote":
        assert (
            _submit_version(
                api, artifact_id, "# Two", headers=OTHER_AUTH_HEADERS
            ).status_code
            == 201
        )
        dispatcher.drain()
        posts.clear()
        assert (
            api.client.post(
                f"/api/artifacts/{artifact_id}/versions/2/promote",
                headers=AUTH_HEADERS,
            ).status_code
            == 200
        )
    elif action == "comment":
        assert _comment(api, artifact_id, exact="One").status_code == 201
    elif action == "reply":
        thread_id = _comment(api, artifact_id, exact="One").json()["id"]
        dispatcher.drain()
        posts.clear()
        assert (
            api.client.post(
                f"/api/artifacts/{artifact_id}/comments/{thread_id}/replies",
                json={"body": "hi"},
                headers=AUTH_HEADERS,
            ).status_code
            == 201
        )
    elif action == "finalize":
        assert _policy(api, artifact_id, status="final").status_code == 200
    elif action == "trash":
        assert _trash(api, artifact_id).status_code == 200
    elif action == "restore":
        assert _trash(api, artifact_id).status_code == 200
        dispatcher.drain()
        posts.clear()
        assert _restore(api, artifact_id).status_code == 200
    elif action == "rotate":
        assert _rotate(api, artifact_id).status_code == 200
    else:
        assert _submit_version(api, artifact_id, "# Two").status_code == 201

    assert dispatcher.drain() == 1
    assert json.loads(posts[0][1])["event"] == kind


def test_the_initial_publish_emits_nothing(hooked) -> None:
    """v1 is the owner's own doing; only later versions are news."""
    api, posts, dispatcher = hooked
    artifact_id = _publish_markdown(api, "# One")
    assert _register_hook(api, artifact_id, [HOOK]).status_code == 200
    assert dispatcher.drain() == 0
    assert posts == []


def test_an_artifact_without_webhooks_queues_nothing(hooked) -> None:
    api, posts, dispatcher = hooked
    artifact_id = _publish_markdown(api, "# One")
    assert _submit_version(api, artifact_id, "# Two").status_code == 201
    assert dispatcher.pending() == 0
    assert dispatcher.drain() == 0


def test_every_registered_url_gets_its_own_delivery(hooked) -> None:
    api, posts, dispatcher = hooked
    artifact_id = _publish_markdown(api, "# One")
    second = "https://hooks.test/other"
    assert _register_hook(api, artifact_id, [HOOK, second]).status_code == 200

    assert _submit_version(api, artifact_id, "# Two").status_code == 201
    assert dispatcher.drain() == 2
    assert {url for url, _, _ in posts} == {HOOK, second}


def test_webhooks_can_be_cleared_and_are_left_alone_when_omitted(hooked) -> None:
    api, posts, dispatcher = hooked
    artifact_id = _publish_markdown(api, "# One")
    assert _register_hook(api, artifact_id, [HOOK]).status_code == 200

    # Omitting the field leaves the list untouched.
    untouched = _policy(api, artifact_id, comments_mode="off")
    assert untouched.json()["webhooks"] == [HOOK]

    cleared = _register_hook(api, artifact_id, [])
    assert cleared.status_code == 200
    assert cleared.json()["webhooks"] == []
    assert _submit_version(api, artifact_id, "# Two").status_code == 201
    assert dispatcher.drain() == 0


def test_an_invalid_webhook_url_is_422(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    plain_http = _register_hook(api, artifact_id, ["http://example.com/hook"])
    assert plain_http.status_code == 422
    assert "https" in plain_http.json()["detail"]

    # The SSRF guard applies to webhooks exactly as it does to git clones.
    loopback = _register_hook(api, artifact_id, ["https://127.0.0.1/hook"])
    assert loopback.status_code == 422
    assert "private" in loopback.json()["detail"]

    metadata = _register_hook(api, artifact_id, ["https://metadata.google.internal/x"])
    assert metadata.status_code == 422

    # Nothing was stored by any of the refusals.
    assert (
        api.client.put(
            f"/api/artifacts/{artifact_id}", json={}, headers=AUTH_HEADERS
        ).json()["webhooks"]
        == []
    )


def test_too_many_webhooks_is_422(hooked) -> None:
    api, _posts, _dispatcher = hooked
    artifact_id = _publish_markdown(api, "# One")
    cap = api.settings.max_webhooks_per_artifact
    urls = [f"https://hooks.test/h{n}" for n in range(cap + 1)]

    resp = _register_hook(api, artifact_id, urls)
    assert resp.status_code == 422
    assert str(cap) in resp.json()["detail"]

    # Exactly at the cap is fine.
    assert _register_hook(api, artifact_id, urls[:cap]).status_code == 200


def test_listing_reports_a_webhook_count_never_the_urls(hooked) -> None:
    api, _posts, _dispatcher = hooked
    artifact_id = _publish_markdown(api, "# One")
    assert _register_hook(api, artifact_id, [HOOK]).status_code == 200

    listed = api.client.get("/api/artifacts", headers=AUTH_HEADERS)
    row = next(a for a in listed.json()["artifacts"] if a["id"] == artifact_id)
    assert row["webhooks_count"] == 1
    assert "webhooks" not in row
    # The URL is a capability; it must not appear anywhere in the listing.
    assert HOOK not in listed.text


def test_webhooks_survive_a_restart(hooked) -> None:
    api, _posts, _dispatcher = hooked
    artifact_id = _publish_markdown(api, "# One")
    assert _register_hook(api, artifact_id, [HOOK]).status_code == 200

    second_store = ArtifactStore(
        backend=api.backend,
        cache_dir=api.settings.cache_dir / "restart",
        cache_max_entries=api.settings.cache_max_entries,
        max_versions=api.settings.max_versions,
    )
    second_store.hydrate()
    meta = second_store.get_meta(artifact_id)
    assert meta is not None and meta.webhooks == [HOOK]


# --------------------------------------------------------------------------
# 0.7.0 — OpenAPI coverage of the new surface
# --------------------------------------------------------------------------


def test_openapi_documents_the_new_routes(api: Api) -> None:
    schema = api.client.get("/openapi.json").json()
    for path, method in (
        ("/api/artifacts/{artifact_id}/restore", "post"),
        ("/api/artifacts/{artifact_id}/purge", "delete"),
        ("/api/artifacts/{artifact_id}/rotate-link", "post"),
        ("/api/artifacts/{artifact_id}/stats", "get"),
    ):
        operation = schema["paths"][path][method]
        assert operation["summary"].strip()
        assert operation["description"].strip()
        assert set(operation["responses"]) >= {"200", "403", "404"}
        parameter = _parameter(schema, path, method, "artifact_id")
        assert parameter["description"].strip()

    # The two destructive routes say so in their own words.
    assert (
        "irreversible"
        in schema["paths"]["/api/artifacts/{artifact_id}/purge"]["delete"][
            "description"
        ].lower()
    )
    rotate = schema["paths"]["/api/artifacts/{artifact_id}/rotate-link"]["post"]
    assert "stops working immediately" in rotate["description"]


def test_openapi_documents_the_new_body_fields(api: Api) -> None:
    schemas = api.client.get("/openapi.json").json()["components"]["schemas"]
    webhooks = schemas["UpdateBody"]["properties"]["webhooks"]
    assert "https" in webhooks["description"]
    base_version = schemas["VersionBody"]["properties"]["base_version"]
    assert "outdated" in base_version["description"]


def test_openapi_public_routes_document_the_share_id(api: Api) -> None:
    schema = api.client.get("/openapi.json").json()
    parameter = _parameter(schema, "/a/{artifact_id}", "get", "artifact_id")
    assert "share" in parameter["description"].lower()
    assert "capability" in parameter["description"]
    # The /api/* handle is documented as the internal, stable one.
    internal = _parameter(schema, "/api/artifacts/{artifact_id}", "put", "artifact_id")
    assert "internal" in internal["description"].lower()


def test_context_documents_the_new_capabilities(api: Api) -> None:
    body = api.client.get("/context").json()
    assert "rotate" in body["sharing"]["rotation"].lower()
    assert "trash" in body["sharing"]["trash"].lower()
    assert "outdated" in body["sharing"]["base_version"]
    assert "link.rotated" in body["webhooks"]["events"]
    assert body["limits"]["max_webhooks_per_artifact"] == (
        api.settings.max_webhooks_per_artifact
    )
    assert "page" in body["analytics"]["views"]


# --------------------------------------------------------------------------
# 0.7.0 — guest invitations
#
# An invitation is a named, revocable capability that lets one human without a
# Keboola account comment on one artifact. The credential is
# "{invitation_id}.{secret}"; it is minted once, stored hashed, and presented
# in the X-Artifact-Guest header.
# --------------------------------------------------------------------------


def _invite(
    api: Api,
    artifact_id: str,
    name: str = "Jana",
    headers: dict[str, str] = AUTH_HEADERS,
):
    """Mint one guest invitation; returns the raw response."""
    return api.client.post(
        f"/api/artifacts/{artifact_id}/invitations",
        json={"name": name},
        headers=headers,
    )


def _guest_headers(review_url: str) -> dict[str, str]:
    """Turn an invitation review URL into the header a guest actually sends.

    This mirrors what the review page's JavaScript does: the credential lives
    in the URL *fragment* and only ever travels in a header.
    """
    assert "#invite=" in review_url
    return {"X-Artifact-Guest": review_url.split("#invite=", 1)[1]}


def _guest_comment(
    api: Api,
    artifact_id: str,
    guest: dict[str, str],
    *,
    version: int = 1,
    exact: str = "Body text",
    body: str = "A guest question.",
):
    return api.client.post(
        f"/api/artifacts/{artifact_id}/comments",
        json={
            "version": version,
            "exact": exact,
            "prefix": "",
            "suffix": "",
            "body": body,
        },
        headers=guest,
    )


def test_invitation_returns_a_one_time_review_url(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title\n\nBody text here.")

    created = _invite(api, artifact_id, "Jana (legal)")
    assert created.status_code == 201, created.text
    payload = created.json()
    assert set(payload) == {"invitation_id", "name", "review_url", "warning"}
    assert payload["name"] == "Jana (legal)"

    share_id = api.client.get(f"/a/{artifact_id}/meta").json()["id"]
    prefix = f"https://testserver/a/{share_id}/review#invite="
    assert payload["review_url"].startswith(prefix)

    # The credential is {invitation_id}.{secret}: the id is public, the secret
    # is not, and neither half may be guessable from the other.
    credential = payload["review_url"][len(prefix):]
    invitation_id, _, secret = credential.partition(".")
    assert invitation_id == payload["invitation_id"]
    assert len(secret) >= 20
    assert secret not in invitation_id

    # ...and it is shown exactly once: nothing else ever echoes it.
    listing = api.client.get(
        f"/api/artifacts/{artifact_id}/invitations", headers=AUTH_HEADERS
    )
    assert listing.status_code == 200
    assert secret not in listing.text
    assert secret not in api.client.get(f"/a/{share_id}/comments").text


def test_invitation_secret_is_stored_hashed_not_in_the_clear(api: Api) -> None:
    """A leaked meta file must not be replayable as an invitation."""
    artifact_id = _publish_markdown(api, "# Title")
    review_url = _invite(api, artifact_id).json()["review_url"]
    secret = review_url.split("#invite=", 1)[1].split(".", 1)[1]

    meta = main.app.state.store.get_meta(artifact_id)
    assert meta is not None
    record = meta.invitations[0]["secret"]
    assert record["algo"] == "pbkdf2-sha256"
    assert secret not in json.dumps(meta.invitations)


def test_invitation_listing_omits_secrets_and_tracks_revocation(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title")
    first = _invite(api, artifact_id, "Jana").json()
    second = _invite(api, artifact_id, "Petr").json()

    listed = api.client.get(
        f"/api/artifacts/{artifact_id}/invitations", headers=AUTH_HEADERS
    ).json()
    assert listed["id"] == artifact_id
    assert [inv["id"] for inv in listed["invitations"]] == [
        first["invitation_id"],
        second["invitation_id"],
    ]
    for invitation in listed["invitations"]:
        assert set(invitation) == {"id", "name", "created_at", "revoked"}
        assert invitation["revoked"] is False
        assert invitation["created_at"]

    revoked = api.client.delete(
        f"/api/artifacts/{artifact_id}/invitations/{first['invitation_id']}",
        headers=AUTH_HEADERS,
    )
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] is True

    after = api.client.get(
        f"/api/artifacts/{artifact_id}/invitations", headers=AUTH_HEADERS
    ).json()["invitations"]
    assert [inv["revoked"] for inv in after] == [True, False]

    # Revoking twice is a no-op, and an unknown invitation is a 404.
    assert api.client.delete(
        f"/api/artifacts/{artifact_id}/invitations/{first['invitation_id']}",
        headers=AUTH_HEADERS,
    ).status_code == 200
    assert api.client.delete(
        f"/api/artifacts/{artifact_id}/invitations/nope", headers=AUTH_HEADERS
    ).status_code == 404


def test_invitations_are_owner_only(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title")
    invitation_id = _invite(api, artifact_id).json()["invitation_id"]

    assert _invite(api, artifact_id, headers=OTHER_AUTH_HEADERS).status_code == 403
    assert api.client.get(
        f"/api/artifacts/{artifact_id}/invitations", headers=OTHER_AUTH_HEADERS
    ).status_code == 403
    assert api.client.delete(
        f"/api/artifacts/{artifact_id}/invitations/{invitation_id}",
        headers=OTHER_AUTH_HEADERS,
    ).status_code == 403
    assert _invite(api, "does-not-exist").status_code == 404


def test_invitation_name_is_validated(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title")
    assert _invite(api, artifact_id, "").status_code == 422
    assert _invite(api, artifact_id, "   ").status_code == 422
    assert _invite(api, artifact_id, "x" * 81).status_code == 422
    assert _invite(api, artifact_id, "x" * 80).status_code == 201


def test_invitation_cap_is_enforced_and_revoking_frees_a_slot(
    api: Api, monkeypatch
) -> None:
    artifact_id = _publish_markdown(api, "# Title")
    monkeypatch.setattr(
        main,
        "settings",
        dataclasses.replace(api.settings, max_invitations_per_artifact=2),
    )

    first = _invite(api, artifact_id, "One").json()
    assert _invite(api, artifact_id, "Two").status_code == 201

    full = _invite(api, artifact_id, "Three")
    assert full.status_code == 422
    assert "limit" in full.json()["detail"]

    # Revoking makes room: the tombstone is dropped rather than kept forever,
    # so "revoke somebody, invite somebody else" works indefinitely.
    api.client.delete(
        f"/api/artifacts/{artifact_id}/invitations/{first['invitation_id']}",
        headers=AUTH_HEADERS,
    )
    assert _invite(api, artifact_id, "Three").status_code == 201
    listed = api.client.get(
        f"/api/artifacts/{artifact_id}/invitations", headers=AUTH_HEADERS
    ).json()["invitations"]
    assert [inv["name"] for inv in listed] == ["Two", "Three"]


def test_invitations_are_refused_on_a_frozen_artifact(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title")
    assert _policy(api, artifact_id, status="final").status_code == 200
    frozen = _invite(api, artifact_id)
    assert frozen.status_code == 409
    assert frozen.json()["error"] == "document is final"

    trashed_id = _publish_markdown(api, "# Trash me")
    assert _trash(api, trashed_id).status_code == 200
    assert _invite(api, trashed_id).json()["error"] == "document is trashed"


# ---------------------------------------------------------------- guest writes


def test_guest_can_comment_with_the_invitation_header(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title\n\nBody text here.")
    invitation = _invite(api, artifact_id, "Jana").json()
    guest = _guest_headers(invitation["review_url"])

    created = _guest_comment(api, artifact_id, guest, body="Is this the final wording?")
    assert created.status_code == 201, created.text
    thread = created.json()
    assert thread["author"] == {"kind": "guest", "name": "Jana"}
    assert thread["body"] == "Is this the final wording?"

    # Nothing about the invitation itself reaches a public response.
    listing = api.client.get(f"/a/{artifact_id}/comments")
    assert listing.status_code == 200
    published = listing.json()["threads"][0]
    assert published["author"] == {"kind": "guest", "name": "Jana"}
    assert invitation["invitation_id"] not in listing.text
    assert guest["X-Artifact-Guest"].split(".", 1)[1] not in listing.text
    _assert_no_credentials(listing.json())


def test_guest_can_reply_and_the_reply_is_attributed(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title\n\nBody text here.")
    guest = _guest_headers(_invite(api, artifact_id, "Jana").json()["review_url"])
    thread_id = _comment(api, artifact_id, exact="Body text").json()["id"]

    replied = api.client.post(
        f"/api/artifacts/{artifact_id}/comments/{thread_id}/replies",
        json={"body": "One question about this."},
        headers=guest,
    )
    assert replied.status_code == 201, replied.text
    assert replied.json()["replies"][0]["author"] == {"kind": "guest", "name": "Jana"}


def test_revoked_invitation_is_401(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title\n\nBody text here.")
    invitation = _invite(api, artifact_id, "Jana").json()
    guest = _guest_headers(invitation["review_url"])
    assert _guest_comment(api, artifact_id, guest).status_code == 201

    api.client.delete(
        f"/api/artifacts/{artifact_id}/invitations/{invitation['invitation_id']}",
        headers=AUTH_HEADERS,
    )
    refused = _guest_comment(api, artifact_id, guest)
    assert refused.status_code == 401
    assert "revoked" in refused.json()["detail"]


def test_unknown_and_tampered_guest_credentials_are_401(api: Api) -> None:
    """Revoked, unknown and wrong-secret all answer alike — no oracle."""
    artifact_id = _publish_markdown(api, "# Title\n\nBody text here.")
    invitation = _invite(api, artifact_id, "Jana").json()
    good = _guest_headers(invitation["review_url"])["X-Artifact-Guest"]
    invitation_id, secret = good.split(".", 1)

    wrong_secret = f"{invitation_id}.{'x' * len(secret)}"
    unknown_id = f"nosuchinvitation.{secret}"
    detail = None
    for value in (wrong_secret, unknown_id, "no-dot-at-all", "half."):
        answer = _guest_comment(api, artifact_id, {"X-Artifact-Guest": value})
        assert answer.status_code == 401, value
        # One answer for all of them: a guest must not be able to tell a
        # revoked invitation from an unknown one from a wrong secret.
        detail = detail or answer.json()["detail"]
        assert answer.json()["detail"] == detail, value

    # No header at all is not a guest; it falls through to token auth, which
    # has nothing to verify either.
    assert _guest_comment(api, artifact_id, {}).status_code in (400, 401)

    # An invitation minted for one artifact does not open another.
    other_id = _publish_markdown(api, "# Other\n\nBody text here.")
    assert _guest_comment(
        api, other_id, {"X-Artifact-Guest": good}
    ).status_code == 401


def test_guest_is_frozen_out_of_a_final_or_trashed_artifact(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title\n\nBody text here.")
    guest = _guest_headers(_invite(api, artifact_id).json()["review_url"])

    assert _policy(api, artifact_id, status="final").status_code == 200
    frozen = _guest_comment(api, artifact_id, guest)
    assert frozen.status_code == 409
    assert frozen.json()["error"] == "document is final"

    assert _policy(api, artifact_id, status="draft").status_code == 200
    assert _trash(api, artifact_id).status_code == 200
    # A guest addresses the artifact by its *public* identifier, and a trashed
    # artifact does not resolve publicly at all — GET /a/{id}/comments and the
    # review page it lives in are already 404 here. So the guest sees the same
    # 404 the rest of the public surface shows, not the owner-facing 409; the
    # owner, addressing it by the internal id, still gets "document is trashed".
    binned = _guest_comment(api, artifact_id, guest)
    assert binned.status_code == 404
    owner_view = _comment(api, artifact_id, exact="Body text")
    assert owner_view.status_code == 409
    assert owner_view.json()["error"] == "document is trashed"


def test_comments_mode_does_not_gate_a_guest(api: Api) -> None:
    """The invitation *is* the grant — its issuer is the owner of that mode."""
    artifact_id = _publish_markdown(api, "# Title\n\nBody text here.")
    guest = _guest_headers(_invite(api, artifact_id).json()["review_url"])
    assert _policy(api, artifact_id, comments_mode="off").status_code == 200

    assert _comment(api, artifact_id, headers=OTHER_AUTH_HEADERS).status_code == 403
    assert _guest_comment(api, artifact_id, guest).status_code == 201


def test_guests_are_rate_limited_per_invitation(api: Api, monkeypatch) -> None:
    artifact_id = _publish_markdown(api, "# Title\n\nBody text here.")
    first = _guest_headers(_invite(api, artifact_id, "Jana").json()["review_url"])
    second = _guest_headers(_invite(api, artifact_id, "Petr").json()["review_url"])
    monkeypatch.setattr(
        main, "settings", dataclasses.replace(api.settings, max_comments_per_day=2)
    )

    assert _guest_comment(api, artifact_id, first).status_code == 201
    thread_id = _guest_comment(api, artifact_id, first).json()["id"]
    limited = _guest_comment(api, artifact_id, first)
    assert limited.status_code == 429
    assert limited.json()["limit"] == 2
    assert "invitation" in limited.json()["detail"].lower()

    # Replies draw on the same bucket...
    assert api.client.post(
        f"/api/artifacts/{artifact_id}/comments/{thread_id}/replies",
        json={"body": "also blocked"},
        headers=first,
    ).status_code == 429

    # ...but budgets are per invitation, and never shared with a project.
    assert _guest_comment(api, artifact_id, second).status_code == 201
    assert _comment(api, artifact_id, exact="Body text").status_code == 201


def test_guest_moderates_only_their_own_threads(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title\n\nBody text here.")
    guest = _guest_headers(_invite(api, artifact_id, "Jana").json()["review_url"])

    mine = _guest_comment(api, artifact_id, guest).json()["id"]
    theirs = _comment(api, artifact_id, exact="Body text").json()["id"]

    resolved = api.client.post(
        f"/api/artifacts/{artifact_id}/comments/{mine}/resolve", headers=guest
    )
    assert resolved.status_code == 200
    assert resolved.json()["resolved"] is True
    assert resolved.json()["resolved_by"] == {"kind": "guest", "name": "Jana"}

    # Reopening their own thread works too.
    assert api.client.post(
        f"/api/artifacts/{artifact_id}/comments/{mine}/resolve",
        json={"resolved": False},
        headers=guest,
    ).status_code == 200

    # Somebody else's thread is not theirs to resolve or delete.
    assert api.client.post(
        f"/api/artifacts/{artifact_id}/comments/{theirs}/resolve", headers=guest
    ).status_code == 403
    assert api.client.delete(
        f"/api/artifacts/{artifact_id}/comments/{theirs}", headers=guest
    ).status_code == 403

    assert api.client.delete(
        f"/api/artifacts/{artifact_id}/comments/{mine}", headers=guest
    ).status_code == 200


def test_guest_cannot_use_the_management_api(api: Api) -> None:
    """An invitation is a voice in one discussion, not a credential.

    Every management route ignores the guest header outright, so a guest is
    simply an unauthenticated caller there — refused before anything happens.
    """
    artifact_id = _publish_markdown(api, "# Title\n\nBody text here.")
    guest = _guest_headers(_invite(api, artifact_id).json()["review_url"])

    refused = [
        api.client.get("/api/artifacts", headers=guest),
        api.client.post(
            f"/api/artifacts/{artifact_id}/versions",
            json={"markdown": "# Mine now"},
            headers=guest,
        ),
        api.client.put(
            f"/api/artifacts/{artifact_id}", json={"status": "final"}, headers=guest
        ),
        api.client.delete(f"/api/artifacts/{artifact_id}", headers=guest),
        api.client.get(f"/api/artifacts/{artifact_id}/invitations", headers=guest),
        api.client.post(f"/api/artifacts/{artifact_id}/rotate-link", headers=guest),
    ]
    for answer in refused:
        assert answer.status_code in (400, 401), answer.request.url

    # The artifact is untouched by any of it.
    listed = api.client.get("/api/artifacts", headers=AUTH_HEADERS).json()
    row = next(r for r in listed["artifacts"] if r["id"] == artifact_id)
    assert row["status"] == "draft"


def test_guest_comments_emit_a_webhook_naming_the_guest(api: Api, monkeypatch) -> None:
    # Registering "https://example.com/hook" goes through the real
    # validate_webhook_url -> DNS resolution path (this test does not use the
    # `hooked` fixture, which stubs that out for its own "hooks.test" host).
    # Injecting a public answer here keeps the whole suite runnable with no
    # network/public DNS available -- see the review's "tests must not depend
    # on public DNS" finding.
    monkeypatch.setattr(
        "src.webhooks._resolve_host_ips", lambda _hostname: ["93.184.216.34"]
    )
    artifact_id = _publish_markdown(api, "# Title\n\nBody text here.")
    assert _policy(
        api, artifact_id, webhooks=["https://example.com/hook"]
    ).status_code == 200
    guest = _guest_headers(_invite(api, artifact_id, "Jana").json()["review_url"])

    sent: list[Any] = []
    main.app.state.webhooks.emit = lambda urls, event: sent.append((urls, event))

    assert _guest_comment(api, artifact_id, guest).status_code == 201
    assert len(sent) == 1
    urls, event = sent[0]
    assert urls == ["https://example.com/hook"]
    assert event.kind == "comment.created"
    assert event.payload["actor"] == "Jana (guest)"
    # A notification is not a place to leak the credential either.
    assert guest["X-Artifact-Guest"] not in json.dumps(event.payload)


# --------------------------------------------------------------- /a/{x}/guest


def test_guest_endpoint_resolves_a_credential_to_a_name(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title")
    invitation = _invite(api, artifact_id, "Jana").json()
    guest = _guest_headers(invitation["review_url"])

    answer = api.client.get(f"/a/{artifact_id}/guest", headers=guest)
    assert answer.status_code == 200
    assert answer.json() == {
        "id": artifact_id,
        "invitation_id": invitation["invitation_id"],
        "name": "Jana",
    }


def test_guest_endpoint_refuses_anything_but_a_live_invitation(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title")
    invitation = _invite(api, artifact_id).json()
    guest = _guest_headers(invitation["review_url"])

    assert api.client.get(f"/a/{artifact_id}/guest").status_code == 401
    assert api.client.get(
        f"/a/{artifact_id}/guest", headers={"X-Artifact-Guest": "garbage"}
    ).status_code == 401
    assert api.client.get("/a/does-not-exist/guest", headers=guest).status_code == 404

    api.client.delete(
        f"/api/artifacts/{artifact_id}/invitations/{invitation['invitation_id']}",
        headers=AUTH_HEADERS,
    )
    assert api.client.get(f"/a/{artifact_id}/guest", headers=guest).status_code == 401


def test_guest_endpoint_follows_the_share_id_and_the_password_gate(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title", password="hunter2")
    invitation = _invite(api, artifact_id, "Jana").json()
    guest = _guest_headers(invitation["review_url"])
    share_id = invitation["review_url"].split("/a/", 1)[1].split("/", 1)[0]

    # An invitation grants a voice, not a way around the reader password.
    assert api.client.get(f"/a/{share_id}/guest", headers=guest).status_code == 401
    unlocked = {**guest, "X-Artifact-Password": "hunter2"}
    assert api.client.get(f"/a/{share_id}/guest", headers=unlocked).status_code == 200

    rotated = _rotate(api, artifact_id).json()["share_id"]
    assert api.client.get(f"/a/{rotated}/guest", headers=unlocked).status_code == 200
    assert api.client.get(f"/a/{share_id}/guest", headers=unlocked).status_code == 404


# --------------------------------------------------------------------------
# 0.7.0 — comment writes accept the share id as well as the internal id
# --------------------------------------------------------------------------


def test_comment_writes_accept_the_share_id_after_a_rotation(api: Api) -> None:
    """The review UI and every capability-URL holder only know the share id."""
    artifact_id = _publish_markdown(api, "# Title\n\nBody text here.")
    share_id = _rotate(api, artifact_id).json()["share_id"]
    assert share_id != artifact_id

    created = _comment(api, share_id, exact="Body text")
    assert created.status_code == 201, created.text
    thread_id = created.json()["id"]
    # The thread belongs to the artifact, under its internal id.
    assert created.json()["artifact_id"] == artifact_id

    assert api.client.post(
        f"/api/artifacts/{share_id}/comments/{thread_id}/replies",
        json={"body": "Answering."},
        headers=AUTH_HEADERS,
    ).status_code == 201
    assert api.client.post(
        f"/api/artifacts/{share_id}/comments/{thread_id}/resolve", headers=AUTH_HEADERS
    ).status_code == 200
    assert api.client.delete(
        f"/api/artifacts/{share_id}/comments/{thread_id}", headers=AUTH_HEADERS
    ).status_code == 200

    # The internal id keeps working throughout — this widens the door, not moves it.
    assert _comment(api, artifact_id, exact="Body text").status_code == 201


def test_a_rotated_away_share_id_cannot_be_commented_on(api: Api) -> None:
    """Widening the path to share ids must not weaken what rotation revokes."""
    artifact_id = _publish_markdown(api, "# Title\n\nBody text here.")
    stale = _rotate(api, artifact_id).json()["share_id"]
    live = _rotate(api, artifact_id).json()["share_id"]

    assert _comment(api, stale, exact="Body text").status_code == 404
    assert _comment(api, live, exact="Body text").status_code == 201


def test_guest_comments_through_the_share_id(api: Api) -> None:
    """Exactly the path the review page takes: share id plus guest header."""
    artifact_id = _publish_markdown(api, "# Title\n\nBody text here.")
    review_url = _invite(api, artifact_id, "Jana").json()["review_url"]
    share_id = review_url.split("/a/", 1)[1].split("/", 1)[0]
    guest = _guest_headers(review_url)

    created = _guest_comment(api, share_id, guest)
    assert created.status_code == 201, created.text
    assert created.json()["author"] == {"kind": "guest", "name": "Jana"}


# --------------------------------------------------------------------------
# Comment writes: the password gate, link rotation, and the guest KDF budget
#
# The four comment-write routes verified *who* is writing but not whether that
# writer may see the artifact at all. These tests pin the three consequences.
# --------------------------------------------------------------------------


def test_guest_comment_writes_need_the_reader_password(api: Api) -> None:
    """An invitation is a grant to comment, never a way past the password.

    GET /a/{id}/guest has always said exactly this; the write routes used to
    disagree, which handed anyone holding an invitation link the whole review
    surface — read and write — of a password-protected document.
    """
    artifact_id = _publish_markdown(
        api, "# Secret\n\nBody text here.", password="hunter2"
    )
    review_url = _invite(api, artifact_id, "Jana").json()["review_url"]
    share_id = review_url.split("/a/", 1)[1].split("/", 1)[0]
    guest = _guest_headers(review_url)
    unlocked = {**guest, "X-Artifact-Password": "hunter2"}

    locked = _guest_comment(api, share_id, guest)
    assert locked.status_code == 401
    assert locked.json()["error"] == "password required"

    created = _guest_comment(api, share_id, unlocked)
    assert created.status_code == 201, created.text
    thread_id = created.json()["id"]

    # ...and so is every later write on the thread the guest opened.
    replies = f"/api/artifacts/{share_id}/comments/{thread_id}/replies"
    assert api.client.post(
        replies, json={"body": "Still me."}, headers=guest
    ).status_code == 401
    assert api.client.post(
        replies, json={"body": "Still me."}, headers=unlocked
    ).status_code == 201

    resolve = f"/api/artifacts/{share_id}/comments/{thread_id}/resolve"
    assert api.client.post(resolve, headers=guest).status_code == 401
    assert api.client.post(resolve, headers=unlocked).status_code == 200

    thread = f"/api/artifacts/{share_id}/comments/{thread_id}"
    assert api.client.delete(thread, headers=guest).status_code == 401
    assert api.client.delete(thread, headers=unlocked).status_code == 200


def test_comment_password_gate_is_the_read_gate(api: Api) -> None:
    """The write gate is the read gate, verbatim — owner exemption included.

    Until 0.14.1 the read path exempted nobody, so neither did this one. Now a
    verified credential of the owning project reads a protected artifact
    without the password, and writing a comment follows the same rule; another
    project still needs the password, exactly as it does to read.
    """
    artifact_id = _publish_markdown(
        api, "# Secret\n\nBody text here.", password="hunter2"
    )
    assert _comment(api, artifact_id, exact="Body text").status_code == 201
    assert _comment(
        api, artifact_id, exact="Body text", headers=OTHER_AUTH_HEADERS
    ).status_code == 401

    unlocked = {**OTHER_AUTH_HEADERS, "X-Artifact-Password": "hunter2"}
    assert _comment(
        api, artifact_id, exact="Body text", headers=unlocked
    ).status_code == 201


def test_rotating_the_link_revokes_comment_writes_through_the_old_id(
    api: Api,
) -> None:
    """Rotation is revocation for writes too, not only for reads.

    A fresh artifact's share id *is* its internal id, so resolving the path by
    internal id first quietly kept the original public identifier alive for
    comment writes forever — and rotating the link after a review URL leaked
    revoked nothing.
    """
    artifact_id = _publish_markdown(api, "# Title\n\nBody text here.")
    review_url = _invite(api, artifact_id, "Jana").json()["review_url"]
    guest = _guest_headers(review_url)
    stale = review_url.split("/a/", 1)[1].split("/", 1)[0]
    assert stale == artifact_id, "the share id starts out equal to the internal id"

    fresh = _rotate(api, artifact_id).json()["share_id"]
    assert fresh != stale

    # The leaked link is dead, for the guest and for any other project holding it.
    assert _guest_comment(api, stale, guest).status_code == 404
    assert _comment(
        api, stale, exact="Body text", headers=OTHER_AUTH_HEADERS
    ).status_code == 404

    # The new share id is what everybody writes through now.
    assert _guest_comment(api, fresh, guest).status_code == 201
    assert _comment(
        api, fresh, exact="Body text", headers=OTHER_AUTH_HEADERS
    ).status_code == 201

    # The internal id survives for the artifact's own owner: API ergonomics,
    # with none of the public reach — an agent addresses what it published.
    assert _comment(api, artifact_id, exact="Body text").status_code == 201


def test_guest_credential_checks_are_throttled(api: Api, monkeypatch) -> None:
    """Verifying an invitation costs a full PBKDF2, so failures are budgeted.

    Without this, a known invitation id on the public GET /a/{id}/guest is a
    free CPU-exhaustion primitive: probes cost the hub 200k iterations each and
    the prober nothing, and they do not even spend a comment slot.
    """
    artifact_id = _publish_markdown(api, "# Title\n\nBody text here.")
    review_url = _invite(api, artifact_id, "Jana").json()["review_url"]
    guest = _guest_headers(review_url)
    invitation_id = guest["X-Artifact-Guest"].split(".", 1)[0]
    wrong = {"X-Artifact-Guest": f"{invitation_id}.not-the-secret"}
    _low_unlock_limit(api, monkeypatch, limit=2)

    assert api.client.get(f"/a/{artifact_id}/guest", headers=wrong).status_code == 401
    # Successful checks never count, so the invited guest keeps working.
    assert api.client.get(f"/a/{artifact_id}/guest", headers=guest).status_code == 200
    assert _guest_comment(api, artifact_id, guest).status_code == 201

    assert api.client.get(f"/a/{artifact_id}/guest", headers=wrong).status_code == 401
    blocked = api.client.get(f"/a/{artifact_id}/guest", headers=wrong)
    assert blocked.status_code == 429
    assert "invitation" in blocked.json()["detail"]

    # One budget covers every route that verifies a guest credential.
    assert _guest_comment(api, artifact_id, wrong).status_code == 429

    # SEC-100-003: a self-chosen X-Real-IP does not buy a fresh budget. The
    # header counts only from a peer inside HUB_TRUSTED_PROXY_CIDRS, which no
    # default deployment configures.
    assert api.client.get(
        f"/a/{artifact_id}/guest", headers={**wrong, "X-Real-IP": "10.0.0.9"}
    ).status_code == 429


# --------------------------------------------------------------------------
# 0.7.0 — visual diff
# --------------------------------------------------------------------------


def _two_versions(api: Api) -> str:
    artifact_id = _publish_markdown(api, "# Report\n\nFirst draft.\n")
    assert _submit_version(api, artifact_id, "# Report\n\nSecond draft.\n").status_code == 201
    return artifact_id


def test_visual_diff_renders_two_sandboxed_frames(api: Api) -> None:
    artifact_id = _two_versions(api)
    resp = api.client.get(f"/a/{artifact_id}/diff/1..2?format=visual")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    page = resp.text

    # Two panes, each in its own opaque origin: no allow-same-origin anywhere.
    assert page.count('sandbox="allow-scripts allow-popups"') == 2
    assert page.count("<iframe") == 2
    assert "allow-same-origin" not in page
    assert 'id="vd-older"' in page and 'id="vd-newer"' in page

    # Version labels and the diff statistics from the ordinary diff.
    assert "v1 &rarr; v2" in page or "v1 → v2" in page
    assert "Visual diff" in page
    assert "First draft" in page and "Second draft" in page
    assert "ahd-scroll" in page


def test_visual_diff_counts_agree_with_the_json_diff(api: Api) -> None:
    artifact_id = _two_versions(api)
    stats = api.client.get(f"/a/{artifact_id}/diff/1..2?format=json").json()["stats"]
    page = api.client.get(f"/a/{artifact_id}/diff/1..2?format=visual").text
    assert f'+{stats["added"]}' in page
    assert f'-{stats["removed"]}' in page


def test_visual_diff_refuses_an_oversized_side(api: Api, monkeypatch) -> None:
    artifact_id = _two_versions(api)
    monkeypatch.setattr(
        main, "settings", dataclasses.replace(api.settings, diff_max_bytes=64)
    )
    resp = api.client.get(f"/a/{artifact_id}/diff/1..2?format=visual")
    assert resp.status_code == 413
    assert "too large" in resp.json()["error"]


def test_visual_diff_hides_a_proposal_from_strangers(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Report\n\nFirst.\n", accept_versions=True)
    proposed = _submit_version(
        api, artifact_id, "# Report\n\nProposed.\n", headers=OTHER_AUTH_HEADERS
    )
    assert proposed.status_code == 201
    number = proposed.json()["version"]

    anonymous = api.client.get(f"/a/{artifact_id}/diff/1..{number}?format=visual")
    assert anonymous.status_code == 403

    # The owner and the proposal's author may both see it.
    for headers in (AUTH_HEADERS, OTHER_AUTH_HEADERS):
        allowed = api.client.get(
            f"/a/{artifact_id}/diff/1..{number}?format=visual", headers=headers
        )
        assert allowed.status_code == 200, headers
        assert "Proposed." in allowed.text


def test_visual_diff_follows_the_share_id_and_rejects_unknown_formats(api: Api) -> None:
    artifact_id = _two_versions(api)
    share_id = _rotate(api, artifact_id).json()["share_id"]
    assert api.client.get(f"/a/{share_id}/diff/1..2?format=visual").status_code == 200
    assert api.client.get(f"/a/{artifact_id}/diff/1..2?format=visual").status_code == 404
    assert api.client.get(f"/a/{share_id}/diff/1..2?format=nope").status_code == 400


# --------------------------------------------------------------------------
# 0.7.0 — the changelog is a page of this service, not a published artifact
# --------------------------------------------------------------------------


def test_changelog_uses_the_shell_design_system(api: Api) -> None:
    resp = api.client.get("/changelog")
    assert resp.status_code == 200
    page = resp.text

    # Shell chrome: the graph-paper grid, the mono/prose font pair and the
    # footer every other service page carries.
    assert 'class="changelog"' in page
    assert "--font-mono" in page
    assert "<footer>" in page
    assert "/changelog.md" in page

    # ...and none of the standalone artifact template.
    assert "{{ARTIFACT_BODY}}" not in page
    assert "hljs" not in page
    assert "mermaid" not in page


def test_changelog_titles_itself_from_the_file(api: Api) -> None:
    """The document's own h1 becomes the hero — it is never rendered twice."""
    resp = api.client.get("/changelog")
    assert resp.text.count("<h1") == 1
    assert "Changelog" in resp.text
    # Release headings still render as content.
    assert "<h2" in resp.text


# --------------------------------------------------------------------------
# 0.7.0 — the admin studio grew the new controls
# --------------------------------------------------------------------------


def test_admin_page_wires_up_the_new_controls(api: Api) -> None:
    page = api.client.get("/admin").text

    # Every new endpoint the studio drives.
    for path in (
        '"/stats"',
        '"/rotate-link"',
        '"/purge"',
        '"/restore"',
        '"/invitations"',
    ):
        assert path in page, path

    # ...and the affordances that drive them.
    for hook in (
        "Rotate link",
        "Trash",
        "Restore",
        "Purge",
        "Type PURGE to confirm",
        "webhooks",
        "Invite",
        "Revoke",
        "Load stats",
        "shown once",
        "note-modal",
        "trashed \\u00b7 link dead",
    ):
        assert hook in page, hook


def test_review_page_supports_guest_mode(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title")
    page = api.client.get(f"/a/{artifact_id}/review").text

    assert "#invite=" in page or "invite=" in page
    assert "X-Artifact-Guest" in page
    assert "rv-guest" in page
    assert "Commenting as " in page
    # The credential is read from the fragment and cleared from the address bar;
    # it must never be appended to a URL.
    assert "location.hash" in page
    assert "replaceState" in page
    assert '"/guest"' in page


def test_context_documents_guests_and_the_visual_diff(api: Api) -> None:
    body = api.client.get("/context").json()
    paths = {(e["method"], e["path"]) for e in body["endpoints"]}
    assert ("POST", "/api/artifacts/{id}/invitations") in paths
    assert ("GET", "/a/{id}/guest") in paths

    assert "X-Artifact-Guest" in body["auth"]["guest_header"]
    assert "fragment" in body["guests"]["secret"]
    assert "invitations/{iid}" in body["guests"]["revocation"]
    assert "visual" in body["versioning"]["diff"]
    assert body["limits"]["max_invitations_per_artifact"] == (
        api.settings.max_invitations_per_artifact
    )
    assert "artifact.trashed" in body["webhooks"]["events"]


class TestDemoLink:
    """The landing page links a showcase artifact only when one is configured."""

    def test_absent_when_not_configured(self, api: Api) -> None:
        body = api.client.get("/").text
        assert "See the demo" not in body

    def test_rendered_when_configured(self, api: Api, monkeypatch) -> None:
        monkeypatch.setattr(
            main,
            "settings",
            dataclasses.replace(api.settings, demo_url="https://hub.example/a/demo"),
        )
        body = api.client.get("/").text
        assert "See the demo" in body
        assert "https://hub.example/a/demo" in body


# --------------------------------------------------------------------------
# Live updating
#
# GET /a/{id}/live is the change-detection endpoint every reader-facing page
# polls; the pages themselves are asserted through the HTML they ship, since
# their behaviour lives in JavaScript this suite does not execute.
# --------------------------------------------------------------------------


_LIVE_KEYS = {
    "id",
    "head_version",
    "updated_at",
    "versions_count",
    "proposed_count",
    "comment_threads",
    "document_status",
    "contributions_frozen",
}


def _live(api: Api, share_id: str, etag: str | None = None):
    headers = {"If-None-Match": etag} if etag else {}
    return api.client.get(f"/a/{share_id}/live", headers=headers)


def test_live_returns_the_snapshot_shape(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title\n\nBody text")
    resp = _live(api, artifact_id)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == _LIVE_KEYS
    assert body["id"] == artifact_id
    assert body["head_version"] == 1
    assert body["versions_count"] == 1
    assert body["proposed_count"] == 0
    assert body["comment_threads"] == 0
    assert body["document_status"] == "draft"
    assert body["contributions_frozen"] is False
    assert body["updated_at"]
    # A change *signal*, not a payload: no content and no owner identity.
    _assert_no_sensitive_keys(body)
    assert "html" not in body


def test_live_caching_headers_and_conditional_request(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title\n\nBody text")
    first = _live(api, artifact_id)
    etag = first.headers["etag"]
    # Strong (no W/ prefix) and quoted, so an intermediary cannot weaken it.
    assert etag.startswith('"') and etag.endswith('"')
    # no-store survives the /a/ middleware, which otherwise sets no-cache.
    assert first.headers["cache-control"] == "no-store"

    again = _live(api, artifact_id, etag)
    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == etag
    assert again.headers["cache-control"] == "no-store"

    # The weak-prefixed and list forms an intermediary may send still match.
    assert _live(api, artifact_id, f"W/{etag}").status_code == 304
    assert _live(api, artifact_id, f'"other", {etag}').status_code == 304
    assert _live(api, artifact_id, '"stale"').status_code == 200


def test_live_etag_changes_when_a_new_version_is_published(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title\n\nBody text")
    before = _live(api, artifact_id)
    assert api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"markdown": "# Title\n\nSecond"},
        headers=AUTH_HEADERS,
    ).status_code == 200

    after = _live(api, artifact_id, before.headers["etag"])
    assert after.status_code == 200
    assert after.headers["etag"] != before.headers["etag"]
    assert after.json()["head_version"] == 2
    assert after.json()["versions_count"] == 2


def test_live_etag_changes_when_a_version_is_proposed(api: Api) -> None:
    artifact_id = _publish_markdown(
        api, "# Title\n\nBody text", accept_versions=True
    )
    before = _live(api, artifact_id)
    assert _submit_version(
        api, artifact_id, "# Title\n\nProposal", headers=OTHER_AUTH_HEADERS
    ).status_code == 201

    after = _live(api, artifact_id, before.headers["etag"])
    assert after.status_code == 200
    assert after.json()["proposed_count"] == 1
    # The head has not moved: a proposal is not a swap, only a signal.
    assert after.json()["head_version"] == 1


def test_live_etag_changes_when_a_comment_is_added(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title\n\nBody text")
    before = _live(api, artifact_id)
    assert _comment(api, artifact_id).status_code == 201

    after = _live(api, artifact_id, before.headers["etag"])
    assert after.status_code == 200
    assert after.headers["etag"] != before.headers["etag"]
    assert after.json()["comment_threads"] == 1


def test_live_etag_changes_when_the_document_is_finalised(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title\n\nBody text")
    before = _live(api, artifact_id)
    assert _policy(api, artifact_id, status="final").status_code == 200

    after = _live(api, artifact_id, before.headers["etag"])
    assert after.status_code == 200
    assert after.headers["etag"] != before.headers["etag"]
    assert after.json()["document_status"] == "final"
    assert after.json()["contributions_frozen"] is True


def test_live_is_readable_while_the_artifact_is_protected(api: Api) -> None:
    """Same rule as /a/{id}/meta: metadata stays public behind the password."""
    artifact_id = _publish_markdown(api, "# Secret", password="hunter2")
    assert api.client.get(f"/a/{artifact_id}/raw").status_code == 401
    assert api.client.get(f"/a/{artifact_id}/meta").status_code == 200

    resp = _live(api, artifact_id)
    assert resp.status_code == 200
    assert resp.json()["head_version"] == 1
    # ...and it still carries nothing the password is protecting.
    assert set(resp.json()) == _LIVE_KEYS


def test_live_resolves_by_share_id_and_404s_for_a_rotated_away_id(
    api: Api,
) -> None:
    artifact_id = _publish_markdown(api, "# Title\n\nBody text")
    assert _live(api, artifact_id).status_code == 200

    share_id = _rotate(api, artifact_id).json()["share_id"]
    assert share_id != artifact_id
    assert _live(api, share_id).status_code == 200
    assert _live(api, share_id).json()["id"] == share_id
    # The old link is dead, which is the signal a polling client stops on.
    assert _live(api, artifact_id).status_code == 404
    assert _live(api, "no-such-artifact").status_code == 404


def test_live_is_documented_in_context_and_openapi(api: Api) -> None:
    entry = {
        e["path"]: e for e in api.client.get("/context").json()["endpoints"]
    }["/a/{id}/live"]
    assert entry["method"] == "GET"
    assert entry["auth"] == "url capability"
    assert "ETag" in entry["purpose"]

    operation = api.client.get("/openapi.json").json()["paths"][
        "/a/{artifact_id}/live"
    ]["get"]
    assert operation["tags"] == ["public"]
    assert operation["summary"].strip()
    assert "If-None-Match" in operation["description"]
    # 422 is FastAPI's own validation response; the four documented here are
    # the ones a polling client actually branches on.
    assert {"200", "304", "404", "502"} <= set(operation["responses"])
    for response in operation["responses"].values():
        assert response["description"].strip()


# ---- the three surfaces ---------------------------------------------------


def test_artifact_wrapper_ships_the_poller_and_the_scroll_reporter(
    api: Api,
) -> None:
    artifact_id = _publish_markdown(api, "# Title\n\nBody text")
    page = api.client.get(f"/a/{artifact_id}").text

    # The shared helper, not a per-page copy of it.
    assert "window.AHLive = (function" in page
    assert "AHLive.watch(" in page
    assert f'window.AH_ID = "{artifact_id}";' in page
    # The banner, hidden until there is something to say.
    assert 'id="ah-live"' in page
    assert 'id="ah-live-go"' in page
    assert 'class="ahlive" id="ah-live" hidden' in page
    # The scroll reporter is injected into the document itself, since the
    # sandboxed frame cannot be read from the outside.
    assert 'id="ah-reporter"' in page
    assert "ah-scroll" in page
    raw = api.client.get(f"/a/{artifact_id}/raw").text
    embedded = pages._inject_before_body_end(raw, pages._SRCDOC_JS)
    assert html.escape(embedded, quote=True) in page
    # ...and it buys the frame no new capability.
    assert "allow-same-origin" not in page


def test_head_page_auto_swaps_but_a_pinned_version_does_not(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title\n\nBody text")
    assert api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"markdown": "# Title\n\nSecond"},
        headers=AUTH_HEADERS,
    ).status_code == 200

    head = api.client.get(f"/a/{artifact_id}").text
    assert "window.AH_PINNED = null;" in head

    pinned = api.client.get(f"/a/{artifact_id}/v/1").text
    # A reader on /v/{n} asked for that version: the shell knows which one,
    # and its banner points at the head instead of swapping.
    assert "window.AH_PINNED = 1;" in pinned
    assert "Open the latest" in pinned
    assert "window.AHLive = (function" in pinned


def test_review_page_ships_the_poller_and_its_banner(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title\n\nBody text")
    page = api.client.get(f"/a/{artifact_id}/review").text

    assert "window.AHLive = (function" in page
    assert "AHLive.watch(" in page
    assert 'id="rv-live"' in page
    assert 'id="rv-live-go"' in page
    # The scroll reporter travels into the document beside the annotation
    # layer, so the shell knows whether the reviewer has scrolled.
    assert 'id="rv-scroll"' in page
    assert "ah-scroll" in page
    # Drafts live outside the DOM, so a live refresh cannot eat a half-typed
    # reply.
    assert "var drafts = {};" in page
    assert "function safeToSwap()" in page


def test_admin_studio_polls_only_expanded_panels(api: Api) -> None:
    page = api.client.get("/admin").text
    assert "window.AHLive = (function" in page
    assert "AHLive.watch(" in page
    # Collapsing a row, re-rendering the list or leaving the studio all stop
    # the watches they own.
    assert "function stopWatchers()" in page
    assert "function stopWatch()" in page


def test_every_live_surface_shares_one_copy_of_the_poller(api: Api) -> None:
    """The helper is one module-level constant, not three pasted copies."""
    artifact_id = _publish_markdown(api, "# Title\n\nBody text")
    marker = "window.AHLive = (function"
    for path in (f"/a/{artifact_id}", f"/a/{artifact_id}/review", "/admin"):
        body = api.client.get(path).text
        assert body.count(marker) == 1, path
        assert pages._LIVE_JS in body, path


# --------------------------------------------------------------------------
# Check-then-act races between concurrent requests on one artifact
# --------------------------------------------------------------------------


def _pause_inside(target, name: str):
    """Make the first call of ``target.<name>`` block until told to go on.

    Returns ``(entered, release)`` events. The first caller sets ``entered``
    just before the real method would run and waits for ``release``; every
    later call goes straight through. This freezes one request *between* its
    check and its act, which is exactly where a second request must not be
    allowed to slip in.
    """
    import threading

    entered, release = threading.Event(), threading.Event()
    real = getattr(target, name)
    first = threading.Lock()
    armed = [True]

    def paused(*args, **kwargs):
        with first:
            fire = armed[0]
            armed[0] = False
        if fire:
            entered.set()
            release.wait(timeout=5)
        return real(*args, **kwargs)

    setattr(target, name, paused)
    return entered, release


def _in_thread(fn):
    """Run ``fn`` in a thread; returns (thread, box) where box[0] is the result."""
    import threading

    box: list = []
    thread = threading.Thread(target=lambda: box.append(fn()))
    thread.start()
    return thread, box


class TestConcurrentMutationsAreSerializedPerArtifact:
    """Every mutating route reads, decides, then writes -- across separate
    locks. Two requests on one artifact can interleave between the decision
    and the write, so both act on a state that is no longer true. These
    tests freeze one request at that seam and let the other run.
    """

    def test_two_updates_do_not_lose_each_others_fields(self, api: Api) -> None:
        """REL-075-005: whole-object rewrites overwrite unrelated newer fields."""
        artifact_id = _publish_markdown(api, "# One")
        store = main.app.state.store
        entered, release = _pause_inside(store, "save_meta")

        first, box1 = _in_thread(
            lambda: _policy(api, artifact_id, password="hunter2").status_code
        )
        assert entered.wait(timeout=5)
        second, box2 = _in_thread(
            lambda: _policy(api, artifact_id, accept_versions_mode="anyone").status_code
        )
        # Give the second request its chance to slip in before releasing.
        second.join(timeout=0.5)
        release.set()
        first.join(timeout=5)
        second.join(timeout=5)
        assert box1 == [200] and box2 == [200], (box1, box2)

        meta = store.get_meta(artifact_id)
        assert meta.password is not None, "the password update was lost"
        assert meta.accept_versions_mode == "anyone", "the accept-mode update was lost"

    def test_two_promotions_of_one_proposal_act_once(self, hooked) -> None:
        """COR-075-007: both promotions observe 'proposed' and both fire."""
        api, posts, dispatcher = hooked
        artifact_id = _publish_markdown(api, "# One", accept_versions=True)
        assert _register_hook(api, artifact_id, [HOOK]).status_code == 200
        assert _submit_version(api, artifact_id, "# Two", headers=OTHER_AUTH_HEADERS).status_code == 201
        dispatcher.drain()
        posts.clear()
        store = main.app.state.store
        entered, release = _pause_inside(store, "set_status")

        def promote():
            return api.client.post(
                f"/api/artifacts/{artifact_id}/versions/2/promote", headers=AUTH_HEADERS
            ).status_code

        first, box1 = _in_thread(promote)
        assert entered.wait(timeout=5)
        second, box2 = _in_thread(promote)
        second.join(timeout=0.5)
        release.set()
        first.join(timeout=5)
        second.join(timeout=5)

        assert sorted(box1 + box2) == [200, 409], (box1, box2)
        dispatcher.drain()
        assert len(posts) == 1, "the promotion side effect fired twice"

    def test_concurrent_deletes_cannot_remove_the_last_live_version(self, api: Api) -> None:
        """COR-075-008: each delete sees the *other* version as still live."""
        artifact_id = _publish_markdown(api, "# One")
        assert _submit_version(api, artifact_id, "# Two").status_code == 201
        store = main.app.state.store
        # Past the store's own last-live check, before the file goes.
        entered, release = _pause_inside(store, "_delete_file_confirmed")

        def delete(version: int):
            return api.client.delete(
                f"/api/artifacts/{artifact_id}/versions/{version}", headers=AUTH_HEADERS
            ).status_code

        first, box1 = _in_thread(lambda: delete(1))
        assert entered.wait(timeout=5)
        second, box2 = _in_thread(lambda: delete(2))
        second.join(timeout=0.5)
        release.set()
        first.join(timeout=5)
        second.join(timeout=5)

        live = [
            row["version"] for row in store.list_versions(artifact_id)
            if row.get("status") == "live"
        ]
        assert live, f"both deletes succeeded ({box1}, {box2}); no live version is left"
        assert 200 in box1 + box2 and 409 in box1 + box2, (box1, box2)

    def test_finalizing_waits_for_an_in_flight_submission(self, api: Api) -> None:
        """COR-075-002: the policy check and the upload are not one step."""
        artifact_id = _publish_markdown(api, "# One", accept_versions=True)
        store = main.app.state.store
        # After the frozen/contributor checks passed, before the version lands.
        entered, release = _pause_inside(store, "add_version_next")

        submit, box_submit = _in_thread(
            lambda: _submit_version(api, artifact_id, "# Two", headers=OTHER_AUTH_HEADERS).status_code
        )
        assert entered.wait(timeout=5)
        finalize, box_final = _in_thread(
            lambda: _policy(api, artifact_id, status="final").status_code
        )
        # The finalize must not complete while a submission it has to be
        # ordered against is between its check and its write.
        finalize.join(timeout=0.5)
        assert box_final == [], "the document was finalized under an in-flight submission"
        release.set()
        submit.join(timeout=5)
        finalize.join(timeout=5)
        assert box_submit == [201] and box_final == [200], (box_submit, box_final)

    def test_a_submission_cannot_land_in_an_artifact_being_purged(self, api: Api) -> None:
        """COR-075-009: nothing fences writes against an in-progress purge."""
        artifact_id = _publish_markdown(api, "# One", accept_versions=True)
        store = main.app.state.store
        entered, release = _pause_inside(store, "delete")

        purge, box_purge = _in_thread(
            lambda: api.client.delete(f"/api/artifacts/{artifact_id}/purge", headers=AUTH_HEADERS).status_code
        )
        assert entered.wait(timeout=5)
        submit, box_submit = _in_thread(
            lambda: _submit_version(api, artifact_id, "# Two", headers=OTHER_AUTH_HEADERS).status_code
        )
        submit.join(timeout=0.5)
        release.set()
        purge.join(timeout=5)
        submit.join(timeout=5)

        assert box_purge == [200], box_purge
        assert box_submit == [404], f"a version landed in a purged artifact: {box_submit}"
        assert api.backend.search_by_tag(f"artifact-id-{artifact_id}") == [], (
            "the purge left a version file behind"
        )

    def test_pinning_and_deleting_the_same_version_stay_consistent(self, api: Api) -> None:
        """COR-075-006: the head can be pinned to a version being deleted."""
        artifact_id = _publish_markdown(api, "# One")
        assert _submit_version(api, artifact_id, "# Two").status_code == 201
        store = main.app.state.store
        entered, release = _pause_inside(store, "_delete_file_confirmed")

        delete, box_delete = _in_thread(
            lambda: api.client.delete(f"/api/artifacts/{artifact_id}/versions/2", headers=AUTH_HEADERS).status_code
        )
        assert entered.wait(timeout=5)
        pin, box_pin = _in_thread(
            lambda: api.client.put(
                f"/api/artifacts/{artifact_id}/head",
                json={"mode": "pinned", "version": 2},
                headers=AUTH_HEADERS,
            ).status_code
        )
        pin.join(timeout=0.5)
        release.set()
        delete.join(timeout=5)
        pin.join(timeout=5)

        meta = store.get_meta(artifact_id)
        existing = {row["version"] for row in store.list_versions(artifact_id)}
        if meta.head_mode == "pinned":
            assert meta.head_version in existing, (
                f"head pinned to deleted version {meta.head_version}; {box_delete}, {box_pin}"
            )


def test_a_deleted_newest_version_number_is_never_reused(api: Api) -> None:
    """COR-075-004: the next number is max(existing)+1, so deleting the newest
    hands its number to different content -- and every link, comment anchor
    and diff that named it now points at something else."""
    artifact_id = _publish_markdown(api, "# One")
    assert _submit_version(api, artifact_id, "# Two").json()["version"] == 2
    assert api.client.delete(f"/api/artifacts/{artifact_id}/versions/2", headers=AUTH_HEADERS).status_code == 200
    assert _submit_version(api, artifact_id, "# Three").json()["version"] == 3

    # ...and still not after a restart with an empty index.
    fresh = ArtifactStore(
        backend=api.backend,
        cache_dir=api.settings.cache_dir / "fresh",
        cache_max_entries=api.settings.cache_max_entries,
        max_versions=api.settings.max_versions,
    )
    fresh.hydrate()
    assert api.client.delete(f"/api/artifacts/{artifact_id}/versions/3", headers=AUTH_HEADERS).status_code == 200
    fresh.hydrate()
    assert fresh.next_version(artifact_id) == 4


# --------------------------------------------------------------------------
# Write ordering: settings last, and no orphan in the caller's project
# --------------------------------------------------------------------------


def _break_version_writes(monkeypatch, method: str) -> None:
    def boom(_envelope):
        raise BackendError("version write failed")
    monkeypatch.setattr(main.app.state.store, method, boom)


def test_a_failed_content_update_does_not_apply_the_settings(api: Api, monkeypatch) -> None:
    """REL-075-004: the password landed while the caller was told the update failed."""
    artifact_id = _publish_markdown(api, "# One")
    _break_version_writes(monkeypatch, "add_version_next")

    resp = _policy(api, artifact_id, password="hunter2", markdown="# Two")
    assert resp.status_code == 502, resp.text

    meta = main.app.state.store.get_meta(artifact_id)
    assert meta.password is None, "a password was applied by an update that failed"
    assert api.client.get(f"/a/{artifact_id}").status_code == 200


def test_a_failed_content_update_discards_its_canonical_copy(api: Api, monkeypatch) -> None:
    """REL-075-008: the copy in the author's project outlived the version it was for."""
    artifact_id = _publish_markdown(api, "# One")
    _break_version_writes(monkeypatch, "add_version_next")
    before = len([c for c in api.canonical_calls if "file_id" in c])

    assert _policy(api, artifact_id, markdown="# Two").status_code == 502

    uploads = [c["file_id"] for c in api.canonical_calls if "file_id" in c]
    deletes = [c["deleted"] for c in api.canonical_calls if "deleted" in c]
    assert len(uploads) == before + 1
    assert uploads[-1] in deletes, "the orphaned canonical copy was not discarded"


def test_a_failed_version_write_on_publish_leaves_nothing_behind(api: Api, monkeypatch) -> None:
    """REL-075-008 on publish: the canonical copy and the meta record both go."""
    monkeypatch.setattr(main, "new_artifact_id", lambda: "doomed-publish-id")
    _break_version_writes(monkeypatch, "add_version")

    resp = api.client.post("/api/artifacts", json={"markdown": "# Doomed"}, headers=AUTH_HEADERS)
    assert resp.status_code == 502, resp.text

    assert main.app.state.store.get_meta("doomed-publish-id") is None
    assert api.backend.search_by_tag("artifact-id-doomed-publish-id") == []
    uploads = [c["file_id"] for c in api.canonical_calls if "file_id" in c]
    deletes = [c["deleted"] for c in api.canonical_calls if "deleted" in c]
    assert uploads and uploads[-1] in deletes


def _code_lines(text: str) -> list[str]:
    """Lines inside fenced code blocks only -- prose may name an anti-pattern."""
    inside, out = False, []
    for line in text.splitlines():
        if line.strip().startswith("```"):
            inside = not inside
            continue
        if inside:
            out.append(line)
    return out


def test_no_documented_curl_example_puts_the_token_in_argv(api: Api) -> None:
    """SEC-075-004: every example passed the token as a curl argument.

    The shell expands -H "X-StorageApi-Token: $KBC_TOKEN" before curl runs,
    so for that process's lifetime the token is visible to anyone who can
    list processes on the host. The documented pattern is a shell function
    that feeds curl a config from a process substitution instead.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    served = {
        "/agent": api.client.get("/agent").text,
        "/skill": api.client.get("/skill").text,
        "README.md": (root / "README.md").read_text(encoding="utf-8"),
    }
    for name, text in served.items():
        code = _code_lines(text)
        offenders = [l for l in code if re.search(r"""-H\s+["']X-StorageApi-Token""", l)]
        assert offenders == [], (name, offenders)
        assert any(l.startswith("hub()") for l in code), f"{name} does not define hub()"


class TestCommentRoutesShareTheArtifactLock:
    """Comment routes address the document by its *public* share id.

    A lock keyed on that path parameter would not serialize them with the
    owner routes, which use the internal id -- so a comment could land on a
    document being finalized, or be resolved on one being trashed. The key
    has to be the internal id, resolved before the lock is taken. The link is
    rotated first so the two ids actually differ.
    """

    @staticmethod
    def _rotated(api: Api) -> tuple[str, str]:
        artifact_id = _publish_markdown(api, "# One")
        assert api.client.post(
            f"/api/artifacts/{artifact_id}/rotate-link", headers=AUTH_HEADERS
        ).status_code == 200
        share_id = main.app.state.store.get_meta(artifact_id).share_id
        assert share_id != artifact_id
        return artifact_id, share_id

    def test_finalizing_waits_for_a_comment_written_via_the_share_id(self, api: Api) -> None:
        artifact_id, share_id = self._rotated(api)
        entered, release = _pause_inside(main.app.state.comments, "create")

        comment, box_comment = _in_thread(
            lambda: _comment(api, share_id, exact="One").status_code
        )
        assert entered.wait(timeout=5)
        finalize, box_final = _in_thread(
            lambda: _policy(api, artifact_id, status="final").status_code
        )
        finalize.join(timeout=0.5)
        assert box_final == [], "the document was finalized under an in-flight comment"
        release.set()
        comment.join(timeout=5)
        finalize.join(timeout=5)
        assert box_comment == [201] and box_final == [200], (box_comment, box_final)

    def test_trashing_waits_for_a_resolve_written_via_the_share_id(self, api: Api) -> None:
        artifact_id, share_id = self._rotated(api)
        thread_id = _comment(api, share_id, exact="One").json()["id"]
        entered, release = _pause_inside(main.app.state.comments, "update")

        resolve, box_resolve = _in_thread(
            lambda: api.client.post(
                f"/api/artifacts/{share_id}/comments/{thread_id}/resolve",
                json={"resolved": True},
                headers=AUTH_HEADERS,
            ).status_code
        )
        assert entered.wait(timeout=5)
        trash, box_trash = _in_thread(lambda: _trash(api, artifact_id).status_code)
        trash.join(timeout=0.5)
        assert box_trash == [], "the document was trashed under an in-flight resolve"
        release.set()
        resolve.join(timeout=5)
        trash.join(timeout=5)
        assert box_resolve == [200] and box_trash == [200], (box_resolve, box_trash)


# --------------------------------------------------------------------------
# SEC-075-003: verifiable agent instructions
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path,filename", [("/agent", "AGENT.md"), ("/skill", "SKILL.md")])
def test_served_documents_carry_their_digest_and_version(api: Api, path: str, filename: str) -> None:
    """A hub says what it serves, so an installer can spot drift before trusting it."""
    resp = api.client.get(path)
    assert resp.status_code == 200
    digest = hashlib.sha256(resp.content).hexdigest()
    assert resp.headers["x-content-sha256"] == digest
    assert resp.headers["etag"] == f'"sha256:{digest}"'
    assert resp.headers["x-hub-version"] == main.SERVICE_VERSION

    unchanged = api.client.get(path, headers={"If-None-Match": resp.headers["etag"]})
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert unchanged.headers["etag"] == resp.headers["etag"]


def test_context_lists_the_documents_with_digests_and_release_assets(api: Api) -> None:
    body = api.client.get("/context").json()
    docs = body["documents"]
    for key, path, filename in (("agent", "/agent", "AGENT.md"), ("skill", "/skill", "SKILL.md")):
        served = api.client.get(path)
        entry = docs[key]
        assert entry["sha256"] == hashlib.sha256(served.content).hexdigest()
        assert entry["bytes"] == len(served.content)
        assert entry["version"] == main.SERVICE_VERSION
        assert entry["url"] == f"https://testserver{path}"
        assert entry["release_asset"] == (
            f"{main.GITHUB_REPO_URL}/releases/download/v{main.SERVICE_VERSION}/{filename}"
        )
    assert docs["sums"] == f"{main.GITHUB_REPO_URL}/releases/download/v{main.SERVICE_VERSION}/SHA256SUMS"


def test_install_instructions_verify_a_release_not_the_live_endpoint(api: Api) -> None:
    """The documented install must come from the signed release, not from /agent.

    The live endpoint is the right thing to *read* -- it describes the hub you
    are talking to -- and the wrong thing to *install*: a file that grants
    Bash/Read/WebFetch authority must be the reviewed, attested release copy.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    for name, text in (
        ("README.md", (root / "README.md").read_text(encoding="utf-8")),
        ("/skill", api.client.get("/skill").text),
    ):
        code = _code_lines(text)
        installs_live = [l for l in code if "$HUB/agent" in l and " -o " in l]
        assert installs_live == [], (name, installs_live)
        assert any("gh release download" in l for l in code), name
        assert any("gh attestation verify" in l for l in code), name


def test_release_workflow_gates_tests_and_attests_the_documents() -> None:
    import yaml

    root = pathlib.Path(__file__).resolve().parent.parent
    wf = yaml.safe_load((root / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))
    triggers = wf.get("on") or wf.get(True)  # PyYAML reads a bare `on:` key as True
    assert triggers["push"]["tags"] == ["v*"]

    (job,) = wf["jobs"].values()
    perms = job["permissions"]
    assert perms["id-token"] == "write" and perms["attestations"] == "write"
    assert perms["contents"] == "write"
    steps = job["steps"]
    uses = [s.get("uses", "") for s in steps]
    runs = "\n".join(s.get("run", "") for s in steps)
    assert any(u.startswith("actions/attest-build-provenance@") for u in uses)
    assert "uv run pytest tests/" in runs, "the release must be gated on the suite"
    assert "SHA256SUMS" in runs
    assert "gh release" in runs


# --------------------------------------------------------------------------
# Interactive sign-in
# --------------------------------------------------------------------------

#: A credential shaped exactly like the ones `/login` hands the browser.
SESSION_HEADERS = {
    "Authorization": "Bearer kbc_at_session_secret",
    "X-Storage-Stack": "us",
    "X-Storage-Project": "123",
}

_STACK = "https://connection.keboola.com"
_CLI_TOKEN_BODY = {
    "accessToken": "kbc_at_session_secret",
    "refreshToken": "kbc_rt_session_secret",
    "tokenType": "Bearer",
    "expiresIn": 3600,
    "sessionId": "sid",
    "user": {"id": 42, "email": "someone@keboola.com", "name": "Someone"},
}


def _pending_body(error: str, interval: int = 5) -> dict:
    return {
        "error": "cliAuth",
        "exceptionId": "x",
        "code": "y",
        "uuid": "z",
        "params": {"error": error, "interval": interval},
        "exceptionDetailUrl": "https://example.com",
    }


def _introspect_body(projects: list[dict]) -> dict:
    return {
        "sessionId": "sid",
        "user": {"id": 42, "email": "someone@keboola.com", "name": "Someone"},
        "grantType": "device_code",
        "sudoVerified": False,
        "createdAt": "2026-09-01T00:00:00+00:00",
        "expiresAt": "2026-09-01T01:00:00+00:00",
        "projects": projects,
    }


def test_a_signed_in_session_publishes_as_the_project_it_names(api: Api) -> None:
    """concept-auth: a Keboola sign-in is one more way to be a project.

    Everything downstream — ownership, the canonical copy, the listing — has
    to behave exactly as it does for a pasted Storage token, which is the
    whole point of resolving both credential shapes into one Owner.
    """
    resp = api.client.post(
        "/api/artifacts", json={"markdown": "# From a session"},
        headers=SESSION_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    artifact_id = resp.json()["id"]

    listed = api.client.get("/api/artifacts", headers=SESSION_HEADERS)
    assert [row["id"] for row in listed.json()["artifacts"]] == [artifact_id]

    # And the same project reaches it through a pasted Storage token, because
    # the identity recorded is the project, not the way it signed in.
    assert api.client.get("/api/artifacts", headers=AUTH_HEADERS).json()[
        "artifacts"
    ][0]["id"] == artifact_id

    assert api.canonical_calls[-1]["project_id"] == 123
    assert api.canonical_calls[-1]["token"] == "kbc_at_session_secret"


def test_a_session_without_a_project_is_an_incomplete_credential(api: Api) -> None:
    resp = api.client.get(
        "/api/artifacts",
        headers={
            "Authorization": "Bearer kbc_at_session_secret",
            "X-Storage-Stack": "us",
        },
    )
    assert resp.status_code == 401
    assert "X-Storage-Project" in resp.json()["detail"]


def test_a_malformed_project_header_is_a_bad_request(api: Api) -> None:
    resp = api.client.get(
        "/api/artifacts", headers={**SESSION_HEADERS, "X-Storage-Project": "abc"}
    )
    assert resp.status_code == 400


def test_a_malformed_project_header_never_breaks_a_public_read(api: Api) -> None:
    """optional_caller must degrade to "no identity", not to a 400."""
    artifact_id = _publish_markdown(api, "# Public")
    resp = api.client.get(
        f"/a/{artifact_id}", headers={**SESSION_HEADERS, "X-Storage-Project": "abc"}
    )
    assert resp.status_code == 200


def test_the_login_page_offers_the_device_flow_and_not_pkce_when_hosted(
    api: Api,
) -> None:
    """A hosted hub has no loopback callback, so only the code flow is on."""
    body = api.client.get("/login").text
    assert "Sign in with a code" in body
    assert "window.HUB_PKCE = false" in body
    assert api.client.get("/login").headers["cache-control"] == "no-store"


def test_pkce_is_refused_without_a_loopback_origin(api: Api) -> None:
    assert api.client.get("/login/pkce/start?stack=us").status_code == 404
    assert api.client.get("/login/callback?code=x&state=y").status_code == 404


def test_starting_a_device_sign_in_returns_the_user_code(api: Api) -> None:
    with respx.mock as mock:
        mock.post(f"{_STACK}/v1/auth/device").mock(
            return_value=httpx.Response(
                200,
                json={
                    "deviceCode": "kbc_dc_secret",
                    "userCode": "ABCD-EFGH",
                    "verificationUri": f"{_STACK}/admin/auth/device",
                    "verificationUriComplete": (
                        f"{_STACK}/admin/auth/device?userCode=ABCD-EFGH"
                    ),
                    "expiresIn": 900,
                    "interval": 5,
                },
            )
        )
        resp = api.client.post("/login/device", json={"stack": "us"})
    assert resp.status_code == 200
    assert resp.json()["user_code"] == "ABCD-EFGH"
    assert resp.json()["stack"] == _STACK
    assert resp.headers["cache-control"] == "no-store"


def test_a_sign_in_against_a_disallowed_stack_is_refused(api: Api) -> None:
    resp = api.client.post("/login/device", json={"stack": "https://evil.example.com"})
    assert resp.status_code == 400


def test_a_stack_without_the_feature_reports_it_rather_than_failing(api: Api) -> None:
    with respx.mock as mock:
        mock.post(f"{_STACK}/v1/auth/device").mock(return_value=httpx.Response(404))
        resp = api.client.post("/login/device", json={"stack": "us"})
    assert resp.status_code == 502
    assert "does not offer" in resp.json()["detail"]


def test_polling_reports_pending_until_the_sign_in_is_approved(api: Api) -> None:
    with respx.mock as mock:
        mock.post(f"{_STACK}/v1/auth/device/token").mock(
            return_value=httpx.Response(400, json=_pending_body("slow_down", 10))
        )
        resp = api.client.post(
            "/login/device/token", json={"stack": "us", "device_code": "kbc_dc_secret"}
        )
    assert resp.status_code == 200
    assert resp.json() == {"status": "pending", "interval": 10, "slow_down": True}


def test_an_approved_sign_in_returns_the_session_and_its_projects(api: Api) -> None:
    with respx.mock as mock:
        mock.post(f"{_STACK}/v1/auth/device/token").mock(
            return_value=httpx.Response(200, json=_CLI_TOKEN_BODY)
        )
        mock.get(f"{_STACK}/v1/auth/token/introspect").mock(
            return_value=httpx.Response(
                200,
                json=_introspect_body([{"id": 123, "name": "Test", "role": "admin"}]),
            )
        )
        resp = api.client.post(
            "/login/device/token", json={"stack": "us", "device_code": "kbc_dc_secret"}
        )
    body = resp.json()
    assert body["status"] == "ok"
    assert body["credential"]["access_token"] == "kbc_at_session_secret"
    assert body["credential"]["user"]["email"] == "someone@keboola.com"
    assert body["projects"] == [{"id": 123, "name": "Test", "role": "admin"}]
    assert body["projects_unavailable"] is False
    assert resp.headers["cache-control"] == "no-store"


def test_a_session_whose_projects_cannot_be_listed_still_signs_in(api: Api) -> None:
    """Introspection is a convenience; failing it must not undo a valid login."""
    with respx.mock as mock:
        mock.post(f"{_STACK}/v1/auth/device/token").mock(
            return_value=httpx.Response(200, json=_CLI_TOKEN_BODY)
        )
        mock.get(f"{_STACK}/v1/auth/token/introspect").mock(
            return_value=httpx.Response(500)
        )
        resp = api.client.post(
            "/login/device/token", json={"stack": "us", "device_code": "kbc_dc_secret"}
        )
    body = resp.json()
    assert body["status"] == "ok"
    assert body["projects"] == []
    assert body["projects_unavailable"] is True


def test_a_declined_sign_in_is_a_bad_request(api: Api) -> None:
    with respx.mock as mock:
        mock.post(f"{_STACK}/v1/auth/device/token").mock(
            return_value=httpx.Response(400, json=_pending_body("access_denied"))
        )
        resp = api.client.post(
            "/login/device/token", json={"stack": "us", "device_code": "kbc_dc_secret"}
        )
    assert resp.status_code == 400
    assert "declined" in resp.json()["detail"]


def test_a_session_can_be_renewed_and_revoked(api: Api) -> None:
    with respx.mock as mock:
        mock.post(f"{_STACK}/v1/auth/token/refresh").mock(
            return_value=httpx.Response(
                200, json={**_CLI_TOKEN_BODY, "accessToken": "kbc_at_next_secret"}
            )
        )
        renewed = api.client.post(
            "/login/refresh", json={"stack": "us", "refresh_token": "kbc_rt_session_secret"}
        )
        assert renewed.json()["credential"]["access_token"] == "kbc_at_next_secret"

        revoked = mock.post(f"{_STACK}/v1/auth/token/revoke").mock(
            return_value=httpx.Response(200, json={})
        )
        out = api.client.post(
            "/login/signout", json={"stack": "us", "token": "kbc_rt_session_secret"}
        )
    assert out.status_code == 204
    assert revoked.called


def test_signing_out_succeeds_even_when_the_stack_refuses(api: Api) -> None:
    """A credential the stack will not revoke is one it has already forgotten."""
    with respx.mock as mock:
        mock.post(f"{_STACK}/v1/auth/token/revoke").mock(
            return_value=httpx.Response(401, json={})
        )
        resp = api.client.post(
            "/login/signout", json={"stack": "us", "token": "kbc_rt_session_secret"}
        )
    assert resp.status_code == 204


def test_starting_sign_ins_is_rate_limited_per_address(api: Api) -> None:
    """The hub relays sign-ins to a stack, so it must not be an open relay."""
    limit = api.settings.max_logins_per_hour
    with respx.mock as mock:
        mock.post(f"{_STACK}/v1/auth/device").mock(return_value=httpx.Response(404))
        for _ in range(limit):
            api.client.post("/login/device", json={"stack": "us"})
        resp = api.client.post("/login/device", json={"stack": "us"})
    assert resp.status_code == 429


def test_polling_has_its_own_budget_far_above_the_starts(api: Api) -> None:
    """One honest sign-in polls ~180 times, so polls cannot share the starts'
    budget — but they still need one of their own."""
    with respx.mock as mock:
        mock.post(f"{_STACK}/v1/auth/device/token").mock(
            return_value=httpx.Response(400, json=_pending_body("authorization_pending"))
        )
        for _ in range(api.settings.max_logins_per_hour + 5):
            resp = api.client.post(
                "/login/device/token",
                json={"stack": "us", "device_code": "kbc_dc_secret"},
            )
            assert resp.status_code == 200
    assert api.settings.max_login_polls_per_hour > api.settings.max_logins_per_hour


def test_every_login_route_is_bounded_per_address(api: Api, monkeypatch) -> None:
    """concept-abuse: /login/* is unauthenticated and calls a Keboola stack.

    The stack cannot bound this from its side — the address it rate limits is
    the hub's, so one abuser there would spend every other visitor's budget.
    Each route has to charge the caller here.
    """
    monkeypatch.setattr(
        main,
        "settings",
        dataclasses.replace(
            api.settings, max_logins_per_hour=2, max_login_polls_per_hour=2
        ),
    )
    routes = {
        "/login/device": {"stack": "us"},
        "/login/device/token": {"stack": "us", "device_code": "kbc_dc_secret"},
        "/login/refresh": {"stack": "us", "refresh_token": "kbc_rt_x"},
        "/login/signout": {"stack": "us", "token": "kbc_rt_x"},
    }
    with respx.mock as mock:
        # Whatever the stack would answer is beside the point: the budget is
        # spent before the call, and the third attempt must not reach it.
        mock.post(url__regex=r".*/v1/auth/.*").mock(
            return_value=httpx.Response(400, json=_pending_body("invalid_request"))
        )
        for path, body in routes.items():
            main._fallback_counts.clear()
            statuses = [
                api.client.post(path, json=body).status_code for _ in range(3)
            ]
            assert statuses[-1] == 429, f"{path} is unbounded: {statuses}"



def test_the_login_endpoints_are_not_marked_as_needing_a_token(api: Api) -> None:
    """A sign-in route the caller must already be signed in for would be absurd."""
    schema = api.client.get("/openapi.json").json()
    for path, methods in schema["paths"].items():
        if not path.startswith("/login"):
            continue
        for operation in methods.values():
            assert "security" not in operation, path


@pytest.fixture
def loopback_api(api: Api, monkeypatch) -> Api:
    """The same hub, answering on the loopback origin PKCE needs.

    A Keboola stack accepts only an ``http://127.0.0.1:{port}`` redirect for
    the PKCE flow, so the flow exists exactly for a hub the user runs
    themselves — which is what this fixture models.
    """
    monkeypatch.setattr(
        main,
        "settings",
        dataclasses.replace(api.settings, public_base_url="http://127.0.0.1:8050"),
    )
    return api


def test_the_loopback_login_page_offers_the_browser_flow(loopback_api: Api) -> None:
    body = loopback_api.client.get("/login").text
    assert "window.HUB_PKCE = true" in body
    assert "Sign in with your browser" in body


def test_pkce_start_redirects_to_the_stack_with_a_challenge(
    loopback_api: Api,
) -> None:
    resp = loopback_api.client.get(
        "/login/pkce/start?stack=us", follow_redirects=False
    )
    assert resp.status_code == 307
    target = httpx.URL(resp.headers["location"])
    assert str(target).startswith(f"{_STACK}/admin/auth/pkce/authorize?")
    assert target.params["codeChallengeMethod"] == "S256"
    assert (
        target.params["redirectUri"] == "http://127.0.0.1:8050/login/callback"
    )
    assert len(target.params["codeChallenge"]) == 43
    assert len(target.params["state"]) >= 22


def test_the_pkce_callback_completes_the_sign_in(loopback_api: Api) -> None:
    start = loopback_api.client.get(
        "/login/pkce/start?stack=us", follow_redirects=False
    )
    state = httpx.URL(start.headers["location"]).params["state"]

    with respx.mock as mock:
        exchange = mock.post(f"{_STACK}/v1/auth/pkce/token").mock(
            return_value=httpx.Response(200, json=_CLI_TOKEN_BODY)
        )
        mock.get(f"{_STACK}/v1/auth/token/introspect").mock(
            return_value=httpx.Response(
                200,
                json=_introspect_body([{"id": 123, "name": "Test", "role": "admin"}]),
            )
        )
        resp = loopback_api.client.get(f"/login/callback?code=the-code&state={state}")

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = resp.text
    assert "kbc_at_session_secret" in body
    assert '"name": "Test"' in body or '"name":"Test"' in body
    sent = json.loads(exchange.calls.last.request.read())
    assert sent["code"] == "the-code"
    assert sent["state"] == state
    assert len(sent["codeVerifier"]) == 43


def test_a_pkce_code_cannot_be_replayed(loopback_api: Api) -> None:
    """The pending verifier is consumed by the first callback, valid or not."""
    start = loopback_api.client.get(
        "/login/pkce/start?stack=us", follow_redirects=False
    )
    state = httpx.URL(start.headers["location"]).params["state"]

    with respx.mock as mock:
        mock.post(f"{_STACK}/v1/auth/pkce/token").mock(
            return_value=httpx.Response(200, json=_CLI_TOKEN_BODY)
        )
        mock.get(f"{_STACK}/v1/auth/token/introspect").mock(
            return_value=httpx.Response(200, json=_introspect_body([]))
        )
        first = loopback_api.client.get(f"/login/callback?code=c&state={state}")
        second = loopback_api.client.get(f"/login/callback?code=c&state={state}")

    assert "kbc_at_session_secret" in first.text
    assert "kbc_at_session_secret" not in second.text
    assert "no longer valid" in second.text


def test_an_unknown_callback_state_says_nothing_about_why(loopback_api: Api) -> None:
    resp = loopback_api.client.get("/login/callback?code=c&state=never-issued")
    assert resp.status_code == 200
    assert "no longer valid" in resp.text


def test_a_declined_pkce_authorization_is_reported_not_exchanged(
    loopback_api: Api,
) -> None:
    start = loopback_api.client.get(
        "/login/pkce/start?stack=us", follow_redirects=False
    )
    state = httpx.URL(start.headers["location"]).params["state"]
    with respx.mock as mock:
        exchange = mock.post(f"{_STACK}/v1/auth/pkce/token")
        resp = loopback_api.client.get(
            f"/login/callback?error=access_denied&state={state}"
        )
    assert not exchange.called
    assert "declined" in resp.text


def test_starting_a_pkce_sign_in_is_bounded_too(
    loopback_api: Api, monkeypatch
) -> None:
    """On a loopback hub, so a 429 cannot be confused with the 404 a hosted
    hub answers for a flow it does not offer."""
    monkeypatch.setattr(
        main,
        "settings",
        dataclasses.replace(
            loopback_api.settings,
            public_base_url="http://127.0.0.1:8050",
            max_logins_per_hour=2,
        ),
    )
    statuses = [
        loopback_api.client.get(
            "/login/pkce/start?stack=us", follow_redirects=False
        ).status_code
        for _ in range(3)
    ]
    assert statuses == [307, 307, 429], statuses


def test_a_project_name_cannot_close_the_script_it_is_embedded_in(
    loopback_api: Api,
) -> None:
    """The callback inlines stack-supplied names into a <script> element."""
    start = loopback_api.client.get(
        "/login/pkce/start?stack=us", follow_redirects=False
    )
    state = httpx.URL(start.headers["location"]).params["state"]
    with respx.mock as mock:
        mock.post(f"{_STACK}/v1/auth/pkce/token").mock(
            return_value=httpx.Response(200, json=_CLI_TOKEN_BODY)
        )
        mock.get(f"{_STACK}/v1/auth/token/introspect").mock(
            return_value=httpx.Response(
                200,
                json=_introspect_body(
                    [{"id": 1, "name": "</script><img src=x>", "role": "admin"}]
                ),
            )
        )
        resp = loopback_api.client.get(f"/login/callback?code=c&state={state}")
    assert "</script><img" not in resp.text
    assert "\\u003c/script>" in resp.text


# --------------------------------------------------------------------------
# Inline scripts
# --------------------------------------------------------------------------


def test_every_inline_script_parses() -> None:
    """Each page ships its JavaScript as a Python string, which nothing checks.

    A Python-style implicit string concatenation ("a" "b") is valid Python and
    a syntax error in JavaScript, so the mistake survives every test that only
    looks at rendered markup — and takes the whole page down in the browser.
    Parsing each script catches that class of typo at test time.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available to parse the inline scripts")
    scripts = {
        name: getattr(pages, name) for name in dir(pages) if name.endswith("_JS")
    }
    assert scripts, "no inline scripts found to check"
    with tempfile.TemporaryDirectory() as tmp:
        for name, source in scripts.items():
            path = pathlib.Path(tmp) / f"{name}.js"
            path.write_text(source)
            result = subprocess.run(
                [node, "--check", str(path)], capture_output=True, text=True
            )
            assert result.returncode == 0, f"{name} does not parse:\n{result.stderr}"


def test_a_hub_side_failure_does_not_discard_the_visitors_credential() -> None:
    """Only 401/403 says anything about the credential.

    The studio and the review page both boot by calling /api/artifacts. When
    the hub's own Storage is down that call is a 502 — the same for every
    visitor — and throwing the sign-in away over it sends people back through
    a sign-in that cannot fix anything.
    """
    # The test itself is in the module both pages share, so neither can drift
    # away from it: only these two statuses are about the credential.
    assert "err.status === 401 || err.status === 403" in pages._SESSION_JS
    for source in (pages._ADMIN_JS, pages._REVIEW_JS):
        assert "SESSION.rejected(err)" in source
        assert source.count("That session is no longer valid") == 1


#: A personal access token, the other credential shape a bearer can be.
PAT_HEADERS = {
    "Authorization": "Bearer kbc_pat_personal_secret",
    "X-Storage-Stack": "us",
    "X-Storage-Project": "123",
}


def test_a_personal_access_token_drives_the_whole_management_api(api: Api) -> None:
    """concept-auth: every /api route takes a PAT, not just the read ones.

    A PAT authenticates the same way a session does, so if one route special-
    cased the Storage token the rest of the surface would quietly diverge.
    This walks a full lifecycle on a PAT alone.
    """
    published = api.client.post(
        "/api/artifacts",
        json={"markdown": "# From a PAT", "accept_versions": True},
        headers=PAT_HEADERS,
    )
    assert published.status_code == 201, published.text
    artifact_id = published.json()["id"]

    updated = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"markdown": "# From a PAT\n\nSecond version."},
        headers=PAT_HEADERS,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2

    listed = api.client.get("/api/artifacts", headers=PAT_HEADERS)
    assert [row["id"] for row in listed.json()["artifacts"]] == [artifact_id]

    pinned = api.client.put(
        f"/api/artifacts/{artifact_id}/head",
        json={"mode": "pinned", "version": 1},
        headers=PAT_HEADERS,
    )
    assert pinned.status_code == 200, pinned.text

    thread = api.client.post(
        f"/api/artifacts/{artifact_id}/comments",
        json={"version": 1, "exact": "From a PAT", "prefix": "", "suffix": "",
              "body": "Looks right."},
        headers=PAT_HEADERS,
    )
    assert thread.status_code == 201, thread.text

    assert api.client.get(
        f"/api/artifacts/{artifact_id}/stats", headers=PAT_HEADERS
    ).status_code == 200
    assert api.client.delete(
        f"/api/artifacts/{artifact_id}", headers=PAT_HEADERS
    ).status_code == 200


def test_a_pat_and_a_session_in_one_project_are_the_same_owner(api: Api) -> None:
    """Ownership is the project, however the caller proved they are in it."""
    artifact_id = api.client.post(
        "/api/artifacts", json={"markdown": "# Shared"}, headers=SESSION_HEADERS
    ).json()["id"]

    promoted = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"markdown": "# Shared\n\nEdited through a PAT."},
        headers=PAT_HEADERS,
    )
    assert promoted.status_code == 200, promoted.text

    # And a Storage token for the same project reaches it too.
    assert api.client.get(
        f"/api/artifacts/{artifact_id}/stats", headers=AUTH_HEADERS
    ).status_code == 200


def test_a_credential_that_cannot_write_says_so(api: Api, monkeypatch) -> None:
    """A read-only PAT verifies, then fails the one write the caller's own
    project needs. The message has to name that, or the caller sees a bare
    502 for a request that was in every other way correct."""
    class _ReadOnly:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def upload(self, name: str, content: bytes, tags: list[str]) -> int:
            raise BackendError("403 Client Error: Forbidden")

    monkeypatch.setattr(main, "KbcFilesBackend", _ReadOnly)
    resp = api.client.post(
        "/api/artifacts", json={"markdown": "# Nope"}, headers=PAT_HEADERS
    )
    assert resp.status_code == 502
    assert "read-only personal access token" in resp.json()["detail"]


# --------------------------------------------------------------------------
# Credential spellings
# --------------------------------------------------------------------------


def test_a_session_authenticates_through_the_standard_bearer_header(api: Api) -> None:
    """concept-auth: a bearer is sent the way bearers are sent.

    Authorization: Bearer is where a kbc_at_* token goes on a Keboola stack,
    so it is where it goes here — one kind of credential, one header.
    """
    resp = api.client.post(
        "/api/artifacts",
        json={"markdown": "# Via Authorization"},
        headers={
            "Authorization": "Bearer kbc_at_session_secret",
            "X-Storage-Stack": "us",
            "X-Storage-Project": "123",
        },
    )
    assert resp.status_code == 201, resp.text
    assert api.canonical_calls[-1]["token"] == "kbc_at_session_secret"


def test_the_bearer_scheme_is_matched_case_insensitively(api: Api) -> None:
    resp = api.client.get(
        "/api/artifacts",
        headers={
            "Authorization": "bearer kbc_at_session_secret",
            "X-Storage-Stack": "us",
            "X-Storage-Project": "123",
        },
    )
    assert resp.status_code == 200


def test_a_storage_token_in_the_bearer_header_is_refused(api: Api) -> None:
    """Each header carries one kind of credential, and says so when it doesn't.

    Accepting either value in either header is what made it impossible to tell
    where a sign-in belongs; the 400 names the header that value wants.
    """
    resp = api.client.get(
        "/api/artifacts",
        headers={"Authorization": "Bearer good-token", "X-Storage-Stack": "us"},
    )
    assert resp.status_code == 400
    assert "X-StorageApi-Token" in resp.json()["detail"]


def test_a_sign_in_token_in_the_storage_header_is_refused(api: Api) -> None:
    """The mistake this contract exists to prevent, and it teaches its own fix."""
    resp = api.client.get(
        "/api/artifacts",
        headers={
            "X-StorageApi-Token": "kbc_at_session_secret",
            "X-Storage-Stack": "us",
            "X-Storage-Project": "123",
        },
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "Authorization: Bearer" in detail
    assert "X-Storage-Project" in detail


def test_two_credentials_in_one_request_are_refused(api: Api) -> None:
    """Resolving this by precedence would authenticate as the one the caller
    did not mean."""
    resp = api.client.get(
        "/api/artifacts",
        headers={
            "Authorization": "Bearer kbc_at_session_secret",
            "X-StorageApi-Token": "good-token",
            "X-Storage-Stack": "us",
            "X-Storage-Project": "123",
        },
    )
    assert resp.status_code == 400
    assert "Send one credential" in resp.json()["detail"]


def test_conflicting_credentials_never_break_a_public_read(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Public")
    resp = api.client.get(
        f"/a/{artifact_id}",
        headers={
            "Authorization": "Bearer kbc_at_session_secret",
            "X-StorageApi-Token": "good-token",
            "X-Storage-Stack": "us",
        },
    )
    assert resp.status_code == 200


def test_a_non_bearer_authorization_scheme_is_ignored(api: Api) -> None:
    """Basic auth from some intermediary must not be read as a credential."""
    resp = api.client.get(
        "/api/artifacts",
        headers={
            "Authorization": "Basic dXNlcjpwYXNz",
            "X-StorageApi-Token": "good-token",
            "X-Storage-Stack": "us",
        },
    )
    assert resp.status_code == 200


def test_curl_examples_carry_both_credentials_and_a_switch(api: Api) -> None:
    """concept-docs: an example must not silently assume one credential.

    Both header blocks are in the markup and CSS hides one, so the page reads
    correctly for whichever credential the visitor holds — and still shows a
    complete, working example with JavaScript off.
    """
    body = api.client.get("/").text
    assert body.count('class="credsw"') == 3
    assert body.count("cred cred-tok") == body.count("cred cred-bearer") == 5
    assert "X-StorageApi-Token: $KBC_TOKEN" in body
    assert "Authorization: Bearer $KBC_TOKEN" in body
    assert "X-Storage-Project: $KBC_PROJECT" in body
    assert pages._CREDENTIAL_JS in body


def test_the_versions_picker_switches_its_example_too(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Doc", accept_versions=True)
    body = api.client.get(f"/a/{artifact_id}/versions?format=html").text
    assert 'class="credsw"' in body
    assert "cred cred-tok" in body and "cred cred-bearer" in body
    assert pages._CREDENTIAL_JS in body


def test_the_token_form_is_what_shows_without_javascript(api: Api) -> None:
    """The default must be the credential that needs no sign-in to try."""
    assert ".cred-bearer { display: none; }" in pages._CSS
    assert ".auth-bearer .cred-tok { display: none; }" in pages._CSS


def test_a_signed_in_session_is_judged_by_the_token_the_stack_resolved(
    api: Api, monkeypatch
) -> None:
    """concept-auth: SEC-075-011 still governs a sign-in credential.

    A bearer is exchanged by the stack for the admin's own Storage token in
    the named project, so ``/v2/storage/tokens/verify`` describes *that*
    token. The destructive-token policy therefore reads real claims for a
    session, exactly as it does for a pasted token — a sign-in is not a way
    around it.
    """
    artifact_id = api.client.post(
        "/api/artifacts", json={"markdown": "# Gated"}, headers=SESSION_HEADERS
    ).json()["id"]

    monkeypatch.setattr(
        main,
        "settings",
        dataclasses.replace(api.settings, destructive_token_policy="admin"),
    )
    # The fixture's session resolves to a project administrator, so it passes.
    assert api.client.post(
        f"/api/artifacts/{artifact_id}/rotate-link", headers=SESSION_HEADERS
    ).status_code == 200


def test_a_sign_in_without_admin_claims_is_refused_the_destructive_routes(
    api: Api, monkeypatch
) -> None:
    """The same session, resolved to a non-admin token, is gated like any other."""
    artifact_id = api.client.post(
        "/api/artifacts", json={"markdown": "# Gated"}, headers=SESSION_HEADERS
    ).json()["id"]

    monkeypatch.setitem(
        _SESSION_TOKENS, "kbc_at_session_secret", {"admin_role": "readOnly"}
    )
    monkeypatch.setattr(
        main,
        "settings",
        dataclasses.replace(api.settings, destructive_token_policy="admin"),
    )
    refused = api.client.delete(
        f"/api/artifacts/{artifact_id}/purge", headers=SESSION_HEADERS
    )
    assert refused.status_code == 403
    assert "destructive_token_policy=admin" in refused.json()["detail"]


def test_a_project_header_with_no_credential_names_the_likely_cause(api: Api) -> None:
    """concept-ops: a stripped Authorization must not look like "you sent nothing".

    A signed-in caller always sends both headers. The project header arriving
    alone means the credential was dropped in transit — and the likeliest
    dropper is a proxy in front of this hub, which already strips X-Kbc-*.
    Without this, a user who has just signed in successfully gets the same
    401 as someone who never had a credential at all.
    """
    resp = api.client.get(
        "/api/artifacts",
        headers={"X-Storage-Stack": "us", "X-Storage-Project": "123"},
    )
    assert resp.status_code == 401
    detail = resp.json()["detail"]
    assert "dropped it" in detail
    assert "/health/headers" in detail


def test_no_credential_at_all_still_reads_as_no_credential(api: Api) -> None:
    """The hint above must not fire for a caller who simply sent nothing."""
    resp = api.client.get("/api/artifacts", headers={"X-Storage-Stack": "us"})
    assert resp.status_code == 401
    assert "dropped it" not in resp.json()["detail"]


# --------------------------------------------------------------------------
# Reader menu (0.18.0) -- pages layer
# --------------------------------------------------------------------------

READER_MENU_LABEL = "What can I do with this document?"


def _frame_with_menu(**kwargs) -> str:
    return pages.artifact_frame_page(
        "Report",
        "<h1>hi</h1>",
        base_url="https://hub.example.com",
        share_id="shr1",
        **kwargs,
    )


def test_frame_page_has_no_reader_menu_by_default():
    page = _frame_with_menu()
    assert READER_MENU_LABEL not in page
    assert "ahmenu" not in page


def test_frame_page_with_reader_menu_renders_button_and_panel():
    page = _frame_with_menu(reader_menu=True)
    assert f'aria-label="{READER_MENU_LABEL}"' in page
    assert 'id="ah-menu-panel"' in page
    assert 'aria-expanded="false"' in page
    # The panel must sit *after* the iframe, so it is hub chrome painted over
    # the document rather than anything the document can reach.
    assert page.index('id="ah-frame"') < page.index('id="ah-menu-btn"')


def test_reader_menu_links_point_at_the_share_id():
    page = _frame_with_menu(reader_menu=True, accept_versions_mode="anyone")
    base = "https://hub.example.com"
    for href in (
        f"{base}/a/shr1/review",
        f"{base}/a/shr1/versions?format=html",
        f"{base}/a/shr1/export/markdown",
        f"{base}/llms.txt",
    ):
        assert html.escape(href, quote=True) in page, href
    assert f'href="{base}/"' in page


def test_reader_menu_proposal_item_follows_accept_versions_mode():
    off = _frame_with_menu(reader_menu=True, accept_versions_mode="off")
    assert "does not accept proposals" in off
    assert "/api/artifacts/shr1/versions" not in off
    for mode in ("anyone", "allowlist"):
        on = _frame_with_menu(reader_menu=True, accept_versions_mode=mode)
        assert "Propose a new version" in on
        assert "does not accept proposals" not in on
        # The how-to keeps the route, but the human link goes to a page a
        # browser can render -- not to a raw-Markdown anchor.
        assert "/api/artifacts/shr1/versions" in on
        assert "/skill#versioning" not in on


def test_reader_menu_escapes_the_share_id():
    page = pages.artifact_frame_page(
        "Report",
        "<h1>hi</h1>",
        base_url="https://hub.example.com",
        share_id='x"><script>bad()</script>',
        reader_menu=True,
    )
    assert "<script>bad()</script>" not in page
    assert "&lt;script&gt;bad()&lt;/script&gt;" in page


def test_reader_menu_holds_no_credential():
    """The menu is static markup plus open/close; it never touches a token."""
    page = _frame_with_menu(reader_menu=True)
    menu = page[page.index('id="ah-menu-btn"') :]
    for forbidden in ("sessionStorage", "hubSession", "X-Storage-Token"):
        assert forbidden not in menu


def test_reader_menu_copy_link_has_a_visible_fallback():
    page = _frame_with_menu(reader_menu=True)
    assert "clipboard" in page
    assert 'id="ah-menu-url"' in page


# --------------------------------------------------------------------------
# Reader menu (0.18.0) -- API layer
# --------------------------------------------------------------------------


def test_reader_menu_is_on_for_a_new_artifact(api: Api) -> None:
    resp = api.client.post(
        "/api/artifacts", json={"markdown": "# Hi"}, headers=AUTH_HEADERS
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["reader_menu"] is True


def test_reader_menu_put_toggles_and_echoes(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Hi")
    off = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"reader_menu": False},
        headers=AUTH_HEADERS,
    )
    assert off.status_code == 200, off.text
    assert off.json()["reader_menu"] is False
    back = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"reader_menu": True},
        headers=AUTH_HEADERS,
    )
    assert back.json()["reader_menu"] is True


def test_reader_menu_survives_a_restart(api: Api) -> None:
    """The flag is in the meta file, so the rebuilt index still has it."""
    artifact_id = _publish_markdown(api, "# Hi")
    api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"reader_menu": False},
        headers=AUTH_HEADERS,
    )
    meta = api.client.app.state.store.get_meta(artifact_id)
    assert meta is not None and meta.reader_menu is False


def test_reader_menu_reported_by_meta_and_listing(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Hi")
    assert api.client.get(f"/a/{artifact_id}/meta").json()["reader_menu"] is True
    api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"reader_menu": False},
        headers=AUTH_HEADERS,
    )
    assert api.client.get(f"/a/{artifact_id}/meta").json()["reader_menu"] is False
    rows = api.client.get("/api/artifacts", headers=AUTH_HEADERS).json()["artifacts"]
    row = next(r for r in rows if r["id"] == artifact_id)
    assert row["reader_menu"] is False


def test_reader_menu_appears_on_the_artifact_page(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Hi")
    page = api.client.get(f"/a/{artifact_id}").text
    assert READER_MENU_LABEL in page
    assert f"/a/{artifact_id}/review" in page
    version = api.client.get(f"/a/{artifact_id}/v/1").text
    assert READER_MENU_LABEL in version


def test_reader_menu_can_be_switched_off_for_one_artifact(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Hi")
    api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"reader_menu": False},
        headers=AUTH_HEADERS,
    )
    assert READER_MENU_LABEL not in api.client.get(f"/a/{artifact_id}").text


def test_reader_menu_never_touches_raw_or_export(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Hi")
    raw_on = api.client.get(f"/a/{artifact_id}/raw").content
    export_on = api.client.get(f"/a/{artifact_id}/export/markdown").content
    assert READER_MENU_LABEL not in raw_on.decode()
    api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"reader_menu": False},
        headers=AUTH_HEADERS,
    )
    assert api.client.get(f"/a/{artifact_id}/raw").content == raw_on
    assert api.client.get(f"/a/{artifact_id}/export/markdown").content == export_on


def test_reader_menu_proposal_row_follows_the_artifact_setting(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Hi")
    assert "does not accept proposals" in api.client.get(f"/a/{artifact_id}").text
    api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"accept_versions_mode": "anyone"},
        headers=AUTH_HEADERS,
    )
    page = api.client.get(f"/a/{artifact_id}").text
    assert "Propose a new version" in page
    assert "does not accept proposals" not in page


def test_reader_menu_is_not_a_destructive_route(api: Api, monkeypatch) -> None:
    """The strictest destructive policy must not gate an ordinary setting."""
    monkeypatch.setattr(
        main,
        "settings",
        dataclasses.replace(api.settings, destructive_token_policy="admin"),
    )
    artifact_id = _publish_markdown(api, "# Hi")
    resp = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"reader_menu": False},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200, resp.text


def test_context_reports_the_reader_menu_default(api: Api) -> None:
    body = api.client.get("/context").json()
    assert body["limits"]["reader_menu_default"] is True


def test_admin_studio_has_a_reader_menu_checkbox(api: Api) -> None:
    page = api.client.get("/admin").text
    assert "reader_menu" in page
    assert "reader menu" in page


def test_reader_menu_panel_is_focusable_and_announced_as_a_dialog():
    page = _frame_with_menu(reader_menu=True)
    assert 'aria-haspopup="dialog"' in page
    assert "panel.focus()" in page
    assert "btn.focus()" in page


def test_reader_menu_markdown_row_says_it_downloads():
    page = _frame_with_menu(reader_menu=True)
    assert "Download as Markdown" in page
    assert "downloads" in page


def test_frame_page_makes_in_page_anchors_work_and_raw_stays_untouched(api):
    """A srcdoc document cannot honour #fragments on its own; the shell injects
    a shim that performs the jump and forwards the address-bar fragment."""
    html_doc = '<html><body><nav><a href="#s2">go</a></nav><h2 id="s2">Two</h2></body></html>'
    r = api.client.post("/api/artifacts", json={"html": html_doc}, headers=AUTH_HEADERS)
    share = r.json()["share_id"]
    page = api.client.get(f"/a/{share}").text
    assert "ah-anchor-go" in page          # parent forwards the fragment down
    assert "ah-anchor" in page             # shim mirrors the section up
    assert 'a[href^=&quot;#&quot;]' in page or "a[href^=" in page   # shim lives inside the escaped srcdoc
    assert api.client.get(f"/a/{share}/raw").text == html_doc
