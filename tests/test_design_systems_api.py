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
    # An unusable credential is not an error on a public read: it is simply
    # no identity, so the catalogue answers with mine: False throughout.
    anon = api.client.get("/api/design-systems", headers={"X-Kbc-Stack": "us"})
    assert anon.status_code == 200
    assert {r["mine"] for r in anon.json()["design_systems"]} == {False}
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


def test_an_id_and_a_slug_read_the_same_way(api):
    """0.20.0: both spellings are public; only a credential makes an answer private."""
    ds_id = _register(api.client).json()["id"]
    assert api.client.get(f"/ds/{ds_id}/bundle").status_code == 200
    assert api.client.get("/ds/corp/bundle").status_code == 200
    # An unknown ref is 404 either way, credential or not.
    assert api.client.get("/ds/does-not-exist/bundle").status_code == 404
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


def test_css_validates_the_mode_of_a_slug_read(api):
    _register(api.client)
    # No credential needed any more; the bad mode is what answers.
    assert api.client.get("/ds/corp/css?mode=sepia").status_code == 422
    assert (
        api.client.get("/ds/corp/css?mode=sepia", headers=AUTH_HEADERS).status_code
        == 422
    )


def test_reader_is_503_while_the_index_is_unhydrated(api):
    """A reader must not report 404 for a design system it simply cannot see.

    404 would be a lie a reader (or an agent following a link) would cache,
    so an unhydrated index answers 503 for every reference it cannot resolve.
    """
    ds_id = _register(api.client).json()["id"]
    designs = api.client.app.state.designs
    designs.hydrated = False
    try:
        r = api.client.get(f"/ds/{ds_id}/bundle")
        assert r.status_code == 503, r.text
        assert "retry" in r.json()["detail"]
        # A slug is no different: 503, never a 404 that would be wrong.
        assert api.client.get("/ds/corp/bundle").status_code == 503
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


# --------------------------------------------------------------------------
# 0.17.0: role variables and CORS on public reads
# --------------------------------------------------------------------------

CORS_ORIGIN = "access-control-allow-origin"
CORS_EXPOSE = "access-control-expose-headers"


def test_id_resolved_reads_are_readable_cross_origin(api):
    """An id is a public capability, so a page on any origin may fetch it."""
    ds_id = _register(api.client).json()["id"]
    for suffix in ("/versions", "/bundle", "/tokens", "/css", "/starter", "/guidance"):
        r = api.client.get(f"/ds/{ds_id}{suffix}")
        assert r.status_code == 200, suffix
        assert r.headers[CORS_ORIGIN] == "*", suffix
        assert r.headers[CORS_EXPOSE] == "X-Hub-Version", suffix


def test_credentialed_reads_are_not_readable_cross_origin(api):
    """A credentialed answer is never shared, whatever the ref looked like."""
    _register(api.client)
    r = api.client.get("/ds/corp/bundle", headers=AUTH_HEADERS)
    assert r.status_code == 200
    assert CORS_ORIGIN not in r.headers and CORS_EXPOSE not in r.headers
    assert r.headers["Cache-Control"] == "private, no-store"


def test_api_routes_are_not_readable_cross_origin(api):
    _register(api.client)
    for path in ("/api/design-systems", "/api/design-systems/corp"):
        r = api.client.get(path, headers=AUTH_HEADERS)
        assert r.status_code == 200, path
        assert CORS_ORIGIN not in r.headers, path


def test_css_carries_the_role_alias_block_in_every_mode(api):
    ds_id = _register(api.client).json()["id"]
    for mode in ("all", "light", "dark"):
        css = api.client.get(f"/ds/{ds_id}/css?mode={mode}").text
        assert "--ds-background:var(--color-bg)" in css, mode
        assert "--ds-chart-1:var(--color-c1)" in css, mode
        assert "--ds-chart-count:2" in css, mode


