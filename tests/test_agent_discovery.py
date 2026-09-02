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
    assert f'<link rel="help" href="{BASE}/skill">' in page
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
    expected = f'<{BASE}/context>; rel="service-desc", <{BASE}/skill>; rel="help"'
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
