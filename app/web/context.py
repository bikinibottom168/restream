"""Application context: the object graph everything else hangs off.

Built once in the FastAPI lifespan and stored on ``app.state.ctx``.  Keeping
construction in one place makes the startup order explicit and gives shutdown a
single, ordered teardown path.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import setup_logging
from app.core.secrets import (
    ADMIN_PASSWORD_HASH,
    TELEGRAM_BOT_TOKEN,
    SecretStore,
)
from app.core.security import hash_password, register_secret, verify_password
from app.core.settings_store import SettingsStore
from app.core.timeutil import utcnow
from app.database import crud
from app.database.db import dispose_engine, init_db, run_db
from app.database.models import EventType
from app.notifications.telegram import TelegramNotifier
from app.providers.manager import ProviderManager
from app.providers.resolver import StreamResolver
from app.streaming.manager import StreamManager

logger = logging.getLogger(__name__)


class AppContext:
    """Owns every long-lived component of the application."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings: Settings = settings or get_settings()
        self.settings.ensure_dirs()
        setup_logging(self.settings.log_dir, self.settings.log_level)

        register_secret(self.settings.telegram_bot_token)
        register_secret(self.settings.admin_password)

        self.store = SettingsStore(self.settings)
        self.secrets = SecretStore(self.settings.data_dir / "secrets.json")
        self.providers = ProviderManager(self.store, self.secrets)
        self.resolver = StreamResolver(self.providers, self.store)
        self.notifier = TelegramNotifier(self.store, self.secrets)
        self.streams = StreamManager(
            settings=self.store,
            providers=self.providers,
            resolver=self.resolver,
            notifier=self.notifier,
            pid_dir=self.settings.pid_dir,
            ffmpeg_log_dir=self.settings.ffmpeg_log_dir,
            data_dir=self.settings.data_dir,
            log_dir=self.settings.log_dir,
        )
        self.started_at = utcnow()
        self.startup_errors: list[str] = []
        self._admin_hash: str = ""

    # ------------------------------------------------------------------ #
    # startup / shutdown
    # ------------------------------------------------------------------ #
    async def startup(self) -> None:
        """Ordered startup. A failure in one step never prevents the UI opening."""
        logger.info(
            "%s v%s starting", self.settings.app_name, self.settings.app_version
        )

        # 1. database
        init_db(self.settings.database_url)

        # 2. runtime settings from the database
        try:
            rows = await run_db(crud.all_settings)
            self.store.load(rows)
        except Exception as exc:  # noqa: BLE001
            logger.exception("could not load stored settings")
            self.startup_errors.append(f"settings: {exc}")

        # 3. interface language (imported here: deps imports this module)
        from app.web.deps import set_template_language

        language = set_template_language(self.store.get_str("ui_language"))
        self.store.add_listener(
            lambda key, value: set_template_language(str(value))
            if key == "ui_language"
            else None
        )
        logger.info("interface language: %s", language)

        # 4. dashboard credentials
        self._load_admin_hash()

        # 5. providers (never fatal - the dashboard must still open)
        try:
            await self.providers.start()
        except Exception as exc:  # noqa: BLE001
            logger.exception("provider startup failed")
            self.startup_errors.append(f"providers: {exc}")

        # 6. ffmpeg, orphan recovery, auto-start, watchdog
        try:
            await self.streams.start()
        except Exception as exc:  # noqa: BLE001
            logger.exception("stream manager startup failed")
            self.startup_errors.append(f"streams: {exc}")

        if not self.streams.ffmpeg_info.available:
            self.startup_errors.append(self.streams.ffmpeg_info.error or "ffmpeg missing")
        if not self.streams.ffprobe_info.available:
            self.startup_errors.append(self.streams.ffprobe_info.error or "ffprobe missing")

        logger.info(
            "dashboard ready on http://%s:%s",
            self.settings.app_host,
            self.settings.app_port,
        )

    async def shutdown(self) -> None:
        """Ordered teardown: streams, providers, notifier, database."""
        logger.info("shutting down")
        try:
            await self.streams.shutdown()
        except Exception:  # noqa: BLE001
            logger.exception("error stopping streams")
        try:
            await self.providers.aclose()
        except Exception:  # noqa: BLE001
            logger.exception("error closing providers")
        try:
            await self.notifier.aclose()
        except Exception:  # noqa: BLE001
            logger.exception("error closing notifier")
        try:
            dispose_engine()
        except Exception:  # noqa: BLE001
            logger.exception("error disposing database engine")
        logger.info("shutdown complete")

    # ------------------------------------------------------------------ #
    # dashboard authentication
    # ------------------------------------------------------------------ #
    def _load_admin_hash(self) -> None:
        stored = self.secrets.get(ADMIN_PASSWORD_HASH)
        env_password = self.settings.admin_password
        if env_password:
            # An env password always wins, and is hashed - never stored plainly.
            self._admin_hash = hash_password(env_password)
            return
        self._admin_hash = stored

    @property
    def auth_enabled(self) -> bool:
        return bool(self.settings.admin_username and self._admin_hash)

    def check_credentials(self, username: str, password: str) -> bool:
        if not self.auth_enabled:
            return True
        if username != self.settings.admin_username:
            return False
        return verify_password(password, self._admin_hash)

    def set_admin_password(self, password: str) -> None:
        digest = hash_password(password)
        self.secrets.set(ADMIN_PASSWORD_HASH, digest)
        self._admin_hash = digest

    # ------------------------------------------------------------------ #
    # secrets used by the settings page
    # ------------------------------------------------------------------ #
    def set_telegram_token(self, token: str) -> None:
        self.secrets.set(TELEGRAM_BOT_TOKEN, token)
        register_secret(token)

    def has_telegram_token(self) -> bool:
        return bool(self.notifier.token)

    # ------------------------------------------------------------------ #
    async def persist_setting(self, key: str, value: Any) -> Any:
        """Validate, apply and store one runtime setting."""
        coerced = self.store.set(key, value)
        await run_db(crud.set_setting, key, self.store.serialize(key))
        await run_db(
            crud.add_event,
            event_type=EventType.CONFIG_CHANGED,
            message=f"{key} changed",
        )
        return coerced

    def health(self) -> dict[str, Any]:
        """Payload for ``GET /health``."""
        snapshots = self.streams.snapshots()
        online = sum(1 for s in snapshots.values() if s.get("state") == "ONLINE")
        return {
            "status": "ok" if self.streams.ffmpeg_info.available else "degraded",
            "version": self.settings.app_version,
            "ffmpeg": self.streams.ffmpeg_info.available,
            "ffprobe": self.streams.ffprobe_info.available,
            "providers_loaded": len(self.providers.all()),
            "telegram": self.notifier.configured,
            "channels_online": online,
            "started_at": self.started_at.isoformat(),
        }
