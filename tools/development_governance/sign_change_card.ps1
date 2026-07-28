param([string]$TestId = "")
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$NodeScript = Join-Path $PSScriptRoot "sign_change_card.mjs"
. (Join-Path $PSScriptRoot "credential_store.ps1")
$Target = Resolve-Stage1CredentialTarget -TestId $TestId

if (-not [string]::IsNullOrEmpty($TestId)) {
    throw "TEST_ONLY_SIGNING_USES_EPHEMERAL_NODE_MODE"
}

$privateMaterial = Get-Stage1CredentialSecret -Target $Target
$startInfo = [Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = "node"
$startInfo.Arguments = '"' + $NodeScript.Replace('"', '\"') + '" --sign'
$startInfo.RedirectStandardInput = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$process = [Diagnostics.Process]::new()
$process.StartInfo = $startInfo
try {
    [void]$process.Start()
    $process.StandardInput.Write($privateMaterial)
    $process.StandardInput.Close()
    $privateMaterial = $null
    $output = $process.StandardOutput.ReadToEnd()
    $errorOutput = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) {
        throw "CHANGE_CARD_SIGNING_FAILED:$errorOutput"
    }
    $output
}
finally {
    $privateMaterial = $null
    $process.Dispose()
}
