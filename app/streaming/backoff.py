"""Retry pacing and restart-loop protection.

Backoff schedule (seconds), as specified::

    attempt 1 -> 3      attempt 2 -> 5      attempt 3 -> 10
    attempt 4 -> 20     attempt 5+ -> 30 (capped by max_delay)

A channel that keeps flapping is throttled by :class:`RestartCircuit` instead
of hammering the provider and the RTMP endpoint forever.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Callable, Sequence

#: The default ladder.
DEFAULT_DELAYS: tuple[int, ...] = (3, 5, 10, 20, 30)


class BackoffPolicy:
    """Turn an attempt number into a delay."""

    def __init__(
        self,
        delays: Sequence[int] = DEFAULT_DELAYS,
        max_delay: int = 30,
    ) -> None:
        if not delays:
            raise ValueError("delays must not be empty")
        self._delays = tuple(int(d) for d in delays)
        self._max_delay = int(max_delay)
        self._attempt = 0

    # ------------------------------------------------------------------ #
    @property
    def attempt(self) -> int:
        """How many failures have been recorded since the last reset."""
        return self._attempt

    @property
    def max_delay(self) -> int:
        return self._max_delay

    def delay_for(self, attempt: int) -> float:
        """Delay for a 1-based *attempt* number, capped at ``max_delay``."""
        if attempt <= 0:
            return 0.0
        index = min(attempt, len(self._delays)) - 1
        return float(min(self._delays[index], self._max_delay))

    def next_delay(self) -> float:
        """Record one more failure and return the delay to wait."""
        self._attempt += 1
        return self.delay_for(self._attempt)

    def reset(self) -> None:
        """Called when the channel comes back online."""
        self._attempt = 0


class RestartCircuit:
    """Slow down a channel that restarts too often.

    More than *threshold* restarts inside *window_seconds* trips the circuit:
    the supervisor then waits ``throttled_delay`` between attempts and the
    operator is told once that the channel is unstable.
    """

    def __init__(
        self,
        *,
        window_seconds: int = 600,
        threshold: int = 10,
        throttled_delay: int = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = float(window_seconds)
        self._threshold = int(threshold)
        self._throttled_delay = float(throttled_delay)
        self._clock = clock
        self._events: deque[float] = deque()
        self._tripped = False
        self._notified = False

    # ------------------------------------------------------------------ #
    def configure(
        self,
        *,
        window_seconds: int | None = None,
        threshold: int | None = None,
        throttled_delay: int | None = None,
    ) -> None:
        """Apply changed settings without losing the current window."""
        if window_seconds is not None:
            self._window = float(window_seconds)
        if threshold is not None:
            self._threshold = int(threshold)
        if throttled_delay is not None:
            self._throttled_delay = float(throttled_delay)

    def _prune(self) -> None:
        cutoff = self._clock() - self._window
        while self._events and self._events[0] < cutoff:
            self._events.popleft()

    def record_restart(self) -> bool:
        """Record a restart. Returns ``True`` when the circuit is now tripped."""
        self._events.append(self._clock())
        self._prune()
        was_tripped = self._tripped
        self._tripped = len(self._events) > self._threshold
        if self._tripped and not was_tripped:
            self._notified = False
        return self._tripped

    @property
    def tripped(self) -> bool:
        self._prune()
        self._tripped = len(self._events) > self._threshold
        return self._tripped

    @property
    def restarts_in_window(self) -> int:
        self._prune()
        return len(self._events)

    @property
    def throttled_delay(self) -> float:
        return self._throttled_delay

    def should_notify(self) -> bool:
        """True exactly once per trip, so Telegram is not spammed."""
        if self.tripped and not self._notified:
            self._notified = True
            return True
        return False

    def reset(self) -> None:
        self._events.clear()
        self._tripped = False
        self._notified = False
