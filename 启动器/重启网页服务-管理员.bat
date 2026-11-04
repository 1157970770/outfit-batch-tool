@echo off
rem Restart the Outfit web server (needs Administrator: the service runs as a
rem scheduled task in session 0, a normal user cannot stop that process).
setlocal
set "PORT=8765"
echo [1/3] stopping process listening on port %PORT% ...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
  echo     taskkill /PID %%p
  taskkill /PID %%p /F
)
rem Use ping instead of timeout: timeout fails when this script is launched
rem from a hidden window (no console input handle).
ping -n 3 127.0.0.1 >nul
echo [2/3] starting scheduled task OutfitWebServer ...
schtasks /run /tn "OutfitWebServer"
ping -n 16 127.0.0.1 >nul
echo [3/3] current listener:
netstat -ano | findstr ":%PORT%" | findstr "LISTENING"
echo.
echo If nothing is listed above, right-click this file and choose
echo "Run as administrator", or reboot and log in again.
rem Write a completion stamp for the VBS launcher (it shows the message box):
rem OK + PID when port is listening, otherwise FAIL.
set "READY="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do set "READY=%%p"
if defined READY (
  >"%TEMP%\outfit_web_restart.stamp" echo OK %READY%
) else (
  >"%TEMP%\outfit_web_restart.stamp" echo FAIL
)
if /i not "%~1"=="silent" pause
