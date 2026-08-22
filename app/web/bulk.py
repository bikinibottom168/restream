"""Bulk channel entry.

Turns a pasted list — or an uploaded JSON file — into channel rows, so adding
thirty channels does not mean opening the form thirty times.

Accepted shapes
---------------

JSON array or ``{"channels": [...]}``::

    [
      {"name": "Sport Channel 01", "url": "https://media.example.com/play?id=82290",
       "stream_key": "sport01"},
      {"name": "Sport Channel 02", "url": "https://media.example.com/play?id=82291"}
    ]

Plain lines, separated by ``|``, a tab, a comma or a semicolon::

    Sport Channel 01 | https://media.example.com/play?id=82290 | sport01
    Sport Channel 02 | https://media.example.com/play?id=82291

Bare URLs, one per line — the name is derived from the URL::

    https://media.example.com/play?id=82290
    https://origin.example.com/live/sport02/index.m3u8

A URL that points straight at media (``.m3u8``, ``.mpd``, ``.flv``, ``.ts``,
``.mp4``) is stored as the channel's direct input; anything else is stored as
the channel's *endpoint*, to be fetched and parsed at resolve time.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlsplit

#: Extensions that mean "this is the media itself, do not fetch and parse it".
MEDIA_EXTENSIONS = (".m3u8", ".mpd", ".flv", ".ts", ".mp4")

#: Non-HTTP schemes FFmpeg can read directly.
DIRECT_SCHEMES = ("rtmp://", "rtmps://", "rtsp://", "srt://", "udp://", "rtp://")

#: Filenames that carry no information about which channel this is.
_GENERIC_FILENAMES = {
    "index", "master", "playlist", "chunklist", "stream", "live", "manifest", "play"
}

_SPLIT_RE = re.compile(r"\s*(?:\||\t|;|,)\s*")
_URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)

#: Field aliases accepted in JSON input.
_NAME_KEYS = ("name", "title", "channel", "channel_name", "label")
_URL_KEYS = ("url", "resolve_url", "endpoint", "link", "source", "page", "play_url")
_MEDIA_KEYS = ("input_url", "media_url", "stream_url", "m3u8", "hls")
_KEY_KEYS = ("stream_key", "key", "rtmp_key")
_RTMP_KEYS = ("rtmp_url", "rtmp", "destination", "output")
_LOGO_KEYS = ("logo", "logo_url", "icon", "image")
_GROUP_KEYS = ("group", "group_title", "category", "genre")
_REF_KEYS = ("provider_ref", "ref", "id", "channel_id")


@dataclass
class BulkResult:
    """Parsed rows plus per-line problems, so nothing fails silently."""

    entries: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.entries) and not self.errors


def is_media_url(url: str) -> bool:
    """True when the URL points at playable media rather than at a page/API."""
    lowered = url.strip().lower()
    if lowered.startswith(DIRECT_SCHEMES):
        return True
    path = urlsplit(lowered).path
    return path.endswith(MEDIA_EXTENSIONS)


def name_from_url(url: str) -> str:
    """Derive a readable channel name from a URL.

    ``.../play?id=82290`` -> ``Channel 82290``
    ``.../live/sport01/index.m3u8`` -> ``sport01``
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "Unnamed channel"

    for key, value in parse_qsl(parts.query):
        if key.lower() in ("id", "cid", "chid", "channel", "channel_id") and value:
            return f"Channel {value}"

    segments = [segment for segment in parts.path.split("/") if segment]
    if segments:
        cleaned = segments[-1].rsplit(".", 1)[0]
        # "index", "master" and friends say nothing - use the folder instead.
        if cleaned.lower() in _GENERIC_FILENAMES and len(segments) >= 2:
            cleaned = segments[-2].rsplit(".", 1)[0]
        if cleaned:
            return cleaned
    return parts.netloc or "Unnamed channel"


def _first(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return str(row[key]).strip()
    return ""


def _entry(
    *,
    name: str,
    url: str = "",
    media_url: str = "",
    stream_key: str = "",
    rtmp_url: str = "",
    logo_url: str = "",
    group_title: str = "",
    provider_ref: str = "",
) -> dict[str, Any]:
    """Build one channel payload, routing the URL to the right column."""
    resolve_url = ""
    input_url = media_url

    if url:
        if is_media_url(url) and not media_url:
            input_url = url
        elif not media_url:
            resolve_url = url
        else:
            resolve_url = url

    return {
        "name": name,
        "resolve_url": resolve_url,
        "input_url": input_url,
        "stream_key": stream_key,
        "rtmp_url": rtmp_url,
        "logo_url": logo_url,
        "group_title": group_title,
        "provider_ref": provider_ref,
    }


def _parse_json(payload: Any, result: BulkResult) -> None:
    rows: Any = payload
    if isinstance(payload, dict):
        for key in ("channels", "data", "items", "list"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
        else:
            rows = [payload]  # a single channel object

    if not isinstance(rows, list):
        result.errors.append("JSON must be an array of channels, or {\"channels\": [...]}")
        return

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            if isinstance(row, str) and row.strip():
                url = row.strip()
                result.entries.append(_entry(name=name_from_url(url), url=url))
            else:
                result.errors.append(f"entry {index}: expected an object")
            continue

        url = _first(row, _URL_KEYS)
        media_url = _first(row, _MEDIA_KEYS)
        name = _first(row, _NAME_KEYS)
        if not url and not media_url:
            result.errors.append(f"entry {index}: no url field found")
            continue
        if not name:
            name = name_from_url(url or media_url)

        result.entries.append(
            _entry(
                name=name,
                url=url,
                media_url=media_url,
                stream_key=_first(row, _KEY_KEYS),
                rtmp_url=_first(row, _RTMP_KEYS),
                logo_url=_first(row, _LOGO_KEYS),
                group_title=_first(row, _GROUP_KEYS),
                provider_ref=_first(row, _REF_KEYS),
            )
        )


def _parse_lines(text: str, result: BulkResult) -> None:
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        # A bare URL on its own line.
        if _URL_RE.match(line) and not _SPLIT_RE.search(line.split("://", 1)[-1]):
            result.entries.append(_entry(name=name_from_url(line), url=line))
            continue

        parts = [part for part in _SPLIT_RE.split(line) if part]
        urls = [part for part in parts if _URL_RE.match(part)]
        if not urls:
            result.errors.append(f"line {number}: no URL found in {line[:60]!r}")
            continue

        url = urls[0]
        labels = [part for part in parts if part not in urls]
        name = labels[0] if labels else name_from_url(url)
        stream_key = labels[1] if len(labels) > 1 else ""

        result.entries.append(_entry(name=name, url=url, stream_key=stream_key))


def parse_channel_list(text: str) -> BulkResult:
    """Parse pasted text or a JSON document into channel payloads."""
    result = BulkResult()
    if not text or not text.strip():
        result.errors.append("nothing to import")
        return result

    stripped = text.strip()
    if stripped[:1] in "[{":
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            result.errors.append(f"invalid JSON: {exc.msg} (line {exc.lineno})")
            return result
        _parse_json(payload, result)
        return result

    _parse_lines(stripped, result)
    return result


def deduplicate(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Drop entries that repeat a URL already seen in the same batch."""
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    dropped = 0
    for entry in entries:
        marker = (entry.get("resolve_url") or entry.get("input_url") or "").strip().lower()
        if marker and marker in seen:
            dropped += 1
            continue
        if marker:
            seen.add(marker)
        unique.append(entry)
    return unique, dropped
