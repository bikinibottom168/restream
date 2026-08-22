"""Per-channel supervisor.

One :class:`StreamSupervisor` per channel, each owning one ``asyncio.Task`` and
one FFmpeg process.  Channels are fully independent: a failure in one never
stops, restarts or re-resolves another.

    START
      |
      v
    resolve source (provider-agnostic)
      |
      v
    validate with ffprobe
      |
      v
    start FFmpeg  --> confirm output is flowing --> ONLINE
      |                                              |
      |                                    fast process monitor (5 s)
      |                                    deep source check (300 s)
      v                                              |
    RECONNECTING <---------- confirmed failure ------+
      |
      v
    refresh THIS channel's URL, restart FFmpeg, verify, ONLINE
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.core.state import ChannelState, can_transition
from app.core.settings_store import SettingsStore
from app.core.timeutil import utcnow
from app.database import crud
from app.database.db import run_db
from app.database.models import EventType
from app.providers.resolver import ResolutionOutcome, StreamResolver
from app.streaming.backoff import BackoffPolicy, RestartCircuit
from app.streaming.ffmpeg import FFmpegManager, FFmpegProcess
from app.streaming.mediamtx import MediaMtxServer, choose_output
from app.streaming.orphan import PidRegistry
from app.streaming.probe import probe_stream

logger = logging.getLogger(__name__)

#: Seconds to wait after spawning before deciding FFmpeg survived startup.
STARTUP_GRACE_SECONDS = 6.0
#: Seconds to wait for the first bytes to reach the RTMP endpoint.
OUTPUT_CONFIRM_SECONDS = 20.0


def slate_within_limit(down_for_seconds: float, max_seconds: int) -> bool:
    """Should the "reconnecting" slate still be running after this long down?

    ``max_seconds <= 0`` means keep it up for the whole outage; otherwise the
    slate is stopped once the outage passes the limit so a very long outage
    stops burning CPU on a continuous encode.
    """
    if max_seconds <= 0:
        return True
    return down_for_seconds <= max_seconds


class StreamSupervisor:
    """Own the lifecycle of exactly one channel."""

    def __init__(
        self,
        channel_id: int,
        *,
        resolver: StreamResolver,
        ffmpeg: FFmpegManager,
        settings: SettingsStore,
        notifier: Any,
        pids: PidRegistry,
        mediamtx: MediaMtxServer | None = None,
    ) -> None:
        self.channel_id = channel_id
        self._resolver = resolver
        self._ffmpeg = ffmpeg
        self._settings = settings
        self._notifier = notifier
        self._pids = pids
        self._mediamtx = mediamtx

        self._task: asyncio.Task[None] | None = None
        self._process: FFmpegProcess | None = None
        self._slate: FFmpegProcess | None = None
        self._egress: FFmpegProcess | None = None
        self._egress_target: str = ""
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._state = ChannelState.STOPPED
        self._backoff = BackoffPolicy(max_delay=settings.get_int("max_restart_delay_seconds"))
        self._circuit = RestartCircuit(
            window_seconds=settings.get_int("restart_window_seconds"),
            threshold=settings.get_int("restart_window_threshold"),
            throttled_delay=settings.get_int("unstable_restart_delay_seconds"),
        )

        self.channel_name = ""
        self.last_outcome: ResolutionOutcome | None = None
        self.last_error = ""
        self.started_monotonic = 0.0
        self.last_check_at = None
        self.consecutive_failures = 0
        self._force_refresh = False
        self._down_since: float | None = None

    # ------------------------------------------------------------------ #
    # public control surface
    # ------------------------------------------------------------------ #
    @property
    def state(self) -> ChannelState:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def process(self) -> FFmpegProcess | None:
        return self._process

    async def start(self) -> bool:
        """Start (or resume) the supervisor task. Returns False if already up."""
        if self.is_running:
            return False
        self._stop_event.clear()
        self._backoff.reset()
        self._task = asyncio.create_task(
            self._run(), name=f"supervisor-channel-{self.channel_id}"
        )
        return True

    async def stop(self, reason: str = "stopped by operator") -> None:
        """Stop the channel and its FFmpeg process, and wait for both."""
        self._stop_event.set()
        self._wake_event.set()
        self._egress_target = ""
        await self._stop_egress()
        await self._stop_slate()
        process = self._process
        if process is not None:
            await process.stop(reason)
        task = self._task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=30)
            except asyncio.TimeoutError:
                logger.warning("channel %s: supervisor did not exit, cancelling", self.channel_id)
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._task = None
        self._process = None
        self._pids.clear(self.channel_id)
        await self._set_state(ChannelState.STOPPED, ffmpeg_pid=None, started_at=None)
        await self._event(EventType.STREAM_STOPPED, reason)

    async def restart(self, reason: str = "restart requested") -> None:
        """Restart this channel only."""
        await self.stop(reason)
        await self._event(EventType.STREAM_RESTART, reason)
        await self.start()

    async def refresh_source(self) -> ResolutionOutcome:
        """Resolve a new URL for this channel and restart it if it changed."""
        channel = await self._load()
        if channel is None:
            return ResolutionOutcome(ok=False, error="channel no longer exists")
        outcome = await self._resolver.refresh(channel)
        self.last_outcome = outcome
        if outcome.ok:
            await self._event(
                EventType.SOURCE_REFRESHED,
                f"new source acquired ({outcome.stream.safe_url() if outcome.stream else ''})",
            )
            if self.is_running:
                await self.restart("source refreshed")
        else:
            await self._event(
                EventType.SOURCE_FAILED, outcome.error, level="error"
            )
        return outcome

    def wake(self) -> None:
        """Interrupt a backoff sleep (used by 'retry now' style actions)."""
        self._wake_event.set()

    def apply_settings(self) -> None:
        """Re-read tunables after the operator saved the settings page."""
        self._backoff = BackoffPolicy(
            max_delay=self._settings.get_int("max_restart_delay_seconds")
        )
        self._circuit.configure(
            window_seconds=self._settings.get_int("restart_window_seconds"),
            threshold=self._settings.get_int("restart_window_threshold"),
            throttled_delay=self._settings.get_int("unstable_restart_delay_seconds"),
        )

    # ------------------------------------------------------------------ #
    # main loop
    # ------------------------------------------------------------------ #
    async def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                channel = await self._load()
                if channel is None:
                    logger.info("channel %s disappeared - supervisor exiting", self.channel_id)
                    return
                self.channel_name = channel.name

                if not channel.enabled:
                    await self._set_state(ChannelState.DISABLED)
                    return

                output_url, buffered = choose_output(
                    self.channel_id,
                    channel.resolved_rtmp(self._settings.get_str("default_rtmp_server")),
                    buffer_enabled=self._settings.get_bool("buffer_enabled"),
                    server=self._mediamtx,
                )
                if not output_url:
                    await self._set_state(
                        ChannelState.CONFIG_REQUIRED,
                        error="no output configured (set an RTMP destination or turn on the buffer)",
                    )
                    logger.info(
                        "channel %s has no output destination - not starting FFmpeg",
                        self.channel_id,
                    )
                    return

                await self._set_state(ChannelState.STARTING)
                outcome = await self._resolve(channel)
                if outcome is None:
                    return  # unsupported / fatal, already reported
                if not outcome.ok:
                    if await self._handle_failure(outcome.error or "source unavailable"):
                        continue
                    return

                started = await self._launch(channel, outcome, output_url, buffered)
                if not started:
                    if await self._handle_failure(self.last_error or "ffmpeg failed to start"):
                        continue
                    return

                reason = await self._monitor()
                await self._teardown(reason)

                if self._stop_event.is_set():
                    return
                if not await self._handle_failure(reason):
                    return
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            await self._teardown("supervisor cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - never let a channel task die silently
            logger.exception("channel %s: supervisor crashed", self.channel_id)
            await self._set_state(ChannelState.ERROR, error=str(exc))
            await self._event(EventType.SYSTEM_ERROR, str(exc), level="error")
            await self._notify_system_error(f"supervisor for {self.channel_name}: {exc}")
        finally:
            # The egress and slate outlive individual outages (so the downstream
            # keeps getting the slate while reconnecting), but must be cleaned up
            # once the channel task itself is done.
            self._egress_target = ""
            await self._stop_egress()
            await self._stop_slate()

    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # "reconnecting" slate (only while buffered and down)
    # ------------------------------------------------------------------ #
    def _slate_wanted(self) -> bool:
        return (
            self._settings.get_bool("buffer_enabled")
            and self._settings.get_bool("buffer_slate_enabled")
            and self._mediamtx is not None
            and self._mediamtx.running
        )

    async def _update_slate(self) -> None:
        """Start, keep, or stop the slate based on how long we have been down.

        Called each retry cycle: it keeps the "reconnecting" screen up during a
        normal outage but takes it down once the outage passes
        ``slate_max_seconds`` so a source that stays off for hours does not keep
        an encode running the whole time.
        """
        if not self._slate_wanted():
            await self._stop_slate()
            return
        down_for = (
            time.monotonic() - self._down_since if self._down_since else 0.0
        )
        if not slate_within_limit(down_for, self._settings.get_int("slate_max_seconds")):
            if self._slate is not None:
                logger.info(
                    "channel %s: outage over slate limit - stopping the slate",
                    self.channel_id,
                )
                await self._stop_slate()
            return
        await self._start_slate()

    async def _start_slate(self) -> None:
        """Publish the slate into this channel's buffer path, if wanted."""
        if not self._slate_wanted():
            return
        # A slate that died (e.g. MediaMTX bounced) should be replaced, not kept.
        if self._slate is not None:
            if self._slate.running:
                return
            await self._stop_slate()
        assert self._mediamtx is not None
        try:
            self._slate = await self._ffmpeg.spawn_slate(
                self.channel_id,
                output_url=self._mediamtx.ingest_url(self.channel_id),
                image_path=self._settings.get_str("slate_path"),
            )
            logger.info("channel %s: slate up while reconnecting", self.channel_id)
        except Exception as exc:  # noqa: BLE001 - slate must never break recovery
            logger.warning("channel %s: could not start slate: %s", self.channel_id, exc)
            self._slate = None

    async def _stop_slate(self) -> None:
        """Stop the slate so the real ingest can take the path back."""
        slate = self._slate
        self._slate = None
        if slate is not None:
            try:
                await slate.stop("real source resumed")
            except Exception:  # noqa: BLE001
                logger.debug("channel %s: slate stop error", self.channel_id)

    # ------------------------------------------------------------------ #
    # egress: buffer -> final RTMP server (only when buffered + destination set)
    # ------------------------------------------------------------------ #
    async def _start_egress(self, final_rtmp: str) -> None:
        """Relay the buffered stream on to the operator's real RTMP server."""
        if not final_rtmp or self._mediamtx is None or not self._mediamtx.running:
            return
        if self._egress is not None and self._egress.running:
            return
        if self._egress is not None:
            await self._stop_egress()
        try:
            self._egress = await self._ffmpeg.spawn_egress(
                self.channel_id,
                input_url=self._mediamtx.ingest_url(self.channel_id),
                output_url=final_rtmp,
            )
            self._egress_target = final_rtmp
            logger.info(
                "channel %s: egress to final server started", self.channel_id
            )
        except Exception as exc:  # noqa: BLE001 - egress must not kill the channel
            logger.warning(
                "channel %s: could not start egress: %s", self.channel_id, exc
            )
            self._egress = None

    async def _stop_egress(self) -> None:
        egress = self._egress
        self._egress = None
        if egress is not None:
            try:
                await egress.stop("egress stopped")
            except Exception:  # noqa: BLE001
                logger.debug("channel %s: egress stop error", self.channel_id)

    async def _resolve(self, channel: Any) -> ResolutionOutcome | None:
        """Resolve the source. Returns ``None`` when the channel must stop."""
        outcome = await self._resolver.resolve(channel, force=self._force_refresh)
        self._force_refresh = False
        self.last_outcome = outcome

        if outcome.unsupported:
            await self._set_state(ChannelState.UNSUPPORTED, error=outcome.error)
            await self._event(
                EventType.SOURCE_UNSUPPORTED, outcome.error, level="error"
            )
            logger.warning(
                "channel %s marked UNSUPPORTED: %s", self.channel_id, outcome.error
            )
            return None

        # While a channel is already down, every retry fails the same way. Log
        # the first failure to the events feed and the operator, then keep the
        # repeats out of the database so a 15-hour outage does not write a
        # thousand identical rows - the retry itself is still logged to the file.
        already_down = self._down_since is not None
        if outcome.auth_error:
            if already_down:
                logger.debug(
                    "channel %s: still auth-failing while down: %s",
                    self.channel_id,
                    outcome.error,
                )
            else:
                await self._event(
                    EventType.PROVIDER_AUTH_FAILED, outcome.error, level="error"
                )
                await self._notify_auth_error(outcome.error)
        elif not outcome.ok:
            if already_down:
                logger.debug(
                    "channel %s: source still down: %s", self.channel_id, outcome.error
                )
            else:
                await self._event(
                    EventType.SOURCE_FAILED, outcome.error, level="warning"
                )
        return outcome

    async def _launch(
        self,
        channel: Any,
        outcome: ResolutionOutcome,
        output_url: str,
        buffered: bool = False,
    ) -> bool:
        """Spawn FFmpeg and confirm output actually starts flowing."""
        assert outcome.stream is not None
        # The slate and the real ingest publish to the same buffer path, so the
        # slate must let go before FFmpeg takes it over.
        await self._stop_slate()
        mode = channel.stream_mode or self._settings.get_str("default_stream_mode")
        try:
            process = await self._ffmpeg.spawn(
                self.channel_id,
                stream=outcome.stream,
                output_url=output_url,
                mode=mode,
                probe=outcome.probe,
                video_bitrate=self._settings.get_str("transcode_video_bitrate"),
                audio_bitrate=self._settings.get_str("transcode_audio_bitrate"),
                preset=self._settings.get_str("transcode_preset"),
            )
        except FileNotFoundError:
            message = (
                f"ffmpeg not found at {self._settings.get_str('ffmpeg_path')!r}; "
                "check the FFmpeg path on the settings page"
            )
            self.last_error = message
            await self._set_state(ChannelState.ERROR, error=message)
            await self._event(EventType.SYSTEM_ERROR, message, level="error")
            return False
        except OSError as exc:
            self.last_error = f"could not start ffmpeg: {exc}"
            await self._set_state(ChannelState.OFFLINE, error=self.last_error)
            return False

        self._process = process
        if process.pid is not None:
            self._pids.record(self.channel_id, process.pid, process.command)

        # Did it survive startup at all?
        if not await self._survives_startup(process):
            self.last_error = process.last_error or "ffmpeg exited immediately"
            await self._set_state(ChannelState.OFFLINE, error=self.last_error)
            await self._event(
                EventType.STREAM_DOWN,
                f"ffmpeg exited during startup: {self.last_error}",
                level="error",
            )
            return False

        # Is data actually reaching the output endpoint?
        if not await self._confirm_output(process):
            self.last_error = (
                process.last_error or "no data reached the output destination"
            )
            # The process may still be alive and holding the buffer path; stop
            # it so the retry (or the slate) can publish cleanly.
            await self._teardown(self.last_error)
            await self._set_state(ChannelState.OFFLINE, error=self.last_error)
            return False

        self.started_monotonic = time.monotonic()
        await self._set_state(
            ChannelState.ONLINE,
            ffmpeg_pid=process.pid,
            started_at=utcnow(),
            error="",
        )
        # When buffered, also relay the buffered stream on to the operator's
        # real RTMP server, if they configured one - otherwise the downstream
        # server would get nothing (a 404) once the buffer took over the output.
        final_rtmp = ""
        if buffered:
            final_rtmp = channel.resolved_rtmp(
                self._settings.get_str("default_rtmp_server")
            )
            if final_rtmp:
                await self._start_egress(final_rtmp)

        if buffered and final_rtmp:
            destination = "buffer server + final RTMP"
        elif buffered:
            destination = "buffer server"
        else:
            destination = f"{output_url.rsplit('/', 1)[0]}/***"
        await self._event(
            EventType.STREAM_STARTED,
            f"relaying to {destination} in {mode} mode",
        )
        await self._announce_recovery()
        self._backoff.reset()
        self.consecutive_failures = 0
        return True

    async def _survives_startup(self, process: FFmpegProcess) -> bool:
        deadline = time.monotonic() + STARTUP_GRACE_SECONDS
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                return False
            if not process.running:
                return False
            await asyncio.sleep(0.5)
        return process.running

    async def _confirm_output(self, process: FFmpegProcess) -> bool:
        """Wait until FFmpeg reports progress, i.e. the output is live.

        This is the RTMP verification step: FFmpeg only advances ``out_time``
        once the muxer has accepted data, so a refused or wrong RTMP endpoint
        shows up here instead of looking 'online' forever.
        """
        deadline = time.monotonic() + OUTPUT_CONFIRM_SECONDS
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                return False
            if not process.running:
                return False
            if process.metrics.out_time_us > 0 or process.metrics.total_size > 0:
                return True
            await asyncio.sleep(0.5)
        logger.warning(
            "channel %s: no output progress within %.0fs",
            self.channel_id,
            OUTPUT_CONFIRM_SECONDS,
        )
        return False

    # ------------------------------------------------------------------ #
    async def _monitor(self) -> str:
        """Watch the running stream. Returns the reason it needs restarting."""
        process = self._process
        if process is None:
            return "ffmpeg process missing"

        monitor_interval = max(1, self._settings.get_int("process_monitor_interval_seconds"))
        deep_interval = max(30, self._settings.get_int("check_interval_seconds"))
        stall_timeout = self._settings.get_int("stall_timeout_seconds")
        threshold = max(1, self._settings.get_int("failure_threshold"))
        next_deep_check = time.monotonic() + deep_interval
        self.consecutive_failures = 0

        while True:
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=monitor_interval)
                return "stopped by operator"
            except asyncio.TimeoutError:
                pass

            # ---- fast checks (every few seconds) -----------------------
            if not process.running:
                code = process.returncode
                detail = process.last_error or process.exit_reason or ""
                return f"ffmpeg exited (code {code}) {detail}".strip()

            # Keep the egress (buffer -> final server) alive on its own. Its
            # death must NOT restart the whole channel - just relaunch the relay
            # so the downstream server keeps getting the stream.
            if self._egress_target and (
                self._egress is None or not self._egress.running
            ):
                logger.info(
                    "channel %s: egress relay is down - restarting it", self.channel_id
                )
                await self._start_egress(self._egress_target)

            if process.is_stalled(stall_timeout):
                await self._event(
                    EventType.STREAM_STALLED,
                    f"no output progress for {stall_timeout}s",
                    level="warning",
                )
                return f"stream stalled (no progress for {stall_timeout}s)"

            stream = self.last_outcome.stream if self.last_outcome else None
            if stream is not None and stream.is_expired:
                return "source URL expired"

            # ---- deep source check (every check_interval) --------------
            if time.monotonic() >= next_deep_check:
                next_deep_check = time.monotonic() + deep_interval
                healthy = await self._deep_check(stream)
                if healthy:
                    self.consecutive_failures = 0
                    if self._state is ChannelState.DEGRADED:
                        await self._set_state(ChannelState.ONLINE, error="")
                else:
                    self.consecutive_failures += 1
                    logger.info(
                        "channel %s: source check failed (%d/%d)",
                        self.channel_id,
                        self.consecutive_failures,
                        threshold,
                    )
                    # Do NOT restart a channel whose OUTPUT is still flowing just
                    # because a *separate* re-probe failed. A second connection to
                    # a token/session-limited source (like an HLS portal) is often
                    # refused even while the live pull is perfectly fine - that
                    # false negative was a real cause of frequent drops. Only the
                    # stall detector (frozen output) or the process exiting ends
                    # the stream; the re-probe just downgrades to DEGRADED.
                    output_flowing = process.running and not process.is_stalled(
                        stall_timeout
                    )
                    if self.consecutive_failures >= threshold and not output_flowing:
                        return (
                            f"source unreachable after {self.consecutive_failures} "
                            "consecutive checks"
                        )
                    if output_flowing:
                        # cap it so the counter does not grow without bound
                        self.consecutive_failures = min(
                            self.consecutive_failures, threshold
                        )
                    await self._set_state(
                        ChannelState.DEGRADED,
                        error=self.last_error or "source re-check failing (output still live)",
                    )

    async def _deep_check(self, stream: Any) -> bool:
        """One ffprobe against the current source URL."""
        if stream is None:
            return True  # nothing to check against; the process monitor governs
        result = await probe_stream(
            stream.url,
            ffprobe_path=self._settings.get_str("ffprobe_path"),
            timeout=float(self._settings.get_int("probe_timeout_seconds")),
            headers=stream.request_headers() or None,
            user_agent=stream.user_agent,
        )
        self.last_check_at = utcnow()
        await run_db(crud.update_channel, self.channel_id, last_check_at=self.last_check_at)
        if not result.ok:
            self.last_error = result.error
        return result.ok

    # ------------------------------------------------------------------ #
    async def _teardown(self, reason: str) -> None:
        process = self._process
        if process is not None:
            await process.stop(reason)
            self._pids.clear(self.channel_id)
        self._process = None

    async def _handle_failure(self, reason: str) -> bool:
        """Record the outage, notify once, wait, and say whether to retry."""
        self.last_error = reason
        if self._stop_event.is_set():
            return False

        channel = await self._load()
        if channel is None or not channel.enabled:
            await self._set_state(ChannelState.STOPPED)
            return False

        await self._set_state(ChannelState.RECONNECTING, error=reason)
        await run_db(crud.increment_restart_count, self.channel_id)

        # Put a "reconnecting" screen on the viewer output while we work behind
        # the scenes. It comes down on its own once the outage passes the slate
        # limit, so a source that is off for hours stops burning CPU.
        await self._update_slate()

        if self._down_since is None:
            self._down_since = time.monotonic()
            await run_db(
                crud.open_downtime, self.channel_id, self.channel_name, reason
            )
            await self._event(EventType.STREAM_DOWN, reason, level="error")
            await self._notify_down(reason)
        else:
            await run_db(crud.bump_downtime_attempts, self.channel_id)

        # Restart-loop protection.
        self._circuit.record_restart()
        if self._circuit.tripped:
            delay = self._circuit.throttled_delay
            if self._circuit.should_notify():
                message = (
                    f"{self._circuit.restarts_in_window} restarts in "
                    f"{self._settings.get_int('restart_window_seconds') // 60} minutes"
                )
                await self._event(EventType.CHANNEL_UNSTABLE, message, level="warning")
                await self._notify_unstable(message)
        else:
            delay = self._backoff.next_delay()

        # The next attempt must ask the provider for a brand-new URL.
        self._force_refresh = True

        logger.info(
            "channel %s: retrying in %.0fs (%s)", self.channel_id, delay, reason
        )
        await self._sleep(delay)
        if self._stop_event.is_set():
            return False
        await self._set_state(ChannelState.RECONNECTING)
        return True

    async def _sleep(self, seconds: float) -> None:
        """Interruptible sleep - stop() or wake() cut it short."""
        self._wake_event.clear()
        waiters = [
            asyncio.create_task(self._stop_event.wait()),
            asyncio.create_task(self._wake_event.wait()),
        ]
        try:
            done, pending = await asyncio.wait(
                waiters, timeout=max(0.0, seconds), return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
        finally:
            for task in waiters:
                if not task.done():
                    task.cancel()

    # ------------------------------------------------------------------ #
    # state / persistence / notifications
    # ------------------------------------------------------------------ #
    async def _load(self) -> Any:
        return await run_db(crud.get_channel, self.channel_id)

    async def _set_state(
        self,
        state: ChannelState,
        *,
        error: str | None = None,
        ffmpeg_pid: int | None = ...,  # type: ignore[assignment]
        started_at: Any = ...,
    ) -> None:
        if not can_transition(self._state, state):
            logger.debug(
                "channel %s: unusual transition %s -> %s",
                self.channel_id,
                self._state.value,
                state.value,
            )
        previous = self._state
        self._state = state
        if error is not None:
            self.last_error = error
        kwargs: dict[str, Any] = {"last_error": error if error is not None else None}
        if ffmpeg_pid is not ...:
            kwargs["ffmpeg_pid"] = ffmpeg_pid
        if started_at is not ...:
            kwargs["started_at"] = started_at
        await run_db(crud.set_channel_status, self.channel_id, state, **kwargs)
        if previous is not state:
            logger.info(
                "channel %s: %s -> %s", self.channel_id, previous.value, state.value
            )

    async def _announce_recovery(self) -> None:
        if self._down_since is None:
            return
        downtime = time.monotonic() - self._down_since
        record = await run_db(crud.close_downtime, self.channel_id)
        attempts = (record.attempts + 1) if record is not None else 1
        self._down_since = None
        self._circuit.reset()
        await self._event(
            EventType.STREAM_RECOVERED,
            f"recovered after {int(downtime)}s and {attempts} attempt(s)",
        )
        await self._notify_recovered(downtime, attempts)

    async def _event(self, event_type: str, message: str, level: str = "info") -> None:
        try:
            await run_db(
                crud.add_event,
                event_type=event_type,
                message=message,
                channel_id=self.channel_id,
                channel_name=self.channel_name,
                level=level,
            )
        except Exception:  # noqa: BLE001 - logging must not break the stream
            logger.exception("could not record event for channel %s", self.channel_id)

    async def _notify_down(self, reason: str) -> None:
        if self._notifier is None:
            return
        try:
            await self._notifier.channel_down(self.channel_id, self.channel_name, reason)
        except Exception:  # noqa: BLE001
            logger.exception("telegram notification failed (down)")

    async def _notify_recovered(self, downtime: float, attempts: int) -> None:
        if self._notifier is None:
            return
        try:
            await self._notifier.channel_recovered(
                self.channel_id, self.channel_name, downtime, attempts
            )
        except Exception:  # noqa: BLE001
            logger.exception("telegram notification failed (recovered)")

    async def _notify_unstable(self, message: str) -> None:
        if self._notifier is None:
            return
        try:
            await self._notifier.channel_unstable(
                self.channel_id, self.channel_name, message
            )
        except Exception:  # noqa: BLE001
            logger.exception("telegram notification failed (unstable)")

    async def _notify_auth_error(self, error: str) -> None:
        if self._notifier is None:
            return
        try:
            await self._notifier.provider_auth_error(error)
        except Exception:  # noqa: BLE001
            logger.exception("telegram notification failed (auth)")

    async def _notify_system_error(self, error: str) -> None:
        if self._notifier is None:
            return
        try:
            await self._notifier.system_error(error)
        except Exception:  # noqa: BLE001
            logger.exception("telegram notification failed (system)")

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict[str, Any]:
        """Live runtime view for the dashboard and the API."""
        process = self._process
        stream = self.last_outcome.stream if self.last_outcome else None
        return {
            "channel_id": self.channel_id,
            "state": self._state.value,
            "supervisor_running": self.is_running,
            "uptime_seconds": (
                round(time.monotonic() - self.started_monotonic, 1)
                if self.started_monotonic and self._state.is_running
                else 0
            ),
            "ffmpeg": process.describe() if process else None,
            "consecutive_failures": self.consecutive_failures,
            "restarts_in_window": self._circuit.restarts_in_window,
            "unstable": self._circuit.tripped,
            "backoff_attempt": self._backoff.attempt,
            "last_error": self.last_error,
            "source_url": stream.safe_url() if stream else "",
            "source_expires_at": (
                stream.expires_at.isoformat() if stream and stream.expires_at else None
            ),
            "probe": (
                self.last_outcome.probe.as_dict()
                if self.last_outcome and self.last_outcome.probe
                else None
            ),
        }
