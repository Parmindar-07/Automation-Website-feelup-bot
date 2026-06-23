@echo off
:: =============================================================
::  launch_bot3.bat — Launch Bot 3 (uses GOOGLE_SHEET_URL_3)
::  Double-click to start, or run from Command Prompt.
:: =============================================================

cd /d "%~dp0"

if not exist ".env" (
    echo [ERROR] .env file not found.
    echo Copy .env.example to .env and fill in your values first.
    pause
    exit /b 1
)

if not exist "venv" (
    echo [SETUP] Creating virtual environment...
    py -m venv venv
)

call "venv\Scripts\activate.bat"
python -m pip install -r requirements.txt -q
python -m playwright install chromium --quiet

echo [INFO] Starting Bot 3...
python bot.py --sheet 3
pause
