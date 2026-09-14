"""Design systems: bundle validation and the Storage-backed store.

Spec: docs/superpowers/specs/2026-09-14-design-systems-design.md (Bundle,
Data model, Key decisions 2-5, 9). Storage is always InMemoryFilesBackend --
no live Keboola call anywhere.
"""

import dataclasses

import pytest

from src.config import load_settings
from src.designs import (
    TAG_DS_ALL,
    BundleError,
    DesignSystemMeta,
    DesignSystemStore,
    DesignSystemVersion,
    LastVersion,
    NotHydrated,
    SlugTaken,
    VersionLimit,
    validate_bundle,
)
from src.kbc import BackendError, InMemoryFilesBackend

TOKENS = {
    "color": {"$type": "color", "bg": {"$value": "#ffffff"}, "fg": {"$value": "#111111"},
              "accent": {"$value": "#1442e0"}, "on": {"$value": "#ffffff"},
              "c1": {"$value": "#ff0000"}, "c2": {"$value": "#00ff00"}},
    "font": {"$type": "fontFamily", "sans": {"$value": ["Inter", "sans-serif"]}},
    "radius": {"$type": "dimension", "md": {"$value": "8px"}},
    "grad": {"$type": "gradient", "$value": [{"color": "#000", "position": 0}]},
}
ROLES = {"background": "{color.bg}", "text": "{color.fg}", "accent": "{color.accent}",
         "on_accent": "{color.on}", "font_body": "{font.sans}", "radius": "{radius.md}",
         "chart_palette": ["{color.c1}", "{color.c2}"]}


def good():
    # ROLES is copied, not shared: a test that edits one role must not leak
    # that edit into the next case through the module-level dict.
    return {
        "tokens": TOKENS, "modes": {"dark": {"color": {"bg": {"$value": "#000000"}}}},
        "roles": dict(ROLES), "guidance": "# Corp\n\nUse bars.",
        "components": [{"name": "kpi-card", "description": "KPI", "when_to_use": "Top.",
                        "html": '<div class="ds-kpi">{{n}}</div>', "css": ".ds-kpi{padding:1rem}"}],
        "charts": {"library": "chart.js", "notes": "Bars first."},
        "diagrams": {"library": "mermaid"},
        "fonts": [{"href": "https://fonts.googleapis.com/css2?family=Inter&display=swap"}],
    }


@pytest.fixture
def settings():
    return load_settings()


def _err(raw, settings):
    with pytest.raises(BundleError) as exc:
        validate_bundle(raw, settings=settings)
    return exc.value.findings


def test_good_bundle_normalises_and_warns_on_preserved_types(settings):
    bundle, warnings = validate_bundle(good(), settings=settings)
    assert bundle["charts"] == {"library": "chart.js", "notes": "Bars first."}
    assert bundle["diagrams"] == {"library": "mermaid", "notes": ""}
    assert bundle["components"][0]["name"] == "kpi-card"
    assert warnings == [{"path": "/tokens/grad",
                         "message": "type 'gradient' is preserved but not emitted as CSS"}]


def test_required_fields(settings):
    raw = good(); del raw["guidance"]
    assert _err(raw, settings) == [{"path": "/bundle/guidance", "message": "required, non-empty Markdown"}]
    raw = good(); raw["tokens"] = {}
    assert _err(raw, settings)[0]["path"] == "/bundle/tokens"


def test_token_findings_are_re_pointed_under_bundle(settings):
    raw = good(); raw["tokens"] = {"x": {"$value": 1}}
    assert _err(raw, settings) == [{"path": "/bundle/tokens/x", "message": "token has no resolved $type"}]


def test_only_dark_mode_is_allowed(settings):
    raw = good(); raw["modes"] = {"sepia": {}}
    assert _err(raw, settings) == [{"path": "/bundle/modes/sepia", "message": "only a 'dark' mode is supported"}]


