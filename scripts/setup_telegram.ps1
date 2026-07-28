[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Setup = Join-Path $PSScriptRoot "setup_telegram.py"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Stage 1 virtual environment was not found."
}

& $Python $Setup
exit $LASTEXITCODE
