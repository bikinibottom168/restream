@echo off
REM ===========================================================================
REM Restream Manager - start the application (Windows)
REM ===========================================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] No virtual environment found.
    echo         Run setup_windows.bat first.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat

if not exist ".env" (
    echo [WARN] No .env file found - using built-in defaults.
)

echo.
echo Starting Restream Manager...
echo Press Ctrl+C to stop. FFmpeg processes are shut down cleanly on exit.
echo.

python -m app.main
set EXITCODE=%errorlevel%

if not "%EXITCODE%"=="0" (
    echo.
    echo [ERROR] The application exited with code %EXITCODE%.
    echo         Check logs\app.log for details.
)
pause
exit /b %EXITCODE%
