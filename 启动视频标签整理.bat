@echo off
rem Start the desktop tag editor without leaving a console window behind:
rem the venv pythonw.exe uses the GUI subsystem, so no black box appears.
rem Falls back to the console python only when the venv is missing.
cd /d "%~dp0"
set "PYW=%~dp0.tmp\tag-venv\Scripts\pythonw.exe"
if exist "%PYW%" (
  start "" "%PYW%" "%~dp0Æô¶¯´ò±êÆ÷.py"
  exit /b 0
)
set "PYTHON_CMD="
if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" set "PYTHON_CMD=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not defined PYTHON_CMD if exist "%USERPROFILE%\.local\bin\python3.cmd" set "PYTHON_CMD=%USERPROFILE%\.local\bin\python3.cmd"
if not defined PYTHON_CMD (
  echo Python 3 runtime was not found on this host.
  pause
  exit /b 1
)
call "%PYTHON_CMD%" tools\template_tag_manager.py
if errorlevel 1 pause
