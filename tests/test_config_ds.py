"""HUB_DS_* settings (design systems, spec 2026-09-14 Key decision 12).

Every design-system limit is a Settings field with an env override; no number
lives in a route or in src/designs.py itself.
"""

from src.config import load_settings
from src.tokens import TokenLimits


def test_ds_defaults(monkeypatch):
    for k in ("HUB_DS_MAX_BUNDLE_BYTES", "HUB_DS_FONT_HOSTS"):
        monkeypatch.delenv(k, raising=False)
    s = load_settings()
    assert s.ds_max_bundle_bytes == 2 * 1024 * 1024
    assert s.ds_max_per_project == 20
    assert s.ds_max_versions == 50
    assert s.ds_font_hosts == ("fonts.googleapis.com",)
    assert s.ds_content_request_bytes > s.ds_max_bundle_bytes
    assert s.token_limits() == TokenLimits(max_depth=16, max_tokens=5000, max_alias_depth=32)


def test_ds_env_overrides(monkeypatch):
    monkeypatch.setenv("HUB_DS_MAX_PER_PROJECT", "3")
    monkeypatch.setenv("HUB_DS_FONT_HOSTS", "fonts.googleapis.com, fonts.bunny.net")
    s = load_settings()
    assert s.ds_max_per_project == 3
    assert s.ds_font_hosts == ("fonts.googleapis.com", "fonts.bunny.net")


def test_reader_menu_default_is_on(monkeypatch):
    """HUB_READER_MENU_DEFAULT decides what a *new* artifact gets."""
    monkeypatch.delenv("HUB_READER_MENU_DEFAULT", raising=False)
    assert load_settings().reader_menu_default is True


def test_reader_menu_default_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("HUB_READER_MENU_DEFAULT", "0")
    assert load_settings().reader_menu_default is False
