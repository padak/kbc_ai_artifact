"""An AI handed a share link must be able to find its way around the hub.

A reader forwards ``/a/{id}`` to their assistant. The assistant fetches it,
its HTML-to-text step drops the ``srcdoc`` attribute the document lives in,
and it is left with a title and nothing else: no hint that this is an
Artifact Hub, where the readable document is, or where the API is described.
These tests pin the three channels that fix that, none of which changes what
a human sees or what ``/a/{id}/raw`` returns byte for byte:

- a visually hidden ``<nav>`` plus ``<link rel>`` relations in the wrapper
  page (and on the unlock form, the first thing an assistant meets on a
  password-protected artifact);
- a ``Link`` response header on everything under ``/a/``;
- a root ``/llms.txt`` in the llmstxt.org convention.
"""

from __future__ import annotations

import src.main as main
from tests.test_api import (
    Api,
    _publish_markdown,
    api,  # noqa: F401 - the fixture this module runs on
)

BASE = "https://testserver"

HUB_DOCUMENTS = (
    f"{BASE}/context",
    f"{BASE}/docs",
    f"{BASE}/openapi.json",
    f"{BASE}/skill",
    f"{BASE}/agent",
    f"{BASE}/llms.txt",
)


def test_artifact_page_declares_link_relations_for_machines(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title\n\nBody text")
    page = api.client.get(f"/a/{artifact_id}").text
    assert (
        f'<link rel="alternate" type="text/html" href="{BASE}/a/{artifact_id}/raw">'
        in page
    )
    assert (
        '<link rel="alternate" type="text/markdown" '
        f'href="{BASE}/a/{artifact_id}/export/markdown">' in page
    )
    assert f'<link rel="service-desc" href="{BASE}/context">' in page
    # Three help documents, each with a title so a machine can tell the
    # entry point, the runtime-agnostic SKILL.md and the Claude Code-only
    # AGENT.md apart; llms.txt comes first because it answers "what is this
    # link" and points at the other two.
    help_links = [
        line for line in page.splitlines() if line.startswith('<link rel="help"')
    ]
    assert help_links == [
        f'<link rel="help" href="{BASE}/llms.txt" '
        'title="What this hub is and how to read a share link">',
        f'<link rel="help" href="{BASE}/skill" '
        'title="SKILL.md for any agent runtime">',
        f'<link rel="help" href="{BASE}/agent" '
        'title="Claude Code subagent definition">',
    ]
    assert '<meta name="generator" content="kbc-artifact-hub' in page


def test_artifact_page_carries_visually_hidden_note_for_agents(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title\n\nBody text")
    page = api.client.get(f"/a/{artifact_id}").text
    assert '<nav class="ah-agents" aria-label="For AI agents">' in page
    # Hidden the accessible way: off-canvas and clipped, never display:none,
    # so text extractors and screen readers still read it.
    assert ".ah-agents{position:absolute;" in page
    assert "display:none" not in page.split(".ah-agents{", 1)[1].split("}", 1)[0]
    for url in HUB_DOCUMENTS:
        assert url in page, url
    assert f"{BASE}/a/{artifact_id}/raw" in page
    assert f"{BASE}/a/{artifact_id}/export/markdown" in page
    assert f"{BASE}/a/{artifact_id}/versions" in page
    assert "X-Artifact-Password" in page


def test_pinned_version_page_carries_the_note_too(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Only version")
    page = api.client.get(f"/a/{artifact_id}/v/1").text
    assert '<nav class="ah-agents" aria-label="For AI agents">' in page
    assert f"{BASE}/context" in page


def test_agent_note_stays_out_of_the_document_itself(api: Api) -> None:
    """The note lives in the wrapper; the artifact bytes are untouched."""
    artifact_id = _publish_markdown(api, "# Title\n\nRaw check")
    raw = api.client.get(f"/a/{artifact_id}/raw").text
    assert "ah-agents" not in raw
    assert "For AI agents" not in raw


def test_unlock_form_carries_the_note_for_agents(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Secret", password="hunter22")
    resp = api.client.get(f"/a/{artifact_id}")
    assert resp.status_code == 401
    assert '<nav class="ah-agents" aria-label="For AI agents">' in resp.text
    assert f"{BASE}/context" in resp.text
    assert f"{BASE}/a/{artifact_id}/raw" in resp.text


def test_every_artifact_response_carries_a_link_header(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title")
    expected = (
        f'<{BASE}/context>; rel="service-desc", '
        f'<{BASE}/llms.txt>; rel="help"; '
        'title="What this hub is and how to read a share link", '
        f'<{BASE}/skill>; rel="help"; title="SKILL.md for any agent runtime", '
        f'<{BASE}/agent>; rel="help"; title="Claude Code subagent definition"'
    )
    for path in (
        f"/a/{artifact_id}",
        f"/a/{artifact_id}/raw",
        f"/a/{artifact_id}/v/1",
        "/a/does-not-exist",
    ):
        resp = api.client.get(path)
        assert resp.headers.get("link") == expected, path


def test_link_header_is_scoped_to_artifact_paths(api: Api) -> None:
    assert "link" not in api.client.get("/context").headers
    assert "link" not in api.client.get("/health").headers


def test_llms_txt_points_at_the_hub_documents(api: Api) -> None:
    resp = api.client.get("/llms.txt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.text.startswith("# ")
    for url in HUB_DOCUMENTS:
        if url.endswith("/llms.txt"):
            continue
        assert url in resp.text, url
    assert f"{BASE}/a/{{id}}/raw" in resp.text
    assert f"{BASE}/a/{{id}}/export/markdown" in resp.text
    assert "X-Artifact-Password" in resp.text
    assert "X-Robots-Tag" not in resp.headers


def test_context_lists_llms_txt(api: Api) -> None:
    body = api.client.get("/context").json()
    paths = {entry["path"] for entry in body["endpoints"]}
    assert "/llms.txt" in paths
    assert body["documents"]["llms_txt"] == f"{BASE}/llms.txt"


# --------------------------------------------------------------------------
# HEAD: the header-only probe the Link header exists for must not be a 405.
# --------------------------------------------------------------------------


def test_head_on_artifact_page_mirrors_get_without_a_body(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title\n\nBody")
    get = api.client.get(f"/a/{artifact_id}")
    head = api.client.head(f"/a/{artifact_id}")
    assert head.status_code == 200
    assert head.content == b""
    for name in ("link", "x-robots-tag", "content-type", "content-security-policy"):
        assert head.headers.get(name) == get.headers.get(name), name


def test_head_is_not_counted_as_a_view(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title")
    assert api.client.head(f"/a/{artifact_id}").status_code == 200
    assert api.client.head(f"/a/{artifact_id}/raw").status_code == 200
    assert main.app.state.statedb.views(artifact_id)["total"] == 0
    assert api.client.get(f"/a/{artifact_id}").status_code == 200
    assert main.app.state.statedb.views(artifact_id)["total"] == 1


def test_head_works_on_the_discovery_documents(api: Api) -> None:
    for path in ("/", "/llms.txt", "/context", "/skill", "/agent", "/health"):
        resp = api.client.head(path)
        assert resp.status_code == 200, path
        assert resp.content == b"", path
        assert resp.headers.get("content-type"), path


def test_head_keeps_the_status_of_the_get_it_mirrors(api: Api) -> None:
    assert api.client.head("/a/does-not-exist").status_code == 404
    protected = _publish_markdown(api, "# Secret", password="hunter22")
    assert api.client.head(f"/a/{protected}").status_code == 401


def test_head_on_a_route_without_get_is_still_refused(api: Api) -> None:
    # /login/device is POST-only; HEAD must not conjure a GET that does not exist.
    assert api.client.head("/login/device").status_code == 405


# --------------------------------------------------------------------------
# The landing page tells humans (and the agents they paste it to) about llms.txt.
# --------------------------------------------------------------------------


def test_landing_page_mentions_llms_txt_for_agents(api: Api) -> None:
    page = api.client.get("/").text
    # Card, hero link row, the "for agents" section and the footer.
    assert page.count(f'href="{BASE}/llms.txt"') == 4
