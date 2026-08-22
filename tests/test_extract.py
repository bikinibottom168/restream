"""Stream URL extraction and DRM detection."""

from __future__ import annotations

import json

from app.providers.extract import (
    detect_drm,
    extract_stream_urls,
    find_iframes,
    pick_stream_url,
    unescape,
)


def test_unescape_handles_json_and_html_escaping():
    assert unescape(r"https:\/\/host\/a.m3u8") == "https://host/a.m3u8"
    assert unescape("https://host/a.m3u8?a=1&amp;b=2") == "https://host/a.m3u8?a=1&b=2"
    assert unescape(r"https://host/a.m3u8") == "https://host/a.m3u8"


def test_extract_from_json_fixture(fixture_text):
    payload = fixture_text("play_response.json")
    urls = extract_stream_urls(payload)
    assert urls, "expected at least one URL"
    assert urls[0].startswith("https://edge-07.media.example/live/sport01/index.m3u8")
    assert "token=abc123def456" in urls[0]


def test_extract_from_parsed_json(fixture_text):
    payload = json.loads(fixture_text("play_response.json"))
    assert pick_stream_url(payload).endswith("expires=1924992000")


def test_extract_from_html_fixture(fixture_text):
    page = fixture_text("live_page.html")
    urls = extract_stream_urls(page, base_url="https://portal.example/play?id=82290")
    assert any("edge-07.media.example" in url for url in urls)
    assert all(url.startswith("https://") for url in urls)


def test_relative_urls_are_resolved():
    body = '<video><source src="/live/sport01/index.m3u8" type="application/x-mpegURL"></video>'
    urls = extract_stream_urls(body, base_url="https://portal.example/play?id=1")
    assert urls == ["https://portal.example/live/sport01/index.m3u8"]


def test_protocol_relative_urls():
    urls = extract_stream_urls(
        '{"url": "//cdn.example/live/x.m3u8"}', base_url="https://portal.example/"
    )
    assert urls == ["https://cdn.example/live/x.m3u8"]


def test_hls_is_preferred_over_other_formats():
    body = json.dumps(
        {
            "mp4": "https://cdn.example/a.mp4",
            "dash": "https://cdn.example/a.mpd",
            "hls": "https://cdn.example/a.m3u8",
        }
    )
    assert pick_stream_url(body) == "https://cdn.example/a.m3u8"


def test_no_urls_returns_empty():
    assert extract_stream_urls("nothing to see here") == []
    assert pick_stream_url("") is None
    assert extract_stream_urls(None) == []


def test_duplicates_are_removed():
    body = "https://cdn.example/a.m3u8 https://cdn.example/a.m3u8"
    assert extract_stream_urls(body) == ["https://cdn.example/a.m3u8"]


def test_find_iframes(fixture_text):
    frames = find_iframes(fixture_text("live_page.html"), base_url="https://portal.example/")
    assert frames == ["https://embed.media.example/player?id=82290"]


# --------------------------------------------------------------------------- #
# DRM detection - the pipeline must refuse, never circumvent
# --------------------------------------------------------------------------- #
def test_plain_aes128_hls_is_not_flagged():
    manifest = (
        "#EXTM3U\n"
        '#EXT-X-KEY:METHOD=AES-128,URI="https://origin.example/key?id=1"\n'
        "#EXTINF:6.0,\nsegment1.ts\n"
    )
    assert detect_drm(manifest) is None


def test_sample_aes_is_flagged():
    manifest = (
        "#EXTM3U\n"
        '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://key",KEYFORMAT="com.apple.streamingkeydelivery"\n'
    )
    assert detect_drm(manifest) == "sample-aes"


def test_widevine_is_flagged():
    assert detect_drm('<ContentProtection schemeIdUri="urn:uuid:EDEF8BA9" value="com.widevine.alpha"/>')


def test_dash_content_protection_is_flagged():
    assert detect_drm("<MPD><ContentProtection/></MPD>") is not None


def test_empty_manifest_is_fine():
    assert detect_drm("") is None


# --------------------------------------------------------------------------- #
# broadened auto extraction (v1.2.2): extensionless stream URLs + deep search
# --------------------------------------------------------------------------- #
def test_extensionless_url_under_strong_key():
    body = json.dumps({"data": {"stream": {"url": "https://cdn.example/live/12345"}}})
    assert pick_stream_url(body) == "https://cdn.example/live/12345"


def test_extensionless_url_ignored_under_weak_key():
    # a "link" to a web page must NOT be picked up as a stream
    body = json.dumps({"link": "https://portal.example/watch/1", "note": "hi"})
    assert pick_stream_url(body) is None


def test_real_m3u8_beats_extensionless():
    body = json.dumps(
        {"a": {"url": "https://cdn/live/1"}, "b": {"file": "https://cdn/hls/playlist.m3u8"}}
    )
    assert pick_stream_url(body) == "https://cdn/hls/playlist.m3u8"


def test_nested_playlist_with_token():
    body = json.dumps({"result": {"playback": "https://edge/hls/playlist.m3u8?token=xyz"}})
    assert pick_stream_url(body).endswith("playlist.m3u8?token=xyz")
