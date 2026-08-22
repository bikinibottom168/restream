"""Secret storage.

Preference order for every secret:

1. the OS keychain (macOS Keychain / Windows Credential Manager) via ``keyring``
2. an environment variable / ``.env`` entry
3. ``data/secrets.json``, created with ``0600`` permissions, used only when
   ``keyring`` is unavailable

Secrets are never written to the SQLite database and never returned to the
dashboard in plaintext.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

from app.core.security import register_secret

logger = logging.getLogger(__name__)

SERVICE_NAME = "restream-manager"

#: Keys handled by this store.
TELEGRAM_BOT_TOKEN = "telegram_bot_token"
SOURCE_API_HEADERS = "source_api_headers"
ADMIN_PASSWORD_HASH = "admin_password_hash"

KNOWN_KEYS = frozenset({TELEGRAM_BOT_TOKEN, SOURCE_API_HEADERS, ADMIN_PASSWORD_HASH})

try:  # pragma: no cover - depends on the host
    import keyring
    from keyring.errors import KeyringError

    _KEYRING_AVAILABLE = True
except Exception:  # pragma: no cover - keyring not installed or no backend
    keyring = None  # type: ignore[assignment]
    KeyringError = Exception  # type: ignore[misc,assignment]
    _KEYRING_AVAILABLE = False


class SecretStore:
    """Read/write secrets, keyring first with an encrypted-at-rest fallback."""

    def __init__(self, fallback_path: Path, use_keyring: bool = True) -> None:
        self._path = fallback_path
        self._use_keyring = use_keyring and _KEYRING_AVAILABLE and self._keyring_works()
        self._cache: dict[str, str] = {}
        self._load_fallback()

    # ------------------------------------------------------------------ #
    @property
    def backend(self) -> str:
        return "keyring" if self._use_keyring else "file"

    @staticmethod
    def _keyring_works() -> bool:
        if not _KEYRING_AVAILABLE:
            return False
        try:  # a backend may be installed but non-functional (headless Linux)
            keyring.get_password(SERVICE_NAME, "__probe__")
            return True
        except Exception as exc:  # pragma: no cover - environment dependent
            logger.info("keyring backend unusable (%s); using file fallback", exc)
            return False

    def _load_fallback(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("could not read secrets file: %s", exc)
            return
        if isinstance(raw, dict):
            self._cache = {str(k): str(v) for k, v in raw.items()}
            for value in self._cache.values():
                register_secret(value)

    def _write_fallback(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._cache, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:  # pragma: no cover - Windows may refuse
            pass
        tmp.replace(self._path)

    # ------------------------------------------------------------------ #
    def get(self, key: str, env_fallback: str | None = None) -> str:
        """Return a secret, falling back to the environment value."""
        if self._use_keyring:
            try:
                value = keyring.get_password(SERVICE_NAME, key)
                if value:
                    register_secret(value)
                    return value
            except KeyringError as exc:  # pragma: no cover
                logger.warning("keyring read failed for %s: %s", key, exc)
        value = self._cache.get(key, "")
        if value:
            return value
        if env_fallback:
            register_secret(env_fallback)
            return env_fallback
        return ""

    def set(self, key: str, value: str) -> None:
        """Store a secret. An empty value deletes it."""
        if not value:
            self.delete(key)
            return
        register_secret(value)
        if self._use_keyring:
            try:
                keyring.set_password(SERVICE_NAME, key, value)
                self._cache.pop(key, None)
                self._write_fallback()
                return
            except KeyringError as exc:  # pragma: no cover
                logger.warning("keyring write failed for %s: %s", key, exc)
        self._cache[key] = value
        self._write_fallback()

    def delete(self, key: str) -> None:
        if self._use_keyring:
            try:
                keyring.delete_password(SERVICE_NAME, key)
            except Exception:  # noqa: BLE001 - deleting a missing key is fine
                logger.debug("no keyring entry to delete for %s", key)
        if key in self._cache:
            self._cache.pop(key, None)
            self._write_fallback()

    def has(self, key: str, env_fallback: str | None = None) -> bool:
        return bool(self.get(key, env_fallback))
