@echo off
cd /d "%~dp0"
title Viral Idea Engine
echo ============================================
echo  Viral Idea Engine
echo ============================================
echo.

REM Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found.
    echo Please install Python 3.11 or newer from https://python.org
    echo Make sure to tick "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

REM Create venv on first run
if not exist ".venv" (
    echo Setting up for the first time - this takes about a minute...
    echo.
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Could not create virtual environment.
        echo Try running: python -m venv .venv
        echo.
        pause
        exit /b 1
    )
)

REM Activate venv
call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo ERROR: Could not activate virtual environment.
    echo Delete the .venv folder and try again.
    echo.
    pause
    exit /b 1
)

REM Install / update dependencies
echo Installing dependencies...
python -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: Dependency install failed. See error above.
    echo.
    pause
    exit /b 1
)

echo.
echo Starting server on http://127.0.0.1:8787 ...
echo Opening browser in 3 seconds...
echo.
echo Leave this window open while you use the app.
echo Close it (or press Ctrl+C) to stop the server.
echo.

REM Open browser after a short delay (in background)
start "" cmd /c "timeout /t 3 /nobreak >nul && start http://127.0.0.1:8787"

REM UTF-8 mode so Unicode characters in API responses don't crash the console
set PYTHONUTF8=1
chcp 65001 >nul 2>&1

REM Run uvicorn in THIS window so errors are visible
python -m uvicorn app.main:app --host 127.0.0.1 --port 8787

echo.
echo Server stopped.
pause
