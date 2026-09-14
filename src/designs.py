"""Design systems: bundle validation and the Storage-backed store.

Spec: docs/superpowers/specs/2026-09-14-design-systems-design.md. The store
mirrors ``src.comments.CommentStore`` for cache/namespace mechanics and
``src.store.ArtifactStore`` for ownership, version allocation and deletion
ordering. Files are tagged ``artifact-hub-ds`` so neither of those stores
ever sees them.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from src.config import Settings
from src.kbc import BackendError, FilesBackend
from src.tokens import (
    EMITTED_TYPES,
    TokenSet,
    TokenValidationError,
    alias_target,
    is_alias,
    validate_document,
)

logger = logging.getLogger(__name__)

#: A slug is chosen by the owner and must never collide with an ``ds_``-shaped
#: id (Key decision 1); the same pattern names a component inside a bundle.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
#: Every design-system id starts with this, and no slug can (the slug pattern
#: has no ``_``), so a ``{ref}`` is classified by syntax alone.
ID_PREFIX = "ds_"

#: Role -> the token type it must point at (Key decision 9). ``chart_palette``
#: is the one list-valued role; every entry in it is a ``color``.
ROLE_TYPES: dict[str, str] = {
    "background": "color", "surface": "color", "text": "color", "muted": "color",
    "border": "color", "accent": "color", "on_accent": "color",
    "font_body": "fontFamily", "font_heading": "fontFamily", "font_mono": "fontFamily",
    "radius": "dimension", "chart_palette": "color",
}
#: A closing ``</style`` inside a component's css. The starter splices css
#: verbatim into one ``<style>`` block, so this would end the block early and
#: let whatever follows render as markup -- rejected at submit time, where the
#: author can still fix it, rather than escaped at render time in one of the
#: several places the css is emitted.
STYLE_CLOSE_RE = re.compile(r"</\s*style", re.IGNORECASE)
CHART_LIBRARIES = ("chart.js", "inline-svg", "none")
DIAGRAM_LIBRARIES = ("mermaid", "none")


class BundleError(ValueError):
    """Every fatal finding of one bundle validation pass, in document order.

    ``findings`` is what the route answers 422 with: JSON Pointers into the
    request body, so an agent can fix the exact field it got wrong.
    """

    def __init__(self, findings: list[dict[str, str]]) -> None:
        super().__init__(f"{len(findings)} bundle finding(s)")
        self.findings = findings


def _f(path: str, message: str) -> dict[str, str]:
    return {"path": path, "message": message}


def _text(value: Any, path: str, *, max_chars: int, required: bool,
          findings: list[dict[str, str]]) -> str:
    """Trim one optional/required free-text field, recording its findings."""
    if value is None:
        if required:
            findings.append(_f(path, "required"))
        return ""
    if not isinstance(value, str):
        findings.append(_f(path, "must be a string"))
        return ""
    value = value.strip()
    if required and not value:
        findings.append(_f(path, "required, non-empty"))
    if len(value) > max_chars:
        findings.append(_f(path, f"longer than {max_chars} characters"))
    return value


def _check_role(role: str, ref: Any, base: TokenSet, dark: TokenSet | None, path: str,
                findings: list[dict[str, str]]) -> None:
    """Type-check one role alias in every effective mode (Key decision 9).

    A role is checked against the dark set as well as the base: a dark
    override may not change a token's type, but it may replace a token the
    base never had, and a template that reads ``accent`` must get a color in
    both looks or not at all.
    """
    if not is_alias(ref):
        findings.append(_f(path, "must be a {path.to.token} alias"))
        return
    want = ROLE_TYPES[role]
    for tokenset in (base, dark):
        if tokenset is None:
            continue
        target = alias_target(ref)
        tok = tokenset.tokens.get(target)
        dotted = ".".join(target)
        if tok is None:
            findings.append(_f(path, f"unknown token '{dotted}'"))
            return
        if tok.type not in EMITTED_TYPES:
            findings.append(_f(
                path, f"token {'.'.join(tok.path)} has type '{tok.type}', which is not emitted"
            ))
            return
        if tok.type != want:
            findings.append(_f(path, f"role requires a {want} token, {'.'.join(tok.path)} is {tok.type}"))
            return


def validate_bundle(raw: Any, *, settings: Settings) -> tuple[dict, list[dict[str, str]]]:
    """Validate and normalise a submitted bundle. Returns ``(bundle, warnings)``.

    Every rule of the spec's *Bundle* section is one branch below. Fatal
    findings are collected per section rather than raised one at a time, so a
    caller sees everything wrong with a submission in a single 422; the token
    document is the exception, because nothing downstream (roles above all)
    can be judged against a document that did not parse.
    """
    findings: list[dict[str, str]] = []
    if not isinstance(raw, dict):
        raise BundleError([_f("/bundle", "must be an object")])
    out: dict[str, Any] = {}

    # tokens + modes -------------------------------------------------------
    tokens = raw.get("tokens")
    modes = raw.get("modes") or {}
    if not isinstance(tokens, dict) or not tokens:
        findings.append(_f("/bundle/tokens", "required, non-empty DTCG object"))
    if not isinstance(modes, dict):
        findings.append(_f("/bundle/modes", "must be an object"))
        modes = {}
    for name in modes:
        if name != "dark":
            findings.append(_f(f"/bundle/modes/{name}", "only a 'dark' mode is supported"))
    dark_doc = modes.get("dark") if isinstance(modes.get("dark"), dict) else None
    base = dark = None
    warnings: list[dict[str, str]] = []
    if isinstance(tokens, dict) and tokens and not findings:
        try:
            base, dark, warnings = validate_document(tokens, dark_doc, limits=settings.token_limits())
        except TokenValidationError as exc:
            # src.tokens points at "/tokens/..."; the bundle sits one level
            # deeper in the request body, so re-point rather than invent.
            findings.extend(_f("/bundle" + f["path"], f["message"]) for f in exc.findings)
    if findings:
        raise BundleError(findings)
    out["tokens"] = tokens
    out["modes"] = {"dark": dark_doc} if dark_doc is not None else {}

    # roles ----------------------------------------------------------------
    roles = raw.get("roles") or {}
    if not isinstance(roles, dict):
        findings.append(_f("/bundle/roles", "must be an object"))
        roles = {}
    for role, ref in roles.items():
        path = f"/bundle/roles/{role}"
        if role not in ROLE_TYPES:
            findings.append(_f(path, "unknown role"))
        elif role == "chart_palette":
            if not isinstance(ref, list) or not 1 <= len(ref) <= settings.ds_max_palette:
                findings.append(_f(path, f"must be a list of 1 to {settings.ds_max_palette} color aliases"))
            else:
                for i, item in enumerate(ref):
                    _check_role(role, item, base, dark, f"{path}/{i}", findings)
        else:
            _check_role(role, ref, base, dark, path, findings)
    out["roles"] = roles

    # guidance -------------------------------------------------------------
    guidance = raw.get("guidance")
    if not isinstance(guidance, str) or not guidance.strip():
        findings.append(_f("/bundle/guidance", "required, non-empty Markdown"))
        guidance = ""
    guidance = guidance.strip()
    if len(guidance.encode("utf-8")) > settings.ds_max_guidance_bytes:
        findings.append(_f("/bundle/guidance", f"longer than {settings.ds_max_guidance_bytes} bytes"))
    out["guidance"] = guidance

    # components -----------------------------------------------------------
    comps = raw.get("components") or []
    if not isinstance(comps, list):
        findings.append(_f("/bundle/components", "must be a list"))
        comps = []
    if len(comps) > settings.ds_max_components:
        findings.append(_f("/bundle/components", f"more than {settings.ds_max_components} components"))
    seen: set[str] = set()
    norm_comps: list[dict[str, str]] = []
    for i, comp in enumerate(comps):
        p = f"/bundle/components/{i}"
        if not isinstance(comp, dict):
            findings.append(_f(p, "must be an object"))
            continue
        name = comp.get("name")
        if not isinstance(name, str) or not SLUG_RE.match(name):
            findings.append(_f(f"{p}/name", "must match ^[a-z0-9][a-z0-9-]{1,39}$"))
            name = ""
        elif name in seen:
            findings.append(_f(f"{p}/name", "duplicate component name"))
        seen.add(name)
        html_ = comp.get("html")
        if not isinstance(html_, str) or not html_.strip():
            findings.append(_f(f"{p}/html", "required, non-empty"))
            html_ = ""
        css = comp.get("css") or ""
        if not isinstance(css, str):
            findings.append(_f(f"{p}/css", "must be a string"))
            css = ""
        elif STYLE_CLOSE_RE.search(css):
            findings.append(_f(f"{p}/css", "css may not contain '</style'"))
        if len(html_.encode("utf-8")) + len(css.encode("utf-8")) > settings.ds_max_component_bytes:
            findings.append(_f(p, f"html + css exceed {settings.ds_max_component_bytes} bytes"))
        norm_comps.append({
            "name": name,
            "description": _text(comp.get("description"), f"{p}/description",
                                 max_chars=settings.ds_max_description_chars,
                                 required=False, findings=findings),
            "when_to_use": _text(comp.get("when_to_use"), f"{p}/when_to_use",
                                 max_chars=settings.ds_max_description_chars,
                                 required=False, findings=findings),
            "html": html_, "css": css,
        })
    out["components"] = norm_comps

    # charts / diagrams ----------------------------------------------------
    for key, allowed in (("charts", CHART_LIBRARIES), ("diagrams", DIAGRAM_LIBRARIES)):
        section = raw.get(key) or {}
        if not isinstance(section, dict):
            findings.append(_f(f"/bundle/{key}", "must be an object"))
            section = {}
        lib = section.get("library", "none")
        if lib not in allowed:
            findings.append(_f(f"/bundle/{key}/library", "must be one of " + ", ".join(allowed)))
        out[key] = {
            "library": lib,
            "notes": _text(section.get("notes"), f"/bundle/{key}/notes",
                           max_chars=settings.ds_max_description_chars,
                           required=False, findings=findings),
        }

    # fonts ----------------------------------------------------------------
    fonts = raw.get("fonts") or []
    if not isinstance(fonts, list):
        findings.append(_f("/bundle/fonts", "must be a list"))
        fonts = []
    if len(fonts) > settings.ds_max_font_links:
        findings.append(_f("/bundle/fonts", f"more than {settings.ds_max_font_links} font links"))
    norm_fonts: list[dict[str, str]] = []
    for i, item in enumerate(fonts):
        p = f"/bundle/fonts/{i}/href"
        href = item.get("href") if isinstance(item, dict) else None
        parts = urlsplit(href) if isinstance(href, str) else None
        if parts is None or parts.scheme != "https":
            findings.append(_f(p, "must be an https URL"))
        elif parts.hostname not in settings.ds_font_hosts:
            findings.append(_f(p, "host must be one of " + ", ".join(settings.ds_font_hosts)))
        else:
            norm_fonts.append({"href": href})
    out["fonts"] = norm_fonts

    if findings:
        raise BundleError(findings)
    # Measured after normalisation, so what is checked is exactly what will be
    # persisted -- not the (possibly much larger) raw request body.
    size = len(json.dumps(out, ensure_ascii=False).encode("utf-8"))
    if size > settings.ds_max_bundle_bytes:
        raise BundleError([
            _f("/bundle", f"bundle exceeds {settings.ds_max_bundle_bytes} bytes after normalisation")
        ])
    return out, warnings


# ===================================================== records and the store

#: Namespace tag every design-system file carries. ``ArtifactStore.hydrate``
#: searches ``artifact-hub`` and never sees these; this store searches only
#: this tag and never sees an artifact.
TAG_DS_ALL = "artifact-hub-ds"
TAG_DS_META = "ds-meta"
_TAG_ID = "ds-id-"
_TAG_OWNER = "ds-owner-"
_TAG_SLUG = "ds-slug-"
_TAG_VER = "ds-ver-"
SCHEMA_VERSION = 1
_CACHE_PREFIX = "ds."
#: Ids safe to build a cache file name from; anything else skips the disk
#: cache rather than letting a name escape the cache directory.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
CACHE_DIR_MODE = 0o700
CACHE_FILE_MODE = 0o600


def tag_ds_id(ds_id: str) -> str:
    return _TAG_ID + ds_id


def tag_ds_owner(key: str) -> str:
    return _TAG_OWNER + key


def tag_ds_slug(slug: str) -> str:
    return _TAG_SLUG + slug


def tag_ds_version(n: int) -> str:
    return f"{_TAG_VER}{n}"


def _tag_value(tags: list[str], prefix: str) -> str | None:
    for tag in tags or []:
        if tag.startswith(prefix):
            return tag[len(prefix):]
    return None


def _stack_host(url: str) -> str:
    return urlsplit(url).hostname or ""


def _ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class SlugTaken(ValueError):
    """The requested slug already names a live registration."""


class VersionLimit(ValueError):
    """The design system already holds the maximum number of versions (409)."""


class LastVersion(ValueError):
    """The only version of a design system may not be deleted (409)."""


class NotHydrated(RuntimeError):
    """The index has not been rebuilt, so uniqueness cannot be established."""


@dataclass
class DesignSystemMeta:
    id: str
    slug: str
    name: str
    description: str
    owner: dict
    created_at: str
    updated_at: str
    #: The highest version number ever allocated, persisted before the newest
    #: version file is deleted so that number is never handed out again.
    version_high_water: int = 0
    schema: int = SCHEMA_VERSION

    @property
    def owner_key(self) -> str:
        return str(self.owner.get("key", ""))

    def to_json(self) -> bytes:
        return json.dumps({
            "schema": self.schema, "id": self.id, "slug": self.slug, "name": self.name,
            "description": self.description, "owner": self.owner, "created_at": self.created_at,
            "updated_at": self.updated_at, "version_high_water": self.version_high_water,
        }, ensure_ascii=False).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes) -> "DesignSystemMeta":
        d = json.loads(raw.decode("utf-8"))
        hw = d.get("version_high_water", 0)
        return cls(
            id=str(d["id"]), slug=str(d["slug"]), name=str(d.get("name", "")),
            description=str(d.get("description", "")), owner=dict(d.get("owner") or {}),
            created_at=str(d.get("created_at", "")), updated_at=str(d.get("updated_at", "")),
            # Defensive: a truncated or hand-edited record must degrade to "no
            # high water known", never to a bool or a negative number that
            # would let the allocator hand out a number already used.
            version_high_water=(
                hw if isinstance(hw, int) and not isinstance(hw, bool) and hw > 0 else 0
            ),
            schema=int(d.get("schema", SCHEMA_VERSION)),
        )

    def projection(self, *, head_version: int | None, versions_count: int, mine: bool,
                   urls: dict) -> dict:
        """The public catalogue/detail row.

        Owner identity is reduced to project id, project name and stack host:
        nothing here is a credential, and no stack URL path is exposed.
        """
        return {
            "id": self.id, "slug": self.slug, "name": self.name,
            "description": self.description,
            "owner": {"project_id": self.owner.get("project_id"),
                      "project_name": self.owner.get("project_name"),
                      "stack_host": _stack_host(str(self.owner.get("stack_url") or ""))},
            "head_version": head_version, "versions_count": versions_count,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "mine": mine, "urls": urls,
        }


@dataclass
class DesignSystemVersion:
    id: str
    version: int
    note: str
    author: dict
    created_at: str
    bundle: dict
    warnings: list = field(default_factory=list)
    schema: int = SCHEMA_VERSION

    def to_json(self) -> bytes:
        return json.dumps({
            "schema": self.schema, "id": self.id, "version": self.version, "note": self.note,
            "author": self.author, "created_at": self.created_at, "bundle": self.bundle,
            "warnings": self.warnings,
        }, ensure_ascii=False).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes) -> "DesignSystemVersion":
        d = json.loads(raw.decode("utf-8"))
        return cls(id=str(d["id"]), version=int(d["version"]), note=str(d.get("note") or ""),
                   author=dict(d.get("author") or {}), created_at=str(d.get("created_at", "")),
                   bundle=dict(d["bundle"]), warnings=list(d.get("warnings") or []),
                   schema=int(d.get("schema", SCHEMA_VERSION)))

    def public_row(self) -> dict:
        return {
            "version": self.version, "note": self.note, "created_at": self.created_at,
            "author": {"project_id": self.author.get("project_id"),
                       "project_name": self.author.get("project_name"),
                       "stack_host": _stack_host(str(self.author.get("stack_url") or ""))},
            "size_bytes": len(self.to_json()), "warnings_count": len(self.warnings),
        }


@dataclass
class _Entry:
    """One logical design system as the index knows it: file pointers only, so
    hydrate never downloads an envelope."""

    meta_file_id: int = -1
    #: Superseded meta files still in Storage, retired on the next write.
    stale_meta_file_ids: set[int] = field(default_factory=set)
    slug: str = ""
    owner_key: str = ""
    versions: dict[int, int] = field(default_factory=dict)   # version -> file id
    high_water: int = 0                                      # seeded from meta when loaded

    def head(self) -> int | None:
        return max(self.versions) if self.versions else None


class DesignSystemStore:
    """Storage-Files-backed catalogue of design systems.

    Two process-local locks, with distinct jobs:

    * ``self._lock`` guards the in-memory index itself. It is held only for
      the dict work and is always released before a backend call, so a slow
      or hanging Storage request never blocks a reader.
    * ``self._mutation_locks[ds_id]`` serializes the *whole body* of
      :meth:`add_version`, :meth:`update_meta`, :meth:`delete_version` and
      :meth:`delete` for one design system -- deliberately including the
      backend calls inside them. Each of those four is a meta
      read-modify-write (``get_meta`` -> mutate -> ``_save_meta``) and the
      index lock cannot span that pair; without this second lock two owner
      threads interleave and the later meta write reverts
      ``version_high_water`` to what its thread read, so a number the other
      thread retired is handed out again after a restart. Reads, ``create``
      and ``reap_aborted`` do not take it.

    Both are correct only under this deployment's "exactly one instance,
    ever" invariant (CLAUDE.md). Two processes would need a shared
    compare-and-swap, which Storage Files cannot provide.
    """

    def __init__(self, backend: FilesBackend, cache_dir: Path, *, cache_max_entries: int,
                 max_versions: int, max_envelope_bytes: int,
                 reap_aborted_after_s: int) -> None:
        self._backend = backend
        self._cache_dir = Path(cache_dir)
        self._cache_max = cache_max_entries
        self._max_versions = max_versions
        self._max_bytes = max_envelope_bytes
        self._reap_after = reap_aborted_after_s
        self._index: dict[str, _Entry] = {}
        self._slugs: dict[str, str] = {}
        self._meta_memory: OrderedDict[tuple[str, int], DesignSystemMeta] = OrderedDict()
        self._version_memory: OrderedDict[tuple[str, int], DesignSystemVersion] = OrderedDict()
        self._lock = threading.Lock()
        #: Per-system mutation locks, created on demand under ``_lock`` and
        #: dropped when the record is gone. Bounded by the number of live
        #: design systems, which ``ds_max_per_project`` already bounds.
        self._mutation_locks: dict[str, threading.Lock] = {}
        self.hydrated = False
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._cache_dir.chmod(CACHE_DIR_MODE)
        except OSError:
            pass

    # ------------------------------------------------------------- hydrate
    def hydrate(self) -> int:
        """Rebuild the whole index from Storage tags alone. Downloads nothing."""
        files = self._backend.search_by_tag(TAG_DS_ALL)
        index: dict[str, _Entry] = {}
        for info in files:
            ds_id = _tag_value(info.tags, _TAG_ID)
            if not ds_id:
                logger.warning("Skipping design-system file %s: no ds-id tag", info.name)
                continue
            entry = index.setdefault(ds_id, _Entry())
            if TAG_DS_META in info.tags:
                # Highest file id wins: a meta update uploads a new file, so an
                # older one must never resurrect a slug or a high-water mark.
                if info.id > entry.meta_file_id:
                    if entry.meta_file_id >= 0:
                        entry.stale_meta_file_ids.add(entry.meta_file_id)
                    entry.meta_file_id = info.id
                    entry.slug = _tag_value(info.tags, _TAG_SLUG) or ""
                    entry.owner_key = _tag_value(info.tags, _TAG_OWNER) or ""
                else:
                    entry.stale_meta_file_ids.add(info.id)
                continue
            ver = _tag_value(info.tags, _TAG_VER)
            if ver and ver.isdigit():
                n = int(ver)
                if entry.versions.get(n, -1) < info.id:
                    entry.versions[n] = info.id
        # A version with no meta is unreachable: the meta file is what proves
        # ownership, so without it nobody could read or delete the version.
        index = {k: v for k, v in index.items() if v.meta_file_id >= 0}
        with self._lock:
            self._index = index
            self._slugs = {e.slug: ds_id for ds_id, e in index.items() if e.slug}
            self._meta_memory.clear()
            self._version_memory.clear()
            self.hydrated = True
        count = sum(1 for e in index.values() if e.versions)
        logger.info("Hydrated design-system index: %d system(s)", count)
        return count

    def count(self) -> int:
        """Publicly visible systems: a meta with no version does not count."""
        with self._lock:
            return sum(1 for e in self._index.values() if e.versions)

    # ------------------------------------------------------------- lookups
    @staticmethod
    def is_id_shaped(ref: str) -> bool:
        return ref.startswith(ID_PREFIX) and _SAFE_ID.match(ref) is not None

    def resolve_ref(self, ref: str) -> str | None:
        with self._lock:
            if self.is_id_shaped(ref):
                return ref if ref in self._index else None
            return self._slugs.get(ref)

    def head_version(self, ds_id: str) -> int | None:
        with self._lock:
            e = self._index.get(ds_id)
            return e.head() if e else None

    def get_meta(self, ds_id: str) -> DesignSystemMeta | None:
        with self._lock:
            e = self._index.get(ds_id)
            if e is None:
                return None
            fid = e.meta_file_id
            cached = self._meta_memory.get((ds_id, fid))
        if cached is not None:
            return cached
        meta = DesignSystemMeta.from_json(self._read(ds_id, fid))
        with self._lock:
            self._meta_memory[(ds_id, fid)] = meta
            self._trim(self._meta_memory)
            e = self._index.get(ds_id)
            # Reading the winning meta is what seeds the allocator after a
            # restart, so a retired number is never handed out again.
            if e is not None and meta.version_high_water > e.high_water:
                e.high_water = meta.version_high_water
        return meta

    def list_all(self) -> list[DesignSystemMeta]:
        """The public catalogue: newest change first, slug as the tie-break."""
        with self._lock:
            ids = [k for k, e in self._index.items() if e.versions]
        metas = [m for m in (self.get_meta(i) for i in ids) if m is not None]
        return sorted(metas, key=lambda m: (-_ts(m.updated_at), m.slug))

    def list_owner(self, owner_key: str) -> list[DesignSystemMeta]:
        """Everything this project owns, inert meta-only records included."""
        with self._lock:
            ids = [k for k, e in self._index.items() if e.owner_key == owner_key]
        return [m for m in (self.get_meta(i) for i in ids) if m is not None]

    def count_owner(self, owner_key: str) -> int:
        with self._lock:
            return sum(1 for e in self._index.values() if e.owner_key == owner_key)

    def get_version(self, ds_id: str, version: int | None) -> DesignSystemVersion | None:
        with self._lock:
            e = self._index.get(ds_id)
            if e is None or not e.versions:
                return None
            n = e.head() if version is None else version
            fid = e.versions.get(n)
            if fid is None:
                return None
            cached = self._version_memory.get((ds_id, fid))
        if cached is not None:
            return cached
        v = DesignSystemVersion.from_json(self._read(ds_id, fid))
        with self._lock:
            self._version_memory[(ds_id, fid)] = v
            self._trim(self._version_memory)
        return v

    def list_versions(self, ds_id: str) -> list[DesignSystemVersion]:
        with self._lock:
            e = self._index.get(ds_id)
            numbers = sorted(e.versions) if e else []
        return [v for v in (self.get_version(ds_id, n) for n in numbers) if v is not None]

    # ------------------------------------------------------------- create
    def create(self, meta: DesignSystemMeta, first: DesignSystemVersion) -> None:
        """Register a new design system: meta first, then v1 (Key decision 5).

        The gap between the two writes is a legitimate, temporary state -- a
        meta with no version is inert publicly, still owner-addressable, and
        :meth:`reap_aborted` clears it once it is too old to be a create still
        in flight.
        """
        if not self.hydrated:
            raise NotHydrated("design-system index is not hydrated")
        if not SLUG_RE.match(meta.slug):
            raise ValueError("malformed slug")
        with self._lock:
            if meta.slug in self._slugs or meta.slug in self._index or meta.id in self._index:
                raise SlugTaken(meta.slug)
        meta_fid = self._upload_meta(meta)
        with self._lock:
            self._index[meta.id] = _Entry(meta_file_id=meta_fid, slug=meta.slug,
                                          owner_key=meta.owner_key,
                                          high_water=meta.version_high_water)
            self._slugs[meta.slug] = meta.id
        fid = self._backend.upload(f"ds-{meta.id}-v{first.version}.json", first.to_json(),
                                   [TAG_DS_ALL, tag_ds_id(meta.id), tag_ds_version(first.version)])
        with self._lock:
            self._index[meta.id].versions[first.version] = fid
            self._version_memory[(meta.id, fid)] = first
            self._trim(self._version_memory)

    # ------------------------------------------------------------- mutate
    @contextmanager
    def _mutating(self, ds_id: str):
        """Serialize one design system's meta read-modify-write cycle.

        Held across the backend calls inside a mutation -- that is the point:
        the index lock is released around those, so it cannot keep two threads
        from each reading the meta and then writing a different descendant of
        it.
        """
        with self._lock:
            lock = self._mutation_locks.setdefault(ds_id, threading.Lock())
        with lock:
            yield

    def add_version(self, ds_id: str,
                    build: Callable[[int], DesignSystemVersion]) -> DesignSystemVersion:
        """Allocate the next version number and write that version.

        ``build`` receives the assigned number, so the caller never has to
        guess one: the number is reserved under the lock *before* the slow
        upload runs, which is what keeps two concurrent submissions in this
        process from picking the same one.
        """
        if not self.hydrated:
            raise NotHydrated("design-system index is not hydrated")
        with self._mutating(ds_id):
            meta = self.get_meta(ds_id)             # seeds entry.high_water from the winning meta
            if meta is None:
                raise KeyError(ds_id)
            with self._lock:
                e = self._index[ds_id]
                if len(e.versions) >= self._max_versions:
                    # A version is immutable and never pruned: the limit is a
                    # 409, not a licence to drop somebody's oldest version.
                    raise VersionLimit(ds_id)
                n = max([e.high_water, *e.versions.keys(), 0]) + 1
                e.high_water = n                    # reserve so a later caller cannot pick n
            version = build(n)
            fid = self._backend.upload(f"ds-{ds_id}-v{n}.json", version.to_json(),
                                       [TAG_DS_ALL, tag_ds_id(ds_id), tag_ds_version(n)])
            with self._lock:
                e = self._index[ds_id]
                e.versions[n] = fid
                self._version_memory[(ds_id, fid)] = version
                self._trim(self._version_memory)
            try:
                self._save_meta(replace(meta, updated_at=version.created_at))
            except BackendError as exc:
                # The append is already durable. Failing the call here would
                # report an error for work that succeeded, and an honest retry
                # would then write a duplicate version; a stale updated_at is
                # repaired by the next meta write instead.
                logger.warning("Design system %s: version %d written but its meta refresh "
                               "failed: %s", ds_id, n, exc)
            return version

    def update_meta(self, ds_id: str, *, name: str | None, description: str | None,
                    now: str) -> DesignSystemMeta:
        """Change the editable meta fields. ``None`` means "leave as it is"."""
        with self._mutating(ds_id):
            meta = self.get_meta(ds_id)
            if meta is None:
                raise KeyError(ds_id)
            new = replace(meta,
                          name=meta.name if name is None else name,
                          description=meta.description if description is None else description,
                          updated_at=now)
            self._save_meta(new)
            return new

    def _save_meta(self, meta: DesignSystemMeta, *, retire_stale: bool = True) -> None:
        """Upload a new meta file, publish it, retire the older ones.

        Storage Files are immutable, so every meta change is a new file; the
        index republishes the pointer before anything is deleted, which is why
        a crash between the two leaves a readable record rather than none.
        ``retire_stale=False`` leaves the superseded ids on the entry for the
        caller to delete later -- :meth:`delete` uses it so no meta file is
        removed ahead of the version files.
        """
        fid = self._upload_meta(meta)
        with self._lock:
            e = self._index[meta.id]
            old = e.meta_file_id
            e.meta_file_id = fid
            e.high_water = max(e.high_water, meta.version_high_water)
            stale = set(e.stale_meta_file_ids) | ({old} if old >= 0 else set())
            e.stale_meta_file_ids = set() if retire_stale else stale
            self._meta_memory[(meta.id, fid)] = meta
            self._trim(self._meta_memory)
        if not retire_stale:
            return
        for sid in sorted(stale):
            try:
                self._backend.delete(sid)
            except BackendError as exc:            # leave it for reap_aborted
                logger.warning("Could not retire stale meta %s: %s", sid, exc)
                with self._lock:
                    self._index[meta.id].stale_meta_file_ids.add(sid)

    def delete_version(self, ds_id: str, version: int, *, now: str) -> None:
        """Remove one version file. The only version of a system is a 409."""
        with self._mutating(ds_id):
            meta = self.get_meta(ds_id)
            if meta is None:
                raise KeyError(ds_id)
            with self._lock:
                e = self._index[ds_id]
                if version not in e.versions:
                    raise KeyError(version)
                if len(e.versions) == 1:
                    raise LastVersion(ds_id)
                fid = e.versions[version]
                is_highest = version == max(e.versions)
            if is_highest and meta.version_high_water < version:
                # Persisted *before* the file goes: otherwise a restart would
                # see a lower highest-surviving number and reissue this one.
                self._save_meta(replace(meta, version_high_water=version, updated_at=now))
            self._backend.delete(fid)
            with self._lock:
                self._index[ds_id].versions.pop(version, None)
                self._version_memory.pop((ds_id, fid), None)
            self._drop_cache(ds_id, fid)

    def delete(self, ds_id: str, *, now: str) -> None:
        """Purge a design system: high water first, children next, meta last.

        The meta file is what authorizes the purge, so removing it while any
        version file survives would leave the leftover unreachable *and*
        undeletable. A failure part-way therefore leaves the meta in place and
        reconciles the index from what survived, so the owner can retry.
        """
        with self._mutating(ds_id):
            meta = self.get_meta(ds_id)
            if meta is None:
                raise KeyError(ds_id)
            with self._lock:
                highest = max(self._index[ds_id].versions or [0])
            if highest > meta.version_high_water:
                # A partial purge must not let a later append reuse a number.
                self._save_meta(replace(meta, version_high_water=highest, updated_at=now),
                                retire_stale=False)
            with self._lock:
                e = self._index[ds_id]
                children = sorted(e.versions.items())
                stale = sorted(e.stale_meta_file_ids)
            for n, fid in children:                   # children first ...
                self._backend.delete(fid)             # BackendError propagates: meta stays
                with self._lock:
                    self._index[ds_id].versions.pop(n, None)
                self._drop_cache(ds_id, fid)
            for sid in stale:                         # ... superseded metas ...
                self._backend.delete(sid)
                with self._lock:
                    self._index[ds_id].stale_meta_file_ids.discard(sid)
            with self._lock:
                meta_fid = self._index[ds_id].meta_file_id
            self._backend.delete(meta_fid)            # ... the winning meta strictly last
            with self._lock:
                entry = self._index.pop(ds_id, None)
                if entry is not None:
                    self._slugs.pop(entry.slug, None)
                for key in [k for k in self._meta_memory if k[0] == ds_id]:
                    self._meta_memory.pop(key, None)
                # The record is gone; drop its lock entry so the dict tracks
                # live systems. A thread already waiting on this very lock
                # object still wakes up correctly and then finds no index
                # entry, which is the ordinary "unknown design system" path.
                self._mutation_locks.pop(ds_id, None)
            self._drop_cache(ds_id, meta_fid)

    def reap_aborted(self, *, now_ts: float) -> int:
        """Remove meta-only records older than the reap window, and every
        stale meta file left behind by a failed retirement.

        A meta with no version is either a create still in flight or one that
        died between its two writes; age is the only thing that tells them
        apart, which is why the window exists at all. Returns how many records
        were removed.
        """
        removed = 0
        with self._lock:
            candidates = [(k, e.meta_file_id, sorted(e.stale_meta_file_ids))
                          for k, e in self._index.items() if not e.versions]
            stale_only = [(k, sorted(e.stale_meta_file_ids))
                          for k, e in self._index.items() if e.versions and e.stale_meta_file_ids]
        for ds_id, meta_fid, stale in candidates:
            meta = self.get_meta(ds_id)
            if meta is None or now_ts - _ts(meta.created_at) < self._reap_after:
                continue
            for sid in stale + [meta_fid]:
                self._backend.delete(sid)
            with self._lock:
                e = self._index.pop(ds_id, None)
                if e is not None:
                    self._slugs.pop(e.slug, None)
            removed += 1
        for ds_id, stale in stale_only:
            for sid in stale:
                self._backend.delete(sid)
                with self._lock:
                    self._index[ds_id].stale_meta_file_ids.discard(sid)
        return removed

    def _drop_cache(self, ds_id: str, fid: int) -> None:
        path = self._cache_path(ds_id, fid)
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------- helpers
    def _upload_meta(self, meta: DesignSystemMeta) -> int:
        return self._backend.upload(
            f"ds-{meta.id}-meta.json", meta.to_json(),
            [TAG_DS_ALL, tag_ds_id(meta.id), TAG_DS_META,
             tag_ds_owner(meta.owner_key), tag_ds_slug(meta.slug)],
        )

    def _trim(self, memory: OrderedDict) -> None:
        while len(memory) > self._cache_max:
            memory.popitem(last=False)

    def _cache_path(self, ds_id: str, fid: int) -> Path | None:
        if _SAFE_ID.match(ds_id) is None:
            return None
        return self._cache_dir / f"{_CACHE_PREFIX}{ds_id}-{fid}.json"

    def _read(self, ds_id: str, fid: int) -> bytes:
        """Raw bytes of one immutable file, disk-cached.

        Storage file ids are never reused and a version file is never
        rewritten, so a cache hit needs no revalidation.
        """
        path = self._cache_path(ds_id, fid)
        if path is not None and path.exists():
            try:
                raw = path.read_bytes()
                if len(raw) <= self._max_bytes:
                    return raw
            except OSError:
                pass
        raw = self._backend.download(fid)
        if len(raw) > self._max_bytes:
            raise BackendError(f"design-system file {fid} exceeds {self._max_bytes} bytes")
        if path is not None:
            tmp = path.with_suffix(".tmp")
            try:
                tmp.write_bytes(raw)
                tmp.chmod(CACHE_FILE_MODE)
                tmp.replace(path)
            except OSError:
                pass
        return raw
