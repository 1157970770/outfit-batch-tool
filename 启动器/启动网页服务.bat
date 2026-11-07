@echo off
chcp 936 >nul
rem ^ 上面这行必须留着：本文件是 ANSI(GBK) 编码，先用 chcp 把控制台代码页固定成 936，
rem   否则在 UTF-8 控制台里中文标注会变成乱码。改中文内容请仍用 ANSI/GBK 保存。
rem
rem Daily start: use "启动网页服务-无窗口.vbs" (no console window at all).
rem This .bat is kept for the first-run init, which needs a typed password.
rem
rem 分区标注（两区共用这一份脚本，不要把「开发版」写死进代码）：
rem   server\web.json 里 dev_mode=true  → 开发版（8766 / web-data-dev）
rem   没有 dev_mode，或值为 false      → 生产版（8765 / web-data）
rem 端口和数据目录也从 web.json 读；开发版窗口会停约 3 秒，让人看清标注。
setlocal
cd /d "%~dp0.."

set "ZONE=生产版"
set "PORT="
set "DATA_ROOT="
if exist "server\web.json" (
  findstr /i /c:"dev_mode" "server\web.json" ^| findstr /i /c:"true" >nul 2>nul
  if not errorlevel 1 set "ZONE=开发版"
  for /f "tokens=2 delims=:" %%p in ('findstr /i /c:"port" "server\web.json"') do set "PORT=%%p"
  for /f "tokens=2 delims=:" %%p in ('findstr /i /c:"data_root" "server\web.json"') do set "DATA_ROOT=%%p"
)
set "PORT=%PORT: =%"
set "PORT=%PORT:,=%"
set "PORT=%PORT:"=%"
set "DATA_ROOT=%DATA_ROOT: =%"
set "DATA_ROOT=%DATA_ROOT:,=%"
set "DATA_ROOT=%DATA_ROOT:"=%"
if not defined PORT set "PORT=8765"
if not defined DATA_ROOT set "DATA_ROOT=web-data"

title 换装批量生产器 · %ZONE% · %PORT%
echo.
echo   ================================================
echo     换装批量生产器      [%ZONE%]
echo     端口 %PORT%         数据目录 %DATA_ROOT%
echo   ================================================
echo.
if /i "%~1"=="--check" exit /b 0

if exist "server\web.json" (
  start "" wscript.exe //nologo "%~dp0..\启动网页服务.vbs"
  if /i "%ZONE%"=="开发版" (
    echo   [开发版] 已在后台启动，约 7 秒后自动打开 http://127.0.0.1:%PORT%/
    ping -n 4 127.0.0.1 >nul
  )
  exit /b 0
)

echo ==========================================
echo   Outfit LAN Web Service   [%ZONE%]
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