"""Design systems: bundle validation and the Storage-backed store.

Spec: docs/superpowers/specs/2026-09-14-design-systems-design.md. The store
mirrors ``src.comments.CommentStore`` for cache/namespace mechanics and
``src.store.ArtifactStore`` for ownership, version allocation and deletion
ordering. Files are tagged ``artifact-hub-ds`` so neither of those stores
ever sees them.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

from src.config import Settings
from src.tokens import (
    EMITTED_TYPES,
    TokenSet,
    TokenValidationError,
    alias_target,
    is_alias,
    validate_document,
)

#: A slug is chosen by the owner and must never collide with an ``ds_``-shaped
#: id (Key decision 1); the same pattern names a component inside a bundle.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")

#: Role -> the token type it must point at (Key decision 9). ``chart_palette``
#: is the one list-valued role; every entry in it is a ``color``.
ROLE_TYPES: dict[str, str] = {
    "background": "color", "surface": "color", "text": "color", "muted": "color",
    "border": "color", "accent": "color", "on_accent": "color",
    "font_body": "fontFamily", "font_heading": "fontFamily", "font_mono": "fontFamily",
    "radius": "dimension", "chart_palette": "color",
}
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