def test_bundle_reports_the_role_variable_map(api):
    ds_id = _register(api.client).json()["id"]
    body = api.client.get(f"/ds/{ds_id}/bundle").json()
    roles = body["variables"]["roles"]
    assert roles["background"] == "--ds-background"
    assert roles["font_body"] == "--ds-font-body"
    assert roles["chart_palette_2"] == "--ds-chart-2"
    assert roles["chart_palette_count"] == "--ds-chart-count"
    # the token map itself is untouched
    assert body["variables"]["color.bg"] == "--color-bg"


def test_role_map_moves_aside_for_a_token_literally_named_roles(api):
    """A token path 'roles' would collide with the sub-map's key.

    The token map wins — it is the older contract — and the role map moves to
    'role_variables'. Documented in /context so an agent reads the right key.
    """
    bundle = good_bundle()
    bundle["tokens"]["roles"] = {"$type": "color", "$value": "#123456"}
    ds_id = _register(api.client, bundle=bundle).json()["id"]
    body = api.client.get(f"/ds/{ds_id}/bundle").json()
    assert body["variables"]["roles"] == "--roles"
    assert body["variables"]["role_variables"]["background"] == "--ds-background"


# --------------------------------------------------------------------------
# 0.17.0: the public gallery
# --------------------------------------------------------------------------


def test_gallery_json_lists_registered_systems_in_the_public_shape(api):
    ds_id = _register(api.client).json()["id"]
    r = api.client.get("/ds?format=json")
    assert r.status_code == 200
    assert r.headers[CORS_ORIGIN] == "*"
    assert r.headers[CORS_EXPOSE] == "X-Hub-Version"
    assert r.headers["Cache-Control"] == "no-cache"
    rows = r.json()["design_systems"]
    assert [x["id"] for x in rows] == [ds_id]
    row = rows[0]
    assert set(row) == {
        "id", "slug", "name", "description", "owner", "head_version",
        "updated_at", "swatches", "urls", "forked_from",
    }
    assert row["forked_from"] is None
    assert row["slug"] == "corp" and row["name"] == "Corp"
    assert row["head_version"] == 1 and row["updated_at"]
    # the public shape carries no project id and no stack host
    assert set(row["owner"]) == {"project_name"}
    assert set(row["urls"]) == {"page", "bundle", "css", "starter"}
    assert row["urls"]["css"].endswith(f"/ds/{ds_id}/css?v=1")
    assert row["swatches"]["background"] == "#ffffff"
    assert row["swatches"]["text"] == "#111111"
    assert row["swatches"]["accent"] == "#1442e0"
    assert row["swatches"]["chart"] == ["#ff0000", "#00ff00"]


def test_gallery_json_swatch_palette_is_capped(api):
    from src import main

    _register(api.client)
    assert main.settings.ds_gallery_swatches >= 1
    rows = api.client.get("/ds?format=json").json()["design_systems"]
    assert len(rows[0]["swatches"]["chart"]) <= main.settings.ds_gallery_swatches


def test_gallery_skips_a_meta_only_record_and_needs_no_credential(api):
    """Only systems with at least one version are listed."""
    ds_id = _register(api.client).json()["id"]
    second = _register(api.client, slug="other").json()["id"]
    api.client.delete(f"/api/design-systems/{second}", headers=AUTH_HEADERS)
    rows = api.client.get("/ds?format=json").json()["design_systems"]
    assert [x["id"] for x in rows] == [ds_id]


def test_gallery_page_is_hub_chrome_listing_every_system(api):
    ds_id = _register(api.client).json()["id"]
    r = api.client.get("/ds")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    page = r.text
    assert f'href="https://testserver/ds/{ds_id}"' in page
    assert "Corp" in page and "corp" in page and "Brand" in page
    assert "background:#1442e0" in page and 'title="accent"' in page
    assert f'href="https://testserver/ds/{ds_id}/bundle?v=1"' in page
    assert "KBC Artifact Hub" in page


def test_gallery_page_has_an_empty_state(api):
    page = api.client.get("/ds").text
    assert "No design system" in page


