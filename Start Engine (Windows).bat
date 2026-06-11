@echo off
cd /d "%~dp0"
echo Viral Idea Engine — Starting...

if not exist ".venv" (
  echo Setting up for first time (this takes about a minute)...
  python -m venv .venv
)

call .venv\Scripts\activate.bat
pip install -q -r requirements.txt

echo Starting server...
start "" python -m uvicorn app.main:app --host 127.0.0.1 --port 8787

timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:8787

echo.
echo Server is running at http://127.0.0.1:8787
echo Close this window to stop the server.
echo.
pause
