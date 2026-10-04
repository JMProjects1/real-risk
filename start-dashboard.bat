@echo off
cd /d "%~dp0"
title REAL Risk Dashboard
where python >nul 2>nul
if %errorlevel%==0 (
    python dashboard.py
) else (
    py dashboard.py
)
echo.
echo The dashboard has stopped.
pause
