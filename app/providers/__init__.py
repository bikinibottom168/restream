"""Pluggable stream providers.

Nothing outside this package knows where a stream URL comes from.  The
streaming layer only ever sees :class:`~app.providers.base.ResolvedStream`.

Built-in providers
------------------
``manual``       operator pastes a URL per channel
``static_m3u8``  fixed URL (optionally built from a base URL + path)
``http_json``    generic authenticated HTTP/JSON API

See ``docs/CREATE_PROVIDER.md`` to add your own.
"""

from app.providers.base import (  # noqa: F401
    ChannelInfo,
    ProviderAuthError,
    ProviderError,
    ProviderHealth,
    ProviderUnavailable,
    ProviderUnsupportedMedia,
    ResolvedStream,
    StreamProvider,
)
from app.providers.factory import ProviderFactory  # noqa: F401

__all__ = [
    "ChannelInfo",
    "ResolvedStream",
    "StreamProvider",
    "ProviderHealth",
    "ProviderError",
    "ProviderAuthError",
    "ProviderUnavailable",
    "ProviderUnsupportedMedia",
    "ProviderFactory",
]
