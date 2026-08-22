"""Configuration validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.settings_store import (
    SettingsStore,
    SettingsValidationError,
)


def test_defaults_are_sane():
    settings = Settings()
    assert settings.app_host == "127.0.0.1", "the dashboard must not bind publicly by default"
    assert settings.app_port == 8787
    assert settings.check_interval_seconds == 300
    assert settings.process_monitor_interval_seconds < settings.check_interval_seconds
    assert settings.default_stream_mode == "copy"


@pytest.mark.parametrize("port", [0, 70000, -1])
def test_invalid_port_rejected(port):
    with pytest.raises(ValidationError):
        Settings(app_port=port)


def test_check_interval_minimum():
    with pytest.raises(ValidationError):
        Settings(check_interval_seconds=10)


def test_rtmp_scheme_validated():
    with pytest.raises(ValidationError):
        Settings(default_rtmp_server="http://not-rtmp.example")
    assert Settings(default_rtmp_server="rtmp://ok.example/live").default_rtmp_server


def test_log_level_normalised():
    assert Settings(log_level="debug").log_level == "DEBUG"
    with pytest.raises(ValidationError):
        Settings(log_level="chatty")


def test_database_url_points_into_data_dir(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    assert settings.database_url.startswith("sqlite:///")
    assert "restream.db" in settings.database_url


def test_ensure_dirs_creates_everything(tmp_path):
    settings = Settings(data_dir=tmp_path / "d", log_dir=tmp_path / "l")
    settings.ensure_dirs()
    for path in (
        settings.data_dir,
        settings.log_dir,
        settings.ffmpeg_log_dir,
        settings.pid_dir,
        settings.debug_dir,
    ):
        assert path.is_dir()


# --------------------------------------------------------------------------- #
# runtime settings store
# --------------------------------------------------------------------------- #
def test_store_falls_back_to_environment(settings_store: SettingsStore):
    assert settings_store.get_int("check_interval_seconds") == 300


def test_store_override_and_validation(settings_store: SettingsStore):
    assert settings_store.set("check_interval_seconds", "600") == 600
    assert settings_store.get_int("check_interval_seconds") == 600

    with pytest.raises(SettingsValidationError):
        settings_store.set("check_interval_seconds", 5)
    with pytest.raises(SettingsValidationError):
        settings_store.set("check_interval_seconds", "not-a-number")
    with pytest.raises(SettingsValidationError):
        settings_store.set("default_rtmp_server", "http://nope")
    with pytest.raises(SettingsValidationError):
        settings_store.set("not_a_real_key", 1)


def test_store_bool_coercion(settings_store: SettingsStore):
    assert settings_store.set("show_full_source_url", "true") is True
    assert settings_store.set("show_full_source_url", "0") is False


def test_store_listener_fires(settings_store: SettingsStore):
    seen = []
    settings_store.add_listener(lambda key, value: seen.append((key, value)))
    settings_store.set("failure_threshold", 3)
    assert seen == [("failure_threshold", 3)]


def test_store_load_ignores_bad_rows(settings_store: SettingsStore):
    settings_store.load({"failure_threshold": "4", "check_interval_seconds": "\"nope\""})
    assert settings_store.get_int("failure_threshold") == 4
    # the bad value was ignored, so the environment default survives
    assert settings_store.get_int("check_interval_seconds") == 300