def test_gallery_answers_503_while_the_index_is_unhydrated(api):
    from src import main

    designs = main.app.state.designs
    designs.hydrated = False
    try:
        assert api.client.get("/ds").status_code == 503
        assert api.client.get("/ds?format=json").status_code == 503
    finally:
        designs.hydrated = True


def test_gallery_route_wins_over_the_ref_route(api):
    """`/ds` is the gallery, never a design system whose id is 'ds'."""
    from src import main

    paths = [r.path for r in main.app.routes if getattr(r, "path", "") == "/ds"]
    assert paths == ["/ds"]
    assert api.client.get("/ds").status_code == 200


def test_context_documents_the_gallery_and_the_role_variables(api):
    body = api.client.get("/context").json()
    paths = {(e["method"], e["path"]) for e in body["endpoints"]}
    assert ("GET", "/ds") in paths and ("GET", "/ds?format=json") in paths
    ds = body["design_systems"]
    assert "/ds" in ds["gallery"]
    prose = ds["role_variables"]
    assert "--ds-" in prose and "variables.roles" in prose
    assert "role_variables" in prose  # the collision rule is documented


def test_llms_txt_names_the_public_gallery(api):
    text = api.client.get("/llms.txt").text
    assert "/ds)" in text or "/ds " in text
    assert "gallery" in text.lower()


# --------------------------------------------------------------------------
# Fix round 1: colour validation, X-Hub-Version, bounded gallery
# --------------------------------------------------------------------------


def _bundle_with_background(colour):
    bundle = good_bundle()
    bundle["tokens"]["color"] = dict(bundle["tokens"]["color"])
    bundle["tokens"]["color"]["bg"] = {"$value": colour}
    return bundle


def test_gallery_drops_a_swatch_that_is_not_a_plain_colour(api):
    """A token value is author-controlled text; the strip is on the hub origin.

    html.escape stops attribute breakout, but ';' ':' '(' ')' survive it, so
    an unvalidated value could add declarations of its own to the inline
    style. The value is dropped server-side, so the JSON the demo consumes is
    clean too -- and the card still renders, just without that chip.
    """
    # src.tokens already refuses ';', '}' and '<' at submit time, so the
    # reachable value is one that needs none of them: a url() makes the hub's
    # own gallery fetch a remote resource for every anonymous visitor.
    injection = "url(https://attacker.example/pixel)"
    registered = _register(api.client, bundle=_bundle_with_background(injection))
    assert registered.status_code == 201, registered.text
    ds_id = registered.json()["id"]
    row = api.client.get("/ds?format=json").json()["design_systems"][0]
    assert "background" not in row["swatches"]
    # the other roles are untouched
    assert row["swatches"]["accent"] == "#1442e0"
    page = api.client.get("/ds").text
    assert "attacker.example" not in page
    assert f'href="https://testserver/ds/{ds_id}"' in page


def test_gallery_keeps_a_functional_colour_notation(api):
    _register(api.client, bundle=_bundle_with_background("rgb(18 52 86 / 0.5)"))
    row = api.client.get("/ds?format=json").json()["design_systems"][0]
    assert row["swatches"]["background"] == "rgb(18 52 86 / 0.5)"


def test_public_reads_send_the_version_they_expose(api):
    """Exposing X-Hub-Version is only useful if it is actually sent."""
    from src import main

    ds_id = _register(api.client).json()["id"]
    for suffix in ("/versions", "/bundle", "/tokens", "/css", "/starter", "/guidance"):
        r = api.client.get(f"/ds/{ds_id}{suffix}")
        assert r.headers["X-Hub-Version"] == main.SERVICE_VERSION, suffix
        assert r.headers[CORS_EXPOSE] == "X-Hub-Version", suffix
    gallery = api.client.get("/ds?format=json")
    assert gallery.headers["X-Hub-Version"] == main.SERVICE_VERSION


