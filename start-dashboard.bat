@echo off
setlocal
cd /d "%~dp0"
title REAL Risk Dashboard (unofficial)

if not exist "%~dp0dashboard.py" (
  echo.
  echo The dashboard files are missing from this folder.
  echo If you opened this from inside the ZIP, close it, right-click the ZIP,
  echo choose "Extract All", then run start-dashboard.bat from the extracted folder.
  echo.
  pause
  exit /b 1
)

rem Find a working Python. "py" comes with the Python install manager; "python" can be
rem a Microsoft Store placeholder, so each one is only used if it actually runs.
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if not defined PY (
  echo.
  echo Python isn't installed, or Windows can't find it.
  echo Install it from https://www.python.org/downloads/ using the Python install manager,
  echo then double-click start-dashboard.bat again.
  echo.
  pause
  exit /b 1
)

%PY% dashboard.py %*
echo.
echo The dashboard has stopped.
pause
