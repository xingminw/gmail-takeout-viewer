@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=%~dp0runtime\python-windows-x64\python.exe"
if exist "%PYTHON_EXE%" (
  "%PYTHON_EXE%" -B "%~dp0app.py"
  goto :eof
)

echo Portable Python was not found:
echo   %PYTHON_EXE%
echo.
echo Run this once from PowerShell:
echo   powershell -ExecutionPolicy Bypass -File tools\bootstrap_portable_windows.ps1
echo.
echo Falling back to system Python if available...
py -B "%~dp0app.py"
