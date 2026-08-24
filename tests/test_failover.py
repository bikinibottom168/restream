"""Backup sources: when to leave the primary, and when it has earned its way back."""

from __future__ import annotations

import json

from app.streaming.failover import (
    FailoverPolicy,
    build_sources,
    fallback_urls_text,
    is_output_failure,
    normalise_fallback_input,
    parse_fallback_sources,
    slow_retry_delay,
)
from app.streaming.relay import SeamlessProfile, relay_input_url, relay_output_url


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def policy(clock: FakeClock, **kwargs) -> FailoverPolicy:
    options = {
        "failover_after_seconds": 120,
        "failure_threshold": 3,
        "min_stable_seconds": 60,
        "failback_after_seconds": 600,
        "penalty_max_seconds": 3_600,
    }
    options.update(kwargs)
    return FailoverPolicy(clock=clock, **options)


# --------------------------------------------------------------------------- #
# parsing what the operator typed
# --------------------------------------------------------------------------- #
def test_plain_text_one_url_per_line():
    sources = parse_fallback_sources(
        "http://43.249.32.198:8080/memfs/a.m3u8\n\nhttps://backup.example.com/b.m3u8\n"
    )
    assert [s.url for s in sources] == [
        "http://43.249.32.198:8080/memfs/a.m3u8",
        "https://backup.example.com/b.m3u8",
    ]
    assert [s.index for s in sources] == [1, 2], "the primary keeps index 0"


def test_json_objects_carry_per_url_playback_hints():
    stored = json.dumps(
        [{"url": "https://b.example.com/x.m3u8", "referer": "https://b.example.com/", "label": "cdn2"}]
    )
    source = parse_fallback_sources(stored)[0]
    assert source.referer == "https://b.example.com/"
    assert source.name == "cdn2"


def test_junk_and_duplicates_are_dropped_not_raised():
    sources = parse_fallback_sources(
        "not a url\n# a comment\nhttps://a.example.com/x.m3u8\nhttps://a.example.com/x.m3u8"
    )
    assert [s.url for s in sources] == ["https://a.example.com/x.m3u8"]


def test_storage_round_trips_back_to_the_textarea():
    typed = "https://a.example.com/x.m3u8\nhttps://b.example.com/y.m3u8"
    stored = normalise_fallback_input(typed)
    assert fallback_urls_text(stored) == typed


def test_build_sources_puts_the_primary_first():
    class Row:
        fallback_urls = "https://b.example.com/y.m3u8"

    sources = build_sources(Row())
    assert sources[0].is_primary
    assert sources[1].url == "https://b.example.com/y.m3u8"
    assert len(sources) == 2


# --------------------------------------------------------------------------- #
# leaving a broken primary
# --------------------------------------------------------------------------- #
def test_a_long_outage_triggers_a_switch():
    clock = FakeClock()
    p = policy(clock)
    p.record_failure(0)
    assert not p.should_leave(0)
    clock.advance(119)
    assert not p.should_leave(0)
    clock.advance(2)
    assert p.should_leave(0)


def test_a_flapping_source_switches_on_the_streak_not_the_clock():
    """The case a plain timer never catches: up 20s, dead, up 20s, dead..."""
    clock = FakeClock()
    p = policy(clock)
    for _ in range(2):
        clock.advance(20)
        p.record_failure(0, ran_seconds=20)
    assert not p.should_leave(0), "two short-lived starts is not yet proof"
    clock.advance(20)
    p.record_failure(0, ran_seconds=20)
    assert p.should_leave(0)
    assert "3 failed attempts" in p.leave_reason(0)


def test_a_source_that_held_properly_starts_a_fresh_outage():
    clock = FakeClock()
    p = policy(clock)
    p.record_failure(0, ran_seconds=5)
    p.record_failure(0, ran_seconds=5)
    p.record_failure(0, ran_seconds=3_600)
    assert not p.should_leave(0), "an hour of good service clears the streak"
    assert p.failures(0) == 1


def test_going_stable_clears_the_accounting():
    clock = FakeClock()
    p = policy(clock)
    p.record_failure(0)
    clock.advance(500)
    p.record_stable(0)
    assert not p.should_leave(0)
    assert p.failing_for(0) == 0


