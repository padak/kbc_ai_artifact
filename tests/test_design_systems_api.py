"""Route tests for design systems. Reuses the ``api`` fixture from test_api."""

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
