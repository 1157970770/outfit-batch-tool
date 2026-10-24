@echo off
setlocal
cd /d "%~dp0.."
set "PY="
call :try python3
if not defined PY call :try py
if not defined PY call :try python
if not defined PY goto nopython
echo Using Python: %PY%
call "%PY%" "%~dp0password_manager.py" %*
if errorlevel 1 pause
goto :eof

:try
if defined PY exit /b 0
call %1 -c "import PySide6" >nul 2>&1
if errorlevel 1 exit /b 0
set "PY=%1"
exit /b 0

:nopython
echo [ERROR] No Python with PySide6 found.
echo Install it first:  pip install PySide6
pause
exit /b 1
