"""Telegram notifications with de-duplication.

The rule that matters: a message is sent on a *state transition*, never on a
timer.  A channel that has been down for two hours produces exactly one DOWN
message, and one RECOVERED message when it comes back - not one every health
check.

Only these events notify:

* ONLINE -> DOWN            (confirmed outage)
* DOWN   -> ONLINE          (recovery, with duration and attempt count)
* channel declared unstable (once per trip of the restart circuit)
* provider authentication failure (rate-limited)
* system error              (rate-limited)

Retries during an outage are deliberately silent; the dashboard already shows
them.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import Any

import httpx

from app.core.secrets import TELEGRAM_BOT_TOKEN, SecretStore
from app.core.security import mask_secret, scrub
from app.core.settings_store import SettingsStore
from app.core.timeutil import format_local, humanize_duration, utcnow

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"

#: Minimum seconds between two identical non-channel alerts.
ALERT_COOLDOWN_SECONDS = 900

#: Telegram hard-limits a message to 4096 characters.
MAX_MESSAGE_LENGTH = 4000


class TelegramNotifier:
    """Send alerts to a Telegram chat, once per state change."""

    def __init__(
        self,
        settings: SettingsStore,
        secrets: SecretStore,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._secrets = secrets
        self._client = client
        self._owns_client = client is None
        self._lock = asyncio.Lock()
        #: channel_id -> "up" | "down"
        self._channel_state: dict[int, str] = {}
        #: alert key -> monotonic timestamp of the last send
        self._last_alert: dict[str, float] = {}
        self.last_error = ""
        self.sent_count = 0

    # ------------------------------------------------------------------ #
    # configuration
    # ------------------------------------------------------------------ #
    @property
    def token(self) -> str:
        return self._secrets.get(
            TELEGRAM_BOT_TOKEN, env_fallback=self._settings.env.telegram_bot_token
        )

    @property
    def chat_id(self) -> str:
        return self._settings.get_str("telegram_chat_id")

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def describe(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "token": mask_secret(self.token) if self.token else "",
            "chat_id": self.chat_id,
            "sent_count": self.sent_count,
            "last_error": self.last_error,
        }

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
            self._owns_client = True
        return self._client

    # ------------------------------------------------------------------ #
    # transport
    # ------------------------------------------------------------------ #
    async def send(self, text: str) -> bool:
        """Scrub, truncate and deliver a message.

        Every notification goes through here, so registered secrets can never
        leak into a chat even if a caller passes a raw error string.
        """
        if not self.configured:
            logger.debug("telegram not configured - dropping notification")
            return False
        return await self._deliver(scrub(text)[:MAX_MESSAGE_LENGTH])

    async def _deliver(self, text: str) -> bool:
        """Perform the actual API call (overridden in tests)."""
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        url = f"{TELEGRAM_API}/bot{self.token}/sendMessage"
        try:
            async with self._lock:  # keep message order stable
                response = await self._http().post(url, json=payload)
        except httpx.HTTPError as exc:
            self.last_error = f"could not reach Telegram: {exc}"
            logger.warning("telegram send failed: %s", exc)
            return False

        if response.status_code != 200:
            # The API echoes the token in no field, but be careful anyway.
            detail = scrub(response.text[:300])
            self.last_error = f"Telegram API returned {response.status_code}: {detail}"
            logger.warning("telegram send failed: %s", self.last_error)
            return False

        self.sent_count += 1
        self.last_error = ""
        return True

    def _should_alert(self, key: str, cooldown: int = ALERT_COOLDOWN_SECONDS) -> bool:
        """Rate-limit repeating non-channel alerts."""
        now = time.monotonic()
        last = self._last_alert.get(key)
        if last is not None and (now - last) < cooldown:
            return False
        self._last_alert[key] = now
        return True

    # ------------------------------------------------------------------ #
    # channel events
    # ------------------------------------------------------------------ #
    async def channel_down(self, channel_id: int, channel_name: str, reason: str) -> bool:
        """Announce an outage - only on the ONLINE -> DOWN transition."""
        if self._channel_state.get(channel_id) == "down":
            logger.debug("channel %s already reported down - not resending", channel_id)
            return False
        self._channel_state[channel_id] = "down"
        text = (
            "🔴 <b>STREAM DOWN</b>\n"
            f"Channel: {html.escape(channel_name or str(channel_id))}\n"
            f"Time: {format_local(utcnow())}\n"
            f"Reason: {html.escape(reason[:200])}\n\n"
            "Refreshing the source URL and restarting this channel..."
        )
        return await self.send(text)

    async def channel_recovered(
        self, channel_id: int, channel_name: str, downtime_seconds: float, attempts: int
    ) -> bool:
        """Announce recovery - only when the channel was previously reported down."""
        if self._channel_state.get(channel_id) != "down":
            self._channel_state[channel_id] = "up"
            return False
        self._channel_state[channel_id] = "up"
        text = (
            "🟢 <b>STREAM RECOVERED</b>\n"
            f"Channel: {html.escape(channel_name or str(channel_id))}\n"
            f"Downtime: {humanize_duration(downtime_seconds)}\n"
            f"Attempts: {attempts}\n"
            f"Time: {format_local(utcnow())}\n\n"
            "RTMP streaming normally."
        )
        return await self.send(text)

    async def channel_unstable(
        self, channel_id: int, channel_name: str, detail: str
    ) -> bool:
        """Sent once when the restart circuit trips for a channel."""
        if not self._should_alert(f"unstable:{channel_id}"):
            return False
        text = (
            "🟠 <b>CHANNEL UNSTABLE</b>\n"
            f"Channel: {html.escape(channel_name or str(channel_id))}\n"
            f"{html.escape(detail[:200])}\n\n"
            "Restart frequency has been reduced. Check the source and the RTMP "
            "destination."
        )
        return await self.send(text)

    def forget_channel(self, channel_id: int) -> None:
        """Drop the remembered state (channel deleted or manually stopped)."""
        self._channel_state.pop(channel_id, None)
        self._last_alert.pop(f"unstable:{channel_id}", None)

    def mark_online(self, channel_id: int) -> None:
        """Seed the state without sending anything (used at startup)."""
        self._channel_state[channel_id] = "up"

    # ------------------------------------------------------------------ #
    # system events
    # ------------------------------------------------------------------ #
    async def provider_auth_error(self, error: str) -> bool:
        if not self._should_alert("provider_auth"):
            return False
        text = (
            "🟠 <b>PROVIDER AUTHENTICATION ERROR</b>\n"
            f"{html.escape(error[:300])}\n"
            f"Time: {format_local(utcnow())}\n\n"
            "Unable to refresh the provider session. Retrying automatically; if "
            "this persists, check the provider credentials."
        )
        return await self.send(text)

    async def system_error(self, error: str) -> bool:
        if not self._should_alert(f"system:{hash(error) & 0xFFFF}"):
            return False
        text = (
            "⚠️ <b>SYSTEM ERROR</b>\n"
            f"{html.escape(error[:400])}\n"
            f"Time: {format_local(utcnow())}"
        )
        return await self.send(text)

    async def system_started(self, channel_count: int) -> bool:
        text = (
            "▶️ <b>RESTREAM MANAGER STARTED</b>\n"
            f"Channels configured: {channel_count}\n"
            f"Time: {format_local(utcnow())}"
        )
        return await self.send(text)

    # ------------------------------------------------------------------ #
    async def send_test(self) -> dict[str, Any]:
        """Dashboard 'Send Test Message' button."""
        if not self.token:
            return {"ok": False, "error": "no bot token configured"}
        if not self.chat_id:
            return {"ok": False, "error": "no chat id configured"}
        ok = await self.send(
            "✅ <b>Test message</b>\n"
            "Restream Manager can reach this chat.\n"
            f"Time: {format_local(utcnow())}"
        )
        return {"ok": ok, "error": self.last_error if not ok else ""}
