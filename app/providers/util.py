"""Helpers shared by the built-in providers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlsplit

#: Query parameters that commonly carry a unix expiry timestamp.
_EXPIRY_KEYS = {"expires", "expire", "exp", "e", "valid_until", "vt", "endtime"}


def guess_expiry(url: str) -> datetime | None:
    """Read an expiry hint out of a signed URL's query string.

    Returns ``None`` when the URL carries no recognisable timestamp - the
    caller then falls back to the configured cache TTL.  This only *reads* a
    value the provider already put there; nothing is ever generated or altered.
    """
    if not url:
        return None
    try:
        query = urlsplit(url).query
    except ValueError:
        return None
    if not query:
        return None
    for key, value in parse_qsl(query, keep_blank_values=False):
        if key.lower() not in _EXPIRY_KEYS:
            continue
        raw = value.strip()
        if not raw.isdigit():
            continue
        number = int(raw)
        if number > 10**12:  # milliseconds
            number //= 1000
        if not 1_000_000_000 <= number <= 4_000_000_000:
            continue
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            continue
    return None


def normalise_headers(value: Any) -> dict[str, str]:
    """Coerce a header mapping that may arrive as a dict or a JSON string."""
    if not value:
        return {}
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items() if str(k).strip()}
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # Accept "Key: value" lines as a convenience.
            headers: dict[str, str] = {}
            for line in text.splitlines():
                if ":" in line:
                    key, _, val = line.partition(":")
                    if key.strip():
                        headers[key.strip()] = val.strip()
            return headers
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items() if str(k).strip()}
    return {}


def join_url(base_url: str, path: str) -> str:
    """Join a configured base URL with a path, tolerating both having slashes."""
    path = (path or "").strip()
    base = (base_url or "").strip()
    if not path:
        return base
    if path.lower().startswith(("http://", "https://")):
        return path
    if not base:
        return path
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def substitute(template: str, **values: Any) -> str:
    """Replace ``{name}`` placeholders without failing on unknown keys."""
    result = template or ""
    for key, value in values.items():
        result = result.replace("{" + key + "}", str(value))
    return result


def preview_body(text: str, limit: int = 800) -> str:
    """Trim a response body for display on the debug page."""
    if not text:
        return ""
    snippet = text[:limit]
    if len(text) > limit:
        snippet += f"\n... ({len(text) - limit} more characters)"
    return snippet
