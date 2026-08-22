"""Start the app on boot and keep it alive - without administrator rights.

Two things people want from an unattended box: the app should come back on its
own after a power cut or reboot, and if it ever crashes it should restart
itself.  Both are done with the operating system's own supervisor so nothing
extra has to run:

* **Windows** - a per-user Scheduled Task triggered at logon, with
  ``RestartOnFailure`` every minute and no execution-time limit, launched with
  ``pythonw.exe`` so there is no console window.  A logon task in the user's own
  context needs no elevation.
* **macOS** - a LaunchAgent in ``~/Library/LaunchAgents`` with ``RunAtLoad`` and
  ``KeepAlive`` (login start + automatic crash restart).  A user LaunchAgent
  needs no ``sudo``.

The render functions are pure and unit-tested; :func:`install`, :func:`remove`
and :func:`status` shell out to ``schtasks`` / ``launchctl``.  Nothing here
takes user input, so there is no command injection surface - every argument is
a fixed switch or a path this process computed itself.
"""

from __future__ import annotations

import getpass
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

#: Project root: <root>/app/core/autostart.py -> parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Identity of the scheduled task / launch agent.
TASK_NAME = "RestreamManager"
LAUNCH_LABEL = "com.restreammanager.app"

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"


# --------------------------------------------------------------------------- #
# executable / path helpers
# --------------------------------------------------------------------------- #
def _python_executable() -> str:
    """The interpreter to launch. On Windows prefer ``pythonw`` (no console)."""
    exe = Path(sys.executable)
    if IS_WINDOWS:
        windowless = exe.with_name("pythonw.exe")
        if windowless.exists():
            return str(windowless)
    return str(exe)


def _current_user() -> str:
    """``DOMAIN\\User`` for the Windows task principal."""
    user = getpass.getuser()
    domain = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME") or ""
    return f"{domain}\\{user}" if domain else user


# --------------------------------------------------------------------------- #
# Windows: scheduled task XML (pure)
# --------------------------------------------------------------------------- #
def render_task_xml(
    *,
    user_id: str,
    command: str,
    working_dir: str,
    arguments: str = "-m app.main",
) -> str:
    """Render the Task Scheduler XML for a logon-start, self-restarting task."""
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" '
        'xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        "    <Description>Restream Manager - start on logon and keep alive</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <LogonTrigger>\n"
        "      <Enabled>true</Enabled>\n"
        f"      <UserId>{user_id}</UserId>\n"
        "    </LogonTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        f"      <UserId>{user_id}</UserId>\n"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <IdleSettings>\n"
        "      <StopOnIdleEnd>false</StopOnIdleEnd>\n"
        "      <RestartOnIdle>false</RestartOnIdle>\n"
        "    </IdleSettings>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>false</Hidden>\n"
        "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n"
        "    <WakeToRun>false</WakeToRun>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <Priority>7</Priority>\n"
        "    <RestartOnFailure>\n"
        "      <Interval>PT1M</Interval>\n"
        "      <Count>999</Count>\n"
        "    </RestartOnFailure>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{command}</Command>\n"
        f"      <Arguments>{arguments}</Arguments>\n"
        f"      <WorkingDirectory>{working_dir}</WorkingDirectory>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