def test_next_index_round_robins_back_to_the_primary():
    assert FailoverPolicy.next_index(0, 3) == 1
    assert FailoverPolicy.next_index(1, 3) == 2
    assert FailoverPolicy.next_index(2, 3) == 0
    assert FailoverPolicy.next_index(0, 1) == 0, "nothing to switch to"


# --------------------------------------------------------------------------- #
# coming back to the primary
# --------------------------------------------------------------------------- #
def test_failback_needs_an_unbroken_run_of_clean_probes():
    clock = FakeClock()
    p = policy(clock)
    for _ in range(9):
        p.record_primary_probe(True)
        clock.advance(60)
    assert not p.failback_ready()
    p.record_primary_probe(False)
    clock.advance(60)
    p.record_primary_probe(True)
    assert p.primary_healthy_for() == 0, "one bad probe restarts the count"


def test_failback_ready_after_the_required_period():
    clock = FakeClock()
    p = policy(clock)
    p.record_primary_probe(True)
    clock.advance(599)
    assert not p.failback_ready()
    clock.advance(2)
    assert p.failback_ready()
    assert p.failback_eta() == 0


def test_a_primary_that_breaks_again_has_to_wait_twice_as_long():
    clock = FakeClock()
    p = policy(clock)
    assert p.required_healthy_seconds() == 600
    assert p.penalise() == 1_200
    assert p.penalise() == 2_400
    assert p.penalise() == 3_600
    assert p.penalise() == 3_600, "capped, so it never waits absurdly long"
    p.forgive()
    assert p.required_healthy_seconds() == 600


def test_penalising_also_discards_the_health_already_earned():
    clock = FakeClock()
    p = policy(clock)
    p.record_primary_probe(True)
    clock.advance(300)
    p.penalise()
    assert p.primary_healthy_for() == 0
    assert p.failback_eta() == -1


# --------------------------------------------------------------------------- #
# everything is down
# --------------------------------------------------------------------------- #
def test_retries_slow_down_only_after_a_long_total_outage():
    common = {"slow_after_seconds": 900, "slow_delay_seconds": 300}
    assert slow_retry_delay(60, normal_delay=30, **common) == 30
    assert slow_retry_delay(899, normal_delay=30, **common) == 30
    assert slow_retry_delay(900, normal_delay=30, **common) == 300


def test_slow_mode_never_speeds_a_retry_up():
    delay = slow_retry_delay(
        5_000, normal_delay=600, slow_after_seconds=900, slow_delay_seconds=300
    )
    assert delay == 600


def test_slow_mode_can_be_turned_off():
    assert (
        slow_retry_delay(
            10_000, normal_delay=30, slow_after_seconds=0, slow_delay_seconds=300
        )
        == 30
    )


# --------------------------------------------------------------------------- #
# the seamless relay
# --------------------------------------------------------------------------- #
def test_relay_urls_are_loopback_only():
    assert relay_output_url(21000).startswith("udp://127.0.0.1:21000")
    listen = relay_input_url(21000)
    assert "overrun_nonfatal=1" in listen, "a buffer overrun must not kill the publisher"
    assert "timeout=0" in listen, "a gap between feeders is not an error"


def test_every_feeder_of_a_channel_encodes_identically():
    profile = SeamlessProfile(size="1280x720", fps=25, video_bitrate="2500k")
    args = profile.encode_args()
    assert "-s" in args and "1280x720" in args
    assert args[args.index("-g") + 1] == "50", "two-second GOP"
    assert profile.encode_args() == args, "the profile is what makes a switch copyable"


# --------------------------------------------------------------------------- #
# telling a broken source from a broken destination
# --------------------------------------------------------------------------- #
def test_a_refused_rtmp_endpoint_is_not_a_source_failure():
    # Rotating backups cannot fix this, and doing so would burn through every
    # source and report "all sources down" for a downstream problem.
    assert is_output_failure("Error opening output files: Connection refused")
    assert is_output_failure("[out#0/flv] Error closing file: Broken pipe")
    assert is_output_failure("Could not write header for output file")


def test_source_side_errors_still_count():
    assert not is_output_failure("Server returned 404 Not Found")
    assert not is_output_failure("stream stalled (no progress for 90s)")
    assert not is_output_failure("")
    # A source that connects and then sends nothing looks like this, and it
    # really does deserve a failover.
    assert not is_output_failure("no data reached the output destination")
