@echo off
rem Start WeBuilt Quote on Windows: double-click this file.
rem Finds Python 3.10+ and hands over to launch.py, which does the rest.
setlocal
cd /d "%~dp0"
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=python"
if not defined PY (
  echo WeBuilt Quote needs Python 3.10 or newer.
  echo Install it from https://www.python.org/downloads/windows/
  echo and tick "Add python.exe to PATH" during setup, then run this again.
  pause
  exit /b 1
)
rem Keep this on one line: an update may replace this file while it runs,
rem and Windows reads a batch file line by line as it goes.
%PY% launch.py %* & exit /b
