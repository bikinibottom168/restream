"""Per-channel endpoint provider.

The simplest way to work with a source whose channels do not follow one URL
pattern: give every channel its own endpoint URL and let this provider fetch it
and pull the media URL out of the answer.

    Channel "Sport Channel 01"  ->  https://media.example.com/play?id=82290
    Channel "Sport Channel 02"  ->  https://media.example.com/play?id=82291
    Channel "Test Channel A"    ->  https://other.example.com/api/live/testa

Each URL is fetched fresh every time the channel is resolved or refreshed, so a
short-lived signed media URL is renewed simply by asking that endpoint again.

The response can be JSON, HTML, a JavaScript blob, a redirect, or the manifest
itself — the parser handles all five. Set a JSON path when you want to be
strict about which field is read.

This provider performs one plain HTTP GET with the headers you configure. It
does not log in, does not manage a session, and never constructs or alters a
signature.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.core.security import mask_url_token
from app.providers.base import (
    ChannelInfo,
    ProviderAuthError,
    ProviderHealth,
    ProviderUnavailable,
    ResolvedStream,
    StreamProvider,
)
from app.providers.extract import extract_stream_urls, find_iframes
from app.providers.jsonpath import get_string
from app.providers.util import guess_expiry, join_url, normalise_headers

logger = logging.getLogger(__name__)

PARSERS = ("auto", "json_path", "text", "location")

#: Response bodies that already are the manifest.
_MANIFEST_MARKERS = ("#EXTM3U", "#EXT-X-", "<MPD")


class UrlEndpointProvider(StreamProvider):
    """Resolve each channel from its own endpoint URL."""

    type_name = "url_endpoint"
    label = "Per-channel endpoint URL"
    supports_discovery = False
    supports_auth = False
    #: The operator supplies a URL on every channel.
    requires_channel_url = True

    def __init__(self, *, client: httpx.AsyncClient | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._client = client
        self._own_client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        # A private client is created only when TLS verification is disabled or
        # a custom timeout is set; otherwise the shared pool is used.
        verify = bool(self.option("verify_tls", True))
        timeout = float(self.option("timeout_seconds", 20) or 20)
        if not verify or self._client is None:
            self._own_client = httpx.AsyncClient(
                timeout=httpx.Timeout(timeout),
                verify=verify,
                follow_redirects=True,
            )

    async def aclose(self) -> None:
        if self._own_client is not None:
            await self._own_client.aclose()
            self._own_client = None

    def _http(self) -> httpx.AsyncClient:
        client = self._own_client or self._client
        if client is None:
            raise ProviderUnavailable("provider has no HTTP client (start() was not called)")
        return client

    # ------------------------------------------------------------------ #
    def _request_headers(self) -> dict[str, str]:
        headers = normalise_headers(self.option("headers"))
        referer = str(self.option("referer", "") or "")
        user_agent = str(self.option("user_agent", "") or "")
        if referer:
            headers.setdefault("Referer", referer)
        if user_agent:
            headers.setdefault("User-Agent", user_agent)
        return headers

    def _playback_headers(self) -> dict[str, str]:
        return normalise_headers(self.option("playback_headers"))

    @staticmethod
    def endpoint_for(channel: ChannelInfo) -> str:
        """The URL configured on this channel, if any."""
        return str(channel.metadata.get("resolve_url") or "").strip()

    # ------------------------------------------------------------------ #
    async def resolve_stream(self, channel: ChannelInfo) -> ResolvedStream:
        endpoint = self.endpoint_for(channel)
        if not endpoint:
            raise ProviderUnavailable(
                "this channel has no endpoint URL - open Channel > Edit and set "
                "'Source endpoint URL'"
            )
        url = join_url(str(self.option("base_url", "") or ""), endpoint)
        if not url.lower().startswith(("http://", "https://")):
            raise ProviderUnavailable(
                "the endpoint URL must start with http:// or https://"
            )

        try:
            response = await self._http().get(
                url, headers=self._request_headers() or None, follow_redirects=True
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(
                f"could not reach {mask_url_token(url)}: {exc}"
            ) from exc

        if response.status_code in (401, 403):
            raise ProviderAuthError(
                f"endpoint answered HTTP {response.status_code}; if it needs a "
                "session, add the required headers on this provider"
            )
        if response.status_code >= 400:
            raise ProviderUnavailable(
                f"endpoint answered HTTP {response.status_code}"
            )

        media_url = await self._parse(response)
        if not media_url:
            raise ProviderUnavailable(
                "no media URL found in the endpoint response - set a JSON path, "
                "or check that the endpoint really returns a stream URL"
            )

        return ResolvedStream(
            channel_id=channel.id,
            url=media_url,
            headers=self._playback_headers(),
            referer=str(self.option("referer", "") or ""),
            user_agent=str(self.option("user_agent", "") or ""),
            expires_at=guess_expiry(media_url),
            provider=self.type_name,
            note=f"resolved from {mask_url_token(url)}",
        )

    async def _parse(self, response: httpx.Response) -> str:
        parser = str(self.option("parser", "auto") or "auto").lower()
        url_path = str(self.option("url_path", "") or "").strip()
        base = str(response.url)
        text = response.text

        if parser == "location":
            return base

        if parser in ("auto", "json_path") and url_path:
            try:
                payload = response.json()
            except (json.JSONDecodeError, ValueError):
                payload = None
            if payload is not None:
                candidate = get_string(payload, url_path)
                if candidate:
                    return join_url(base, candidate)
            if parser == "json_path":
                return ""

        if parser == "text":
            first = text.strip().splitlines()[0].strip() if text.strip() else ""
            return first if first.lower().startswith("http") else ""

        # auto -------------------------------------------------------------
        if any(marker in text[:512] for marker in _MANIFEST_MARKERS):
            return base  # the endpoint served the manifest directly

        candidates = extract_stream_urls(text, base_url=base)
        if candidates:
            return candidates[0]

        # One hop into an embedded player, when the answer was a page.
        if "html" in response.headers.get("content-type", "").lower():
            if not bool(self.option("follow_iframe", True)):
                return ""
            for frame in find_iframes(text, base_url=base)[:2]:
                try:
                    nested = await self._http().get(
                        frame, headers=self._request_headers() or None, follow_redirects=True
                    )
                except httpx.HTTPError:
                    continue
                if nested.status_code >= 400:
                    continue
                nested_candidates = extract_stream_urls(
                    nested.text, base_url=str(nested.url)
                )
                if nested_candidates:
                    return nested_candidates[0]
        return ""

    async def refresh_stream(self, channel: ChannelInfo) -> ResolvedStream:
        """Nothing is cached here, so a refresh is simply another request."""
        return await self.resolve_stream(channel)

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            ok=True,
            message="ready - each channel carries its own endpoint URL",
            authenticated=True,
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def config_schema() -> list[dict[str, Any]]:
        return [
            {
                "key": "base_url",
                "label": "Base URL (optional)",
                "type": "url",
                "placeholder": "https://media.example.com",
                "help": "Only needed if you want to enter relative paths on channels.",
            },
            {
                "key": "parser",
                "label": "Response parser",
                "type": "choice",
                "choices": list(PARSERS),
                "default": "auto",
                "help": "'auto' handles JSON, HTML and JavaScript. 'json_path' is strict.",
            },
            {
                "key": "url_path",
                "label": "Stream URL JSON path",
                "type": "text",
                "placeholder": "data.stream.url",
                "help": "Which field holds the URL. Leave empty to search the whole response.",
            },
            {
                "key": "referer",
                "label": "Referer",
                "type": "text",
                "placeholder": "https://media.example.com/",
                "help": "Sent both when fetching the endpoint and by FFmpeg.",
            },
            {
                "key": "user_agent",
                "label": "User-Agent",
                "type": "text",
                "placeholder": "Mozilla/5.0 (compatible; RestreamManager/1.0)",
            },
            {
                "key": "headers",
                "label": "Request headers (JSON)",
                "type": "json",
                "placeholder": '{"Cookie": "session=..."}',
                "help": "Sent when fetching the endpoint. Masked everywhere it is shown.",
            },
            {
                "key": "playback_headers",
                "label": "Playback headers (JSON)",
                "type": "json",
                "placeholder": '{"Referer": "https://media.example.com/"}',
                "help": "Sent by FFmpeg with the media request.",
            },
            {
                "key": "timeout_seconds",
                "label": "Timeout (seconds)",
                "type": "number",
                "default": 20,
                "placeholder": "20",
            },
            {"key": "verify_tls", "label": "Verify TLS certificates", "type": "bool", "default": True},
        ]
