"""Application-level stream orchestration.

Owns one :class:`~app.streaming.supervisor.StreamSupervisor` per channel and
exposes the operations the API and the dashboard call: start, stop, restart,
refresh, test, sync, plus the status snapshot the UI polls.

The watchdog here is intentionally thin - all per-channel intelligence lives in
the supervisor.  This class only makes sure the right supervisors exist.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import psutil

from app.core.settings_store import SettingsStore
from app.core.state import ChannelState
from app.core.timeutil import utcnow
from app.database import crud
from app.database.db import run_db
from app.database.models import EventType
from app.providers.base import (
    ChannelInfo,
    DiscoveryNotSupported,
    ProviderAuthError,
    ProviderError,
)
from app.providers.manager import ProviderManager
from app.providers.resolver import ResolutionOutcome, StreamResolver
from app.streaming.ffmpeg import FFmpegManager
from app.streaming.mediamtx import (
    MediaMtxServer,
    choose_output,
    resolve_tool,
    viewer_host,
    viewer_urls,
)
from app.streaming.orphan import PidRegistry
from app.streaming.probe import BinaryInfo, check_binary
from app.streaming.supervisor import StreamSupervisor

logger = logging.getLogger(__name__)

#: How often the watchdog reconciles supervisors with the database.
WATCHDOG_INTERVAL_SECONDS = 15


class StreamManager:
    """Coordinates every channel supervisor."""

    def __init__(
        self,
        *,
        settings: SettingsStore,
        providers: ProviderManager,
        resolver: StreamResolver,
        notifier: Any,
        pid_dir: Any,
        ffmpeg_log_dir: Any,
        data_dir: Any = None,
        log_dir: Any = None,
    ) -> None:
        self._settings = settings
        self._providers = providers
        self._resolver = resolver
        self._notifier = notifier
        self._pids = PidRegistry(pid_dir)
        self._ffmpeg = FFmpegManager(
            ffmpeg_path=resolve_tool(settings.get_str("ffmpeg_path"), "ffmpeg"),
            ffmpeg_log_dir=ffmpeg_log_dir,
        )
        # The local buffer/relay server. Constructed always, started only when
        # buffering is enabled - so the direct-RTMP path is untouched by default.
        from pathlib import Path as _Path

        self._mediamtx = MediaMtxServer(
            data_dir=_Path(data_dir) if data_dir else _Path(pid_dir).parent,
            log_dir=_Path(log_dir) if log_dir else _Path(ffmpeg_log_dir).parent,
            rtmp_port=settings.get_int("mediamtx_rtmp_port"),
            hls_port=settings.get_int("mediamtx_hls_port"),
            api_port=settings.get_int("mediamtx_api_port"),
            buffer_seconds=settings.get_int("buffer_seconds"),
            binary_path=settings.get_str("mediamtx_path"),
            log_level=settings.get_str("log_level"),
        )
        self._supervisors: dict[int, StreamSupervisor] = {}
        self._watchdog_task: asyncio.Task[None] | None = None
        self._shutting_down = False
        self._started_at = time.monotonic()

        self.ffmpeg_info = BinaryInfo(name="ffmpeg", path=settings.get_str("ffmpeg_path"))
        self.ffprobe_info = BinaryInfo(name="ffprobe", path=settings.get_str("ffprobe_path"))
        settings.add_listener(self._on_setting_changed)

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # buffered relay (MediaMTX)
    # ------------------------------------------------------------------ #
    @property
    def buffer_enabled(self) -> bool:
        return self._settings.get_bool("buffer_enabled")

    @property
    def buffer_active(self) -> bool:
        """True when buffering is on AND the server is actually up."""
        return self.buffer_enabled and self._mediamtx.running

    def output_for(self, channel: Any) -> tuple[str, bool]:
        """``(output_url, buffered)`` for a channel, honouring buffer mode."""
        resolved = channel.resolved_rtmp(self._settings.get_str("default_rtmp_server"))
        return choose_output(
            channel.id,
            resolved,
            buffer_enabled=self.buffer_enabled,
            server=self._mediamtx,
        )

    def has_output(self, channel: Any) -> bool:
        url, _ = self.output_for(channel)
        return bool(url)

    def viewer_host(self) -> str:
        return viewer_host(
            self._settings.get_str("viewer_host"), self._settings.get_str("app_host")
        )

    def viewer_urls_for(self, channel_id: int) -> dict[str, str]:
        """Player URLs for a channel when buffering is on; empty dict otherwise."""
        if not self.buffer_enabled:
            return {}
        return viewer_urls(
            channel_id,
            host=self.viewer_host(),
            rtmp_port=self._settings.get_int("mediamtx_rtmp_port"),
            hls_port=self._settings.get_int("mediamtx_hls_port"),
        )

    def mediamtx_status(self) -> dict[str, Any]:
        info = self._mediamtx.describe()
        info["enabled"] = self.buffer_enabled
        info["binary_found"] = bool(self._mediamtx.resolve_binary())
        return info

    async def _ensure_mediamtx(self) -> None:
        """Start or stop MediaMTX to match the current buffer setting."""
        if self.buffer_enabled and not self._mediamtx.running:
            ok = await self._mediamtx.start()
            if ok:
                await run_db(
                    crud.add_event,
                    event_type=EventType.SYSTEM_STARTED,
                    message=f"buffer server started (delay {self._settings.get_int('buffer_seconds')}s)",
                )
            else:
                logger.error("buffer enabled but MediaMTX did not start: %s", self._mediamtx.last_error)
                await run_db(
                    crud.add_event,
                    event_type=EventType.SYSTEM_ERROR,
                    message=f"buffer server could not start: {self._mediamtx.last_error}",
                    level="error",
                )
        elif not self.buffer_enabled and self._mediamtx.running:
            await self._mediamtx.stop()

    async def start(self) -> None:
        """Startup sequence: binaries, orphans, state reset, auto-start, watchdog."""
        await self.check_binaries()
        await self._ensure_mediamtx()

        reclaimed = self._pids.reclaim()
        if reclaimed:
            await run_db(
                crud.add_event,
                event_type=EventType.SYSTEM_STARTED,
                message=f"reclaimed {len(reclaimed)} orphaned ffmpeg process(es)",
                level="warning",
            )

        cleared = await run_db(crud.reset_runtime_state)
        if cleared:
            logger.info("reset %d stale channel status rows", cleared)
        await run_db(crud.close_orphan_downtime)

        await self.autostart_channels()

        self._watchdog_task = asyncio.create_task(self._watchdog(), name="stream-watchdog")
        await run_db(
            crud.add_event,
            event_type=EventType.SYSTEM_STARTED,
            message="stream manager started",
        )

    async def shutdown(self) -> None:
        """Stop everything cleanly - no FFmpeg process may survive."""
        self._shutting_down = True
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._watchdog_task = None

        supervisors = list(self._supervisors.values())
        if supervisors:
            logger.info("stopping %d channel(s)", len(supervisors))
            await asyncio.gather(
                *(s.stop("application shutting down") for s in supervisors),
                return_exceptions=True,
            )
        self._supervisors.clear()
        self._pids.clear_all()
        try:
            await self._mediamtx.stop()
        except Exception:  # noqa: BLE001
            logger.exception("error stopping MediaMTX")
        await run_db(
            crud.add_event,
            event_type=EventType.SYSTEM_STOPPED,
            message="stream manager stopped",
        )
        logger.info("stream manager shut down")

    # ------------------------------------------------------------------ #
    def _on_setting_changed(self, key: str, value: Any) -> None:
        if key == "ffmpeg_path":
            resolved = resolve_tool(str(value), "ffmpeg")
            self._ffmpeg.set_path(resolved)
            self.ffmpeg_info.path = resolved
        if key in {
            "max_restart_delay_seconds",
            "restart_window_seconds",
            "restart_window_threshold",
            "unstable_restart_delay_seconds",
        }:
            for supervisor in self._supervisors.values():
                supervisor.apply_settings()
        if key in {
            "buffer_enabled",
            "buffer_seconds",
            "mediamtx_path",
            "mediamtx_rtmp_port",
            "mediamtx_hls_port",
            "mediamtx_api_port",
        }:
            # Reconcile the server off the event loop: schedule the coroutine.
            self._schedule_mediamtx_reconcile()

    def _schedule_mediamtx_reconcile(self) -> None:
        async def _reconcile() -> None:
            await self._mediamtx.apply_settings(
                rtmp_port=self._settings.get_int("mediamtx_rtmp_port"),
                hls_port=self._settings.get_int("mediamtx_hls_port"),
                api_port=self._settings.get_int("mediamtx_api_port"),
                buffer_seconds=self._settings.get_int("buffer_seconds"),
                binary_path=self._settings.get_str("mediamtx_path"),
            )
            await self._ensure_mediamtx()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(_reconcile())

    async def check_binaries(self) -> tuple[BinaryInfo, BinaryInfo]:
        """Verify ffmpeg and ffprobe, and detect FFmpeg's option support."""
        ffmpeg_path = resolve_tool(self._settings.get_str("ffmpeg_path"), "ffmpeg")
        ffprobe_path = resolve_tool(self._settings.get_str("ffprobe_path"), "ffprobe")
        self.ffmpeg_info = await check_binary(ffmpeg_path, "ffmpeg")
        self.ffprobe_info = await check_binary(ffprobe_path, "ffprobe")
        if self.ffmpeg_info.available:
            self._ffmpeg.set_path(ffmpeg_path)
            await self._ffmpeg.detect()
        else:
            logger.error("ffmpeg unavailable: %s", self.ffmpeg_info.error)
        if not self.ffprobe_info.available:
            logger.error("ffprobe unavailable: %s", self.ffprobe_info.error)
        return self.ffmpeg_info, self.ffprobe_info

    # ------------------------------------------------------------------ #
    # supervisors
    # ------------------------------------------------------------------ #
    def supervisor(self, channel_id: int) -> StreamSupervisor:
        supervisor = self._supervisors.get(channel_id)
        if supervisor is None:
            supervisor = StreamSupervisor(
                channel_id,
                resolver=self._resolver,
                ffmpeg=self._ffmpeg,
                settings=self._settings,
                notifier=self._notifier,
                pids=self._pids,
                mediamtx=self._mediamtx,
            )
            self._supervisors[channel_id] = supervisor
        return supervisor

    def peek(self, channel_id: int) -> StreamSupervisor | None:
        return self._supervisors.get(channel_id)

    def forget(self, channel_id: int) -> None:
        self._supervisors.pop(channel_id, None)
        self._resolver.forget(channel_id)
        self._pids.clear(channel_id)

    # ------------------------------------------------------------------ #
    # channel operations
    # ------------------------------------------------------------------ #
    async def start_channel(self, channel_id: int) -> dict[str, Any]:
        channel = await run_db(crud.get_channel, channel_id)
        if channel is None:
            return {"ok": False, "error": "channel not found"}
        if not channel.enabled:
            return {"ok": False, "error": "channel is disabled"}
        if not self.ffmpeg_info.available:
            return {"ok": False, "error": self.ffmpeg_info.error or "ffmpeg unavailable"}
        if not self.has_output(channel):
            await run_db(
                crud.set_channel_status, channel_id, ChannelState.CONFIG_REQUIRED
            )
            return {"ok": False, "error": "no output configured (set an RTMP destination or turn on the buffer)"}
        supervisor = self.supervisor(channel_id)
        started = await supervisor.start()
        return {"ok": True, "already_running": not started}

    async def stop_channel(self, channel_id: int, reason: str = "stopped by operator") -> dict[str, Any]:
        supervisor = self.peek(channel_id)
        if supervisor is None:
            await run_db(crud.set_channel_status, channel_id, ChannelState.STOPPED)
            return {"ok": True, "already_stopped": True}
        await supervisor.stop(reason)
        return {"ok": True}

    async def restart_channel(self, channel_id: int) -> dict[str, Any]:
        supervisor = self.supervisor(channel_id)
        await supervisor.restart("restart requested from dashboard")
        return {"ok": True}

    async def refresh_channel(self, channel_id: int) -> dict[str, Any]:
        """Resolve a new URL for one channel; restart only that channel."""
        supervisor = self.supervisor(channel_id)
        outcome = await supervisor.refresh_source()
        return {"ok": outcome.ok, "error": outcome.error, "result": outcome.as_dict()}

    async def test_source(self, channel_id: int) -> ResolutionOutcome:
        """Resolve + ffprobe without touching FFmpeg or the RTMP output."""
        channel = await run_db(crud.get_channel, channel_id)
        if channel is None:
            return ResolutionOutcome(ok=False, error="channel not found")
        return await self._resolver.test(channel)

    async def set_enabled(self, channel_id: int, enabled: bool) -> dict[str, Any]:
        await run_db(crud.update_channel, channel_id, enabled=enabled)
        if not enabled:
            await self.stop_channel(channel_id, "channel disabled")
            await run_db(crud.set_channel_status, channel_id, ChannelState.DISABLED)
        else:
            await run_db(crud.set_channel_status, channel_id, ChannelState.STOPPED)
        return {"ok": True}

    # ------------------------------------------------------------------ #
    # bulk operations
    # ------------------------------------------------------------------ #
    async def autostart_channels(self) -> int:
        """Start channels that are enabled, auto_start and fully configured."""
        channels = await run_db(crud.channels_needing_autostart)
        started = 0
        for channel in channels:
            if not self.has_output(channel):
                await run_db(
                    crud.set_channel_status, channel.id, ChannelState.CONFIG_REQUIRED
                )
                logger.info(
                    "channel %s (%s) skipped: no RTMP destination",
                    channel.id,
                    channel.name,
                )
                continue
            await self.supervisor(channel.id).start()
            started += 1
        if started:
            logger.info("auto-started %d channel(s)", started)
        return started

    async def start_all(self) -> dict[str, Any]:
        channels = await run_db(crud.list_channels, enabled_only=True)
        results = [await self.start_channel(channel.id) for channel in channels]
        started = sum(1 for r in results if r.get("ok"))
        return {"ok": True, "started": started, "total": len(channels)}

    async def stop_all(self, reason: str = "stop all requested") -> dict[str, Any]:
        supervisors = list(self._supervisors.values())
        await asyncio.gather(
            *(s.stop(reason) for s in supervisors), return_exceptions=True
        )
        return {"ok": True, "stopped": len(supervisors)}

    async def restart_many(self, channel_ids: list[int]) -> dict[str, Any]:
        await asyncio.gather(
            *(self.restart_channel(cid) for cid in channel_ids), return_exceptions=True
        )
        return {"ok": True, "count": len(channel_ids)}

    async def refresh_many(self, channel_ids: list[int]) -> dict[str, Any]:
        results = await asyncio.gather(
            *(self.refresh_channel(cid) for cid in channel_ids), return_exceptions=True
        )
        ok = sum(1 for r in results if isinstance(r, dict) and r.get("ok"))
        return {"ok": True, "refreshed": ok, "total": len(channel_ids)}

    # ------------------------------------------------------------------ #
    # channel discovery / sync
    # ------------------------------------------------------------------ #
    async def sync_channels(self, provider_id: int | None = None) -> dict[str, Any]:
        """Compare the provider's channel list with the database.

        New channels are added (disabled until an RTMP destination exists);
        channels that vanished upstream are flagged, never deleted.
        """
        providers = (
            [await run_db(crud.get_provider, provider_id)]
            if provider_id is not None
            else await run_db(crud.list_providers, enabled_only=True)
        )
        summary: dict[str, Any] = {
            "ok": True,
            "added": [],
            "updated": [],
            "missing": [],
            "errors": [],
        }

        for row in providers:
            if row is None:
                summary["errors"].append("provider not found")
                continue
            provider = self._providers.get(row.id)
            if provider is None:
                summary["errors"].append(f"{row.name}: provider is not loaded")
                continue
            try:
                discovered: list[ChannelInfo] = await provider.list_channels()
            except DiscoveryNotSupported as exc:
                summary["errors"].append(f"{row.name}: {exc}")
                continue
            except ProviderAuthError as exc:
                summary["errors"].append(f"{row.name}: {exc}")
                await run_db(
                    crud.set_provider_auth_state, row.id, ok=False, error=str(exc)
                )
                await self._notify_auth_error(str(exc))
                continue
            except ProviderError as exc:
                summary["errors"].append(f"{row.name}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001
                logger.exception("sync failed for provider %s", row.name)
                summary["errors"].append(f"{row.name}: unexpected error: {exc}")
                continue

            await run_db(crud.set_provider_auth_state, row.id, ok=True, error="")

            for info in discovered:
                existing = await run_db(
                    crud.get_channel_by_provider_ref, row.id, info.id
                )
                if existing is None:
                    created = await run_db(
                        crud.create_channel,
                        name=info.name,
                        provider_id=row.id,
                        provider_ref=info.id,
                        logo_url=info.logo,
                        external_id=str(info.metadata.get("external_id", "")),
                        group_title=str(info.metadata.get("group_title", "")),
                        input_url=str(info.metadata.get("url", "")),
                        stream_mode=self._settings.get_str("default_stream_mode"),
                        enabled=False,
                        auto_start=False,
                        status=ChannelState.CONFIG_REQUIRED.value,
                    )
                    summary["added"].append({"id": created.id, "name": created.name})
                    await run_db(
                        crud.add_event,
                        event_type=EventType.CHANNEL_ADDED,
                        message=f"discovered from provider {row.name}",
                        channel_id=created.id,
                        channel_name=created.name,
                    )
                else:
                    changes: dict[str, Any] = {}
                    if info.name and info.name != existing.name:
                        changes["name"] = info.name
                    if info.logo and info.logo != existing.logo_url:
                        changes["logo_url"] = info.logo
                    playlist_url = str(info.metadata.get("url", ""))
                    if playlist_url and playlist_url != existing.input_url:
                        changes["input_url"] = playlist_url
                    if not existing.source_present:
                        changes["source_present"] = True
                    if changes:
                        await run_db(crud.update_channel, existing.id, **changes)
                        summary["updated"].append(
                            {"id": existing.id, "name": existing.name}
                        )

            missing = await run_db(
                crud.mark_missing_from_source, row.id, [c.id for c in discovered]
            )
            for channel in missing:
                summary["missing"].append({"id": channel.id, "name": channel.name})
                await run_db(
                    crud.add_event,
                    event_type=EventType.CHANNEL_REMOVED_FROM_SOURCE,
                    message=f"no longer offered by provider {row.name}",
                    channel_id=channel.id,
                    channel_name=channel.name,
                    level="warning",
                )

        return summary

    # ------------------------------------------------------------------ #
    # watchdog
    # ------------------------------------------------------------------ #
    async def _watchdog(self) -> None:
        """Keep supervisors in step with the database."""
        logger.info("watchdog started (every %ds)", WATCHDOG_INTERVAL_SECONDS)
        try:
            while not self._shutting_down:
                await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)
                try:
                    await self._reconcile()
                except Exception:  # noqa: BLE001 - watchdog must never die
                    logger.exception("watchdog iteration failed")
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            logger.info("watchdog stopped")
            raise

    async def _reconcile(self) -> None:
        channels = await run_db(crud.list_channels)
        known_ids = {channel.id for channel in channels}

        # 1) supervisors for channels that no longer exist
        for channel_id in list(self._supervisors):
            if channel_id not in known_ids:
                supervisor = self._supervisors.pop(channel_id)
                await supervisor.stop("channel deleted")
                self._resolver.forget(channel_id)

        # 2) auto-start channels whose supervisor died or was never started
        for channel in channels:
            supervisor = self._supervisors.get(channel.id)
            should_run = (
                channel.enabled
                and channel.auto_start
                and self.has_output(channel)
            )
            if should_run and (supervisor is None or not supervisor.is_running):
                if self.ffmpeg_info.available:
                    logger.info(
                        "watchdog: (re)starting auto-start channel %s", channel.id
                    )
                    await self.supervisor(channel.id).start()
            elif supervisor is not None and not channel.enabled and supervisor.is_running:
                await supervisor.stop("channel disabled")

    # ------------------------------------------------------------------ #
    # status
    # ------------------------------------------------------------------ #
    def snapshot(self, channel_id: int) -> dict[str, Any]:
        supervisor = self._supervisors.get(channel_id)
        if supervisor is None:
            return {
                "channel_id": channel_id,
                "state": None,
                "supervisor_running": False,
                "ffmpeg": None,
            }
        return supervisor.snapshot()

    def snapshots(self) -> dict[int, dict[str, Any]]:
        return {cid: s.snapshot() for cid, s in self._supervisors.items()}

    def running_process_count(self) -> int:
        return sum(
            1
            for s in self._supervisors.values()
            if s.process is not None and s.process.running
        )

    def system_metrics(self) -> dict[str, Any]:
        """CPU, memory, process count and application uptime for the dashboard."""
        try:
            cpu = psutil.cpu_percent(interval=None)
            memory = psutil.virtual_memory()
            process = psutil.Process()
            own_memory = process.memory_info().rss
        except psutil.Error as exc:  # pragma: no cover - platform dependent
            logger.debug("psutil metrics unavailable: %s", exc)
            return {"available": False}
        return {
            "available": True,
            "cpu_percent": round(cpu, 1),
            "memory_percent": round(memory.percent, 1),
            "memory_used_mb": round(memory.used / 1024 / 1024),
            "memory_total_mb": round(memory.total / 1024 / 1024),
            "app_memory_mb": round(own_memory / 1024 / 1024, 1),
            "ffmpeg_processes": self.running_process_count(),
            "uptime_seconds": round(time.monotonic() - self._started_at),
            "checked_at": utcnow().isoformat(),
        }

    @property
    def ffmpeg_capabilities(self) -> dict[str, Any]:
        return self._ffmpeg.capabilities.as_dict()

    async def _notify_auth_error(self, error: str) -> None:
        if self._notifier is None:
            return
        try:
            await self._notifier.provider_auth_error(error)
        except Exception:  # noqa: BLE001
            logger.exception("telegram notification failed (auth)")
