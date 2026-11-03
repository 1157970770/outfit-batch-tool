@echo off
rem Outfit web server start guard:
rem 1) cd to this script's folder (no Chinese chars needed in content)
rem 2) skip if port 8765 is already listening (prevents double start)
rem 3) start the server in foreground
cd /d "%~dp0"
netstat -ano | findstr ":8765" | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 exit /b 0
"C:\Users\EDY\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" -u run_web.py
