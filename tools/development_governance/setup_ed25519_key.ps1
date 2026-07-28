param(
    [string]$TestId = "",
    [switch]$TestAllowExistingPublic,
    [switch]$TestFailAfterPublicReplacement,
    [switch]$TestTamperTransportTag
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$NodeScript = Join-Path $PSScriptRoot "sign_change_card.mjs"
. (Join-Path $PSScriptRoot "credential_store.ps1")

function Assert-ControlledPath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$AllowMissingLeaf
    )
    $rootFull = [IO.Path]::GetFullPath($Root)
    $pathFull = [IO.Path]::GetFullPath($Path)
    $prefix = $rootFull.TrimEnd('\') + '\'
    if (-not $pathFull.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "CONTROLLED_PATH_ESCAPE"
    }
    $relative = $pathFull.Substring($prefix.Length)
    $cursor = $rootFull
    $parts = $relative.Split('\', [StringSplitOptions]::RemoveEmptyEntries)
    for ($index = 0; $index -lt $parts.Count; $index++) {
        $cursor = Join-Path $cursor $parts[$index]
        if (-not (Test-Path -LiteralPath $cursor)) {
            if ($AllowMissingLeaf -and $index -eq ($parts.Count - 1)) { return }
            throw "CONTROLLED_PATH_MISSING"
        }
        $item = Get-Item -LiteralPath $cursor -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "CONTROLLED_PATH_REPARSE"
        }
    }
}

function Convert-HexBytes {
    param([Parameter(Mandatory = $true)][string]$Hex)
    if ($Hex -cnotmatch '^[0-9a-f]{64}$') { throw "TRANSPORT_KEY_INVALID" }
    $bytes = [byte[]]::new(32)
    for ($index = 0; $index -lt 32; $index++) {
        $bytes[$index] = [Convert]::ToByte($Hex.Substring($index * 2, 2), 16)
    }
    return $bytes
}

function Test-FixedTimeBytes {
    param(
        [Parameter(Mandatory = $true)][byte[]]$Left,
        [Parameter(Mandatory = $true)][byte[]]$Right
    )
    if ($Left.Length -ne $Right.Length) { return $false }
    [int]$difference = 0
    for ($index = 0; $index -lt $Left.Length; $index++) {
        $difference = $difference -bor ($Left[$index] -bxor $Right[$index])
    }
    return $difference -eq 0
}

$Target = Resolve-Stage1CredentialTarget -TestId $TestId
$IsTest = -not [string]::IsNullOrEmpty($TestId)
if (
    ($TestAllowExistingPublic -or $TestFailAfterPublicReplacement -or
        $TestTamperTransportTag) -and -not $IsTest
) {
    throw "TEST_SWITCH_REQUIRES_TEST_ID"
}
if ($TestFailAfterPublicReplacement -and -not $TestAllowExistingPublic) {
    throw "TEST_FAILURE_REQUIRES_EXISTING_PUBLIC"
}
if ($IsTest) {
    $TestRoot = Join-Path $ProjectRoot "docs\stage1_p0\patches\P0-A\test-only\$TestId"
    $AllowedRoot = Join-Path $ProjectRoot "docs\stage1_p0\patches\P0-A\test-only"
    New-Item -ItemType Directory -Force -Path $AllowedRoot | Out-Null
    Assert-ControlledPath -Root $ProjectRoot -Path $AllowedRoot
    if (-not (Test-Path -LiteralPath $TestRoot)) {
        New-Item -ItemType Directory -Path $TestRoot | Out-Null
    }
    Assert-ControlledPath -Root $AllowedRoot -Path $TestRoot
    $PublicKeyPath = Join-Path $TestRoot "public.pem"
}
else {
    $PublicKeyPath = Join-Path $ProjectRoot "security\research-signing-ed25519.pub.pem"
}

Assert-ControlledPath -Root $ProjectRoot -Path $PublicKeyPath -AllowMissingLeaf
if (Test-Stage1Credential -Target $Target) {
    throw "RESEARCH_SIGNING_CREDENTIAL_ALREADY_EXISTS"
}
if (
    $IsTest -and (Test-Path -LiteralPath $PublicKeyPath) -and
    -not $TestAllowExistingPublic
) {
    throw "RESEARCH_PUBLIC_KEY_ALREADY_EXISTS"
}

