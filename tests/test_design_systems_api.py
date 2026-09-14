"""Route tests for design systems. Reuses the ``api`` fixture from test_api."""

import dataclasses

from tests.test_api import AUTH_HEADERS, OTHER_AUTH_HEADERS, api  # noqa: F401
from tests.test_designs import good as good_bundle


def _register(client, slug="corp", headers=AUTH_HEADERS, **extra):
    body = {
        "slug": slug,
        "name": "Corp",
        "description": "Brand",
        "bundle": good_bundle(),
        **extra,
    }
    return client.post("/api/design-systems", json=body, headers=headers)


def test_health_counts_design_systems(api):
    assert api.client.get("/health").json()["design_systems"] == 0
    assert _register(api.client).status_code == 201
    assert api.client.get("/health").json()["design_systems"] == 1


def test_ds_post_body_limit_is_the_ds_budget(api):
    from src import main

    assert (
        main._request_body_limit("POST", "/api/design-systems")
        == main.settings.ds_content_request_bytes
    )
    assert (
        main._request_body_limit("POST", "/api/design-systems/corp/versions")
        == main.settings.ds_content_request_bytes
    )
    assert (
        main._request_body_limit("PUT", "/api/design-systems/corp")
        == main.settings.max_small_request_bytes
    )
    assert (
        main._request_body_limit("POST", "/api/artifacts")
        == main.settings.max_content_request_bytes
    )


def test_ds_reader_responses_carry_agent_headers(api):
    ds_id = _register(api.client).json()["id"]
    r = api.client.get(f"/ds/{ds_id}/css")
    assert r.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert 'rel="service-desc"' in r.headers["Link"]
    assert r.headers["Cache-Control"] == "no-cache"


# --------------------------------------------------------------------------
# Management routes
# --------------------------------------------------------------------------


