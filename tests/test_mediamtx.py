"""The MediaMTX buffer/relay manager."""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

from app.streaming import mediamtx as mm


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #
def test_channel_path_is_stable_and_valid():
    assert mm.channel_path(82) == "ch82"
    assert mm.channel_path(1) == "ch1"


def test_segment_count_respects_bounds():
    assert mm.segment_count_for(30) >= mm.MIN_SEGMENTS
    assert mm.segment_count_for(0) == mm.MIN_SEGMENTS
    assert mm.segment_count_for(10_000) == mm.MAX_SEGMENTS
    # a bigger delay target yields at least as many segments
    assert mm.segment_count_for(60) >= mm.segment_count_for(20)


def test_viewer_host_prefers_explicit_then_falls_back():
    assert mm.viewer_host("stream.example.com", "127.0.0.1") == "stream.example.com"
    assert mm.viewer_host("", "192.168.1.5") == "192.168.1.5"
    # unreachable bind addresses fall back to loopback
    assert mm.viewer_host("", "0.0.0.0") == "127.0.0.1"
    assert mm.viewer_host("", "") == "127.0.0.1"


def test_ingest_url_is_loopback():
    url = mm.ingest_url(82, rtmp_port=1935)
    assert url == "rtmp://127.0.0.1:1935/ch82"


def test_viewer_urls_use_public_host():
    urls = mm.viewer_urls(82, host="192.168.1.5", rtmp_port=1935, hls_port=8888)
    assert urls["rtmp"] == "rtmp://192.168.1.5:1935/ch82"
    assert urls["hls"] == "http://192.168.1.5:8888/ch82/index.m3u8"


def test_render_config_has_the_essentials():
    text = mm.render_config(
        rtmp_port=1935, hls_port=8888, api_port=9997, buffer_seconds=30
    )
    assert "rtmpAddress: :1935" in text
    assert "hlsAddress: :8888" in text
    assert "apiAddress: 127.0.0.1:9997" in text
    assert "hls: yes" in text
    assert "all_others:" in text
    # the delay target shapes the HLS window
    assert f"hlsSegmentCount: {mm.segment_count_for(30)}" in text


def test_render_config_sanitises_log_level():
    text = mm.render_config(
        rtmp_port=1, hls_port=2, api_port=3, buffer_seconds=5, log_level="NONSENSE"
    )
    assert "logLevel: info" in text


def test_download_asset_names_a_real_looking_release():
    asset = mm.download_asset("1.11.3")
    assert asset["filename"].startswith("mediamtx_v1.11.3_")
    assert asset["url"].endswith(asset["filename"])
    assert asset["target"].endswith(mm._binary_name())  # noqa: SLF001


# --------------------------------------------------------------------------- #
# binary resolution
# --------------------------------------------------------------------------- #
def test_resolve_binary_prefers_configured_path(tmp_path):
    binary = tmp_path / "mediamtx"
    binary.write_text("#!/bin/sh\n")
    assert mm.resolve_binary(str(binary)) == str(binary)


def test_resolve_binary_rejects_missing_configured_path(tmp_path):
    assert mm.resolve_binary(str(tmp_path / "nope")) is None


def test_resolve_binary_finds_bundled(monkeypatch, tmp_path):
    fake_root = tmp_path
    (fake_root / "bin").mkdir()
    bundled = fake_root / "bin" / mm._binary_name()  # noqa: SLF001
    bundled.write_text("#!/bin/sh\n")
    monkeypatch.setattr(mm, "BASE_DIR", fake_root)
    monkeypatch.setattr(mm.shutil, "which", lambda name: None)
    assert mm.resolve_binary("") == str(bundled)


# --------------------------------------------------------------------------- #
# process lifecycle (with a fake binary that just binds the api port)
# --------------------------------------------------------------------------- #
def _write_fake_mediamtx(path: Path, rtmp_port: int) -> Path:
    """A stand-in that binds the RTMP port and idles, like the real server."""
    script = path / "fake_mediamtx.py"
    script.write_text(
        "import socket, sys, time\n"
        f"s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        f"s.bind(('127.0.0.1', {rtmp_port})); s.listen(5)\n"
        "while True:\n"
        "    time.sleep(0.2)\n",
        encoding="utf-8",
    )
    launcher = path / ("mm_launch.sh")
    launcher.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n")
    launcher.chmod(0o755)
    return launcher


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX launcher")
async def test_server_starts_and_stops(tmp_path):
    rtmp_port = _free_port()
    launcher = _write_fake_mediamtx(tmp_path, rtmp_port)
    server = mm.MediaMtxServer(
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        rtmp_port=rtmp_port,
        hls_port=_free_port(),
        api_port=_free_port(),
        buffer_seconds=30,
        binary_path=str(launcher),
    )
    assert await server.start() is True
    assert server.running is True
    assert server.config_path.exists()
    assert server.pid is not None
    await server.stop()
    assert server.running is False


