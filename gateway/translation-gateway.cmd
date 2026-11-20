@echo off
setlocal
rem Python: %TRANSLATION_GATEWAY_PYTHON% (or the older %TEAMS_GATEWAY_PYTHON%) if set,
rem otherwise .venv in this folder, otherwise the audio app's environment (the gateway only needs filelock).
set "TC_PY=%TRANSLATION_GATEWAY_PYTHON%"
if "%TC_PY%"=="" set "TC_PY=%TEAMS_GATEWAY_PYTHON%"
if "%TC_PY%"=="" if exist "%~dp0.venv\Scripts\python.exe" set "TC_PY=%~dp0.venv\Scripts\python.exe"
if "%TC_PY%"=="" set "TC_PY=%~dp0..\audio\.venv\Scripts\python.exe"
if not exist "%TC_PY%" (
  echo Python environment not found: %TC_PY%
  echo Create one in this folder: py -3.12 -m venv .venv
  echo and then: .venv\Scripts\python.exe -m pip install -r requirements.txt
  exit /b 1
)
cd /d "%~dp0"
"%TC_PY%" -B -X utf8 -m translation_gateway %*
exit /b %errorlevel%