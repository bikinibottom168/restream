"""MediaMTX: the local buffer/relay server that keeps viewers connected.

Why this exists
---------------
When FFmpeg pulls a live ``m3u8`` and pushes it straight to the viewer, the
viewer's connection is slaved to the source: the moment the source stalls, the
output stalls and the player (VLC and friends) disconnects.  The fix is to put
a small media server *between* the ingest FFmpeg and the viewer:

    source m3u8 --(copy)--> FFmpeg ingest --> MediaMTX --> VLC / players

MediaMTX holds the viewer connection itself, independently of the ingest
process.  So the ingest FFmpeg can die and be restarted (re-login, new URL,
whatever the supervisor decides) without the viewer ever being disconnected -
they keep pulling from MediaMTX's HLS buffer, which is why a short source
dropout is invisible and a long one shows a "buffering" spinner instead of a
dead stream.

This module owns exactly one MediaMTX process for the whole application and
generates its configuration.  It does not transcode anything - the heavy work
stays in FFmpeg, and only when a channel opts into transcoding.  Everything
here is copy-friendly and cheap, which matters on a modest machine.

Nothing in this module reaches the network on import; the pure helpers
(:func:`channel_path`, :func:`render_config`, the URL builders) are what the
tests exercise, and the process lifecycle mirrors ``FFmpegProcess``.
"""

from __future__ import annotations

import asyncio
import logging
import math
import shutil
import socket
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform.startswith("win")

#: Project root: <root>/app/streaming/mediamtx.py -> parents[2] == <root>
BASE_DIR = Path(__file__).resolve().parents[2]

#: Nominal HLS segment length. Real segments cut on keyframes, so the effective
#: buffer is (segment_count x source keyframe interval); this is only the floor
#: used to turn a "buffer_seconds" target into a segment count.
NOMINAL_SEGMENT_SECONDS = 1

#: MediaMTX refuses very small segment counts; keep a sane band.
MIN_SEGMENTS = 7
MAX_SEGMENTS = 120

GRACEFUL_TIMEOUT = 5.0


# --------------------------------------------------------------------------- #
# pure helpers (unit-tested; no process, no network)
# --------------------------------------------------------------------------- #
def channel_path(channel_id: int) -> str:
    """Stable MediaMTX path name for a channel, e.g. ``ch82``.

    A channel keeps the same path for its whole life, so the viewer URL an
    operator copies never changes even as the ingest process restarts.
    """
    return f"ch{int(channel_id)}"


def segment_count_for(buffer_seconds: int) -> int:
    """Translate a delay target in seconds into an HLS segment count."""
    seconds = max(0, int(buffer_seconds))
    count = math.ceil(seconds / NOMINAL_SEGMENT_SECONDS) if seconds else MIN_SEGMENTS
    return max(MIN_SEGMENTS, min(MAX_SEGMENTS, count))


def _binary_name() -> str:
    return "mediamtx.exe" if IS_WINDOWS else "mediamtx"


def resolve_binary(configured_path: str = "") -> str | None:
    """Locate the MediaMTX binary: explicit path, then ``<root>/bin``, then PATH."""
    if configured_path:
        candidate = Path(configured_path).expanduser()
        if candidate.is_file():
            return str(candidate)
        # A configured path that doesn't exist is a real error, not a fallback.
        logger.warning("configured mediamtx_path %s does not exist", configured_path)
        return None
    bundled = BASE_DIR / "bin" / _binary_name()
    if bundled.is_file():
        return str(bundled)
    found = shutil.which("mediamtx")
    return found or None


def resolve_tool(configured: str, tool: str) -> str:
    """Resolve an ``ffmpeg``/``ffprobe`` path, preferring a bundled ``bin/`` copy.

    * An explicit path the operator set is always respected.
    * A bare command (``ffmpeg``) or empty value prefers ``<root>/bin`` - where
      the setup script drops downloaded binaries - and otherwise falls back to
      the bare command so a system install on ``PATH`` still works.
    """
    value = (configured or "").strip()
    if value and value.lower() not in (tool, f"{tool}.exe"):
        return value  # an explicit choice - honour it
    bundled = BASE_DIR / "bin" / (f"{tool}.exe" if IS_WINDOWS else tool)
    if bundled.is_file():
        return str(bundled)
    return value or tool


def viewer_host(configured_host: str, app_host: str) -> str:
    """Pick the host viewers use to reach MediaMTX.

    An explicit ``viewer_host`` wins.  Otherwise the app's bind host is used,
    except ``0.0.0.0`` / empty which are not reachable addresses - those fall
    back to ``127.0.0.1`` so at least local playback works.
    """
    host = (configured_host or "").strip()
    if host:
        return host
    app = (app_host or "").strip()
    if not app or app in {"0.0.0.0", "::", "*"}:
        return "127.0.0.1"
    return app


def ingest_url(channel_id: int, *, rtmp_port: int) -> str:
    """Where the ingest FFmpeg publishes (always loopback - never leaves the box)."""
    return f"rtmp://127.0.0.1:{int(rtmp_port)}/{channel_path(channel_id)}"


