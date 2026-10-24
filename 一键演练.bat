@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo === 万相3.0 换装批量：离线演练（不花钱、不需要 Key）===
echo.
python3 demo.py --count 5
echo.
pause
