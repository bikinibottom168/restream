"""Telegram notification de-duplication."""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.core.secrets import SecretStore
from app.core.settings_store import SettingsStore
from app.notifications.telegram import TelegramNotifier


class RecordingNotifier(TelegramNotifier):
    """Captures messages instead of calling the Telegram API."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.messages: list[str] = []

    @property
    def configured(self) -> bool:  # type: ignore[override]
        return True

    async def _deliver(self, text: str) -> bool:  # type: ignore[override]
        self.messages.append(text)
        self.sent_count += 1
        return True


@pytest.fixture
def notifier(tmp_path):
    store = SettingsStore(get_settings())
    secrets = SecretStore(tmp_path / "secrets.json", use_keyring=False)
    return RecordingNotifier(store, secrets)


async def test_down_is_sent_once(notifier):
    assert await notifier.channel_down(1, "Sport Channel 01", "source unreachable")
    assert await notifier.channel_down(1, "Sport Channel 01", "still unreachable") is False
    assert await notifier.channel_down(1, "Sport Channel 01", "and again") is False
    assert len(notifier.messages) == 1
    assert "STREAM DOWN" in notifier.messages[0]


async def test_recovered_only_after_down(notifier):
    assert await notifier.channel_recovered(1, "Sport Channel 01", 48, 2) is False
    assert notifier.messages == []

    await notifier.channel_down(1, "Sport Channel 01", "source unreachable")
    assert await notifier.channel_recovered(1, "Sport Channel 01", 48, 2) is True
    assert "STREAM RECOVERED" in notifier.messages[-1]
    assert "48s" in notifier.messages[-1]
    assert "Attempts: 2" in notifier.messages[-1]


async def test_full_down_up_cycle(notifier):
    await notifier.channel_down(1, "A", "x")
    await notifier.channel_recovered(1, "A", 10, 1)
    await notifier.channel_down(1, "A", "x again")
    await notifier.channel_recovered(1, "A", 20, 3)
    kinds = ["DOWN" if "DOWN" in m else "RECOVERED" for m in notifier.messages]
    assert kinds == ["DOWN", "RECOVERED", "DOWN", "RECOVERED"]


async def test_channels_are_independent(notifier):
    await notifier.channel_down(1, "A", "x")
    await notifier.channel_down(2, "B", "y")
    assert len(notifier.messages) == 2
    await notifier.channel_down(1, "A", "x")
    assert len(notifier.messages) == 2


async def test_unstable_alert_is_rate_limited(notifier):
    assert await notifier.channel_unstable(1, "A", "12 restarts in 10 minutes") is True
    assert await notifier.channel_unstable(1, "A", "13 restarts in 10 minutes") is False


async def test_provider_auth_error_rate_limited(notifier):
    assert await notifier.provider_auth_error("session expired") is True
    assert await notifier.provider_auth_error("session expired") is False


async def test_forget_channel_resets_state(notifier):
    await notifier.channel_down(1, "A", "x")
    notifier.forget_channel(1)
    assert await notifier.channel_down(1, "A", "x") is True


async def test_secrets_are_scrubbed_from_messages(tmp_path):
    from app.core.security import register_secret

    store = SettingsStore(get_settings())
    secrets = SecretStore(tmp_path / "secrets.json", use_keyring=False)
    notifier = RecordingNotifier(store, secrets)
    register_secret("super-secret-token-value")
    await notifier.system_error("failed using super-secret-token-value in request")
    assert "super-secret-token-value" not in notifier.messages[0]
    assert "***" in notifier.messages[0]


def test_describe_masks_token(tmp_path):
    store = SettingsStore(get_settings())
    secrets = SecretStore(tmp_path / "secrets.json", use_keyring=False)
    secrets.set("telegram_bot_token", "1234567890:AAbbCCddEEffGG")
    notifier = TelegramNotifier(store, secrets)
    described = notifier.describe()
    assert described["token"].startswith("***")
    assert "AAbbCC" not in described["token"]
