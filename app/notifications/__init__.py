"""Outbound notifications."""

from app.notifications.telegram import TelegramNotifier  # noqa: F401

__all__ = ["TelegramNotifier"]
