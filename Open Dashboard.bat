@echo off
rem Opens the Product Gap Finder dashboard in your browser.
rem Leave this window open while you use the dashboard; close it to stop.
cd /d "%~dp0"
".venv\Scripts\streamlit.exe" run dashboard\app.py --server.headless false --browser.gatherUsageStats false
pause
