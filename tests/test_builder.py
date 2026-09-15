"""Tests for src.builder: HTML/Markdown/git artifact building.

``build_from_git`` needs a public https clone to run end-to-end, which is not
available offline. Its input validation (https-only) is exercised directly,
and the private helpers it delegates to for entry-file selection and image
inlining (``_default_entry``, ``_resolve_entry``, ``_inline_images``) are
tested directly against a plain directory tree / a local git fixture repo,
since those helpers operate on any filesystem path and do not themselves
require a network clone.

Where the whole of ``build_from_git`` is what is under test — the order it
does its work in, and whether it agrees with ``build_from_html`` — the clone
is the only part stubbed out: ``_FakeClone`` copies a committed local fixture
into the destination and ``_check_git_host`` is silenced, so no test resolves
or reaches a host. Everything after the clone runs for real, the fixture's own
``.git`` included.
"""

import base64
import dataclasses
import os
import shutil
import subprocess
import types
from pathlib import Path

import pytest

import src.builder as builder_module
from src.builder import (
    BuildError,
    DEFAULT_GIT_USERNAME,
    DEFAULT_TITLE,
    FRAME_RUNTIME_MARKER,
    HLJS_VERSION,
    MERMAID_VERSION,
    REDACTED,
    _authed_clone_url,
    _check_git_host,
    _clone,
    _default_entry,
    _inline_images,
    _ip_is_blocked,
    _last_line,
    _repo_size_bytes,
    _resolve_entry,
    _scrub,
    _unwrap_frame_runtime,
    _validate_git_ref,
    _validate_git_url,
    build_from_git,
    build_from_html,
    build_from_markdown,
)

# A well-known minimal valid 1x1 PNG (base64), decoded at test time so the
# fixture always contains real, valid image bytes.
_PNG_1X1_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _png_bytes() -> bytes:
    return base64.b64decode(_PNG_1X1_B64)


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def _make_fixture_repo(tmp_path: Path) -> Path:
    """A small local git repo: README.md with a relative image reference."""
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    images_dir = repo / "images"
    images_dir.mkdir()
    (images_dir / "pixel.png").write_bytes(_png_bytes())
    (repo / "README.md").write_text(
        "# Fixture Repo\n\n![pixel](images/pixel.png)\n", encoding="utf-8"
    )

    _run_git(["git", "init"], repo)
    _run_git(["git", "config", "user.email", "test@example.com"], repo)
    _run_git(["git", "config", "user.name", "Test User"], repo)
    _run_git(["git", "add", "-A"], repo)
    _run_git(["git", "commit", "-m", "initial commit"], repo)
    return repo


# --------------------------------------------------------------------------
# build_from_html
# --------------------------------------------------------------------------


class TestBuildFromHtml:
    def test_title_tag_wins_over_h1(self):
        html = (
            "<html><head><title>From Title</title></head>"
            "<body><h1>From H1</h1></body></html>"
        )
        result = build_from_html(html)
        assert result.title == "From Title"
        assert result.source_type == "html"
        assert result.html == html

    def test_h1_used_when_no_title_tag(self):
        html = "<body><h1>From H1</h1></body>"
        result = build_from_html(html)
        assert result.title == "From H1"

    def test_explicit_title_wins_over_everything(self):
        html = "<html><head><title>From Title</title></head><body><h1>From H1</h1></body></html>"
        result = build_from_html(html, title="Explicit Title")
        assert result.title == "Explicit Title"

    def test_fallback_default_title(self):
        html = "<body><p>no headings here</p></body>"
        result = build_from_html(html)
        assert result.title == DEFAULT_TITLE


# --------------------------------------------------------------------------
# Claude frame-runtime unwrapping: a page saved from claude.ai's artifact
# viewer must be republished as the document its author wrote, not as the
# viewer's wrapper around it.
# --------------------------------------------------------------------------


# A saved artifact in miniature: the injected head (bootstrap script and style
# reset) followed by the authored document sitting in the body. The script
# carries a title-shaped string on purpose — the real one is further down, so
# anything reading titles before the rebuild picks the wrong one.
_WRAPPED = (
    "<!doctype html><html><head><!-- frame-runtime -->"
    '<script>window.__FRAME_PREAMBLE={"v":1};'
    'var frame_title="<title>claude.ai</title>";</script>'
    "<!-- /frame-runtime --><meta charset=utf8>"
    "<style>body{margin:0;font:14px system-ui;background:#faf9f5;color:#141413}"
    "</style></head><body>\n"
    "<title>Real Document</title>\n"
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=X">\n'
    "<style>:root{--ink:#111}</style>\n"
    "<main><h1>Heading</h1><p>Body copy.</p></main>\n"
    "</body></html>"
)


