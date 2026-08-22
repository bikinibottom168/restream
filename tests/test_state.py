"""Channel state machine."""

from __future__ import annotations

import pytest

from app.core.state import (
    ALLOWED_TRANSITIONS,
    ChannelState,
    can_transition,
    parse_state,
)


def test_every_state_has_transitions_defined():
    for state in ChannelState:
        assert state in ALLOWED_TRANSITIONS, f"{state} has no transition rules"


def test_happy_path():
    path = [
        ChannelState.STOPPED,
        ChannelState.STARTING,
        ChannelState.ONLINE,
        ChannelState.DEGRADED,
        ChannelState.RECONNECTING,
        ChannelState.STARTING,
        ChannelState.ONLINE,
    ]
    for current, target in zip(path, path[1:]):
        assert can_transition(current, target), f"{current} -> {target} should be allowed"


def test_recovery_from_offline():
    assert can_transition(ChannelState.RECONNECTING, ChannelState.OFFLINE)
    assert can_transition(ChannelState.OFFLINE, ChannelState.STARTING)
    assert can_transition(ChannelState.OFFLINE, ChannelState.ONLINE)


def test_illegal_transitions():
    assert not can_transition(ChannelState.DISABLED, ChannelState.ONLINE)
    assert not can_transition(ChannelState.STOPPED, ChannelState.ONLINE)
    assert not can_transition(ChannelState.CONFIG_REQUIRED, ChannelState.ONLINE)


def test_self_transition_is_allowed():
    for state in ChannelState:
        assert can_transition(state, state)


def test_state_classification():
    assert ChannelState.ONLINE.is_running
    assert ChannelState.DEGRADED.is_running
    assert not ChannelState.RECONNECTING.is_running

    assert ChannelState.RECONNECTING.is_active
    assert not ChannelState.DISABLED.is_active

    assert ChannelState.OFFLINE.is_down
    assert ChannelState.ERROR.is_down
    assert not ChannelState.ONLINE.is_down


def test_badges_are_defined():
    for state in ChannelState:
        assert state.badge


@pytest.mark.parametrize(
    "value,expected",
    [
        ("ONLINE", ChannelState.ONLINE),
        ("online", ChannelState.ONLINE),
        ("", ChannelState.STOPPED),
        (None, ChannelState.STOPPED),
        ("NONSENSE", ChannelState.STOPPED),
        (ChannelState.ERROR, ChannelState.ERROR),
    ],
)
def test_parse_state(value, expected):
    assert parse_state(value) is expected
