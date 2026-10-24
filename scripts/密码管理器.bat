@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY="
for %%P in (python3 python py) do (
  if not defined PY (
    %%P -c "import PySide6" >nul 2>&1 && set "PY=%%P"
  )
)
if not defined PY (
  echo 没找到带 PySide6 的 Python。
  echo 先跑一次：pip install PySide6
  pause
  exit /b 1
)
%PY% "%~dp0密码管理器.py"
if errorlevel 1 pause
