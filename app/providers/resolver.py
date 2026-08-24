"""Stream resolution pipeline.

    channel row
        |
        v  provider.resolve_stream() / refresh_stream()
    ResolvedStream (url + headers + cookies + expiry)
        |
        v  manifest inspection      -> refuse DRM-protected media
        v  ffprobe validation       -> confirm it is actually playable
        v  persisted on the channel row

The supervisor calls only this module; it never learns which provider a channel
uses.  A per-channel lock keeps a manual "Refresh Source" click and the
watchdog from resolving the same channel twice at once.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.security import mask_url_token
from app.core.settings_store import SettingsStore
from app.core.timeutil import ensure_utc, utcnow
from app.database import crud
from app.database.db import run_db
from app.providers.base import (
    ChannelInfo,
    ProviderAuthError,
    ProviderConfigError,
    ProviderError,
    ProviderUnavailable,
    ProviderUnsupportedMedia,
    ResolvedStream,
    StreamProvider,
)
from app.providers.extract import detect_drm
from app.providers.manager import ProviderManager
from app.providers.util import normalise_headers
from app.streaming.probe import ProbeResult, probe_stream

logger = logging.getLogger(__name__)

#: Only this much of a manifest is read for the DRM check.
_MANIFEST_PEEK_BYTES = 64 * 1024


@dataclass(slots=True)
class ResolutionOutcome:
    """Everything the supervisor needs to know about one resolution attempt."""

    ok: bool
    stream: ResolvedStream | None = None
    probe: ProbeResult | None = None
    error: str = ""
    unsupported: bool = False
    auth_error: bool = False
    from_cache: bool = False

    @property
    def url(self) -> str:
        return self.stream.url if self.stream else ""

    def as_dict(self, *, reveal: bool = False) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error": self.error,
            "unsupported": self.unsupported,
            "auth_error": self.auth_error,
            "from_cache": self.from_cache,
            "stream": self.stream.as_dict(reveal=reveal) if self.stream else None,
            "probe": self.probe.as_dict() if self.probe else None,
        }


class StreamResolver:
    """Resolve, vet and persist the input for a channel."""

    def __init__(self, providers: ProviderManager, settings: SettingsStore) -> None:
        self._providers = providers
        self._settings = settings
        self._locks: dict[int, asyncio.Lock] = {}

    # ------------------------------------------------------------------ #
    def _lock_for(self, channel_id: int) -> asyncio.Lock:
        lock = self._locks.get(channel_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[channel_id] = lock
        return lock

    def forget(self, channel_id: int) -> None:
        self._locks.pop(channel_id, None)

    # ------------------------------------------------------------------ #
    @staticmethod
    def channel_info(channel: Any) -> ChannelInfo:
        """Build the provider-facing view of a channel row."""
        info = ChannelInfo.from_model(channel)
        info.metadata.update(
            {
                "headers": channel.playback_headers,
                "referer": channel.playback_referer or "",
                "user_agent": channel.playback_user_agent or "",
            }
        )
        return info

    def _apply_channel_overrides(self, stream: ResolvedStream, channel: Any) -> ResolvedStream:
        """Per-channel playback hints win over provider-wide defaults."""
        headers = dict(stream.headers)
        headers.update(normalise_headers(channel.playback_headers))
        stream.headers = headers
        if channel.playback_referer:
            stream.referer = channel.playback_referer
        if channel.playback_user_agent:
            stream.user_agent = channel.playback_user_agent
        if not stream.user_agent:
            stream.user_agent = self._settings.get_str("source_user_agent")
        return stream

    def _cached_stream(self, channel: Any) -> ResolvedStream | None:
        """Reuse the stored URL while it is still fresh."""
        url = (channel.source_url or "").strip()
        if not url:
            return None
        expires_at = ensure_utc(channel.source_expires_at)
        if expires_at is not None and expires_at <= utcnow():
            return None
        resolved_at = ensure_utc(channel.source_resolved_at)
        ttl = self._settings.get_int("source_cache_ttl_seconds")
        if resolved_at is None or ttl <= 0:
            return None
        if (utcnow() - resolved_at).total_seconds() >= ttl:
            return None
        return ResolvedStream(
            channel_id=channel.provider_ref or str(channel.id),
            url=url,
            expires_at=expires_at,
            provider=str(channel.provider_id or "manual"),
            note="cached",
        )

    # ------------------------------------------------------------------ #
    async def _check_manifest(self, stream: ResolvedStream) -> None:
        """Refuse media that would need a licence or key exchange.

        A plain HLS manifest with ``METHOD=AES-128`` is fine - FFmpeg fetches
        that key over the same authorised session a normal player uses.  DRM
        schemes (Widevine, PlayReady, FairPlay, DASH ContentProtection) are
        refused outright: this application implements no circumvention.
        """
        url = stream.url
        if not stream.is_http:
            return
        path = url.lower().split("?", 1)[0]
        if not path.endswith((".m3u8", ".mpd")):
            return
        try:
            response = await self._providers.client.get(
                url,
                headers=stream.request_headers() or None,
                follow_redirects=True,
                timeout=httpx.Timeout(10.0),
            )
        except httpx.HTTPError as exc:
            logger.debug("manifest peek failed for %s: %s", mask_url_token(url), exc)
            return
        if response.status_code in (401, 403):
            raise ProviderAuthError(
                f"manifest request rejected with HTTP {response.status_code}"
            )
        if response.status_code >= 400:
            return
        scheme = detect_drm(response.text[:_MANIFEST_PEEK_BYTES])
        if scheme:
            raise ProviderUnsupportedMedia(
                f"source is protected media ({scheme}); this application does not "
                "implement DRM, key extraction or licence handling"
            )

    # ------------------------------------------------------------------ #
    async def resolve(
        self, channel: Any, *, force: bool = False, validate: bool = True
    ) -> ResolutionOutcome:
        """Resolve (and optionally validate) the input for *channel*."""
        async with self._lock_for(channel.id):
            return await self._resolve_locked(channel, force=force, validate=validate)

    async def _resolve_locked(
        self, channel: Any, *, force: bool, validate: bool, _retry: bool = True
    ) -> ResolutionOutcome:
        stream: ResolvedStream | None = None
        from_cache = False

        if not force:
            stream = self._cached_stream(channel)
            from_cache = stream is not None

        if stream is None:
            provider: StreamProvider = self._providers.for_channel(channel)
            info = self.channel_info(channel)
            try:
                if force:
                    stream = await provider.refresh_stream(info)
                else:
                    stream = await provider.resolve_stream(info)
            except ProviderUnsupportedMedia as exc:
                await self._persist_error(channel.id, str(exc))
                return ResolutionOutcome(ok=False, error=str(exc), unsupported=True)
            except ProviderAuthError as exc:
                await self._persist_error(channel.id, str(exc))
                await self._record_auth_failure(channel, str(exc))
                return ResolutionOutcome(ok=False, error=str(exc), auth_error=True)
            except (ProviderUnavailable, ProviderConfigError, ProviderError) as exc:
                await self._persist_error(channel.id, str(exc))
                return ResolutionOutcome(ok=False, error=str(exc))
            except Exception as exc:  # noqa: BLE001 - a provider bug must not kill the loop
                logger.exception("provider raised while resolving channel %s", channel.id)
                message = f"unexpected provider error: {exc}"
                await self._persist_error(channel.id, message)
                return ResolutionOutcome(ok=False, error=message)

            if stream is None or not stream.url:
                message = "provider returned an empty stream URL"
                await self._persist_error(channel.id, message)
                return ResolutionOutcome(ok=False, error=message)

        stream = self._apply_channel_overrides(stream, channel)

        # ---- refuse protected media ------------------------------------
        try:
            await self._check_manifest(stream)
        except ProviderUnsupportedMedia as exc:
            await self._persist_error(channel.id, str(exc))
            return ResolutionOutcome(
                ok=False, stream=stream, error=str(exc), unsupported=True
            )
        except ProviderAuthError as exc:
            if from_cache and _retry:
                logger.info(
                    "cached URL for channel %s is no longer authorised - refreshing",
                    channel.id,
                )
                return await self._resolve_locked(
                    channel, force=True, validate=validate, _retry=False
                )
            await self._persist_error(channel.id, str(exc))
            await self._record_auth_failure(channel, str(exc))
            return ResolutionOutcome(
                ok=False, stream=stream, error=str(exc), auth_error=True
            )

        # ---- validate ---------------------------------------------------
        probe: ProbeResult | None = None
        if validate:
            probe = await probe_stream(
                stream.url,
                ffprobe_path=self._settings.get_str("ffprobe_path"),
                timeout=float(self._settings.get_int("probe_timeout_seconds")),
                headers=stream.request_headers() or None,
                user_agent=stream.user_agent,
            )
            if not probe.ok:
                if from_cache and _retry:
                    logger.info(
                        "cached source for channel %s failed validation - re-resolving",
                        channel.id,
                    )
                    return await self._resolve_locked(
                        channel, force=True, validate=True, _retry=False
                    )
                await self._persist_error(channel.id, probe.error)
                return ResolutionOutcome(
                    ok=False, stream=stream, probe=probe, error=probe.error
                )

        # ---- persist ----------------------------------------------------
        if not from_cache:
            await run_db(
                crud.mark_source_resolved,
                channel.id,
                stream.url,
                expires_at=stream.expires_at,
                error="",
            )
            logger.info(
                "channel %s resolved -> %s", channel.id, mask_url_token(stream.url)
            )
        else:
            await run_db(crud.update_channel, channel.id, resolve_error="")

        return ResolutionOutcome(
            ok=True, stream=stream, probe=probe, from_cache=from_cache
        )

    # ------------------------------------------------------------------ #
    async def resolve_direct(
        self,
        channel: Any,
        source: Any,
        *,
        validate: bool = True,
    ) -> ResolutionOutcome:
        """Vet a backup URL the operator typed, bypassing the provider.

        A fallback is a plain media URL: there is nothing to log in to and no
        token to renew, so the provider is skipped entirely and only the parts
        that protect the relay are kept - the DRM refusal and the ffprobe.

        The result is deliberately *not* written to ``channel.source_url``:
        that column caches the primary's resolution, and overwriting it with a
        backup URL would hide the primary's real state and poison its cache.
        """
        url = (getattr(source, "url", "") or "").strip()
        if not url:
            return ResolutionOutcome(ok=False, error="backup source has no URL")

        stream = ResolvedStream(
            channel_id=channel.provider_ref or str(channel.id),
            url=url,
            provider="fallback",
            note=getattr(source, "name", "fallback"),
        )
        stream = self._apply_channel_overrides(stream, channel)
        # Per-URL hints win over the channel-wide ones: a backup usually lives
        # on a different host and often needs a different Referer.
        headers = dict(stream.headers)
        headers.update(normalise_headers(getattr(source, "headers", {}) or {}))
        stream.headers = headers
        if getattr(source, "referer", ""):
            stream.referer = source.referer
        if getattr(source, "user_agent", ""):
            stream.user_agent = source.user_agent

        try:
            await self._check_manifest(stream)
        except ProviderUnsupportedMedia as exc:
            return ResolutionOutcome(
                ok=False, stream=stream, error=str(exc), unsupported=True
            )
        except ProviderAuthError as exc:
            return ResolutionOutcome(ok=False, stream=stream, error=str(exc))

        probe: ProbeResult | None = None
        if validate:
            probe = await probe_stream(
                stream.url,
                ffprobe_path=self._settings.get_str("ffprobe_path"),
                timeout=float(self._settings.get_int("probe_timeout_seconds")),
                headers=stream.request_headers() or None,
                user_agent=stream.user_agent,
            )
            if not probe.ok:
                return ResolutionOutcome(
                    ok=False, stream=stream, probe=probe, error=probe.error
                )

        logger.info(
            "channel %s using backup source %s -> %s",
            channel.id,
            getattr(source, "name", "fallback"),
            mask_url_token(stream.url),
        )
        return ResolutionOutcome(ok=True, stream=stream, probe=probe)

    # ------------------------------------------------------------------ #
    async def refresh(self, channel: Any, *, validate: bool = True) -> ResolutionOutcome:
        """Force a new URL for exactly this channel (nothing else is touched)."""
        return await self.resolve(channel, force=True, validate=validate)

    async def test(self, channel: Any) -> ResolutionOutcome:
        """Dashboard 'Test Source': resolve + probe, never start FFmpeg."""
        return await self.resolve(channel, force=True, validate=True)

    # ------------------------------------------------------------------ #
    async def _persist_error(self, channel_id: int, error: str) -> None:
        await run_db(crud.update_channel, channel_id, resolve_error=error[:1000])

    async def _record_auth_failure(self, channel: Any, error: str) -> None:
        if channel.provider_id:
            await run_db(
                crud.set_provider_auth_state, channel.provider_id, ok=False, error=error
            )