$transport = [byte[]]::new(32)
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($transport)
$transportHex = -join ($transport | ForEach-Object { $_.ToString("x2") })
$key = $null
$iv = $null
$encrypted = $null
$providedTag = $null
$expectedTag = $null
$hmac = $null
$authenticatedMaterial = $null
$publicBytes = $null
$fingerprintBytes = $null
$aes = $null
$decryptor = $null
$privateBytes = $null
$privateMaterial = $null
$temporaryPublic = $null
$backupPublic = $null
$oldPublicBytes = $null
$publicWasReplaced = $false
try {
    $generatedJson = $transportHex | & node $NodeScript --generate-wrapped
    if ($LASTEXITCODE -ne 0) { throw "ED25519_GENERATION_FAILED" }
    $generated = $generatedJson | ConvertFrom-Json
    $key = Convert-HexBytes -Hex $transportHex
    $iv = [Convert]::FromBase64String([string]$generated.iv)
    $encrypted = [Convert]::FromBase64String([string]$generated.encryptedPrivateKey)
    $providedTag = [Convert]::FromBase64String([string]$generated.tag)
    if ($TestTamperTransportTag) {
        $providedTag[0] = $providedTag[0] -bxor 1
    }
    $normalizedPublic = ([string]$generated.publicKey).Replace("`r`n", "`n")
    $publicBytes = [Text.Encoding]::UTF8.GetBytes($normalizedPublic)
    $fingerprintBytes = [Text.Encoding]::ASCII.GetBytes([string]$generated.fingerprint)
    $authenticatedMaterial = [byte[]]::new(
        $iv.Length + $encrypted.Length + $publicBytes.Length + $fingerprintBytes.Length
    )
    [Array]::Copy($iv, 0, $authenticatedMaterial, 0, $iv.Length)
    [Array]::Copy($encrypted, 0, $authenticatedMaterial, $iv.Length, $encrypted.Length)
    [Array]::Copy(
        $publicBytes,
        0,
        $authenticatedMaterial,
        $iv.Length + $encrypted.Length,
        $publicBytes.Length
    )
    [Array]::Copy(
        $fingerprintBytes,
        0,
        $authenticatedMaterial,
        $iv.Length + $encrypted.Length + $publicBytes.Length,
        $fingerprintBytes.Length
    )
    $hmac = [Security.Cryptography.HMACSHA256]::new($key)
    $expectedTag = $hmac.ComputeHash($authenticatedMaterial)
    if (-not (Test-FixedTimeBytes -Left $expectedTag -Right $providedTag)) {
        throw "ED25519_TRANSPORT_AUTHENTICATION_FAILED"
    }
    $aes = [Security.Cryptography.Aes]::Create()
    $aes.Mode = [Security.Cryptography.CipherMode]::CBC
    $aes.Padding = [Security.Cryptography.PaddingMode]::PKCS7
    $aes.Key = $key
    $aes.IV = $iv
    $decryptor = $aes.CreateDecryptor()
    $privateBytes = $decryptor.TransformFinalBlock($encrypted, 0, $encrypted.Length)
    $privateMaterial = [Text.Encoding]::UTF8.GetString($privateBytes)

    $pairInput = [PSCustomObject]@{
        privateKey = $privateMaterial
        publicKey = $normalizedPublic
    } | ConvertTo-Json -Compress
    $pairResultJson = $pairInput | & node $NodeScript --check-pair
    $pairInput = $null
    if ($LASTEXITCODE -ne 0) { throw "ED25519_KEY_PAIR_MISMATCH" }
    $pairResult = $pairResultJson | ConvertFrom-Json
    if (
        [string]$pairResult.status -cne "PAIR_VERIFIED" -or
        [string]$pairResult.fingerprint -cne [string]$generated.fingerprint
    ) {
        throw "ED25519_GENERATED_FINGERPRINT_MISMATCH"
    }

    Set-Stage1Credential -Target $Target -Secret $privateMaterial

    $parent = Split-Path -Parent $PublicKeyPath
    $temporaryPublic = Join-Path $parent (".research-signing-" + [guid]::NewGuid().ToString("N") + ".tmp")
    [IO.File]::WriteAllText(
        $temporaryPublic,
        $normalizedPublic,
        [Text.UTF8Encoding]::new($false)
    )
    Assert-ControlledPath -Root $ProjectRoot -Path $temporaryPublic
    if (Test-Path -LiteralPath $PublicKeyPath) {
        $oldPublicBytes = [IO.File]::ReadAllBytes($PublicKeyPath)
        $backupPublic = Join-Path $parent (
            ".research-signing-backup-" + [guid]::NewGuid().ToString("N") + ".tmp"
        )
        [IO.File]::Replace($temporaryPublic, $PublicKeyPath, $backupPublic)
        $publicWasReplaced = $true
    }
    else {
        Move-Item -LiteralPath $temporaryPublic -Destination $PublicKeyPath
    }
    $temporaryPublic = $null
    if ($TestFailAfterPublicReplacement) {
        throw "TEST_POST_REPLACEMENT_FAILURE"
    }
    $installedPublic = [IO.File]::ReadAllText($PublicKeyPath).Replace("`r`n", "`n")
    $installedPairInput = [PSCustomObject]@{
        privateKey = $privateMaterial
        publicKey = $installedPublic
    } | ConvertTo-Json -Compress
    $installedPairResultJson = $installedPairInput | & node $NodeScript --check-pair
    $installedPairInput = $null
    if ($LASTEXITCODE -ne 0) { throw "ED25519_INSTALLED_KEY_PAIR_MISMATCH" }
    $installedPairResult = $installedPairResultJson | ConvertFrom-Json
    if (
        [string]$installedPairResult.status -cne "PAIR_VERIFIED" -or
        [string]$installedPairResult.fingerprint -cne [string]$generated.fingerprint
    ) {
        throw "ED25519_INSTALLED_FINGERPRINT_MISMATCH"
    }
    if (-not (Test-Stage1Credential -Target $Target)) {
        throw "RESEARCH_SIGNING_CREDENTIAL_VERIFICATION_FAILED"
    }
    [PSCustomObject]@{
        fingerprint = [string]$generated.fingerprint
        key_id = if ($IsTest) { "TEST_ONLY" } else { "P0_DEVELOPMENT_CHANGE_CARD" }
        status = "CREATED"
    } | ConvertTo-Json -Compress
}
catch {
    try { Remove-Stage1Credential -Target $Target } catch {}
    if ($publicWasReplaced -and $null -ne $oldPublicBytes) {
        [IO.File]::WriteAllBytes($PublicKeyPath, $oldPublicBytes)
    }
    elseif ($IsTest) {
        Remove-Item -LiteralPath $PublicKeyPath -Force -ErrorAction SilentlyContinue
    }
    throw
}
finally {
    if ($null -ne $privateBytes) { [Array]::Clear($privateBytes, 0, $privateBytes.Length) }
    $privateMaterial = $null
    if ($null -ne $temporaryPublic) {
        Remove-Item -LiteralPath $temporaryPublic -Force -ErrorAction SilentlyContinue
    }
    if ($null -ne $backupPublic) {
        Remove-Item -LiteralPath $backupPublic -Force -ErrorAction SilentlyContinue
    }
    if ($null -ne $transport) { [Array]::Clear($transport, 0, $transport.Length) }
    if ($null -ne $key) { [Array]::Clear($key, 0, $key.Length) }
    if ($null -ne $iv) { [Array]::Clear($iv, 0, $iv.Length) }
    if ($null -ne $encrypted) { [Array]::Clear($encrypted, 0, $encrypted.Length) }
    if ($null -ne $providedTag) { [Array]::Clear($providedTag, 0, $providedTag.Length) }
    if ($null -ne $expectedTag) { [Array]::Clear($expectedTag, 0, $expectedTag.Length) }
    if ($null -ne $authenticatedMaterial) {
        [Array]::Clear($authenticatedMaterial, 0, $authenticatedMaterial.Length)
    }
    if ($null -ne $publicBytes) { [Array]::Clear($publicBytes, 0, $publicBytes.Length) }
    if ($null -ne $fingerprintBytes) {
        [Array]::Clear($fingerprintBytes, 0, $fingerprintBytes.Length)
    }
    if ($null -ne $oldPublicBytes) {
        [Array]::Clear($oldPublicBytes, 0, $oldPublicBytes.Length)
    }
    if ($null -ne $decryptor) { $decryptor.Dispose() }
    if ($null -ne $aes) { $aes.Dispose() }
    if ($null -ne $hmac) { $hmac.Dispose() }
}
