"""Provider behaviour, exercised entirely against a mock HTTP transport."""

from __future__ import annotations

import json

import httpx
import pytest

from app.providers.base import (
    ChannelInfo,
    ProviderAuthError,
    ProviderUnavailable,
    ResolvedStream,
)
from app.providers.factory import ProviderFactory
from app.providers.http_json import HttpJsonProvider
from app.providers.manual import ManualProvider
from app.providers.static_m3u8 import StaticM3U8Provider, parse_m3u
from app.providers.url_endpoint import UrlEndpointProvider
from app.providers.util import guess_expiry, join_url, normalise_headers


def channel(**kwargs) -> ChannelInfo:
    metadata = kwargs.pop("metadata", {})
    return ChannelInfo(
        id=kwargs.pop("id", "sport01"),
        name=kwargs.pop("name", "Sport Channel 01"),
        metadata=metadata,
    )


# --------------------------------------------------------------------------- #
# manual
# --------------------------------------------------------------------------- #
async def test_manual_returns_channel_url():
    provider = ManualProvider()
    stream = await provider.resolve_stream(
        channel(metadata={"input_url": "https://origin.example/a.m3u8"})
    )
    assert stream.url == "https://origin.example/a.m3u8"
    assert stream.provider == "manual"


async def test_manual_requires_a_url():
    with pytest.raises(ProviderUnavailable):
        await ManualProvider().resolve_stream(channel(metadata={}))


async def test_manual_rejects_unknown_scheme():
    with pytest.raises(ProviderUnavailable):
        await ManualProvider().resolve_stream(
            channel(metadata={"input_url": "gopher://old.example/a"})
        )


async def test_manual_passes_playback_headers():
    provider = ManualProvider()
    stream = await provider.resolve_stream(
        channel(
            metadata={
                "input_url": "https://origin.example/a.m3u8",
                "referer": "https://portal.example/",
                "user_agent": "ExamplePlayer/1.0",
                "headers": {"X-Token": "abc"},
            }
        )
    )
    headers = stream.request_headers()
    assert headers["Referer"] == "https://portal.example/"
    assert headers["User-Agent"] == "ExamplePlayer/1.0"
    assert headers["X-Token"] == "abc"


# --------------------------------------------------------------------------- #
# static m3u8 / playlist parsing
# --------------------------------------------------------------------------- #
def test_parse_m3u(fixture_text):
    entries = parse_m3u(fixture_text("channels.m3u"))
    assert [entry.id for entry in entries] == ["sport01", "sport02", "testa", "testb"]
    assert entries[0].name == "Sport Channel 01"
    assert entries[0].logo == "https://cdn.example/logo1.png"
    assert entries[0].metadata["group_title"] == "Sports"
    assert entries[1].metadata["url"].endswith("token=xyz789")


def test_parse_m3u_tolerates_junk():
    assert parse_m3u("") == []
    assert parse_m3u("#EXTM3U\n#EXTINF:broken line\n") == []


async def test_static_template_resolution():
    provider = StaticM3U8Provider(
        config={"url_template": "https://origin.example/live/{channel_id}/index.m3u8"}
    )
    stream = await provider.resolve_stream(channel(id="sport01"))
    assert stream.url == "https://origin.example/live/sport01/index.m3u8"


async def test_static_channel_url_wins_over_template():
    provider = StaticM3U8Provider(config={"url_template": "https://tpl.example/{channel_id}.m3u8"})
    stream = await provider.resolve_stream(
        channel(metadata={"input_url": "https://explicit.example/a.m3u8"})
    )
    assert stream.url == "https://explicit.example/a.m3u8"


