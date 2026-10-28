@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================
echo   换装批量生产器 - 局域网网页服务
echo ==========================================
if not exist "server\web.json" (
  echo 首次启动，需要先创建管理员账号。
  python3 run_web.py --init
  if errorlevel 1 (
    pause
    exit /b 1
  )
)
python3 run_web.py
pause
