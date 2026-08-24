"""Source failover: a channel keeps working when its primary source does not.

    primary (provider / endpoint URL)
        |
        |  fails for `failover_after_seconds`, OR `failure_threshold` starts in
        |  a row that die before `min_stable_seconds` (the "flapping" case a
        |  plain timer never catches, because every flap resets the clock)
        v
    fallback 1 -> fallback 2 -> ... (round-robin while everything is down)
        |
        |  primary answers probes cleanly for `failback_after_seconds`, then
        |  survives a `shadow_seconds` shadow run
        v
    primary again

The rule is deliberately asymmetric: leaving a broken primary is urgent (nobody
is watching anything), coming back to it is not (the switch itself costs a
glitch, so it must be worth it).  A primary that dies again right after a
failback doubles the wait before the next one, which is what stops the
back-and-forth a fixed timer produces.

Everything here is pure - numbers in, decisions out - so the whole policy is
unit-testable without a stream, a process or a real clock.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

#: More than this many fallbacks per channel is a configuration mistake.
MAX_FALLBACK_SOURCES = 8

#: Schemes FFmpeg can actually take as an input URL.
_ALLOWED_SCHEMES = ("http://", "https://", "rtmp://", "rtmps://", "rtsp://", "srt://", "udp://")


@dataclass(slots=True)
class SourceCandidate:
    """One place a channel's video can come from.

    ``index`` 0 is always the primary, whose URL is produced by the provider at
    resolve time and therefore left empty here.  Fallbacks carry the media URL
    the operator typed, plus optional per-URL playback hints (a backup on a
    different host often needs a different Referer).
    """

    index: int
    url: str = ""
    label: str = ""
    referer: str = ""
    user_agent: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def is_primary(self) -> bool:
        return self.index == 0

    @property
    def name(self) -> str:
        if self.label:
            return self.label
        if self.is_primary:
            return "primary"
        return f"fallback {self.index}"

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"url": self.url}
        if self.label:
            data["label"] = self.label
        if self.referer:
            data["referer"] = self.referer
        if self.user_agent:
            data["user_agent"] = self.user_agent
        if self.headers:
            data["headers"] = dict(self.headers)
        return data


def _clean_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.startswith("#"):
        return ""
    if not text.lower().startswith(_ALLOWED_SCHEMES):
        return ""
    return text


def parse_fallback_sources(raw: Any) -> list[SourceCandidate]:
    """Read the stored fallback list in any of the shapes it can arrive in.

    Accepts a JSON array of strings, a JSON array of objects, or plain text
    with one URL per line - which is what the textarea on the channel form
    posts.  Anything unparseable yields an empty list rather than an error: a
    typo in a backup URL must never stop a channel from starting.
    """
    if not raw:
        return []
    entries: Iterable[Any]
    if isinstance(raw, (list, tuple)):
        entries = raw
    else:
        text = str(raw).strip()
        if not text:
            return []
        entries = []
        if text.startswith("["):
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, list):
                entries = decoded
        if not entries:
            entries = [line for line in text.replace(",", "\n").splitlines()]

    sources: list[SourceCandidate] = []
    seen: set[str] = set()
    for entry in entries:
        if isinstance(entry, dict):
            url = _clean_url(entry.get("url"))
            label = str(entry.get("label") or "").strip()[:64]
            referer = str(entry.get("referer") or "").strip()
            user_agent = str(entry.get("user_agent") or "").strip()
            headers = entry.get("headers") or {}
            headers = (
                {str(k): str(v) for k, v in headers.items()}
                if isinstance(headers, dict)
                else {}
            )
        else:
            url = _clean_url(entry)
            label = referer = user_agent = ""
            headers = {}
        if not url or url in seen:
            continue
        seen.add(url)
        sources.append(
            SourceCandidate(
                index=len(sources) + 1,
                url=url,
                label=label,
                referer=referer,
                user_agent=user_agent,
                headers=headers,
            )
        )
        if len(sources) >= MAX_FALLBACK_SOURCES:
            break
    return sources


def serialize_fallback_sources(sources: list[SourceCandidate]) -> str:
    """Canonical storage form: a JSON array, or ``''`` when there are none."""
    if not sources:
        return ""
    return json.dumps([source.as_dict() for source in sources])


def normalise_fallback_input(raw: Any) -> str:
    """Turn whatever the form posted into the canonical stored value."""
    return serialize_fallback_sources(parse_fallback_sources(raw))


def fallback_urls_text(raw: Any) -> str:
    """Render the stored value back for the textarea (one URL per line)."""
    return "\n".join(source.url for source in parse_fallback_sources(raw))


def build_sources(channel: Any) -> list[SourceCandidate]:
    """``[primary, *fallbacks]`` for a channel row."""
    primary = SourceCandidate(index=0, label="primary")
    return [primary, *parse_fallback_sources(getattr(channel, "fallback_urls", ""))]


#: Phrases FFmpeg uses when the *destination* refused the stream, rather than
#: the source failing to deliver one.
_OUTPUT_FAILURE_MARKERS = (
    "error opening output",
    "error closing file",
    "broken pipe",
    "could not write header",
    "rtmp server sent error",
)


def is_output_failure(message: str) -> bool:
    """Is this failure the destination rather than the source?

    Rotating to a backup URL cannot fix an RTMP endpoint that is refusing
    connections - it just churns through every source and reports a misleading
    "all sources down" while the actual problem sits downstream.  Those
    failures are retried in place, on whichever source is already on air.

    Deliberately narrow: "no output reached the destination" is *not* listed,
    because a source that connects and then sends nothing looks exactly the
    same from here, and that one really does deserve a failover.
    """
    lowered = (message or "").lower()
    return any(marker in lowered for marker in _OUTPUT_FAILURE_MARKERS)


def slow_retry_delay(
    down_for_seconds: float,
    *,
    normal_delay: float,
    slow_after_seconds: int,
    slow_delay_seconds: int,
) -> float:
    """Back off harder once *every* source has been down for a long time.

    A source that has been dead for a quarter of an hour will not come back
    sooner because we asked it nine times a minute; the slow mode keeps the
    channel retrying forever without hammering the origin or filling the log.
    """
    if slow_after_seconds > 0 and down_for_seconds >= slow_after_seconds:
        return max(float(normal_delay), float(slow_delay_seconds))
    return float(normal_delay)


class FailoverPolicy:
    """Decides when to leave a source and when it is safe to come back."""

    def __init__(
        self,
        *,
        failover_after_seconds: int = 120,
        failure_threshold: int = 3,
        min_stable_seconds: int = 60,
        failback_after_seconds: int = 600,
        penalty_max_seconds: int = 3_600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._failover_after = float(failover_after_seconds)
        self._threshold = max(1, int(failure_threshold))
        self._min_stable = float(min_stable_seconds)
        self._failback_after = float(failback_after_seconds)
        self._penalty_max = float(penalty_max_seconds)
        self._clock = clock

        self._streaks: dict[int, int] = {}
        self._failing_since: dict[int, float] = {}
        self._primary_healthy_since: float | None = None
        self._penalty = 1

    # ------------------------------------------------------------------ #
    def configure(
        self,
        *,
        failover_after_seconds: int | None = None,
        failure_threshold: int | None = None,
        min_stable_seconds: int | None = None,
        failback_after_seconds: int | None = None,
        penalty_max_seconds: int | None = None,
    ) -> None:
        """Apply changed settings without losing the current accounting."""
        if failover_after_seconds is not None:
            self._failover_after = float(failover_after_seconds)
        if failure_threshold is not None:
            self._threshold = max(1, int(failure_threshold))
        if min_stable_seconds is not None:
            self._min_stable = float(min_stable_seconds)
        if failback_after_seconds is not None:
            self._failback_after = float(failback_after_seconds)
        if penalty_max_seconds is not None:
            self._penalty_max = float(penalty_max_seconds)

    # ---- leaving a source -------------------------------------------- #
    def record_failure(self, index: int, *, ran_seconds: float = 0.0) -> None:
        """One failed attempt on *index*.

        ``ran_seconds`` is how long it stayed up before dying.  A source that
        held for longer than ``min_stable_seconds`` is treated as a fresh
        outage (streak 1, clock restarted); anything shorter is a flap and adds
        to the streak, so a channel that keeps bouncing still reaches the
        threshold instead of resetting its timer forever.
        """
        if ran_seconds >= self._min_stable:
            self._streaks[index] = 1
            self._failing_since[index] = self._clock()
        else:
            self._streaks[index] = self._streaks.get(index, 0) + 1
            self._failing_since.setdefault(index, self._clock())

    def record_stable(self, index: int) -> None:
        """*index* has been online long enough to count as working again."""
        self._streaks.pop(index, None)
        self._failing_since.pop(index, None)

    def failures(self, index: int) -> int:
        return self._streaks.get(index, 0)

    def failing_for(self, index: int) -> float:
        since = self._failing_since.get(index)
        return 0.0 if since is None else max(0.0, self._clock() - since)

    def should_leave(self, index: int) -> bool:
        """Has *index* failed badly enough to be worth swapping out?"""
        if self._streaks.get(index, 0) >= self._threshold:
            return True
        since = self._failing_since.get(index)
        if since is None:
            return False
        return (self._clock() - since) >= self._failover_after

    def leave_reason(self, index: int) -> str:
        streak = self._streaks.get(index, 0)
        if streak >= self._threshold:
            return f"{streak} failed attempts in a row"
        return f"down for {int(self.failing_for(index))}s"

    @staticmethod
    def next_index(current: int, total: int) -> int:
        """Round-robin to the next source, wrapping back to the primary."""
        if total <= 1:
            return 0
        return (current + 1) % total

    # ---- coming back to the primary ---------------------------------- #
    def record_primary_probe(self, ok: bool) -> None:
        """One health probe of the primary while a fallback is on air."""
        if ok:
            if self._primary_healthy_since is None:
                self._primary_healthy_since = self._clock()
        else:
            self._primary_healthy_since = None

    def primary_healthy_for(self) -> float:
        if self._primary_healthy_since is None:
            return 0.0
        return max(0.0, self._clock() - self._primary_healthy_since)

    def required_healthy_seconds(self) -> float:
        """How long the primary must stay clean before we trust it again."""
        return min(self._failback_after * self._penalty, self._penalty_max)

    def failback_ready(self) -> bool:
        if self._primary_healthy_since is None:
            return False
        return self.primary_healthy_for() >= self.required_healthy_seconds()

    def failback_eta(self) -> float:
        """Seconds still to wait, or ``-1`` when the primary is not healthy."""
        if self._primary_healthy_since is None:
            return -1.0
        return max(0.0, self.required_healthy_seconds() - self.primary_healthy_for())

    def penalise(self) -> float:
        """The primary broke again straight after a failback - wait longer."""
        if self.required_healthy_seconds() < self._penalty_max:
            self._penalty *= 2
        self._primary_healthy_since = None
        return self.required_healthy_seconds()

    def forgive(self) -> None:
        """The primary has proven itself - go back to the base wait."""
        self._penalty = 1

    @property
    def penalty(self) -> int:
        return self._penalty

    def reset_primary_health(self) -> None:
        self._primary_healthy_since = None
