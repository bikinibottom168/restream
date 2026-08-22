@echo off
REM ===========================================================================
REM Restream Manager - first-time setup for Windows 10/11
REM Installs everything needed: Python deps, and downloads FFmpeg + MediaMTX
REM into bin\ automatically. Falls back to clear instructions if a step fails.
REM ===========================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo ==========================================
echo   Restream Manager - Windows setup
echo ==========================================
echo.

REM ---- 0. architecture ------------------------------------------------------
set ARCH=amd64
if /I "%PROCESSOR_ARCHITECTURE%"=="ARM64" set ARCH=arm64

REM ---- 1. Python ------------------------------------------------------------
where python >nul 2>&1
if errorlevel 1 (
    echo [WARN] Python was not found on PATH.
    where winget >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] winget is not available to install Python automatically.
        echo         Install Python 3.11+ from https://www.python.org/downloads/
        echo         and tick "Add python.exe to PATH", then run this script again.
        pause
        exit /b 1
    )
    echo [..] Installing Python 3.12 with winget ^(a UAC prompt may appear^)
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    echo.
    echo [OK] Python was installed. PATH changes need a fresh terminal.
    echo      Please CLOSE this window and run setup_windows.bat again.
    pause
    exit /b 0
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)"
if errorlevel 1 (
    echo [ERROR] Python 3.11 or newer is required. Found %PYVER%.
    echo         Install a newer Python from https://www.python.org/downloads/
    pause
    exit /b 1
)
echo [OK] Python %PYVER% found.

REM ---- 2. virtual environment ----------------------------------------------
if not exist ".venv" (
    echo [..] Creating virtual environment in .venv
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Could not create the virtual environment.
        pause
        exit /b 1
    )
) else (
    echo [OK] Virtual environment already exists.
)
call .venv\Scripts\activate.bat

REM ---- 3. dependencies ------------------------------------------------------
echo [..] Upgrading pip
python -m pip install --upgrade pip --quiet
echo [..] Installing Python dependencies (this can take a minute)
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency installation failed. See the messages above.
    pause
    exit /b 1
)
echo [OK] Dependencies installed.

REM ---- 4. .env + folders ----------------------------------------------------
if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo [OK] Created .env from .env.example.
) else (
    echo [OK] .env already exists, leaving it untouched.
)
if not exist "data" mkdir data
if not exist "logs" mkdir logs
if not exist "logs\ffmpeg" mkdir logs\ffmpeg
if not exist "bin" mkdir bin

REM ---- 5. FFmpeg ------------------------------------------------------------
echo.
where ffmpeg >nul 2>&1
if not errorlevel 1 (
    echo [OK] FFmpeg found on PATH.
    goto :mediamtx
)
if exist "bin\ffmpeg.exe" (
    echo [OK] FFmpeg already present in bin\.
    goto :mediamtx
)
echo [..] Downloading FFmpeg ^(release essentials^)...
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile \"$env:TEMP\ff.zip\"; Remove-Item -Recurse -Force \"$env:TEMP\ffx\" -ErrorAction SilentlyContinue; Expand-Archive -Force \"$env:TEMP\ff.zip\" \"$env:TEMP\ffx\"; exit 0 } catch { Write-Host $_; exit 1 }"
if errorlevel 1 (
    echo [WARN] FFmpeg download failed. Get it from https://www.gyan.dev/ffmpeg/builds/
    echo        and copy ffmpeg.exe and ffprobe.exe into the bin\ folder.
    goto :mediamtx
)
for /d %%d in ("%TEMP%\ffx\ffmpeg-*") do (
    copy /Y "%%d\bin\ffmpeg.exe" "bin\ffmpeg.exe" >nul
    copy /Y "%%d\bin\ffprobe.exe" "bin\ffprobe.exe" >nul
)
if exist "bin\ffmpeg.exe" (
    echo [OK] FFmpeg installed into bin\.
) else (
    echo [WARN] Could not place FFmpeg in bin\. Copy ffmpeg.exe/ffprobe.exe there manually.
)

:mediamtx
REM ---- 6. MediaMTX (anti-drop buffer) --------------------------------------
echo.
if exist "bin\mediamtx.exe" (
    echo [OK] MediaMTX already present in bin\.
    goto :selfcheck
)
echo [..] Finding the latest MediaMTX release...
for /f "delims=" %%v in ('powershell -NoProfile -Command "try{(Invoke-RestMethod -UseBasicParsing https://api.github.com/repos/bluenviron/mediamtx/releases/latest).tag_name}catch{'v1.11.3'}"') do set MTXTAG=%%v
if "%MTXTAG%"=="" set MTXTAG=v1.11.3
set MTXURL=https://github.com/bluenviron/mediamtx/releases/download/%MTXTAG%/mediamtx_%MTXTAG%_windows_%ARCH%.zip
echo [..] Downloading MediaMTX %MTXTAG% (%ARCH%)...
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -Uri '%MTXURL%' -OutFile \"$env:TEMP\mtx.zip\"; Remove-Item -Recurse -Force \"$env:TEMP\mtxx\" -ErrorAction SilentlyContinue; Expand-Archive -Force \"$env:TEMP\mtx.zip\" \"$env:TEMP\mtxx\"; Copy-Item -Force \"$env:TEMP\mtxx\mediamtx.exe\" 'bin\mediamtx.exe'; exit 0 } catch { Write-Host $_; exit 1 }"
if exist "bin\mediamtx.exe" (
    echo [OK] MediaMTX installed into bin\. Turn the anti-drop buffer on in Settings.
) else (
    echo [WARN] MediaMTX download failed. Get %MTXURL:~0,60%...
    echo        from https://github.com/bluenviron/mediamtx/releases and put
    echo        mediamtx.exe into the bin\ folder. The app runs fine without it
    echo        ^(the buffer just stays off^).
)

:selfcheck
REM ---- 7. self-check --------------------------------------------------------
echo.
echo [..] Verifying the installation
python -c "import app.main; print('[OK] Application imports cleanly.')"
if errorlevel 1 (
    echo [ERROR] The application could not be imported. See the traceback above.
    pause
    exit /b 1
)

REM ---- 8. auto-start (optional) --------------------------------------------
echo.
echo ------------------------------------------
set /p AUTOSTART="Start the app automatically on every boot and restart it if it crashes? (Y/N): "
if /I "%AUTOSTART%"=="Y" (
    echo [..] Enabling auto-start...
    python -c "import json; from app.core import autostart; r=autostart.install(); print(('[OK] ' if r.get('ok') else '[WARN] ') + (r.get('message') or r.get('error') or ''))"
    echo      ^(You can change this later in Settings, or with autostart_remove.bat^)
) else (
    echo [OK] Skipped. You can enable it later in Settings or with autostart_install.bat.
)

echo.
echo ==========================================
echo   Setup complete
echo ==========================================
echo.
echo   1. Start the application with:  run_windows.bat
echo   2. Open http://127.0.0.1:8787
echo   3. Optional: Settings -^> anti-drop buffer to enable MediaMTX
echo.
pause
