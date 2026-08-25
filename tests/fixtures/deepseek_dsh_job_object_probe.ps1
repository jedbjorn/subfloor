param(
    [Parameter(Mandatory = $true)][Int64]$JobHandle,
    [Parameter(Mandatory = $true)][Int64]$DescriptorHandle,
    [Parameter(Mandatory = $true)][String]$ExpectedDomainId
)

$PolicyPath = Join-Path $PSScriptRoot "deepseek_dsh_windows_provenance_policy.json"
$PolicySha256 = "d59d7dd8f78f97fce736b75aa21d615fca75f0940a6a7f051a6b2b60de88da7f"
$policyBytes = [System.IO.File]::ReadAllBytes($PolicyPath)
$policyDigest = [Convert]::ToHexString(
    [System.Security.Cryptography.SHA256]::HashData($policyBytes)
).ToLowerInvariant()
if ($policyDigest -ne $PolicySha256.ToLowerInvariant()) { exit 77 }
$Policy = ([System.Text.Encoding]::UTF8.GetString($policyBytes) |
    ConvertFrom-Json -ErrorAction Stop)
$MaxDescriptorBytes = [Int64]$Policy.max_descriptor_bytes
$AdapterPath = Join-Path $PSScriptRoot "deepseek_dsh_windows_native_adapter.ps1"
$AdapterSha256 = "e2e2cda8222de2a15d0b511a633ddbc5809938fd6051aa449f5044a47797e313"
$adapterBytes = [System.IO.File]::ReadAllBytes($AdapterPath)
$adapterDigest = [Convert]::ToHexString(
    [System.Security.Cryptography.SHA256]::HashData($adapterBytes)
).ToLowerInvariant()
if ($adapterDigest -ne $AdapterSha256) { exit 77 }
. $AdapterPath

$nativeSource = @"
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

