@echo off
REM ===========================================================================
REM Restream Manager - disable auto-start (remove the Scheduled Task)
REM ===========================================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] No virtual environment found. Run setup_windows.bat first.
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat

echo [..] Removing auto-start...
python -c "import json; from app.core import autostart; r=autostart.remove(); print(('[OK] ' if r.get('ok') else '[ERROR] ') + (r.get('message') or r.get('error') or ''))"

echo.
pause
