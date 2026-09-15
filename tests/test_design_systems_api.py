"""Route tests for design systems. Reuses the ``api`` fixture from test_api."""

import dataclasses
import threading

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


# --------------------------------------------------------------------------
# Reader routes under /ds/{ref}
# --------------------------------------------------------------------------


def test_id_is_public_slug_needs_credential_and_never_leaks_existence(api):
    ds_id = _register(api.client).json()["id"]
    assert api.client.get(f"/ds/{ds_id}/bundle").status_code == 200
    assert api.client.get("/ds/corp/bundle").status_code == 401
    # identical answer for a slug that does not exist
    assert api.client.get("/ds/does-not-exist/bundle").status_code == 401
    assert api.client.get("/ds/corp/bundle", headers=AUTH_HEADERS).status_code == 200
    assert (
        api.client.get("/ds/does-not-exist/bundle", headers=AUTH_HEADERS).status_code
        == 404
    )
    assert api.client.get("/ds/ds_unknown/bundle").status_code == 404
    assert (
        api.client.get("/ds/corp/bundle", headers=AUTH_HEADERS).headers["Cache-Control"]
        == "private, no-store"
    )


def test_bundle_tokens_css_starter_guidance(api):
    ds_id = _register(api.client).json()["id"]
    b = api.client.get(f"/ds/{ds_id}/bundle").json()
    assert b["version"] == 1
    assert b["bundle"]["guidance"] == "# Corp\n\nUse bars."
    assert "variables" in b
    assert b["variables"]["color.accent"] == "--color-accent"
    assert b["urls"]["starter"].endswith(f"/ds/{ds_id}/starter?v=1")
    t = api.client.get(f"/ds/{ds_id}/tokens").json()
    assert set(t) == {"tokens", "modes"}
    css = api.client.get(f"/ds/{ds_id}/css")
    assert css.headers["content-type"].startswith("text/css")
    assert "--color-bg: #ffffff" in css.text
    assert "--color-bg: #000000" in api.client.get(f"/ds/{ds_id}/css?mode=dark").text
    assert api.client.get(f"/ds/{ds_id}/css?mode=sepia").status_code == 422
    s = api.client.get(f"/ds/{ds_id}/starter")
    assert s.headers["Content-Security-Policy"].startswith("sandbox")
    assert s.headers["X-Content-Type-Options"] == "nosniff"
    assert "{{BODY}}" in s.text
    g = api.client.get(f"/ds/{ds_id}/guidance")
    assert g.headers["content-type"].startswith("text/markdown")
    assert g.text.startswith("# Corp")


def test_version_selection(api):
    ds_id = _register(api.client).json()["id"]
    api.client.post(
        f"/api/design-systems/{ds_id}/versions",
        json={"bundle": {**good_bundle(), "guidance": "# v2"}},
        headers=AUTH_HEADERS,
    )
    assert api.client.get(f"/ds/{ds_id}/guidance").text.startswith("# v2")
    assert api.client.get(f"/ds/{ds_id}/guidance?v=1").text.startswith("# Corp")
    assert api.client.get(f"/ds/{ds_id}/guidance?v=9").status_code == 404
    assert api.client.get(f"/ds/{ds_id}/guidance?v=zero").status_code == 422
    assert api.client.get(f"/ds/{ds_id}/guidance?v=0").status_code == 422
    rows = api.client.get(f"/ds/{ds_id}/versions").json()
    assert rows["head_version"] == 2
    assert [r["version"] for r in rows["versions"]] == [1, 2]


def test_style_guide_page(api):
    ds_id = _register(api.client).json()["id"]
    page = api.client.get(f"/ds/{ds_id}")
    assert page.status_code == 200 and "srcdoc=" in page.text and "sandbox=" in page.text
    assert f"/ds/{ds_id}/starter?v=1" in page.text
    assert api.client.get(f"/ds/{ds_id}/css?mode=dark").status_code == 200
    b = {**good_bundle()}
    del b["modes"]
    other = api.client.post(
        "/api/design-systems",
        json={"slug": "nodark", "name": "N", "bundle": b},
        headers=AUTH_HEADERS,
    ).json()["id"]
    assert api.client.get(f"/ds/{other}/css?mode=dark").status_code == 404


# --------------------------------------------------------------------------
# Provenance on artifact writes
# --------------------------------------------------------------------------


def _publish(client, **body):
    return client.post(
        "/api/artifacts", json={"html": "<h1>x</h1>", **body}, headers=AUTH_HEADERS
    )