class TestUnwrapFrameRuntime:
    def test_wrapped_document_is_rebuilt_standalone(self):
        out, rebuilt = _unwrap_frame_runtime(_WRAPPED)
        assert rebuilt is True
        assert "__FRAME_PREAMBLE" not in out
        assert "frame-runtime" not in out
        assert out.startswith("<!doctype html>")
        assert out.rstrip().endswith("</html>")

    def test_authored_head_elements_are_hoisted_into_head(self):
        out, _ = _unwrap_frame_runtime(_WRAPPED)
        head, body = out.split("</head>", 1)
        assert "<title>Real Document</title>" in head
        assert "fonts.googleapis.com" in head
        assert "--ink:#111" in head
        # The scan stops at the first non-head element: content stays content.
        assert "<main>" not in head
        assert "<main><h1>Heading</h1>" in body

    def test_injected_reset_style_is_discarded(self):
        """The reset forces a cream background and 14px system font."""
        out, _ = _unwrap_frame_runtime(_WRAPPED)
        assert "#faf9f5" not in out
        assert "14px system-ui" not in out

    def test_body_tag_with_attributes_is_handled(self):
        out, rebuilt = _unwrap_frame_runtime(
            _WRAPPED.replace("<body>", '<body class="x" data-y="1">')
        )
        assert rebuilt is True
        assert "<title>Real Document</title>" in out
        assert 'data-y="1"' not in out

    def test_wrapper_without_authored_head_elements_still_works(self):
        wrapped = (
            "<!doctype html><html><head><!-- frame-runtime -->"
            "<script>var a=1;</script></head><body><p>Just content.</p></body></html>"
        )
        out, rebuilt = _unwrap_frame_runtime(wrapped)
        assert rebuilt is True
        assert "var a=1" not in out
        assert "<p>Just content.</p>" in out.split("</head>", 1)[1]

    @pytest.mark.parametrize("closing_marker", ["<!-- /frame-runtime -->", ""])
    def test_a_tag_shaped_string_inside_the_bootstrap_is_not_markup(
        self, closing_marker
    ):
        """The body is looked for past `</head>`, never inside the script.

        The injected script is ~13 KB of opaque JavaScript. Splitting the
        document at the first `<body` *substring* would cut the document in
        half here and publish the tail of the script as content.
        """
        wrapped = (
            "<!doctype html><html><head><!-- frame-runtime -->"
            "<script>var t='<body onload=x>';</script>"
            f"{closing_marker}</head><body>\n"
            "<title>Real</title>\n<p>content</p>\n</body></html>"
        )
        out, rebuilt = _unwrap_frame_runtime(wrapped)
        assert rebuilt is True
        assert "<script>" not in out
        assert "frame-runtime" not in out
        assert "<title>Real</title>" in out.split("</head>", 1)[0]
        assert "<p>content</p>" in out.split("</head>", 1)[1]

    def test_plain_document_passes_through_unchanged(self):
        plain = (
            "<!doctype html><html><head><title>Mine</title></head>"
            "<body><p>Hi</p></body></html>"
        )
        out, rebuilt = _unwrap_frame_runtime(plain)
        assert rebuilt is False
        assert out == plain

    def test_a_document_that_only_writes_about_the_marker_is_left_alone(self):
        """The marker in the body is prose; the author's own head must survive."""
        about = (
            "<!doctype html><html><head><title>How the wrapper works</title>"
            '<script src="mine.js"></script></head><body><p>Claude injects '
            f"<code>{FRAME_RUNTIME_MARKER}</code> into the head.</p></body></html>"
        )
        out, rebuilt = _unwrap_frame_runtime(about)
        assert rebuilt is False
        assert out == about

    def test_a_page_with_an_implicit_head_that_mentions_the_marker_survives(self):
        """Requiring an enclosing <head> also covers documents that have none.

        Rejecting only a </head> *before* the marker left this shape exposed: a
        page about HTML parsing, with no head tags of its own, carrying the
        structural tags inside a script string. Rebuilding it dropped the
        opening content and promoted an inert JS fragment to live body text.
        """
        about = (
            "<!doctype html><html><body>"
            "<h1>How saved pages are wrapped</h1>"
            f"<p>The viewer injects <code>{FRAME_RUNTIME_MARKER}</code> first.</p>"
            '<script>var sample = "</head><body>";</script>'
            "<p>Keep reading.</p></body></html>"
        )
        out, rebuilt = _unwrap_frame_runtime(about)
        assert rebuilt is False
        assert out == about

    @pytest.mark.parametrize(
        "malformed",
        [
            pytest.param(
                "<!doctype html><html><head><!-- frame-runtime -->"
                "<script>x</script></head>",
                id="head-closes-but-no-body",
            ),
            pytest.param(FRAME_RUNTIME_MARKER, id="marker-alone"),
            pytest.param(
                "<!doctype html><html><head><!-- frame-runtime --></head>"
                "<body>   </body></html>",
                id="empty-body",
            ),
        ],
    )
    def test_malformed_wrapper_returns_the_input_unchanged(self, malformed):
        """Unknown shapes pass through: this runs over every HTML publish."""
        out, rebuilt = _unwrap_frame_runtime(malformed)
        assert rebuilt is False
        assert out == malformed


class TestBuildFromHtmlUnwrapsFrameRuntime:
    def test_the_rebuilt_document_is_what_gets_published(self):
        result = build_from_html(_WRAPPED)
        assert result.source_type == "html"
        assert result.html == _unwrap_frame_runtime(_WRAPPED)[0]
        assert len(result.html) < len(_WRAPPED)

    def test_the_authored_title_still_wins(self):
        """Unwrapping runs first, so the bootstrap script cannot supply it."""
        assert build_from_html(_WRAPPED).title == "Real Document"

    def test_an_explicit_title_still_wins(self):
        result = build_from_html(_WRAPPED, title="Explicit Title")
        assert result.title == "Explicit Title"
        assert "__FRAME_PREAMBLE" not in result.html

    def test_an_unwrapped_document_is_published_byte_for_byte(self):
        html = "<html><head><title>Mine</title></head><body><h1>Hi</h1></body></html>"
        assert build_from_html(html).html == html


# --------------------------------------------------------------------------
# The same rebuild on the git path. A saved artifact committed to a repository
# is the same saved artifact, so publishing it by git_url rather than by html
# must not be the difference between a wrapper stripped and a wrapper served.
# --------------------------------------------------------------------------


_GIT_URL = "https://git.example.com/owner/repo.git"

