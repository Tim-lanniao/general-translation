@echo off
setlocal
set "TC_PY=%TEAMS_GATEWAY_PYTHON%"
if "%TC_PY%"=="" set "TC_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%TC_PY%" (
  echo Python environment not found. Run setup.ps1 first.
  exit /b 1
)
cd /d "%~dp0"
"%TC_PY%" -B -X utf8 -m teams_caption.gateway %*
exit /b %errorlevel%
