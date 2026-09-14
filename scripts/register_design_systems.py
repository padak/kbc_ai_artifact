#!/usr/bin/env python3
"""Register the sample design systems in ``examples/design-systems`` on a hub.

Each file is the full ``POST /api/design-systems`` body (``slug``, ``name``,
``description``, ``note``, ``bundle``). The script lists the hub's catalogue
once, then for every selected file either creates the system (slug absent) or
appends a version to it (slug present), and prints one line per file:

    slug  id  version  page_url

``--dry-run`` validates every bundle locally with ``src.designs.validate_bundle``
and makes no network call at all — it is the check to run before a release.

Environment (required unless ``--dry-run``):

===================== ==========================================================
``HUB_URL``           Base URL of the hub, e.g. ``https://hub.example.com``
``KBC_STACK``         Storage stack the token belongs to, sent as
                      ``X-Storage-Stack``
``KBC_TOKEN``         Storage API token of the *owning* project, sent as
                      ``X-StorageApi-Token``. Never printed or logged.
``HUB_REGISTER_TIMEOUT_S``  Optional per-request timeout in seconds
                      (default ``DEFAULT_TIMEOUT_S``).
===================== ==========================================================
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import Settings  # noqa: E402
from src.designs import validate_bundle  # noqa: E402

#: Directory holding one JSON body per sample design system.
EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples" / "design-systems"
#: Per-request timeout when ``HUB_REGISTER_TIMEOUT_S`` is not set, in seconds.
DEFAULT_TIMEOUT_S = 60.0
#: Environment variables that must be present for a non-dry run.
REQUIRED_ENV = ("HUB_URL", "KBC_STACK", "KBC_TOKEN")


class RegisterError(RuntimeError):
    """A fatal, already-explained failure; ``main`` turns it into exit code 1."""


def timeout_seconds() -> float:
    raw = os.environ.get("HUB_REGISTER_TIMEOUT_S")
    if raw is None or raw.strip() == "":
        return DEFAULT_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        raise RegisterError(
            f"HUB_REGISTER_TIMEOUT_S must be a number of seconds, got {raw!r}")
    if value <= 0:
        raise RegisterError("HUB_REGISTER_TIMEOUT_S must be greater than zero")
    return value


def load_bodies(only: Iterable[str]) -> list[dict[str, Any]]:
    """Every example body, or just the requested slugs, in file-name order."""
    wanted = list(only)
    paths = sorted(EXAMPLES_DIR.glob("*.json"))
    if not paths:
        raise RegisterError(f"no design-system files found in {EXAMPLES_DIR}")
    bodies = []
    for path in paths:
        body = json.loads(path.read_text(encoding="utf-8"))
        body["_path"] = path
        bodies.append(body)
    if wanted:
        by_slug = {b["slug"]: b for b in bodies}
        missing = [s for s in wanted if s not in by_slug]
        if missing:
            raise RegisterError("unknown --only slug(s): " + ", ".join(missing))
        bodies = [by_slug[s] for s in wanted]
    return bodies


def check_env() -> tuple[str, str, str]:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise RegisterError(
            "missing required environment variable(s): " + ", ".join(missing)
            + " (or pass --dry-run to validate locally without a hub)")
    return (os.environ["HUB_URL"].rstrip("/"), os.environ["KBC_STACK"],
            os.environ["KBC_TOKEN"])


def _detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()[:500] or "<empty response body>"
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if detail is None:
        return json.dumps(payload)[:500]
    if isinstance(detail, str):
        return detail
    return json.dumps(detail)[:500]


def _raise_for_status(response: httpx.Response, what: str) -> None:
    if response.status_code >= 400:
        raise RegisterError(f"{what} failed with HTTP {response.status_code}: "
                            f"{_detail(response)}")


def dry_run_settings() -> Settings:
    """Settings carrying only the default ``HUB_DS_*`` limits.

    ``validate_bundle`` reads limits and nothing else, so a dry run never needs
    the hub's own credentials — the three required fields are placeholders, not
    invented defaults for anything that is used. A dry run deliberately
    validates against the *documented defaults* rather than whatever a local
    shell happens to export, so the answer is the same on every machine.
    """
    return Settings(hub_storage_token="", hub_stack_url="", secret_key="")


def dry_run(bodies: list[dict[str, Any]]) -> None:
    settings = dry_run_settings()
    for body in bodies:
        normalised, warnings = validate_bundle(body["bundle"], settings=settings)
        components = len(normalised.get("components") or [])
        note = f"  {len(warnings)} warning(s)" if warnings else ""
        print(f"{body['slug']:22} ok  {components} component(s)"
              f"  {len(json.dumps(normalised).encode()):>7} bytes{note}")
        for warning in warnings:
            print(f"{'':22}   ! {warning.get('path', '')}: {warning.get('message', '')}")


def existing_slugs(client: httpx.Client, hub_url: str) -> dict[str, str]:
    """``slug -> id`` for everything already in the hub's catalogue."""
    response = client.get(f"{hub_url}/api/design-systems")
    _raise_for_status(response, "GET /api/design-systems")
    payload = response.json()
    rows = payload.get("design_systems", payload) if isinstance(payload, dict) else payload
    return {row["slug"]: row["id"] for row in rows if row.get("slug")}


def register(bodies: list[dict[str, Any]], hub_url: str, stack: str, token: str) -> None:
    headers = {"X-StorageApi-Token": token, "X-Storage-Stack": stack,
               "Accept": "application/json"}
    with httpx.Client(headers=headers, timeout=timeout_seconds()) as client:
        known = existing_slugs(client, hub_url)
        for body in bodies:
            slug = body["slug"]
            if slug in known:
                payload = {"bundle": body["bundle"], "note": body.get("note") or ""}
                response = client.post(
                    f"{hub_url}/api/design-systems/{slug}/versions", json=payload)
                what = f"POST /api/design-systems/{slug}/versions"
            else:
                payload = {k: v for k, v in body.items() if not k.startswith("_")}
                response = client.post(f"{hub_url}/api/design-systems", json=payload)
                what = "POST /api/design-systems"
            _raise_for_status(response, what)
            result = response.json()
            ds_id = result.get("id", "")
            version = result.get("version", result.get("head_version", ""))
            page_url = result.get("url") or f"{hub_url}/ds/{ds_id}"
            print(f"{slug}  {ds_id}  v{version}  {page_url}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="register_design_systems.py",
        description="Register the sample design systems on a KBC Artifact Hub.")
    parser.add_argument("--only", action="append", default=[], metavar="SLUG",
                        help="register just this slug; repeatable")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate the bundles locally, make no network call")
    args = parser.parse_args(argv)
    try:
        bodies = load_bodies(args.only)
        if args.dry_run:
            dry_run(bodies)
        else:
            hub_url, stack, token = check_env()
            register(bodies, hub_url, stack, token)
    except RegisterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"error: request to the hub failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
