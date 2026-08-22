"""Request models and response serialisers for the JSON API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.core.security import mask_url_token, shorten_url
from app.core.state import ChannelState, parse_state
from app.core.timeutil import ensure_utc, humanize_duration, isoformat, seconds_since


# --------------------------------------------------------------------------- #
# requests
# --------------------------------------------------------------------------- #
class ChannelPayload(BaseModel):
    """Create/update payload for a channel."""

    name: str = Field(min_length=1, max_length=255)
    provider_id: int | None = None
    provider_ref: str = ""
    input_url: str = ""
    #: Per-channel endpoint that answers with the media URL.
    resolve_url: str = ""
    logo_url: str = ""
    group_title: str = ""
    rtmp_url: str = ""
    stream_key: str = ""
    stream_mode: str = "copy"
    enabled: bool = True
    auto_start: bool = False
    sort_order: int = 0
    playback_referer: str = ""
    playback_user_agent: str = ""
    playback_headers_json: str = ""

    @field_validator("stream_mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        if v not in ("copy", "transcode"):
            raise ValueError("stream_mode must be 'copy' or 'transcode'")
        return v

    @field_validator("rtmp_url")
    @classmethod
    def _rtmp(cls, v: str) -> str:
        v = v.strip()
        if v and not v.startswith(("rtmp://", "rtmps://")):
            raise ValueError("rtmp_url must start with rtmp:// or rtmps://")
        return v

    @field_validator("playback_headers_json")
    @classmethod
    def _headers(cls, v: str) -> str:
        import json

        v = (v or "").strip()
        if not v:
            return ""
        try:
            parsed = json.loads(v)
        except json.JSONDecodeError as exc:
            raise ValueError("playback headers must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("playback headers must be a JSON object")
        return json.dumps(parsed)


class ProviderPayload(BaseModel):
    """Create/update payload for a provider."""

    name: str = Field(min_length=1, max_length=128)
    type: str = Field(min_length=1, max_length=48)
    enabled: bool = True
    is_default: bool = False
    config: dict[str, Any] = Field(default_factory=dict)
    #: Credentials - write-only, stored in the secret store, never returned.
    username: str | None = None
    password: str | None = None
    token: str | None = None
    cookie: str | None = None


class SettingsPayload(BaseModel):
    """Partial update of runtime settings."""

    values: dict[str, Any] = Field(default_factory=dict)
    telegram_bot_token: str | None = None
    admin_password: str | None = None


class BulkActionPayload(BaseModel):
    action: str
    channel_ids: list[int] = Field(default_factory=list)

    @field_validator("action")
    @classmethod
    def _action(cls, v: str) -> str:
        allowed = {"start", "stop", "restart", "refresh", "enable", "disable"}
        if v not in allowed:
            raise ValueError(f"action must be one of {sorted(allowed)}")
        return v


class ChannelListPayload(BaseModel):
    """Bulk channel entry: pasted lines or a JSON document."""

    text: str = ""
    provider_id: int | None = None
    stream_mode: str = ""
    stream_key_prefix: str = ""
    enabled: bool = True
    auto_start: bool = False

    @field_validator("stream_mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        if v and v not in ("copy", "transcode"):
            raise ValueError("stream_mode must be 'copy' or 'transcode'")
        return v


class IptvChannelRow(BaseModel):
    """One row of the IPTV form's channel list.

    ``id`` is set for rows that came from an existing channel, so the save can
    tell an edit from a new addition.
    """

    id: int | None = None
    name: str = ""
    url: str = ""
    stream_key: str = ""


class IptvPayload(BaseModel):
    """The friendly IPTV form: login once, then a list of named URLs."""

    id: int | None = None
    name: str = Field(min_length=1, max_length=128)
    requires_login: bool = False
    base_url: str = ""
    login_url: str = "/login"
    success_url: str = ""
    prime_url: str = ""
    username_field: str = "username"
    password_field: str = "password"
    username: str | None = None
    password: str | None = None
    url_path: str = ""
    stream_mode: str = ""
    enabled: bool = True
    auto_start: bool = False
    channels: list[IptvChannelRow] = Field(default_factory=list)
    #: Channel ids the form loaded, so removed rows can be deleted precisely.
    #: Empty means "don't delete anything" (e.g. the add form).
    known_channel_ids: list[int] = Field(default_factory=list)

    @field_validator("stream_mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        if v and v not in ("copy", "transcode"):
            raise ValueError("stream_mode must be 'copy' or 'transcode'")
        return v


class IptvPreviewPayload(BaseModel):
    """Fetch one URL through an unsaved IPTV config, to inspect the response."""

    url: str = ""
    requires_login: bool = False
    base_url: str = ""
    login_url: str = "/login"
    success_url: str = ""
    prime_url: str = ""
    username_field: str = "username"
    password_field: str = "password"
    username: str | None = None
    password: str | None = None
    #: The chosen JSON field, so a test resolves the same way a save would.
    url_path: str = ""
    #: When editing, credentials may already be stored under this provider id.
    provider_id: int | None = None


class ImportPayload(BaseModel):
    data: dict[str, Any]
    replace: bool = False


# --------------------------------------------------------------------------- #
# responses
# --------------------------------------------------------------------------- #
def serialize_channel(
    channel: Any,
    snapshot: dict[str, Any] | None = None,
    *,
    reveal_url: bool = False,
    default_rtmp: str = "",
    provider_name: str = "",
) -> dict[str, Any]:
    """Combine a database row with live supervisor data for the UI."""
    snapshot = snapshot or {}
    state = parse_state(snapshot.get("state") or channel.status)
    ffmpeg = snapshot.get("ffmpeg") or {}
    metrics = ffmpeg.get("metrics") or {}
    source_url = channel.source_url or ""

    return {
        "id": channel.id,
        "name": channel.name,
        "logo_url": channel.logo_url,
        "group_title": channel.group_title,
        "provider_id": channel.provider_id,
        "provider_name": provider_name,
        "provider_ref": channel.provider_ref,
        "input_url": channel.input_url,
        "resolve_url": getattr(channel, "resolve_url", "") or "",
        "enabled": channel.enabled,
        "auto_start": channel.auto_start,
        "source_present": channel.source_present,
        "status": state.value,
        "status_badge": state.badge,
        "status_label": state.label,
        "stream_mode": channel.stream_mode,
        "rtmp_url": channel.resolved_rtmp(default_rtmp),
        "rtmp_configured": bool(channel.resolved_rtmp(default_rtmp)),
        "stream_key": channel.stream_key,
        "source_url": source_url if reveal_url else mask_url_token(source_url),
        "source_url_short": shorten_url(source_url),
        "source_resolved_at": isoformat(channel.source_resolved_at),
        "source_expires_at": isoformat(channel.source_expires_at),
        "source_age": humanize_duration(seconds_since(channel.source_resolved_at)),
        "resolve_count": channel.resolve_count,
        "resolve_error": channel.resolve_error,
        "ffmpeg_pid": ffmpeg.get("pid") or channel.ffmpeg_pid,
        "ffmpeg_running": bool(ffmpeg.get("running")),
        "started_at": isoformat(channel.started_at),
        "uptime_seconds": snapshot.get("uptime_seconds", 0),
        "uptime": humanize_duration(snapshot.get("uptime_seconds") or 0),
        "last_check_at": isoformat(channel.last_check_at),
        "last_check_age": humanize_duration(seconds_since(channel.last_check_at)),
        "last_online_at": isoformat(channel.last_online_at),
        "restart_count": channel.restart_count,
        "restarts_in_window": snapshot.get("restarts_in_window", 0),
        "unstable": snapshot.get("unstable", False),
        "last_error": snapshot.get("last_error") or channel.last_error,
        "bitrate": metrics.get("bitrate", ""),
        "speed": metrics.get("speed", ""),
        "out_time": metrics.get("out_time", ""),
        "seconds_since_progress": metrics.get("seconds_since_progress"),
        "probe": snapshot.get("probe"),
        "supervisor_running": snapshot.get("supervisor_running", False),
    }


def serialize_provider(
    provider: Any,
    *,
    secrets_present: dict[str, bool] | None = None,
    instance: Any = None,
) -> dict[str, Any]:
    """Serialise a provider row. Credentials are reported as present/absent only."""
    return {
        "id": provider.id,
        "name": provider.name,
        "type": provider.type,
        "enabled": provider.enabled,
        "is_default": provider.is_default,
        "config": provider.config,
        "last_auth_at": isoformat(provider.last_auth_at),
        "last_auth_ok": provider.last_auth_ok,
        "last_error": provider.last_error,
        "secrets": secrets_present or {},
        "loaded": instance is not None,
        "supports_discovery": bool(
            getattr(instance, "supports_discovery", False) if instance else False
        ),
    }


def serialize_event(event: Any) -> dict[str, Any]:
    return {
        "id": event.id,
        "channel_id": event.channel_id,
        "channel_name": event.channel_name,
        "event_type": event.event_type,
        "level": event.level,
        "message": event.message,
        "created_at": isoformat(event.created_at),
    }


def serialize_downtime(record: Any) -> dict[str, Any]:
    down_at = ensure_utc(record.down_at)
    recovered_at = ensure_utc(record.recovered_at)
    duration = (
        (recovered_at - down_at).total_seconds() if down_at and recovered_at else None
    )
    return {
        "id": record.id,
        "channel_id": record.channel_id,
        "channel_name": record.channel_name,
        "down_at": isoformat(record.down_at),
        "recovered_at": isoformat(record.recovered_at),
        "duration_seconds": duration,
        "duration": humanize_duration(duration) if duration is not None else "ongoing",
        "cause": record.cause,
        "attempts": record.attempts,
    }


def summarise(channels: list[dict[str, Any]]) -> dict[str, int]:
    """Counts for the dashboard cards."""
    counters = {
        "total": len(channels),
        "online": 0,
        "offline": 0,
        "reconnecting": 0,
        "disabled": 0,
    }
    for channel in channels:
        state = parse_state(channel["status"])
        if state in (ChannelState.ONLINE, ChannelState.DEGRADED):
            counters["online"] += 1
        elif state in (ChannelState.RECONNECTING, ChannelState.STARTING):
            counters["reconnecting"] += 1
        elif state in (
            ChannelState.DISABLED,
            ChannelState.CONFIG_REQUIRED,
            ChannelState.UNSUPPORTED,
        ):
            counters["disabled"] += 1
        else:
            counters["offline"] += 1
    return counters
