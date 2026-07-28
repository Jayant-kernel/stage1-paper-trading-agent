$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$DashboardRoot = Join-Path $ProjectRoot "dashboard"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$BridgeScript = Join-Path $ProjectRoot "scripts\run_control_room.py"

function Test-LocalUrl {
    param([string]$Url)
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

if (-not (Test-LocalUrl "http://127.0.0.1:8766/api/health")) {
    Start-Process `
        -FilePath $Python `
        -ArgumentList @($BridgeScript) `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden
}

if (-not (Test-LocalUrl "http://localhost:3000")) {
    Start-Process `
        -FilePath "npm.cmd" `
        -ArgumentList @("run", "dev") `
        -WorkingDirectory $DashboardRoot `
        -WindowStyle Hidden
}

$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    if (
        (Test-LocalUrl "http://127.0.0.1:8766/api/health") -and
        (Test-LocalUrl "http://localhost:3000")
    ) {
        $ready = $true
        break
    }
    Start-Sleep -Milliseconds 500
}

if (-not $ready) {
    throw "The control room did not become ready. Re-run this command once."
}

Write-Host "Stage 1 control room is ready at http://localhost:3000" -ForegroundColor Green
Start-Process "http://localhost:3000"
