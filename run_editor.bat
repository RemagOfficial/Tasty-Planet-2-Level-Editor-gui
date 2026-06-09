@echo off
setlocal

REM Always run from this script's directory
cd /d "%~dp0"

REM Prefer the Python launcher on Windows, then fall back to python
where py >nul 2>&1
if %errorlevel%==0 (
    py tp2editor.py
) else (
    python tp2editor.py
)

if errorlevel 1 (
    echo.
    echo Failed to start tp2editor.py.
    echo Make sure Python is installed and available in PATH.
    pause
)
