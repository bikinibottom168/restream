"""FFmpeg command construction, progress parsing and log masking."""

from __future__ import annotations

from app.providers.base import ResolvedStream
from app.streaming.ffmpeg import (
    FFmpegCapabilities,
    FFmpegProcess,
    build_command,
    build_egress_command,
    build_header_blob,
    build_publisher_command,
    build_slate_command,
    build_watch_command,
    resolve_video_encoder,
    safe_command,
)
from app.streaming.relay import SeamlessProfile
from app.streaming.probe import ProbeResult


def stream(**kwargs) -> ResolvedStream:
    payload = {
        "channel_id": "sport01",
        "url": "https://origin.example/live/sport01/index.m3u8?token=secret123",
    }
    payload.update(kwargs)
    return ResolvedStream(**payload)


CAPS = FFmpegCapabilities(
    reconnect=True,
    reconnect_streamed=True,
    reconnect_delay_max=True,
    reconnect_on_network_error=True,
    detected=True,
)


def test_copy_mode_is_the_default():
    command = build_command(
        ffmpeg_path="ffmpeg",
        stream=stream(),
        output_url="rtmp://server.example/live/channel01",
        caps=CAPS,
    )
    assert command[0] == "ffmpeg"
    assert "-c" in command and command[command.index("-c") + 1] == "copy"
    assert command[-1] == "rtmp://server.example/live/channel01"
    assert command[-2] == "flv" and command[-3] == "-f"
    assert "libx264" not in command


def test_copy_mode_has_player_like_tolerance():
    command = build_command(
        ffmpeg_path="ffmpeg", stream=stream(), output_url="rtmp://x/y", caps=CAPS
    )
    fflags = command[command.index("-fflags") + 1]
    assert "discardcorrupt" in fflags  # skip a bad packet instead of dropping
    assert "genpts" in fflags
    assert "igndts" in fflags
    assert command[command.index("-avoid_negative_ts") + 1] == "make_zero"
    assert command[command.index("-max_muxing_queue_size") + 1] == "4096"


def test_progress_pipe_is_always_requested():
    command = build_command(
        ffmpeg_path="ffmpeg", stream=stream(), output_url="rtmp://x/y", caps=CAPS
    )
    assert "-progress" in command
    assert command[command.index("-progress") + 1] == "pipe:1"


def test_reconnect_flags_follow_capabilities():
    with_caps = build_command(
        ffmpeg_path="ffmpeg", stream=stream(), output_url="rtmp://x/y", caps=CAPS
    )
    assert "-reconnect" in with_caps
    assert "-reconnect_streamed" in with_caps
    assert "-reconnect_delay_max" in with_caps

    without = build_command(
        ffmpeg_path="ffmpeg",
        stream=stream(),
        output_url="rtmp://x/y",
        caps=FFmpegCapabilities(),
    )
    assert "-reconnect" not in without, "unsupported options must not be passed"


def test_reconnect_at_eof_is_set_for_live_http():
    command = build_command(
        ffmpeg_path="ffmpeg", stream=stream(), output_url="rtmp://x/y", caps=CAPS
    )
    assert "-reconnect_at_eof" in command
    # and it is not passed when the build has no reconnect support at all
    without = build_command(
        ffmpeg_path="ffmpeg", stream=stream(), output_url="rtmp://x/y",
        caps=FFmpegCapabilities(),
    )
    assert "-reconnect_at_eof" not in without


def test_reconnect_flags_skipped_for_non_http_input():
    command = build_command(
        ffmpeg_path="ffmpeg",
        stream=stream(url="srt://origin.example:9000"),
        output_url="rtmp://x/y",
        caps=CAPS,
    )
    assert "-reconnect" not in command


def test_headers_and_user_agent_are_passed_through():
    command = build_command(
        ffmpeg_path="ffmpeg",
        stream=stream(
            headers={"X-Token": "abc"},
            referer="https://portal.example/",
            user_agent="ExamplePlayer/1.0",
            cookies={"sid": "xyz"},
        ),
        output_url="rtmp://x/y",
        caps=CAPS,
    )
    assert command[command.index("-user_agent") + 1] == "ExamplePlayer/1.0"
    blob = command[command.index("-headers") + 1]
    assert "X-Token: abc" in blob
    assert "Referer: https://portal.example/" in blob
    assert "Cookie: sid=xyz" in blob
    assert "User-Agent" not in blob, "user agent uses its own option"


