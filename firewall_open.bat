@echo off
REM ===========================================================================
REM Restream Manager - open the buffer (MediaMTX) ports on Windows Firewall
REM so viewers on other devices (phone / another PC) can watch.
REM   RTMP 1935  +  HLS 8888.  Right-click -> Run as administrator.
REM If you changed the ports in Settings, edit the numbers below.
REM ===========================================================================
setlocal
set RTMP_PORT=1935
set HLS_PORT=8888

net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] This needs administrator rights.
    echo         Right-click firewall_open.bat and choose "Run as administrator".
    pause
    exit /b 1
)

echo [..] Allowing inbound TCP %RTMP_PORT% (RTMP) and %HLS_PORT% (HLS)...
netsh advfirewall firewall delete rule name="Restream Manager RTMP" >nul 2>&1
netsh advfirewall firewall delete rule name="Restream Manager HLS" >nul 2>&1
netsh advfirewall firewall add rule name="Restream Manager RTMP" dir=in action=allow protocol=TCP localport=%RTMP_PORT%
netsh advfirewall firewall add rule name="Restream Manager HLS" dir=in action=allow protocol=TCP localport=%HLS_PORT%

echo.
echo [OK] Done. Other devices on the same network can now reach:
echo      HLS : http://THIS-PC-IP:%HLS_PORT%/chN/index.m3u8
echo      RTMP: rtmp://THIS-PC-IP:%RTMP_PORT%/chN
echo      (find THIS-PC-IP with 'ipconfig' - the IPv4 Address)
echo.
echo To undo this later, run firewall_close.bat as administrator.
echo.
pause