def viewer_urls(
    channel_id: int,
    *,
    host: str,
    rtmp_port: int,
    hls_port: int,
) -> dict[str, str]:
    """The URLs an operator hands to players for this channel."""
    path = channel_path(channel_id)
    return {
        "rtmp": f"rtmp://{host}:{int(rtmp_port)}/{path}",
        "hls": f"http://{host}:{int(hls_port)}/{path}/index.m3u8",
    }


def download_asset(version: str = "1.11.3") -> dict[str, str]:
    """Best-effort description of the release asset for this OS/arch.

    Used by setup docs and the UI to tell the operator exactly what to drop in
    ``<root>/bin`` - the app never bypasses the web-fetch rules to grab it.
    """
    import platform

    machine = platform.machine().lower()
    arch = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine, machine or "amd64")
    if IS_WINDOWS:
        os_tag, ext = "windows", "zip"
    elif sys.platform == "darwin":
        os_tag, ext = "darwin", "tar.gz"
    else:
        os_tag, ext = "linux", "tar.gz"
    name = f"mediamtx_v{version}_{os_tag}_{arch}.{ext}"
    base = "https://github.com/bluenviron/mediamtx/releases/download"
    return {
        "filename": name,
        "url": f"{base}/v{version}/{name}",
        "target": str(BASE_DIR / "bin" / _binary_name()),
    }


def choose_output(
    channel_id: int,
    resolved_rtmp: str,
    *,
    buffer_enabled: bool,
    server: "MediaMtxServer | None",
) -> tuple[str, bool]:
    """Decide where a channel's ingest FFmpeg should publish.

    Returns ``(output_url, buffered)``. When buffering is on and the local
    server is up, the ingest publishes into MediaMTX (and viewers watch from
    there); otherwise it falls back to the operator's configured RTMP
    destination exactly as before, so nothing breaks when the buffer is off or
    the server could not start.
    """
    if buffer_enabled and server is not None and server.running:
        return server.ingest_url(channel_id), True
    return resolved_rtmp, False


def render_config(
    *,
    rtmp_port: int,
    hls_port: int,
    api_port: int,
    buffer_seconds: int,
    log_level: str = "info",
) -> str:
    """Render a minimal, copy-friendly ``mediamtx.yml``.

    * RTMP is enabled for both ingest (loopback publish) and viewer playback.
    * HLS is the resilient viewer path: a deep segment window is the delay
      buffer that hides short source dropouts and keeps players connected
      across ingest restarts.
    * RTSP/WebRTC/SRT are off - fewer moving parts on a modest box.
    * ``all_others`` lets any channel path be published without pre-declaring
      each one, and ``runOnDemand`` is *not* used: the buffer must fill even
      before the first viewer arrives.
    """
    segments = segment_count_for(buffer_seconds)
    level = (log_level or "info").lower()
    if level not in {"error", "warn", "info", "debug"}:
        level = "info"
    return (
        "# generated by Restream Manager - do not edit by hand\n"
        f"logLevel: {level}\n"
        "logDestinations: [stdout]\n"
        "readTimeout: 30s\n"
        "writeTimeout: 30s\n"
        "\n"
        "api: yes\n"
        f"apiAddress: 127.0.0.1:{int(api_port)}\n"
        "\n"
        "rtmp: yes\n"
        f"rtmpAddress: :{int(rtmp_port)}\n"
        "rtmpEncryption: \"no\"\n"
        "\n"
        "hls: yes\n"
        f"hlsAddress: :{int(hls_port)}\n"
        "hlsAlwaysRemux: yes\n"
        "hlsVariant: mpegts\n"
        f"hlsSegmentCount: {segments}\n"
        f"hlsSegmentDuration: {NOMINAL_SEGMENT_SECONDS}s\n"
        "hlsAllowOrigin: \"*\"\n"
        "\n"
        "rtsp: no\n"
        "webrtc: no\n"
        "srt: no\n"
        "\n"
        "paths:\n"
        "  all_others:\n"
    )


