@echo off
rem Opens the Product Gap Finder dashboard in your browser.
rem Leave this window open while you use the dashboard; close it to stop.
cd /d "%~dp0"
echo Starting the dashboard... your browser will open in a few seconds.
rem Open the browser once the dashboard has had a moment to start.
start "" /min cmd /c "timeout /t 5 /nobreak >nul & start http://localhost:8501"
".venv\Scripts\streamlit.exe" run dashboard\app.py
pause
