@echo off
rem Opens the Product Gap Finder dashboard in your browser.
rem The dashboard runs hidden in the background (no window to close by accident).
rem If it's already running, this just opens the browser. To stop it, use Stop Dashboard.bat.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0dashboard\start_dashboard.ps1"
