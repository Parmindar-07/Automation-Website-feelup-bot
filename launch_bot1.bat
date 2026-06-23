@echo off
:: =============================================================
::  launch_bot1.bat — Launch Bot 1 (uses GOOGLE_SHEET_URL)
::  Double-click to start, or run from Command Prompt.
:: =============================================================

cd /d "%~dp0"

:: Verify .env exists before proceeding
if not exist ".env" (
    echo [ERROR] .env file not found.
    echo Copy .env.example to .env and fill in your values first.
    pause
    exit /b 1
)

:: Create virtual environment on first run
if not exist "venv" (
    echo [SETUP] Creating virtual environment...
    py -m venv venv
)

:: Activate venv and install / update dependencies
call "venv\Scripts\activate.bat"
python -m pip install -r requirements.txt -q
python -m playwright install chromium --quiet

:: Start the bot
echo [INFO] Starting Bot 1...
python bot.py --sheet 1
pause