#: The wrapper carrying a repository-relative image on both sides of the line
#: the rebuild draws: one in the injected head it discards, one in the authored
#: body it keeps. Both name the same file, so only the *order* of the rebuild
#: and the image inlining can tell them apart.
_WRAPPED_WITH_IMAGES = _WRAPPED.replace(
    "<!-- /frame-runtime -->",
    '<!-- /frame-runtime --><img src="images/pixel.png" alt="injected">',
).replace(
    "<p>Body copy.</p>",
    '<p>Body copy.</p><img src="images/pixel.png" alt="authored">',
)


class _FakeClone:
    """Stand-in for ``_clone``: copies a committed fixture into ``dest``.

    The clone is the one part of ``build_from_git`` that cannot run offline.
    Everything downstream works on whatever landed on disk, so a copied fixture
    exercises the real path — ``_head_commit`` included, since the copy brings
    the fixture's ``.git`` with it.
    """

    def __init__(self, fixture: Path) -> None:
        self.fixture = fixture

    def __call__(self, git_url, ref, dest, timeout_s, **kwargs) -> None:
        shutil.copytree(self.fixture, dest)


def _make_entry_repo(tmp_path: Path, name: str, content: str) -> Path:
    """A committed repo whose entry is ``name``, beside ``images/pixel.png``."""
    repo = tmp_path / "entry-repo"
    repo.mkdir()
    (repo / "images").mkdir()
    (repo / "images" / "pixel.png").write_bytes(_png_bytes())
    (repo / name).write_text(content, encoding="utf-8")

    _run_git(["git", "init"], repo)
    _run_git(["git", "config", "user.email", "test@example.com"], repo)
    _run_git(["git", "config", "user.name", "Test User"], repo)
    _run_git(["git", "add", "-A"], repo)
    _run_git(["git", "commit", "-m", "initial commit"], repo)
    return repo


def _publish_from_git(
    tmp_path,
    monkeypatch,
    settings,
    content: str,
    *,
    name: str = "index.html",
    title: str | None = None,
):
    """Run ``build_from_git`` end to end over a one-entry local repository."""
    monkeypatch.setattr(builder_module, "_check_git_host", lambda *a, **k: None)
    monkeypatch.setattr(
        builder_module, "_clone", _FakeClone(_make_entry_repo(tmp_path, name, content))
    )
    return build_from_git(_GIT_URL, None, None, title, settings)


class TestBuildFromGitUnwrapsFrameRuntime:
    def test_a_wrapped_entry_file_is_rebuilt(self, tmp_path, monkeypatch, settings):
        result = _publish_from_git(tmp_path, monkeypatch, settings, _WRAPPED)
        assert result.source_type == "git-html"
        assert "__FRAME_PREAMBLE" not in result.html
        assert "frame-runtime" not in result.html
        assert "#faf9f5" not in result.html  # the injected reset goes too
        assert "<main><h1>Heading</h1>" in result.html

    def test_the_git_path_stores_what_the_html_path_would(
        self, tmp_path, monkeypatch, settings
    ):
        """The whole point: the same bytes, the same stored document."""
        direct = build_from_html(_WRAPPED)
        result = _publish_from_git(tmp_path, monkeypatch, settings, _WRAPPED)
        assert result.html == direct.html
        assert result.title == direct.title

    def test_the_authored_title_still_wins(self, tmp_path, monkeypatch, settings):
        """Derived from the rebuilt document, not from the bootstrap script."""
        result = _publish_from_git(tmp_path, monkeypatch, settings, _WRAPPED)
        assert result.title == "Real Document"

    def test_an_explicit_title_still_wins(self, tmp_path, monkeypatch, settings):
        result = _publish_from_git(
            tmp_path, monkeypatch, settings, _WRAPPED, title="Explicit Title"
        )
        assert result.title == "Explicit Title"

    def test_the_commit_is_still_recorded(self, tmp_path, monkeypatch, settings):
        result = _publish_from_git(tmp_path, monkeypatch, settings, _WRAPPED)
        assert result.git_commit is not None
        assert len(result.git_commit) >= 40

    def test_ordinary_html_is_published_byte_for_byte(
        self, tmp_path, monkeypatch, settings
    ):
        plain = (
            "<!doctype html><html><head><title>Mine</title></head>"
            "<body><h1>Hi</h1><p>Nothing to rebuild here.</p></body></html>"
        )
        result = _publish_from_git(tmp_path, monkeypatch, settings, plain)
        assert result.html == plain
        assert result.title == "Mine"

    def test_a_page_that_only_writes_about_the_marker_is_left_alone(
        self, tmp_path, monkeypatch, settings
    ):
        """A repository documenting the wrapper keeps the head it committed."""
        about = (
            "<!doctype html><html><head><title>How the wrapper works</title>"
            '<script src="mine.js"></script></head><body><p>Claude injects '
            f"<code>{FRAME_RUNTIME_MARKER}</code> into the head.</p></body></html>"
        )
        result = _publish_from_git(tmp_path, monkeypatch, settings, about)
        assert result.html == about

    def test_a_markdown_entry_is_unaffected(self, tmp_path, monkeypatch, settings):
        """The Markdown branch renders and inlines exactly as it did before."""
        result = _publish_from_git(
            tmp_path,
            monkeypatch,
            settings,
            "# Fixture Repo\n\n![pixel](images/pixel.png)\n",
            name="README.md",
        )
        assert result.source_type == "git-markdown"
        assert result.title == "Fixture Repo"
        assert "data:image/png;base64," in result.html
        assert "images/pixel.png" not in result.html


