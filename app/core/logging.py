"""Logging configuration.

* console handler for interactive use
* rotating ``logs/app.log`` (10 MB x 5 backups)
* one rotating ``logs/ffmpeg/<channel_id>.log`` per channel
* a filter that scrubs registered secrets from every record
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Iterator

from app.core.security import scrub

MAX_BYTES = 10 * 1024 * 1024  # 10 MB
BACKUP_COUNT = 5

_APP_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-28s | %(message)s"
_FFMPEG_FORMAT = "%(asctime)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_channel_loggers: dict[int, logging.Logger] = {}
_configured = False


class SecretScrubbingFilter(logging.Filter):
    """Replace any registered secret literal in the formatted message."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive, malformed record
            return True
        cleaned = scrub(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    """Configure the root logger. Safe to call more than once."""
    global _configured
    if _configured:
        return

    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    formatter = logging.Formatter(_APP_FORMAT, datefmt=_DATE_FORMAT)
    scrubber = SecretScrubbingFilter()

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    console.addFilter(scrubber)
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "app.log",
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(scrubber)
    root.addHandler(file_handler)

    # Uvicorn's access log is noisy next to a 3-second dashboard poll.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def get_channel_logger(channel_id: int, ffmpeg_log_dir: Path) -> logging.Logger:
    """Return (and lazily create) the dedicated FFmpeg logger for a channel."""
    if channel_id in _channel_loggers:
        return _channel_loggers[channel_id]

    ffmpeg_log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"ffmpeg.channel.{channel_id}")
    logger.setLevel(logging.INFO)
    logger.propagate = False  # keep FFmpeg noise out of app.log

    if not logger.handlers:
        handler = logging.handlers.RotatingFileHandler(
            ffmpeg_log_dir / f"{channel_id}.log",
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(_FFMPEG_FORMAT, datefmt=_DATE_FORMAT))
        handler.addFilter(SecretScrubbingFilter())
        logger.addHandler(handler)

    _channel_loggers[channel_id] = logger
    return logger


def close_channel_logger(channel_id: int) -> None:
    """Release the file handle for a channel logger (used on delete/shutdown)."""
    logger = _channel_loggers.pop(channel_id, None)
    if logger is None:
        return
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def close_all_channel_loggers() -> None:
    for channel_id in list(_channel_loggers):
        close_channel_logger(channel_id)


def tail_file(path: Path, lines: int = 200, chunk_size: int = 8192) -> list[str]:
    """Read the last *lines* lines of a file without loading it into RAM.

    Reads backwards in chunks; returns an empty list when the file is missing.
    """
    if not path.exists() or not path.is_file():
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            end = handle.tell()
            buffer = b""
            newline_count = 0
            position = end
            while position > 0 and newline_count <= lines:
                read_size = min(chunk_size, position)
                position -= read_size
                handle.seek(position)
                chunk = handle.read(read_size)
                buffer = chunk + buffer
                newline_count = buffer.count(b"\n")
            text = buffer.decode("utf-8", errors="replace")
    except OSError:
        return []
    result = text.splitlines()
    return result[-lines:]


def iter_log_files(ffmpeg_log_dir: Path) -> Iterator[Path]:
    if not ffmpeg_log_dir.exists():
        return iter(())
    return (p for p in sorted(ffmpeg_log_dir.glob("*.log")))
