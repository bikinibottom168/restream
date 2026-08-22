"""Provider factory and registry of provider classes.

    provider = ProviderFactory.create("http_json", config=..., secrets=...)

Adding a provider takes two lines: import the class and call
:meth:`ProviderFactory.register`.  Modules dropped into ``app/providers/custom/``
are imported and registered automatically at startup - see
``docs/CREATE_PROVIDER.md``.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from pathlib import Path
from typing import Any

import httpx

from app.providers.base import ProviderConfigError, StreamProvider
from app.providers.http_json import HttpJsonProvider
from app.providers.iptv import IptvProvider
from app.providers.manual import ManualProvider
from app.providers.static_m3u8 import StaticM3U8Provider
from app.providers.url_endpoint import UrlEndpointProvider

logger = logging.getLogger(__name__)


class ProviderFactory:
    """Create provider instances from a type name."""

    _registry: dict[str, type[StreamProvider]] = {}

    # ------------------------------------------------------------------ #
    @classmethod
    def register(cls, provider_cls: type[StreamProvider]) -> type[StreamProvider]:
        """Register a provider class. Usable as a decorator."""
        type_name = getattr(provider_cls, "type_name", "")
        if not type_name or type_name == "base":
            raise ValueError(
                f"{provider_cls.__name__} must define a unique 'type_name'"
            )
        if type_name in cls._registry and cls._registry[type_name] is not provider_cls:
            logger.warning("provider type %r is being replaced", type_name)
        cls._registry[type_name] = provider_cls
        return provider_cls

    @classmethod
    def get_class(cls, type_name: str) -> type[StreamProvider]:
        provider_cls = cls._registry.get((type_name or "").strip())
        if provider_cls is None:
            raise ProviderConfigError(
                f"unknown provider type {type_name!r}; available: "
                + ", ".join(sorted(cls._registry))
            )
        return provider_cls

    @classmethod
    def create(
        cls,
        type_name: str,
        *,
        provider_id: int | None = None,
        name: str = "",
        config: dict[str, Any] | None = None,
        secrets: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> StreamProvider:
        """Instantiate a provider of *type_name*."""
        provider_cls = cls.get_class(type_name)
        kwargs: dict[str, Any] = {
            "provider_id": provider_id,
            "name": name,
            "config": config or {},
            "secrets": secrets or {},
        }
        # Providers that share the application-wide HTTP client declare a
        # 'client' parameter; providers that own their session do not.
        signature = inspect.signature(provider_cls.__init__)
        if "client" in signature.parameters and client is not None:
            kwargs["client"] = client
        return provider_cls(**kwargs)

    # ------------------------------------------------------------------ #
    @classmethod
    def available(cls) -> list[dict[str, Any]]:
        """Descriptors for the dashboard's provider-type dropdown."""
        entries: list[dict[str, Any]] = []
        for type_name, provider_cls in sorted(cls._registry.items()):
            schema_fn = getattr(provider_cls, "config_schema", None)
            schema = schema_fn() if callable(schema_fn) else []
            entries.append(
                {
                    "type": type_name,
                    "label": getattr(provider_cls, "label", type_name),
                    "supports_auth": getattr(provider_cls, "supports_auth", False),
                    "requires_channel_url": getattr(
                        provider_cls, "requires_channel_url", False
                    ),
                    "schema": schema,
                    "doc": (provider_cls.__doc__ or "").strip().splitlines()[0]
                    if provider_cls.__doc__
                    else "",
                }
            )
        return entries

    @classmethod
    def types(cls) -> list[str]:
        return sorted(cls._registry)

    @classmethod
    def config_schema(cls, type_name: str) -> list[dict[str, Any]]:
        provider_cls = cls.get_class(type_name)
        schema_fn = getattr(provider_cls, "config_schema", None)
        return schema_fn() if callable(schema_fn) else []


# --------------------------------------------------------------------------- #
# built-ins
# --------------------------------------------------------------------------- #
ProviderFactory.register(IptvProvider)
ProviderFactory.register(ManualProvider)
ProviderFactory.register(StaticM3U8Provider)
ProviderFactory.register(UrlEndpointProvider)
ProviderFactory.register(HttpJsonProvider)


def load_custom_providers(package: str = "app.providers.custom") -> list[str]:
    """Import every module in *package* and register the providers it defines.

    Lets an operator drop ``my_provider.py`` into ``app/providers/custom/``
    without editing any existing file.
    """
    loaded: list[str] = []
    try:
        module = importlib.import_module(package)
    except ModuleNotFoundError:
        return loaded

    search_paths = [Path(p) for p in getattr(module, "__path__", [])]
    if not search_paths:
        return loaded

    for module_info in pkgutil.iter_modules([str(path) for path in search_paths]):
        if module_info.name.startswith("_"):
            continue
        full_name = f"{package}.{module_info.name}"
        try:
            submodule = importlib.import_module(full_name)
        except Exception:  # noqa: BLE001 - a broken custom file must not stop startup
            logger.exception("could not import custom provider %s", full_name)
            continue
        for _, member in inspect.getmembers(submodule, inspect.isclass):
            if (
                issubclass(member, StreamProvider)
                and member is not StreamProvider
                and getattr(member, "type_name", "base") != "base"
                and member.__module__ == full_name
            ):
                try:
                    ProviderFactory.register(member)
                    loaded.append(member.type_name)
                    logger.info("registered custom provider %r", member.type_name)
                except ValueError:
                    logger.exception("could not register %s", member.__name__)
    return loaded
