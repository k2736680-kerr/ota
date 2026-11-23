@echo off
cd /d "%~dp0"
echo Wearable OTA console: http://127.0.0.1:8765
echo Close this window to stop it.
python start.py
if not "%ERRORLEVEL%"=="0" pause
