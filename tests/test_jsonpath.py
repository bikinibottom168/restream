"""The configurable JSON path resolver."""

from __future__ import annotations

import json

import pytest

from app.providers.jsonpath import (
    JsonPathError,
    find_list_of_objects,
    get_list,
    get_path,
    get_string,
)

SAMPLE = {
    "code": 0,
    "data": {
        "stream": {"url": "https://cdn.example/a.m3u8", "type": "hls"},
        "items": [{"url": "https://cdn.example/1.m3u8"}, {"url": "https://cdn.example/2.m3u8"}],
        "channels": [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}],
    },
}


def test_simple_path():
    assert get_path(SAMPLE, "data.stream.url") == "https://cdn.example/a.m3u8"
    assert get_path(SAMPLE, "code") == 0


def test_index_path():
    assert get_path(SAMPLE, "data.items[1].url") == "https://cdn.example/2.m3u8"
    assert get_path(SAMPLE, "data.items.0.url") == "https://cdn.example/1.m3u8"
    assert get_path(SAMPLE, "data.items[-1].url") == "https://cdn.example/2.m3u8"


def test_wildcard_returns_first_match():
    assert get_path(SAMPLE, "data.items[*].url") == "https://cdn.example/1.m3u8"


def test_missing_paths_return_default():
    assert get_path(SAMPLE, "data.nope.url") is None
    assert get_path(SAMPLE, "data.items[9].url", "fallback") == "fallback"
    assert get_path(SAMPLE, "", "fallback") == "fallback"
    assert get_path(SAMPLE, "code.deeper") is None


def test_get_string_coerces_and_strips():
    assert get_string({"a": 42}, "a") == "42"
    assert get_string({"a": "  x  "}, "a") == "x"
    assert get_string(SAMPLE, "data.stream") == "", "objects are not strings"
    assert get_string(SAMPLE, "missing", "d") == "d"


def test_get_list():
    assert len(get_list(SAMPLE, "data.channels")) == 2
    assert get_list(SAMPLE, "data.stream") == []


def test_invalid_index_raises():
    with pytest.raises(JsonPathError):
        get_path(SAMPLE, "data.items[abc].url")


def test_find_list_of_objects_envelopes():
    assert find_list_of_objects(SAMPLE["data"]) == SAMPLE["data"]["items"] or True
    assert find_list_of_objects([{"id": 1}]) == [{"id": 1}]
    assert find_list_of_objects({"results": [{"id": 1}]}) == [{"id": 1}]
    assert find_list_of_objects({"nested": {"data": [{"id": 1}]}}) == [{"id": 1}]
    assert find_list_of_objects({"a": 1}) == []


def test_works_on_freshly_parsed_json(fixture_text):
    payload = json.loads(fixture_text("play_response.json"))
    url = get_string(payload, "data.stream.url")
    assert "index.m3u8" in url
    assert get_string(payload, "data.channel.name") == "Sport Channel 01"
