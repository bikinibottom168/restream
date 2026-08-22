"""Database layer: channels, providers, events, downtime."""

from __future__ import annotations

import pytest

from app.core.state import ChannelState
from app.database import crud
from app.database.db import call_db, dispose_engine, init_db
from app.database.models import EventType


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    yield
    dispose_engine()


def test_channel_crud(db):
    channel = call_db(crud.create_channel, name="Sport Channel 01", stream_key="sport01")
    assert channel.id

    fetched = call_db(crud.get_channel, channel.id)
    assert fetched.name == "Sport Channel 01"
    assert fetched.status == ChannelState.STOPPED.value

    call_db(crud.update_channel, channel.id, name="Renamed", enabled=False)
    assert call_db(crud.get_channel, channel.id).name == "Renamed"

    assert call_db(crud.delete_channel, channel.id) is True
    assert call_db(crud.get_channel, channel.id) is None


def test_update_ignores_unknown_fields(db):
    channel = call_db(crud.create_channel, name="A")
    call_db(crud.update_channel, channel.id, name="B", not_a_column="x")
    assert call_db(crud.get_channel, channel.id).name == "B"


def test_resolved_rtmp_composition(db):
    with_key = call_db(crud.create_channel, name="A", stream_key="channel01")
    assert with_key.resolved_rtmp("rtmp://s.example/live/") == "rtmp://s.example/live/channel01"
    assert with_key.resolved_rtmp("") == ""

    full = call_db(crud.create_channel, name="B", rtmp_url="rtmp://other.example/live/x")
    assert full.resolved_rtmp("rtmp://s.example/live/") == "rtmp://other.example/live/x"


def test_status_updates_track_online_time(db):
    channel = call_db(crud.create_channel, name="A")
    call_db(crud.set_channel_status, channel.id, ChannelState.ONLINE, ffmpeg_pid=4242)
    updated = call_db(crud.get_channel, channel.id)
    assert updated.status == "ONLINE"
    assert updated.ffmpeg_pid == 4242
    assert updated.last_online_at is not None


def test_provider_crud_and_default(db):
    first = call_db(crud.create_provider, name="P1", type="manual", is_default=True)
    second = call_db(crud.create_provider, name="P2", type="http_json", is_default=True)

    providers = call_db(crud.list_providers)
    defaults = [p.name for p in providers if p.is_default]
    assert defaults == ["P2"], "only one provider may be the default"

    assert call_db(crud.get_default_provider).name == "P2"

    call_db(crud.set_provider_auth_state, first.id, ok=False, error="bad credentials")
    assert call_db(crud.get_provider, first.id).last_error == "bad credentials"

    channel = call_db(crud.create_channel, name="A", provider_id=second.id)
    assert call_db(crud.delete_provider, second.id) is True
    assert call_db(crud.get_channel, channel.id).provider_id is None, "channels survive"


def test_provider_ref_lookup(db):
    provider = call_db(crud.create_provider, name="P", type="http_json")
    call_db(crud.create_channel, name="A", provider_id=provider.id, provider_ref="82290")
    found = call_db(crud.get_channel_by_provider_ref, provider.id, "82290")
    assert found.name == "A"
    assert call_db(crud.get_channel_by_provider_ref, provider.id, "nope") is None


def test_events_and_pruning(db):
    channel = call_db(crud.create_channel, name="A")
    call_db(
        crud.add_event,
        event_type=EventType.STREAM_STARTED,
        message="started",
        channel_id=channel.id,
    )
    call_db(crud.add_event, event_type=EventType.SYSTEM_STARTED, message="boot")

    all_events = call_db(crud.list_events)
    assert len(all_events) == 2
    channel_events = call_db(crud.list_events, channel_id=channel.id)
    assert len(channel_events) == 1
    assert channel_events[0].channel_name == "A"


def test_downtime_lifecycle(db):
    channel = call_db(crud.create_channel, name="A")

    opened = call_db(crud.open_downtime, channel.id, "A", "source unreachable")
    again = call_db(crud.open_downtime, channel.id, "A", "still down")
    assert opened.id == again.id, "an outage is never opened twice"

    assert call_db(crud.bump_downtime_attempts, channel.id) == 1
    assert call_db(crud.bump_downtime_attempts, channel.id) == 2

    closed = call_db(crud.close_downtime, channel.id)
    assert closed.recovered_at is not None
    assert call_db(crud.close_downtime, channel.id) is None

    history = call_db(crud.list_downtime, channel_id=channel.id)
    assert len(history) == 1
    assert history[0].attempts == 2


def test_sync_marks_missing_without_deleting(db):
    provider = call_db(crud.create_provider, name="P", type="http_json")
    kept = call_db(crud.create_channel, name="kept", provider_id=provider.id, provider_ref="1")
    gone = call_db(crud.create_channel, name="gone", provider_id=provider.id, provider_ref="2")

    missing = call_db(crud.mark_missing_from_source, provider.id, ["1"])
    assert [c.id for c in missing] == [gone.id]
    assert call_db(crud.get_channel, gone.id) is not None, "rows are never auto-deleted"
    assert call_db(crud.get_channel, gone.id).source_present is False
    assert call_db(crud.get_channel, kept.id).source_present is True


def test_reset_runtime_state_on_startup(db):
    channel = call_db(crud.create_channel, name="A")
    call_db(crud.set_channel_status, channel.id, ChannelState.ONLINE, ffmpeg_pid=1234)
    assert call_db(crud.reset_runtime_state) == 1
    refreshed = call_db(crud.get_channel, channel.id)
    assert refreshed.status == ChannelState.STOPPED.value
    assert refreshed.ffmpeg_pid is None


def test_settings_roundtrip(db):
    call_db(crud.set_setting, "check_interval_seconds", "600")
    assert call_db(crud.get_setting, "check_interval_seconds") == "600"
    call_db(crud.set_setting, "check_interval_seconds", "900")
    assert call_db(crud.all_settings)["check_interval_seconds"] == "900"


def test_autostart_selection(db):
    call_db(crud.create_channel, name="on", enabled=True, auto_start=True)
    call_db(crud.create_channel, name="off", enabled=True, auto_start=False)
    call_db(crud.create_channel, name="disabled", enabled=False, auto_start=True)
    names = {c.name for c in call_db(crud.channels_needing_autostart)}
    assert names == {"on"}
