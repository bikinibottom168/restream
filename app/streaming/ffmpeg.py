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
import os
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


#: Hardware H.264 encoders, best-first. The first one FFmpeg reports is used
#: for "auto" - each offloads encoding off the CPU so a modest box can
#: transcode several channels for stability.
HW_ENCODER_PREFERENCE: tuple[str, ...] = (
    "h264_videotoolbox",  # macOS (Apple Silicon + Intel)
    "h264_nvenc",         # NVIDIA
    "h264_qsv",           # Intel QuickSync
    "h264_amf",           # AMD
    "h264_v4l2m2m",       # some ARM SBCs
)


@dataclass(slots=True)
class FFmpegCapabilities:
    """What the installed FFmpeg build actually supports."""

    version: str = ""
    reconnect: bool = False
    reconnect_streamed: bool = False
    reconnect_delay_max: bool = False
    reconnect_on_network_error: bool = False
    hw_encoders: list[str] = field(default_factory=list)
    detected: bool = False

    def best_hw_encoder(self) -> str:
        """The preferred available hardware encoder, or '' if none."""
        for name in HW_ENCODER_PREFERENCE:
            if name in self.hw_encoders:
                return name
        return ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "reconnect": self.reconnect,
            "reconnect_streamed": self.reconnect_streamed,
            "reconnect_delay_max": self.reconnect_delay_max,
            "reconnect_on_network_error": self.reconnect_on_network_error,
            "hw_encoders": list(self.hw_encoders),
            "best_hw_encoder": self.best_hw_encoder(),
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
    caps.hw_encoders = await _detect_hw_encoders(ffmpeg_path, timeout=timeout)
    caps.detected = True
    logger.info("ffmpeg capabilities: %s", caps.as_dict())
    return caps


