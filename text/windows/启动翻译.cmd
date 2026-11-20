@echo off
setlocal
rem Everyday entry: text translation first (gateway and Qwen, no Whisper); audio captions start from the window.
rem Python: %TRANSLATION_DESK_PYTHON% if set, otherwise .venv in this folder.
rem The desk starts ..\..\gateway itself and opens ..\..\audio with %AUDIO_CAPTION_PYTHON% or audio\.venv.
set "TC_PY=%TRANSLATION_DESK_PYTHON%"
if "%TC_PY%"=="" set "TC_PY=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%TC_PY%" (
  echo Python environment not found: %TC_PY%
  echo Create one in this folder: py -3.12 -m venv .venv
  echo and then: .venv\Scripts\python.exe -m pip install -r requirements.txt
  pause
  exit /b 1
)
rem A relative data root means the folder it was started from, for every component started from here.
if defined TRANSLATION_DATA for %%I in ("%TRANSLATION_DATA%") do set "TRANSLATION_DATA=%%~fI"
cd /d "%~dp0"
start "" "%TC_PY%" -m translation_desk %*
exit /b 0