def test_aac_bitstream_filter_only_when_needed():
    aac = ProbeResult(ok=True, streams=[{"codec_type": "audio", "codec_name": "aac"}])
    mp2 = ProbeResult(ok=True, streams=[{"codec_type": "audio", "codec_name": "mp2"}])

    with_aac = build_command(
        ffmpeg_path="ffmpeg", stream=stream(), output_url="rtmp://x/y", probe=aac
    )
    assert "aac_adtstoasc" in with_aac

    without = build_command(
        ffmpeg_path="ffmpeg", stream=stream(), output_url="rtmp://x/y", probe=mp2
    )
    assert "aac_adtstoasc" not in without


def test_transcode_mode():
    command = build_command(
        ffmpeg_path="ffmpeg",
        stream=stream(),
        output_url="rtmp://x/y",
        mode="transcode",
        video_bitrate="3000k",
        audio_bitrate="160k",
        preset="fast",
    )
    assert "libx264" in command
    assert command[command.index("-preset") + 1] == "fast"
    assert command[command.index("-b:v") + 1] == "3000k"
    assert command[command.index("-bufsize") + 1] == "6000k"
    assert command[command.index("-c:a") + 1] == "aac"


def test_slate_command_uses_colour_without_an_image():
    command = build_slate_command(
        ffmpeg_path="ffmpeg", output_url="rtmp://127.0.0.1:1935/ch5"
    )
    assert "lavfi" in command
    assert any(arg.startswith("color=") for arg in command)
    assert "libx264" in command
    assert command[-1] == "rtmp://127.0.0.1:1935/ch5"
    assert command[-2] == "flv" and command[-3] == "-f"
    # a silent audio track is always present so the FLV muxer is happy
    assert any("anullsrc" in arg for arg in command)


def test_slate_command_loops_a_configured_image():
    command = build_slate_command(
        ffmpeg_path="ffmpeg",
        output_url="rtmp://127.0.0.1:1935/ch5",
        image_path="/tmp/reconnect.png",
    )
    assert "-loop" in command
    assert "/tmp/reconnect.png" in command
    assert not any(arg.startswith("color=") for arg in command)


def test_seamless_feeder_writes_mpegts_with_the_shared_profile():
    profile = SeamlessProfile(size="854x480", fps=30, video_bitrate="1500k")
    command = build_command(
        ffmpeg_path="ffmpeg",
        stream=stream(),
        output_url="udp://127.0.0.1:21000?pkt_size=1316",
        mode="copy",  # overridden: a seamless feeder must always encode
        profile=profile,
        output_format="mpegts",
    )
    assert "-c" not in command or command[command.index("-c") + 1] != "copy"
    assert command[command.index("-s") + 1] == "854x480"
    assert command[command.index("-f") + 1] == "mpegts"
    assert "+resend_headers" in command, "a publisher attaching late still needs the tables"


def test_publisher_restamps_so_a_source_switch_does_not_go_backwards():
    command = build_publisher_command(
        ffmpeg_path="ffmpeg",
        input_url="udp://127.0.0.1:21000",
        output_url="rtmp://a.rtmp.youtube.com/live2/key",
    )
    assert command[command.index("-use_wallclock_as_timestamps") + 1] == "1"
    assert command[command.index("-c") + 1] == "copy", "the publisher never re-encodes"
    assert command[command.index("-f") + 1] == "flv"


def test_seamless_slate_matches_the_feeder_profile():
    profile = SeamlessProfile(size="854x480", fps=30, video_bitrate="1500k")
    command = build_slate_command(
        ffmpeg_path="ffmpeg",
        output_url="udp://127.0.0.1:21000?pkt_size=1316",
        profile=profile,
        output_format="mpegts",
    )
    # A slate encoded differently from the real sources would break the very
    # stream it is covering for, because the publisher only copies packets.
    assert command[command.index("-s") + 1] == "854x480"
    assert "-tune" not in command
    # The input side uses -f lavfi, so the muxer is the last -f on the line.
    assert command[len(command) - 1 - command[::-1].index("-f") + 1] == "mpegts"


def test_watch_command_throws_the_bytes_away():
    command = build_watch_command(ffmpeg_path="ffmpeg", stream=stream())
    assert command[command.index("-c") + 1] == "copy", "proving it sustains costs no CPU"
    assert "-progress" in command
    assert command[-1] not in ("-", ""), "output must be a real null sink, not stdout"