class TestGitRebuildRunsBeforeImageInlining:
    """Order matters, and only one order is right.

    Inlining first would rewrite ``src`` attributes inside ~13 KB of bootstrap
    script that the rebuild is about to throw away, and spend the shared inline
    budget doing it. Rebuilding first leaves the inliner nothing but authored
    markup to look at.
    """

    def test_only_the_authored_image_is_inlined(
        self, tmp_path, monkeypatch, settings
    ):
        result = _publish_from_git(
            tmp_path, monkeypatch, settings, _WRAPPED_WITH_IMAGES
        )
        assert 'alt="injected"' not in result.html
        assert 'alt="authored"' in result.html
        assert result.html.count("data:image/png;base64,") == 1
        assert "images/pixel.png" not in result.html

    def test_the_discarded_wrapper_cannot_spend_the_inline_budget(
        self, tmp_path, monkeypatch, settings
    ):
        """A budget for exactly one image must reach the author's image.

        Inlining before the rebuild would hand that one budget to the injected
        reference — it comes first in the document — and then discard it,
        leaving the authored image as a dead relative link to a repository the
        hub does not serve.
        """
        room_for_one = dataclasses.replace(
            settings, max_inline_total_bytes=len(_png_bytes())
        )
        result = _publish_from_git(
            tmp_path, monkeypatch, room_for_one, _WRAPPED_WITH_IMAGES
        )
        assert result.html.count("data:image/png;base64,") == 1
        assert "images/pixel.png" not in result.html


# --------------------------------------------------------------------------
# build_from_markdown
# --------------------------------------------------------------------------


class TestBuildFromMarkdown:
    def test_table_renders(self):
        md = "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
        result = build_from_markdown(md)
        assert "<table>" in result.html
        assert "<th>a</th>" in result.html
        assert "<td>1</td>" in result.html

    def test_mermaid_fence_becomes_pre_mermaid(self):
        md = "```mermaid\ngraph TD;\nA-->B;\n```\n"
        result = build_from_markdown(md)
        assert '<pre class="mermaid">' in result.html
        assert "graph TD" in result.html

    def test_python_fence_gets_language_class(self):
        md = "```python\nprint('hi')\n```\n"
        result = build_from_markdown(md)
        assert "language-python" in result.html

    def test_front_matter_title(self):
        md = '---\ntitle: "From Front Matter"\n---\n\n# A Heading\n'
        result = build_from_markdown(md)
        assert result.title == "From Front Matter"

    def test_h1_title_fallback_when_no_front_matter(self):
        md = "# My Heading\n\nSome body text.\n"
        result = build_from_markdown(md)
        assert result.title == "My Heading"

    def test_fallback_default_title(self):
        md = "Just a paragraph, no heading at all.\n"
        result = build_from_markdown(md)
        assert result.title == DEFAULT_TITLE

    def test_full_page_with_dark_mode_media_query(self):
        md = "# Title\n\nBody text.\n"
        result = build_from_markdown(md)
        lowered = result.html.lower()
        assert "<!doctype html" in lowered
        assert "<html" in lowered
        assert "prefers-color-scheme: dark" in result.html
        assert result.source_type == "markdown"


# --------------------------------------------------------------------------
# build_from_git: URL validation
# --------------------------------------------------------------------------


class TestValidateGitUrl:
    def test_https_url_accepted(self):
        url = "https://github.com/owner/repo.git"
        assert _validate_git_url(url) == url

    def test_file_url_rejected(self):
        with pytest.raises(BuildError):
            _validate_git_url("file:///etc/passwd")

    def test_ftp_scheme_rejected(self):
        with pytest.raises(BuildError):
            _validate_git_url("ftp://example.com/repo.git")

    def test_plain_http_rejected(self):
        with pytest.raises(BuildError):
            _validate_git_url("http://example.com/repo.git")

    def test_scheme_only_no_hostname_rejected(self):
        with pytest.raises(BuildError):
            _validate_git_url("https://")


class TestBuildFromGitRejectsNonHttps(object):
    """build_from_git must reject non-https URLs before attempting a clone."""

    def test_file_url_rejected(self, settings):
        with pytest.raises(BuildError):
            build_from_git("file:///tmp/some-repo", None, None, None, settings)

    def test_ftp_url_rejected(self, settings):
        with pytest.raises(BuildError):
            build_from_git("ftp://example.com/repo.git", None, None, None, settings)

    def test_ssh_style_url_rejected(self, settings):
        with pytest.raises(BuildError):
            build_from_git(
                "git@github.com:owner/repo.git", None, None, None, settings
            )


# --------------------------------------------------------------------------
# Private-repository clone credentials
# --------------------------------------------------------------------------


class TestAuthedCloneUrl:
    """URL construction for an authenticated clone.

    This is the only place the git token is ever materialized, so its exact
    shape (and its quoting) is worth pinning down.
    """

    def test_default_username_is_used_when_none_given(self):
        assert (
            _authed_clone_url("https://github.com/owner/repo.git", None, "ghp_abc")
            == f"https://{DEFAULT_GIT_USERNAME}:ghp_abc@github.com/owner/repo.git"
        )

    def test_explicit_username_is_used(self):
        assert (
            _authed_clone_url("https://gitlab.com/g/p.git", "deploy-user", "s3cr3t")
            == "https://deploy-user:s3cr3t@gitlab.com/g/p.git"
        )

    def test_special_characters_are_percent_encoded(self):
        # A token containing '@', ':' and '/' must not be able to break out of
        # the userinfo section and rewrite the host or the path.
        url = _authed_clone_url(
            "https://github.com/owner/repo.git", "user@example.com", "p@ss:w/rd +x"
        )
        assert url == (
            "https://user%40example.com:p%40ss%3Aw%2Frd%20%2Bx"
            "@github.com/owner/repo.git"
        )

    def test_host_and_port_are_preserved(self):
        url = _authed_clone_url("https://git.example.com:8443/g/p.git", None, "tok")
        assert url.endswith("@git.example.com:8443/g/p.git")

    def test_existing_userinfo_is_replaced_not_appended(self):
        url = _authed_clone_url("https://someone:old@github.com/o/r.git", None, "new")
        assert url == f"https://{DEFAULT_GIT_USERNAME}:new@github.com/o/r.git"
        assert "old" not in url

    def test_no_credentials_leak_into_the_unauthenticated_url(self):
        original = "https://github.com/owner/repo.git"
        _authed_clone_url(original, None, "ghp_abc")
        assert original == "https://github.com/owner/repo.git"


