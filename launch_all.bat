@echo off
:: =============================================================
::  launch_all.bat — Launch Bot 1, 2, and 3 in separate windows
::  Each bot runs independently with its own browser profile.
:: =============================================================

cd /d "%~dp0"

if not exist ".env" (
    echo [ERROR] .env file not found.
    echo Copy .env.example to .env and fill in your values first.
    pause
    exit /b 1
)

:: Create venv once if it does not exist
if not exist "venv" (
    echo [SETUP] Creating virtual environment...
    py -m venv venv
    call "venv\Scripts\activate.bat"
    python -m pip install -r requirements.txt -q
    python -m playwright install chromium --quiet
)

echo [INFO] Starting all three bots in separate windows...

start "CR Bot 1" cmd /k "cd /d %~dp0 && call venv\Scripts\activate.bat && python bot.py --sheet 1"
start "CR Bot 2" cmd /k "cd /d %~dp0 && call venv\Scripts\activate.bat && python bot.py --sheet 2"
start "CR Bot 3" cmd /k "cd /d %~dp0 && call venv\Scripts\activate.bat && python bot.py --sheet 3"

echo [INFO] All bots launched. Close individual windows to stop each bot.
pause