async def _detect_hw_encoders(ffmpeg_path: str, timeout: float = 15.0) -> list[str]:
    """Hardware H.264 encoders that actually WORK on this machine.

    A build can advertise ``h264_nvenc`` with no NVIDIA card present, which
    would make every transcode fail. So each advertised encoder is confirmed
    with a tiny real test-encode; only the ones that succeed are returned, and
    "auto" is therefore always safe.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            ffmpeg_path,
            "-hide_banner",
            "-encoders",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (FileNotFoundError, OSError, asyncio.TimeoutError) as exc:
        logger.debug("could not list ffmpeg encoders: %s", exc)
        return []
    text = stdout.decode("utf-8", errors="replace")
    advertised = [name for name in HW_ENCODER_PREFERENCE if name in text]

    working: list[str] = []
    for encoder in advertised:
        if await _hw_encoder_works(ffmpeg_path, encoder):
            working.append(encoder)
        else:
            logger.info("hardware encoder %s is advertised but not usable here", encoder)
    return working


async def _hw_encoder_works(ffmpeg_path: str, encoder: str, timeout: float = 12.0) -> bool:
    """True if a tiny test-encode with *encoder* succeeds (device really present)."""
    args = [
        ffmpeg_path, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=black:s=128x72:r=5",
        "-frames:v", "2", "-an", "-c:v", encoder, "-f", "null", "-",
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        code = await asyncio.wait_for(process.wait(), timeout=timeout)
        return code == 0
    except (FileNotFoundError, OSError):
        return False
    except asyncio.TimeoutError:
        try:
            process.kill()
            await process.wait()
        except (ProcessLookupError, OSError):  # pragma: no cover
            pass
        return False


def build_header_blob(headers: dict[str, str]) -> str:
    """Render headers in the CRLF-delimited form the ``-headers`` option wants."""
    return "".join(f"{key}: {value}\r\n" for key, value in headers.items() if key)


def resolve_video_encoder(hardware: str, caps: FFmpegCapabilities | None) -> str:
    """Pick the video encoder for transcode mode.

    ``hardware`` is the ``transcode_hardware`` setting:
      * ``off``  -> always software ``libx264``
      * ``auto`` -> the best hardware encoder FFmpeg advertises, else libx264
      * a name (``videotoolbox``/``nvenc``/``qsv``/``amf``) -> that encoder if
        available, otherwise fall back to software.
    """
    choice = (hardware or "auto").strip().lower()
    caps = caps or FFmpegCapabilities()
    if choice in ("off", "software", "libx264", "cpu"):
        return "libx264"
    if choice == "auto":
        return caps.best_hw_encoder() or "libx264"
    wanted = choice if choice.startswith("h264_") else f"h264_{choice}"
    return wanted if wanted in caps.hw_encoders else "libx264"


def _video_encoder_args(encoder: str, video_bitrate: str, preset: str) -> list[str]:
    """Encoder-specific output args for one H.264 encoder at a live bitrate."""
    bufsize = _double_bitrate(video_bitrate)
    common_rate = ["-b:v", video_bitrate, "-maxrate", video_bitrate, "-bufsize", bufsize]
    gop = ["-g", "50", "-keyint_min", "50"]
    if encoder == "h264_videotoolbox":
        return [
            "-c:v", "h264_videotoolbox", "-profile:v", "main",
            "-pix_fmt", "yuv420p", "-realtime", "1", *common_rate, *gop,
        ]
    if encoder == "h264_nvenc":
        return [
            "-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ll",
            "-profile:v", "main", "-pix_fmt", "yuv420p", "-rc", "cbr",
            *common_rate, *gop,
        ]
    if encoder == "h264_qsv":
        return [
            "-c:v", "h264_qsv", "-profile:v", "main",
            "-pix_fmt", "nv12", *common_rate, *gop,
        ]
    if encoder == "h264_amf":
        return [
            "-c:v", "h264_amf", "-profile:v", "main",
            "-usage", "lowlatency", *common_rate, *gop,
        ]
    if encoder == "h264_v4l2m2m":
        return ["-c:v", "h264_v4l2m2m", "-pix_fmt", "yuv420p", *common_rate, *gop]
    # software x264
    return [
        "-c:v", "libx264", "-preset", preset, "-profile:v", "main",
        "-pix_fmt", "yuv420p", *common_rate, *gop, "-sc_threshold", "0",
    ]


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
    video_encoder: str = "libx264",
    extra_input_args: list[str] | None = None,
    extra_output_args: list[str] | None = None,
    output_format: str = "flv",
    profile: Any = None,
    ts_offset: float | None = None,
) -> list[str]:
    """Assemble the FFmpeg argument vector for one relay.

    ``-c copy`` is the default because it costs almost no CPU; transcoding is
    opt-in per channel for sources whose codecs RTMP/FLV cannot carry.

    Passing a :class:`~app.streaming.relay.SeamlessProfile` overrides both the
    mode and the codec settings: every source of a seamless channel must encode
    identically, or the publisher copying its packets would break on a switch.
    ``output_format='mpegts'`` sends the result into the local UDP relay
    instead of straight to an RTMP endpoint.  ``ts_offset`` then anchors this
    feeder's output timeline to the channel's relay epoch, so the feeder that
    replaces it carries on from a later point instead of restarting at zero.
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

    # Tolerate what a *player* shrugs off but a strict remux would trip on:
    #   +genpts        - fill in missing presentation timestamps
    #   +discardcorrupt- drop corrupt packets (a segment glitch) instead of
    #                    erroring out, the way VLC skips a bad frame
    #   +igndts        - ignore broken decode timestamps from the source TS
    args += ["-fflags", "+genpts+discardcorrupt+igndts"]
    if extra_input_args:
        args += list(extra_input_args)
    args += ["-i", stream.url]

    # ---- output --------------------------------------------------------
    if profile is not None:
        args += profile.encode_args()
    elif mode == "transcode":
        args += _video_encoder_args(video_encoder or "libx264", video_bitrate, preset)
        args += [
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
        # Normalise timestamps so the muxer does not choke on the source's
        # discontinuities ("Non-monotonous DTS") - the #1 cause of copy drops.
        "-avoid_negative_ts", "make_zero",
        "-max_muxing_queue_size", "4096",
    ]
    if ts_offset is not None:
        args += ["-output_ts_offset", f"{max(0.0, float(ts_offset)):.3f}"]
    args += _container_args(output_format)
    if extra_output_args:
        args += list(extra_output_args)
    args.append(output_url)
    return args


