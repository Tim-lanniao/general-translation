@echo off
setlocal
rem Python: %AUDIO_CAPTION_PYTHON% if set, otherwise the .venv created by setup.ps1.
rem The translation gateway in ..\gateway is found automatically and started on demand.
set "TC_PY=%AUDIO_CAPTION_PYTHON%"
if "%TC_PY%"=="" set "TC_PY=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%TC_PY%" (
  echo Python environment not found: %TC_PY%
  echo Run in this folder: powershell -ExecutionPolicy Bypass -File setup.ps1
  pause
  exit /b 1
)
rem A relative data root means the folder it was started from, for every component started from here.
if defined TRANSLATION_DATA for %%I in ("%TRANSLATION_DATA%") do set "TRANSLATION_DATA=%%~fI"
cd /d "%~dp0"
start "" "%TC_PY%" -m audio_caption %*
exit /b 0