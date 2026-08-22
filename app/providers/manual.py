"""Manual provider.

The operator creates a channel in the dashboard and pastes the source URL.
Optional per-channel playback headers (referer, user-agent, cookie) can be set
on the channel and are passed straight through to FFmpeg.

This provider never performs a network request of its own.
"""

from __future__ import annotations

from typing import Any

from app.providers.base import (
    ChannelInfo,
    ProviderHealth,
    ProviderUnavailable,
    ResolvedStream,
    StreamProvider,
)
from app.providers.util import guess_expiry, normalise_headers

#: Input schemes FFmpeg can read.
ALLOWED_SCHEMES = (
    "http://",
    "https://",
    "rtmp://",
    "rtmps://",
    "rtsp://",
    "srt://",
    "udp://",
    "rtp://",
    "file://",
)


class ManualProvider(StreamProvider):
    """Serve the URL stored on the channel row."""

    type_name = "manual"
    label = "Manual URL"
    supports_discovery = False
    supports_auth = False
    requires_channel_url = True

    async def resolve_stream(self, channel: ChannelInfo) -> ResolvedStream:
        url = str(channel.metadata.get("input_url") or "").strip()
        if not url:
            raise ProviderUnavailable(
                "no source URL set for this channel - open Channel > Edit and paste one"
            )
        if not url.lower().startswith(ALLOWED_SCHEMES):
            raise ProviderUnavailable(
                "unsupported URL scheme; expected one of " + ", ".join(ALLOWED_SCHEMES)
            )

        headers = normalise_headers(channel.metadata.get("headers"))
        return ResolvedStream(
            channel_id=channel.id,
            url=url,
            headers=headers,
            referer=str(channel.metadata.get("referer") or ""),
            user_agent=str(channel.metadata.get("user_agent") or ""),
            expires_at=guess_expiry(url),
            provider=self.type_name,
            note="operator-supplied URL",
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            ok=True,
            message="manual provider needs no connection",
            authenticated=True,
        )

    @staticmethod
    def config_schema() -> list[dict[str, Any]]:
        """Field descriptors rendered by the dashboard's provider form."""
        return []
