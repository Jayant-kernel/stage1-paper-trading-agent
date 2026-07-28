param([string]$TestId = "")
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "credential_store.ps1")
$Target = Resolve-Stage1CredentialTarget -TestId $TestId
Remove-Stage1Credential -Target $Target
if (Test-Stage1Credential -Target $Target) {
    throw "RESEARCH_SIGNING_CREDENTIAL_DELETE_FAILED"
}
[PSCustomObject]@{
    key_id = if ([string]::IsNullOrEmpty($TestId)) {
        "P0_DEVELOPMENT_CHANGE_CARD"
    } else {
        "TEST_ONLY"
    }
    status = "ABSENT"
} | ConvertTo-Json -Compress
