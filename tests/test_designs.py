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
    NotHydrated,
    SlugTaken,
    validate_bundle,
)
from src.kbc import InMemoryFilesBackend

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