# --------------------------------------------------------------------------- #
# macOS: LaunchAgent plist (pure)
# --------------------------------------------------------------------------- #
def render_launch_agent(
    *,
    python: str,
    working_dir: str,
    log_dir: str,
) -> str:
    """Render a LaunchAgent that starts at login and restarts on crash."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "  <key>Label</key>\n"
        f"  <string>{LAUNCH_LABEL}</string>\n"
        "  <key>ProgramArguments</key>\n"
        "  <array>\n"
        f"    <string>{python}</string>\n"
        "    <string>-m</string>\n"
        "    <string>app.main</string>\n"
        "  </array>\n"
        "  <key>WorkingDirectory</key>\n"
        f"  <string>{working_dir}</string>\n"
        "  <key>RunAtLoad</key>\n"
        "  <true/>\n"
        "  <key>KeepAlive</key>\n"
        "  <true/>\n"
        "  <key>StandardOutPath</key>\n"
        f"  <string>{log_dir}/autostart.out.log</string>\n"
        "  <key>StandardErrorPath</key>\n"
        f"  <string>{log_dir}/autostart.err.log</string>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def supported() -> bool:
    return IS_WINDOWS or IS_MACOS


def platform_name() -> str:
    if IS_WINDOWS:
        return "windows"
    if IS_MACOS:
        return "macos"
    return sys.platform


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=30, check=False
    )


def status() -> dict[str, Any]:
    """Report whether auto-start is installed."""
    if not supported():
        return {
            "supported": False,
            "installed": False,
            "platform": platform_name(),
            "error": "auto-start is only wired up for Windows and macOS",
        }
    try:
        if IS_WINDOWS:
            result = _run(["schtasks", "/Query", "/TN", TASK_NAME])
            installed = result.returncode == 0
        else:
            installed = _launch_agent_path().exists()
        return {
            "supported": True,
            "installed": installed,
            "platform": platform_name(),
            "error": "",
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "supported": True,
            "installed": False,
            "platform": platform_name(),
            "error": str(exc),
        }


def install() -> dict[str, Any]:
    """Register auto-start for the current user. No elevation required."""
    if not supported():
        return {"ok": False, "error": "auto-start is only supported on Windows and macOS"}
    try:
        if IS_WINDOWS:
            return _install_windows()
        return _install_macos()
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        return {"ok": False, "error": str(exc)}


def remove() -> dict[str, Any]:
    """Remove the auto-start entry."""
    if not supported():
        return {"ok": False, "error": "auto-start is only supported on Windows and macOS"}
    try:
        if IS_WINDOWS:
            return _remove_windows()
        return _remove_macos()
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        return {"ok": False, "error": str(exc)}


# --------------------------------------------------------------------------- #
# Windows implementation
# --------------------------------------------------------------------------- #
def _install_windows() -> dict[str, Any]:
    xml = render_task_xml(
        user_id=_current_user(),
        command=_python_executable(),
        working_dir=str(PROJECT_ROOT),
    )
    xml_path = PROJECT_ROOT / "data" / "autostart_task.xml"
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    # Task Scheduler wants UTF-16 for its XML import.
    xml_path.write_text(xml, encoding="utf-16")
    result = _run(
        ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"]
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        hint = ""
        if "denied" in detail.lower() or "access" in detail.lower():
            hint = (
                " - Windows refused it without elevation. Right-click "
                "autostart_install.bat and 'Run as administrator', or run the "
                "app once as administrator and try again."
            )
        return {"ok": False, "error": (detail or "schtasks failed") + hint}
    return {"ok": True, "installed": True, "message": "auto-start installed"}


def _remove_windows() -> dict[str, Any]:
    result = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().lower()
        if "cannot find" in detail or "does not exist" in detail:
            return {"ok": True, "installed": False, "message": "auto-start was not set"}
        return {"ok": False, "error": (result.stderr or result.stdout).strip()}
    return {"ok": True, "installed": False, "message": "auto-start removed"}


# --------------------------------------------------------------------------- #
# macOS implementation
# --------------------------------------------------------------------------- #
def _install_macos() -> dict[str, Any]:
    plist_path = _launch_agent_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(
        render_launch_agent(
            python=str(Path(sys.executable)),
            working_dir=str(PROJECT_ROOT),
            log_dir=str(log_dir),
        ),
        encoding="utf-8",
    )
    # Reload so it takes effect now (ignore unload errors on a fresh install).
    _run(["launchctl", "unload", str(plist_path)])
    result = _run(["launchctl", "load", "-w", str(plist_path)])
    if result.returncode != 0:
        return {"ok": False, "error": (result.stderr or result.stdout).strip()}
    return {"ok": True, "installed": True, "message": "auto-start installed"}


def _remove_macos() -> dict[str, Any]:
    plist_path = _launch_agent_path()
    if plist_path.exists():
        _run(["launchctl", "unload", "-w", str(plist_path)])
        try:
            plist_path.unlink()
        except OSError as exc:  # pragma: no cover
            return {"ok": False, "error": str(exc)}
    return {"ok": True, "installed": False, "message": "auto-start removed"}
