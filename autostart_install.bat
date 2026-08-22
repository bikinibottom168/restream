@echo off
REM ===========================================================================
REM Restream Manager - enable auto-start (start on boot + restart on crash)
REM Creates a per-user Scheduled Task. No administrator rights required.
REM ===========================================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] No virtual environment found. Run setup_windows.bat first.
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat

echo [..] Enabling auto-start...
python -c "import json; from app.core import autostart; r=autostart.install(); print(('[OK] ' if r.get('ok') else '[ERROR] ') + (r.get('message') or r.get('error') or ''))"

echo.
echo The app will now start automatically after you log in, and restart itself
echo if it ever stops. You can remove this from the Settings page or by running
echo autostart_remove.bat.
echo.
pause
