"""Runtime settings: environment defaults overridden by dashboard edits.

``Settings`` (pydantic) holds the immutable values read from the environment.
Anything the operator can change from ``/settings`` at runtime is stored in the
``settings`` table and layered on top by this class, so a change takes effect
without restarting the process.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from app.core.config import Settings

logger = logging.getLogger(__name__)

#: key -> (python type, Settings attribute providing the default)
EDITABLE_KEYS: dict[str, tuple[type, str]] = {
    "check_interval_seconds": (int, "check_interval_seconds"),
    "process_monitor_interval_seconds": (int, "process_monitor_interval_seconds"),
    "failure_threshold": (int, "failure_threshold"),
    "stall_timeout_seconds": (int, "stall_timeout_seconds"),
    "probe_timeout_seconds": (int, "probe_timeout_seconds"),
    "max_restart_delay_seconds": (int, "max_restart_delay_seconds"),
    "restart_window_seconds": (int, "restart_window_seconds"),
    "restart_window_threshold": (int, "restart_window_threshold"),
    "unstable_restart_delay_seconds": (int, "unstable_restart_delay_seconds"),
    "stall_detection_enabled": (bool, "stall_detection_enabled"),
    "stall_seconds": (int, "stall_seconds"),
    "stall_auto_restart": (bool, "stall_auto_restart"),
    "failover_enabled": (bool, "failover_enabled"),
    "failover_after_seconds": (int, "failover_after_seconds"),
    "failover_failure_threshold": (int, "failover_failure_threshold"),
    "failover_min_stable_seconds": (int, "failover_min_stable_seconds"),
    "auto_failback": (bool, "auto_failback"),
    "failback_after_seconds": (int, "failback_after_seconds"),
    "failback_probe_interval_seconds": (int, "failback_probe_interval_seconds"),
    "failback_shadow_seconds": (int, "failback_shadow_seconds"),
    "failback_penalty_window_seconds": (int, "failback_penalty_window_seconds"),
    "failback_penalty_max_seconds": (int, "failback_penalty_max_seconds"),
    "all_down_slow_after_seconds": (int, "all_down_slow_after_seconds"),
    "all_down_retry_delay_seconds": (int, "all_down_retry_delay_seconds"),
    "slate_keep_for_rtmp": (bool, "slate_keep_for_rtmp"),
    "seamless_video_size": (str, "seamless_video_size"),
    "seamless_fps": (int, "seamless_fps"),
    "relay_port_base": (int, "relay_port_base"),
    "ffmpeg_path": (str, "ffmpeg_path"),
    "ffprobe_path": (str, "ffprobe_path"),
    "default_rtmp_server": (str, "default_rtmp_server"),
    "default_stream_mode": (str, "default_stream_mode"),
    "transcode_video_bitrate": (str, "transcode_video_bitrate"),
    "transcode_audio_bitrate": (str, "transcode_audio_bitrate"),
    "transcode_preset": (str, "transcode_preset"),
    "transcode_hardware": (str, "transcode_hardware"),
    "buffer_enabled": (bool, "buffer_enabled"),
    "buffer_seconds": (int, "buffer_seconds"),
    "buffer_slate_enabled": (bool, "buffer_slate_enabled"),
    "slate_path": (str, "slate_path"),
    "slate_max_seconds": (int, "slate_max_seconds"),
    "mediamtx_path": (str, "mediamtx_path"),
    "mediamtx_rtmp_port": (int, "mediamtx_rtmp_port"),
    "mediamtx_hls_port": (int, "mediamtx_hls_port"),
    "mediamtx_api_port": (int, "mediamtx_api_port"),
    "viewer_host": (str, "viewer_host"),
    "source_cache_ttl_seconds": (int, "source_cache_ttl_seconds"),
    "source_user_agent": (str, "source_user_agent"),
    "telegram_chat_id": (str, "telegram_chat_id"),
    "show_full_source_url": (bool, "show_full_source_url"),
    "ui_language": (str, "ui_language"),
}

#: Sensible bounds, enforced on every write.
NUMERIC_BOUNDS: dict[str, tuple[int, int]] = {
    "check_interval_seconds": (30, 86_400),
    "process_monitor_interval_seconds": (1, 60),
    "failure_threshold": (1, 20),
    "stall_timeout_seconds": (10, 3_600),
    "probe_timeout_seconds": (5, 120),
    "max_restart_delay_seconds": (5, 3_600),
    "restart_window_seconds": (60, 86_400),
    "restart_window_threshold": (2, 100),
    "unstable_restart_delay_seconds": (10, 3_600),
    "source_cache_ttl_seconds": (0, 86_400),
    "stall_seconds": (20, 3_600),
    "failover_after_seconds": (30, 86_400),
    "failover_failure_threshold": (1, 20),
    "failover_min_stable_seconds": (10, 3_600),
    "failback_after_seconds": (60, 86_400),
    "failback_probe_interval_seconds": (15, 3_600),
    "failback_shadow_seconds": (0, 3_600),
    "failback_penalty_window_seconds": (0, 86_400),
    "failback_penalty_max_seconds": (60, 86_400),
    "all_down_slow_after_seconds": (0, 86_400),
    "all_down_retry_delay_seconds": (10, 3_600),
    "seamless_fps": (5, 60),
    "relay_port_base": (1_024, 65_000),
    "buffer_seconds": (0, 300),
    "slate_max_seconds": (0, 86_400),
    "mediamtx_rtmp_port": (1, 65_535),
    "mediamtx_hls_port": (1, 65_535),
    "mediamtx_api_port": (1, 65_535),
}


class SettingsValidationError(ValueError):
    """Raised when a dashboard-supplied setting fails validation."""


def _coerce(key: str, value: Any, expected: type) -> Any:
    if expected is bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if expected is int:
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise SettingsValidationError(f"{key} must be a whole number") from exc
        low, high = NUMERIC_BOUNDS.get(key, (0, 2**31 - 1))
        if not low <= number <= high:
            raise SettingsValidationError(f"{key} must be between {low} and {high}")
        return number
    text = "" if value is None else str(value).strip()
    if key == "default_rtmp_server" and text and not text.startswith(("rtmp://", "rtmps://")):
        raise SettingsValidationError("default_rtmp_server must start with rtmp:// or rtmps://")
    if key == "default_stream_mode" and text not in {"copy", "transcode"}:
        raise SettingsValidationError("default_stream_mode must be 'copy' or 'transcode'")
    if key == "transcode_hardware":
        allowed = {
            "auto", "off", "software", "libx264", "cpu",
            "videotoolbox", "nvenc", "qsv", "amf", "v4l2m2m",
        }
        if text.lower() not in allowed and not text.lower().startswith("h264_"):
            raise SettingsValidationError(
                "transcode_hardware must be auto/off or a known encoder name"
            )
        text = text.lower()
    if key == "seamless_video_size":
        width, _, height = text.lower().partition("x")
        if not (width.isdigit() and height.isdigit()):
            raise SettingsValidationError("seamless_video_size must look like 1280x720")
        text = text.lower()
    if key == "ui_language" and text.lower() not in {"th", "en"}:
        raise SettingsValidationError("ui_language must be 'th' or 'en'")
    return text


class SettingsStore:
    """Effective configuration = environment defaults + database overrides."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._overrides: dict[str, Any] = {}
        self._listeners: list[Callable[[str, Any], None]] = []

    # ------------------------------------------------------------------ #
    @property
    def env(self) -> Settings:
        """The immutable environment-derived settings."""
        return self._settings

    def add_listener(self, callback: Callable[[str, Any], None]) -> None:
        self._listeners.append(callback)

    def load(self, rows: dict[str, str]) -> None:
        """Populate overrides from ``{key: json_value}`` database rows."""
        for key, raw in rows.items():
            if key not in EDITABLE_KEYS:
                continue
            expected, _ = EDITABLE_KEYS[key]
            try:
                decoded = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                decoded = raw
            try:
                self._overrides[key] = _coerce(key, decoded, expected)
            except SettingsValidationError as exc:
                logger.warning("ignoring stored setting %s: %s", key, exc)

    def get(self, key: str) -> Any:
        if key in self._overrides:
            return self._overrides[key]
        if key in EDITABLE_KEYS:
            return getattr(self._settings, EDITABLE_KEYS[key][1])
        return getattr(self._settings, key)

    def get_int(self, key: str) -> int:
        return int(self.get(key))

    def get_str(self, key: str) -> str:
        return str(self.get(key) or "")

    def get_bool(self, key: str) -> bool:
        return bool(self.get(key))

    def set(self, key: str, value: Any) -> Any:
        """Validate and apply an override. Returns the coerced value."""
        if key not in EDITABLE_KEYS:
            raise SettingsValidationError(f"'{key}' is not an editable setting")
        expected, _ = EDITABLE_KEYS[key]
        coerced = _coerce(key, value, expected)
        self._overrides[key] = coerced
        for callback in self._listeners:
            try:
                callback(key, coerced)
            except Exception:  # noqa: BLE001 - a listener must not break a save
                logger.exception("settings listener failed for %s", key)
        return coerced

    def serialize(self, key: str) -> str:
        return json.dumps(self.get(key))

    def as_dict(self) -> dict[str, Any]:
        return {key: self.get(key) for key in EDITABLE_KEYS}

    def overrides(self) -> dict[str, Any]:
        return dict(self._overrides)
