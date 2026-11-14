@echo off
rem Starts Lirico in the background (no console). Look for its icon in the tray.
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" "Lirico.pyw"
