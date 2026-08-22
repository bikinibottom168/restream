"""ffprobe-based validation and binary detection.

A source is probed before FFmpeg is launched, so a dead URL produces a clear
dashboard error instead of an FFmpeg process that flaps forever.  Every probe
runs with a hard timeout and the process is killed if it overruns - a hung
ffprobe must never wedge the supervisor.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass, field
from typing import Any

from app.core.security import mask_url_token

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BinaryInfo:
    """Result of checking that ffmpeg/ffprobe exist and run."""

    name: str
    path: str
    available: bool = False
    version: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "available": self.available,
            "version": self.version,
            "error": self.error,
        }


@dataclass(slots=True)
class ProbeResult:
    """What ffprobe found at a source URL."""

    ok: bool = False
    error: str = ""
    streams: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: int = 0

    @property
    def video(self) -> dict[str, Any] | None:
        return next((s for s in self.streams if s.get("codec_type") == "video"), None)

    @property
    def audio(self) -> dict[str, Any] | None:
        return next((s for s in self.streams if s.get("codec_type") == "audio"), None)

    @property
    def video_codec(self) -> str:
        video = self.video
        return str(video.get("codec_name", "")) if video else ""

    @property
    def audio_codec(self) -> str:
        audio = self.audio
        return str(audio.get("codec_name", "")) if audio else ""

    @property
    def resolution(self) -> str:
        video = self.video
        if not video:
            return ""
        width, height = video.get("width"), video.get("height")
        return f"{width}x{height}" if width and height else ""

    def summary(self) -> str:
        if not self.ok:
            return self.error or "probe failed"
        parts = []
        if self.video_codec:
            parts.append(f"video={self.video_codec}")
        if self.resolution:
            parts.append(self.resolution)
        if self.audio_codec:
            parts.append(f"audio={self.audio_codec}")
        return ", ".join(parts) or "no streams reported"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error": self.error,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "resolution": self.resolution,
            "elapsed_ms": self.elapsed_ms,
            "summary": self.summary(),
        }


def _header_block(headers: dict[str, str] | None) -> list[str]:
    """Render extra HTTP headers in the CRLF form ffmpeg/ffprobe expect."""
    if not headers:
        return []
    blob = "".join(f"{key}: {value}\r\n" for key, value in headers.items())
    return ["-headers", blob]


async def _run(args: list[str], timeout: float) -> tuple[int, str, str]:
    """Run a command, always reaping the process even on timeout."""
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("command timed out after %.1fs: %s", timeout, args[0])
        try:
            process.kill()
        except ProcessLookupError:  # pragma: no cover - already gone
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:  # pragma: no cover - stuck in uninterruptible IO
            logger.error("could not reap %s after kill", args[0])
        raise
    return (
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


async def check_binary(path: str, name: str, timeout: float = 10.0) -> BinaryInfo:
    """Verify that ``<path> -version`` runs, and capture the version line."""
    from app.streaming.mediamtx import resolve_tool

    path = resolve_tool(path, name)
    info = BinaryInfo(name=name, path=path)
    resolved = shutil.which(path) if not any(sep in path for sep in ("/", "\\")) else path
    if resolved:
        info.path = resolved
    try:
        code, stdout, stderr = await _run([path, "-version"], timeout=timeout)
    except FileNotFoundError:
        info.error = (
            f"{name} was not found. Install it and either add it to PATH or set "
            f"{name.upper()}_PATH to the full path of the executable."
        )
        return info
    except asyncio.TimeoutError:
        info.error = f"{name} did not respond to -version within {timeout:.0f}s"
        return info
    except OSError as exc:
        info.error = f"could not execute {name}: {exc}"
        return info

    if code != 0:
        info.error = (stderr or stdout).strip()[:400] or f"{name} exited with code {code}"
        return info

    first_line = (stdout or "").splitlines()[0] if stdout else ""
    info.available = True
    info.version = first_line.strip()
    return info


async def probe_stream(
    url: str,
    *,
    ffprobe_path: str = "ffprobe",
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
    user_agent: str = "",
) -> ProbeResult:
    """Validate that *url* currently yields readable media.

    Returns a :class:`ProbeResult` - it never raises for a bad stream, only for
    a missing ffprobe binary.
    """
    if not url:
        return ProbeResult(ok=False, error="no source URL to probe")

    from app.streaming.mediamtx import resolve_tool

    ffprobe_path = resolve_tool(ffprobe_path, "ffprobe")
    args: list[str] = [
        ffprobe_path,
        "-hide_banner",
        "-v",
        "error",
        "-of",
        "json",
        "-show_entries",
        "stream=index,codec_type,codec_name,width,height,bit_rate,avg_frame_rate",
    ]
    if user_agent:
        args += ["-user_agent", user_agent]
    args += _header_block(headers)
    args += ["-i", url]

    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        code, stdout, stderr = await _run(args, timeout=timeout)
    except FileNotFoundError:
        return ProbeResult(
            ok=False,
            error=f"ffprobe not found at {ffprobe_path!r}; check the FFPROBE_PATH setting",
        )
    except asyncio.TimeoutError:
        return ProbeResult(
            ok=False,
            error=f"source did not respond within {timeout:.0f}s",
            elapsed_ms=int((loop.time() - started) * 1000),
        )
    except OSError as exc:
        return ProbeResult(ok=False, error=f"could not run ffprobe: {exc}")

    elapsed_ms = int((loop.time() - started) * 1000)

    if code != 0:
        message = (stderr or "").strip().splitlines()
        detail = message[-1] if message else f"ffprobe exited with code {code}"
        logger.info("probe failed for %s: %s", mask_url_token(url), detail)
        return ProbeResult(ok=False, error=detail[:400], elapsed_ms=elapsed_ms)

    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return ProbeResult(
            ok=False, error="ffprobe returned malformed JSON", elapsed_ms=elapsed_ms
        )

    streams = payload.get("streams") or []
    if not streams:
        return ProbeResult(
            ok=False,
            error="source contains no readable streams",
            elapsed_ms=elapsed_ms,
        )
    has_media = any(s.get("codec_type") in ("video", "audio") for s in streams)
    if not has_media:
        return ProbeResult(
            ok=False,
            error="source contains no audio or video track",
            streams=streams,
            elapsed_ms=elapsed_ms,
        )
    return ProbeResult(ok=True, streams=streams, elapsed_ms=elapsed_ms)
