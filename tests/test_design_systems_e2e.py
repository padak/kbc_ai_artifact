"""Acceptance: register -> restart with an empty cache -> discover -> build ->
publish with provenance -> revise."""

from tests.test_api import AUTH_HEADERS, api  # noqa: F401
from tests.test_designs import good as good_bundle


def test_fresh_session_agent_flow(api):
    client = api.client
    created = client.post(
        "/api/design-systems",
        json={"slug": "corp", "name": "Corp", "bundle": good_bundle()},
        headers=AUTH_HEADERS,
    )
    assert created.status_code == 201
    ds_id = created.json()["id"]

    # "restart": wipe the design-system disk cache and rebuild the index from
    # Storage tags alone. Only the design-system cache files are removed, not
    # the whole cache directory: the state sidecar keeps a live SQLite
    # connection to a file in there, and pulling it out from under that
    # connection wedges the app's own shutdown — an artefact of the test, not
    # of a real restart, which starts from a cold process.
    for path in api.settings.cache_dir.glob("ds.*"):
        path.unlink()
    client.app.state.hydrated = False
    client.app.state.designs.hydrate()

    catalogue = client.get("/api/design-systems", headers=AUTH_HEADERS).json()[
        "design_systems"
    ]
    chosen = next(r for r in catalogue if r["slug"] == "corp")
    detail = client.get(
        f"/api/design-systems/{chosen['slug']}", headers=AUTH_HEADERS
    ).json()
    n = detail["versions"][0]["version"]
    bundle = client.get(f"/ds/{ds_id}/bundle?v={n}").json()
    starter = client.get(f"/ds/{ds_id}/starter?v={n}").text
    kpi = next(c for c in bundle["bundle"]["components"] if c["name"] == "kpi-card")
    doc = starter.replace("{{TITLE}}", "Q3 report", 1).replace(
        "{{BODY}}", f"<h1>Q3 report</h1>{kpi['html']}", 1
    )

    published = client.post(
        "/api/artifacts",
        json={
            "html": doc,
            "markdown_source": "# Q3 report\n\nKPI: 42",
            "design_system": f"{ds_id}@{n}",
        },
        headers=AUTH_HEADERS,
    )
    assert published.status_code == 201
    art = published.json()
    assert art["design_system"] == {"id": ds_id, "slug": "corp", "version": n}
    raw = client.get(f"/a/{art['share_id']}/raw").text
    assert kpi["html"] in raw and "--color-accent" in raw

    meta = client.get(f"/a/{art['share_id']}/meta").json()
    same = meta["design_system"]
    revised = client.post(
        f"/api/artifacts/{art['id']}/versions",
        json={
            "html": doc.replace("42", "43"),
            "design_system": f"{same['id']}@{same['version']}",
        },
        headers=AUTH_HEADERS,
    )
    assert revised.status_code == 201 and revised.json()["design_system"] == same

    client.post(
        f"/api/design-systems/{ds_id}/versions",
        json={"bundle": good_bundle()},
        headers=AUTH_HEADERS,
    )
    assert (
        client.delete(
            f"/api/design-systems/{ds_id}/versions/{n}", headers=AUTH_HEADERS
        ).status_code
        == 204
    )
    assert client.get(f"/ds/{ds_id}/bundle?v={n}").status_code == 404
    # The artifact's provenance record survives the deletion of what it names.
    assert client.get(f"/a/{art['share_id']}/meta").json()["design_system"] == same
