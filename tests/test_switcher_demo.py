# tests/test_switcher_demo.py
"""Static checks on examples/switcher/index.html.

This document is published as a standalone artifact (git_url +
git_path=examples/switcher), so it never runs through the hub's own CSP or
CDN-allowlist enforcement — these checks are the only guardrail that it
stays within the same constraints hub-published artifacts must meet:
jsdelivr-only external scripts, no plaintext http://, and styling done only
through --ds-* role variables (see the 0.17.0 amendment).
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SWITCHER = ROOT / "examples/switcher/index.html"

FALLBACK_RE = re.compile(r"/\* fallback \*/(.*?)/\* fallback \*/", re.S)
HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")
SCRIPT_SRC_RE = re.compile(r'<script[^>]+src="([^"]+)"')


def _read():
    assert SWITCHER.exists(), "examples/switcher/index.html is missing"
    return SWITCHER.read_text()


def test_file_exists_and_is_reasonably_sized():
    content = _read()
    size = len(content.encode("utf-8"))
    assert size < 200_000, f"switcher demo is {size} bytes, expected < 200 KB"


def test_no_plaintext_http_links():
    content = _read()
    assert "http://" not in content


def test_external_scripts_only_from_jsdelivr():
    content = _read()
    srcs = SCRIPT_SRC_RE.findall(content)
    assert srcs, "expected at least one external <script src=...>"
    for src in srcs:
        assert src.startswith("https://cdn.jsdelivr.net/"), (
            f"external script must load from cdn.jsdelivr.net, got: {src}"
        )


def test_uses_ds_role_variables_extensively():
    content = _read()
    assert content.count("--ds-") >= 10


def test_no_hardcoded_hex_colors_outside_fallback_block():
    content = _read()
    match = FALLBACK_RE.search(content)
    assert match, "expected a /* fallback */ ... /* fallback */ marked block"
    fallback_block = match.group(1)
    # The fallback block itself is allowed (even required) to carry literal
    # hex colours -- it is the neutral palette shown before any design
    # system's CSS has loaded.
    assert HEX_COLOR_RE.search(fallback_block), (
        "fallback block should define literal hex colours"
    )

    rest = content[: match.start()] + content[match.end() :]
    stray = HEX_COLOR_RE.findall(rest)
    assert not stray, f"hex colours found outside the fallback block: {stray}"


def test_fetches_the_public_gallery_json():
    content = _read()
    assert "format=json" in content
    assert "/ds?format=json" in content


def test_has_hub_meta_tag_with_default_and_override_support():
    content = _read()
    assert '<meta name="hub"' in content
    assert "?hub=" in content or "'hub'" in content or '"hub"' in content


def test_references_chartjs_pinned_to_hub_constant():
    content = _read()
    # Must match builder.CHARTJS_JS exactly so the demo and the hub's own
    # server-rendered starters/style guides agree on one Chart.js version.
    import sys

    sys.path.insert(0, str(ROOT))
    from src.builder import CHARTJS_JS

    assert CHARTJS_JS in content


def test_document_has_core_switcher_affordances():
    content = _read()
    assert '<select id="ds"' in content
    assert "data-theme" in content
    assert "history.replaceState" in content
    assert "getComputedStyle" in content
