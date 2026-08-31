"""Generic authenticated HTTP/JSON provider.

Everything is configuration - no endpoint, field name or payload shape is
hardcoded.  A provider row holds something like::

    {
      "base_url": "https://media.internal.example",
      "auth": {
        "type": "form",
        "url": "/login",
        "username_field": "username",
        "password_field": "password"
      },
      "channels": {
        "url": "/api/channels",
        "list_path": "data",
        "id_field": "id",
        "name_field": "name"
      },
      "stream": {
        "url": "/api/play?id={channel_id}",
        "url_path": "data.stream.url"
      }
    }

Supported authentication types: ``none``, ``basic``, ``bearer``, ``form``,
``cookie``, ``headers``.  Credentials live in the OS keychain (or the
permission-restricted secrets file), never in the database and never in a log.

Session handling: one :class:`httpx.AsyncClient` per provider keeps the cookie
jar.  A ``401``/``403``/login-redirect triggers exactly one re-authentication,
serialised by a lock so thirty channels cannot log in thirty times.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Iterable

import httpx

from app.core.security import mask_headers, mask_url_token
from app.providers.base import (
    ChannelInfo,
    DiscoveryNotSupported,
    ProviderAuthError,
    ProviderConfigError,
    ProviderHealth,
    ProviderUnavailable,
    ResolvedStream,
    StreamProvider,
)
from app.providers.extract import extract_stream_urls
from app.providers.jsonpath import find_list_of_objects, get_list, get_path, get_string
from app.providers.util import (
    guess_expiry,
    join_url,
    normalise_headers,
    preview_body,
    substitute,
)

logger = logging.getLogger(__name__)

AUTH_TYPES = ("none", "basic", "bearer", "form", "cookie", "headers")
PARSERS = ("auto", "json_path", "text", "location")

#: A recent desktop Chrome User-Agent, used by default so a request looks like
#: the browser's own AJAX call. Override per provider via the user_agent config.
DEFAULT_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

#: Secret keys this provider reads from the secret store.
SECRET_USERNAME = "username"
SECRET_PASSWORD = "password"
SECRET_TOKEN = "token"
SECRET_COOKIE = "cookie"

_LOGIN_HINTS = ("/login", "/signin", "/sign-in", "/auth/login", "/authen")

#: Shortest gap between two "the answer was empty, try logging in again"
#: refreshes. An empty answer is also what an off-air channel returns, and a
#: relay retries every few seconds, so without this a dead channel would hammer
#: the login endpoint all night.
QUIET_REAUTH_COOLDOWN_SECONDS = 120.0

#: Matches any <input ...> tag and any attribute inside it.
_INPUT_TAG_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(
    r"""([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.IGNORECASE
)
_META_CSRF_RE = re.compile(
    r"""<meta[^>]*\bname\s*=\s*["'](?P<name>csrf[_-]?token)["'][^>]*"""
    r"""\bcontent\s*=\s*["'](?P<value>[^"']*)["']""",
    re.IGNORECASE,
)


def _parse_input_attrs(tag: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in _ATTR_RE.finditer(tag):
        key = match.group(1).lower()
        value = match.group(2)
        if value is None:
            value = match.group(3)
        if value is None:
            value = match.group(4)
        attrs[key] = value or ""
    return attrs


def _extract_hidden_tokens(html_text: str) -> dict[str, str]:
    """Return every hidden ``<input>`` in the page as ``{name: value}``.

    This mirrors what a browser submits: all hidden form fields - CSRF tokens
    included, whatever they are named (``csrf_tv_name``, ``_token``, ...) - are
    carried into the login POST. Attribute order does not matter.
    """
    if not html_text:
        return {}
    fields: dict[str, str] = {}
    for tag in _INPUT_TAG_RE.findall(html_text):
        attrs = _parse_input_attrs(tag)
        if attrs.get("type", "").lower() != "hidden":
            continue
        name = attrs.get("name")
        if name and name not in fields:
            fields[name] = attrs.get("value", "")
    # A meta csrf-token is a common fallback when the token is not a hidden input.
    for match in _META_CSRF_RE.finditer(html_text):
        name = match.group("name")
        if name not in fields and match.group("value"):
            fields[name] = match.group("value")
    return fields


def _login_path_of(url: str) -> str:
    try:
        from urllib.parse import urlsplit

        return urlsplit(url).path.rstrip("/").lower()
    except ValueError:
        return ""


def _is_login_page(response: httpx.Response, login_path: str = "") -> bool:
    """True when a response is (still) the login page - a bounce."""
    final = str(response.url).lower()
    if login_path:
        lp = _login_path_of(login_path)
        if lp and lp in _login_path_of(final):
            return True
    if any(hint in final for hint in _LOGIN_HINTS):
        return True
    content_type = response.headers.get("content-type", "").lower()
    if "html" not in content_type and content_type:
        return False
    body = response.text[:4000].lower()
    return 'type="password"' in body or "type='password'" in body


def _has_login_form(response: httpx.Response) -> bool:
    """Content-only check: the body still renders a login form (password field).

    Used to judge whether a login POST failed. URL is not considered here
    because the POST target IS the login URL - only the *content* tells us
    whether we got the form back (failure) or moved past it (success).
    """
    content_type = response.headers.get("content-type", "").lower()
    if content_type and "html" not in content_type:
        return False
    # Scan the whole body (not just the head): a login form can sit in a modal
    # far down the page, and a false "no form" would read as a false success.
    body = response.text.lower()
    return 'type="password"' in body or "type='password'" in body


def _looks_like_login(response: httpx.Response) -> bool:
    """Heuristic for 'the server bounced us to a login form'."""
    return _is_login_page(response)


class HttpJsonProvider(StreamProvider):
    """Configurable HTTP/JSON provider with session management."""

    type_name = "http_json"
    label = "HTTP JSON API"
    supports_auth = True
    requires_channel_url = False

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._client: httpx.AsyncClient | None = None
        self._auth_lock = asyncio.Lock()
        self._auth_generation = 0
        self._last_error = ""
        #: When a quiet-session refresh last ran (see _retry_after_relogin).
        self._quiet_reauth_at = 0.0
        #: What the most recent form login actually did, for the "test login"
        #: button to show (names and URLs only - never a password value).
        self._login_debug: dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    # configuration accessors
    # ------------------------------------------------------------------ #
    @property
    def base_url(self) -> str:
        return str(self.option("base_url", "") or "").strip()

    @property
    def auth_type(self) -> str:
        value = str(self.option("auth.type", "none") or "none").strip().lower()
        return value if value in AUTH_TYPES else "none"

    @property
    def supports_discovery(self) -> bool:  # type: ignore[override]
        return bool(self.option("channels.url"))

    @property
    def timeout(self) -> float:
        try:
            return float(self.option("timeout_seconds", 20) or 20)
        except (TypeError, ValueError):
            return 20.0

    def _static_headers(self) -> dict[str, str]:
        """Headers attached to every API request."""
        headers = normalise_headers(self.option("headers"))
        if self.auth_type == "bearer":
            token = self.secret(SECRET_TOKEN)
            if token:
                name = str(self.option("auth.header_name", "Authorization"))
                fmt = str(self.option("auth.header_format", "Bearer {token}"))
                headers[name] = substitute(fmt, token=token)
        elif self.auth_type == "cookie":
            cookie = self.secret(SECRET_COOKIE)
            if cookie:
                headers["Cookie"] = cookie
        elif self.auth_type == "headers":
            headers.update(normalise_headers(self.option("auth.headers")))
            token = self.secret(SECRET_TOKEN)
            if token:
                name = str(self.option("auth.header_name", "Authorization"))
                fmt = str(self.option("auth.header_format", "{token}"))
                headers[name] = substitute(fmt, token=token)
        return headers

    def playback_headers(self) -> dict[str, str]:
        """Headers FFmpeg must send when fetching the media itself."""
        headers = normalise_headers(self.option("playback_headers"))
        if self.option("stream.forward_auth_headers", False):
            headers.update(self._static_headers())
        if self.option("stream.forward_cookies", True) and self._client is not None:
            jar = {c.name: c.value for c in self._client.cookies.jar}
            if jar:
                existing = headers.get("Cookie", "")
                joined = "; ".join(f"{k}={v}" for k, v in jar.items())
                headers["Cookie"] = f"{existing}; {joined}" if existing else joined
        return headers

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if self._client is not None:
            return
        verify = bool(self.option("verify_tls", True))
        limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)

        # Keep the client's default headers minimal so the LOGIN request stays
        # exactly as the site expects it. Some login endpoints reject a POST that
        # carries an Origin / X-Requested-With header (they read it as a
        # cross-site or bot request and answer 403), so those browser-like
        # headers are added only to the stream/channel requests - see
        # _browser_headers(), applied in _send() - never to the login.
        headers: dict[str, str] = {}
        user_agent = str(self.option("user_agent", "") or "")
        if user_agent:
            headers["User-Agent"] = user_agent

        self._client = httpx.AsyncClient(
            base_url=self.base_url or "",
            timeout=httpx.Timeout(self.timeout),
            verify=verify,
            limits=limits,
            headers=headers,
            follow_redirects=True,
        )
        # Reuse the login session from a previous run so the app does not have to
        # log in again on every restart (which looks suspicious to a portal).
        self._load_cookies()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._authenticated = False

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise ProviderConfigError("provider is not started; call start() first")
        return self._client

    # ------------------------------------------------------------------ #
    # cookie-jar persistence (so a restart does not force a fresh login)
    # ------------------------------------------------------------------ #
    def _load_cookies(self) -> None:
        path = self._cookie_path
        if path is None or self._client is None:
            return
        try:
            from pathlib import Path

            path = Path(path)
            if not path.is_file():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.debug("provider %s: could not load cookies: %s", self.name, exc)
            return
        loaded = 0
        for cookie in data if isinstance(data, list) else []:
            try:
                self._client.cookies.set(
                    cookie["name"],
                    cookie.get("value", ""),
                    domain=cookie.get("domain", "") or "",
                    path=cookie.get("path", "/") or "/",
                )
                loaded += 1
            except (KeyError, TypeError):
                continue
        if loaded:
            # Assume the restored session is still good; a stale one just bounces
            # to the login page on the first request and triggers one re-login.
            self._authenticated = True
            logger.info(
                "provider %s: restored %d cookie(s) from the last session",
                self.name,
                loaded,
            )

    def _save_cookies(self) -> None:
        path = self._cookie_path
        if path is None or self._client is None:
            return
        try:
            from pathlib import Path

            path = Path(path)
            jar = [
                {
                    "name": c.name,
                    "value": c.value or "",
                    "domain": c.domain or "",
                    "path": c.path or "/",
                }
                for c in self._client.cookies.jar
            ]
            if not jar:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(jar), encoding="utf-8")
            try:  # best-effort: keep session cookies private (POSIX)
                path.chmod(0o600)
            except OSError:  # pragma: no cover - Windows / unusual FS
                pass
        except (OSError, ValueError) as exc:
            logger.debug("provider %s: could not save cookies: %s", self.name, exc)

    # ------------------------------------------------------------------ #
    # authentication
    # ------------------------------------------------------------------ #
    async def authenticate(self, force: bool = False) -> bool:
        """Establish a session using the configured authentication type.

        Serialised with a lock: while one caller logs in, the others wait and
        then reuse the session instead of logging in again.
        """
        auth_type = self.auth_type
        if auth_type in ("none", "bearer", "cookie", "headers"):
            # Nothing to negotiate - credentials ride on every request.
            self._authenticated = True
            return True

        generation_before = self._auth_generation
        async with self._auth_lock:
            if not force and self._auth_generation != generation_before:
                return self._authenticated  # somebody else just logged in
            if not force and self._authenticated:
                return True

            if auth_type == "basic":
                username = self.secret(SECRET_USERNAME)
                password = self.secret(SECRET_PASSWORD)
                if not username:
                    raise ProviderAuthError("basic auth selected but no username is set")
                client = self._require_client()
                client.auth = httpx.BasicAuth(username, password)
                self._authenticated = True
                self._auth_generation += 1
                return True

            ok = await self._form_login()
            self._authenticated = ok
            self._auth_generation += 1
            if ok:
                # Persist the fresh session so the next app start reuses it.
                self._save_cookies()
            return ok

    async def _form_login(self) -> bool:
        client = self._require_client()
        login_path = str(self.option("auth.url", "") or "").strip()
        if not login_path:
            raise ProviderConfigError("form login selected but no login URL is configured")

        username = self.secret(SECRET_USERNAME)
        password = self.secret(SECRET_PASSWORD)
        if not username or not password:
            raise ProviderAuthError(
                "form login needs a username and password - set them on the provider page"
            )

        username_field = str(self.option("auth.username_field", "username"))
        password_field = str(self.option("auth.password_field", "password"))
        method = str(self.option("auth.method", "POST") or "POST").upper()
        encoding = str(self.option("auth.encoding", "form") or "form").lower()

        url = join_url(self.base_url, login_path)

        debug: dict[str, Any] = {
            "login_url": mask_url_token(url),
            "primed": False,
            "prime_final_url": "",
            "hidden_fields": [],
            "posted_fields": [],
            "post_status": None,
            "final_url": "",
            "cookies": 0,
        }
        self._login_debug = debug

        # Prime the session, the way a browser does: GET the login page first so
        # the server sets its session cookie (PHPSESSID etc.) BEFORE we post the
        # credentials, and so we can read any CSRF/hidden token out of the form.
        # Without this many sites accept the POST but never bind the session, so
        # a later request bounces straight back to the login page.
        csrf_fields: dict[str, str] = {}
        if bool(self.option("auth.prime", False)):
            # Fetch the login page like a browser navigation (HTML), so the form
            # and its hidden fields come back and the session cookie is seeded.
            prime_headers = {
                "User-Agent": DEFAULT_BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,th;q=0.8",
            }
            ua = str(self.option("user_agent", "") or "")
            if ua:
                prime_headers["User-Agent"] = ua

            explicit_prime = str(self.option("auth.prime_url", "") or "").strip()
            # Where to look for the login form and its hidden CSRF field:
            #   1. the page the operator named, or the login URL itself, then
            #   2. the site root - the login form very often lives on the home
            #      page even when it POSTs elsewhere (e.g. godtv shows it on /
            #      and submits to /authen). This fallback means an operator only
            #      has to enter the submit URL and it still finds csrf_tv_name.
            candidates: list[str] = [join_url(self.base_url, explicit_prime or login_path)]
            if not explicit_prime:
                from urllib.parse import urlsplit

                parts = urlsplit(url)
                if parts.scheme and parts.netloc:
                    candidates.append(f"{parts.scheme}://{parts.netloc}/")
                elif self.base_url:
                    candidates.append(join_url(self.base_url, "/"))

            tried: set[str] = set()
            for candidate in candidates:
                candidate = candidate.strip()
                if not candidate or candidate in tried:
                    continue
                tried.add(candidate)
                try:
                    prime = await client.get(
                        candidate, headers=prime_headers, follow_redirects=True
                    )
                except httpx.HTTPError as exc:
                    logger.debug(
                        "provider %s: could not prime %s: %s", self.name, candidate, exc
                    )
                    continue
                debug["primed"] = True
                debug["prime_final_url"] = mask_url_token(str(prime.url))
                found = _extract_hidden_tokens(prime.text)
                if found:
                    csrf_fields = found
                    debug["prime_source"] = mask_url_token(candidate)
                    logger.info(
                        "provider %s: primed login from %s, carried %d hidden field(s): %s",
                        self.name,
                        candidate,
                        len(found),
                        ", ".join(found),
                    )
                    break
            debug["hidden_fields"] = list(csrf_fields)

        payload: dict[str, Any] = {username_field: username, password_field: password}
        # Hidden/CSRF fields from the page come first; explicit config wins.
        payload.update(csrf_fields)
        extra = self.option("auth.extra_fields")
        if isinstance(extra, dict):
            payload.update({str(k): str(v) for k, v in extra.items()})
        debug["posted_fields"] = list(payload)

        kwargs: dict[str, Any] = {"headers": self._static_headers() or None}
        if encoding == "json":
            kwargs["json"] = payload
        else:
            kwargs["data"] = payload

        try:
            response = await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise ProviderAuthError(f"login request failed: {exc}") from exc

        accepted = self.option("auth.success_status") or [200, 201, 204, 302, 303]
        if isinstance(accepted, int):
            accepted = [accepted]
        if response.status_code not in set(int(code) for code in accepted):
            raise ProviderAuthError(
                f"login rejected with HTTP {response.status_code}"
            )

        failure_marker = str(self.option("auth.failure_text", "") or "").strip()
        if failure_marker and failure_marker.lower() in response.text.lower():
            raise ProviderAuthError("login page reported invalid credentials")

        # Optionally lift a token out of the login response.
        token_path = str(self.option("auth.token_path", "") or "").strip()
        if token_path:
            try:
                payload_json = response.json()
            except (json.JSONDecodeError, ValueError):
                payload_json = None
            token = get_string(payload_json, token_path) if payload_json is not None else ""
            if not token:
                raise ProviderAuthError(
                    f"login succeeded but no token was found at '{token_path}'"
                )
            self._secrets[SECRET_TOKEN] = token
            name = str(self.option("auth.header_name", "Authorization"))
            fmt = str(self.option("auth.header_format", "Bearer {token}"))
            client.headers[name] = substitute(fmt, token=token)

        cookie_count = len(client.cookies.jar)
        debug["post_status"] = response.status_code
        debug["final_url"] = mask_url_token(str(response.url))
        debug["cookies"] = cookie_count
        success_marker = str(self.option("auth.success_text", "") or "").strip()
        if success_marker and success_marker.lower() not in response.text.lower():
            raise ProviderAuthError("login response did not contain the expected marker")

        # If a "success page" is configured, use it as the primary signal: a
        # successful login lands there (e.g. godtv redirects to /main).
        success_url = str(self.option("auth.success_url", "") or "").strip()
        if success_url and not token_path:
            final_path = _login_path_of(str(response.url))
            want = _login_path_of(success_url)
            landed = bool(want and want in final_path)
            still_login = _has_login_form(response)
            debug["landed_on_success_url"] = landed
            debug["still_shows_login_form"] = still_login
            if landed:
                logger.info("provider %s: login landed on %s", self.name, success_url)
                return True
            if not still_login:
                # Some portals redirect with JavaScript (window.location=...)
                # instead of an HTTP redirect, so the URL stays on the login
                # path even though the login worked. If the response no longer
                # shows a login form, treat that as success.
                logger.info(
                    "provider %s: login left the login form (soft redirect) - "
                    "treating as success even though the URL is not %s",
                    self.name,
                    success_url,
                )
                return True
            raise ProviderAuthError(
                f"login did not reach the expected page ({success_url}) and the "
                "response still shows the login form - the username/password or "
                "the field names are likely wrong."
            )

        # Otherwise: if the POST just re-rendered the login FORM, the credentials
        # or the field names are wrong (content-based, since the POST URL is the
        # login URL either way).
        if not token_path and _has_login_form(response):
            raise ProviderAuthError(
                "login did not go through - the response still shows the login "
                "form. Check the username/password, and the field names in "
                "'Advanced login options'."
            )
        if not token_path and cookie_count == 0 and not success_marker:
            logger.warning(
                "provider %s: login returned no cookie and no token - the session "
                "may not be usable",
                self.name,
            )
        logger.info("provider %s authenticated (%d cookies)", self.name, cookie_count)
        return True

    # ------------------------------------------------------------------ #
    # request plumbing
    # ------------------------------------------------------------------ #
    async def _retry_after_relogin(
        self,
        what: str,
        url: str,
        *,
        method: str = "GET",
        body: Any = None,
    ) -> httpx.Response | None:
        """Log in again and repeat one request, for a session that died quietly.

        :meth:`_request` already recovers from the honest signals - a 401, a
        403, a bounce to the login form.  Not every site gives one.  This one
        answers an expired session with a perfectly ordinary ``200`` whose body
        simply has no stream in it, and because nothing looks wrong the session
        is never refreshed: the channel fails the same way for hours, and the
        only cure is opening the Providers page and pressing *Test login* -
        which does nothing more than force the login this method now does.

        Rate-limited: an empty answer is also what a genuinely off-air channel
        returns, and a relay retries every few seconds.  Without the cooldown a
        dead channel would hammer the login endpoint all night.
        """
        if self.auth_type not in ("form", "basic"):
            return None  # no session to refresh; the answer means what it says
        now = time.monotonic()
        if now - self._quiet_reauth_at < QUIET_REAUTH_COOLDOWN_SECONDS:
            return None
        self._quiet_reauth_at = now

        logger.info(
            "provider %s: %s came back empty - the session may have expired "
            "without saying so; logging in again and retrying",
            self.name,
            what,
        )
        self._authenticated = False
        try:
            if not await self.authenticate(force=True):
                return None
        except (ProviderAuthError, ProviderConfigError):
            raise
        except Exception:  # noqa: BLE001 - a failed refresh must not mask the real error
            logger.exception("provider %s: re-login failed", self.name)
            return None
        return await self._request(url, method=method, body=body, allow_reauth=False)

    async def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        body: Any = None,
        allow_reauth: bool = True,
    ) -> httpx.Response:
        client = self._require_client()
        if self.auth_type in ("form", "basic") and not self._authenticated:
            await self.authenticate()

        login_path = str(self.option("auth.url", "") or "").strip()
        generation = self._auth_generation
        response = await self._send(client, method, url, body)

        if response.status_code in (401, 403) or _is_login_page(response, login_path):
            if not allow_reauth:
                raise ProviderAuthError(
                    f"request to {mask_url_token(url)} bounced to the login page - "
                    "the session was not accepted"
                )
            logger.info(
                "provider %s: session appears expired (HTTP %s) - re-authenticating",
                self.name,
                response.status_code,
            )
            self._authenticated = False
            await self.authenticate(force=self._auth_generation == generation)
            response = await self._send(client, method, url, body)
            if response.status_code in (401, 403) or _is_login_page(response, login_path):
                raise ProviderAuthError(
                    "still bounced to the login page after re-authenticating - the "
                    "login did not establish a usable session (check credentials, "
                    "the login field names, or whether the site needs a CSRF token)"
                )

        if response.status_code >= 400:
            raise ProviderUnavailable(
                f"{mask_url_token(url)} answered HTTP {response.status_code}"
            )
        return response

    def _browser_headers(self) -> dict[str, str]:
        """Headers that make a stream/channel request look like the browser's
        own AJAX call. Applied to data requests only, never to login, because
        many portals serve their JSON only to an XHR-style request (and serve an
        HTML page to anything else). Config headers/user_agent override these.
        """
        headers: dict[str, str] = {
            "User-Agent": DEFAULT_BROWSER_UA,
            "Accept": "application/json, text/javascript, text/html, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.9,th;q=0.8",
            "X-Requested-With": "XMLHttpRequest",
        }
        user_agent = str(self.option("user_agent", "") or "")
        if user_agent:
            headers["User-Agent"] = user_agent
        referer = str(self.option("referer", "") or "").strip()
        if not referer and self.base_url:
            referer = self.base_url.rstrip("/") + "/"
        if referer:
            headers["Referer"] = referer
        return headers

    async def _send(
        self, client: httpx.AsyncClient, method: str, url: str, body: Any
    ) -> httpx.Response:
        # data requests carry the browser-like headers; config values win
        merged = self._browser_headers()
        merged.update(self._static_headers())
        kwargs: dict[str, Any] = {"headers": merged or None}
        if body is not None:
            if isinstance(body, (dict, list)):
                kwargs["json"] = body
            else:
                kwargs["content"] = body
        try:
            return await client.request(method.upper(), url, **kwargs)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(
                f"request to {mask_url_token(url)} failed: {exc}"
            ) from exc

    # ------------------------------------------------------------------ #
    # channels
    # ------------------------------------------------------------------ #
    async def list_channels(self) -> list[ChannelInfo]:
        path = str(self.option("channels.url", "") or "").strip()
        if not path:
            raise DiscoveryNotSupported(
                "no channels URL configured for this provider"
            )
        url = join_url(self.base_url, path)
        method = str(self.option("channels.method", "GET"))
        response = await self._request(url, method=method)
        try:
            return self._channels_from(response)
        except ProviderUnavailable:
            # Same quiet-expiry problem the stream resolve has: a stale session
            # can answer with a perfectly valid, perfectly empty payload.
            retry = await self._retry_after_relogin(
                "the channel list", url, method=method
            )
            if retry is None:
                raise
            return self._channels_from(retry)

    def _channels_from(self, response: httpx.Response) -> list[ChannelInfo]:
        """Turn one channel-list response into channels, or say why it cannot."""
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProviderUnavailable(
                "channel list endpoint did not return JSON"
            ) from exc

        list_path = str(self.option("channels.list_path", "") or "").strip()
        rows: Iterable[Any]
        if list_path:
            rows = get_list(payload, list_path)
            if not rows:
                raise ProviderUnavailable(
                    f"no list found at '{list_path}' in the channel list response"
                )
        else:
            rows = find_list_of_objects(payload, hint_keys=("id", "name"))
            if not rows:
                raise ProviderUnavailable(
                    "could not locate a channel array; set channels.list_path"
                )

        id_field = str(self.option("channels.id_field", "id"))
        name_field = str(self.option("channels.name_field", "name"))
        logo_field = str(self.option("channels.logo_field", "logo"))
        group_field = str(self.option("channels.group_field", "group"))

        channels: list[ChannelInfo] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            identifier = get_string(row, id_field)
            name = get_string(row, name_field) or identifier
            if not identifier or identifier in seen:
                continue
            seen.add(identifier)
            channels.append(
                ChannelInfo(
                    id=identifier,
                    name=name,
                    logo=get_string(row, logo_field),
                    metadata={
                        "group_title": get_string(row, group_field),
                        "external_id": identifier,
                    },
                )
            )
        if not channels:
            raise ProviderUnavailable(
                "channel list contained no usable entries - check "
                "channels.id_field and channels.name_field"
            )
        return channels

    # ------------------------------------------------------------------ #
    # stream resolution
    # ------------------------------------------------------------------ #
    async def resolve_stream(self, channel: ChannelInfo) -> ResolvedStream:
        # A per-channel endpoint always wins over the provider-wide template,
        # so channels that do not follow one pattern can each carry their own.
        per_channel = str(channel.metadata.get("resolve_url") or "").strip()
        if per_channel:
            url = join_url(self.base_url, per_channel)
        else:
            template = str(self.option("stream.url", "") or "").strip()
            if not template:
                raise ProviderConfigError(
                    "this provider has no stream resolve URL, and this channel has "
                    "no endpoint URL of its own"
                )
            if not channel.id:
                raise ProviderUnavailable("channel has no provider reference")
            url = join_url(
                self.base_url,
                substitute(template, channel_id=channel.id, ref=channel.id, id=channel.id),
            )
        body = self.option("stream.body")
        if isinstance(body, (dict, list)):
            body = json.loads(
                substitute(json.dumps(body), channel_id=channel.id, ref=channel.id)
            )

        response = await self._request(
            url, method=str(self.option("stream.method", "GET")), body=body
        )

        stream_url = self._parse_stream_url(response, channel)
        if not stream_url:
            retry = await self._retry_after_relogin(
                "the stream resolve",
                url,
                method=str(self.option("stream.method", "GET")),
                body=body,
            )
            if retry is not None:
                response = retry
                stream_url = self._parse_stream_url(response, channel)
        if not stream_url:
            raise ProviderUnavailable(
                "no stream URL found in the resolve response (a fresh login did "
                "not change that): check stream.url_path, switch the parser to "
                "'auto', or the channel may simply be off air"
            )

        expires_at = guess_expiry(stream_url)
        expires_path = str(self.option("stream.expires_path", "") or "").strip()
        if expires_path and not expires_at:
            expires_at = self._parse_expiry(response, expires_path)

        headers = self.playback_headers()
        headers_path = str(self.option("stream.headers_path", "") or "").strip()
        if headers_path:
            try:
                extra = get_path(response.json(), headers_path, None)
            except (json.JSONDecodeError, ValueError):
                extra = None
            headers.update(normalise_headers(extra))

        return ResolvedStream(
            channel_id=channel.id,
            url=stream_url,
            headers=headers,
            referer=str(self.option("referer", "") or ""),
            user_agent=str(self.option("user_agent", "") or ""),
            expires_at=expires_at,
            provider=self.type_name,
            note=f"resolved via {str(self.option('stream.parser', 'auto'))}",
        )

    def _parse_stream_url(self, response: httpx.Response, channel: ChannelInfo) -> str:
        parser = str(self.option("stream.parser", "auto") or "auto").lower()
        url_path = str(self.option("stream.url_path", "") or "").strip()
        text = response.text
        base = str(response.url)

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
                    return join_url(self.base_url, candidate)
            if parser == "json_path":
                return ""

        if parser == "text":
            candidate = text.strip().splitlines()[0].strip() if text.strip() else ""
            return candidate if candidate.lower().startswith("http") else ""

        # auto: the body may already be a manifest, or embed the URL anywhere
        if text[:512].lstrip().startswith(("#EXTM3U", "#EXT-X-")):
            return base
        candidates = extract_stream_urls(text, base_url=base)
        return candidates[0] if candidates else ""

    def _parse_expiry(self, response: httpx.Response, path: str) -> Any:
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError):
            return None
        raw = get_string(payload, path)
        if not raw:
            return None
        from datetime import datetime, timezone

        if raw.isdigit():
            number = int(raw)
            if number > 10**12:
                number //= 1000
            try:
                return datetime.fromtimestamp(number, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    async def refresh_stream(self, channel: ChannelInfo) -> ResolvedStream:
        """Ask the endpoint for a brand-new URL.

        Nothing is cached inside this provider, so a refresh is simply another
        authorised request - which is exactly how a short-lived signed URL is
        meant to be renewed.
        """
        return await self.resolve_stream(channel)

    # ------------------------------------------------------------------ #
    # diagnostics
    # ------------------------------------------------------------------ #
    async def health(self) -> ProviderHealth:
        try:
            await self.authenticate()
        except ProviderAuthError as exc:
            self._last_error = str(exc)
            return ProviderHealth(ok=False, message=str(exc), authenticated=False)
        except ProviderConfigError as exc:
            return ProviderHealth(ok=False, message=str(exc), authenticated=False)

        if not self.option("channels.url"):
            return ProviderHealth(
                ok=True, message="authenticated", authenticated=self._authenticated
            )
        try:
            channels = await self.list_channels()
        except (ProviderUnavailable, ProviderAuthError, DiscoveryNotSupported) as exc:
            return ProviderHealth(
                ok=False, message=str(exc), authenticated=self._authenticated
            )
        return ProviderHealth(
            ok=True,
            message=f"{len(channels)} channels available",
            authenticated=self._authenticated,
            channel_count=len(channels),
        )

    async def debug_report(self) -> dict[str, Any]:
        """Data for the dashboard's provider debug page (fully masked)."""
        report: dict[str, Any] = {
            "provider": self.name,
            "type": self.type_name,
            "base_url": self.base_url,
            "auth_type": self.auth_type,
            "request_headers": mask_headers(self._static_headers()),
            "steps": [],
        }

        # step 1 - authentication
        step: dict[str, Any] = {"name": "authenticate", "ok": False, "detail": ""}
        try:
            await self.authenticate(force=True)
            step["ok"] = True
            step["detail"] = f"auth type: {self.auth_type}"
        except (ProviderAuthError, ProviderConfigError) as exc:
            step["detail"] = str(exc)
        report["steps"].append(step)

        client = self._client
        report["cookies"] = (
            [{"name": c.name, "value": "***", "domain": c.domain} for c in client.cookies.jar]
            if client is not None
            else []
        )

        # step 2 - channel list
        if self.option("channels.url"):
            step = {"name": "list_channels", "ok": False, "detail": ""}
            try:
                url = join_url(self.base_url, str(self.option("channels.url")))
                response = await self._request(url)
                step["status"] = response.status_code
                step["content_type"] = response.headers.get("content-type", "")
                step["preview"] = preview_body(response.text)
                channels = await self.list_channels()
                step["ok"] = True
                step["detail"] = f"{len(channels)} channels parsed"
                report["channel_count"] = len(channels)
                report["sample_channels"] = [c.as_dict() for c in channels[:5]]
            except Exception as exc:  # noqa: BLE001 - diagnostics must not raise
                step["detail"] = str(exc)
            report["steps"].append(step)

        return report

    # ------------------------------------------------------------------ #
    @staticmethod
    def config_schema() -> list[dict[str, Any]]:
        return [
            {
                "key": "base_url",
                "label": "Base URL",
                "type": "url",
                "required": True,
                "placeholder": "https://media.internal.example.com",
                "help": "Scheme + host of your source. Every path below is relative to it.",
            },
            {
                "key": "timeout_seconds",
                "label": "Timeout (seconds)",
                "type": "number",
                "default": 20,
                "placeholder": "20",
            },
            {"key": "verify_tls", "label": "Verify TLS certificates", "type": "bool", "default": True},
            {
                "key": "auth.type",
                "label": "Authentication",
                "type": "choice",
                "choices": list(AUTH_TYPES),
                "help": "Pick 'none' if the endpoint needs no credentials.",
            },
            {
                "key": "auth.url",
                "label": "Login URL",
                "type": "text",
                "placeholder": "/login",
                "help": "Relative to the base URL. Only used by 'form' authentication.",
            },
            {"key": "auth.method", "label": "Login method", "type": "choice", "choices": ["POST", "GET"]},
            {"key": "auth.encoding", "label": "Login body", "type": "choice", "choices": ["form", "json"]},
            {
                "key": "auth.username_field",
                "label": "Username field",
                "type": "text",
                "default": "username",
                "placeholder": "username",
                "help": "The form field name your login page expects.",
            },
            {
                "key": "auth.password_field",
                "label": "Password field",
                "type": "text",
                "default": "password",
                "placeholder": "password",
            },
            {
                "key": "auth.token_path",
                "label": "Token JSON path",
                "type": "text",
                "placeholder": "data.token",
                "help": "Where to find a token in the login response, if it returns one.",
            },
            {
                "key": "auth.header_name",
                "label": "Auth header name",
                "type": "text",
                "default": "Authorization",
                "placeholder": "Authorization",
            },
            {
                "key": "auth.header_format",
                "label": "Auth header format",
                "type": "text",
                "default": "Bearer {token}",
                "placeholder": "Bearer {token}",
            },
            {
                "key": "auth.failure_text",
                "label": "Login failure marker",
                "type": "text",
                "placeholder": "Invalid username or password",
                "help": "Text that appears when login fails but the server still answers 200.",
            },
            {
                "key": "channels.url",
                "label": "Channels URL",
                "type": "text",
                "placeholder": "/api/channels",
                "help": "Leave empty if you add channels by hand. Fill it to enable Sync Channels.",
            },
            {
                "key": "channels.list_path",
                "label": "Channels JSON path",
                "type": "text",
                "placeholder": "data.list",
                "help": "Where the array of channels sits. Leave empty to auto-detect.",
            },
            {
                "key": "channels.id_field",
                "label": "Channel id field",
                "type": "text",
                "default": "id",
                "placeholder": "id",
            },
            {
                "key": "channels.name_field",
                "label": "Channel name field",
                "type": "text",
                "default": "name",
                "placeholder": "name",
            },
            {
                "key": "channels.logo_field",
                "label": "Channel logo field",
                "type": "text",
                "default": "logo",
                "placeholder": "logo",
            },
            {
                "key": "stream.url",
                "label": "Stream resolve URL",
                "type": "text",
                "placeholder": "/api/play?id={channel_id}",
                "help": "{channel_id} is replaced with the channel's provider id.",
            },
            {"key": "stream.method", "label": "Resolve method", "type": "choice", "choices": ["GET", "POST"]},
            {
                "key": "stream.parser",
                "label": "Response parser",
                "type": "choice",
                "choices": list(PARSERS),
                "help": "'auto' handles JSON, HTML and JavaScript. Use 'json_path' to be strict.",
            },
            {
                "key": "stream.url_path",
                "label": "Stream URL JSON path",
                "type": "text",
                "placeholder": "data.stream.url",
                "help": "Dotted path. Supports items[0].url and items[*].url.",
            },
            {
                "key": "stream.expires_path",
                "label": "Expiry JSON path",
                "type": "text",
                "placeholder": "data.expires_at",
                "help": "Optional. Unix timestamp or ISO date telling us when the URL dies.",
            },
            {"key": "stream.forward_cookies", "label": "Send session cookies to FFmpeg", "type": "bool", "default": True},
            {
                "key": "referer",
                "label": "Playback Referer",
                "type": "text",
                "placeholder": "https://media.internal.example.com/",
                "help": "Sent to FFmpeg when it fetches the media itself.",
            },
            {
                "key": "user_agent",
                "label": "User-Agent",
                "type": "text",
                "placeholder": "Mozilla/5.0 (compatible; RestreamManager/1.0)",
            },
            {
                "key": "headers",
                "label": "Extra API headers (JSON)",
                "type": "json",
                "placeholder": '{"X-Api-Key": "your-key"}',
                "help": "Sent with every API request. Masked everywhere it is displayed.",
            },
            {
                "key": "playback_headers",
                "label": "Playback headers (JSON)",
                "type": "json",
                "placeholder": '{"Referer": "https://media.internal.example.com/"}',
                "help": "Sent with the media request only, not with the API calls.",
            },
        ]