def _container_args(output_format: str) -> list[str]:
    """Muxer flags for the container this process writes."""
    if output_format == "mpegts":
        return [
            # The publisher may attach after the feeder started, so repeat the
            # PAT/PMT tables instead of sending them once at the top.
            "-mpegts_flags", "+resend_headers",
            "-flush_packets", "1",
            "-f", "mpegts",
        ]
    return ["-flvflags", "no_duration_filesize", "-f", "flv"]


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
        "-fflags", "+genpts+discardcorrupt+igndts",
        "-i", input_url,
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        "-max_muxing_queue_size", "4096",
        "-flvflags", "no_duration_filesize",
        "-f", "flv",
        output_url,
    ]


def build_publisher_command(
    *,
    ffmpeg_path: str,
    input_url: str,
    output_url: str,
) -> list[str]:
    """The process that must never restart on a seamless channel.

    It reads the local MPEG-TS relay and copies it to the real destination, so
    the RTMP session survives every source switch behind it.

    It copies the timeline through untouched.  Continuity across a switch is
    the *feeder's* job: each one is given ``-output_ts_offset`` anchored to the
    channel's relay epoch (see :func:`build_command`), so successive feeders
    hand over an already-monotonic timeline.

    Restamping here instead - ``-use_wallclock_as_timestamps`` - also survives
    the switch, but it assigns arrival time to every packet, and UDP delivers
    in bursts: audio frames within one burst all land on nearly the same
    timestamp and then jump, which produces a constant stream of timestamp
    discontinuities and lets audio drift away from video.

    Note the deliberately thin ``-fflags``.  The tolerance flags the ingest
    uses (``+genpts``, ``+igndts``) exist for sources that are actually broken;
    the relay carries MPEG-TS this application produced itself, and asking
    FFmpeg to re-derive timestamps for it made the output markedly *worse* -
    measured over a source switch, 365 non-monotonic DTS against 22 with them
    off.
    """
    return [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel", "warning",
        "-nostats",
        "-progress", "pipe:1",
        "-fflags", "+discardcorrupt",
        "-i", input_url,
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        "-max_muxing_queue_size", "4096",
        "-flvflags", "no_duration_filesize",
        "-f", "flv",
        output_url,
    ]


def build_watch_command(
    *,
    ffmpeg_path: str,
    stream: ResolvedStream,
    caps: FFmpegCapabilities | None = None,
) -> list[str]:
    """Pull a source and throw the bytes away, to prove it actually sustains.

    Used as the last gate before returning to a recovered primary: an ffprobe
    that succeeds only says the first seconds parsed, while this keeps reading
    for as long as the caller watches it.  It copies rather than decodes, so
    the cost is bandwidth, not CPU.
    """
    caps = caps or FFmpegCapabilities()
    args: list[str] = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel", "warning",
        "-nostats",
        "-progress", "pipe:1",
    ]
    if stream.is_http:
        if caps.reconnect:
            args += ["-reconnect", "1"]
        if caps.reconnect_streamed:
            args += ["-reconnect_streamed", "1"]
        if caps.reconnect_delay_max:
            args += ["-reconnect_delay_max", "5"]
        if stream.user_agent:
            args += ["-user_agent", stream.user_agent]
        blob = build_header_blob(
            {
                key: value
                for key, value in stream.request_headers().items()
                if key.lower() != "user-agent"
            }
        )
        if blob:
            args += ["-headers", blob]
    args += [
        "-fflags", "+genpts+discardcorrupt+igndts",
        "-i", stream.url,
        "-c", "copy",
        # os.devnull already exists, and without -y FFmpeg stops to ask whether
        # to overwrite it - which on a pipe means exiting immediately.
        "-y",
        "-f", "mpegts",
        os.devnull,
    ]
    return args


