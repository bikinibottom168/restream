"""Windows console hardening.

A Windows console window ships with **Quick Edit** enabled.  Clicking anywhere
in it - or dragging across it, which is easy to do by accident while reading
logs - puts the console into selection mode, and selection mode blocks every
write to stdout until the selection is cleared.

For a normal command-line tool that is harmless.  For this application it is
not: the log line being written belongs to a coroutine on the event loop, so a
blocked ``write`` freezes the loop itself.  The dashboard stops answering, the
watchdog stops reconciling, no channel is started or recovered, and the whole
thing looks hung until the window is closed and reopened.

Two defences, because either alone is incomplete:

* turn Quick Edit off here, so a stray click cannot pause the process;
* put logging behind a queue (see :mod:`app.core.logging`), so even a console
  that does block - a different terminal, a redirected pipe nobody reads -
  cannot reach the event loop.

Everything in this module is a no-op off Windows.
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)

#: Console input flags (winbase.h).
_ENABLE_QUICK_EDIT = 0x0040
_ENABLE_INSERT_MODE = 0x0020
_ENABLE_EXTENDED_FLAGS = 0x0080
_STD_INPUT_HANDLE = -10


def is_windows() -> bool:
    return sys.platform.startswith("win")


def disable_quick_edit() -> bool:
    """Stop a click in the console window from pausing the process.

    Returns ``True`` when the mode was changed, ``False`` when there was
    nothing to do (not Windows, no console attached, or already off).  Never
    raises: failing to harden the console must not stop the app from starting.
    """
    if not is_windows():
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(_STD_INPUT_HANDLE)
        if handle in (0, -1, None):
            return False  # no console (service, pythonw, redirected)

        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False  # stdin is a pipe, not a console

        current = mode.value
        if not current & _ENABLE_QUICK_EDIT:
            return False

        # ENABLE_EXTENDED_FLAGS must be set for the quick-edit bit to be
        # honoured at all; clearing it alone is silently ignored.
        wanted = (current & ~_ENABLE_QUICK_EDIT) | _ENABLE_EXTENDED_FLAGS | _ENABLE_INSERT_MODE
        if not kernel32.SetConsoleMode(handle, wanted):
            return False
        logger.info(
            "console quick-edit disabled - clicking in the window can no longer "
            "pause the application"
        )
        return True
    except Exception:  # noqa: BLE001 - hardening is best-effort by definition
        logger.debug("could not adjust the console mode", exc_info=True)
        return False