def test_the_style_guide_page_is_not_a_cross_origin_read(api):
    """CORS is for machine reads; the HTML page has no cross-origin consumer."""
    ds_id = _register(api.client).json()["id"]
    r = api.client.get(f"/ds/{ds_id}")
    assert r.status_code == 200
    assert CORS_ORIGIN not in r.headers and CORS_EXPOSE not in r.headers


def test_gallery_is_bounded_and_says_when_it_truncated(api, monkeypatch):
    from src import main

    def cap(n):
        monkeypatch.setattr(
            main, "settings", dataclasses.replace(main.settings, ds_gallery_max_rows=n)
        )

    cap(2)
    for slug in ("one", "two", "three"):
        assert _register(api.client, slug=slug).status_code == 201
    body = api.client.get("/ds?format=json").json()
    assert len(body["design_systems"]) == 2
    assert body["truncated"] is True

    cap(50)
    body = api.client.get("/ds?format=json").json()
    assert len(body["design_systems"]) == 3
    assert body["truncated"] is False


def test_gallery_page_is_kept_out_of_search_indexes(api):
    assert api.client.get("/ds").headers["X-Robots-Tag"] == "noindex, nofollow"


def test_gallery_refuses_a_format_it_does_not_serve(api):
    assert api.client.get("/ds?format=xml").status_code == 422
    assert api.client.get("/ds?format=").status_code == 200
    assert api.client.get("/ds").status_code == 200
    assert api.client.get("/ds?format=json").status_code == 200


def test_context_publishes_the_gallery_limits(api):
    from src import main

    limits = api.client.get("/context").json()["limits"]
    assert limits["ds_gallery_swatches"] == main.settings.ds_gallery_swatches
    assert limits["ds_gallery_max_rows"] == main.settings.ds_gallery_max_rows


# --------------------------------------------------------------------------
# 0.19.0: /ds is a front door, not just a list
# --------------------------------------------------------------------------


def _with_switcher(monkeypatch, url="https://hub.example/a/SwItChEr"):
    from src import main

    monkeypatch.setattr(
        main, "settings", dataclasses.replace(main.settings, style_switcher_url=url)
    )
    return url


def test_ds_page_leads_with_a_hero_that_pitches_the_feature(api):
    page = api.client.get("/ds").text
    assert "<h1>Design systems</h1>" in page
    assert "Register your brand once" in page
    assert 'href="https://testserver/skill"' in page


def test_ds_page_carries_the_three_why_cards(api):
    page = api.client.get("/ds").text
    for heading in ("Say it once", "Never drifts", "Presented, not just stored"):
        assert f"<h3>{heading}</h3>" in page


def test_ds_page_has_a_how_it_works_strip(api):
    page = api.client.get("/ds").text
    for step in ("Register", "Agent lists &amp; picks", "Starter + components",
                 "Publish with provenance"):
        assert step in page
    # 0.20.0: the catalogue is public, and the page must not say otherwise.
    assert "Keboola credential required" not in page
    assert "no credential needed" in page
    assert 'href="https://testserver/context"' in page


def test_ds_page_points_agents_at_their_own_entry_points(api):
    page = api.client.get("/ds").text
    for path in ("/skill", "/agent", "/llms.txt"):
        assert f'href="https://testserver{path}"' in page


def test_ds_page_still_lists_the_gallery_and_its_empty_state(api):
    assert "No design system" in api.client.get("/ds").text
    ds_id = _register(api.client).json()["id"]
    page = api.client.get("/ds").text
    assert f'href="https://testserver/ds/{ds_id}"' in page
    assert "background:#1442e0" in page


def test_ds_page_omits_the_switcher_when_none_is_configured(api):
    from src import main

    assert main.settings.style_switcher_url is None
    page = api.client.get("/ds").text
    assert 'class="ds-switcher"' not in page
    assert "<iframe" not in page
    assert "see it live" not in page