def test_egress_command_is_a_copy_relay():
    command = build_egress_command(
        ffmpeg_path="ffmpeg",
        input_url="rtmp://127.0.0.1:1935/ch5",
        output_url="rtmp://cdn.example/live/key123",
    )
    assert command[command.index("-i") + 1] == "rtmp://127.0.0.1:1935/ch5"
    assert "-c" in command and command[command.index("-c") + 1] == "copy"
    assert command[-1] == "rtmp://cdn.example/live/key123"
    assert command[-2] == "flv" and command[-3] == "-f"
    assert "libx264" not in command


def test_resolve_video_encoder():
    caps = FFmpegCapabilities(hw_encoders=["h264_videotoolbox", "h264_qsv"])
    # auto picks the best available (videotoolbox is first in preference)
    assert resolve_video_encoder("auto", caps) == "h264_videotoolbox"
    # off always software
    assert resolve_video_encoder("off", caps) == "libx264"
    # a specific available encoder is honoured
    assert resolve_video_encoder("qsv", caps) == "h264_qsv"
    # a specific UNavailable encoder falls back to software
    assert resolve_video_encoder("nvenc", caps) == "libx264"
    # auto with no hardware -> software
    assert resolve_video_encoder("auto", FFmpegCapabilities()) == "libx264"


def test_transcode_uses_hardware_encoder_when_selected():
    caps = FFmpegCapabilities(hw_encoders=["h264_nvenc"])
    command = build_command(
        ffmpeg_path="ffmpeg", stream=stream(), output_url="rtmp://x/y",
        mode="transcode", caps=caps, video_encoder="h264_nvenc",
        video_bitrate="2500k",
    )
    assert "h264_nvenc" in command
    assert "libx264" not in command
    assert command[command.index("-b:v") + 1] == "2500k"
    assert command[command.index("-c:a") + 1] == "aac"


def test_header_blob_format():
    blob = build_header_blob({"A": "1", "B": "2"})
    assert blob == "A: 1\r\nB: 2\r\n"
    assert build_header_blob({}) == ""


def test_safe_command_masks_tokens_and_headers():
    command = build_command(
        ffmpeg_path="ffmpeg",
        stream=stream(headers={"Authorization": "Bearer secret"}),
        output_url="rtmp://server.example/live/key123",
        caps=CAPS,
    )
    rendered = safe_command(command)
    assert "secret123" not in rendered, "the source token must be masked"
    assert "Bearer secret" not in rendered, "headers must be masked"
    assert "origin.example" in rendered, "the host stays visible for debugging"


# --------------------------------------------------------------------------- #
# progress parsing / stall detection
# --------------------------------------------------------------------------- #
def make_process() -> FFmpegProcess:
    from pathlib import Path
    import tempfile

    return FFmpegProcess(1, ["ffmpeg"], ffmpeg_log_dir=Path(tempfile.mkdtemp()))


def test_progress_parsing():
    process = make_process()
    for key, value in [
        ("bitrate", "2500.5kbits/s"),
        ("out_time", "00:00:10.000000"),
        ("out_time_us", "10000000"),
        ("speed", "1.01x"),
        ("frame", "250"),
        ("total_size", "3145728"),
        ("progress", "continue"),
    ]:
        process._apply_progress(key, value)  # noqa: SLF001 - unit under test

    metrics = process.metrics
    assert metrics.bitrate == "2500.5kbits/s"
    assert metrics.out_time_us == 10_000_000
    assert metrics.speed == "1.01x"
    assert metrics.frames == 250
    assert metrics.total_size == 3_145_728
    assert metrics.as_dict()["out_time_seconds"] == 10.0


def test_progress_ignores_garbage():
    process = make_process()
    process._apply_progress("out_time_us", "N/A")  # noqa: SLF001
    process._apply_progress("frame", "")  # noqa: SLF001
    assert process.metrics.out_time_us == 0
    assert process.metrics.frames == 0


def test_stall_detection_needs_a_running_process():
    process = make_process()
    assert process.is_stalled(10) is False, "a process that never started is not stalled"


def test_progress_end_sets_exit_reason():
    process = make_process()
    process._apply_progress("progress", "end")  # noqa: SLF001
    assert "end of stream" in process.exit_reason
