"""The IPTV provider and its config builder."""

from __future__ import annotations

import httpx
import pytest

from app.providers.base import ChannelInfo, ProviderAuthError
from app.providers.factory import ProviderFactory
from app.providers.iptv import IptvProvider


def test_registered():
    assert "iptv" in ProviderFactory.types()
    entry = next(t for t in ProviderFactory.available() if t["type"] == "iptv")
    assert entry["supports_auth"] is True
    assert entry["requires_channel_url"] is True


def test_build_config_without_login():
    cfg = IptvProvider.build_config(requires_login=False)
    assert cfg["auth"]["type"] == "none"
    assert cfg["stream"]["parser"] == "auto"
    assert cfg["stream"]["forward_cookies"] is True


def test_build_config_with_login():
    cfg = IptvProvider.build_config(
        requires_login=True,
        base_url="https://media.example.com",
        login_url="/signin",
        username_field="user",
        password_field="pass",
        url_path="data.stream.url",
    )
    assert cfg["auth"]["type"] == "form"
    assert cfg["auth"]["url"] == "/signin"
    assert cfg["auth"]["username_field"] == "user"
    assert cfg["base_url"] == "https://media.example.com"
    assert cfg["stream"]["url_path"] == "data.stream.url"


def build(handler, **config) -> IptvProvider:
    provider = IptvProvider(name="tv", config=config, secrets={"username": "u", "password": "p"})
    provider._client = httpx.AsyncClient(  # noqa: SLF001 - test seam
        transport=httpx.MockTransport(handler), follow_redirects=True
    )
    return provider


def ch(url: str) -> ChannelInfo:
    return ChannelInfo(id="82290", name="Sport Channel 01", metadata={"resolve_url": url})


async def test_resolves_per_channel_url_after_login(fixture_text):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/login":
            return httpx.Response(200, json={"ok": True}, headers={"set-cookie": "sid=1; Path=/"})
        if request.url.path == "/play":
            return httpx.Response(200, text=fixture_text("play_response.json"))
        return httpx.Response(404)

    provider = build(
        handler,
        base_url="https://media.example.com",
        auth={"type": "form", "url": "/login"},
        stream={"parser": "auto", "url_path": "data.stream.url"},
    )
    stream = await provider.resolve_stream(ch("https://media.example.com/play?id=82290"))
    assert "/login" in calls
    assert stream.url.startswith("https://edge-07.media.example/live/sport01/index.m3u8")
    await provider.aclose()


async def test_works_without_login():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"url": "https://cdn.example/a.m3u8"})

    provider = build(handler, auth={"type": "none"}, stream={"parser": "auto"})
    stream = await provider.resolve_stream(ch("https://media.example.com/play?id=1"))
    assert stream.url == "https://cdn.example/a.m3u8"
    await provider.aclose()


async def test_absolute_channel_url_ignores_base_url():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"url": "https://cdn.example/a.m3u8"})

    provider = build(handler, base_url="https://ignored.example", auth={"type": "none"})
    await provider.resolve_stream(ch("https://real.example/play?id=9"))
    assert any("real.example" in u for u in seen)
    assert not any("ignored.example" in u for u in seen)
    await provider.aclose()


async def test_no_discovery():
    provider = build(lambda r: httpx.Response(200), auth={"type": "none"})
    assert provider.supports_discovery is False
    await provider.aclose()


# --------------------------------------------------------------------------- #
# browser-like request headers (v1.2.3) - the reason a URL "vanished" for a
# plain client even though login and cookies were fine
# --------------------------------------------------------------------------- #
async def test_sends_browser_like_headers_by_default():
    provider = IptvProvider(name="t", config={"base_url": "https://media.example.com"})
    await provider.start()
    headers = provider._browser_headers()  # noqa: SLF001 - data-request headers
    assert headers.get("X-Requested-With") == "XMLHttpRequest"
    assert "Mozilla" in headers.get("User-Agent", "")
    assert headers.get("Accept", "").startswith("application/json")
    assert headers.get("Referer") == "https://media.example.com/"
    # login must stay clean: the client itself carries none of these
    assert provider._client.headers.get("X-Requested-With") is None  # noqa: SLF001
    await provider.aclose()