def test_ds_page_embeds_the_switcher_raw_in_an_opaque_sandbox(api, monkeypatch):
    url = _with_switcher(monkeypatch)
    page = api.client.get("/ds").text
    assert f'src="{url}/raw"' in page
    assert 'class="ds-switcher"' in page
    frame = page.split('class="ds-switcher"', 1)[1].split(">", 1)[0]
    assert "sandbox=" in frame
    assert "allow-same-origin" not in frame
    assert "One document, ten looks" in page
    assert f'href="{url}">Open full screen' in page


def test_ds_page_hero_offers_the_switcher_only_when_configured(api, monkeypatch):
    assert "Try the live switcher" not in api.client.get("/ds").text
    _with_switcher(monkeypatch)
    assert "Try the live switcher" in api.client.get("/ds").text


def test_ds_page_hero_offers_the_walkthrough_only_when_configured(api, monkeypatch):
    from src import main

    assert "Read the walkthrough" not in api.client.get("/ds").text
    monkeypatch.setattr(
        main,
        "settings",
        dataclasses.replace(main.settings, design_demo_url="https://hub.example/a/Walk"),
    )
    page = api.client.get("/ds").text
    assert "Read the walkthrough" in page
    assert 'href="https://hub.example/a/Walk"' in page


def test_landing_page_links_the_design_systems_front_door(api):
    page = api.client.get("/").text
    hero = page.split('<div class="hero-links">', 1)[1].split("</div>", 1)[0]
    assert '<a class="primary" href="https://testserver/ds">Design systems</a>' in hero


def test_style_switcher_url_comes_from_the_environment(monkeypatch):
    from src import config

    monkeypatch.delenv("HUB_STYLE_SWITCHER_URL", raising=False)
    assert config.load_settings().style_switcher_url is None
    monkeypatch.setenv("HUB_STYLE_SWITCHER_URL", " https://hub.example/a/S ")
    assert config.load_settings().style_switcher_url == "https://hub.example/a/S"


# --------------------------------------------------------------------------
# 0.20.0: reading the catalogue needs no credential
# (spec: docs/superpowers/specs/2026-09-16-design-systems-0.20-amendment.md)
# --------------------------------------------------------------------------


def test_catalogue_list_is_readable_without_a_credential(api):
    """Anonymous callers see the catalogue; only `mine` needs a credential."""
    ds_id = _register(api.client).json()["id"]
    r = api.client.get("/api/design-systems")
    assert r.status_code == 200, r.text
    rows = r.json()["design_systems"]
    assert [x["id"] for x in rows] == [ds_id]
    assert rows[0]["mine"] is False
    # An anonymous answer is the same for everyone, so a shared cache may keep
    # it -- unlike the credentialed one below.
    assert r.headers["Cache-Control"] == "no-cache"

    mine = api.client.get("/api/design-systems", headers=AUTH_HEADERS)
    assert mine.json()["design_systems"][0]["mine"] is True
    assert mine.headers["Cache-Control"] == "private, no-store"
    other = api.client.get("/api/design-systems", headers=OTHER_AUTH_HEADERS)
    assert other.json()["design_systems"][0]["mine"] is False


def test_catalogue_detail_is_readable_without_a_credential(api):
    ds_id = _register(api.client).json()["id"]
    for ref in (ds_id, "corp"):
        r = api.client.get(f"/api/design-systems/{ref}")
        assert r.status_code == 200, ref
        body = r.json()
        assert body["id"] == ds_id and body["mine"] is False
        assert [v["version"] for v in body["versions"]] == [1]
        assert r.headers["Cache-Control"] == "no-cache"
    assert (
        api.client.get(f"/api/design-systems/{ds_id}", headers=AUTH_HEADERS).json()[
            "mine"
        ]
        is True
    )
    assert api.client.get("/api/design-systems/nope").status_code == 404


def _make_meta_only(api, ds_id):
    """Strip the version pointers, as a registration that died mid-flight."""
    api.client.app.state.designs._index[ds_id].versions.clear()