class _FailedGit:
    """Stand-in for a ``subprocess.CompletedProcess`` from a failed git call."""

    returncode = 128
    stdout = ""

    def __init__(self, stderr: str) -> None:
        self.stderr = stderr


class TestScrubbing:
    """Nothing derived from git output may carry the token back to the caller."""

    def test_authed_clone_url_in_git_stderr_is_scrubbed(self):
        token = "ghp_SuperSecretToken123"
        authed = _authed_clone_url("https://github.com/o/private.git", None, token)
        stderr = (
            "Cloning into '/tmp/artifact-git-x/repo'...\n"
            f"fatal: unable to access '{authed}/': "
            "The requested URL returned error: 403\n"
        )
        # Exactly how _clone builds the user-facing detail of a BuildError.
        reason = _last_line(stderr, token)
        message = str(BuildError(f"Could not clone the repository. git said: {reason}"))

        assert token not in message
        assert DEFAULT_GIT_USERNAME not in message
        assert "github.com" in message  # the useful part survives

    def test_token_with_special_chars_is_scrubbed_in_encoded_form(self):
        token = "p@ss/w:rd"
        authed = _authed_clone_url("https://github.com/o/p.git", None, token)
        assert _scrub(f"fatal: could not read from {authed}", token) == (
            "fatal: could not read from https://github.com/o/p.git"
        )

    def test_bare_token_without_url_is_still_redacted(self):
        token = "glpat-Abc123"
        scrubbed = _scrub(f"remote: token {token} is expired", token)
        assert token not in scrubbed
        assert REDACTED in scrubbed

    def test_scrub_without_secret_still_strips_userinfo(self):
        assert _scrub("https://user:pw@example.com/r.git") == (
            "https://example.com/r.git"
        )

    def test_private_repo_error_points_at_git_token(self, monkeypatch, tmp_path):
        """An unauthenticated clone of a private repo must suggest git_token."""
        monkeypatch.setattr(
            builder_module,
            "_run_git",
            lambda args, timeout_s, **kwargs: _FailedGit(
                "fatal: could not read Username for 'https://github.com': "
                "terminal prompts disabled\n"
            ),
        )
        with pytest.raises(BuildError) as exc:
            builder_module._clone(
                "https://github.com/o/private.git", None, tmp_path / "repo", 5
            )
        assert "git_token" in str(exc.value)

    def test_failed_authed_clone_error_carries_no_token(self, monkeypatch, tmp_path):
        """The end-to-end path: a rejected authenticated clone leaks nothing."""
        token = "ghp_LeakMeIfYouCan"
        authed = _authed_clone_url("https://github.com/o/private.git", None, token)
        monkeypatch.setattr(
            builder_module,
            "_run_git",
            lambda args, timeout_s, **kwargs: _FailedGit(
                f"fatal: unable to access '{authed}/': "
                "The requested URL returned error: 403\n"
            ),
        )
        with pytest.raises(BuildError) as exc:
            builder_module._clone(
                "https://github.com/o/private.git",
                "main",
                tmp_path / "repo",
                5,
                token=token,
            )
        message = str(exc.value)
        assert token not in message
        assert DEFAULT_GIT_USERNAME not in message


# --------------------------------------------------------------------------
# Entry-file selection helpers (private, but importable and independently
# testable: they operate on a plain directory tree, no git/network involved)
# --------------------------------------------------------------------------


