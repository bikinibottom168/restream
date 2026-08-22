"""Static M3U8 provider.

Three ways to use it, all configured from the dashboard:

1. per-channel URL - identical to the manual provider but with shared
   playback headers applied to every channel
2. ``url_template`` - e.g. ``https://origin.example.com/live/{channel_id}/index.m3u8``
   so a channel only needs its id
3. ``playlist_url`` - an M3U/M3U8 playlist that also powers *Sync Channels*

The URL is treated as dynamic even here: it is re-read from the playlist on
every refresh, because an origin can move a channel between nodes.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from app.providers.base import (
    ChannelInfo,
    ProviderAuthError,
    ProviderHealth,
    ProviderUnavailable,
    ResolvedStream,
    StreamProvider,
)
from app.providers.util import guess_expiry, join_url, normalise_headers, substitute

logger = logging.getLogger(__name__)

_EXTINF_RE = re.compile(
    r"^#EXTINF:(?P<duration>-?\d+(?:\.\d+)?)\s*(?P<attrs>[^,]*),(?P<name>.*)$"
)
_ATTR_RE = re.compile(r'(?P<key>[\w-]+)\s*=\s*"(?P<value>[^"]*)"')


def parse_m3u(text: str) -> list[ChannelInfo]:
    """Parse an extended M3U playlist into :class:`ChannelInfo` entries.

    Understands the usual ``tvg-id`` / ``tvg-name`` / ``tvg-logo`` /
    ``group-title`` attributes.  A malformed record is skipped rather than
    raising, so one bad line cannot break an entire sync.
    """
    entries: list[ChannelInfo] = []
    if not text:
        return entries

    pending: ChannelInfo | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.upper().startswith("#EXTM3U"):
            continue
        if line.startswith("#EXTINF"):
            match = _EXTINF_RE.match(line)
            if not match:
                pending = None
                continue
            attrs = {
                m.group("key").lower(): m.group("value")
                for m in _ATTR_RE.finditer(match.group("attrs") or "")
            }
            display_name = (match.group("name") or "").strip()
            name = display_name or attrs.get("tvg-name", "") or "Unnamed channel"
            ref = (attrs.get("tvg-id") or attrs.get("tvg-chno") or name).strip()
            pending = ChannelInfo(
                id=ref,
                name=name,
                logo=attrs.get("tvg-logo", ""),
                metadata={
                    "group_title": attrs.get("group-title", ""),
                    "external_id": attrs.get("tvg-id", ""),
                },
            )
            continue
        if line.startswith("#"):
            continue  # #EXTVLCOPT, #EXTGRP, ...
        if pending is not None:
            pending.metadata["url"] = line
            entries.append(pending)
            pending = None
    return entries


class StaticM3U8Provider(StreamProvider):
    """Serve URLs from a template, a playlist, or the channel row."""

    type_name = "static_m3u8"
    label = "Static M3U8"
    supports_auth = False
    requires_channel_url = True

    def __init__(self, *, client: httpx.AsyncClient | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._client = client
        self._cache: list[ChannelInfo] = []
        self._cached_at = 0.0

    # ------------------------------------------------------------------ #
    @property
    def supports_discovery(self) -> bool:  # type: ignore[override]
        return bool(self.option("playlist_url"))

    @property
    def _cache_ttl(self) -> int:
        try:
            return max(0, int(self.option("cache_ttl_seconds", 900)))
        except (TypeError, ValueError):
            return 900

    def _playback_headers(self) -> dict[str, str]:
        return normalise_headers(self.option("headers"))

    async def aclose(self) -> None:
        self._cache = []

    # ------------------------------------------------------------------ #
    async def _load_playlist(self, force: bool = False) -> list[ChannelInfo]:
        playlist_url = str(self.option("playlist_url", "") or "").strip()
        if not playlist_url:
            raise ProviderUnavailable("no playlist URL configured for this provider")
        if self._client is None:
            raise ProviderUnavailable("provider has no HTTP client (start() was not called)")

        fresh = self._cache and (time.monotonic() - self._cached_at) < self._cache_ttl
        if fresh and not force:
            return self._cache

        headers = self._playback_headers()
        try:
            response = await self._client.get(
                playlist_url, headers=headers or None, follow_redirects=True
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"could not fetch playlist: {exc}") from exc
        if response.status_code in (401, 403):
            raise ProviderAuthError(
                f"playlist request rejected with HTTP {response.status_code}"
            )
        if response.status_code >= 400:
            raise ProviderUnavailable(
                f"playlist request failed with HTTP {response.status_code}"
            )

        entries = parse_m3u(response.text)
        if not entries:
            raise ProviderUnavailable("playlist contained no #EXTINF entries")
        self._cache = entries
        self._cached_at = time.monotonic()
        logger.info("provider %s loaded %d playlist entries", self.name, len(entries))
        return entries

    def _find(self, entries: list[ChannelInfo], channel: ChannelInfo) -> ChannelInfo | None:
        wanted = (channel.id or "").strip().lower()
        for entry in entries:
            if wanted and entry.id.strip().lower() == wanted:
                return entry
        name = (channel.name or "").strip().lower()
        for entry in entries:
            if name and entry.name.strip().lower() == name:
                return entry
        return None

    # ------------------------------------------------------------------ #
    async def resolve_stream(self, channel: ChannelInfo) -> ResolvedStream:
        headers = self._playback_headers()
        referer = str(self.option("referer", "") or "")
        user_agent = str(self.option("user_agent", "") or "")

        # 1) a URL stored on the channel wins
        url = str(channel.metadata.get("input_url") or "").strip()

        # 2) otherwise build it from the template
        if not url:
            template = str(self.option("url_template", "") or "").strip()
            if template and channel.id:
                url = substitute(template, channel_id=channel.id, ref=channel.id)
                url = join_url(str(self.option("base_url", "") or ""), url)

        # 3) otherwise look it up in the playlist
        if not url and self.option("playlist_url"):
            entries = await self._load_playlist()
            match = self._find(entries, channel)
            if match is None:
                entries = await self._load_playlist(force=True)
                match = self._find(entries, channel)
            if match is not None:
                url = str(match.metadata.get("url") or "")

        if not url:
            raise ProviderUnavailable(
                "no URL for this channel: set one on the channel, configure a "
                "url_template, or add the channel to the playlist"
            )

        return ResolvedStream(
            channel_id=channel.id,
            url=url,
            headers=headers,
            referer=referer,
            user_agent=user_agent,
            expires_at=guess_expiry(url),
            provider=self.type_name,
            note="static source",
        )

    async def refresh_stream(self, channel: ChannelInfo) -> ResolvedStream:
        if self.option("playlist_url"):
            await self._load_playlist(force=True)
        return await self.resolve_stream(channel)

    async def list_channels(self) -> list[ChannelInfo]:
        return await self._load_playlist(force=True)

    async def health(self) -> ProviderHealth:
        if not self.option("playlist_url"):
            return ProviderHealth(
                ok=True, message="static provider, nothing to contact", authenticated=True
            )
        try:
            entries = await self._load_playlist()
        except ProviderUnavailable as exc:
            return ProviderHealth(ok=False, message=str(exc))
        except ProviderAuthError as exc:
            return ProviderHealth(ok=False, message=str(exc))
        return ProviderHealth(
            ok=True,
            message=f"playlist reachable ({len(entries)} channels)",
            authenticated=True,
            channel_count=len(entries),
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def config_schema() -> list[dict[str, Any]]:
        return [
            {
                "key": "base_url",
                "label": "Base URL",
                "type": "url",
                "placeholder": "https://origin.example.com",
                "help": "Optional. Prefixed to a relative URL template.",
            },
            {
                "key": "url_template",
                "label": "URL template",
                "type": "text",
                "placeholder": "https://origin.example.com/live/{channel_id}/index.m3u8",
                "help": "{channel_id} is replaced with each channel's provider id.",
            },
            {
                "key": "playlist_url",
                "label": "Playlist URL (M3U)",
                "type": "url",
                "placeholder": "https://origin.example.com/playlist.m3u",
                "help": "Optional. Enables Sync Channels for this provider.",
            },
            {
                "key": "referer",
                "label": "Referer",
                "type": "text",
                "placeholder": "https://origin.example.com/",
                "help": "Sent to FFmpeg when fetching the source.",
            },
            {
                "key": "user_agent",
                "label": "User-Agent",
                "type": "text",
                "placeholder": "Mozilla/5.0 (compatible; RestreamManager/1.0)",
            },
            {
                "key": "headers",
                "label": "Extra headers (JSON)",
                "type": "json",
                "placeholder": '{"X-Token": "your-token"}',
                "help": "Masked everywhere it is displayed.",
            },
            {
                "key": "cache_ttl_seconds",
                "label": "Playlist cache (seconds)",
                "type": "number",
                "default": 900,
                "placeholder": "900",
            },
        ]
