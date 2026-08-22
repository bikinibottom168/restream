"""Retry pacing and restart-loop protection."""

from __future__ import annotations

from app.streaming.backoff import BackoffPolicy, RestartCircuit


def test_backoff_ladder_matches_specification():
    policy = BackoffPolicy()
    assert policy.delay_for(1) == 3
    assert policy.delay_for(2) == 5
    assert policy.delay_for(3) == 10
    assert policy.delay_for(4) == 20
    assert policy.delay_for(5) == 30
    assert policy.delay_for(9) == 30, "attempts past the ladder stay at the cap"


def test_backoff_respects_max_delay():
    policy = BackoffPolicy(max_delay=8)
    assert policy.delay_for(4) == 8
    assert policy.delay_for(100) == 8


def test_next_delay_advances_and_resets():
    policy = BackoffPolicy()
    assert policy.next_delay() == 3
    assert policy.next_delay() == 5
    assert policy.attempt == 2
    policy.reset()
    assert policy.attempt == 0
    assert policy.next_delay() == 3, "coming back online restarts the ladder"


def test_zero_attempt_has_no_delay():
    assert BackoffPolicy().delay_for(0) == 0


# --------------------------------------------------------------------------- #
class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_circuit_trips_after_threshold():
    clock = FakeClock()
    circuit = RestartCircuit(window_seconds=600, threshold=3, throttled_delay=60, clock=clock)

    for _ in range(3):
        assert circuit.record_restart() is False
    assert circuit.record_restart() is True
    assert circuit.tripped
    assert circuit.throttled_delay == 60


def test_circuit_window_expires():
    clock = FakeClock()
    circuit = RestartCircuit(window_seconds=600, threshold=2, throttled_delay=60, clock=clock)
    for _ in range(3):
        circuit.record_restart()
    assert circuit.tripped

    clock.advance(601)
    assert not circuit.tripped
    assert circuit.restarts_in_window == 0


def test_circuit_notifies_once_per_trip():
    clock = FakeClock()
    circuit = RestartCircuit(window_seconds=600, threshold=1, throttled_delay=60, clock=clock)
    circuit.record_restart()
    circuit.record_restart()
    assert circuit.should_notify() is True
    assert circuit.should_notify() is False, "no repeat alert while still tripped"

    circuit.reset()
    circuit.record_restart()
    circuit.record_restart()
    assert circuit.should_notify() is True, "a new trip alerts again"


def test_circuit_reconfigure():
    circuit = RestartCircuit(window_seconds=600, threshold=10, throttled_delay=60)
    circuit.configure(threshold=2, throttled_delay=90)
    for _ in range(3):
        circuit.record_restart()
    assert circuit.tripped
    assert circuit.throttled_delay == 90
