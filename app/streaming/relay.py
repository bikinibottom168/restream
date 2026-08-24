"""Local MPEG-TS relay: the piece that makes a source switch invisible.

FFmpeg cannot swap its input while it runs, so the ordinary way to change
source is to kill the process and start another one.  The publisher connection
dies with it, and everything downstream notices: MediaMTX tears the path down
and restarts the HLS media sequence, the egress relay hits EOF and exits, and a
downstream service like YouTube sees the push stop.

Seamless mode splits that one process in two:

    feeder ffmpeg (source / slate)  --MPEG-TS over UDP-->  publisher ffmpeg
       ^ restarted on every switch                            ^ never restarts
                                                              |
                                                              v
                                                   RTMP (MediaMTX or the
                                                   operator's real server)

Only the feeder is replaced on a switch.  The publisher keeps its RTMP session
open the whole time, so nothing downstream ever learns that the source changed.

Two things make that safe, and both are non-negotiable:

* every feeder encodes to the **same** parameters (:class:`SeamlessProfile`),
  because the publisher copies packets and cannot change codec mid-stream;
* the publisher stamps arrival time over the feeder's timestamps
  (``-use_wallclock_as_timestamps``), because each feeder restarts its clock at
  zero and a backwards jump would break the muxer.

The cost is a real encode per channel, which is why this is opt-in per channel
rather than the default.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass

RELAY_HOST = "127.0.0.1"

#: UDP payload that fits inside one Ethernet frame (7 x 188-byte TS packets).
TS_PACKET_SIZE = 1316

#: Receive buffer on the publisher side, in bytes. Generous on purpose: a
#: momentary scheduling hiccup must not drop packets and corrupt the stream.
RELAY_FIFO_SIZE = 5_000_000


def relay_output_url(port: int, host: str = RELAY_HOST) -> str:
    """URL the feeder writes MPEG-TS to."""
    return f"udp://{host}:{int(port)}?pkt_size={TS_PACKET_SIZE}"


def relay_input_url(port: int, host: str = RELAY_HOST) -> str:
    """URL the publisher reads MPEG-TS from.

    ``overrun_nonfatal`` keeps the publisher alive if the kernel buffer ever
    overflows, and ``timeout=0`` stops FFmpeg from calling a quiet relay - a
    channel between feeders - an error and exiting.
    """
    return (
        f"udp://{host}:{int(port)}"
        f"?fifo_size={RELAY_FIFO_SIZE}&overrun_nonfatal=1&timeout=0&pkt_size={TS_PACKET_SIZE}"
    )


def pick_relay_port(preferred: int, *, host: str = RELAY_HOST, attempts: int = 200) -> int:
    """First free UDP port at or above *preferred*.

    Ports are picked per channel at start time instead of derived from the
    channel id, so a machine already using the port range - or two copies of
    the app - cannot collide silently.
    """
    start = max(1024, int(preferred))
    for offset in range(max(1, attempts)):
        candidate = start + offset
        if candidate > 65_535:
            break
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            try:
                probe.bind((host, candidate))
            except OSError:
                continue
        return candidate
    raise OSError(f"no free UDP port available near {start}")


@dataclass(slots=True)
class SeamlessProfile:
    """The one encoding every feeder for a channel must produce.

    Identical parameters are what allow the publisher to copy packets across a
    source switch.  A 1080p primary and a 720p backup are only interchangeable
    because both are re-encoded to this.
    """

    size: str = "1280x720"
    fps: int = 25
    video_bitrate: str = "2500k"
    audio_bitrate: str = "128k"
    preset: str = "veryfast"
    encoder: str = "libx264"

    @property
    def gop(self) -> int:
        """Two-second GOP: the keyframe cadence players and HLS expect."""
        return max(2, int(self.fps) * 2)

    def video_args(self) -> list[str]:
        args = ["-c:v", self.encoder]
        if self.encoder in ("libx264", "h264"):
            args += ["-preset", self.preset, "-profile:v", "main", "-level", "4.0"]
        args += [
            "-pix_fmt", "yuv420p",
            "-s", self.size,
            "-r", str(int(self.fps)),
            "-g", str(self.gop),
            "-keyint_min", str(self.gop),
            "-sc_threshold", "0",
            "-b:v", self.video_bitrate,
            "-maxrate", self.video_bitrate,
            "-bufsize", _double(self.video_bitrate),
        ]
        return args

    def audio_args(self) -> list[str]:
        return [
            "-c:a", "aac",
            "-b:a", self.audio_bitrate,
            "-ar", "44100",
            "-ac", "2",
        ]

    def encode_args(self) -> list[str]:
        return [*self.video_args(), *self.audio_args()]

    def describe(self) -> str:
        return f"{self.size}@{self.fps} {self.video_bitrate} ({self.encoder})"


def _double(bitrate: str) -> str:
    """``2500k`` -> ``5000k`` for the encoder buffer size."""
    text = str(bitrate).strip().lower()
    if text.endswith(("k", "m")):
        try:
            return f"{int(float(text[:-1]) * 2)}{text[-1]}"
        except ValueError:
            return text
    try:
        return str(int(float(text) * 2))
    except ValueError:
        return text
