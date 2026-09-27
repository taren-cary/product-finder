@echo off
rem Stops the Product Gap Finder dashboard running in the background.
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'streamlit' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Dashboard stopped.
timeout /t 3 >nul