def test_meta_only_record_stays_the_owners_alone(api):
    ds_id = _register(api.client).json()["id"]
    _make_meta_only(api, ds_id)
    assert api.client.get(f"/api/design-systems/{ds_id}").status_code == 404
    assert (
        api.client.get(
            f"/api/design-systems/{ds_id}", headers=OTHER_AUTH_HEADERS
        ).status_code
        == 404
    )
    owner = api.client.get(f"/api/design-systems/{ds_id}", headers=AUTH_HEADERS)
    assert owner.status_code == 200 and owner.json()["head_version"] is None


def test_slug_reads_need_no_credential_and_are_cross_origin(api):
    """0.20.0 retires the 401-before-lookup rule: the gallery lists every slug."""
    _register(api.client)
    r = api.client.get("/ds/corp/bundle")
    assert r.status_code == 200, r.text
    assert r.headers[CORS_ORIGIN] == "*"
    assert r.headers[CORS_EXPOSE] == "X-Hub-Version"
    assert r.headers["Cache-Control"] == "no-cache"
    for suffix in ("/versions", "/tokens", "/css", "/starter", "/guidance"):
        anon = api.client.get(f"/ds/corp{suffix}")
        assert anon.status_code == 200, suffix
        assert anon.headers[CORS_ORIGIN] == "*", suffix
    # A slug that does not exist is now plainly 404, like an unknown id.
    assert api.client.get("/ds/does-not-exist/bundle").status_code == 404
    assert api.client.get("/ds/Not_A_Slug/bundle").status_code == 404
    # The HTML style guide is reachable by slug too.
    assert api.client.get("/ds/corp").status_code == 200


def test_an_id_read_with_a_credential_is_private(api):
    """The rule is about the credential, not about how the ref was spelled."""
    ds_id = _register(api.client).json()["id"]
    r = api.client.get(f"/ds/{ds_id}/bundle", headers=AUTH_HEADERS)
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "private, no-store"
    assert CORS_ORIGIN not in r.headers


# --------------------------------------------------------------------------
# 0.20.0: fork
# --------------------------------------------------------------------------


def _fork(client, ref="corp", headers=OTHER_AUTH_HEADERS, **body):
    return client.post(
        f"/api/design-systems/{ref}/fork",
        json={"slug": "mine", **body},
        headers=headers,
    )


def test_fork_copies_the_bundle_into_a_new_owned_system(api):
    src = _register(api.client).json()
    r = _fork(api.client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] != src["id"] and body["id"].startswith("ds_")
    assert body["slug"] == "mine"
    assert body["name"] == "Corp (fork)"
    assert body["description"] == "Brand"
    assert body["version"] == 1 and body["head_version"] == 1
    assert body["mine"] is True
    assert body["owner"]["project_id"] == 999
    assert body["forked_from"] == {
        "id": src["id"],
        "slug": "corp",
        "version": 1,
    }
    # the copy is byte-for-byte the source bundle, warnings included
    copy_bundle = api.client.get(f"/ds/{body['id']}/bundle").json()
    source_bundle = api.client.get(f"/ds/{src['id']}/bundle").json()
    assert copy_bundle["bundle"] == source_bundle["bundle"]
    assert api.client.get(f"/api/design-systems/{body['id']}").json()["versions"][0][
        "warnings_count"
    ] == 1

    # the source is untouched: same owner, same head, no forked_from
    after = api.client.get(f"/api/design-systems/{src['id']}").json()
    assert after["owner"]["project_id"] == 123
    assert after["head_version"] == 1 and after["versions_count"] == 1
    assert after["forked_from"] is None