public static class ScDshJobProbe {
    public const int JobObjectExtendedLimitInformation = 9;
    public const int ObjectBasicInformation = 0;
    public const uint JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800;
    public const uint JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000;
    public const uint FILE_TYPE_PIPE = 0x0003;
    public const uint FILE_READ_DATA = 0x0001;
    public const uint FORBIDDEN_WRITE_ACCESS = 0x500D0116;

    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_BASIC_LIMIT_INFORMATION {
        public Int64 PerProcessUserTimeLimit;
        public Int64 PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct IO_COUNTERS {
        public UInt64 ReadOperationCount;
        public UInt64 WriteOperationCount;
        public UInt64 OtherOperationCount;
        public UInt64 ReadTransferCount;
        public UInt64 WriteTransferCount;
        public UInt64 OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct PUBLIC_OBJECT_BASIC_INFORMATION {
        public uint Attributes;
        public uint GrantedAccess;
        public uint HandleCount;
        public uint PointerCount;
        [MarshalAs(UnmanagedType.ByValArray, SizeConst = 10)]
        public uint[] Reserved;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool IsProcessInJob(
        IntPtr processHandle,
        IntPtr jobHandle,
        out bool result
    );

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool QueryInformationJobObject(
        IntPtr jobHandle,
        int infoClass,
        IntPtr info,
        uint infoLength,
        out uint returnLength
    );

    [DllImport("kernel32.dll")]
    public static extern IntPtr GetCurrentProcess();

    [DllImport("kernel32.dll")]
    public static extern uint GetCurrentProcessId();

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint GetFileType(IntPtr handle);

    [DllImport("ntdll.dll")]
    public static extern int NtQueryObject(
        IntPtr handle,
        int infoClass,
        out PUBLIC_OBJECT_BASIC_INFORMATION info,
        uint infoLength,
        out uint returnLength
    );

    public static uint LimitFlags(IntPtr jobHandle) {
        int size = Marshal.SizeOf<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>();
        IntPtr buffer = Marshal.AllocHGlobal(size);
        try {
            uint returned;
            if (!QueryInformationJobObject(
                    jobHandle,
                    JobObjectExtendedLimitInformation,
                    buffer,
                    (uint)size,
                    out returned)) {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            return Marshal.PtrToStructure<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>(
                buffer
            ).BasicLimitInformation.LimitFlags;
        } finally {
            Marshal.FreeHGlobal(buffer);
        }
    }

    public static uint GrantedAccess(IntPtr handle) {
        PUBLIC_OBJECT_BASIC_INFORMATION info;
        uint returned;
        int status = NtQueryObject(
            handle,
            ObjectBasicInformation,
            out info,
            (uint)Marshal.SizeOf<PUBLIC_OBJECT_BASIC_INFORMATION>(),
            out returned
        );
        if (status != 0) throw new InvalidOperationException("NtQueryObject failed");
        return info.GrantedAccess;
    }
}
"@

function Refuse-Provenance {
    throw [System.Security.SecurityException]::new("untrusted execution provenance")
}

function Get-PolicyValue($Reference, $State) {
    if ($Reference -isnot [String]) {
        return $Reference
    }
    if ($State.ContainsKey($Reference)) { return $State[$Reference] }
    if (-not $Reference.Contains(".")) { return $Reference }
    $parts = $Reference.Split(".")
    $value = $State[$parts[0]]
    foreach ($part in $parts[1..($parts.Count - 1)]) { $value = $value.$part }
    return $value
}

function Test-PolicyRule($Rule, $State) {
    $left = Get-PolicyValue $Rule.left $State
    $right = Get-PolicyValue $Rule.right $State
    switch ($Rule.operator) {
        "equals" { return $left -ceq $right }
        "matches" { return $left -is [String] -and $left -cmatch $right }
        "exact_fields" {
            $actual = @($left.PSObject.Properties.Name | Sort-Object)
            $expected = @($right | Sort-Object)
            return (Compare-Object $expected $actual).Count -eq 0
        }
        "canonical_json" {
            $document = [System.Text.Json.JsonDocument]::Parse($right)
            try {
                return [System.Text.Json.JsonSerializer]::Serialize(
                    $document.RootElement
                ) -ceq $right
            } finally { $document.Dispose() }
        }
        "integer_min" { return $left -is [Int64] -and $left -ge [Int64]$right }
        "integer_lte" { return $left -is [Int64] -and $left -le [Int64]$right }
        "integer_gt" { return $left -is [Int64] -and $left -gt [Int64]$right }
        "integer_difference_lte" {
            return $left -is [Int64] -and $right -is [Int64] -and
                ($left - $right) -le [Int64]$Rule.limit
        }
        default { Refuse-Provenance }
    }
}

try {
    Add-Type -TypeDefinition $nativeSource
    $job = [IntPtr]::new($JobHandle)
    $descriptor = [IntPtr]::new($DescriptorHandle)
    $inExpectedJob = $false
    $membershipQuerySucceeded = [ScDshJobProbe]::IsProcessInJob(
            [ScDshJobProbe]::GetCurrentProcess(),
            $job,
            [ref]$inExpectedJob)
    $flags = [ScDshJobProbe]::LimitFlags($job)
    $fileType = [ScDshJobProbe]::GetFileType($descriptor)
    $access = [ScDshJobProbe]::GrantedAccess($descriptor)
    $nativeArguments = @{
        Policy = $Policy
        MembershipQuerySucceeded = $membershipQuerySucceeded
        JobMember = $inExpectedJob
        LimitFlags = $flags
        FileType = $fileType
        GrantedAccess = $access
    }
    $nativeFacts = ConvertTo-ScDshNativeFacts @nativeArguments
    $state = @{
        policy = $Policy
        payload = $null
        facts = $nativeFacts
        context = $null
    }
    foreach ($rule in $Policy.rules) {
        if ($rule.stage -eq "native" -and
                -not (Test-PolicyRule $rule $state)) {
            Refuse-Provenance
        }
    }

    $readArguments = @{
        Handle = $descriptor
        MaxBytes = $MaxDescriptorBytes
        TimeoutMs = [Int32]$Policy.descriptor_read_timeout_ms
        EnvelopeHeader = $Policy.envelope_header
    }
    $envelopeBytes = Read-ScDshFramedDescriptor @readArguments
    $envelopeText = [System.Text.Encoding]::ASCII.GetString($envelopeBytes)
    $lines = $envelopeText.Split([char]10)
    $payloadBytes = [Convert]::FromBase64String($lines[1])
    $signature = [Convert]::FromBase64String($lines[2])
    if ([Convert]::ToBase64String($payloadBytes) -ne $lines[1] -or
            [Convert]::ToBase64String($signature) -ne $lines[2]) {
        Refuse-Provenance
    }

    $rsa = [System.Security.Cryptography.RSA]::Create()
    try {
        $keyBytes = [Convert]::FromBase64String($Policy.public_key_spki_base64)
        $bytesRead = 0
        $rsa.ImportSubjectPublicKeyInfo($keyBytes, [ref]$bytesRead)
        $signatureValid = $bytesRead -eq $keyBytes.Length -and $rsa.VerifyData(
                $payloadBytes,
                $signature,
                [System.Security.Cryptography.HashAlgorithmName]::SHA256,
                [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)
    } finally {
        $rsa.Dispose()
    }

    $utf8 = [System.Text.UTF8Encoding]::new($false, $true)
    $payload = ($utf8.GetString($payloadBytes) | ConvertFrom-Json -ErrorAction Stop)
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $state.payload = $payload
    $state.facts.signature_valid = $signatureValid
    $state.context = [PSCustomObject]@{
            payload_text = $utf8.GetString($payloadBytes)
            expected_domain_id = $ExpectedDomainId
            expected_job_handle = $JobHandle
            expected_process_id = [ScDshJobProbe]::GetCurrentProcessId()
            now_unix_ms = $now
    }
    foreach ($rule in $Policy.rules) {
        if ($rule.stage -eq "descriptor" -and
                -not (Test-PolicyRule $rule $state)) {
            Refuse-Provenance
        }
    }

    @{
        contract = $Policy.contract
        provenance = "managed"
        job_member = $true
        non_breakaway = $true
        inherited_descriptor = $true
        descriptor_pipe_read_only = $true
        descriptor_signature = "rsa-sha256"
        domain_id = $payload.domain_id
        binding_generation = [Int64]$payload.binding_generation
    } | ConvertTo-Json -Compress
} catch {
    exit 77
}