async def test_user_agent_and_referer_override():
    provider = IptvProvider(
        name="t",
        config={
            "base_url": "https://media.example.com",
            "user_agent": "MyPlayer/2.0",
            "referer": "https://portal.example/watch",
        },
    )
    await provider.start()
    headers = provider._browser_headers()  # noqa: SLF001
    assert headers.get("User-Agent") == "MyPlayer/2.0"
    assert headers.get("Referer") == "https://portal.example/watch"
    await provider.aclose()


async def test_resolve_requires_xhr_header_like_wms_portals(fixture_text):
    """A server that only returns JSON to an XHR request still resolves."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(200, headers={"set-cookie": "sid=1; Path=/"})
        # emulate: HTML unless the request looks like the browser's AJAX call
        if request.headers.get("x-requested-with") != "XMLHttpRequest":
            return httpx.Response(200, text="<html>player</html>",
                                  headers={"content-type": "text/html"})
        return httpx.Response(
            200,
            json={"status": "ok",
                  "file": "https://edge.example/live/a/playlist.m3u8?wmsAuthSign=abc",
                  "ep": ""},
        )

    provider = IptvProvider(
        name="t",
        config={"base_url": "https://media.example.com", "auth": {"type": "form", "url": "/login"}},
        secrets={"username": "u", "password": "p"},
    )
    await provider.start()
    # swap in the mock transport but KEEP the browser-like default headers
    provider._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://media.example.com",
        transport=httpx.MockTransport(handler),
        headers=dict(provider._client.headers),  # carry the defaults over
        follow_redirects=True,
    )
    stream = await provider.resolve_stream(
        ChannelInfo(id="1", name="F1", metadata={"resolve_url": "https://media.example.com/play?id=1"})
    )
    assert stream.url.endswith("playlist.m3u8?wmsAuthSign=abc")
    await provider.aclose()


async def test_login_request_stays_clean_so_it_is_not_rejected():
    """Login must NOT carry AJAX/Origin headers - some endpoints 403 on those."""
    import httpx

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            seen["xrw"] = request.headers.get("x-requested-with")
            seen["origin"] = request.headers.get("origin")
            # emulate a portal that rejects a login carrying an AJAX/Origin header
            if request.headers.get("x-requested-with") or request.headers.get("origin"):
                return httpx.Response(403)
            return httpx.Response(200, headers={"set-cookie": "sid=1; Path=/"})
        return httpx.Response(200, json={"file": "https://cdn.example/a.m3u8"})

    provider = IptvProvider(
        name="t",
        config={"base_url": "https://media.example.com", "auth": {"type": "form", "url": "/login"}},
        secrets={"username": "u", "password": "p"},
    )
    await provider.start()
    provider._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://media.example.com",
        transport=httpx.MockTransport(handler),
        headers=dict(provider._client.headers),  # minimal defaults, like real start()
        follow_redirects=True,
    )
    # login succeeds precisely because it sent no AJAX/Origin header
    stream = await provider.resolve_stream(
        ChannelInfo(id="1", name="A", metadata={"resolve_url": "https://media.example.com/play?id=1"})
    )
    assert seen["xrw"] is None
    assert seen["origin"] is None
    assert stream.url == "https://cdn.example/a.m3u8"
    await provider.aclose()


# --------------------------------------------------------------------------- #
# CSRF-primed login (v1.2.5) - the godtv flow: GET the login page, read the
# hidden csrf_tv_name, then POST it with the credentials
# --------------------------------------------------------------------------- #
async def test_csrf_primed_login_flow():
    import httpx

    state: dict[str, dict] = {}  # sid -> {csrf, auth}

    def sid_of(request: httpx.Request) -> str:
        cookie = request.headers.get("cookie", "")
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("PHPSESSID="):
                return part[len("PHPSESSID="):]
        return ""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/authen":
            sid = sid_of(request) or "sess1"
            state[sid] = {"csrf": "TOK_ABC123", "auth": False}
            html = (
                '<form method="post" action="/authen">'
                '<input type="hidden" name="csrf_tv_name" value="TOK_ABC123">'
                '<input name="username"><input type="password" name="password"></form>'
            )
            return httpx.Response(200, text=html,
                                  headers={"content-type": "text/html",
                                           "set-cookie": f"PHPSESSID={sid}; Path=/"})
        if request.method == "POST" and path == "/authen":
            sid = sid_of(request)
            sess = state.get(sid)
            body = request.content.decode()
            from urllib.parse import parse_qs
            form = {k: v[0] for k, v in parse_qs(body).items()}
            if (sess and form.get("csrf_tv_name") == sess["csrf"]
                    and form.get("username") == "ballthai07"
                    and form.get("password") == "iceza0251"):
                sess["auth"] = True
                return httpx.Response(302, headers={"location": "/"})
            # failed -> re-render the login form
            return httpx.Response(200, text='<form action="/authen"><input type="password"></form>',
                                  headers={"content-type": "text/html"})
        if path == "/":
            return httpx.Response(200, text="<html>home</html>",
                                  headers={"content-type": "text/html"})
        if path == "/play":
            sess = state.get(sid_of(request))
            if not sess or not sess.get("auth"):
                return httpx.Response(200, text='<form action="/authen"><input type="password"></form>',
                                      headers={"content-type": "text/html"})
            if request.headers.get("x-requested-with") != "XMLHttpRequest":
                return httpx.Response(200, text="<html>player</html>",
                                      headers={"content-type": "text/html"})
            return httpx.Response(200, json={"status": "ok",
                                             "file": "https://edge/live/a/playlist.m3u8?wmsAuthSign=xx",
                                             "ep": ""})
        return httpx.Response(404)

    cfg = IptvProvider.build_config(
        requires_login=True, base_url="https://godtv.vip", login_url="/authen",
        username_field="username", password_field="password",
    )
    assert cfg["auth"]["prime"] is True
    provider = IptvProvider(name="godtv", config=cfg,
                            secrets={"username": "ballthai07", "password": "iceza0251"})
    await provider.start()
    provider._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://godtv.vip",
        transport=httpx.MockTransport(handler),
        headers=dict(provider._client.headers),
        follow_redirects=True,
    )
    stream = await provider.resolve_stream(
        ChannelInfo(id="82290", name="F1",
                    metadata={"resolve_url": "https://godtv.vip/play?id=82290"})
    )
    assert stream.url.endswith("playlist.m3u8?wmsAuthSign=xx")
    await provider.aclose()


async def test_wrong_password_reports_login_form_bounce():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/authen":
            return httpx.Response(
                200,
                text='<input type="hidden" name="csrf_tv_name" value="T"><input type="password">',
                headers={"content-type": "text/html", "set-cookie": "PHPSESSID=s; Path=/"},
            )
        # any POST with wrong creds -> login form again
        return httpx.Response(200, text='<form><input type="password"></form>',
                              headers={"content-type": "text/html"})

    cfg = IptvProvider.build_config(requires_login=True, base_url="https://godtv.vip", login_url="/authen")
    provider = IptvProvider(name="godtv", config=cfg,
                            secrets={"username": "x", "password": "wrong"})
    await provider.start()
    provider._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://godtv.vip", transport=httpx.MockTransport(handler),
        headers=dict(provider._client.headers), follow_redirects=True,
    )
    with pytest.raises(ProviderAuthError):
        await provider.authenticate(force=True)
    await provider.aclose()


async def test_success_url_landing_is_the_login_signal():
    """When a success page is set, reaching it means success; bouncing to the
    login page means failure (the godtv /main behaviour)."""
    import httpx

    def make(land_on_main: bool):
        def handler(req: httpx.Request) -> httpx.Response:
            p = req.url.path
            if req.method == "GET" and p == "/authen":
                return httpx.Response(
                    200,
                    text='<input type="hidden" name="csrf_tv_name" value="T"><input type="password">',
                    headers={"content-type": "text/html", "set-cookie": "PHPSESSID=s; Path=/"},
                )
            if req.method == "POST" and p == "/authen":
                return httpx.Response(302, headers={"location": "/main" if land_on_main else "/"})
            if p == "/main":
                return httpx.Response(200, text="main", headers={"content-type": "text/html"})
            if p == "/":
                return httpx.Response(200, text='<form><input type="password"></form>',
                                      headers={"content-type": "text/html"})
            if p == "/play":
                return httpx.Response(200, json={"file": "https://cdn/a.m3u8"})
            return httpx.Response(404)
        return handler

    async def attempt(land: bool):
        cfg = IptvProvider.build_config(
            requires_login=True, base_url="https://godtv.vip",
            login_url="/authen", success_url="/main",
        )
        provider = IptvProvider(name="g", config=cfg, secrets={"username": "u", "password": "p"})
        await provider.start()
        provider._client = httpx.AsyncClient(  # noqa: SLF001
            base_url="https://godtv.vip", transport=httpx.MockTransport(make(land)),
            headers=dict(provider._client.headers), follow_redirects=True,
        )
        try:
            return await provider.authenticate(force=True)
        finally:
            await provider.aclose()

    assert await attempt(True) is True
    with pytest.raises(ProviderAuthError):
        await attempt(False)


# --------------------------------------------------------------------------- #
# the "Test login" endpoint (v1.2.6) - login-only, no URL fetch
# --------------------------------------------------------------------------- #
class _FakeProviders:
    def __init__(self, secrets=None):
        self._secrets = secrets or {}

    def secrets_for(self, provider_id):
        return self._secrets


class _FakeCtx:
    def __init__(self, secrets=None):
        self.providers = _FakeProviders(secrets)


async def test_prime_url_targets_the_form_page_not_the_post_url():
    """When the form is shown at /login-page but POSTs to /authen, priming the
    form page is what picks up csrf_tv_name."""
    import httpx

    primed_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if request.method == "GET" and p == "/login-page":
            primed_paths.append(p)
            return httpx.Response(
                200,
                text='<input type="hidden" name="csrf_tv_name" value="TOK">'
                '<input type="password">',
                headers={"content-type": "text/html", "set-cookie": "PHPSESSID=s; Path=/"},
            )
        if request.method == "POST" and p == "/authen":
            from urllib.parse import parse_qs

            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if form.get("csrf_tv_name") == "TOK":
                return httpx.Response(302, headers={"location": "/main"})
            return httpx.Response(200, text='<input type="password">',
                                  headers={"content-type": "text/html"})
        if p == "/main":
            return httpx.Response(200, text="welcome", headers={"content-type": "text/html"})
        return httpx.Response(404)

    cfg = IptvProvider.build_config(
        requires_login=True, base_url="https://godtv.vip",
        login_url="/authen", prime_url="/login-page", success_url="/main",
    )
    assert cfg["auth"]["prime_url"] == "/login-page"
    provider = IptvProvider(name="g", config=cfg, secrets={"username": "u", "password": "p"})
    await provider.start()
    provider._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://godtv.vip", transport=httpx.MockTransport(handler),
        headers=dict(provider._client.headers), follow_redirects=True,
    )
    assert await provider.authenticate(force=True) is True
    assert "/login-page" in primed_paths
    assert provider._login_debug["hidden_fields"] == ["csrf_tv_name"]  # noqa: SLF001
    await provider.aclose()


async def test_prime_falls_back_to_site_root_for_csrf():
    """godtv's real shape: the form + csrf_tv_name live on / but POST to /authen.
    With only the submit URL configured, priming must still find the token by
    falling back to the site root."""
    import httpx

    got: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if request.method == "GET" and p == "/authen":
            got.append("GET /authen")
            # the submit endpoint does not render the form
            return httpx.Response(404)
        if request.method == "GET" and p == "/":
            got.append("GET /")
            return httpx.Response(
                200,
                text='<form action="/authen" method="post">'
                '<input type="hidden" name="csrf_tv_name" value="ROOTTOK">'
                '<input name="username"><input type="password" name="password"></form>',
                headers={"content-type": "text/html", "set-cookie": "PHPSESSID=z; Path=/"},
            )
        if request.method == "POST" and p == "/authen":
            from urllib.parse import parse_qs

            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if form.get("csrf_tv_name") == "ROOTTOK":
                return httpx.Response(302, headers={"location": "/main"})
            return httpx.Response(200, text='<input type="password">',
                                  headers={"content-type": "text/html"})
        if p == "/main":
            return httpx.Response(200, text="ok", headers={"content-type": "text/html"})
        return httpx.Response(404)

    # Note: no prime_url configured - the root fallback must kick in.
    cfg = IptvProvider.build_config(
        requires_login=True, base_url="https://godtv.vip",
        login_url="/authen", success_url="/main",
    )
    provider = IptvProvider(name="godtv", config=cfg,
                            secrets={"username": "ballthai07", "password": "iceza0251"})
    await provider.start()
    provider._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://godtv.vip", transport=httpx.MockTransport(handler),
        headers=dict(provider._client.headers), follow_redirects=True,
    )
    assert await provider.authenticate(force=True) is True
    assert "GET /" in got  # it tried the root after /authen had no form
    assert provider._login_debug["hidden_fields"] == ["csrf_tv_name"]  # noqa: SLF001
    assert provider._login_debug["prime_source"].endswith("/")  # noqa: SLF001
    await provider.aclose()


async def test_soft_js_redirect_counts_as_success():
    """A portal that redirects with JavaScript keeps the URL on the login path
    but no longer shows the login form - that is a success, not a bounce."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if request.method == "GET" and p == "/authen":
            return httpx.Response(
                200,
                text='<input type="hidden" name="csrf_tv_name" value="T"><input type="password">',
                headers={"content-type": "text/html", "set-cookie": "PHPSESSID=s; Path=/"},
            )
        if request.method == "POST" and p == "/authen":
            # No HTTP redirect: return the authenticated page inline (JS would
            # have set window.location). Crucially, no login form in the body.
            return httpx.Response(
                200,
                text='<html><body>เข้าสู่ระบบสำเร็จ <script>window.location="/main"</script></body></html>',
                headers={"content-type": "text/html"},
            )
        return httpx.Response(404)

    cfg = IptvProvider.build_config(
        requires_login=True, base_url="https://godtv.vip",
        login_url="/authen", success_url="/main",
    )
    provider = IptvProvider(name="g", config=cfg, secrets={"username": "u", "password": "p"})
    await provider.start()
    provider._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://godtv.vip", transport=httpx.MockTransport(handler),
        headers=dict(provider._client.headers), follow_redirects=True,
    )
    assert await provider.authenticate(force=True) is True
    assert provider._login_debug["landed_on_success_url"] is False  # noqa: SLF001
    assert provider._login_debug["still_shows_login_form"] is False  # noqa: SLF001
    await provider.aclose()


async def test_test_login_endpoint_rejects_when_no_login():
    from app.web.api import api_iptv_test_login
    from app.web.schemas import IptvPreviewPayload

    r = await api_iptv_test_login(
        IptvPreviewPayload(requires_login=False), ctx=_FakeCtx()
    )
    assert r["ok"] is False
    assert r["logged_in"] is False


async def test_test_login_endpoint_needs_credentials():
    from app.web.api import api_iptv_test_login
    from app.web.schemas import IptvPreviewPayload

    r = await api_iptv_test_login(
        IptvPreviewPayload(requires_login=True, login_url="https://godtv.vip/authen"),
        ctx=_FakeCtx(),
    )
    assert r["ok"] is False
    assert "username" in r["error"]


async def test_test_login_endpoint_needs_a_login_url():
    from app.web.api import api_iptv_test_login
    from app.web.schemas import IptvPreviewPayload

    r = await api_iptv_test_login(
        IptvPreviewPayload(
            requires_login=True, login_url="", base_url="",
            username="u", password="p",
        ),
        ctx=_FakeCtx(),
    )
    assert r["ok"] is False
    assert "login" in r["error"].lower()
