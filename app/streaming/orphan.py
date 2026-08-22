"""Orphan FFmpeg recovery.

If the application crashes or the machine loses power, FFmpeg children can
survive.  On the next start they must be cleaned up - but only *ours*.

Identification is deliberately strict.  For every process we start we record
the pid, the process creation time and the exact argument vector.  A survivor
is adopted for termination only when all three still match, which no unrelated
FFmpeg on the machine can accidentally satisfy: a recycled pid will have a
different creation time, and another application's FFmpeg will have a
different command line.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

from app.core.security import mask_url_token

logger = logging.getLogger(__name__)

#: Tolerance when comparing process creation timestamps, in seconds.
CREATE_TIME_TOLERANCE = 1.5


@dataclass(slots=True)
class PidRecord:
    """What we remember about a process we spawned."""

    channel_id: int
    pid: int
    create_time: float
    cmdline: list[str]
    started_at: float
    owner_pid: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "pid": self.pid,
            "create_time": self.create_time,
            "cmdline": self.cmdline,
            "started_at": self.started_at,
            "owner_pid": self.owner_pid,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PidRecord | None":
        try:
            return cls(
                channel_id=int(data["channel_id"]),
                pid=int(data["pid"]),
                create_time=float(data["create_time"]),
                cmdline=[str(part) for part in data.get("cmdline", [])],
                started_at=float(data.get("started_at", 0.0)),
                owner_pid=int(data.get("owner_pid", 0)),
            )
        except (KeyError, TypeError, ValueError):
            return None


class PidRegistry:
    """On-disk record of the FFmpeg processes this application started."""

    def __init__(self, pid_dir: Path) -> None:
        self._dir = pid_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    def _path(self, channel_id: int) -> Path:
        return self._dir / f"channel-{channel_id}.json"

    def record(self, channel_id: int, pid: int, cmdline: list[str]) -> PidRecord | None:
        """Remember a freshly started process."""
        try:
            create_time = psutil.Process(pid).create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.Error) as exc:
            logger.debug("could not read create_time for pid %s: %s", pid, exc)
            return None
        record = PidRecord(
            channel_id=channel_id,
            pid=pid,
            create_time=create_time,
            cmdline=list(cmdline),
            started_at=time.time(),
            owner_pid=os.getpid(),
        )
        try:
            self._path(channel_id).write_text(
                json.dumps(record.as_dict()), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("could not write pid file for channel %s: %s", channel_id, exc)
        return record

    def clear(self, channel_id: int) -> None:
        path = self._path(channel_id)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - permission problem
            logger.debug("could not remove pid file %s: %s", path, exc)

    def load_all(self) -> list[PidRecord]:
        records: list[PidRecord] = []
        for path in sorted(self._dir.glob("channel-*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                logger.debug("ignoring unreadable pid file %s", path)
                continue
            record = PidRecord.from_dict(data)
            if record is not None:
                records.append(record)
        return records

    # ------------------------------------------------------------------ #
    def _matches(self, record: PidRecord) -> psutil.Process | None:
        """Return the live process only when it is provably the one we started."""
        try:
            process = psutil.Process(record.pid)
            create_time = process.create_time()
            cmdline = process.cmdline()
            name = (process.name() or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return None
        except psutil.Error as exc:  # pragma: no cover - platform quirk
            logger.debug("psutil error inspecting pid %s: %s", record.pid, exc)
            return None

        if abs(create_time - record.create_time) > CREATE_TIME_TOLERANCE:
            logger.debug(
                "pid %s was recycled (create_time differs) - not ours", record.pid
            )
            return None
        if "ffmpeg" not in name and not any("ffmpeg" in part.lower() for part in cmdline[:1]):
            logger.debug("pid %s is not an ffmpeg process - not ours", record.pid)
            return None
        if cmdline != record.cmdline:
            logger.debug("pid %s has a different command line - not ours", record.pid)
            return None
        return process

    def reclaim(self, *, timeout: float = 8.0) -> list[PidRecord]:
        """Terminate leftovers from a previous run. Returns what was killed."""
        killed: list[PidRecord] = []
        for record in self.load_all():
            if record.owner_pid == os.getpid():
                continue  # ours, in this very run
            process = self._matches(record)
            if process is None:
                self.clear(record.channel_id)
                continue
            masked = " ".join(mask_url_token(part) for part in record.cmdline[-1:])
            logger.warning(
                "reclaiming orphaned ffmpeg for channel %s (pid %s) %s",
                record.channel_id,
                record.pid,
                masked,
            )
            try:
                process.terminate()
                try:
                    process.wait(timeout=timeout)
                except psutil.TimeoutExpired:
                    logger.warning("orphan pid %s ignored terminate, killing", record.pid)
                    process.kill()
                    process.wait(timeout=timeout)
            except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
                logger.warning("could not stop orphan pid %s: %s", record.pid, exc)
            except psutil.Error as exc:  # pragma: no cover
                logger.warning("psutil failed stopping pid %s: %s", record.pid, exc)
            else:
                killed.append(record)
            self.clear(record.channel_id)
        if killed:
            logger.info("reclaimed %d orphaned ffmpeg process(es)", len(killed))
        return killed

    def clear_all(self) -> None:
        for path in self._dir.glob("channel-*.json"):
            try:
                path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover
                pass
