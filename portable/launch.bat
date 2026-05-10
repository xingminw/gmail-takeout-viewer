@echo off
setlocal
cd /d "%~dp0\.."
if exist "runtime\python-windows-x64\python.exe" (
  "runtime\python-windows-x64\python.exe" -B portable\launch.py %*
) else (
  py -B portable\launch.py %*
)
pause