def test_provenance_is_resolved_stored_and_echoed(api):
    ds_id = _register(api.client).json()["id"]
    api.client.post(
        f"/api/design-systems/{ds_id}/versions",
        json={"bundle": good_bundle()},
        headers=AUTH_HEADERS,
    )
    r = _publish(api.client, design_system="corp@1")
    assert r.status_code == 201
    assert r.json()["design_system"] == {"id": ds_id, "slug": "corp", "version": 1}
    share = r.json()["share_id"]
    assert api.client.get(f"/a/{share}/meta").json()["design_system"] == {
        "id": ds_id,
        "slug": "corp",
        "version": 1,
    }
    r2 = _publish(api.client, design_system=ds_id)  # head
    assert r2.json()["design_system"]["version"] == 2
    assert _publish(api.client).json()["design_system"] is None
    v = api.client.post(
        f"/api/artifacts/{r.json()['id']}/versions",
        json={"html": "<h1>y</h1>", "design_system": f"{ds_id}@2"},
        headers=AUTH_HEADERS,
    )
    assert v.status_code == 201 and v.json()["design_system"]["version"] == 2
    # The history is newest-first, so sort by version to read the provenance
    # of v1 and v2 in order.
    rows = sorted(
        api.client.get(f"/a/{share}/versions").json()["versions"],
        key=lambda row: row["version"],
    )
    assert [row["design_system"]["version"] for row in rows] == [1, 2]


def test_provenance_errors(api):
    ds_id = _register(api.client).json()["id"]
    assert _publish(api.client, design_system="corp@9").status_code == 422
    assert _publish(api.client, design_system="nope").status_code == 422
    assert _publish(api.client, design_system="corp@0").status_code == 422
    assert _publish(api.client, design_system="corp@1@2").status_code == 422
    art = _publish(api.client).json()["id"]
    # metadata-only PUT: provenance describes content, so it needs content
    r = api.client.put(
        f"/api/artifacts/{art}", json={"design_system": "corp"}, headers=AUTH_HEADERS
    )
    assert r.status_code == 422
    r = api.client.put(
        f"/api/artifacts/{art}",
        json={"html": "<p>z</p>", "design_system": "corp"},
        headers=AUTH_HEADERS,
    )
    assert r.status_code == 200 and r.json()["design_system"]["slug"] == "corp"
    api.client.delete(f"/api/design-systems/{ds_id}", headers=AUTH_HEADERS)
    share = api.client.get("/api/artifacts", headers=AUTH_HEADERS).json()["artifacts"][
        0
    ]["share_id"]
    # untouched by the delete
    assert api.client.get(f"/a/{share}/meta").json()["design_system"]["slug"] == "corp"


# --------------------------------------------------------------------------
# Discovery: /context, /llms.txt, OpenAPI
# --------------------------------------------------------------------------


def test_context_documents_design_systems(api):
    ctx = api.client.get("/context").json()
    paths = {(e["method"], e["path"]) for e in ctx["endpoints"]}
    for m, path in [
        ("GET", "/api/design-systems"),
        ("POST", "/api/design-systems"),
        ("GET", "/api/design-systems/{ref}"),
        ("PUT", "/api/design-systems/{ref}"),
        ("POST", "/api/design-systems/{ref}/versions"),
        ("DELETE", "/api/design-systems/{ref}/versions/{n}"),
        ("DELETE", "/api/design-systems/{ref}"),
        ("GET", "/ds/{ref}"),
        ("GET", "/ds/{ref}/versions"),
        ("GET", "/ds/{ref}/bundle"),
        ("GET", "/ds/{ref}/tokens"),
        ("GET", "/ds/{ref}/css"),
        ("GET", "/ds/{ref}/starter"),
        ("GET", "/ds/{ref}/guidance"),
    ]:
        assert (m, path) in paths, (m, path)
    ds = ctx["design_systems"]
    assert ds["roles"]["accent"] == "color" and ds["ref"]["id_prefix"] == "ds_"
    assert [step[:1] for step in ds["agent_recipe"]] == [str(i) for i in range(1, 10)]
    assert "design_system" in ctx["publish_body"]
    for key in (
        "ds_max_bundle_bytes",
        "ds_max_per_project",
        "ds_max_versions",
        "ds_max_versions_per_day",
        "ds_max_tokens",
        "ds_max_components",
        "ds_max_palette",
        "ds_max_font_links",
        "ds_font_hosts",
        "ds_derived_cache_entries",
    ):
        assert key in ctx["limits"], key
    assert "/api/design-systems" in api.client.get("/llms.txt").text


# --------------------------------------------------------------------------
# Review round 1
# --------------------------------------------------------------------------


