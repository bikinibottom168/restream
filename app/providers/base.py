"""Provider contract and the shared data model.

Every provider implements :class:`StreamProvider`.  The supervisor, the FFmpeg
manager, the health monitor and the dashboard depend on *this module only* -
never on a concrete provider - so a new provider is a single new file plus one
registration line in :mod:`app.providers.factory`.

    provider.authenticate()          -> establish a session (no-op for many)
    provider.list_channels()         -> [ChannelInfo, ...]
    provider.resolve_stream(channel) -> ResolvedStream
    provider.refresh_stream(channel) -> ResolvedStream   (bypasses any cache)
    provider.health()                -> ProviderHealth
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.core.security import mask_headers, mask_url_token


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
class ProviderError(RuntimeError):
    """Base class for provider failures."""


class ProviderAuthError(ProviderError):
    """Authentication failed, or the session was rejected (401/403/login)."""


class ProviderUnavailable(ProviderError):
    """The provider answered but produced no usable stream."""


class ProviderUnsupportedMedia(ProviderError):
    """The media needs DRM/licence handling this application does not implement."""


class ProviderConfigError(ProviderError):
    """The stored provider configuration is incomplete or invalid."""


class DiscoveryNotSupported(ProviderError):
    """This provider cannot enumerate channels."""


# --------------------------------------------------------------------------- #
# data model
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ChannelInfo:
    """A channel as the provider sees it.

    ``id`` is the provider-side identifier (what gets stored in
    ``channels.provider_ref``), not the local database primary key.
    """

    id: str
    name: str
    logo: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    #: Local database id, populated when the channel exists locally.
    local_id: int | None = None

    @classmethod
    def from_model(cls, channel: Any) -> "ChannelInfo":
        """Build a :class:`ChannelInfo` from a ``Channel`` ORM row."""
        return cls(
            id=(channel.provider_ref or "").strip() or str(channel.id),
            name=channel.name,
            logo=channel.logo_url or "",
            local_id=channel.id,
            metadata={
                "input_url": channel.input_url or "",
                "resolve_url": getattr(channel, "resolve_url", "") or "",
                "group_title": channel.group_title or "",
                "external_id": channel.external_id or "",
                "last_source_url": channel.source_url or "",
            },
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "logo": self.logo,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class ResolvedStream:
    """A playable input, plus everything FFmpeg needs to fetch it.

    Some authorised sources only serve media when the request carries the same
    headers the player used - a ``Referer``, a session cookie, a specific
    ``User-Agent``.  All of that travels with the URL so
    :class:`~app.streaming.ffmpeg.FFmpegManager` can pass it through, and so it
    can be masked consistently in logs and in the dashboard.
    """

    channel_id: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    referer: str = ""
    user_agent: str = ""
    expires_at: datetime | None = None
    provider: str = ""
    note: str = ""

    # ------------------------------------------------------------------ #
    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) >= self.expires_at

    @property
    def is_http(self) -> bool:
        return self.url.lower().startswith(("http://", "https://"))

    def request_headers(self) -> dict[str, str]:
        """Every header that must accompany a fetch of :attr:`url`.

        Merges the explicit headers with the referer, user-agent and cookie
        jar.  Header keys already present win, so a provider can override.
        """
        headers: dict[str, str] = {}
        if self.referer:
            headers["Referer"] = self.referer
        if self.user_agent:
            headers["User-Agent"] = self.user_agent
        if self.cookies:
            headers["Cookie"] = "; ".join(
                f"{name}={value}" for name, value in self.cookies.items()
            )
        headers.update(self.headers)  # explicit headers take precedence
        return headers

    def safe_url(self) -> str:
        return mask_url_token(self.url)

    def as_dict(self, *, reveal: bool = False) -> dict[str, Any]:
        """Serialise for the API. Secrets are masked unless *reveal* is set."""
        return {
            "channel_id": self.channel_id,
            "url": self.url if reveal else mask_url_token(self.url),
            "headers": self.headers if reveal else mask_headers(self.headers),
            "cookies": (
                self.cookies if reveal else {k: "***" for k in self.cookies}
            ),
            "referer": self.referer,
            "user_agent": self.user_agent,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "provider": self.provider,
            "note": self.note,
        }


@dataclass(slots=True)
class ProviderHealth:
    """Outcome of :meth:`StreamProvider.health`."""

    ok: bool
    message: str = ""
    authenticated: bool = False
    channel_count: int | None = None
    details: dict[str, Any] = field(default_factory=dict)
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "authenticated": self.authenticated,
            "channel_count": self.channel_count,
            "details": self.details,
            "checked_at": self.checked_at.isoformat(),
        }


# --------------------------------------------------------------------------- #
# the contract
# --------------------------------------------------------------------------- #
class StreamProvider(abc.ABC):
    """Base class for every source of streams.

    Subclasses must set :attr:`type_name` and implement :meth:`resolve_stream`.
    Everything else has a working default.
    """

    #: Stable identifier stored in ``providers.type``.
    type_name: str = "base"
    #: Label shown in the dashboard's provider dropdown.
    label: str = "Base provider"
    #: Whether :meth:`list_channels` is implemented.
    supports_discovery: bool = False
    #: Whether :meth:`authenticate` does anything.
    supports_auth: bool = False
    #: Whether the operator supplies a URL per channel (manual/static styles).
    requires_channel_url: bool = False

    def __init__(
        self,
        *,
        provider_id: int | None = None,
        name: str = "",
        config: dict[str, Any] | None = None,
        secrets: dict[str, str] | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.name = name or self.label
        self.config: dict[str, Any] = dict(config or {})
        self._secrets: dict[str, str] = dict(secrets or {})
        self._authenticated = False

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Allocate resources (HTTP clients, caches). Called once."""
        return None

    async def aclose(self) -> None:
        """Release resources. Must never raise."""
        return None

    # ------------------------------------------------------------------ #
    # the five methods a provider offers
    # ------------------------------------------------------------------ #
    async def authenticate(self) -> bool:
        """Establish or renew a session.

        Providers that need no credentials return ``True`` immediately.
        Raise :class:`ProviderAuthError` when credentials are rejected - the
        caller must never retry blindly in that case.
        """
        self._authenticated = True
        return True

    async def list_channels(self) -> list[ChannelInfo]:
        """Return the channels this provider currently offers."""
        raise DiscoveryNotSupported(
            f"provider '{self.type_name}' cannot list channels; add them manually"
        )

    @abc.abstractmethod
    async def resolve_stream(self, channel: ChannelInfo) -> ResolvedStream:
        """Return a playable stream for *channel*.

        Implementations may serve a cached URL as long as it has not expired.
        """

    async def refresh_stream(self, channel: ChannelInfo) -> ResolvedStream:
        """Force a brand-new resolution, ignoring any cache.

        The default simply calls :meth:`resolve_stream`; override when the
        provider keeps its own cache.
        """
        return await self.resolve_stream(channel)

    async def health(self) -> ProviderHealth:
        """Cheap liveness check used by the dashboard and ``/health``."""
        return ProviderHealth(
            ok=True, message="no health check implemented", authenticated=self._authenticated
        )

    # ------------------------------------------------------------------ #
    # helpers for subclasses
    # ------------------------------------------------------------------ #
    @property
    def authenticated(self) -> bool:
        return self._authenticated

    def secret(self, key: str, default: str = "") -> str:
        """Read a credential supplied by the secret store."""
        return self._secrets.get(key, default) or default

    def set_secrets(self, secrets: dict[str, str]) -> None:
        self._secrets = dict(secrets or {})

    def option(self, path: str, default: Any = None) -> Any:
        """Read ``config`` with a dotted path, e.g. ``option('auth.type')``."""
        node: Any = self.config
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node if node is not None else default

    def describe(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "name": self.name,
            "type": self.type_name,
            "label": self.label,
            "supports_discovery": self.supports_discovery,
            "supports_auth": self.supports_auth,
            "requires_channel_url": self.requires_channel_url,
            "authenticated": self._authenticated,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.__class__.__name__} id={self.provider_id} name={self.name!r}>"
