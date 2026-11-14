@echo off
rem Starts Lirico with a console and verbose logging (shows each line as it is displayed).
cd /d "%~dp0"
".venv\Scripts\python.exe" -m lirico --debug
