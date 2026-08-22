"""Stream URL extraction and manifest inspection.

These helpers are deliberately format-agnostic: an endpoint may answer with
JSON, with HTML, with a redirect, or with a JavaScript blob that embeds the URL
in an escaped form.  Nothing here decrypts anything - when a manifest declares
DRM or sample-level encryption the channel is reported as UNSUPPORTED and the
pipeline stops.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit

#: Media extensions we are willing to hand to FFmpeg, best first.
MEDIA_EXTENSIONS = ("m3u8", "mpd", "flv", "ts", "mp4")

#: Keys commonly used to carry a playable URL in a JSON payload.
URL_LIKE_KEYS = frozenset(
    {
        "url",
        "src",
        "source",
        "file",
        "stream",
        "stream_url",
        "streamurl",
        "play_url",
        "playurl",
        "hls",
        "hls_url",
        "m3u8",
        "link",
        "path",
        "uri",
        "manifest",
    }
)

#: Keys that so strongly mean "the stream" that we accept an http(s) URL under
#: them even when it has no file extension (e.g. ``/live/12345``). Kept narrow
#: on purpose so a "link" to a web page is not mistaken for the media.
STRONG_URL_KEYS = frozenset(
    {
        "url",
        "src",
        "source",
        "file",
        "stream",
        "stream_url",
        "streamurl",
        "play_url",
        "playurl",
        "playurl2",
        "hls",
        "hls_url",
        "m3u8",
        "manifest",
        "playback",
        "playback_url",
    }
)

_URL_PATTERN = re.compile(
    r"(?P<url>(?:https?://|//)[^\s'\"<>()\\\[\]]+?"
    r"\.(?:m3u8|mpd|flv|ts|mp4)(?:\?[^\s'\"<>()\\\[\]]*)?)",
    re.IGNORECASE,
)

_RELATIVE_PATTERN = re.compile(
    r"""["'(](?P<url>/[^\s'"<>()\\\[\]]+?"""
    r"""\.(?:m3u8|mpd|flv|ts|mp4)(?:\?[^\s'"<>()\\\[\]]*)?)["')]""",
    re.IGNORECASE,
)

_ATTRIBUTE_PATTERN = re.compile(
    r"""(?:src|data-src|data-url|data-file|file|href)\s*[:=]\s*["'](?P<url>[^"']+)["']""",
    re.IGNORECASE,
)

_IFRAME_PATTERN = re.compile(
    r"""<iframe[^>]+src\s*=\s*["'](?P<url>[^"']+)["']""", re.IGNORECASE
)

#: Encryption methods that require a key/licence exchange we do not implement.
_UNSUPPORTED_ENCRYPTION = (
    "sample-aes",
    "sample-aes-ctr",
    "cenc",
    "widevine",
    "playready",
    "fairplay",
    "com.apple.streamingkeydelivery",
    "com.widevine.alpha",
    "com.microsoft.playready",
)


class DRMProtectedError(RuntimeError):
    """Raised when a manifest requires DRM/licence handling."""


def unescape(text: str) -> str:
    r"""Normalise escaped URL text.

    Handles ``https:\/\/host`` (JSON/JS escaping), ``/``, ``%2F`` inside a
    scheme, and HTML entities such as ``&amp;``.
    """
    if not text:
        return ""
    out = text.replace("\\/", "/")
    out = out.replace("\\u002F", "/").replace("\\u002f", "/")
    out = out.replace("\\u0026", "&").replace("\\\\", "\\")
    out = html.unescape(out)
    return out


def _clean(url: str) -> str:
    url = url.strip().strip("\"'` \t\r\n")
    url = url.rstrip(",;")
    # Strip a trailing backslash left over from escaped payloads.
    while url.endswith("\\"):
        url = url[:-1]
    return url


def _score(url: str) -> tuple[int, int]:
    """Sort key: prefer HLS, then progressive formats; prefer absolute URLs."""
    lowered = url.lower()
    path = urlsplit(lowered).path
    extension_rank = len(MEDIA_EXTENSIONS)
    for index, extension in enumerate(MEDIA_EXTENSIONS):
        if path.endswith(f".{extension}"):
            extension_rank = index
            break
    absolute_rank = 0 if lowered.startswith("http") else 1
    return (extension_rank, absolute_rank)


def _walk_json(node: Any, found: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str):
                candidate = _clean(unescape(value))
                key_lower = str(key).lower()
                if key_lower in URL_LIKE_KEYS and _has_media_extension(candidate):
                    # best case: a keyed value that is a real media URL
                    found.append(candidate)
                elif key_lower in STRONG_URL_KEYS and _looks_like_http(candidate):
                    # a stream-ish key holding an http URL with no extension -
                    # kept as a lower-priority candidate (sorts after real m3u8)
                    found.append(candidate)
                else:
                    found.extend(_regex_urls(candidate))
            else:
                _walk_json(value, found)
    elif isinstance(node, list):
        for item in node:
            _walk_json(item, found)
    elif isinstance(node, str):
        found.extend(_regex_urls(_clean(unescape(node))))


def _has_media_extension(url: str) -> bool:
    if not url:
        return False
    path = urlsplit(url).path.lower()
    return any(path.endswith(f".{extension}") for extension in MEDIA_EXTENSIONS)


def _looks_like_http(url: str) -> bool:
    if not url:
        return False
    lowered = url.lower()
    return lowered.startswith(("http://", "https://")) or lowered.startswith("//")


def _regex_urls(text: str) -> list[str]:
    results: list[str] = []
    for match in _URL_PATTERN.finditer(text):
        results.append(_clean(match.group("url")))
    for match in _RELATIVE_PATTERN.finditer(text):
        results.append(_clean(match.group("url")))
    return results


def _dedupe(urls: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


def extract_stream_urls(payload: str | bytes | dict | list, base_url: str = "") -> list[str]:
    """Return every plausible media URL found in *payload*, best candidate first.

    Works on JSON documents, HTML pages, JavaScript blobs and plain text.
    Relative URLs are resolved against *base_url* when one is supplied.
    """
    if payload is None:
        return []

    found: list[str] = []

    if isinstance(payload, (dict, list)):
        _walk_json(payload, found)
    else:
        text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
        text = unescape(text)

        # 1) structured JSON, when the body happens to parse
        stripped = text.strip()
        if stripped[:1] in "{[":
            try:
                _walk_json(json.loads(stripped), found)
            except json.JSONDecodeError:
                pass

        # 2) HTML/JS attributes such as src="..." or file: "..."
        for match in _ATTRIBUTE_PATTERN.finditer(text):
            candidate = _clean(match.group("url"))
            if _has_media_extension(candidate):
                found.append(candidate)

        # 3) a plain regex sweep over the whole document
        found.extend(_regex_urls(text))

    normalised: list[str] = []
    for url in _dedupe(found):
        if url.startswith("//"):
            scheme = urlsplit(base_url).scheme or "https"
            url = f"{scheme}:{url}"
        elif url.startswith("/") and base_url:
            url = urljoin(base_url, url)
        elif not url.lower().startswith("http"):
            if not base_url:
                continue
            url = urljoin(base_url, url)
        normalised.append(url)

    return sorted(_dedupe(normalised), key=_score)


def pick_stream_url(payload: str | bytes | dict | list, base_url: str = "") -> str | None:
    """Return the single best media URL in *payload*, or ``None``."""
    candidates = extract_stream_urls(payload, base_url=base_url)
    return candidates[0] if candidates else None


def find_iframes(html_text: str, base_url: str = "") -> list[str]:
    """Return iframe sources, so a caller can follow an embedded player."""
    results = []
    for match in _IFRAME_PATTERN.finditer(unescape(html_text or "")):
        url = _clean(match.group("url"))
        if url.startswith("//"):
            scheme = urlsplit(base_url).scheme or "https"
            url = f"{scheme}:{url}"
        elif url.startswith("/") and base_url:
            url = urljoin(base_url, url)
        if url.lower().startswith("http"):
            results.append(url)
    return _dedupe(results)


def detect_drm(manifest_text: str) -> str | None:
    """Return the DRM/encryption scheme declared by a manifest, if any.

    A plain ``METHOD=AES-128`` HLS manifest is *not* reported: FFmpeg fetches
    that key over the same authorised session, exactly as a normal player does.
    Schemes that need a licence server are reported so the caller can mark the
    channel UNSUPPORTED instead of attempting anything.
    """
    if not manifest_text:
        return None
    lowered = manifest_text.lower()
    for scheme in _UNSUPPORTED_ENCRYPTION:
        if scheme in lowered:
            return scheme
    if "<contentprotection" in lowered:
        return "mpeg-dash contentprotection"
    return None


def assert_playable(manifest_text: str) -> None:
    """Raise :class:`DRMProtectedError` when a manifest needs DRM handling."""
    scheme = detect_drm(manifest_text)
    if scheme:
        raise DRMProtectedError(
            f"source declares protected media ({scheme}); this application does not "
            "implement DRM or licence handling"
        )
