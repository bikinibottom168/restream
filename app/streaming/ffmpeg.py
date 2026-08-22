"""FFmpeg process management.

One OS process per channel, started with ``asyncio.create_subprocess_exec`` -
never ``shell=True``, so a URL containing shell metacharacters is harmless.

Two pipes are read continuously:

* ``stdout`` carries ``-progress pipe:1`` key/value output, which tells us the
  stream is genuinely moving (a live FFmpeg process is *not* proof of that)
* ``stderr`` goes to ``logs/ffmpeg/<channel_id>.log`` and into a small ring
  buffer so the dashboard can show the last error without opening the file

Shutdown is graceful first (``q`` on stdin), then ``terminate()``, then
``kill()`` - and it works the same way on Windows and macOS.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.logging import get_channel_logger
from app.core.security import mask_headers, mask_url_token, scrub
from app.providers.base import ResolvedStream
from app.streaming.probe import ProbeResult

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform.startswith("win")

#: How long to wait for each stage of the shutdown ladder.
GRACEFUL_QUIT_TIMEOUT = 5.0
TERMINATE_TIMEOUT = 5.0

#: Lines of FFmpeg stderr kept in memory per channel.
STDERR_RING = 200


@dataclass(slots=True)
class FFmpegCapabilities:
    """What the installed FFmpeg build actually supports."""

    version: str = ""
    reconnect: bool = False
    reconnect_streamed: bool = False
    reconnect_delay_max: bool = False
    reconnect_on_network_error: bool = False
    detected: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "reconnect": self.reconnect,
            "reconnect_streamed": self.reconnect_streamed,
            "reconnect_delay_max": self.reconnect_delay_max,
            "reconnect_on_network_error": self.reconnect_on_network_error,
        }


async def detect_capabilities(ffmpeg_path: str, timeout: float = 15.0) -> FFmpegCapabilities:
    """Ask FFmpeg which HTTP reconnect options it understands.

    Passing an unsupported option makes FFmpeg exit immediately, so the option
    list is built from what this binary reports rather than from assumption.
    """
    caps = FFmpegCapabilities()
    try:
        process = await asyncio.create_subprocess_exec(
            ffmpeg_path,
            "-hide_banner",
            "-h",
            "full",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (FileNotFoundError, OSError) as exc:
        logger.warning("could not run %s for capability detection: %s", ffmpeg_path, exc)
        return caps
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        logger.warning("ffmpeg capability detection timed out")
        return caps

    text = stdout.decode("utf-8", errors="replace")
    caps.reconnect = "-reconnect " in text or "reconnect  " in text
    caps.reconnect_streamed = "reconnect_streamed" in text
    caps.reconnect_delay_max = "reconnect_delay_max" in text
    caps.reconnect_on_network_error = "reconnect_on_network_error" in text
    caps.detected = True
    logger.info("ffmpeg capabilities: %s", caps.as_dict())
    return caps


def build_header_blob(headers: dict[str, str]) -> str:
    """Render headers in the CRLF-delimited form the ``-headers`` option wants."""
    return "".join(f"{key}: {value}\r\n" for key, value in headers.items() if key)


def build_command(
    *,
    ffmpeg_path: str,
    stream: ResolvedStream,
    output_url: str,
    mode: str = "copy",
    caps: FFmpegCapabilities | None = None,
    probe: ProbeResult | None = None,
    video_bitrate: str = "2500k",
    audio_bitrate: str = "128k",
    preset: str = "veryfast",
    extra_input_args: list[str] | None = None,
    extra_output_args: list[str] | None = None,
) -> list[str]:
    """Assemble the FFmpeg argument vector for one relay.

    ``-c copy`` is the default because it costs almost no CPU; transcoding is
    opt-in per channel for sources whose codecs RTMP/FLV cannot carry.
    """
    caps = caps or FFmpegCapabilities()
    args: list[str] = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostats",
        "-progress",
        "pipe:1",
    ]

    # ---- input ---------------------------------------------------------
    if stream.is_http:
        if caps.reconnect:
            args += ["-reconnect", "1"]
            # Keep retrying when a live source hits EOF mid-stream (a dropout),
            # instead of exiting. Same option family as -reconnect, so the
            # capability probe for -reconnect gates it too.
            args += ["-reconnect_at_eof", "1"]
        if caps.reconnect_streamed:
            args += ["-reconnect_streamed", "1"]
        if caps.reconnect_on_network_error:
            args += ["-reconnect_on_network_error", "1"]
        if caps.reconnect_delay_max:
            args += ["-reconnect_delay_max", "5"]

        user_agent = stream.user_agent
        if user_agent:
            args += ["-user_agent", user_agent]

        # Everything except User-Agent goes through -headers.
        header_map = {
            key: value
            for key, value in stream.request_headers().items()
            if key.lower() != "user-agent"
        }
        blob = build_header_blob(header_map)
        if blob:
            args += ["-headers", blob]

    args += ["-fflags", "+genpts"]
    if extra_input_args:
        args += list(extra_input_args)
    args += ["-i", stream.url]

    # ---- output --------------------------------------------------------
    if mode == "transcode":
        args += [
            "-c:v", "libx264",
            "-preset", preset,
            "-profile:v", "main",
            "-pix_fmt", "yuv420p",
            "-b:v", video_bitrate,
            "-maxrate", video_bitrate,
            "-bufsize", _double_bitrate(video_bitrate),
            "-g", "50",
            "-keyint_min", "50",
            "-sc_threshold", "0",
            "-c:a", "aac",
            "-b:a", audio_bitrate,
            "-ar", "44100",
            "-ac", "2",
        ]
    else:
        args += ["-c", "copy"]
        # Copying AAC out of MPEG-TS into FLV needs this bitstream filter.
        if probe is not None and probe.audio_codec.lower() == "aac":
            args += ["-bsf:a", "aac_adtstoasc"]

    args += [
        "-max_muxing_queue_size", "1024",
        "-flvflags", "no_duration_filesize",
        "-f", "flv",
    ]
    if extra_output_args:
        args += list(extra_output_args)
    args.append(output_url)
    return args


def build_egress_command(
    *,
    ffmpeg_path: str,
    input_url: str,
    output_url: str,
) -> list[str]:
    """A copy relay from the local buffer (MediaMTX) to the final RTMP server.

    When buffering is on the ingest publishes into MediaMTX; this second, cheap
    ``-c copy`` process reads the buffered stream back out and pushes it to the
    operator's real destination.  Because it reads from the buffer (not the
    flaky source), a short source dropout - or the slate during a longer one -
    keeps flowing to the downstream server instead of ending the push.
    """
    return [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel", "warning",
        "-nostats",
        "-progress", "pipe:1",
        "-fflags", "+genpts",
        "-i", input_url,
        "-c", "copy",
        "-max_muxing_queue_size", "1024",
        "-flvflags", "no_duration_filesize",
        "-f", "flv",
        output_url,
    ]


def build_slate_command(
    *,
    ffmpeg_path: str,
    output_url: str,
    image_path: str = "",
    size: str = "1280x720",
    color: str = "0x111827",
    fps: int = 15,
    video_bitrate: str = "500k",
) -> list[str]:
    """A tiny looping "reconnecting" feed to publish while a channel is down.

    Kept deliberately cheap - ``ultrafast`` + ``stillimage`` at a low frame rate
    and bitrate - because it only ever runs for channels that are *currently*
    down, so on a modest machine the cost is bounded to the few that are out at
    any moment.  With no image configured it falls back to a solid colour, which
    needs no font stack and works on every FFmpeg build.

    It publishes into the channel's MediaMTX path, so viewers already pulling
    that path see the slate instead of a frozen player; when the real ingest
    recovers it takes the path back over.
    """
    args: list[str] = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostats",
        "-progress",
        "pipe:1",
        "-re",
    ]
    if image_path:
        args += ["-loop", "1", "-framerate", str(fps), "-i", image_path]
    else:
        args += ["-f", "lavfi", "-i", f"color=c={color}:s={size}:r={fps}"]
    # A silent stereo track keeps players and the FLV muxer happy.
    args += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    args += [
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "stillimage",
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-g", str(fps * 2),
        "-b:v", video_bitrate,
        "-maxrate", video_bitrate,
        "-bufsize", _double_bitrate(video_bitrate),
        "-c:a", "aac",
        "-b:a", "64k",
        "-ar", "44100",
        "-ac", "2",
        "-max_muxing_queue_size", "1024",
        "-flvflags", "no_duration_filesize",
        "-f", "flv",
        output_url,
    ]
    return args


def _double_bitrate(value: str) -> str:
    """``2500k`` -> ``5000k`` for the encoder buffer size."""
    text = value.strip().lower()
    for suffix, factor in (("k", 1), ("m", 1)):
        if text.endswith(suffix):
            try:
                return f"{int(float(text[:-1]) * 2)}{suffix}"
            except ValueError:
                return value
    try:
        return str(int(float(text) * 2))
    except ValueError:
        return value


def safe_command(args: list[str]) -> str:
    """Render a command for logging with URLs, tokens and headers masked."""
    rendered: list[str] = []
    skip_next = False
    for index, item in enumerate(args):
        if skip_next:
            rendered.append("'***'")
            skip_next = False
            continue
        if item == "-headers":
            rendered.append(item)
            skip_next = True
            continue
        if item.lower().startswith(("http://", "https://", "rtmp://", "rtmps://", "srt://")):
            rendered.append(shlex.quote(mask_url_token(item)))
            continue
        rendered.append(shlex.quote(item))
    return scrub(" ".join(rendered))


@dataclass(slots=True)
class FFmpegMetrics:
    """Live numbers parsed from ``-progress pipe:1``."""

    out_time_us: int = 0
    out_time: str = "00:00:00.000000"
    bitrate: str = ""
    speed: str = ""
    frames: int = 0
    dropped_frames: int = 0
    total_size: int = 0
    progress: str = ""
    last_change_monotonic: float = field(default_factory=time.monotonic)

    def as_dict(self) -> dict[str, Any]:
        return {
            "out_time": self.out_time,
            "out_time_seconds": round(self.out_time_us / 1_000_000, 1),
            "bitrate": self.bitrate,
            "speed": self.speed,
            "frames": self.frames,
            "dropped_frames": self.dropped_frames,
            "total_size": self.total_size,
            "progress": self.progress,
            "seconds_since_progress": round(
                time.monotonic() - self.last_change_monotonic, 1
            ),
        }


class FFmpegProcess:
    """A single running FFmpeg relay."""

    def __init__(
        self,
        channel_id: int,
        command: list[str],
        *,
        ffmpeg_log_dir: Path,
    ) -> None:
        self.channel_id = channel_id
        self.command = command
        self._log_dir = ffmpeg_log_dir
        self._process: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_lines: list[str] = []
        self._started_monotonic: float = 0.0
        self._stopping = False
        self.metrics = FFmpegMetrics()
        self.exit_reason = ""

    # ------------------------------------------------------------------ #
    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    @property
    def returncode(self) -> int | None:
        return self._process.returncode if self._process else None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def uptime_seconds(self) -> float:
        if not self._started_monotonic:
            return 0.0
        return time.monotonic() - self._started_monotonic

    @property
    def last_error(self) -> str:
        for line in reversed(self._stderr_lines):
            lowered = line.lower()
            if any(word in lowered for word in ("error", "failed", "invalid", "denied", "refused")):
                return line
        return self._stderr_lines[-1] if self._stderr_lines else ""

    def recent_output(self, limit: int = 50) -> list[str]:
        return self._stderr_lines[-limit:]

    def is_stalled(self, timeout_seconds: float) -> bool:
        """True when FFmpeg is alive but its output clock stopped advancing."""
        if not self.running or timeout_seconds <= 0:
            return False
        # Give a freshly started process time to emit its first progress block.
        if self.uptime_seconds < max(timeout_seconds, 15.0):
            return False
        return (time.monotonic() - self.metrics.last_change_monotonic) > timeout_seconds

    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Spawn the process and begin draining both pipes."""
        channel_log = get_channel_logger(self.channel_id, self._log_dir)
        channel_log.info("=" * 70)
        channel_log.info("starting: %s", safe_command(self.command))

        self._process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._started_monotonic = time.monotonic()
        self.metrics = FFmpegMetrics()
        self._stopping = False
        self.exit_reason = ""
        self._stdout_task = asyncio.create_task(
            self._read_progress(), name=f"ffmpeg-progress-{self.channel_id}"
        )
        self._stderr_task = asyncio.create_task(
            self._read_stderr(channel_log), name=f"ffmpeg-stderr-{self.channel_id}"
        )
        logger.info("channel %s: ffmpeg started (pid %s)", self.channel_id, self.pid)

    async def wait(self) -> int:
        if self._process is None:
            return -1
        return await self._process.wait()

    # ------------------------------------------------------------------ #
    async def _read_progress(self) -> None:
        assert self._process is not None
        stream = self._process.stdout
        if stream is None:
            return
        try:
            while True:
                raw = await stream.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                self._apply_progress(key.strip(), value.strip())
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception:  # noqa: BLE001 - a parse error must not kill the channel
            logger.exception("channel %s: progress reader failed", self.channel_id)

    def _apply_progress(self, key: str, value: str) -> None:
        metrics = self.metrics
        if key == "out_time_us" or key == "out_time_ms":
            try:
                parsed = int(value)
            except ValueError:
                return
            # out_time_ms is documented as microseconds in most builds; both
            # keys are monotonic, which is all the stall check needs.
            if parsed > metrics.out_time_us:
                metrics.out_time_us = parsed
                metrics.last_change_monotonic = time.monotonic()
        elif key == "out_time":
            metrics.out_time = value
        elif key == "bitrate":
            metrics.bitrate = value.strip()
        elif key == "speed":
            metrics.speed = value.strip()
        elif key == "frame":
            try:
                metrics.frames = int(value)
            except ValueError:
                pass
        elif key == "drop_frames":
            try:
                metrics.dropped_frames = int(value)
            except ValueError:
                pass
        elif key == "total_size":
            try:
                size = int(value)
            except ValueError:
                return
            if size > metrics.total_size:
                metrics.total_size = size
                metrics.last_change_monotonic = time.monotonic()
        elif key == "progress":
            metrics.progress = value
            if value == "end":
                self.exit_reason = self.exit_reason or "ffmpeg reported end of stream"

    async def _read_stderr(self, channel_log: logging.Logger) -> None:
        assert self._process is not None
        stream = self._process.stderr
        if stream is None:
            return
        try:
            while True:
                raw = await stream.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                safe_line = scrub(mask_url_token(line))
                channel_log.info(safe_line)
                self._stderr_lines.append(safe_line)
                if len(self._stderr_lines) > STDERR_RING:
                    del self._stderr_lines[: len(self._stderr_lines) - STDERR_RING]
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception:  # noqa: BLE001
            logger.exception("channel %s: stderr reader failed", self.channel_id)

    # ------------------------------------------------------------------ #
    async def stop(self, reason: str = "stopped by operator") -> int | None:
        """Stop FFmpeg: ``q`` -> ``terminate`` -> ``kill``. Never leaves a zombie."""
        if self._process is None:
            return None
        self._stopping = True
        self.exit_reason = reason
        process = self._process

        if process.returncode is None:
            # 1) ask FFmpeg to finish writing and exit cleanly
            try:
                if process.stdin is not None and not process.stdin.is_closing():
                    process.stdin.write(b"q\n")
                    await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, RuntimeError, OSError):
                logger.debug("channel %s: could not send q to ffmpeg", self.channel_id)
            try:
                await asyncio.wait_for(process.wait(), timeout=GRACEFUL_QUIT_TIMEOUT)
            except asyncio.TimeoutError:
                logger.info(
                    "channel %s: ffmpeg ignored 'q', terminating", self.channel_id
                )

        # 2) SIGTERM / TerminateProcess
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:  # pragma: no cover - exited in between
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=TERMINATE_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning(
                    "channel %s: ffmpeg did not terminate, killing", self.channel_id
                )

        # 3) SIGKILL
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:  # pragma: no cover
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:  # pragma: no cover - unkillable process
                logger.error(
                    "channel %s: ffmpeg pid %s could not be killed",
                    self.channel_id,
                    process.pid,
                )

        await self._cleanup_pipes()
        logger.info(
            "channel %s: ffmpeg stopped (rc=%s, %s)",
            self.channel_id,
            process.returncode,
            reason,
        )
        return process.returncode

    async def _cleanup_pipes(self) -> None:
        for task in (self._stdout_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._stdout_task, self._stderr_task):
            if task is None:
                continue
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._stdout_task = None
        self._stderr_task = None
        if self._process is not None and self._process.stdin is not None:
            try:
                self._process.stdin.close()
            except (RuntimeError, OSError):  # pragma: no cover
                pass

    # ------------------------------------------------------------------ #
    def describe(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "running": self.running,
            "returncode": self.returncode,
            "uptime_seconds": round(self.uptime_seconds, 1),
            "metrics": self.metrics.as_dict(),
            "last_error": self.last_error,
            "exit_reason": self.exit_reason,
        }


