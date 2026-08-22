"""SQLAlchemy models.

Five tables:

* ``providers``         - one row per configured source provider
* ``channels``          - one row per relay (source in, RTMP out)
* ``settings``          - runtime overrides edited from the dashboard
* ``stream_events``     - append-only audit trail
* ``downtime_records``  - one row per outage, closed on recovery

A channel points at a provider but stores nothing provider-specific beyond an
opaque ``provider_ref``; the streaming layer never inspects it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.state import ChannelState


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp (naive datetimes cause subtle bugs)."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Provider(Base):
    """A configured source provider.

    ``config`` is JSON and never contains a credential - usernames, passwords,
    tokens and cookies live in the secret store keyed by provider id.
    """

    __tablename__ = "providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Matches ``StreamProvider.type_name``.
    type: Mapped[str] = mapped_column(String(48), nullable=False, default="manual")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    config_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    last_auth_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_auth_ok: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    # ------------------------------------------------------------------ #
    @property
    def config(self) -> dict[str, Any]:
        try:
            parsed = json.loads(self.config_json or "{}")
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def set_config(self, value: dict[str, Any]) -> None:
        self.config_json = json.dumps(value or {}, indent=2, sort_keys=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Provider id={self.id} name={self.name!r} type={self.type}>"


class Channel(Base):
    """A single relay: one source in, one RTMP destination out."""

    __tablename__ = "channels"
    __table_args__ = (
        Index("ix_channels_provider_ref", "provider_id", "provider_ref"),
        Index("ix_channels_enabled", "enabled"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # ---- identity -------------------------------------------------------
    external_id: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    logo_url: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    group_title: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ---- source ---------------------------------------------------------
    provider_id: Mapped[int | None] = mapped_column(
        ForeignKey("providers.id", ondelete="SET NULL")
    )
    #: Opaque provider-side identifier. Never interpreted outside the provider.
    provider_ref: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    #: Operator-supplied media URL, used as-is (manual / static providers).
    input_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Operator-supplied *endpoint* for this channel - a page or API URL that
    #: answers with the media URL. One channel, one URL. When set it overrides
    #: any provider-wide URL template.
    resolve_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Per-channel playback hints handed to FFmpeg.
    playback_referer: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    playback_user_agent: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    playback_headers_json: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: Last successfully resolved URL (may carry a short-lived token).
    source_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source_resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolve_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    resolve_error: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # ---- destination ----------------------------------------------------
    rtmp_url: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    stream_key: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    stream_mode: Mapped[str] = mapped_column(String(16), default="copy", nullable=False)

    # ---- operational flags ----------------------------------------------
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    auto_start: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source_present: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ---- runtime status --------------------------------------------------
    status: Mapped[str] = mapped_column(
        String(32), default=ChannelState.STOPPED.value, nullable=False
    )
    ffmpeg_pid: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_online_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    restart_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    # ------------------------------------------------------------------ #
    @property
    def playback_headers(self) -> dict[str, str]:
        if not self.playback_headers_json:
            return {}
        try:
            parsed = json.loads(self.playback_headers_json)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {str(k): str(v) for k, v in parsed.items()}

    def resolved_rtmp(self, default_server: str = "") -> str:
        """Compose the effective output URL.

        A full ``rtmp_url`` always wins; otherwise the default server and the
        per-channel stream key are joined.
        """
        if self.rtmp_url:
            return self.rtmp_url
        if default_server and self.stream_key:
            return f"{default_server.rstrip('/')}/{self.stream_key.lstrip('/')}"
        return ""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Channel id={self.id} name={self.name!r} status={self.status}>"


class Setting(Base):
    """Key/value overrides for :class:`app.core.settings_store.SettingsStore`."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class EventType(str):
    """String constants for :attr:`StreamEvent.event_type`."""

    STREAM_STARTED = "stream_started"
    STREAM_STOPPED = "stream_stopped"
    STREAM_DOWN = "stream_down"
    STREAM_RECOVERED = "stream_recovered"
    STREAM_RESTART = "stream_restart"
    STREAM_STALLED = "stream_stalled"
    SOURCE_REFRESHED = "source_refreshed"
    SOURCE_FAILED = "source_failed"
    SOURCE_UNSUPPORTED = "source_unsupported"
    PROVIDER_AUTH_OK = "provider_auth_ok"
    PROVIDER_AUTH_FAILED = "provider_auth_failed"
    CHANNEL_ADDED = "channel_added"
    CHANNEL_UPDATED = "channel_updated"
    CHANNEL_REMOVED_FROM_SOURCE = "channel_removed_from_source"
    CHANNEL_UNSTABLE = "channel_unstable"
    CONFIG_CHANGED = "config_changed"
    SYSTEM_STARTED = "system_started"
    SYSTEM_STOPPED = "system_stopped"
    SYSTEM_ERROR = "system_error"


ALL_EVENT_TYPES: tuple[str, ...] = (
    EventType.STREAM_STARTED,
    EventType.STREAM_STOPPED,
    EventType.STREAM_DOWN,
    EventType.STREAM_RECOVERED,
    EventType.STREAM_RESTART,
    EventType.STREAM_STALLED,
    EventType.SOURCE_REFRESHED,
    EventType.SOURCE_FAILED,
    EventType.SOURCE_UNSUPPORTED,
    EventType.PROVIDER_AUTH_OK,
    EventType.PROVIDER_AUTH_FAILED,
    EventType.CHANNEL_ADDED,
    EventType.CHANNEL_UPDATED,
    EventType.CHANNEL_REMOVED_FROM_SOURCE,
    EventType.CHANNEL_UNSTABLE,
    EventType.CONFIG_CHANGED,
    EventType.SYSTEM_STARTED,
    EventType.SYSTEM_STOPPED,
    EventType.SYSTEM_ERROR,
)


class StreamEvent(Base):
    """Append-only log of everything that happened to a channel."""

    __tablename__ = "stream_events"
    __table_args__ = (
        Index("ix_events_channel_created", "channel_id", "created_at"),
        Index("ix_events_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int | None] = mapped_column(
        ForeignKey("channels.id", ondelete="SET NULL")
    )
    channel_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    level: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class DowntimeRecord(Base):
    """One outage. ``recovered_at`` stays NULL until the channel comes back."""

    __tablename__ = "downtime_records"
    __table_args__ = (Index("ix_downtime_channel", "channel_id", "down_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int | None] = mapped_column(
        ForeignKey("channels.id", ondelete="SET NULL")
    )
    channel_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    down_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cause: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    @property
    def duration_seconds(self) -> float | None:
        if self.recovered_at is None:
            return None
        return (self.recovered_at - self.down_at).total_seconds()
