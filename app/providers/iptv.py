"""IPTV provider - the friendly, fill-in-the-blanks source.

Same machinery as :class:`~app.providers.http_json.HttpJsonProvider`, but aimed
at the common case people actually have:

* optional form login (username + password), done once and reused, and
* a list of channels where each one carries its own URL and its own name -
  ``https://media.example.com/play?id=82290`` and so on.

There is no channel-list endpoint and no URL template to configure: you paste
one URL per channel and name each one yourself, as many as you like on a single
provider. The dashboard offers a dedicated "IPTV" form that writes exactly the
configuration this class expects, so an operator never has to think in terms of
JSON paths and auth types unless they want to.

Everything else - logging in once for all channels, re-logging in when the
session expires, refusing DRM-protected media, masking tokens - is inherited
unchanged. Nothing here logs in on your behalf to a system you do not control;
it performs the same authorised request your own player would, with the
credentials you enter.
"""

from __future__ import annotations

from typing import Any

from app.providers.http_json import AUTH_TYPES, PARSERS, HttpJsonProvider


class IptvProvider(HttpJsonProvider):
    """Form login + one named URL per channel."""

    type_name = "iptv"
    label = "IPTV"
    supports_auth = True
    #: Every channel supplies its own URL.
    requires_channel_url = True

    @property
    def supports_discovery(self) -> bool:  # type: ignore[override]
        # Channels are entered by hand, never discovered from a list endpoint.
        return False

    @staticmethod
    def config_schema() -> list[dict[str, Any]]:
        """A short schema for the generic editor.

        The dashboard normally uses the dedicated IPTV form instead, but this
        keeps the provider editable the ordinary way too.
        """
        return [
            {
                "key": "auth.type",
                "label": "Authentication",
                "type": "choice",
                "choices": list(AUTH_TYPES),
                "default": "none",
                "help": "Pick 'form' to log in with a username and password, or 'none'.",
            },
            {
                "key": "base_url",
                "label": "Base URL (optional)",
                "type": "url",
                "placeholder": "https://media.example.com",
                "help": "Only needed if the channel URLs and login URL are relative paths.",
            },
            {
                "key": "auth.url",
                "label": "Login URL",
                "type": "text",
                "placeholder": "/login",
                "help": "The page that accepts the username and password.",
            },
            {
                "key": "auth.username_field",
                "label": "Username field",
                "type": "text",
                "default": "username",
            },
            {
                "key": "auth.password_field",
                "label": "Password field",
                "type": "text",
                "default": "password",
            },
            {
                "key": "stream.url_path",
                "label": "Stream URL JSON path (optional)",
                "type": "text",
                "placeholder": "data.stream.url",
                "help": "Where the media URL sits in the response. Leave empty to auto-detect.",
            },
            {
                "key": "stream.parser",
                "label": "Response parser",
                "type": "choice",
                "choices": list(PARSERS),
                "default": "auto",
            },
        ]

    @staticmethod
    def build_config(
        *,
        requires_login: bool,
        base_url: str = "",
        login_url: str = "/login",
        username_field: str = "username",
        password_field: str = "password",
        login_method: str = "POST",
        login_encoding: str = "form",
        success_url: str = "",
        prime_url: str = "",
        url_path: str = "",
        parser: str = "auto",
    ) -> dict[str, Any]:
        """Assemble the nested config dict from the friendly form's flat fields."""
        return {
            "base_url": base_url.strip(),
            "auth": {
                "type": "form" if requires_login else "none",
                "url": login_url.strip() or "/login",
                "method": (login_method or "POST").upper(),
                "encoding": (login_encoding or "form").lower(),
                "username_field": username_field.strip() or "username",
                "password_field": password_field.strip() or "password",
                # Optional page a successful login lands on (e.g. /main). When
                # set it is the definitive success signal.
                "success_url": success_url.strip(),
                # Optional page that actually renders the login form, when it is
                # not the same URL the form POSTs to. The prime GET fetches this
                # to pick up the session cookie and the hidden CSRF field (e.g.
                # godtv's csrf_tv_name). Falls back to the login URL.
                "prime_url": prime_url.strip(),
                # Load the login page first to pick up its session cookie and any
                # hidden CSRF field (e.g. godtv's csrf_tv_name), the way a browser
                # does, then submit. Without this many logins never bind.
                "prime": True,
            },
            "stream": {
                "parser": parser if parser in PARSERS else "auto",
                "url_path": url_path.strip(),
                "forward_cookies": True,
            },
        }
