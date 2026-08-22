"""Bulk channel entry parsing."""

from __future__ import annotations

from app.web.bulk import (
    deduplicate,
    is_media_url,
    name_from_url,
    parse_channel_list,
)


# --------------------------------------------------------------------------- #
# URL classification
# --------------------------------------------------------------------------- #
def test_media_urls_are_recognised():
    assert is_media_url("https://origin.example.com/live/a/index.m3u8")
    assert is_media_url("https://origin.example.com/a.mpd")
    assert is_media_url("https://origin.example.com/a.ts?token=x")
    assert is_media_url("rtmp://origin.example.com/live/a")
    assert is_media_url("srt://origin.example.com:9000")


def test_endpoint_urls_are_not_media():
    assert not is_media_url("https://media.example.com/play?id=82290")
    assert not is_media_url("https://media.example.com/api/stream/82290")
    assert not is_media_url("https://media.example.com/watch/sport01")


def test_name_from_url():
    assert name_from_url("https://media.example.com/play?id=82290") == "Channel 82290"
    assert name_from_url("https://origin.example.com/live/sport01/index.m3u8") == "sport01"
    assert name_from_url("https://origin.example.com/live/sport02.m3u8") == "sport02"
    assert name_from_url("https://origin.example.com") == "origin.example.com"


# --------------------------------------------------------------------------- #
# line format
# --------------------------------------------------------------------------- #
def test_pipe_separated_lines():
    result = parse_channel_list(
        """
        Sport Channel 01 | https://media.example.com/play?id=82290 | sport01
        Sport Channel 02 | https://media.example.com/play?id=82291 | sport02
        """
    )
    assert not result.errors
    assert len(result.entries) == 2
    first = result.entries[0]
    assert first["name"] == "Sport Channel 01"
    assert first["resolve_url"] == "https://media.example.com/play?id=82290"
    assert first["input_url"] == "", "an endpoint URL must not become a media URL"
    assert first["stream_key"] == "sport01"


def test_comma_and_tab_separators():
    result = parse_channel_list(
        "Sport Channel 01, https://media.example.com/play?id=1\n"
        "Sport Channel 02\thttps://media.example.com/play?id=2"
    )
    assert len(result.entries) == 2
    assert result.entries[1]["name"] == "Sport Channel 02"


def test_media_url_goes_to_input_url():
    result = parse_channel_list("Test A | https://origin.example.com/live/a/index.m3u8")
    entry = result.entries[0]
    assert entry["input_url"] == "https://origin.example.com/live/a/index.m3u8"
    assert entry["resolve_url"] == ""


def test_bare_urls_get_derived_names():
    result = parse_channel_list(
        "https://media.example.com/play?id=82290\n"
        "https://origin.example.com/live/sport02/index.m3u8"
    )
    assert [entry["name"] for entry in result.entries] == ["Channel 82290", "sport02"]
    assert result.entries[0]["resolve_url"].endswith("id=82290")
    assert result.entries[1]["input_url"].endswith("index.m3u8")


def test_comments_and_blank_lines_ignored():
    result = parse_channel_list(
        "# my channels\n\nA | https://media.example.com/play?id=1\n\n# done\n"
    )
    assert len(result.entries) == 1
    assert not result.errors


def test_line_without_url_is_reported():
    result = parse_channel_list("just some text\nA | https://media.example.com/play?id=1")
    assert len(result.entries) == 1
    assert len(result.errors) == 1
    assert "line 1" in result.errors[0]


# --------------------------------------------------------------------------- #
# JSON format
# --------------------------------------------------------------------------- #
def test_json_array():
    result = parse_channel_list(
        """
        [
          {"name": "Sport Channel 01", "url": "https://media.example.com/play?id=82290",
           "stream_key": "sport01", "logo": "https://cdn/1.png"},
          {"name": "Sport Channel 02", "url": "https://media.example.com/play?id=82291"}
        ]
        """
    )
    assert not result.errors
    assert len(result.entries) == 2
    assert result.entries[0]["stream_key"] == "sport01"
    assert result.entries[0]["logo_url"] == "https://cdn/1.png"
    assert result.entries[0]["resolve_url"].endswith("id=82290")


def test_json_envelope_and_aliases():
    result = parse_channel_list(
        """
        {"channels": [
          {"title": "A", "endpoint": "https://media.example.com/play?id=1", "key": "a01"},
          {"channel": "B", "m3u8": "https://origin.example.com/b.m3u8", "rtmp": "rtmp://s/live/b"}
        ]}
        """
    )
    assert len(result.entries) == 2
    assert result.entries[0]["name"] == "A"
    assert result.entries[0]["stream_key"] == "a01"
    assert result.entries[1]["input_url"] == "https://origin.example.com/b.m3u8"
    assert result.entries[1]["rtmp_url"] == "rtmp://s/live/b"


def test_json_array_of_plain_strings():
    result = parse_channel_list('["https://media.example.com/play?id=42"]')
    assert result.entries[0]["name"] == "Channel 42"


def test_json_entry_without_url_is_reported():
    result = parse_channel_list('[{"name": "A"}]')
    assert result.entries == []
    assert "entry 1" in result.errors[0]


def test_invalid_json_is_reported_clearly():
    result = parse_channel_list('[{"name": "A",}]')
    assert result.entries == []
    assert "invalid JSON" in result.errors[0]


def test_empty_input():
    result = parse_channel_list("   ")
    assert result.entries == []
    assert result.errors == ["nothing to import"]


# --------------------------------------------------------------------------- #
# de-duplication
# --------------------------------------------------------------------------- #
def test_deduplicate_by_url():
    result = parse_channel_list(
        "A | https://media.example.com/play?id=1\n"
        "B | https://media.example.com/play?id=1\n"
        "C | https://media.example.com/play?id=2"
    )
    entries, dropped = deduplicate(result.entries)
    assert dropped == 1
    assert [entry["name"] for entry in entries] == ["A", "C"]


# --------------------------------------------------------------------------- #
# JSON field suggestion (powers the IPTV "Preview" button)
# --------------------------------------------------------------------------- #
def test_suggest_string_paths_ranks_media_first():
    from app.providers.jsonpath import suggest_string_paths

    sample = {
        "code": 0,
        "data": {
            "channel": {"id": "82290", "name": "Sport Channel 01"},
            "stream": {"url": "https://edge/live/a.m3u8?token=abc", "type": "hls"},
        },
    }
    paths = suggest_string_paths(sample)
    assert paths[0]["path"] == "data.stream.url"
    assert paths[0]["looks_like_media"] is True
    all_paths = {p["path"] for p in paths}
    assert "data.channel.id" in all_paths
    assert "data.channel.name" in all_paths


def test_suggest_string_paths_handles_lists():
    from app.providers.jsonpath import suggest_string_paths

    sample = {"sources": [{"file": "https://cdn/a.m3u8"}, {"file": "https://cdn/b.m3u8"}]}
    paths = suggest_string_paths(sample)
    assert any(p["path"] == "sources[0].file" for p in paths)
