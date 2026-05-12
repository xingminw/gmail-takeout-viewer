@echo off
setlocal
cd /d "%~dp0"
set GMAIL_VIEWER_READONLY=1
py -B "app\portable_launch.py" %*
pause
