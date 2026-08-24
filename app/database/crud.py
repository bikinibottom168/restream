"""Data-access helpers.

Every function takes an open :class:`~sqlalchemy.orm.Session` as its first
argument so it can be used with :func:`app.database.db.call_db` and
:func:`app.database.db.run_db`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.core.state import ChannelState
from app.core.timeutil import ensure_utc, utcnow
from app.database.models import (
    Channel,
    DowntimeRecord,
    EventType,
    Provider,
    Setting,
    StreamEvent,
)

logger = logging.getLogger(__name__)

#: Fields a caller is allowed to write through :func:`update_channel`.
WRITABLE_CHANNEL_FIELDS = frozenset(
    {
        "external_id",
        "name",
        "logo_url",
        "group_title",
        "sort_order",
        "provider_id",
        "provider_ref",
        "input_url",
        "resolve_url",
        "fallback_urls",
        "active_source_index",
        "failover_after_seconds",
        "failback_after_seconds",
        "auto_failback",
        "seamless_switch",
        "playback_referer",
        "playback_user_agent",
        "playback_headers_json",
        "source_url",
        "source_resolved_at",
        "source_expires_at",
        "resolve_count",
        "resolve_error",
        "rtmp_url",
        "stream_key",
        "stream_mode",
        "enabled",
        "auto_start",
        "source_present",
        "status",
        "ffmpeg_pid",
        "started_at",
        "last_check_at",
        "last_online_at",
        "restart_count",
        "last_error",
    }
)


# --------------------------------------------------------------------------- #
# channels
# --------------------------------------------------------------------------- #
def list_channels(session: Session, *, enabled_only: bool = False) -> list[Channel]:
    stmt = select(Channel).order_by(Channel.sort_order, Channel.name)
    if enabled_only:
        stmt = stmt.where(Channel.enabled.is_(True))
    return list(session.scalars(stmt))


def get_channel(session: Session, channel_id: int) -> Channel | None:
    return session.get(Channel, channel_id)


def get_channel_by_provider_ref(
    session: Session, provider_id: int | None, provider_ref: str
) -> Channel | None:
    if not provider_ref:
        return None
    stmt = select(Channel).where(
        Channel.provider_id == provider_id, Channel.provider_ref == provider_ref
    )
    return session.scalars(stmt).first()


def list_channels_for_provider(session: Session, provider_id: int) -> list[Channel]:
    stmt = select(Channel).where(Channel.provider_id == provider_id)
    return list(session.scalars(stmt))


def create_channel(session: Session, **fields: Any) -> Channel:
    payload = {k: v for k, v in fields.items() if k in WRITABLE_CHANNEL_FIELDS}
    payload.setdefault("status", ChannelState.STOPPED.value)
    channel = Channel(**payload)
    session.add(channel)
    session.flush()  # assign the primary key before the caller needs it
    return channel


def update_channel(session: Session, channel_id: int, **fields: Any) -> Channel | None:
    channel = session.get(Channel, channel_id)
    if channel is None:
        return None
    for key, value in fields.items():
        if key in WRITABLE_CHANNEL_FIELDS:
            setattr(channel, key, value)
        else:
            logger.debug("ignoring non-writable channel field %r", key)
    channel.updated_at = utcnow()
    session.flush()
    return channel


def set_channel_status(
    session: Session,
    channel_id: int,
    status: ChannelState | str,
    *,
    last_error: str | None = None,
    ffmpeg_pid: int | None = ...,  # type: ignore[assignment]
    started_at: datetime | None = ...,  # type: ignore[assignment]
) -> Channel | None:
    """Persist a state change. ``...`` means "leave this column alone"."""
    channel = session.get(Channel, channel_id)
    if channel is None:
        return None
    channel.status = status.value if isinstance(status, ChannelState) else str(status)
    if last_error is not None:
        channel.last_error = last_error
    if ffmpeg_pid is not ...:
        channel.ffmpeg_pid = ffmpeg_pid
    if started_at is not ...:
        channel.started_at = started_at
    if channel.status == ChannelState.ONLINE.value:
        channel.last_online_at = utcnow()
    channel.updated_at = utcnow()
    session.flush()
    return channel


def delete_channel(session: Session, channel_id: int) -> bool:
    channel = session.get(Channel, channel_id)
    if channel is None:
        return False
    session.execute(
        update(StreamEvent)
        .where(StreamEvent.channel_id == channel_id)
        .values(channel_id=None)
    )
    session.execute(
        update(DowntimeRecord)
        .where(DowntimeRecord.channel_id == channel_id)
        .values(channel_id=None)
    )
    session.delete(channel)
    return True


def increment_restart_count(session: Session, channel_id: int) -> int:
    channel = session.get(Channel, channel_id)
    if channel is None:
        return 0
    channel.restart_count += 1
    channel.updated_at = utcnow()
    session.flush()
    return channel.restart_count


def mark_source_resolved(
    session: Session,
    channel_id: int,
    url: str,
    *,
    expires_at: datetime | None = None,
    error: str = "",
) -> Channel | None:
    channel = session.get(Channel, channel_id)
    if channel is None:
        return None
    if url:
        channel.source_url = url
        channel.source_resolved_at = utcnow()
        channel.resolve_count += 1
        channel.source_expires_at = expires_at
    channel.resolve_error = error
    channel.updated_at = utcnow()
    session.flush()
    return channel


def channels_needing_autostart(session: Session) -> list[Channel]:
    """Enabled + auto_start channels that already have a destination."""
    stmt = select(Channel).where(
        Channel.enabled.is_(True), Channel.auto_start.is_(True)
    )
    return list(session.scalars(stmt))


def count_by_status(session: Session) -> dict[str, int]:
    stmt = select(Channel.status, func.count(Channel.id)).group_by(Channel.status)
    return {status: count for status, count in session.execute(stmt)}


# --------------------------------------------------------------------------- #
# providers
# --------------------------------------------------------------------------- #
def list_providers(session: Session, *, enabled_only: bool = False) -> list[Provider]:
    stmt = select(Provider).order_by(Provider.is_default.desc(), Provider.name)
    if enabled_only:
        stmt = stmt.where(Provider.enabled.is_(True))
    return list(session.scalars(stmt))


def get_provider(session: Session, provider_id: int | None) -> Provider | None:
    if provider_id is None:
        return None
    return session.get(Provider, provider_id)


def get_default_provider(session: Session) -> Provider | None:
    stmt = select(Provider).where(
        Provider.is_default.is_(True), Provider.enabled.is_(True)
    )
    provider = session.scalars(stmt).first()
    if provider is not None:
        return provider
    stmt = select(Provider).where(Provider.enabled.is_(True)).order_by(Provider.id)
    return session.scalars(stmt).first()


def create_provider(
    session: Session,
    *,
    name: str,
    type: str,
    config: dict[str, Any] | None = None,
    enabled: bool = True,
    is_default: bool = False,
) -> Provider:
    provider = Provider(name=name, type=type, enabled=enabled, is_default=is_default)
    provider.set_config(config or {})
    session.add(provider)
    session.flush()
    if is_default:
        _clear_other_defaults(session, provider.id)
    return provider


def update_provider(
    session: Session,
    provider_id: int,
    *,
    name: str | None = None,
    type: str | None = None,
    config: dict[str, Any] | None = None,
    enabled: bool | None = None,
    is_default: bool | None = None,
) -> Provider | None:
    provider = session.get(Provider, provider_id)
    if provider is None:
        return None
    if name is not None:
        provider.name = name
    if type is not None:
        provider.type = type
    if config is not None:
        provider.set_config(config)
    if enabled is not None:
        provider.enabled = enabled
    if is_default is not None:
        provider.is_default = is_default
        if is_default:
            _clear_other_defaults(session, provider_id)
    provider.updated_at = utcnow()
    session.flush()
    return provider


def _clear_other_defaults(session: Session, keep_id: int) -> None:
    session.execute(
        update(Provider).where(Provider.id != keep_id).values(is_default=False)
    )


def set_provider_auth_state(
    session: Session, provider_id: int, *, ok: bool, error: str = ""
) -> Provider | None:
    provider = session.get(Provider, provider_id)
    if provider is None:
        return None
    provider.last_auth_ok = ok
    provider.last_auth_at = utcnow()
    provider.last_error = error[:1000]
    session.flush()
    return provider


def delete_provider(session: Session, provider_id: int) -> bool:
    provider = session.get(Provider, provider_id)
    if provider is None:
        return False
    session.execute(
        update(Channel).where(Channel.provider_id == provider_id).values(provider_id=None)
    )
    session.delete(provider)
    return True


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
def all_settings(session: Session) -> dict[str, str]:
    return {row.key: row.value for row in session.scalars(select(Setting))}


def get_setting(session: Session, key: str) -> str | None:
    row = session.get(Setting, key)
    return row.value if row else None


def set_setting(session: Session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow()
    session.flush()


def set_settings(session: Session, values: dict[str, str]) -> None:
    for key, value in values.items():
        set_setting(session, key, value)


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #
def add_event(
    session: Session,
    *,
    event_type: str,
    message: str = "",
    channel_id: int | None = None,
    channel_name: str = "",
    level: str = "info",
) -> StreamEvent:
    if channel_id and not channel_name:
        channel = session.get(Channel, channel_id)
        channel_name = channel.name if channel else ""
    entry = StreamEvent(
        channel_id=channel_id,
        channel_name=channel_name,
        event_type=event_type,
        message=message[:4000],
        level=level,
    )
    session.add(entry)
    session.flush()
    return entry


def list_events(
    session: Session,
    *,
    channel_id: int | None = None,
    event_types: Sequence[str] | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[StreamEvent]:
    stmt = select(StreamEvent).order_by(StreamEvent.created_at.desc(), StreamEvent.id.desc())
    if channel_id is not None:
        stmt = stmt.where(StreamEvent.channel_id == channel_id)
    if event_types:
        stmt = stmt.where(StreamEvent.event_type.in_(list(event_types)))
    return list(session.scalars(stmt.limit(limit).offset(offset)))


def prune_events(session: Session, keep_days: int = 30) -> int:
    cutoff = utcnow() - timedelta(days=keep_days)
    result = session.execute(delete(StreamEvent).where(StreamEvent.created_at < cutoff))
    return int(result.rowcount or 0)


# --------------------------------------------------------------------------- #
# downtime
# --------------------------------------------------------------------------- #
def open_downtime(
    session: Session, channel_id: int, channel_name: str, cause: str
) -> DowntimeRecord:
    """Open an outage record, reusing one that is still open."""
    existing = current_downtime(session, channel_id)
    if existing is not None:
        return existing
    record = DowntimeRecord(
        channel_id=channel_id, channel_name=channel_name, cause=cause[:250], attempts=0
    )
    session.add(record)
    session.flush()
    return record


def current_downtime(session: Session, channel_id: int) -> DowntimeRecord | None:
    stmt = (
        select(DowntimeRecord)
        .where(
            DowntimeRecord.channel_id == channel_id,
            DowntimeRecord.recovered_at.is_(None),
        )
        .order_by(DowntimeRecord.down_at.desc())
    )
    return session.scalars(stmt).first()


def bump_downtime_attempts(session: Session, channel_id: int) -> int:
    record = current_downtime(session, channel_id)
    if record is None:
        return 0
    record.attempts += 1
    session.flush()
    return record.attempts


def close_downtime(session: Session, channel_id: int) -> DowntimeRecord | None:
    """Close the open outage and return it (with duration available)."""
    record = current_downtime(session, channel_id)
    if record is None:
        return None
    record.recovered_at = utcnow()
    session.flush()
    return record


def list_downtime(
    session: Session, *, channel_id: int | None = None, limit: int = 200
) -> list[DowntimeRecord]:
    stmt = select(DowntimeRecord).order_by(DowntimeRecord.down_at.desc())
    if channel_id is not None:
        stmt = stmt.where(DowntimeRecord.channel_id == channel_id)
    return list(session.scalars(stmt.limit(limit)))


def downtime_summary(session: Session, *, days: int = 7) -> list[dict[str, Any]]:
    """Outage count and total downtime per channel over the last *days*."""
    cutoff = utcnow() - timedelta(days=days)
    records = session.scalars(
        select(DowntimeRecord).where(DowntimeRecord.down_at >= cutoff)
    )
    summary: dict[int | None, dict[str, Any]] = {}
    now = utcnow()
    for record in records:
        bucket = summary.setdefault(
            record.channel_id,
            {"channel_id": record.channel_id, "channel_name": record.channel_name,
             "outages": 0, "total_seconds": 0.0},
        )
        bucket["outages"] += 1
        down_at = ensure_utc(record.down_at) or now
        recovered = ensure_utc(record.recovered_at) or now
        bucket["total_seconds"] += max(0.0, (recovered - down_at).total_seconds())
    return sorted(summary.values(), key=lambda item: item["total_seconds"], reverse=True)


# --------------------------------------------------------------------------- #
# bulk helpers used by sync / import
# --------------------------------------------------------------------------- #
def mark_missing_from_source(
    session: Session, provider_id: int, present_refs: Iterable[str]
) -> list[Channel]:
    """Flag channels of *provider_id* whose ref is no longer offered upstream.

    Rows are never deleted automatically - the operator decides.
    """
    refs = set(present_refs)
    missing: list[Channel] = []
    stmt = select(Channel).where(Channel.provider_id == provider_id)
    for channel in session.scalars(stmt):
        should_be_present = channel.provider_ref in refs
        if channel.source_present != should_be_present:
            channel.source_present = should_be_present
            channel.updated_at = utcnow()
            if not should_be_present:
                missing.append(channel)
    session.flush()
    return missing


def reset_runtime_state(session: Session) -> int:
    """On startup, no FFmpeg is running - clear stale RUNNING-ish statuses."""
    running_states = [
        ChannelState.ONLINE.value,
        ChannelState.DEGRADED.value,
        ChannelState.STARTING.value,
        ChannelState.RECONNECTING.value,
    ]
    result = session.execute(
        update(Channel)
        .where(Channel.status.in_(running_states))
        .values(status=ChannelState.STOPPED.value, ffmpeg_pid=None, started_at=None)
    )
    return int(result.rowcount or 0)


def close_orphan_downtime(session: Session) -> int:
    """Close outages left open by a crash so history stays sane."""
    stmt = select(DowntimeRecord).where(DowntimeRecord.recovered_at.is_(None))
    count = 0
    for record in session.scalars(stmt):
        record.recovered_at = utcnow()
        record.cause = (record.cause or "") + " (closed at restart)"
        count += 1
    session.flush()
    return count


__all__ = [name for name in dir() if not name.startswith("_")]
