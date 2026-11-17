@echo off
setlocal
rem Python: %TEAMS_CAPTION_PYTHON% if set, otherwise the .venv created by setup.ps1
set "TC_PY=%TEAMS_CAPTION_PYTHON%"
if "%TC_PY%"=="" set "TC_PY=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%TC_PY%" (
  echo Python environment not found: %TC_PY%
  echo Run in this folder: powershell -ExecutionPolicy Bypass -File setup.ps1
  pause
  exit /b 1
)
cd /d "%~dp0"
start "" "%TC_PY%" -m teams_caption %*
exit /b 0
