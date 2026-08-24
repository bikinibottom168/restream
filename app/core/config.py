"""Application configuration.

Every value can be supplied through the environment or a ``.env`` file that
lives next to this project.  Validation is done with pydantic-settings so a bad
value fails loudly at startup instead of halfway through a stream.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.version import APP_TITLE, APP_VERSION

# Project root: <root>/app/core/config.py -> parents[2] == <root>
BASE_DIR = Path(__file__).resolve().parents[2]

StreamMode = Literal["copy", "transcode"]


class Settings(BaseSettings):
    """Validated application settings."""

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- identity ---------------------------------------------------------
    app_name: str = APP_TITLE
    app_version: str = APP_VERSION

    # ---- web --------------------------------------------------------------
    app_host: str = "127.0.0.1"
    app_port: int = 8787
    admin_username: str = ""
    admin_password: str = ""

    # ---- monitoring -------------------------------------------------------
    check_interval_seconds: int = 300
    process_monitor_interval_seconds: int = 5
    failure_threshold: int = 2
    #: A brief input hiccup that a player would hide should not trigger a
    #: restart, so give the output clock some grace before calling it stalled.
    stall_timeout_seconds: int = 90
    probe_timeout_seconds: int = 20

    # ---- restart / backoff ------------------------------------------------
    max_restart_delay_seconds: int = 30
    restart_window_seconds: int = 600
    restart_window_threshold: int = 10
    unstable_restart_delay_seconds: int = 60

    # ---- source failover --------------------------------------------------
    #: Master switch for backup sources. Off means a channel only ever uses its
    #: primary, whatever it has stored in ``fallback_urls``.
    failover_enabled: bool = True
    #: Leave the primary once it has been down this long...
    failover_after_seconds: int = 120
    #: ...or this many starts in a row have died before proving stable, which
    #: is the flapping case a plain timer never catches.
    failover_failure_threshold: int = 3
    #: A source must hold this long to count as genuinely working.
    failover_min_stable_seconds: int = 60
    #: Return to the primary by itself once it proves healthy again. Off by
    #: default: a switch costs a glitch, so the operator picks the moment.
    auto_failback: bool = False
    #: How long the primary must probe clean before a failback is considered.
    failback_after_seconds: int = 600
    #: How often the primary is probed while a fallback is on air.
    failback_probe_interval_seconds: int = 60
    #: Final gate: run the primary into a throwaway output for this long and
    #: require it to keep flowing. A probe that passes is not proof it lasts.
    failback_shadow_seconds: int = 90
    #: A primary that breaks again within this long after a failback doubles
    #: the healthy period required before the next one (capped below).
    failback_penalty_window_seconds: int = 900
    failback_penalty_max_seconds: int = 3_600
    #: Once every source has been down this long, retry this slowly instead of
    #: hammering origins that are clearly not coming back this minute.
    all_down_slow_after_seconds: int = 900
    all_down_retry_delay_seconds: int = 300
    #: Ignore ``slate_max_seconds`` for channels that push to a downstream RTMP
    #: server: letting the slate stop there makes the downstream service end
    #: the broadcast, which costs far more than the slate's CPU.
    slate_keep_for_rtmp: bool = True
    #: Seamless switching (per channel): the encoding every source is
    #: normalised to, and where the local UDP relay ports start.
    seamless_video_size: str = "1280x720"
    seamless_fps: int = 25
    relay_port_base: int = 21_000

    # ---- ffmpeg -----------------------------------------------------------
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"

    # ---- streaming defaults ----------------------------------------------
    default_rtmp_server: str = ""
    default_stream_mode: StreamMode = "copy"
    transcode_video_bitrate: str = "2500k"
    transcode_audio_bitrate: str = "128k"
    transcode_preset: str = "veryfast"
    #: Hardware encoder for transcode mode: 'auto' (use a detected GPU encoder,
    #: else software), 'off' (always software x264), or a specific encoder name
    #: (videotoolbox / nvenc / qsv / amf).
    transcode_hardware: str = "auto"

    # ---- buffered relay (MediaMTX) ---------------------------------------
    #: When on, each channel publishes into a local MediaMTX server that holds
    #: a delay buffer and keeps the viewer connection alive across reconnects.
    buffer_enabled: bool = False
    #: Target delay/buffer depth in seconds (viewers play this far behind live).
    buffer_seconds: int = 30
    #: Show a "reconnecting" slate on the viewer output during a long outage
    #: (only for channels that are actually down, to bound CPU).
    buffer_slate_enabled: bool = True
    #: Optional image for that slate. Empty = a plain dark screen (no font stack
    #: needed, works on every FFmpeg build).
    slate_path: str = ""
    #: Stop the slate after a source has been down this long, to stop burning
    #: CPU during a very long outage (viewers then see "unavailable" until it is
    #: back). 0 = keep the slate up for the whole outage.
    slate_max_seconds: int = 600
    #: Path to the MediaMTX binary. Empty = look in <root>/bin then on PATH.
    mediamtx_path: str = ""
    #: Ports MediaMTX listens on for publishing/ingest and viewer playback.
    mediamtx_rtmp_port: int = 1935
    mediamtx_hls_port: int = 8888
    mediamtx_api_port: int = 9997
    #: Host/IP viewers use to reach MediaMTX (e.g. a LAN IP or domain). Empty
    #: falls back to the app host, or 127.0.0.1 when that is 0.0.0.0.
    viewer_host: str = ""

    # ---- sources ----------------------------------------------------------
    source_cache_ttl_seconds: int = 1800
    source_user_agent: str = "Mozilla/5.0 (compatible; RestreamManager/1.0)"

    # ---- notifications ----------------------------------------------------
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ---- privacy ----------------------------------------------------------
    show_full_source_url: bool = False
    log_level: str = "INFO"

    # ---- interface --------------------------------------------------------
    #: Dashboard language: "th" or "en".
    ui_language: str = "th"

    # ---- paths ------------------------------------------------------------
    data_dir: Path = Field(default=BASE_DIR / "data")
    log_dir: Path = Field(default=BASE_DIR / "logs")

    # ------------------------------------------------------------------ #
    # validators
    # ------------------------------------------------------------------ #
    @field_validator("app_port")
    @classmethod
    def _valid_port(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError("APP_PORT must be between 1 and 65535")
        return v

    @field_validator("check_interval_seconds")
    @classmethod
    def _valid_check_interval(cls, v: int) -> int:
        if v < 30:
            raise ValueError("CHECK_INTERVAL_SECONDS must be >= 30")
        return v

    @field_validator("process_monitor_interval_seconds")
    @classmethod
    def _valid_monitor_interval(cls, v: int) -> int:
        if not 1 <= v <= 60:
            raise ValueError("PROCESS_MONITOR_INTERVAL_SECONDS must be between 1 and 60")
        return v

    @field_validator("failure_threshold")
    @classmethod
    def _valid_threshold(cls, v: int) -> int:
        if v < 1:
            raise ValueError("FAILURE_THRESHOLD must be >= 1")
        return v

    @field_validator("stall_timeout_seconds", "probe_timeout_seconds")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("value must be > 0")
        return v

    @field_validator("default_rtmp_server")
    @classmethod
    def _valid_rtmp(cls, v: str) -> str:
        v = v.strip()
        if v and not v.startswith(("rtmp://", "rtmps://")):
            raise ValueError("DEFAULT_RTMP_SERVER must start with rtmp:// or rtmps://")
        return v

    @field_validator("seamless_video_size")
    @classmethod
    def _valid_size(cls, v: str) -> str:
        text = (v or "").strip().lower()
        width, _, height = text.partition("x")
        if not (width.isdigit() and height.isdigit()):
            raise ValueError("SEAMLESS_VIDEO_SIZE must look like 1280x720")
        return text

    @field_validator("ui_language")
    @classmethod
    def _valid_language(cls, v: str) -> str:
        value = (v or "").strip().lower()
        if value not in ("th", "en"):
            raise ValueError("UI_LANGUAGE must be 'th' or 'en'")
        return value

    @field_validator("log_level")
    @classmethod
    def _valid_log_level(cls, v: str) -> str:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}")
        return upper

    # ------------------------------------------------------------------ #
    # derived values
    # ------------------------------------------------------------------ #
    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        return f"sqlite:///{(self.data_dir / 'restream.db').as_posix()}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ffmpeg_log_dir(self) -> Path:
        return self.log_dir / "ffmpeg"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def debug_dir(self) -> Path:
        return self.log_dir / "debug"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pid_dir(self) -> Path:
        return self.data_dir / "pids"

    def ensure_dirs(self) -> None:
        """Create every directory the application writes to."""
        for path in (
            self.data_dir,
            self.log_dir,
            self.ffmpeg_log_dir,
            self.debug_dir,
            self.pid_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings singleton."""
    return Settings()


def reload_settings() -> Settings:
    """Clear the cache and re-read the environment (used by tests)."""
    get_settings.cache_clear()
    return get_settings()