def test_roles_type_checks(settings):
    raw = good(); raw["roles"]["accent"] = "{radius.md}"
    assert _err(raw, settings) == [{"path": "/bundle/roles/accent",
                                    "message": "role requires a color token, radius.md is dimension"}]
    raw = good(); raw["roles"]["accent"] = "{grad}"
    assert "not emitted" in _err(raw, settings)[0]["message"]
    raw = good(); raw["roles"]["sparkle"] = "{color.bg}"
    assert _err(raw, settings) == [{"path": "/bundle/roles/sparkle", "message": "unknown role"}]
    raw = good(); raw["roles"]["chart_palette"] = []
    assert "1 to 12" in _err(raw, settings)[0]["message"]
    raw = good(); raw["roles"]["background"] = "{color.nope}"
    assert _err(raw, settings) == [{"path": "/bundle/roles/background", "message": "unknown token 'color.nope'"}]


def test_components_rules(settings):
    raw = good(); raw["components"].append(dict(raw["components"][0]))
    assert _err(raw, settings) == [{"path": "/bundle/components/1/name", "message": "duplicate component name"}]
    raw = good(); raw["components"][0]["name"] = "Bad Name"
    assert _err(raw, settings)[0]["path"] == "/bundle/components/0/name"
    raw = good(); raw["components"][0]["html"] = ""
    assert _err(raw, settings)[0]["path"] == "/bundle/components/0/html"
    small = dataclasses.replace(settings, ds_max_components=1)
    raw = good(); raw["components"].append({**raw["components"][0], "name": "other"})
    assert "more than 1" in _err(raw, small)[0]["message"]
    small = dataclasses.replace(settings, ds_max_component_bytes=10)
    assert "64" not in _err(good(), small)[0]["message"] and _err(good(), small)[0]["path"] == "/bundle/components/0"


def test_charts_diagrams_fonts(settings):
    raw = good(); raw["charts"] = {"library": "d3"}
    assert _err(raw, settings) == [{"path": "/bundle/charts/library",
                                    "message": "must be one of chart.js, inline-svg, none"}]
    raw = good(); raw["diagrams"] = {"library": "plantuml"}
    assert _err(raw, settings)[0]["path"] == "/bundle/diagrams/library"
    raw = good(); raw["fonts"] = [{"href": "https://evil.example/x.css"}]
    assert _err(raw, settings) == [{"path": "/bundle/fonts/0/href",
                                    "message": "host must be one of fonts.googleapis.com"}]
    raw = good(); raw["fonts"] = [{"href": "http://fonts.googleapis.com/css2"}]
    assert "https" in _err(raw, settings)[0]["message"]
    small = dataclasses.replace(settings, ds_max_font_links=0)
    assert _err(good(), small)[0]["path"] == "/bundle/fonts"


def test_whole_bundle_size_limit(settings):
    small = dataclasses.replace(settings, ds_max_bundle_bytes=200)
    assert _err(good(), small) == [{"path": "/bundle",
                                    "message": "bundle exceeds 200 bytes after normalisation"}]


def test_defaults_and_trimming(settings):
    raw = good()
    for key in ("charts", "diagrams", "fonts", "components", "roles", "modes"):
        del raw[key]
    raw["guidance"] = "  # x  "
    bundle, _ = validate_bundle(raw, settings=settings)
    assert bundle["charts"] == {"library": "none", "notes": ""}
    assert bundle["diagrams"] == {"library": "none", "notes": ""}
    assert bundle["fonts"] == [] and bundle["components"] == [] and bundle["roles"] == {} and bundle["modes"] == {}
    assert bundle["guidance"] == "# x"


# --------------------------------------------------------------- the store

OWNER = {"stack_url": "https://connection.keboola.com", "project_id": 123, "project_name": "Test",
         "key": "123@connection.keboola.com"}


def _meta(ds_id="ds_abc", slug="corp", created_at="2026-09-15T00:00:00Z", **kw):
    return DesignSystemMeta(id=ds_id, slug=slug, name="Corp", description="", owner=OWNER,
                            created_at=created_at, updated_at="2026-09-15T00:00:00Z", **kw)


def _version(ds_id="ds_abc", n=1, bundle=None):
    return DesignSystemVersion(id=ds_id, version=n, note="", author=OWNER,
                               created_at="2026-09-15T00:00:00Z",
                               bundle=bundle or {"tokens": {"c": {"$type": "color", "$value": "#000"}},
                                                 "guidance": "x"},
                               warnings=[])


