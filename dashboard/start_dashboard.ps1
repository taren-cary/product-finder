# Starts the dashboard in the background if it isn't running, then opens it.
# Called by "Open Dashboard.bat". Output goes to logs\dashboard.log.
$root = Split-Path -Parent $PSScriptRoot
$url = "http://localhost:8501"

function Test-Dashboard {
    try { return (Invoke-WebRequest -UseBasicParsing "$url/_stcore/health" -TimeoutSec 3).Content -eq "ok" }
    catch { return $false }
}

if (-not (Test-Dashboard)) {
    New-Item -ItemType Directory -Force -Path "$root\logs" | Out-Null
    Start-Process -FilePath "$root\.venv\Scripts\streamlit.exe" `
        -ArgumentList "run", "dashboard\app.py" `
        -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput "$root\logs\dashboard.log" `
        -RedirectStandardError "$root\logs\dashboard_errors.log"
    # Wait up to 40 seconds for it to come up.
    foreach ($i in 1..20) {
        Start-Sleep -Seconds 2
        if (Test-Dashboard) { break }
    }
}

Start-Process $url