async def test_static_playlist_lookup(fixture_text):
    body = fixture_text("channels.m3u")

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://origin.example/playlist.m3u"
        return httpx.Response(200, text=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = StaticM3U8Provider(
        client=client, config={"playlist_url": "https://origin.example/playlist.m3u"}
    )
    stream = await provider.resolve_stream(channel(id="sport02"))
    assert stream.url.endswith("token=xyz789")

    listed = await provider.list_channels()
    assert len(listed) == 4
    await client.aclose()


async def test_static_playlist_missing_channel(fixture_text):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=fixture_text("channels.m3u")))
    )
    provider = StaticM3U8Provider(
        client=client, config={"playlist_url": "https://origin.example/playlist.m3u"}
    )
    with pytest.raises(ProviderUnavailable):
        await provider.resolve_stream(channel(id="does-not-exist", name="nope"))
    await client.aclose()


# --------------------------------------------------------------------------- #
# http_json
# --------------------------------------------------------------------------- #
def build_http_provider(handler, **config) -> HttpJsonProvider:
    base = {
        "base_url": "https://portal.example",
        "auth": {"type": "form", "url": "/login"},
        "channels": {"url": "/api/channels", "list_path": "data"},
        "stream": {"url": "/api/play?id={channel_id}", "url_path": "data.stream.url"},
    }
    base.update(config)
    provider = HttpJsonProvider(
        name="test",
        config=base,
        secrets={"username": "operator", "password": "hunter2"},
    )
    provider._client = httpx.AsyncClient(  # noqa: SLF001 - test seam
        base_url="https://portal.example",
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    return provider


async def test_http_json_login_and_resolve(fixture_text):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/login":
            assert b"operator" in request.content
            return httpx.Response(200, json={"ok": True}, headers={"set-cookie": "sid=abc; Path=/"})
        if request.url.path == "/api/play":
            return httpx.Response(200, text=fixture_text("play_response.json"))
        return httpx.Response(404)

    provider = build_http_provider(handler)
    stream = await provider.resolve_stream(channel(id="82290"))
    assert "/login" in calls
    assert stream.url.startswith("https://edge-07.media.example/live/sport01/index.m3u8")
    assert stream.expires_at is not None, "expiry should be read from the URL"
    await provider.aclose()


async def test_http_json_reauthenticates_once_on_401(fixture_text):
    state = {"logins": 0, "authorised": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            state["logins"] += 1
            state["authorised"] = True
            return httpx.Response(200, json={"ok": True})
        if not state["authorised"]:
            return httpx.Response(401, json={"error": "session expired"})
        state["authorised"] = False  # the session dies after one call
        return httpx.Response(200, text=fixture_text("play_response.json"))

    provider = build_http_provider(handler)
    await provider.resolve_stream(channel(id="82290"))
    first_logins = state["logins"]
    await provider.resolve_stream(channel(id="82290"))
    assert state["logins"] == first_logins + 1, "exactly one re-login per expiry"
    await provider.aclose()


async def test_http_json_gives_up_after_failed_reauth():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(403)

    provider = build_http_provider(handler)
    with pytest.raises(ProviderAuthError):
        await provider.resolve_stream(channel(id="82290"))
    await provider.aclose()


async def test_http_json_rejects_bad_credentials():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    provider = build_http_provider(handler)
    with pytest.raises(ProviderAuthError):
        await provider.authenticate(force=True)
    await provider.aclose()


async def test_http_json_detects_login_redirect():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(
            200,
            text='<html><form><input type="password" name="password"></form></html>',
            headers={"content-type": "text/html"},
        )

    provider = build_http_provider(handler)
    with pytest.raises(ProviderAuthError):
        await provider.resolve_stream(channel(id="82290"))
    await provider.aclose()


async def test_http_json_channel_list():
    payload = {
        "data": [
            {"id": "1", "name": "Sport Channel 01", "logo": "https://cdn/1.png"},
            {"id": "2", "name": "Sport Channel 02"},
            {"id": "2", "name": "duplicate is dropped"},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json=payload)

    provider = build_http_provider(handler)
    channels = await provider.list_channels()
    assert [c.id for c in channels] == ["1", "2"]
    assert channels[0].logo == "https://cdn/1.png"
    await provider.aclose()


async def test_http_json_bearer_auth_sends_header():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"data": {"stream": {"url": "https://cdn/a.m3u8"}}})

    provider = build_http_provider(handler, auth={"type": "bearer"})
    provider.set_secrets({"token": "tok-123"})
    await provider.resolve_stream(channel(id="1"))
    assert seen.get("authorization") == "Bearer tok-123"
    await provider.aclose()


async def test_http_json_auto_parser_finds_url_in_html():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(
            200,
            text='<video><source src="https://cdn.example/live/a.m3u8"></video>',
            headers={"content-type": "text/html"},
        )

    provider = build_http_provider(
        handler, stream={"url": "/api/play?id={channel_id}", "parser": "auto"}
    )
    stream = await provider.resolve_stream(channel(id="1"))
    assert stream.url == "https://cdn.example/live/a.m3u8"
    await provider.aclose()


async def test_http_json_reports_missing_url():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"data": {}})

    provider = build_http_provider(handler)
    with pytest.raises(ProviderUnavailable):
        await provider.resolve_stream(channel(id="1"))
    await provider.aclose()


