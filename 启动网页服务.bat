@echo off
rem Daily start: use "启动网页服务-无窗口.vbs" (no console window at all).
rem This .bat is kept for the first-run init, which needs a typed password.
cd /d "%~dp0"
if exist "server\web.json" (
  start "" wscript.exe //nologo "%~dp0启动网页服务-无窗口.vbs"
  exit /b 0
)
echo ==========================================
echo   Outfit LAN Web Service
echo ==========================================
set "PYTHON_CMD="
if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" set "PYTHON_CMD=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not defined PYTHON_CMD if exist "%USERPROFILE%\.local\bin\python3.cmd" set "PYTHON_CMD=%USERPROFILE%\.local\bin\python3.cmd"
if not defined PYTHON_CMD (
  where python3 >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=python3"
)
if not defined PYTHON_CMD (
  echo Python 3 runtime was not found on this host.
  pause
  exit /b 1
)
if not exist "server\web.json" (
  echo First start: create the administrator account.
  call "%PYTHON_CMD%" run_web.py --init
  if errorlevel 1 (
    pause
    exit /b 1
  )
)
call "%PYTHON_CMD%" run_web.py
pause
