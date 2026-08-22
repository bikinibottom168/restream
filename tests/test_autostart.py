"""Auto-start (start on boot + restart on crash) registration."""

from __future__ import annotations

import subprocess

from app.core import autostart


def test_render_task_xml_has_the_essentials():
    xml = autostart.render_task_xml(
        user_id="PC\\ice",
        command=r"C:\proj\.venv\Scripts\pythonw.exe",
        working_dir=r"C:\proj",
    )
    assert "<LogonTrigger>" in xml
    assert "<UserId>PC\\ice</UserId>" in xml
    assert "pythonw.exe" in xml
    assert "<Arguments>-m app.main</Arguments>" in xml
    assert "<WorkingDirectory>C:\\proj</WorkingDirectory>" in xml
    # restart on crash + no execution time limit (must not be killed after days)
    assert "<RestartOnFailure>" in xml
    assert "<Interval>PT1M</Interval>" in xml
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml


def test_render_launch_agent_has_the_essentials():
    plist = autostart.render_launch_agent(
        python="/proj/.venv/bin/python",
        working_dir="/proj",
        log_dir="/proj/logs",
    )
    assert autostart.LAUNCH_LABEL in plist
    assert "<key>RunAtLoad</key>" in plist
    assert "<key>KeepAlive</key>" in plist
    assert "/proj/.venv/bin/python" in plist
    assert "<string>app.main</string>" in plist


def _fake_run(recorder):
    def run(args):
        recorder.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")
    return run


def test_status_windows_reports_installed(monkeypatch):
    monkeypatch.setattr(autostart, "IS_WINDOWS", True)
    monkeypatch.setattr(autostart, "IS_MACOS", False)
    monkeypatch.setattr(autostart, "_run", lambda args: subprocess.CompletedProcess(args, 0, "", ""))
    result = autostart.status()
    assert result["supported"] is True
    assert result["installed"] is True


def test_status_windows_reports_not_installed(monkeypatch):
    monkeypatch.setattr(autostart, "IS_WINDOWS", True)
    monkeypatch.setattr(autostart, "IS_MACOS", False)
    monkeypatch.setattr(autostart, "_run", lambda args: subprocess.CompletedProcess(args, 1, "", "ERROR: cannot find"))
    assert autostart.status()["installed"] is False


def test_install_windows_writes_xml_and_calls_schtasks(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart, "IS_WINDOWS", True)
    monkeypatch.setattr(autostart, "IS_MACOS", False)
    monkeypatch.setattr(autostart, "PROJECT_ROOT", tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(autostart, "_run", _fake_run(calls))

    result = autostart.install()
    assert result["ok"] is True
    xml_file = tmp_path / "data" / "autostart_task.xml"
    assert xml_file.exists()
    # exactly one schtasks /Create call referencing the XML
    assert any("/Create" in c and "/XML" in c for c in calls)


def test_remove_windows_missing_task_is_success(monkeypatch):
    monkeypatch.setattr(autostart, "IS_WINDOWS", True)
    monkeypatch.setattr(autostart, "IS_MACOS", False)
    monkeypatch.setattr(
        autostart,
        "_run",
        lambda args: subprocess.CompletedProcess(args, 1, "", "ERROR: The system cannot find the file specified."),
    )
    result = autostart.remove()
    assert result["ok"] is True
    assert result["installed"] is False


def test_install_windows_access_denied_gives_hint(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart, "IS_WINDOWS", True)
    monkeypatch.setattr(autostart, "IS_MACOS", False)
    monkeypatch.setattr(autostart, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        autostart,
        "_run",
        lambda args: subprocess.CompletedProcess(args, 1, "", "ERROR: Access is denied."),
    )
    result = autostart.install()
    assert result["ok"] is False
    assert "administrator" in result["error"].lower()