class TestDefaultEntrySelection:
    def test_prefers_index_html(self, tmp_path):
        (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
        (tmp_path / "README.md").write_text("# Readme", encoding="utf-8")
        entry = _default_entry(tmp_path, "the repository root")
        assert entry.name == "index.html"

    def test_falls_back_to_readme(self, tmp_path):
        (tmp_path / "README.md").write_text("# Readme", encoding="utf-8")
        entry = _default_entry(tmp_path, "the repository root")
        assert entry.name == "README.md"

    def test_readme_lookup_is_case_insensitive(self, tmp_path):
        (tmp_path / "readme.markdown").write_text("# Readme", encoding="utf-8")
        entry = _default_entry(tmp_path, "the repository root")
        assert entry.name == "readme.markdown"

    def test_falls_back_to_single_html_file(self, tmp_path):
        (tmp_path / "page.html").write_text("<html></html>", encoding="utf-8")
        entry = _default_entry(tmp_path, "the repository root")
        assert entry.name == "page.html"

    def test_raises_when_nothing_matches(self, tmp_path):
        (tmp_path / "notes.txt").write_text("hi", encoding="utf-8")
        with pytest.raises(BuildError):
            _default_entry(tmp_path, "the repository root")

    def test_raises_when_multiple_html_files_ambiguous(self, tmp_path):
        (tmp_path / "a.html").write_text("<html></html>", encoding="utf-8")
        (tmp_path / "b.html").write_text("<html></html>", encoding="utf-8")
        with pytest.raises(BuildError):
            _default_entry(tmp_path, "the repository root")


class TestResolveEntry:
    def test_explicit_path_to_file(self, tmp_path):
        (tmp_path / "docs.md").write_text("# Docs", encoding="utf-8")
        entry = _resolve_entry(tmp_path, "docs.md")
        assert entry.name == "docs.md"

    def test_explicit_path_to_directory_uses_default_entry(self, tmp_path):
        sub = tmp_path / "docs"
        sub.mkdir()
        (sub / "index.html").write_text("<html></html>", encoding="utf-8")
        entry = _resolve_entry(tmp_path, "docs")
        assert entry.name == "index.html"

    def test_no_path_uses_default_entry(self, tmp_path):
        (tmp_path / "index.htm").write_text("<html></html>", encoding="utf-8")
        entry = _resolve_entry(tmp_path, None)
        assert entry.name == "index.htm"

    def test_rejects_path_escaping_repo_root(self, tmp_path):
        with pytest.raises(BuildError):
            _resolve_entry(tmp_path, "../outside.md")

    def test_rejects_disallowed_suffix(self, tmp_path):
        (tmp_path / "data.json").write_text("{}", encoding="utf-8")
        with pytest.raises(BuildError):
            _resolve_entry(tmp_path, "data.json")

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(BuildError):
            _resolve_entry(tmp_path, "missing.md")


# --------------------------------------------------------------------------
# Image inlining helper (private, testable against a local fixture repo)
# --------------------------------------------------------------------------


class TestInlineImages:
    def test_relative_image_is_inlined_as_data_uri(self, tmp_path, settings):
        repo = _make_fixture_repo(tmp_path)
        html = '<img src="images/pixel.png" alt="pixel">'
        result = _inline_images(
            html,
            base_dir=repo,
            repo_root=repo,
            max_image_bytes=settings.max_inline_image_bytes,
            max_total_bytes=settings.max_inline_total_bytes,
        )
        assert "data:image/png;base64," in result
        assert "images/pixel.png" not in result

    def test_external_url_left_untouched(self, tmp_path, settings):
        repo = _make_fixture_repo(tmp_path)
        html = '<img src="https://example.com/pic.png">'
        result = _inline_images(
            html,
            base_dir=repo,
            repo_root=repo,
            max_image_bytes=settings.max_inline_image_bytes,
            max_total_bytes=settings.max_inline_total_bytes,
        )
        assert result == html

    def test_oversized_image_left_as_link(self, tmp_path, settings):
        repo = _make_fixture_repo(tmp_path)
        html = '<img src="images/pixel.png">'
        result = _inline_images(
            html,
            base_dir=repo,
            repo_root=repo,
            max_image_bytes=1,  # smaller than the fixture PNG
            max_total_bytes=settings.max_inline_total_bytes,
        )
        assert result == html

    def test_total_budget_exhausted_leaves_image_as_link(self, tmp_path, settings):
        repo = _make_fixture_repo(tmp_path)
        html = '<img src="images/pixel.png">'
        result = _inline_images(
            html,
            base_dir=repo,
            repo_root=repo,
            max_image_bytes=settings.max_inline_image_bytes,
            max_total_bytes=0,
        )
        assert result == html

    def test_image_outside_repo_root_left_untouched(self, tmp_path, settings):
        repo = _make_fixture_repo(tmp_path)
        (tmp_path / "outside.png").write_bytes(_png_bytes())
        html = '<img src="../outside.png">'
        result = _inline_images(
            html,
            base_dir=repo,
            repo_root=repo,
            max_image_bytes=settings.max_inline_image_bytes,
            max_total_bytes=settings.max_inline_total_bytes,
        )
        assert result == html

    def test_missing_image_left_untouched(self, tmp_path, settings):
        repo = _make_fixture_repo(tmp_path)
        html = '<img src="images/does-not-exist.png">'
        result = _inline_images(
            html,
            base_dir=repo,
            repo_root=repo,
            max_image_bytes=settings.max_inline_image_bytes,
            max_total_bytes=settings.max_inline_total_bytes,
        )
        assert result == html


# --------------------------------------------------------------------------
# Clone credential handling: token must stay OUT of argv, and git must not
# inherit the whole process environment (sec-secrets / sec-crypto-data).
# --------------------------------------------------------------------------


class _CapturingRun:
    """Drop-in for ``subprocess.run`` recording argv and the env kwarg."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.calls: list[dict] = []
        self._rc = returncode
        self._out = stdout
        self._err = stderr

    def __call__(self, args, **kwargs):
        self.calls.append({"args": list(args), "kwargs": kwargs})
        return types.SimpleNamespace(
            returncode=self._rc, stdout=self._out, stderr=self._err
        )


class TestCloneCredentialHandling:
    def test_token_never_appears_in_argv(self, monkeypatch, tmp_path):
        cap = _CapturingRun()
        monkeypatch.setattr(builder_module.subprocess, "run", cap)
        token = "ghp_SuperSecretToken123"
        _clone(
            "https://github.com/o/private.git",
            "main",
            tmp_path / "repo",
            5,
            token=token,
        )
        assert cap.calls, "git clone was not invoked"
        argv = cap.calls[0]["args"]
        joined = " ".join(argv)
        assert token not in joined
        assert "x-access-token:" not in joined
        # The clean, unauthenticated URL is what lands in argv.
        assert "https://github.com/o/private.git" in argv

    def test_username_and_token_not_in_argv(self, monkeypatch, tmp_path):
        cap = _CapturingRun()
        monkeypatch.setattr(builder_module.subprocess, "run", cap)
        _clone(
            "https://gitlab.com/g/p.git",
            None,
            tmp_path / "repo",
            5,
            username="deploy-bot",
            token="s3cr3t-token",
        )
        joined = " ".join(cap.calls[0]["args"])
        assert "deploy-bot" not in joined
        assert "s3cr3t-token" not in joined

    def test_git_env_is_curated_no_unrelated_secret(self, monkeypatch):
        cap = _CapturingRun()
        monkeypatch.setattr(builder_module.subprocess, "run", cap)
        monkeypatch.setenv("HUB_SENTINEL_SECRET", "leak-me-not")
        # NB: builder_module._run_git, not the local test helper of the same name.
        builder_module._run_git(["git", "--version"], 5)
        env = cap.calls[0]["kwargs"]["env"]
        assert "HUB_SENTINEL_SECRET" not in env
        assert "leak-me-not" not in env.values()
        assert env.get("GIT_TERMINAL_PROMPT") == "0"
        assert "PATH" in env  # git still resolvable

    def test_clone_env_excludes_app_secret_but_carries_askpass(
        self, monkeypatch, tmp_path
    ):
        cap = _CapturingRun()
        monkeypatch.setattr(builder_module.subprocess, "run", cap)
        monkeypatch.setenv("HUB_SENTINEL_SECRET", "leak-me-not")
        _clone(
            "https://github.com/o/p.git",
            None,
            tmp_path / "repo",
            5,
            token="ghp_x",
        )
        env = cap.calls[0]["kwargs"]["env"]
        assert "leak-me-not" not in env.values()
        assert "HUB_SENTINEL_SECRET" not in env
        # The credential travels via a GIT_ASKPASS helper, never argv.
        assert env.get("GIT_ASKPASS")

    def test_partial_clone_blob_filter_is_added(self, monkeypatch, tmp_path):
        cap = _CapturingRun()
        monkeypatch.setattr(builder_module.subprocess, "run", cap)
        _clone(
            "https://github.com/o/p.git",
            None,
            tmp_path / "repo",
            5,
            blob_limit_bytes=1024,
        )
        argv = cap.calls[0]["args"]
        assert "--filter=blob:limit=1024" in argv


# --------------------------------------------------------------------------
# SSRF guard on the git host (sec-ssrf)
# --------------------------------------------------------------------------


class TestGitHostSsrfGuard:
    def test_metadata_ip_rejected(self):
        with pytest.raises(BuildError):
            _check_git_host(
                "https://evil.example.com/r.git",
                allow_private=False,
                resolver=lambda h: ["169.254.169.254"],
            )

    def test_a_resolver_error_is_refused_not_allowed(self):
        """The host guard must fail closed, like the webhook one.

        This check and git's own resolution are separate lookups, so "could
        not resolve" is not evidence the destination is safe: a DNS server
        the attacker controls can fail this probe and answer git with an
        internal address a moment later.
        """
        def unresolvable(_hostname: str) -> list[str]:
            raise OSError("SERVFAIL")

        with pytest.raises(BuildError, match="resolve"):
            _check_git_host(
                "https://evil.example.com/r.git",
                allow_private=False,
                resolver=unresolvable,
            )

    def test_an_empty_answer_is_refused(self):
        with pytest.raises(BuildError, match="resolve"):
            _check_git_host(
                "https://evil.example.com/r.git",
                allow_private=False,
                resolver=lambda _h: [],
            )

    def test_private_10_range_rejected(self):
        with pytest.raises(BuildError):
            _check_git_host(
                "https://evil.example.com/r.git",
                allow_private=False,
                resolver=lambda h: ["10.1.2.3"],
            )

    def test_public_host_allowed(self):
        # Must not raise.
        _check_git_host(
            "https://github.com/o/r.git",
            allow_private=False,
            resolver=lambda h: ["140.82.112.3"],
        )

    def test_literal_loopback_ip_short_circuits_dns(self):
        called = {"n": 0}

        def resolver(h):
            called["n"] += 1
            return ["9.9.9.9"]

        with pytest.raises(BuildError):
            _check_git_host(
                "https://127.0.0.1/r.git", allow_private=False, resolver=resolver
            )
        assert called["n"] == 0  # a literal blocked IP is caught without DNS

    def test_metadata_hostname_rejected(self):
        with pytest.raises(BuildError):
            _check_git_host(
                "https://metadata.google.internal/r.git",
                allow_private=False,
                resolver=lambda h: ["8.8.8.8"],
            )

    def test_dot_internal_suffix_rejected(self):
        with pytest.raises(BuildError):
            _check_git_host(
                "https://build.svc.internal/r.git",
                allow_private=False,
                resolver=lambda h: ["8.8.8.8"],
            )

    def test_allow_private_bypasses_guard(self):
        # Even a loopback literal is allowed when explicitly opted in.
        _check_git_host(
            "https://127.0.0.1/r.git",
            allow_private=True,
            resolver=lambda h: ["10.0.0.1"],
        )

    def test_ipv4_mapped_ipv6_metadata_blocked(self):
        assert _ip_is_blocked("::ffff:169.254.169.254")

    def test_public_ip_not_blocked(self):
        assert not _ip_is_blocked("140.82.112.3")

    def test_clone_time_recheck_blocks_dns_rebinding(self, settings, monkeypatch):
        """Host is public at the pre-check, private at the clone-time re-check.

        build_from_git re-resolves and re-validates immediately before the clone
        subprocess, so a rebind after the initial _check_git_host must raise a
        BuildError and never reach the clone.
        """
        answers = iter([["140.82.112.3"], ["10.0.0.5"]])

        def flipping_resolver(_hostname):
            return next(answers)

        monkeypatch.setattr(builder_module, "_resolve_host_ips", flipping_resolver)

        def _must_not_clone(*args, **kwargs):
            raise AssertionError("clone must not run after a blocked re-resolve")

        monkeypatch.setattr(builder_module, "_clone", _must_not_clone)

        with pytest.raises(BuildError, match="private"):
            build_from_git(
                "https://evil.example.com/r.git", None, None, None, settings
            )

    def test_build_from_git_blocks_private_resolution(self, monkeypatch, settings):
        monkeypatch.setattr(
            builder_module, "_resolve_host_ips", lambda h: ["10.0.0.9"]
        )
        with pytest.raises(BuildError):
            build_from_git(
                "https://sneaky.example.com/r.git", None, None, None, settings
            )


# --------------------------------------------------------------------------
# Clone size bound now counts the .git directory (sec-abuse-limits / file-deser)
# --------------------------------------------------------------------------


class TestRepoSizeCountsGit:
    def test_git_directory_is_counted(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "pack").write_bytes(b"x" * 5000)
        (repo / "index.html").write_text("<html></html>", encoding="utf-8")
        size = _repo_size_bytes(repo)
        # A huge history in .git is now included in the hard-stop measurement.
        assert size >= 5000 + len("<html></html>")

    def test_symlink_not_followed_for_size(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        big = tmp_path / "big.bin"
        big.write_bytes(b"y" * 10000)
        (repo / "link.bin").symlink_to(big)
        # Symlink target bytes are not counted (containment enforced elsewhere).
        assert _repo_size_bytes(repo) == 0


# --------------------------------------------------------------------------
# Symlink escape in entry selection (sec-injection / sec-file-deser)
# --------------------------------------------------------------------------


class TestEntrySymlinkContainment:
    def test_default_readme_symlink_escape_rejected(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        secret = tmp_path / "passwd_like"
        secret.write_text("root:x:0:0:\n", encoding="utf-8")
        (repo / "README.md").symlink_to(secret)
        with pytest.raises(BuildError):
            _resolve_entry(repo, None)

    def test_explicit_index_symlink_escape_rejected(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        secret = tmp_path / "passwd_like"
        secret.write_text("root:x:0:0:\n", encoding="utf-8")
        (repo / "index.html").symlink_to(secret)
        with pytest.raises(BuildError):
            _resolve_entry(repo, "index.html")

    def test_legit_entry_still_resolves(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "README.md").write_text("# hello\n", encoding="utf-8")
        assert _resolve_entry(repo, None).name == "README.md"

    def test_image_symlink_escape_left_as_link(self, tmp_path, settings):
        repo = tmp_path / "repo"
        (repo / "images").mkdir(parents=True)
        outside = tmp_path / "outside.png"
        outside.write_bytes(_png_bytes())
        (repo / "images" / "evil.png").symlink_to(outside)
        html = '<img src="images/evil.png">'
        result = _inline_images(
            html,
            base_dir=repo,
            repo_root=repo,
            max_image_bytes=settings.max_inline_image_bytes,
            max_total_bytes=settings.max_inline_total_bytes,
        )
        assert result == html  # escaping symlink is never inlined


# --------------------------------------------------------------------------
# CDN pinning + intentional raw-HTML rendering (sec-web)
# --------------------------------------------------------------------------


class TestCdnPinningAndRawHtml:
    def test_cdn_versions_are_exact_patch_pins(self):
        assert HLJS_VERSION.count(".") == 2
        assert MERMAID_VERSION.count(".") == 2

    def test_rendered_page_uses_pinned_versions_not_floating_major(self):
        page = build_from_markdown("# hi\n").html
        assert f"mermaid@{MERMAID_VERSION}" in page
        assert f"cdn-release@{HLJS_VERSION}" in page
        assert "mermaid@11/" not in page
        assert "cdn-release@11/" not in page

    def test_raw_html_in_markdown_is_preserved_by_design(self):
        # html:True is intentional; the sandbox iframe is the boundary, so raw
        # HTML must pass through here rather than being stripped/escaped.
        md = 'Before\n\n<div class="marker">raw</div>\n\nAfter\n'
        page = build_from_markdown(md).html
        assert '<div class="marker">raw</div>' in page


class TestCommitShaRefIsRefusedClearly:
    """git clone --branch takes a branch or a tag, never a commit id.

    The public contract used to offer "branch, tag or commit", so an
    immutable-source workflow passing a commit id got a raw git failure from
    a subprocess instead of an answer about its request. The contract says
    branch or tag now, and a commit id is refused in terms the caller can
    act on.
    """

    def test_a_full_commit_sha_is_refused_before_any_clone(self):
        with pytest.raises(BuildError, match="branch or a tag"):
            _validate_git_ref("0" * 40)

    def test_a_sha256_object_id_is_refused_too(self):
        with pytest.raises(BuildError, match="branch or a tag"):
            _validate_git_ref("a" * 64)

    def test_the_message_names_the_offending_ref(self):
        with pytest.raises(BuildError) as caught:
            _validate_git_ref("f" * 40)
        assert "f" * 40 in str(caught.value)

    def test_branches_and_tags_pass_through_unchanged(self):
        for ref in ("main", "v1.2.3", "release/2026-09", "abc1234", None, ""):
            assert _validate_git_ref(ref) == ref

    def test_a_hex_name_shorter_than_an_object_id_is_still_a_branch(self):
        """Only unambiguous object ids are refused, not any hex-looking name."""
        assert _validate_git_ref("deadbeef") == "deadbeef"


class TestChartjsCdnConstant:
    """Chart.js is pinned exactly the way MERMAID_VERSION/HLJS_VERSION are,
    for the design-system starter/style-guide documents (src/designkit.py)."""

    def test_chartjs_cdn_constant_is_exact_pinned(self):
        from src import builder

        assert builder.CHARTJS_VERSION.count(".") == 2
        assert (
            builder.CHARTJS_JS
            == f"https://cdn.jsdelivr.net/npm/chart.js@{builder.CHARTJS_VERSION}/dist/chart.umd.min.js"
        )
