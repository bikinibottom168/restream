"""A small, dependency-free JSON path resolver.

Supports the shapes a provider configuration realistically needs::

    data.stream.url
    data.items[0].url
    result.sources[*].file      -> first match found
    url                         -> a top-level key

Written deliberately narrow: no filters, no expressions, nothing that could
surprise an operator editing a text field in the dashboard.
"""

from __future__ import annotations

import re
from typing import Any, Iterator

_SEGMENT_RE = re.compile(r"^(?P<key>[^\[\]]*)(?P<indexes>(?:\[[^\[\]]+\])*)$")
_INDEX_RE = re.compile(r"\[([^\[\]]+)\]")

_MISSING = object()


class JsonPathError(ValueError):
    """Raised when a path is syntactically invalid."""


def _split(path: str) -> list[str]:
    return [segment for segment in path.split(".") if segment != ""]


def _apply_indexes(node: Any, indexes: list[str]) -> Any:
    for raw in indexes:
        token = raw.strip()
        if token == "*":
            return _Wildcard(node)
        try:
            position = int(token)
        except ValueError as exc:
            raise JsonPathError(f"invalid list index {raw!r}") from exc
        if not isinstance(node, (list, tuple)):
            return _MISSING
        if position < 0:
            position += len(node)
        if not 0 <= position < len(node):
            return _MISSING
        node = node[position]
    return node


class _Wildcard:
    """Marker holding every element of a list for the next path segment."""

    __slots__ = ("items",)

    def __init__(self, node: Any) -> None:
        self.items: list[Any] = list(node) if isinstance(node, (list, tuple)) else []


def _step(node: Any, segment: str) -> Any:
    match = _SEGMENT_RE.match(segment)
    if match is None:
        raise JsonPathError(f"invalid path segment {segment!r}")
    key = match.group("key")
    indexes = _INDEX_RE.findall(match.group("indexes") or "")

    if isinstance(node, _Wildcard):
        for item in node.items:
            candidate = _step(item, segment)
            if candidate is not _MISSING:
                return candidate
        return _MISSING

    if key == "*":
        if indexes:
            raise JsonPathError("'*' cannot be combined with an index")
        return _Wildcard(node)

    if key:
        if isinstance(node, dict):
            if key not in node:
                return _MISSING
            node = node[key]
        elif isinstance(node, (list, tuple)):
            # Allow "items.0.url" as an alias for "items[0].url".
            try:
                node = node[int(key)]
            except (ValueError, IndexError):
                return _MISSING
        else:
            return _MISSING

    if indexes:
        node = _apply_indexes(node, indexes)
    return node


def get_path(data: Any, path: str, default: Any = None) -> Any:
    """Return the value at *path*, or *default* when it is absent.

    >>> get_path({"data": {"stream": {"url": "x"}}}, "data.stream.url")
    'x'
    >>> get_path({"a": [{"u": 1}, {"u": 2}]}, "a[1].u")
    2
    >>> get_path({"a": [{"u": "z"}]}, "a[*].u")
    'z'
    """
    if not path:
        return default
    node: Any = data
    for segment in _split(path):
        node = _step(node, segment)
        if node is _MISSING:
            return default
    if isinstance(node, _Wildcard):
        return node.items[0] if node.items else default
    return default if node is _MISSING else node


def get_string(data: Any, path: str, default: str = "") -> str:
    """Like :func:`get_path` but always returns a stripped string."""
    value = get_path(data, path, default)
    if value is None:
        return default
    if isinstance(value, (dict, list, tuple)):
        return default
    return str(value).strip()


def get_list(data: Any, path: str) -> list[Any]:
    """Return the list at *path*; an empty list when it is missing."""
    value = get_path(data, path, None)
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def iter_dicts(node: Any) -> Iterator[dict[str, Any]]:
    """Yield every dictionary found anywhere inside *node* (breadth-first)."""
    queue: list[Any] = [node]
    while queue:
        current = queue.pop(0)
        if isinstance(current, dict):
            yield current
            queue.extend(current.values())
        elif isinstance(current, (list, tuple)):
            queue.extend(current)


#: extensions that mark a value as a media URL when suggesting paths
_MEDIA_HINTS = (".m3u8", ".mpd", ".flv", ".ts", ".mp4")


def suggest_string_paths(node: Any, prefix: str = "") -> list[dict[str, Any]]:
    """List every dotted path whose value is a string, best candidates first.

    Used by the dashboard's "preview" so an operator can click the field that
    holds the stream URL instead of guessing the path. Each entry is
    ``{"path": "data.stream.url", "value": "...", "looks_like_url": bool}``.
    Values are truncated; the caller is responsible for masking tokens.
    """
    out: list[dict[str, Any]] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, f"{path}.{key}" if path else str(key))
        elif isinstance(value, list):
            for index, child in enumerate(value[:5]):
                walk(child, f"{path}[{index}]")
        elif isinstance(value, str):
            lowered = value.lower()
            is_url = lowered.startswith(("http://", "https://")) or lowered.startswith("//")
            is_media = is_url and any(ext in lowered.split("?", 1)[0] for ext in _MEDIA_HINTS)
            out.append(
                {
                    "path": path,
                    "value": value[:120],
                    "looks_like_url": is_url,
                    "looks_like_media": is_media,
                }
            )

    walk(node, prefix)
    # media URLs first, then other URLs, then the rest
    out.sort(key=lambda e: (not e["looks_like_media"], not e["looks_like_url"]))
    return out


def find_list_of_objects(payload: Any, hint_keys: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Locate the most plausible list of records inside an unknown envelope.

    Used when the operator has not configured an explicit ``list_path``.
    """
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "channels", "items", "list", "results", "rows", "streams"):
        value = payload.get(key)
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value
        if isinstance(value, dict):
            nested = find_list_of_objects(value, hint_keys)
            if nested:
                return nested
    best: list[dict[str, Any]] = []
    for value in payload.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            if hint_keys and not any(k in value[0] for k in hint_keys):
                continue
            if len(value) > len(best):
                best = value
    if best:
        return best
    # Nothing at this level - descend one branch at a time.
    for value in payload.values():
        if isinstance(value, dict):
            nested = find_list_of_objects(value, hint_keys)
            if nested:
                return nested
    return []