def test_v_query_rejects_every_non_canonical_integer(api):
    ds_id = _register(api.client).json()["id"]
    # "\u00b2" is str.isdigit() but not int()-parseable: it must be a 422,
    # never an unhandled 500.
    assert api.client.get(f"/ds/{ds_id}/bundle?v=\u00b2").status_code == 422
    assert api.client.get(f"/ds/{ds_id}/bundle?v=1e3").status_code == 422
    # A leading zero is not the canonical form of a version number.
    assert api.client.get(f"/ds/{ds_id}/bundle?v=01").status_code == 422
    assert api.client.get(f"/ds/{ds_id}/bundle?v=-1").status_code == 422
    assert api.client.get(f"/ds/{ds_id}/bundle?v=1").status_code == 200


def test_catalogue_does_not_download_version_envelopes(api):
    for slug in ("one", "two"):
        ds_id = _register(api.client, slug=slug).json()["id"]
        api.client.post(
            f"/api/design-systems/{ds_id}/versions",
            json={"bundle": good_bundle()},
            headers=AUTH_HEADERS,
        )
    # Cold caches, as after a restart: anything the catalogue needs it must
    # fetch, so the spy below sees the real fan-out rather than LRU hits.
    designs = api.client.app.state.designs
    designs._meta_memory.clear()
    designs._version_memory.clear()
    for path in api.settings.cache_dir.glob("ds.*"):
        path.unlink()

    downloaded: list[str] = []
    backend = api.backend
    real_download = backend.download

    def spy(file_id: int) -> bytes:
        downloaded.append(backend.files[file_id][0].name)
        return real_download(file_id)

    backend.download = spy
    try:
        rows = api.client.get("/api/design-systems", headers=AUTH_HEADERS).json()[
            "design_systems"
        ]
    finally:
        backend.download = real_download
    assert {r["slug"]: r["versions_count"] for r in rows} == {"one": 2, "two": 2}
    # Counting versions must never pull a version envelope: only meta files
    # (ds-{id}-meta.json) may be downloaded here.
    assert [n for n in downloaded if "-meta" not in n] == []


def test_concurrent_creation_cannot_exceed_the_per_project_cap(api, monkeypatch):
    from src import main

    monkeypatch.setattr(
        main, "settings", dataclasses.replace(main.settings, ds_max_per_project=1)
    )
    barrier = threading.Barrier(2)
    results: dict[str, int] = {}

    def register(slug: str) -> None:
        barrier.wait(timeout=10)
        results[slug] = _register(api.client, slug=slug).status_code

    threads = [
        threading.Thread(target=register, args=(slug,)) for slug in ("first", "second")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
        assert not t.is_alive()
    assert sorted(results.values()) == [201, 429]
    owner_key = next(iter(api.client.app.state.designs._index.values())).owner_key
    assert api.client.app.state.designs.count_owner(owner_key) == 1


def test_css_authenticates_a_slug_before_validating_mode(api):
    _register(api.client)
    # A slug ref is 401 without a credential whatever the query says.
    assert api.client.get("/ds/corp/css?mode=sepia").status_code == 401
    assert (
        api.client.get("/ds/corp/css?mode=sepia", headers=AUTH_HEADERS).status_code
        == 422
    )


def test_reader_is_503_while_the_index_is_unhydrated(api):
    """A reader must not report 404 for a design system it simply cannot see.

    The check sits *after* the slug/credential branch, so an unhydrated index
    can never be used as an oracle for whether a slug exists.
    """
    ds_id = _register(api.client).json()["id"]
    designs = api.client.app.state.designs
    designs.hydrated = False
    try:
        r = api.client.get(f"/ds/{ds_id}/bundle")
        assert r.status_code == 503, r.text
        assert "retry" in r.json()["detail"]
        # Still 401 before the lookup: no hydration oracle for a slug.
        assert api.client.get("/ds/corp/bundle").status_code == 401
        # Hydration state is reported by /health, it is not a failure there.
        assert api.client.get("/health").status_code == 200
    finally:
        designs.hydrated = True
    assert api.client.get(f"/ds/{ds_id}/bundle").status_code == 200


def test_stored_bundle_that_no_longer_renders_is_502_not_500(api, monkeypatch):
    """Lowering a token limit after a bundle was stored must not be a 500."""
    from src import main

    ds_id = _register(api.client).json()["id"]
    monkeypatch.setattr(
        main, "settings", dataclasses.replace(main.settings, ds_max_tokens=1)
    )
    main._DS_DERIVED.clear()
    r = api.client.get(f"/ds/{ds_id}/css")
    assert r.status_code == 502, r.text
    assert r.json()["error"] == "stored design system cannot be rendered"
    page = api.client.get(f"/ds/{ds_id}")
    assert page.status_code == 502, page.text
    assert page.json()["error"] == "stored design system cannot be rendered"
    main._DS_DERIVED.clear()