class FFmpegManager:
    """Builds commands and spawns :class:`FFmpegProcess` instances."""

    def __init__(
        self,
        *,
        ffmpeg_path: str,
        ffmpeg_log_dir: Path,
    ) -> None:
        self.ffmpeg_path = ffmpeg_path
        self.ffmpeg_log_dir = ffmpeg_log_dir
        self.capabilities = FFmpegCapabilities()

    async def detect(self) -> FFmpegCapabilities:
        self.capabilities = await detect_capabilities(self.ffmpeg_path)
        return self.capabilities

    def set_path(self, ffmpeg_path: str) -> None:
        self.ffmpeg_path = ffmpeg_path

    async def spawn(
        self,
        channel_id: int,
        *,
        stream: ResolvedStream,
        output_url: str,
        mode: str = "copy",
        probe: ProbeResult | None = None,
        video_bitrate: str = "2500k",
        audio_bitrate: str = "128k",
        preset: str = "veryfast",
    ) -> FFmpegProcess:
        """Build the command and start FFmpeg for one channel."""
        command = build_command(
            ffmpeg_path=self.ffmpeg_path,
            stream=stream,
            output_url=output_url,
            mode=mode,
            caps=self.capabilities,
            probe=probe,
            video_bitrate=video_bitrate,
            audio_bitrate=audio_bitrate,
            preset=preset,
        )
        logger.debug(
            "channel %s command: %s (headers: %s)",
            channel_id,
            safe_command(command),
            mask_headers(stream.request_headers()),
        )
        process = FFmpegProcess(channel_id, command, ffmpeg_log_dir=self.ffmpeg_log_dir)
        await process.start()
        return process

    async def spawn_slate(
        self,
        channel_id: int,
        *,
        output_url: str,
        image_path: str = "",
        size: str = "1280x720",
        color: str = "0x111827",
        video_bitrate: str = "500k",
    ) -> FFmpegProcess:
        """Start the looping "reconnecting" slate for a down channel."""
        command = build_slate_command(
            ffmpeg_path=self.ffmpeg_path,
            output_url=output_url,
            image_path=image_path,
            size=size,
            color=color,
            video_bitrate=video_bitrate,
        )
        logger.info("channel %s: starting slate -> %s", channel_id, safe_command(command))
        process = FFmpegProcess(channel_id, command, ffmpeg_log_dir=self.ffmpeg_log_dir)
        await process.start()
        return process

    async def spawn_egress(
        self,
        channel_id: int,
        *,
        input_url: str,
        output_url: str,
    ) -> FFmpegProcess:
        """Start the copy relay from the buffer to the final RTMP destination."""
        command = build_egress_command(
            ffmpeg_path=self.ffmpeg_path,
            input_url=input_url,
            output_url=output_url,
        )
        logger.info(
            "channel %s: starting egress -> %s",
            channel_id,
            safe_command(command),
        )
        process = FFmpegProcess(channel_id, command, ffmpeg_log_dir=self.ffmpeg_log_dir)
        await process.start()
        return process
