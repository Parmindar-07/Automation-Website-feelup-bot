@echo off
cd /d "%~dp0"

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo .env file bana di gayi hai. Pehle .env update karo, phir Launcher.bat dobara chalao.
  pause
  exit /b 1
)

if not exist "venv" (
  py -m venv venv
)

call "venv\Scripts\activate.bat"
python -m pip install -r requirements.txt
python -m playwright install chromium
python bot.py
pause