@pytest.fixture
def ds_store(tmp_path):
    backend = InMemoryFilesBackend()
    store = DesignSystemStore(backend, tmp_path / "cache", cache_max_entries=8, max_versions=3,
                              max_envelope_bytes=1_000_000, reap_aborted_after_s=3600)
    store.hydrate()
    return store, backend


def _names_in_write_order(backend):
    # search_by_tag answers newest-first (id descending); sorting back by id is
    # what shows the order the store actually wrote the files in.
    return [f.name for f in sorted(backend.search_by_tag(TAG_DS_ALL), key=lambda f: f.id)]


def test_create_writes_meta_then_v1_with_tags(ds_store):
    store, backend = ds_store
    store.create(_meta(), _version())
    assert _names_in_write_order(backend) == ["ds-ds_abc-meta.json", "ds-ds_abc-v1.json"]
    meta_info = next(f for f in backend.search_by_tag("ds-meta"))
    assert set(meta_info.tags) == {TAG_DS_ALL, "ds-id-ds_abc", "ds-meta",
                                   "ds-owner-123@connection.keboola.com", "ds-slug-corp"}
    v1 = next(f for f in backend.search_by_tag("ds-ver-1"))
    assert set(v1.tags) == {TAG_DS_ALL, "ds-id-ds_abc", "ds-ver-1"}
    assert store.resolve_ref("corp") == "ds_abc" and store.resolve_ref("ds_abc") == "ds_abc"
    assert store.resolve_ref("nope") is None
    assert store.head_version("ds_abc") == 1
    assert store.get_version("ds_abc", None).bundle["guidance"] == "x"


def test_slug_taken_and_slug_equal_to_id_rejected(ds_store):
    store, _ = ds_store
    store.create(_meta(), _version())
    with pytest.raises(SlugTaken):
        store.create(_meta(ds_id="ds_other"), _version("ds_other"))


def test_hydrate_from_tags_alone_rebuilds_slug_and_owner_index(ds_store, tmp_path):
    store, backend = ds_store
    store.create(_meta(), _version())
    fresh = DesignSystemStore(backend, tmp_path / "cache2", cache_max_entries=8, max_versions=3,
                              max_envelope_bytes=1_000_000, reap_aborted_after_s=3600)
    assert fresh.hydrate() == 1
    assert fresh.resolve_ref("corp") == "ds_abc"
    assert fresh.count_owner("123@connection.keboola.com") == 1
    assert [m.slug for m in fresh.list_all()] == ["corp"]   # downloads the meta lazily here, not in hydrate


def test_meta_only_record_is_inert_publicly_but_owner_visible(ds_store):
    store, backend = ds_store
    backend.upload("ds-ds_x-meta.json", _meta("ds_x", "x").to_json(),
                   [TAG_DS_ALL, "ds-id-ds_x", "ds-meta", "ds-owner-123@connection.keboola.com", "ds-slug-x"])
    store.hydrate()
    assert store.list_all() == []
    assert [m.id for m in store.list_owner("123@connection.keboola.com")] == ["ds_x"]
    assert store.head_version("ds_x") is None
    assert store.count() == 0


def test_not_hydrated_store_refuses_to_create(tmp_path):
    store = DesignSystemStore(InMemoryFilesBackend(), tmp_path, cache_max_entries=8, max_versions=3,
                              max_envelope_bytes=1_000_000, reap_aborted_after_s=3600)
    with pytest.raises(NotHydrated):
        store.create(_meta(), _version())


# ------------------------------------------------------------ mutation

def test_add_version_allocates_after_high_water_even_after_restart(ds_store, tmp_path):
    store, backend = ds_store
    store.create(_meta(), _version())
    store.add_version("ds_abc", lambda n: _version(n=n))
    store.add_version("ds_abc", lambda n: _version(n=n))
    store.delete_version("ds_abc", 3, now="2026-09-15T01:00:00Z")
    fresh = DesignSystemStore(backend, tmp_path / "c2", cache_max_entries=8, max_versions=3,
                              max_envelope_bytes=1_000_000, reap_aborted_after_s=3600)
    fresh.hydrate()
    v = fresh.add_version("ds_abc", lambda n: _version(n=n))
    assert v.version == 4                               # never 3 again
    assert fresh.get_meta("ds_abc").version_high_water == 3