def test_register_returns_projection_and_urls(api):
    r = _register(api.client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert (
        body["slug"] == "corp"
        and body["version"] == 1
        and body["head_version"] == 1
        and body["mine"] is True
    )
    assert body["id"].startswith("ds_")
    assert body["owner"]["project_id"] == 123
    assert body["urls"]["bundle"].endswith(f"/ds/{body['id']}/bundle?v=1")
    assert body["urls"]["page"].endswith(f"/ds/{body['id']}")
    assert body["warnings"][0]["path"] == "/tokens/grad"


def test_register_conflicts_and_validation(api):
    _register(api.client)
    assert _register(api.client).status_code == 409
    assert _register(api.client, slug="ds_looks_like_id").status_code == 422
    bad = api.client.post(
        "/api/design-systems",
        json={"slug": "xx", "name": "X", "bundle": {"tokens": {}, "guidance": ""}},
        headers=AUTH_HEADERS,
    )
    assert bad.status_code == 422 and bad.json()["detail"][0]["path"] == "/bundle/tokens"
    assert (
        api.client.post(
            "/api/design-systems",
            json={"slug": "yy", "name": "Y", "bundle": good_bundle()},
            headers={"X-Kbc-Stack": "us"},
        ).status_code
        == 401
    )


def test_per_project_cap_and_daily_cap(api, monkeypatch):
    from src import main

    monkeypatch.setattr(
        main, "settings", dataclasses.replace(main.settings, ds_max_per_project=1)
    )
    assert _register(api.client, slug="aa").status_code == 201
    assert _register(api.client, slug="bb").status_code == 429
    # other project unaffected
    assert _register(api.client, slug="cc", headers=OTHER_AUTH_HEADERS).status_code == 201
    monkeypatch.setattr(
        main,
        "settings",
        dataclasses.replace(
            main.settings, ds_max_per_project=20, ds_max_versions_per_day=3
        ),
    )
    # The daily budget counts every version this project writes, registrations
    # included: "aa" above already spent one, "dd" here spends the second, so
    # with a budget of 3 exactly one more version fits.
    ref = _register(api.client, slug="dd").json()["id"]
    assert (
        api.client.post(
            f"/api/design-systems/{ref}/versions",
            json={"bundle": good_bundle()},
            headers=AUTH_HEADERS,
        ).status_code
        == 201
    )
    assert (
        api.client.post(
            f"/api/design-systems/{ref}/versions",
            json={"bundle": good_bundle()},
            headers=AUTH_HEADERS,
        ).status_code
        == 429
    )


def test_catalogue_lists_everyone_and_marks_mine(api):
    _register(api.client, slug="mine")
    _register(api.client, slug="theirs", headers=OTHER_AUTH_HEADERS)
    rows = api.client.get("/api/design-systems", headers=AUTH_HEADERS).json()[
        "design_systems"
    ]
    assert {r["slug"]: r["mine"] for r in rows} == {"mine": True, "theirs": False}
    assert (
        api.client.get(
            "/api/design-systems", headers={"X-Kbc-Stack": "us"}
        ).status_code
        == 401
    )
    r = api.client.get("/api/design-systems", headers=AUTH_HEADERS)
    assert r.headers["Cache-Control"] == "private, no-store"


def test_get_put_versions_and_owner_rules(api):
    ds_id = _register(api.client).json()["id"]
    one = api.client.get("/api/design-systems/corp", headers=OTHER_AUTH_HEADERS).json()
    assert (
        one["id"] == ds_id
        and one["mine"] is False
        and [v["version"] for v in one["versions"]] == [1]
    )
    assert (
        api.client.put(
            "/api/design-systems/corp",
            json={"name": "Corp 2"},
            headers=OTHER_AUTH_HEADERS,
        ).status_code
        == 403
    )
    assert (
        api.client.put(
            "/api/design-systems/corp", json={"description": None}, headers=AUTH_HEADERS
        ).status_code
        == 422
    )
    r = api.client.put(
        "/api/design-systems/corp",
        json={"name": "Corp 2", "description": ""},
        headers=AUTH_HEADERS,
    )
    assert (
        r.status_code == 200
        and r.json()["name"] == "Corp 2"
        and r.json()["description"] == ""
    )
    assert (
        api.client.post(
            f"/api/design-systems/{ds_id}/versions",
            json={"bundle": good_bundle(), "note": "v2"},
            headers=OTHER_AUTH_HEADERS,
        ).status_code
        == 403
    )
    v2 = api.client.post(
        f"/api/design-systems/{ds_id}/versions",
        json={"bundle": good_bundle(), "note": "v2"},
        headers=AUTH_HEADERS,
    )
    assert v2.status_code == 201
    assert v2.json()["version"] == 2 and v2.json()["head_version"] == 2


def test_version_limit_409_and_delete_rules(api, monkeypatch):
    from src import main

    monkeypatch.setattr(
        main, "settings", dataclasses.replace(main.settings, ds_max_versions=2)
    )
    ds_id = _register(api.client).json()["id"]
    api.client.post(
        f"/api/design-systems/{ds_id}/versions",
        json={"bundle": good_bundle()},
        headers=AUTH_HEADERS,
    )
    assert (
        api.client.post(
            f"/api/design-systems/{ds_id}/versions",
            json={"bundle": good_bundle()},
            headers=AUTH_HEADERS,
        ).status_code
        == 409
    )
    assert (
        api.client.delete(
            f"/api/design-systems/{ds_id}/versions/1", headers=OTHER_AUTH_HEADERS
        ).status_code
        == 403
    )
    assert (
        api.client.delete(
            f"/api/design-systems/{ds_id}/versions/1", headers=AUTH_HEADERS
        ).status_code
        == 204
    )
    assert (
        api.client.delete(
            f"/api/design-systems/{ds_id}/versions/2", headers=AUTH_HEADERS
        ).status_code
        == 409
    )
    assert (
        api.client.delete(
            f"/api/design-systems/{ds_id}", headers=AUTH_HEADERS
        ).status_code
        == 204
    )
    assert api.client.get(f"/ds/{ds_id}/bundle").status_code == 404
    assert _register(api.client).status_code == 201  # slug free again


def test_destructive_policy_applies_to_ds_deletes(api, monkeypatch):
    from src import main

    monkeypatch.setattr(
        main,
        "settings",
        dataclasses.replace(
            main.settings,
            destructive_token_policy="allowlist",
            destructive_token_ids=("nobody",),
        ),
    )
    ds_id = _register(api.client).json()["id"]
    assert (
        api.client.delete(
            f"/api/design-systems/{ds_id}", headers=AUTH_HEADERS
        ).status_code
        == 403
    )
    assert (
        api.client.put(
            f"/api/design-systems/{ds_id}", json={"name": "ok"}, headers=AUTH_HEADERS
        ).status_code
        == 200
    )


def test_create_is_502_when_not_hydrated(api):
    api.client.app.state.designs.hydrated = False
    assert _register(api.client).status_code == 502
