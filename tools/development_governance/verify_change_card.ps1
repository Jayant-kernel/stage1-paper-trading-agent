Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$NodeScript = Join-Path $PSScriptRoot "verify_change_card.mjs"
$output = & node $NodeScript --verify
if ($LASTEXITCODE -ne 0) {
    throw "CHANGE_CARD_VERIFICATION_FAILED"
}
$output
