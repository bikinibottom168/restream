"""Interface language."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.i18n import (
    DEFAULT_LANGUAGE,
    LANGUAGES,
    THAI,
    make_translator,
    normalise,
    translate,
)

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "app" / "templates"


def test_supported_languages():
    assert set(LANGUAGES) == {"en", "th"}
    assert DEFAULT_LANGUAGE in LANGUAGES


@pytest.mark.parametrize(
    "value,expected",
    [("th", "th"), ("EN", "en"), ("", DEFAULT_LANGUAGE), (None, DEFAULT_LANGUAGE), ("fr", DEFAULT_LANGUAGE)],
)
def test_normalise(value, expected):
    assert normalise(value) == expected


def test_translate_thai():
    assert translate("Dashboard", "th") == "แดชบอร์ด"
    assert translate("Providers", "th") == "แหล่งสัญญาณ"
    assert translate("Start All", "th") == "เริ่มทั้งหมด"


def test_english_is_passthrough():
    assert translate("Dashboard", "en") == "Dashboard"
    assert translate("Anything at all", "en") == "Anything at all"


def test_unknown_phrase_falls_back_to_english():
    assert translate("A phrase nobody translated", "th") == "A phrase nobody translated"


def test_translator_callable():
    t = make_translator("th")
    assert t("Settings") == "ตั้งค่า"
    assert t("not in the table") == "not in the table"

    t_en = make_translator("en")
    assert t_en("Settings") == "Settings"


def test_no_empty_or_identical_translations():
    """Every Thai entry must actually be Thai, not a copy of the English."""
    # Proper nouns and HTTP header names stay in English on purpose.
    KEEP_AS_IS = {
        "Telegram", "Cookie", "API token", "Base URL", "Bot token", "Chat id",
        "FFmpeg PID", "Stream key", "Key", "User-Agent", "Referer",
    }
    suspicious = [
        key
        for key, value in THAI.items()
        if not value.strip()
        or (value == key and not key.isupper() and key not in KEEP_AS_IS)
    ]
    assert suspicious == [], f"untranslated entries: {suspicious}"


# --------------------------------------------------------------------------- #
# template integration
# --------------------------------------------------------------------------- #
def test_templates_only_call_t_with_known_phrases():
    """Every ``t('...')`` in a template must exist in the Thai table.

    Catches a typo like ``t('Dashbaord')`` that would silently render English.
    """
    pattern = re.compile(r"t\(\s*'([^']+)'\s*\)")
    missing: set[str] = set()
    for path in TEMPLATE_DIR.rglob("*.html"):
        for phrase in pattern.findall(path.read_text(encoding="utf-8")):
            if phrase not in THAI:
                missing.add(f"{path.name}: {phrase}")
    assert not missing, f"phrases used in templates but not translated: {sorted(missing)}"


def test_navigation_is_translated():
    """The menus the operator sees first must all be covered."""
    for phrase in ("Dashboard", "Providers", "Events", "History", "Logs", "Settings"):
        assert THAI.get(phrase), f"navigation item {phrase!r} has no Thai text"


def test_explain_stream_error_maps_common_causes():
    from app.core.i18n import explain_stream_error

    assert "โทเคน" in explain_stream_error("Server returned 403 Forbidden")
    assert "พอร์ต" in explain_stream_error("bind: address already in use")
    assert "transcode" in explain_stream_error("Non-monotonous DTS in output stream")
    assert "ปลายทาง" in explain_stream_error("Connection reset by peer")
    # unknown errors return empty so the caller shows the raw text
    assert explain_stream_error("some totally novel message") == ""
    assert explain_stream_error("") == ""
