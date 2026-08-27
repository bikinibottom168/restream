"""Detect - and evidence - an event loop that has stopped running.

Everything this application does lives on one asyncio event loop: the
dashboard, the watchdog, every channel supervisor.  If something blocks that
loop, nothing fails and nothing is logged; the process simply stops doing
anything, keeps its port open, and looks alive from the outside.  The operator
sees stale channel rows, new channels that are never started, and a dashboard
that hangs - and the only cure they have is closing the window and starting
again.

A monitor that lives *on* that loop cannot report this, because it is stopped
too.  So the beat is written from the loop and read from a plain daemon
thread, which keeps running whatever the loop is doing.

When the beat goes stale the detector writes ``logs/stall-<timestamp>.txt``
containing a stack for **every** thread.  That file names the exact line the
loop is stuck on, which turns "it froze again" into something fixable.  It can
also restart the process, but that is opt-in: a restart drops every stream, so
it is the operator's call whether a frozen relay is worse than a restarted one.
"""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

#: How often the loop records that it is still turning.
DEFAULT_BEAT_SECONDS = 5.0

#: How far behind the beat may fall before the loop counts as stalled. Well
#: clear of an ordinary slow tick - a channel start can hold the loop for a
#: second or two - but short enough to catch the freeze while it is happening.
DEFAULT_STALL_SECONDS = 60.0


class StallDetector:
    """Watch the event loop from a thread that the loop cannot block."""

    def __init__(
        self,
        *,
        log_dir: Path,
        beat_seconds: float = DEFAULT_BEAT_SECONDS,
        stall_seconds: float = DEFAULT_STALL_SECONDS,
        on_stall: Callable[[float, Path | None], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._log_dir = Path(log_dir)
        self._beat_seconds = max(1.0, float(beat_seconds))
        self._stall_seconds = max(self._beat_seconds * 2, float(stall_seconds))
        self._on_stall = on_stall
        self._clock = clock

        self._last_beat = clock()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._task: asyncio.Task[None] | None = None
        self._stalled_since: float | None = None
        self.stall_count = 0
        self.longest_stall = 0.0

    # ------------------------------------------------------------------ #
    @property
    def behind_seconds(self) -> float:
        with self._lock:
            return max(0.0, self._clock() - self._last_beat)

    @property
    def stalled(self) -> bool:
        return self.behind_seconds > self._stall_seconds

    def beat(self) -> None:
        """Record that the loop is still turning."""
        with self._lock:
            self._last_beat = self._clock()

    # ------------------------------------------------------------------ #
    async def _beat_forever(self) -> None:
        while not self._stop.is_set():
            self.beat()
            try:
                await asyncio.sleep(self._beat_seconds)
            except asyncio.CancelledError:
                raise

    def start(self) -> None:
        """Start the beat (on the loop) and the watcher (on a thread)."""
        if self._thread is not None:
            return
        self._stop.clear()
        self.beat()
        self._task = asyncio.create_task(self._beat_forever(), name="heartbeat")
        self._thread = threading.Thread(
            target=self._watch, name="stall-detector", daemon=True
        )
        self._thread.start()
        logger.info(
            "stall detector armed (beat %.0fs, alert after %.0fs)",
            self._beat_seconds,
            self._stall_seconds,
        )

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=2.0)

    # ------------------------------------------------------------------ #
    def _watch(self) -> None:
        """Runs on a thread; must not touch the loop or async primitives."""
        poll = min(self._beat_seconds, 5.0)
        while not self._stop.wait(poll):
            behind = self.behind_seconds
            if behind > self._stall_seconds:
                if self._stalled_since is None:
                    self._stalled_since = self._clock()
                    self._report(behind)
            elif self._stalled_since is not None:
                stuck_for = self._clock() - self._stalled_since
                self._stalled_since = None
                self.longest_stall = max(self.longest_stall, stuck_for)
                logger.error(
                    "event loop recovered after being stuck for %.0fs", stuck_for
                )

    def _report(self, behind: float) -> None:
        self.stall_count += 1
        dump = self._dump_stacks()
        logger.error(
            "EVENT LOOP STALLED: no heartbeat for %.0fs. The application is not "
            "processing anything. Thread stacks written to %s",
            behind,
            dump if dump else "(could not be written)",
        )
        if self._on_stall is not None:
            try:
                self._on_stall(behind, dump)
            except Exception:  # noqa: BLE001 - a handler must not kill the watcher
                logger.exception("stall handler failed")

    def _dump_stacks(self) -> Path | None:
        """Write every thread's stack, so the next freeze names its own cause."""
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            path = self._log_dir / f"stall-{time.strftime('%Y%m%d-%H%M%S')}.txt"
            with path.open("w", encoding="utf-8") as handle:
                handle.write(
                    "The event loop stopped running. Every thread's stack "
                    "follows; the one inside app/ is where it is stuck.\n\n"
                )
                faulthandler.dump_traceback(file=handle, all_threads=True)
            return path
        except Exception:  # noqa: BLE001 - evidence is best-effort
            logger.debug("could not write the stall dump", exc_info=True)
            return None


def restart_process() -> None:
    """Replace this process with a fresh copy of itself.

    Only reached when the operator has opted in.  FFmpeg children are left
    behind, which is deliberate and safe: startup reclaims processes it can
    prove it started, matching pid, creation time and argument vector.
    """
    logger.error("restarting the application after a stall (auto-restart is on)")
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, "-m", "app.main"])