# --------------------------------------------------------------------------- #
# url_endpoint - one URL per channel
# --------------------------------------------------------------------------- #
def build_url_provider(handler, **config) -> UrlEndpointProvider:
    provider = UrlEndpointProvider(name="per-channel", config=config)
    provider._own_client = httpx.AsyncClient(  # noqa: SLF001 - test seam
        transport=httpx.MockTransport(handler), follow_redirects=True
    )
    return provider


def endpoint_channel(url: str, **kwargs) -> ChannelInfo:
    return ChannelInfo(
        id=kwargs.pop("id", "82290"),
        name=kwargs.pop("name", "Sport Channel 01"),
        metadata={"resolve_url": url},
    )


async def test_url_endpoint_extracts_from_json(fixture_text):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/play"
        assert request.url.params["id"] == "82290"
        return httpx.Response(200, text=fixture_text("play_response.json"))

    provider = build_url_provider(handler)
    stream = await provider.resolve_stream(
        endpoint_channel("https://media.example.com/play?id=82290")
    )
    assert stream.url.startswith("https://edge-07.media.example/live/sport01/index.m3u8")
    assert stream.expires_at is not None
    await provider.aclose()


async def test_url_endpoint_json_path_is_strict():
    payload = {"data": {"stream": {"url": "https://cdn.example/a.m3u8"}},
               "decoy": "https://cdn.example/wrong.m3u8"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    provider = build_url_provider(handler, parser="json_path", url_path="data.stream.url")
    stream = await provider.resolve_stream(endpoint_channel("https://media.example.com/play?id=1"))
    assert stream.url == "https://cdn.example/a.m3u8"
    await provider.aclose()


async def test_url_endpoint_extracts_from_html(fixture_text):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=fixture_text("live_page.html"), headers={"content-type": "text/html"}
        )

    provider = build_url_provider(handler)
    stream = await provider.resolve_stream(endpoint_channel("https://media.example.com/play?id=82290"))
    assert "edge-07.media.example" in stream.url
    await provider.aclose()


async def test_url_endpoint_serves_manifest_directly():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="#EXTM3U\n#EXT-X-VERSION:3\nseg1.ts\n")

    provider = build_url_provider(handler)
    stream = await provider.resolve_stream(endpoint_channel("https://media.example.com/live.m3u8"))
    assert stream.url == "https://media.example.com/live.m3u8"
    await provider.aclose()


async def test_url_endpoint_requires_a_url():
    provider = build_url_provider(lambda r: httpx.Response(200))
    with pytest.raises(ProviderUnavailable):
        await provider.resolve_stream(ChannelInfo(id="1", name="A", metadata={}))
    await provider.aclose()


async def test_url_endpoint_reports_auth_failure():
    provider = build_url_provider(lambda r: httpx.Response(403))
    with pytest.raises(ProviderAuthError):
        await provider.resolve_stream(endpoint_channel("https://media.example.com/play?id=1"))
    await provider.aclose()


async def test_url_endpoint_reports_missing_media_url():
    provider = build_url_provider(
        lambda r: httpx.Response(200, json={"status": "ok"})
    )
    with pytest.raises(ProviderUnavailable):
        await provider.resolve_stream(endpoint_channel("https://media.example.com/play?id=1"))
    await provider.aclose()


