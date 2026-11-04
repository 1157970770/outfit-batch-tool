@echo off
rem Double-click to register (or remove) the daily tag-store backup task.
rem   install_tag_backup_task.bat            -> register, daily 10:30
rem   install_tag_backup_task.bat -Remove    -> unregister
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_tag_backup_task.ps1" %*
echo.
pause