def test_fork_takes_an_explicit_name_description_note_and_version(api):
    src = _register(api.client).json()
    second = good_bundle()
    second["guidance"] = "# Corp v2"
    assert api.client.post(
        f"/api/design-systems/{src['id']}/versions",
        json={"bundle": second, "note": "v2"},
        headers=AUTH_HEADERS,
    ).status_code == 201
    r = _fork(
        api.client,
        name="Ours",
        description="Our take",
        note="forked at v1",
        version=1,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Ours" and body["description"] == "Our take"
    assert body["forked_from"]["version"] == 1
    assert api.client.get(f"/ds/{body['id']}/guidance").text.startswith("# Corp\n")
    # ...and forking the head picks v2
    head = _fork(api.client, slug="mine2").json()
    assert head["forked_from"]["version"] == 2
    assert api.client.get(f"/ds/{head['id']}/guidance").text.startswith("# Corp v2")
    assert _fork(api.client, slug="mine3", version=99).status_code == 404
    assert _fork(api.client, slug="mine4", version=0).status_code == 422


def test_fork_refuses_a_taken_slug_and_a_bad_one(api):
    _register(api.client)
    assert _fork(api.client, slug="corp").status_code == 409
    assert _fork(api.client, slug="Nope").status_code == 422
    assert _fork(api.client, slug="ds_x").status_code == 422
    assert _fork(api.client, ref="nothing-here").status_code == 404
    # a credential is required, unlike a read
    assert _fork(api.client, headers={"X-Kbc-Stack": "us"}).status_code == 401


def test_fork_counts_against_the_forkers_own_cap(api, monkeypatch):
    from src import main

    _register(api.client)
    monkeypatch.setattr(
        main, "settings", dataclasses.replace(main.settings, ds_max_per_project=1)
    )
    assert _fork(api.client, slug="one").status_code == 201
    r = _fork(api.client, slug="two")
    assert r.status_code == 429, r.text
    # the source owner's own slot is untouched by the forker's cap
    assert _register(api.client, slug="corp2").status_code == 429  # owner also at 1


def test_forking_grants_no_authority_over_the_source(api):
    src = _register(api.client).json()
    forked = _fork(api.client).json()
    assert api.client.put(
        f"/api/design-systems/{src['id']}",
        json={"name": "Hijacked"},
        headers=OTHER_AUTH_HEADERS,
    ).status_code == 403
    assert api.client.post(
        f"/api/design-systems/{src['id']}/versions",
        json={"bundle": good_bundle()},
        headers=OTHER_AUTH_HEADERS,
    ).status_code == 403
    assert api.client.delete(
        f"/api/design-systems/{src['id']}", headers=OTHER_AUTH_HEADERS
    ).status_code == 403
    assert api.client.get(f"/api/design-systems/{src['id']}").json()["name"] == "Corp"
    # but the forker owns the copy outright
    assert api.client.put(
        f"/api/design-systems/{forked['id']}",
        json={"name": "Ours"},
        headers=OTHER_AUTH_HEADERS,
    ).status_code == 200


def test_fork_provenance_survives_a_rebuild_and_reaches_the_gallery(api):
    src = _register(api.client).json()
    forked = _fork(api.client).json()
    api.client.app.state.designs.hydrate()
    again = api.client.get(f"/api/design-systems/{forked['id']}").json()
    assert again["forked_from"] == {"id": src["id"], "slug": "corp", "version": 1}
    rows = {
        x["slug"]: x for x in api.client.get("/ds?format=json").json()["design_systems"]
    }
    assert rows["mine"]["forked_from"]["slug"] == "corp"
    assert rows["corp"]["forked_from"] is None
    assert "forked from" in api.client.get("/ds").text


def test_a_bundle_that_no_longer_validates_is_still_forkable(api, monkeypatch):
    """The source proves it validated; a limit lowered afterwards must not bite."""
    from src import main

    _register(api.client)
    monkeypatch.setattr(
        main, "settings", dataclasses.replace(main.settings, ds_max_tokens=1)
    )
    assert _fork(api.client).status_code == 201


def test_context_documents_the_fork_route(api):
    body = api.client.get("/context").json()
    by_path = {
        (e["method"], e["path"]): e for e in body["endpoints"]
    }
    entry = by_path[("POST", "/api/design-systems/{ref}/fork")]
    assert "token" in entry["auth"]
    assert "fork" in entry["purpose"].lower()
    assert "fork" in body["design_systems"]


# --------------------------------------------------------------------------
# 0.20.0, fix round 1
# --------------------------------------------------------------------------


def test_an_unusable_credential_on_a_public_read_is_simply_anonymous(api):
    """`mine` is the only credentialed field, so a bad token is no error."""
    _register(api.client)
    bad = {"X-StorageApi-Token": "rejected-token", "X-Kbc-Stack": "us"}
    r = api.client.get("/api/design-systems", headers=bad)
    assert r.status_code == 200, r.text
    assert r.json()["design_systems"][0]["mine"] is False
    one = api.client.get("/api/design-systems/corp", headers=bad)
    assert one.status_code == 200 and one.json()["mine"] is False
    # a malformed project header is no different
    malformed = {**AUTH_HEADERS, "X-Storage-Project": "not-a-number"}
    assert api.client.get("/api/design-systems", headers=malformed).status_code == 200


def test_anonymous_catalogue_reads_are_readable_cross_origin(api):
    """A browser page must be able to read the list that points at the bundles."""
    _register(api.client)
    for path in ("/api/design-systems", "/api/design-systems/corp"):
        from src import main

        r = api.client.get(path)
        assert r.status_code == 200, path
        assert r.headers[CORS_ORIGIN] == "*", path
        assert r.headers[CORS_EXPOSE] == "X-Hub-Version", path
        assert r.headers["X-Hub-Version"] == main.SERVICE_VERSION, path
        credentialed = api.client.get(path, headers=AUTH_HEADERS)
        assert credentialed.headers["Cache-Control"] == "private, no-store", path
        assert CORS_ORIGIN not in credentialed.headers, path


def test_catalogue_is_503_while_the_index_is_unhydrated(api):
    """404-shaped silence would be a lie; the gallery already answers 503."""
    _register(api.client)
    designs = api.client.app.state.designs
    designs.hydrated = False
    try:
        assert api.client.get("/api/design-systems").status_code == 503
        assert api.client.get("/api/design-systems/corp").status_code == 503
    finally:
        designs.hydrated = True
    assert api.client.get("/api/design-systems").status_code == 200


def test_fork_validates_the_slug_before_resolving_the_source(api):
    """Registration checks the slug first; forking must answer the same way."""
    _register(api.client)
    r = _fork(api.client, slug="Nope", version=99)
    assert r.status_code == 422, r.text
    assert _fork(api.client, ref="nothing-here", slug="Nope").status_code == 422


def test_fork_keeps_the_source_versions_schema(api):
    """A copy is a copy: the envelope's schema stamp travels with the bundle."""
    src = _register(api.client).json()
    store = api.client.app.state.designs
    # The store hands out the cached envelope itself, so stamping it here is
    # what a version written by another schema generation would look like.
    stored = store.get_version(src["id"], 1)
    stored.schema += 7
    forked = _fork(api.client).json()
    assert store.get_version(forked["id"], 1).schema == stored.schema


def test_fork_of_a_meta_only_source_is_404(api):
    ds_id = _register(api.client).json()["id"]
    _make_meta_only(api, ds_id)
    assert _fork(api.client, ref=ds_id).status_code == 404


def test_fork_body_cannot_dictate_its_own_provenance(api):
    src = _register(api.client).json()
    forked = _fork(
        api.client, forked_from={"id": "ds_lies", "slug": "lies", "version": 9}
    )
    assert forked.status_code == 201
    assert forked.json()["forked_from"] == {
        "id": src["id"],
        "slug": "corp",
        "version": 1,
    }


def test_editing_a_fork_preserves_its_provenance(api):
    src = _register(api.client).json()
    forked = _fork(api.client).json()
    r = api.client.put(
        f"/api/design-systems/{forked['id']}",
        json={"name": "Ours", "description": "Changed"},
        headers=OTHER_AUTH_HEADERS,
    )
    assert r.status_code == 200, r.text
    assert r.json()["forked_from"] == {"id": src["id"], "slug": "corp", "version": 1}
    api.client.app.state.designs.hydrate()
    assert api.client.get(f"/api/design-systems/{forked['id']}").json()[
        "forked_from"
    ]["id"] == src["id"]