def build_slate_command(
    *,
    ffmpeg_path: str,
    output_url: str,
    image_path: str = "",
    size: str = "1280x720",
    color: str = "0x111827",
    fps: int = 15,
    video_bitrate: str = "500k",
    profile: Any = None,
    output_format: str = "flv",
    ts_offset: float | None = None,
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

    On a seamless channel it feeds the local relay instead, and must then use
    the channel's :class:`~app.streaming.relay.SeamlessProfile` verbatim - the
    publisher copies whatever arrives, so a slate encoded differently from the
    real sources would break the stream it is supposed to be covering for.
    """
    if profile is not None:
        size = profile.size
        fps = int(profile.fps)
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
    # A silent stereo track keeps players and the muxer happy.
    args += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    if profile is not None:
        args += profile.encode_args()
    else:
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
        ]
    args += ["-max_muxing_queue_size", "1024"]
    if ts_offset is not None:
        args += ["-output_ts_offset", f"{max(0.0, float(ts_offset)):.3f}"]
    args += _container_args(output_format)
    args.append(output_url)
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

    def encoder_for(self, hardware: str) -> str:
        """The video encoder a 'auto'/'off'/named hardware setting resolves to."""
        return resolve_video_encoder(hardware, self.capabilities)

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
        hardware: str = "auto",
        profile: Any = None,
        output_format: str = "flv",
        ts_offset: float | None = None,
    ) -> FFmpegProcess:
        """Build the command and start FFmpeg for one channel."""
        video_encoder = resolve_video_encoder(hardware, self.capabilities)
        if profile is not None and not profile.encoder:
            profile.encoder = video_encoder
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
            video_encoder=video_encoder,
            profile=profile,
            output_format=output_format,
            ts_offset=ts_offset,
        )
        if profile is not None:
            logger.info(
                "channel %s: seamless feeder encoding %s",
                channel_id,
                profile.describe(),
            )
        elif mode == "transcode":
            logger.info(
                "channel %s: transcoding with %s", channel_id, video_encoder
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
        profile: Any = None,
        output_format: str = "flv",
        ts_offset: float | None = None,
    ) -> FFmpegProcess:
        """Start the looping "reconnecting" slate for a down channel."""
        command = build_slate_command(
            ffmpeg_path=self.ffmpeg_path,
            output_url=output_url,
            image_path=image_path,
            size=size,
            color=color,
            video_bitrate=video_bitrate,
            profile=profile,
            output_format=output_format,
            ts_offset=ts_offset,
        )
        logger.info("channel %s: starting slate -> %s", channel_id, safe_command(command))
        process = FFmpegProcess(channel_id, command, ffmpeg_log_dir=self.ffmpeg_log_dir)
        await process.start()
        return process

    async def spawn_publisher(
        self,
        channel_id: int,
        *,
        input_url: str,
        output_url: str,
    ) -> FFmpegProcess:
        """Start the long-lived publisher of a seamless channel."""
        command = build_publisher_command(
            ffmpeg_path=self.ffmpeg_path,
            input_url=input_url,
            output_url=output_url,
        )
        logger.info(
            "channel %s: starting seamless publisher -> %s",
            channel_id,
            safe_command(command),
        )
        process = FFmpegProcess(channel_id, command, ffmpeg_log_dir=self.ffmpeg_log_dir)
        await process.start()
        return process

    async def spawn_watch(
        self,
        channel_id: int,
        *,
        stream: ResolvedStream,
    ) -> FFmpegProcess:
        """Start a throwaway pull used to prove a source really sustains."""
        command = build_watch_command(
            ffmpeg_path=self.ffmpeg_path,
            stream=stream,
            caps=self.capabilities,
        )
        logger.debug(
            "channel %s: shadow run -> %s", channel_id, safe_command(command)
        )
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
