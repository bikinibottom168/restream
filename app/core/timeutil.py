"""Small time helpers shared by the supervisor, API and templates.

SQLite has no native timezone support, so datetimes come back naive even when
they were written as aware UTC values.  Everything that reads a timestamp from
the database passes it through :func:`ensure_utc` first.
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime | str | None) -> datetime | None:
    """Normalise a timestamp to aware UTC.

    Accepts a datetime (naive values are assumed to be UTC, which is what
    SQLite hands back) or an ISO-8601 string, because templates receive
    already-serialised values.  Anything unparseable becomes ``None``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def seconds_since(value: datetime | str | None) -> float | None:
    """Seconds elapsed since *value*, or ``None`` when it is unset."""
    aware = ensure_utc(value)
    if aware is None:
        return None
    return (utcnow() - aware).total_seconds()


def humanize_duration(seconds: float | int | None) -> str:
    """``3725`` -> ``'1h 2m 5s'``. Returns ``'-'`` for ``None``."""
    if seconds is None:
        return "-"
    total = int(max(0, seconds))
    days, rest = divmod(total, 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def isoformat(value: datetime | str | None) -> str | None:
    aware = ensure_utc(value)
    return aware.isoformat() if aware else None


def format_local(value: datetime | str | None, fmt: str = "%d/%m/%Y %H:%M:%S") -> str:
    """Render a timestamp in the machine's local timezone."""
    aware = ensure_utc(value)
    if aware is None:
        return "-"
    return aware.astimezone().strftime(fmt)
