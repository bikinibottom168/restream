@echo off
REM ===========================================================================
REM Restream Manager - remove the firewall rules added by firewall_open.bat.
REM Right-click -> Run as administrator.
REM ===========================================================================
setlocal
net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] This needs administrator rights.
    echo         Right-click firewall_close.bat and choose "Run as administrator".
    pause
    exit /b 1
)

echo [..] Removing the Restream Manager firewall rules...
netsh advfirewall firewall delete rule name="Restream Manager RTMP"
netsh advfirewall firewall delete rule name="Restream Manager HLS"
echo.
echo [OK] Removed. Other devices can no longer reach the buffer ports.
echo.
pause
