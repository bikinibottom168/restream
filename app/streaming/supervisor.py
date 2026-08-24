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
      |
      |  still failing after failover_after_seconds (or too many short-lived
      |  starts in a row)
      v
    switch to the channel's next backup URL, and - only once the primary has
    probed clean for a long time and survived a shadow run - switch back
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
from app.streaming.failover import (
    FailoverPolicy,
    SourceCandidate,
    build_sources,
    is_output_failure,
    slow_retry_delay,
)
from app.streaming.ffmpeg import FFmpegManager, FFmpegProcess
from app.streaming.mediamtx import MediaMtxServer, choose_output
from app.streaming.orphan import PidRegistry
from app.streaming.probe import probe_stream
from app.streaming.relay import (
    SeamlessProfile,
    pick_relay_port,
    relay_input_url,
    relay_output_url,
)

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
        self._egress_last_start = 0.0
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._state = ChannelState.STOPPED
        self._backoff = BackoffPolicy(max_delay=settings.get_int("max_restart_delay_seconds"))
        self._circuit = RestartCircuit(
            window_seconds=settings.get_int("restart_window_seconds"),
            threshold=settings.get_int("restart_window_threshold"),
            throttled_delay=settings.get_int("unstable_restart_delay_seconds"),
        )

        self._failover = FailoverPolicy(
            failover_after_seconds=settings.get_int("failover_after_seconds"),
            failure_threshold=settings.get_int("failover_failure_threshold"),
            min_stable_seconds=settings.get_int("failover_min_stable_seconds"),
            failback_after_seconds=settings.get_int("failback_after_seconds"),
            penalty_max_seconds=settings.get_int("failback_penalty_max_seconds"),
        )

        self.channel_name = ""
        self.last_outcome: ResolutionOutcome | None = None
        self.last_error = ""
        self.started_monotonic = 0.0
        self.last_check_at = None
        self.consecutive_failures = 0
        self._force_refresh = False
        self._down_since: float | None = None

        # ---- source failover -----------------------------------------
        #: [primary, *backups], rebuilt from the channel row every cycle.
        self._sources: list[SourceCandidate] = [SourceCandidate(index=0, label="primary")]
        self._active_index = 0
        #: Set when a switch is decided; the main loop applies it instead of
        #: treating the restart as an outage.
        self._switch_to: int | None = None
        #: True only for a switch the failback gate approved. Wrapping round to
        #: the primary because every source is failing is not a recovery, and
        #: must not be announced - or penalised - as one.
        self._switch_is_failback = False
        #: Why the pending switch was decided, so the event log says something
        #: better than "ffmpeg exited" for a switch the operator asked for.
        self._switch_reason = ""
        #: Which sources have already been tried during the current outage, so
        #: "everything is down" is announced once rather than every lap.
        self._tried_while_down: set[int] = set()
        self._all_down_announced = False
        self._failed_back_at = 0.0
        self._failback_next_probe = 0.0
        self._shadow: FFmpegProcess | None = None
        self._shadow_deadline = 0.0
        #: Seamless mode: one publisher that outlives every source switch.
        self._seamless = False
        self._publisher: FFmpegProcess | None = None
        self._publisher_target = ""
        self._relay_port = 0
        self._restored_source = False
        #: The operator's real RTMP destination, if any. Kept here so the slate
        #: rules can tell "viewers only" from "a downstream service is watching".
        self._downstream_rtmp = ""

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
        await self._stop_shadow()
        await self._stop_publisher()
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
        self._failover.configure(
            failover_after_seconds=self._settings.get_int("failover_after_seconds"),
            failure_threshold=self._settings.get_int("failover_failure_threshold"),
            min_stable_seconds=self._settings.get_int("failover_min_stable_seconds"),
            failback_after_seconds=self._settings.get_int("failback_after_seconds"),
            penalty_max_seconds=self._settings.get_int("failback_penalty_max_seconds"),
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

                self._sources = build_sources(channel)
                if not self._restored_source:
                    # Come back on whatever source was working before the app
                    # restarted, instead of forcing an outage on a primary that
                    # was already known to be broken.
                    self._restored_source = True
                    stored = int(getattr(channel, "active_source_index", 0) or 0)
                    if 0 < stored < len(self._sources):
                        self._active_index = stored
                        logger.info(
                            "channel %s: resuming on %s",
                            self.channel_id,
                            self._sources[stored].name,
                        )
                self._seamless = self._seamless_wanted(channel)
                self._downstream_rtmp = channel.resolved_rtmp(
                    self._settings.get_str("default_rtmp_server")
                )
                if self._active_index >= len(self._sources):
                    # A backup was deleted while it was on air.
                    await self._set_active_index(0, persist=True)

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

                source = self._sources[self._active_index]

                await self._set_state(ChannelState.STARTING)
                outcome = await self._resolve_source(channel, source)
                if outcome is None:
                    return  # unsupported / fatal, already reported
                if not outcome.ok:
                    reason = outcome.error or "source unavailable"
                    self._record_source_failure(reason)
                    if await self._handle_failure(reason):
                        continue
                    return

                started = await self._launch(channel, outcome, output_url, buffered)
                if not started:
                    reason = self.last_error or "ffmpeg failed to start"
                    self._record_source_failure(reason)
                    if await self._handle_failure(reason):
                        continue
                    return

                reason = await self._monitor()
                ran_seconds = (
                    time.monotonic() - self.started_monotonic
                    if self.started_monotonic
                    else 0.0
                )
                await self._teardown(reason)

                if self._stop_event.is_set():
                    return

                # A switch the supervisor decided on (or the operator asked
                # for) is not an outage: apply it and start the new source
                # straight away, without backoff or a downtime record.
                if self._switch_to is not None:
                    await self._apply_switch(reason)
                    continue

                self._record_source_failure(reason, ran_seconds=ran_seconds)
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
            # The egress, slate and seamless publisher outlive individual
            # outages (so the downstream keeps getting the slate while
            # reconnecting), but must be cleaned up once the channel task
            # itself is done.
            self._egress_target = ""
            await self._stop_egress()
            await self._stop_slate()
            await self._stop_shadow()
            await self._stop_publisher()

    # ------------------------------------------------------------------ #
    # source selection
    # ------------------------------------------------------------------ #
    @property
    def active_source(self) -> SourceCandidate:
        if self._active_index < len(self._sources):
            return self._sources[self._active_index]
        return self._sources[0]

    @property
    def on_fallback(self) -> bool:
        return self._active_index != 0

    def _failover_enabled(self) -> bool:
        return self._settings.get_bool("failover_enabled") and len(self._sources) > 1

    def _auto_failback_enabled(self, channel: Any) -> bool:
        """Per-channel choice, falling back to the global default."""
        mode = (getattr(channel, "auto_failback", "") or "inherit").strip().lower()
        if mode == "on":
            return True
        if mode == "off":
            return False
        return self._settings.get_bool("auto_failback")

    def _seamless_wanted(self, channel: Any) -> bool:
        return bool(getattr(channel, "seamless_switch", False)) and len(self._sources) > 1

    def _profile(self) -> SeamlessProfile:
        """The single encoding every source of this channel is normalised to."""
        return SeamlessProfile(
            size=self._settings.get_str("seamless_video_size") or "1280x720",
            fps=self._settings.get_int("seamless_fps"),
            video_bitrate=self._settings.get_str("transcode_video_bitrate"),
            audio_bitrate=self._settings.get_str("transcode_audio_bitrate"),
            preset=self._settings.get_str("transcode_preset"),
            encoder=self._ffmpeg.encoder_for(
                self._settings.get_str("transcode_hardware")
            ),
        )

    def _failover_after(self, channel: Any) -> int:
        override = int(getattr(channel, "failover_after_seconds", 0) or 0)
        return override or self._settings.get_int("failover_after_seconds")

    def _failback_after(self, channel: Any) -> int:
        override = int(getattr(channel, "failback_after_seconds", 0) or 0)
        return override or self._settings.get_int("failback_after_seconds")

    async def _set_active_index(self, index: int, *, persist: bool = True) -> None:
        self._active_index = max(0, min(index, len(self._sources) - 1))
        self._failback_next_probe = 0.0
        if persist:
            await run_db(
                crud.update_channel,
                self.channel_id,
                active_source_index=self._active_index,
            )

    async def switch_source(self, index: int, reason: str = "operator request") -> bool:
        """Move this channel to *index* now (dashboard 'use primary' button).

        Works on a stopped channel too - the source list is read from the row
        rather than from whatever the last run happened to load.
        """
        channel = await self._load()
        if channel is not None:
            self._sources = build_sources(channel)
        if index < 0 or index >= len(self._sources):
            return False
        if index == self._active_index:
            return False
        self._switch_to = index
        self._switch_reason = reason
        if not self.is_running:
            await self._set_active_index(index)
            self._switch_to = None
            self._switch_reason = ""
            return True
        logger.info(
            "channel %s: switching to %s (%s)",
            self.channel_id,
            self._sources[index].name,
            reason,
        )
        # Ending the current process makes the main loop pick the switch up.
        process = self._process
        if process is not None:
            await process.stop(reason)
        self.wake()
        return True

    async def _apply_switch(self, reason: str) -> None:
        """Commit a decided switch and tell the operator about it."""
        target = self._switch_to or 0
        is_failback = self._switch_is_failback
        reason = self._switch_reason or reason
        self._switch_to = None
        self._switch_is_failback = False
        self._switch_reason = ""
        previous = self.active_source
        await self._set_active_index(target)
        current = self.active_source
        self._backoff.reset()
        self._failover.record_stable(target)
        if target == 0 and is_failback:
            self._failed_back_at = time.monotonic()
            await self._event(
                EventType.SOURCE_FAILBACK,
                f"back on the primary source ({reason})",
            )
            await self._notify_failover(current.name, reason, back_to_primary=True)
        else:
            await self._event(
                EventType.SOURCE_FAILOVER,
                f"switched from {previous.name} to {current.name}: {reason}",
                level="warning",
            )
            await self._notify_failover(current.name, reason, back_to_primary=False)
        # Cover the couple of seconds the new source needs to come up.
        await self._start_slate()

    def _record_source_failure(self, reason: str, ran_seconds: float = 0.0) -> None:
        """Count a failure against the current source - unless it was not one.

        A refused RTMP endpoint fails every source equally, so counting it
        would rotate through the whole list and report "all sources down" for
        a problem that lives entirely downstream.
        """
        if is_output_failure(reason):
            logger.info(
                "channel %s: destination failure, keeping %s (%s)",
                self.channel_id,
                self.active_source.name,
                reason[:120],
            )
            return
        self._failover.record_failure(self._active_index, ran_seconds=ran_seconds)
        self._tried_while_down.add(self._active_index)

    async def _resolve_source(
        self, channel: Any, source: SourceCandidate
    ) -> ResolutionOutcome | None:
        """Resolve whichever source is currently on air."""
        if source.is_primary:
            return await self._resolve(channel)

        outcome = await self._resolver.resolve_direct(channel, source)
        self.last_outcome = outcome
        if outcome.unsupported:
            # A protected backup is a configuration mistake in one URL, not a
            # reason to give up on the channel: fail it and let the loop move on.
            logger.warning(
                "channel %s: backup %s is protected media - skipping it",
                self.channel_id,
                source.name,
            )
            return ResolutionOutcome(ok=False, error=outcome.error)
        if not outcome.ok and self._down_since is None:
            await self._event(
                EventType.SOURCE_FAILED,
                f"{source.name}: {outcome.error}",
                level="warning",
            )
        return outcome

    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # "reconnecting" slate (only while buffered and down)
    # ------------------------------------------------------------------ #
    def _slate_wanted(self) -> bool:
        if not self._settings.get_bool("buffer_slate_enabled"):
            return False
        # On a seamless channel the slate feeds the relay, so it works with or
        # without the buffer - and it is what keeps the publisher (and the
        # downstream service behind it) fed while no source is available.
        if self._seamless and self._publisher is not None:
            return True
        return (
            self._settings.get_bool("buffer_enabled")
            and self._mediamtx is not None
            and self._mediamtx.running
        )

    def _slate_limit(self) -> int:
        """Seconds of outage after which the slate stops. 0 = never.

        A channel pushing to a downstream RTMP service is the exception: if the
        slate stops there the push starves, and services like YouTube end the
        broadcast within about a minute - which costs the operator far more
        than the slate's CPU ever would.
        """
        if self._downstream_rtmp and self._settings.get_bool("slate_keep_for_rtmp"):
            return 0
        return self._settings.get_int("slate_max_seconds")

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
        if not slate_within_limit(down_for, self._slate_limit()):
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
        if self._seamless and self._relay_port:
            slate_target = relay_output_url(self._relay_port)
            profile: SeamlessProfile | None = self._profile()
            slate_format = "mpegts"
        elif self._mediamtx is not None:
            slate_target = self._mediamtx.ingest_url(self.channel_id)
            profile = None
            slate_format = "flv"
        else:
            return
        try:
            self._slate = await self._ffmpeg.spawn_slate(
                self.channel_id,
                output_url=slate_target,
                image_path=self._settings.get_str("slate_path"),
                profile=profile,
                output_format=slate_format,
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
        self._egress_last_start = time.monotonic()
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
        # The slate and the real ingest publish to the same path (or the same
        # relay), so the slate must let go before FFmpeg takes over.
        await self._stop_slate()
        mode = channel.stream_mode or self._settings.get_str("default_stream_mode")

        # Seamless channels do not publish directly: they encode to the shared
        # profile and feed the local relay, and the publisher behind it - which
        # survives this process - is what talks to the real destination.
        profile: SeamlessProfile | None = None
        target = output_url
        spawn_format = "flv"
        if self._seamless:
            profile = self._profile()
            spawn_format = "mpegts"
            if not self._relay_port:
                try:
                    self._relay_port = pick_relay_port(
                        self._settings.get_int("relay_port_base")
                    )
                except OSError as exc:
                    self.last_error = f"seamless relay unavailable: {exc}"
                    await self._set_state(ChannelState.OFFLINE, error=self.last_error)
                    return False
            target = relay_output_url(self._relay_port)
            mode = "transcode"

        try:
            process = await self._ffmpeg.spawn(
                self.channel_id,
                stream=outcome.stream,
                output_url=target,
                mode=mode,
                probe=outcome.probe,
                video_bitrate=self._settings.get_str("transcode_video_bitrate"),
                audio_bitrate=self._settings.get_str("transcode_audio_bitrate"),
                preset=self._settings.get_str("transcode_preset"),
                hardware=self._settings.get_str("transcode_hardware"),
                profile=profile,
                output_format=spawn_format,
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

        # On a seamless channel the publisher is the process that proves the
        # destination accepted data, so it has to be up before we can confirm.
        confirm_on = process
        if self._seamless:
            if not await self._ensure_publisher(output_url):
                self.last_error = (
                    self.last_error or "the seamless publisher would not start"
                )
                await self._teardown(self.last_error)
                await self._set_state(ChannelState.OFFLINE, error=self.last_error)
                return False
            assert self._publisher is not None
            confirm_on = self._publisher

        # Is data actually reaching the output endpoint?
        if not await self._confirm_output(confirm_on):
            self.last_error = (
                confirm_on.last_error or "no data reached the output destination"
            )
            # The process may still be alive and holding the buffer path; stop
            # it so the retry (or the slate) can publish cleanly.
            await self._teardown(self.last_error)
            if self._seamless:
                # A publisher that never delivered anything is not worth
                # keeping: _ensure_publisher would otherwise reuse it forever
                # because it is technically still running.
                await self._stop_publisher()
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

    async def _ensure_publisher(self, output_url: str) -> bool:
        """Start the long-lived publisher, or keep the one already running.

        Keeping it is the entire point of seamless mode: the RTMP session it
        holds is what the downstream server and every connected player see, and
        it must not be interrupted just because the source behind it changed.
        """
        if (
            self._publisher is not None
            and self._publisher.running
            and self._publisher_target == output_url
        ):
            return True
        await self._stop_publisher()
        try:
            self._publisher = await self._ffmpeg.spawn_publisher(
                self.channel_id,
                input_url=relay_input_url(self._relay_port),
                output_url=output_url,
            )
        except (OSError, FileNotFoundError) as exc:
            self.last_error = f"could not start the seamless publisher: {exc}"
            self._publisher = None
            return False
        self._publisher_target = output_url
        if not await self._survives_startup(self._publisher):
            self.last_error = (
                self._publisher.last_error or "the seamless publisher exited at startup"
            )
            await self._stop_publisher()
            return False
        return True

    async def _stop_publisher(self) -> None:
        publisher = self._publisher
        self._publisher = None
        self._publisher_target = ""
        if publisher is not None:
            try:
                await publisher.stop("publisher stopped")
            except Exception:  # noqa: BLE001
                logger.debug("channel %s: publisher stop error", self.channel_id)

    async def _stop_shadow(self) -> None:
        shadow = self._shadow
        self._shadow = None
        self._shadow_deadline = 0.0
        if shadow is not None:
            try:
                await shadow.stop("shadow run finished")
            except Exception:  # noqa: BLE001
                logger.debug("channel %s: shadow stop error", self.channel_id)

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
        min_stable = max(1, self._settings.get_int("failover_min_stable_seconds"))
        counted_stable = False
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
            if (
                self._egress_target
                and (self._egress is None or not self._egress.running)
                and time.monotonic() - self._egress_last_start > 10.0
            ):
                # The 10s guard stops a tight restart loop when the final server
                # itself is unreachable (egress would die on start every time).
                logger.info(
                    "channel %s: egress relay is down - restarting it", self.channel_id
                )
                await self._start_egress(self._egress_target)

            # The seamless publisher holds the session the destination and the
            # players are attached to. Losing it defeats the whole mode, so it
            # is brought back immediately rather than on the next channel cycle.
            if self._seamless and self._publisher_target:
                target = self._publisher_target
                if self._publisher is None or not self._publisher.running:
                    logger.warning(
                        "channel %s: seamless publisher died - restarting it",
                        self.channel_id,
                    )
                    if not await self._ensure_publisher(target):
                        return "seamless publisher could not be restarted"

            # A source that has held this long is working, whatever it did
            # before; clearing the streak here is what keeps an old flap from
            # counting towards a failover hours later.
            if not counted_stable and process.uptime_seconds >= min_stable:
                counted_stable = True
                self._failover.record_stable(self._active_index)
                self._tried_while_down.clear()

            # ---- on a backup: has the primary earned its way back? ------
            if self._active_index != 0:
                switch_reason = await self._failback_tick()
                if switch_reason:
                    self._switch_to = 0
                    self._switch_is_failback = True
                    return switch_reason

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

    async def _failback_tick(self) -> str:
        """One step of "is the primary trustworthy again?".

        Deliberately non-blocking: the shadow run takes a minute or more, and
        the fast process monitor must keep watching the backup that is actually
        on air the whole time.  Each call does at most one probe or one check
        of a shadow already running.
        """
        now = time.monotonic()

        # A shadow run in progress is the final gate - nothing else matters.
        if self._shadow is not None:
            if not self._shadow.running:
                detail = self._shadow.last_error or "shadow run ended early"
                await self._stop_shadow()
                self._failover.reset_primary_health()
                logger.info(
                    "channel %s: primary failed its shadow run (%s)",
                    self.channel_id,
                    detail,
                )
                return ""
            if now >= self._shadow_deadline:
                flowing = self._shadow.metrics.out_time_us > 0
                await self._stop_shadow()
                if flowing:
                    return (
                        "primary probed clean for "
                        f"{int(self._failover.primary_healthy_for())}s and held a shadow run"
                    )
                self._failover.reset_primary_health()
                logger.info(
                    "channel %s: primary shadow run produced no output",
                    self.channel_id,
                )
            return ""

        if now < self._failback_next_probe:
            return ""
        self._failback_next_probe = now + max(
            15, self._settings.get_int("failback_probe_interval_seconds")
        )

        channel = await self._load()
        if channel is None or not self._auto_failback_enabled(channel):
            return ""
        self._failover.configure(failback_after_seconds=self._failback_after(channel))

        # A quiet probe: the primary is not being pulled right now, so a second
        # connection to it is safe and tells us something real.
        outcome = await self._resolver.resolve(channel, force=True, validate=True)
        self._failover.record_primary_probe(outcome.ok)
        if not outcome.ok:
            logger.debug(
                "channel %s: primary still unhealthy (%s)", self.channel_id, outcome.error
            )
            return ""
        if not self._failover.failback_ready():
            logger.debug(
                "channel %s: primary healthy for %ds of %ds needed",
                self.channel_id,
                int(self._failover.primary_healthy_for()),
                int(self._failover.required_healthy_seconds()),
            )
            return ""

        # Probing proves the first seconds parse, not that the stream lasts.
        shadow_seconds = self._settings.get_int("failback_shadow_seconds")
        if shadow_seconds <= 0 or outcome.stream is None:
            return f"primary probed clean for {int(self._failover.primary_healthy_for())}s"
        try:
            self._shadow = await self._ffmpeg.spawn_watch(
                self.channel_id, stream=outcome.stream
            )
        except (OSError, FileNotFoundError) as exc:
            logger.warning(
                "channel %s: could not start the shadow run: %s", self.channel_id, exc
            )
            self._shadow = None
            return ""
        self._shadow_deadline = now + shadow_seconds
        logger.info(
            "channel %s: primary looks healthy - shadow running it for %ds",
            self.channel_id,
            shadow_seconds,
        )
        return ""

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
        """Stop the source process only.

        The slate, the egress and (on a seamless channel) the publisher all
        deliberately outlive this: they are what keeps the output alive while
        a new source is brought up behind them.
        """
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

        self._sources = build_sources(channel)
        self._seamless = self._seamless_wanted(channel)
        self._downstream_rtmp = channel.resolved_rtmp(
            self._settings.get_str("default_rtmp_server")
        )
        self._failover.configure(failover_after_seconds=self._failover_after(channel))

        # A primary that breaks again right after we returned to it has not
        # really recovered; make the next failback earn a much longer proof.
        if self._active_index == 0 and self._failed_back_at:
            since = time.monotonic() - self._failed_back_at
            window = self._settings.get_int("failback_penalty_window_seconds")
            self._failed_back_at = 0.0
            if window > 0 and since <= window:
                required = self._failover.penalise()
                message = (
                    f"primary failed again {int(since)}s after switching back - "
                    f"the next return now needs {int(required)}s of clean probes"
                )
                logger.info("channel %s: %s", self.channel_id, message)
                await self._event(EventType.SOURCE_FAILOVER, message, level="warning")
            else:
                self._failover.forgive()

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

        # ---- try a different source before waiting on this one again ----
        switched = False
        if self._failover_enabled() and self._failover.should_leave(self._active_index):
            target = FailoverPolicy.next_index(self._active_index, len(self._sources))
            if target != self._active_index:
                detail = self._failover.leave_reason(self._active_index)
                self._switch_to = target
                await self._apply_switch(detail)
                switched = True
                # The new source deserves a prompt first attempt rather than
                # inheriting the delay the broken one had built up.
                self._backoff.reset()

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

        # Once every source has been tried and none of them worked, say so
        # once, then stop hammering origins that are plainly not coming back.
        if len(self._sources) > 1 and not switched:
            if (
                len(self._tried_while_down) >= len(self._sources)
                and not self._all_down_announced
            ):
                self._all_down_announced = True
                message = (
                    f"all {len(self._sources)} sources are unavailable: {reason}"
                )
                await self._event(EventType.ALL_SOURCES_DOWN, message, level="error")
                await self._notify_all_down(message)

        if self._down_since is not None and not switched:
            delay = slow_retry_delay(
                time.monotonic() - self._down_since,
                normal_delay=delay,
                slow_after_seconds=self._settings.get_int("all_down_slow_after_seconds"),
                slow_delay_seconds=self._settings.get_int("all_down_retry_delay_seconds"),
            )

        # The next attempt must ask the provider for a brand-new URL.
        self._force_refresh = True

        logger.info(
            "channel %s: retrying %s in %.0fs (%s)",
            self.channel_id,
            self.active_source.name,
            delay,
            reason,
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
        self._tried_while_down.clear()
        self._all_down_announced = False
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

    async def _notify_failover(
        self, source_name: str, detail: str, *, back_to_primary: bool
    ) -> None:
        if self._notifier is None:
            return
        try:
            await self._notifier.channel_failover(
                self.channel_id,
                self.channel_name,
                source_name,
                detail,
                back_to_primary=back_to_primary,
            )
        except Exception:  # noqa: BLE001
            logger.exception("telegram notification failed (failover)")

    async def _notify_all_down(self, detail: str) -> None:
        if self._notifier is None:
            return
        try:
            await self._notifier.channel_all_sources_down(
                self.channel_id, self.channel_name, detail
            )
        except Exception:  # noqa: BLE001
            logger.exception("telegram notification failed (all sources down)")

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
            "source_count": len(self._sources),
            "active_source_index": self._active_index,
            "active_source": self.active_source.name,
            "on_fallback": self.on_fallback,
            "seamless": self._seamless,
            "primary_healthy_seconds": round(self._failover.primary_healthy_for()),
            "failback_eta_seconds": (
                round(self._failover.failback_eta()) if self.on_fallback else None
            ),
            "failback_shadow_running": self._shadow is not None,
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