# --------------------------------------------------------------------------- #
# process lifecycle
# --------------------------------------------------------------------------- #
class MediaMtxServer:
    """Owns the single MediaMTX process for the application."""

    def __init__(
        self,
        *,
        data_dir: Path,
        log_dir: Path,
        rtmp_port: int = 1935,
        hls_port: int = 8888,
        api_port: int = 9997,
        buffer_seconds: int = 30,
        binary_path: str = "",
        log_level: str = "info",
    ) -> None:
        self._data_dir = Path(data_dir)
        self._log_dir = Path(log_dir)
        self.rtmp_port = int(rtmp_port)
        self.hls_port = int(hls_port)
        self.api_port = int(api_port)
        self.buffer_seconds = int(buffer_seconds)
        self.binary_path = binary_path
        self.log_level = log_level

        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._log_lines: list[str] = []
        self.last_error = ""

    # ------------------------------------------------------------------ #
    @property
    def config_path(self) -> Path:
        return self._data_dir / "mediamtx.yml"

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    def resolve_binary(self) -> str | None:
        return resolve_binary(self.binary_path)

    def write_config(self) -> Path:
        """(Re)write mediamtx.yml from the current settings and return its path."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        text = render_config(
            rtmp_port=self.rtmp_port,
            hls_port=self.hls_port,
            api_port=self.api_port,
            buffer_seconds=self.buffer_seconds,
            log_level=self.log_level,
        )
        self.config_path.write_text(text, encoding="utf-8")
        return self.config_path

    # ------------------------------------------------------------------ #
    async def start(self) -> bool:
        """Start MediaMTX. Returns False (with ``last_error`` set) if it can't."""
        if self.running:
            return True
        binary = self.resolve_binary()
        if not binary:
            self.last_error = (
                "MediaMTX binary not found - put it in the project's bin/ folder "
                "or set mediamtx_path"
            )
            logger.error(self.last_error)
            return False

        self.write_config()
        try:
            self._process = await asyncio.create_subprocess_exec(
                binary,
                str(self.config_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self._data_dir),
            )
        except (FileNotFoundError, OSError) as exc:
            self.last_error = f"could not launch MediaMTX: {exc}"
            logger.error(self.last_error)
            self._process = None
            return False

        self._stderr_task = asyncio.create_task(
            self._drain_output(), name="mediamtx-log"
        )
        # Give it a moment to bind its ports (or fail fast on a port clash).
        ok = await self._wait_until_listening(timeout=8.0)
        if not ok:
            self.last_error = self.last_error or "MediaMTX did not start listening"
            logger.error("MediaMTX failed to come up: %s", self.last_error)
            await self.stop()
            return False
        logger.info(
            "MediaMTX up (pid %s) rtmp:%d hls:%d buffer:%ds",
            self.pid,
            self.rtmp_port,
            self.hls_port,
            self.buffer_seconds,
        )
        return True

    async def _wait_until_listening(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.running:
                return False
            if _port_open("127.0.0.1", self.api_port) or _port_open(
                "127.0.0.1", self.rtmp_port
            ):
                return True
            await asyncio.sleep(0.25)
        return self.running and (
            _port_open("127.0.0.1", self.api_port)
            or _port_open("127.0.0.1", self.rtmp_port)
        )

    async def _drain_output(self) -> None:
        assert self._process is not None
        stream = self._process.stdout
        if stream is None:
            return
        log_file = self._log_dir / "mediamtx.log"
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            log_file = None  # type: ignore[assignment]
        try:
            while True:
                raw = await stream.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                self._log_lines.append(line)
                if len(self._log_lines) > 200:
                    del self._log_lines[: len(self._log_lines) - 200]
                lowered = line.lower()
                if "err" in lowered or "fatal" in lowered:
                    self.last_error = line
                if log_file is not None:
                    try:
                        with log_file.open("a", encoding="utf-8") as handle:
                            handle.write(line + "\n")
                    except OSError:
                        pass
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise
        except Exception:  # noqa: BLE001
            logger.exception("MediaMTX log reader failed")

    async def stop(self) -> None:
        """Stop MediaMTX: terminate, then kill. Never leaves it running."""
        process = self._process
        if process is None:
            return
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:  # pragma: no cover
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=GRACEFUL_TIMEOUT)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:  # pragma: no cover
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:  # pragma: no cover
                    logger.error("MediaMTX pid %s could not be killed", process.pid)
        if self._stderr_task is not None and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._stderr_task = None
        logger.info("MediaMTX stopped")
        self._process = None

    async def apply_settings(
        self,
        *,
        rtmp_port: int,
        hls_port: int,
        api_port: int,
        buffer_seconds: int,
        binary_path: str,
    ) -> None:
        """Adopt new settings; restart the server if a live value changed."""
        changed = (
            self.rtmp_port != int(rtmp_port)
            or self.hls_port != int(hls_port)
            or self.api_port != int(api_port)
            or self.buffer_seconds != int(buffer_seconds)
            or self.binary_path != binary_path
        )
        self.rtmp_port = int(rtmp_port)
        self.hls_port = int(hls_port)
        self.api_port = int(api_port)
        self.buffer_seconds = int(buffer_seconds)
        self.binary_path = binary_path
        if changed and self.running:
            await self.stop()
            await self.start()

    # ------------------------------------------------------------------ #
    def ingest_url(self, channel_id: int) -> str:
        return ingest_url(channel_id, rtmp_port=self.rtmp_port)

    def viewer_urls(self, channel_id: int, host: str) -> dict[str, str]:
        return viewer_urls(
            channel_id, host=host, rtmp_port=self.rtmp_port, hls_port=self.hls_port
        )

    def recent_log(self, limit: int = 50) -> list[str]:
        return self._log_lines[-limit:]

    def describe(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "pid": self.pid,
            "rtmp_port": self.rtmp_port,
            "hls_port": self.hls_port,
            "api_port": self.api_port,
            "buffer_seconds": self.buffer_seconds,
            "binary": self.resolve_binary() or "",
            "last_error": self.last_error,
        }


def _port_open(host: str, port: int) -> bool:
    """True if something is accepting TCP connections on host:port."""
    try:
        with socket.create_connection((host, int(port)), timeout=0.5):
            return True
    except (OSError, ValueError):
        return False