async def test_server_refuses_when_rtmp_port_is_taken(tmp_path):
    """A busy RTMP port is reported clearly instead of causing I/O errors."""
    busy = socket.socket()
    busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    busy.bind(("127.0.0.1", 0))
    busy.listen(5)
    taken_port = busy.getsockname()[1]
    fake_binary = tmp_path / "mediamtx"
    fake_binary.write_text("#!/bin/sh\n")  # exists, so we reach the port check
    try:
        server = mm.MediaMtxServer(
            data_dir=tmp_path / "data",
            log_dir=tmp_path / "logs",
            rtmp_port=taken_port,
            hls_port=_free_port(),
            api_port=_free_port(),
            binary_path=str(fake_binary),
        )
        assert await server.start() is False
        assert str(taken_port) in server.last_error
        assert "in use" in server.last_error.lower()
    finally:
        busy.close()


async def test_server_reports_missing_binary(tmp_path):
    server = mm.MediaMtxServer(
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        binary_path=str(tmp_path / "does-not-exist"),
    )
    assert await server.start() is False
    assert "not found" in server.last_error or "does not exist" in server.last_error.lower() or server.last_error


class _FakeServer:
    def __init__(self, running: bool):
        self.running = running

    def ingest_url(self, channel_id):
        return f"rtmp://127.0.0.1:1935/ch{channel_id}"


def test_resolve_tool_prefers_bin_for_bare_command(monkeypatch, tmp_path):
    root = tmp_path
    (root / "bin").mkdir()
    name = "ffmpeg.exe" if mm.IS_WINDOWS else "ffmpeg"
    binary = root / "bin" / name
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setattr(mm, "BASE_DIR", root)
    # a bare command resolves to the bundled binary
    assert mm.resolve_tool("ffmpeg", "ffmpeg") == str(binary)
    assert mm.resolve_tool("", "ffmpeg") == str(binary)


def test_resolve_tool_respects_an_explicit_path(monkeypatch, tmp_path):
    monkeypatch.setattr(mm, "BASE_DIR", tmp_path)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / ("ffmpeg.exe" if mm.IS_WINDOWS else "ffmpeg")).write_text("x")
    # an explicit path is honoured even when a bundled copy exists
    assert mm.resolve_tool("/opt/ffmpeg/ffmpeg", "ffmpeg") == "/opt/ffmpeg/ffmpeg"


def test_resolve_tool_falls_back_to_bare_command(monkeypatch, tmp_path):
    monkeypatch.setattr(mm, "BASE_DIR", tmp_path)  # no bin/ here
    assert mm.resolve_tool("ffprobe", "ffprobe") == "ffprobe"


def test_choose_output_uses_buffer_when_up():
    url, buffered = mm.choose_output(
        7, "rtmp://cdn.example/live/x",
        buffer_enabled=True, server=_FakeServer(running=True),
    )
    assert buffered is True
    assert url == "rtmp://127.0.0.1:1935/ch7"


def test_choose_output_falls_back_when_buffer_off():
    url, buffered = mm.choose_output(
        7, "rtmp://cdn.example/live/x",
        buffer_enabled=False, server=_FakeServer(running=True),
    )
    assert buffered is False
    assert url == "rtmp://cdn.example/live/x"


def test_choose_output_falls_back_when_server_down():
    url, buffered = mm.choose_output(
        7, "rtmp://cdn.example/live/x",
        buffer_enabled=True, server=_FakeServer(running=False),
    )
    assert buffered is False
    assert url == "rtmp://cdn.example/live/x"


def test_slate_within_limit():
    from app.streaming.supervisor import slate_within_limit

    # within the window -> keep it up
    assert slate_within_limit(30, 600) is True
    assert slate_within_limit(600, 600) is True
    # past the window -> take it down
    assert slate_within_limit(601, 600) is False
    assert slate_within_limit(15 * 3600, 600) is False
    # 0 (or less) means never auto-stop
    assert slate_within_limit(99999, 0) is True


def test_write_config_creates_file(tmp_path):
    server = mm.MediaMtxServer(
        data_dir=tmp_path / "data", log_dir=tmp_path / "logs", buffer_seconds=20
    )
    path = server.write_config()
    assert path.exists()
    assert "all_others:" in path.read_text(encoding="utf-8")
