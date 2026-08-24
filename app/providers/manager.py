"""Provider lifecycle management.

Owns one live :class:`~app.providers.base.StreamProvider` instance per
configured provider row, plus a shared HTTP client for providers that do not
keep their own session.  The rest of the application asks this manager for the
provider of a channel and never constructs one itself.

Concurrency note: thirty channels share these instances, so an ``http_json``
provider logs in once and every channel reuses that session.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.core.secrets import SecretStore
from app.core.settings_store import SettingsStore
from app.database import crud
from app.database.db import run_db
from app.providers.base import ProviderConfigError, StreamProvider
from app.providers.factory import ProviderFactory, load_custom_providers
from app.providers.manual import ManualProvider

logger = logging.getLogger(__name__)

#: Secret keys stored per provider.
PROVIDER_SECRET_KEYS = ("username", "password", "token", "cookie")


def secret_key(provider_id: int, name: str) -> str:
    return f"provider:{provider_id}:{name}"


class ProviderManager:
    """Build, cache and dispose provider instances."""

    def __init__(self, settings: SettingsStore, secrets: SecretStore) -> None:
        self._settings = settings
        self._secrets = secrets
        self._client: httpx.AsyncClient | None = None
        self._instances: dict[int, StreamProvider] = {}
        self._fallback = ManualProvider(name="Manual (built-in)")
        self._lock = asyncio.Lock()
        self._custom_types: list[str] = []

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        self._custom_types = load_custom_providers()
        if self._client is None:
            limits = httpx.Limits(max_connections=40, max_keepalive_connections=20)
            timeout = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)
            self._client = httpx.AsyncClient(
                limits=limits,
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": self._settings.get_str("source_user_agent")},
            )
        await self.reload()

    async def aclose(self) -> None:
        async with self._lock:
            for provider in self._instances.values():
                await self._safe_close(provider)
            self._instances.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        logger.info("provider manager closed")

    @staticmethod
    async def _safe_close(provider: StreamProvider) -> None:
        try:
            await provider.aclose()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.exception("error closing provider %s", provider.name)

    # ------------------------------------------------------------------ #
    # secrets
    # ------------------------------------------------------------------ #
    def _cookie_path_for(self, provider_id: int) -> Any:
        """Per-provider file the login cookie jar is persisted to."""
        from pathlib import Path

        try:
            data_dir = Path(self._settings.env.data_dir)
        except Exception:  # noqa: BLE001 - never block provider build on this
            return None
        return data_dir / "cookies" / f"provider_{provider_id}.json"

    def secrets_for(self, provider_id: int) -> dict[str, str]:
        return {
            name: self._secrets.get(secret_key(provider_id, name))
            for name in PROVIDER_SECRET_KEYS
        }

    def set_secret(self, provider_id: int, name: str, value: str) -> None:
        if name not in PROVIDER_SECRET_KEYS:
            raise ValueError(f"unknown provider secret {name!r}")
        self._secrets.set(secret_key(provider_id, name), value)
        instance = self._instances.get(provider_id)
        if instance is not None:
            instance.set_secrets(self.secrets_for(provider_id))

    def has_secret(self, provider_id: int, name: str) -> bool:
        return bool(self._secrets.get(secret_key(provider_id, name)))

    def clear_secrets(self, provider_id: int) -> None:
        for name in PROVIDER_SECRET_KEYS:
            self._secrets.delete(secret_key(provider_id, name))

    # ------------------------------------------------------------------ #
    # loading
    # ------------------------------------------------------------------ #
    async def reload(self) -> None:
        """Rebuild every provider instance from the database."""
        rows = await run_db(crud.list_providers)
        async with self._lock:
            for provider in self._instances.values():
                await self._safe_close(provider)
            self._instances.clear()
            for row in rows:
                if not row.enabled:
                    continue
                instance = await self._build(row)
                if instance is not None:
                    self._instances[row.id] = instance
        logger.info("loaded %d provider(s)", len(self._instances))

    async def reload_one(self, provider_id: int) -> StreamProvider | None:
        row = await run_db(crud.get_provider, provider_id)
        async with self._lock:
            existing = self._instances.pop(provider_id, None)
            if existing is not None:
                await self._safe_close(existing)
            if row is None or not row.enabled:
                return None
            instance = await self._build(row)
            if instance is not None:
                self._instances[provider_id] = instance
            return instance

    async def _build(self, row: Any) -> StreamProvider | None:
        try:
            provider = ProviderFactory.create(
                row.type,
                provider_id=row.id,
                name=row.name,
                config=row.config,
                secrets=self.secrets_for(row.id),
                client=self._client,
                cookie_path=self._cookie_path_for(row.id),
            )
            await provider.start()
            return provider
        except ProviderConfigError as exc:
            logger.error("provider %s is misconfigured: %s", row.name, exc)
            await run_db(crud.set_provider_auth_state, row.id, ok=False, error=str(exc))
            return None
        except Exception as exc:  # noqa: BLE001 - one bad provider must not stop startup
            logger.exception("could not build provider %s", row.name)
            await run_db(
                crud.set_provider_auth_state, row.id, ok=False, error=str(exc)
            )
            return None

    # ------------------------------------------------------------------ #
    # lookup
    # ------------------------------------------------------------------ #
    def get(self, provider_id: int | None) -> StreamProvider | None:
        if provider_id is None:
            return None
        return self._instances.get(provider_id)

    def all(self) -> dict[int, StreamProvider]:
        return dict(self._instances)

    def for_channel(self, channel: Any) -> StreamProvider:
        """Return the provider a channel should use.

        Falls back to the built-in manual provider so a channel with a pasted
        URL keeps working even when no provider row exists yet.
        """
        provider = self.get(getattr(channel, "provider_id", None))
        if provider is not None:
            return provider
        if getattr(channel, "input_url", ""):
            return self._fallback
        if len(self._instances) == 1:
            return next(iter(self._instances.values()))
        return self._fallback

    async def build_preview(self, row: Any) -> StreamProvider:
        """Build a throwaway instance for a *Test* button, without caching it.

        The caller is responsible for closing it.
        """
        provider = ProviderFactory.create(
            row.type,
            provider_id=row.id,
            name=row.name,
            config=row.config,
            secrets=self.secrets_for(row.id) if row.id else {},
            client=self._client,
        )
        await provider.start()
        return provider

    # ------------------------------------------------------------------ #
    def describe(self) -> list[dict[str, Any]]:
        return [provider.describe() for provider in self._instances.values()]

    @property
    def custom_types(self) -> list[str]:
        return list(self._custom_types)

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("provider manager has not been started")
        return self._client
