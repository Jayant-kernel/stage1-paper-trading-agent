Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not ("Stage1CredentialNative" -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class Stage1CredentialNative {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct CREDENTIAL {
        public UInt32 Flags;
        public UInt32 Type;
        public string TargetName;
        public string Comment;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public UInt32 CredentialBlobSize;
        public IntPtr CredentialBlob;
        public UInt32 Persist;
        public UInt32 AttributeCount;
        public IntPtr Attributes;
        public string TargetAlias;
        public string UserName;
    }

    [DllImport("advapi32.dll", EntryPoint = "CredWriteW", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool CredWrite(ref CREDENTIAL credential, UInt32 flags);

    [DllImport("advapi32.dll", EntryPoint = "CredReadW", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool CredRead(string target, UInt32 type, UInt32 flags, out IntPtr credential);

    [DllImport("advapi32.dll", EntryPoint = "CredDeleteW", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool CredDelete(string target, UInt32 type, UInt32 flags);

    [DllImport("advapi32.dll", EntryPoint = "CredFree")]
    public static extern void CredFree(IntPtr credential);
}
"@
}

$script:CredentialTypeGeneric = [UInt32]1
$script:CredentialPersistLocalMachine = [UInt32]2
$script:ErrorNotFound = 1168
$script:ProductionTarget = "Stage1PaperAgent/P0/DevelopmentChangeCard/Ed25519/v1"
$script:TestTargetPattern = "^Stage1PaperAgent/P0/TestOnly/Ed25519/[0-9a-f]{32}$"

function Resolve-Stage1CredentialTarget {
    param([string]$TestId = "")
    if ([string]::IsNullOrEmpty($TestId)) {
        return $script:ProductionTarget
    }
    if ($TestId -cnotmatch "^[0-9a-f]{32}$") {
        throw "CREDENTIAL_TEST_ID_REJECTED"
    }
    return "Stage1PaperAgent/P0/TestOnly/Ed25519/$TestId"
}

function Assert-Stage1AllowedCredentialTarget {
    param([Parameter(Mandatory = $true)][string]$Target)
    if (
        $Target -cne $script:ProductionTarget -and
        $Target -cnotmatch $script:TestTargetPattern
    ) {
        throw "CREDENTIAL_TARGET_REJECTED"
    }
}

function Test-Stage1Credential {
    param([Parameter(Mandatory = $true)][string]$Target)
    Assert-Stage1AllowedCredentialTarget -Target $Target
    $pointer = [IntPtr]::Zero
    if ([Stage1CredentialNative]::CredRead(
        $Target,
        $script:CredentialTypeGeneric,
        0,
        [ref]$pointer
    )) {
        [Stage1CredentialNative]::CredFree($pointer)
        return $true
    }
    $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
    if ($errorCode -eq $script:ErrorNotFound) {
        return $false
    }
    throw "CREDENTIAL_STATUS_FAILED:$errorCode"
}

function Set-Stage1Credential {
    param(
        [Parameter(Mandatory = $true)][string]$Target,
        [Parameter(Mandatory = $true)][string]$Secret
    )
    Assert-Stage1AllowedCredentialTarget -Target $Target
    if ([string]::IsNullOrWhiteSpace($Secret)) {
        throw "CREDENTIAL_SECRET_EMPTY"
    }
    $bytes = [Text.Encoding]::Unicode.GetBytes($Secret)
    $blob = [Runtime.InteropServices.Marshal]::AllocHGlobal($bytes.Length)
    try {
        [Runtime.InteropServices.Marshal]::Copy($bytes, 0, $blob, $bytes.Length)
        $credential = New-Object Stage1CredentialNative+CREDENTIAL
        $credential.Type = $script:CredentialTypeGeneric
        $credential.TargetName = $Target
        $credential.CredentialBlobSize = [UInt32]$bytes.Length
        $credential.CredentialBlob = $blob
        $credential.Persist = $script:CredentialPersistLocalMachine
        $credential.UserName = "Stage1PaperAgentResearchSigning"
        if (-not [Stage1CredentialNative]::CredWrite([ref]$credential, 0)) {
            $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
            throw "CREDENTIAL_WRITE_FAILED:$errorCode"
        }
    }
    finally {
        [Array]::Clear($bytes, 0, $bytes.Length)
        if ($blob -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeGlobalAllocUnicode($blob)
        }
    }
}

function Get-Stage1CredentialSecret {
    param([Parameter(Mandatory = $true)][string]$Target)
    Assert-Stage1AllowedCredentialTarget -Target $Target
    $pointer = [IntPtr]::Zero
    if (-not [Stage1CredentialNative]::CredRead(
        $Target,
        $script:CredentialTypeGeneric,
        0,
        [ref]$pointer
    )) {
        $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
        throw "CREDENTIAL_READ_FAILED:$errorCode"
    }
    try {
        $credential = [Runtime.InteropServices.Marshal]::PtrToStructure(
            $pointer,
            [type][Stage1CredentialNative+CREDENTIAL]
        )
        if ($credential.CredentialBlobSize -eq 0) {
            throw "CREDENTIAL_SECRET_EMPTY"
        }
        return [Runtime.InteropServices.Marshal]::PtrToStringUni(
            $credential.CredentialBlob,
            [int]($credential.CredentialBlobSize / 2)
        )
    }
    finally {
        [Stage1CredentialNative]::CredFree($pointer)
    }
}

function Remove-Stage1Credential {
    param([Parameter(Mandatory = $true)][string]$Target)
    Assert-Stage1AllowedCredentialTarget -Target $Target
    if (-not (Test-Stage1Credential -Target $Target)) {
        return
    }
    if (-not [Stage1CredentialNative]::CredDelete(
        $Target,
        $script:CredentialTypeGeneric,
        0
    )) {
        $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
        throw "CREDENTIAL_DELETE_FAILED:$errorCode"
    }
    if (Test-Stage1Credential -Target $Target) {
        throw "CREDENTIAL_DELETE_VERIFICATION_FAILED"
    }
}
