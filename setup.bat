@echo off
rem One-time setup: creates .venv and installs dependencies.
cd /d "%~dp0"
python -m venv .venv || (echo Python 3.10+ is required: https://www.python.org/downloads/ & exit /b 1)
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
echo.
echo Done. Start Lirico with run.bat