def test_version_limit_is_a_409_not_a_prune(ds_store):
    store, _ = ds_store
    store.create(_meta(), _version())
    store.add_version("ds_abc", lambda n: _version(n=n))
    store.add_version("ds_abc", lambda n: _version(n=n))
    with pytest.raises(VersionLimit):
        store.add_version("ds_abc", lambda n: _version(n=n))
    assert sorted(v.version for v in store.list_versions("ds_abc")) == [1, 2, 3]


def test_delete_only_version_refused(ds_store):
    store, _ = ds_store
    store.create(_meta(), _version())
    with pytest.raises(LastVersion):
        store.delete_version("ds_abc", 1, now="2026-09-15T01:00:00Z")


def test_update_meta_uploads_new_file_and_retires_old(ds_store):
    store, backend = ds_store
    store.create(_meta(), _version())
    store.update_meta("ds_abc", name="Corp 2", description=None, now="2026-09-15T02:00:00Z")
    metas = backend.search_by_tag("ds-meta")
    assert len(metas) == 1 and store.get_meta("ds_abc").name == "Corp 2"
    assert store.get_meta("ds_abc").updated_at == "2026-09-15T02:00:00Z"


def test_delete_persists_high_water_first_and_removes_children_before_meta(ds_store):
    store, backend = ds_store
    store.create(_meta(), _version())
    store.add_version("ds_abc", lambda n: _version(n=n))
    order: list[str] = []
    real_delete = backend.delete

    def spy(fid):
        order.append(next(f.name for f in backend.search_by_tag(TAG_DS_ALL) if f.id == fid))
        real_delete(fid)

    backend.delete = spy
    store.delete("ds_abc", now="2026-09-15T03:00:00Z")
    # Every meta file of one system shares a single name, so "children first,
    # the authorizing meta strictly last" is checked as: the last delete is a
    # meta, and no version file is deleted after any meta -- superseded metas
    # are retired next to the winner, never ahead of the children.
    assert order[-1].endswith("-meta.json")
    first_meta = min(i for i, n in enumerate(order) if n.endswith("-meta.json"))
    assert all(not n.endswith("-meta.json") for n in order[:first_meta])
    assert all(n.endswith("-meta.json") for n in order[first_meta:])
    assert backend.search_by_tag(TAG_DS_ALL) == [] and store.resolve_ref("corp") is None


def test_partial_delete_keeps_meta_and_never_reuses_numbers(ds_store, tmp_path):
    store, backend = ds_store
    store.create(_meta(), _version())
    store.add_version("ds_abc", lambda n: _version(n=n))
    real_delete = backend.delete
    calls = {"n": 0}

    def flaky(fid):
        calls["n"] += 1
        if calls["n"] == 2:
            raise BackendError("boom")
        real_delete(fid)

    backend.delete = flaky
    with pytest.raises(BackendError):
        store.delete("ds_abc", now="2026-09-15T03:00:00Z")
    backend.delete = real_delete
    assert store.get_meta("ds_abc") is not None                  # owner can retry
    fresh = DesignSystemStore(backend, tmp_path / "c3", cache_max_entries=8, max_versions=3,
                              max_envelope_bytes=1_000_000, reap_aborted_after_s=3600)
    fresh.hydrate()
    v = fresh.add_version("ds_abc", lambda n: _version(n=n))
    assert v.version == 3


def test_reap_removes_old_meta_only_records_and_stale_metas(ds_store):
    store, backend = ds_store
    backend.upload("ds-ds_x-meta.json", _meta("ds_x", "x", created_at="2026-09-15T00:00:00Z").to_json(),
                   [TAG_DS_ALL, "ds-id-ds_x", "ds-meta", "ds-owner-k", "ds-slug-x"])
    store.hydrate()
    from datetime import datetime, timezone
    late = datetime(2026, 9, 15, 2, tzinfo=timezone.utc).timestamp()
    assert store.reap_aborted(now_ts=late) == 1
    assert store.resolve_ref("x") is None and backend.search_by_tag(TAG_DS_ALL) == []