async def test_url_endpoint_sends_configured_headers():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"url": "https://cdn.example/a.m3u8"})

    provider = build_url_provider(
        handler,
        headers={"X-Token": "abc"},
        referer="https://media.example.com/",
        user_agent="ExamplePlayer/1.0",
    )
    stream = await provider.resolve_stream(endpoint_channel("https://media.example.com/play?id=1"))
    assert seen["x-token"] == "abc"
    assert seen["referer"] == "https://media.example.com/"
    assert stream.user_agent == "ExamplePlayer/1.0"
    await provider.aclose()


async def test_http_json_per_channel_url_overrides_template(fixture_text):
    """A channel with its own endpoint ignores the provider-wide template."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/login":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, text=fixture_text("play_response.json"))

    provider = build_http_provider(handler)
    channel = ChannelInfo(
        id="99999",
        name="Odd one out",
        metadata={"resolve_url": "https://portal.example/special/route?x=1"},
    )
    await provider.resolve_stream(channel)
    assert any("special/route" in url for url in seen)
    assert not any("api/play" in url for url in seen), "the template must be skipped"
    await provider.aclose()


# --------------------------------------------------------------------------- #
# factory & helpers
# --------------------------------------------------------------------------- #
def test_factory_knows_builtin_types():
    assert set(ProviderFactory.types()) >= {"manual", "static_m3u8", "http_json", "url_endpoint"}
    entry = next(t for t in ProviderFactory.available() if t["type"] == "http_json")
    assert entry["supports_auth"] is True
    assert any(field["key"] == "stream.url_path" for field in entry["schema"])


def test_factory_creates_and_rejects():
    provider = ProviderFactory.create("manual", name="x")
    assert isinstance(provider, ManualProvider)
    with pytest.raises(Exception):
        ProviderFactory.create("does-not-exist")


def test_guess_expiry_reads_unix_timestamp():
    assert guess_expiry("https://cdn/a.m3u8?expires=1924992000") is not None
    assert guess_expiry("https://cdn/a.m3u8?expires=1924992000000") is not None
    assert guess_expiry("https://cdn/a.m3u8") is None
    assert guess_expiry("https://cdn/a.m3u8?expires=not-a-number") is None
    assert guess_expiry("https://cdn/a.m3u8?expires=5") is None


def test_normalise_headers_accepts_several_shapes():
    assert normalise_headers({"A": "1"}) == {"A": "1"}
    assert normalise_headers('{"A": "1"}') == {"A": "1"}
    assert normalise_headers("A: 1\nB: 2") == {"A": "1", "B": "2"}
    assert normalise_headers("") == {}
    assert normalise_headers(None) == {}


def test_join_url():
    assert join_url("https://a.example", "/b") == "https://a.example/b"
    assert join_url("https://a.example/", "b") == "https://a.example/b"
    assert join_url("https://a.example", "https://c.example/d") == "https://c.example/d"
    assert join_url("", "/b") == "/b"


def test_resolved_stream_request_headers_merge():
    stream = ResolvedStream(
        channel_id="1",
        url="https://cdn/a.m3u8",
        headers={"X-A": "1"},
        cookies={"sid": "abc"},
        referer="https://portal.example/",
        user_agent="UA/1.0",
    )
    headers = stream.request_headers()
    assert headers["Cookie"] == "sid=abc"
    assert headers["Referer"] == "https://portal.example/"
    assert headers["User-Agent"] == "UA/1.0"
    assert headers["X-A"] == "1"


def test_resolved_stream_masks_secrets_in_dict():
    stream = ResolvedStream(
        channel_id="1",
        url="https://cdn/a.m3u8?token=supersecret",
        headers={"Authorization": "Bearer supersecret"},
        cookies={"sid": "abc"},
    )
    masked = stream.as_dict()
    assert "supersecret" not in json.dumps(masked)
    revealed = stream.as_dict(reveal=True)
    assert "supersecret" in json.dumps(revealed)
