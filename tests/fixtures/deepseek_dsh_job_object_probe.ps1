param(
    [Parameter(Mandatory = $true)][Int64]$JobHandle,
    [Parameter(Mandatory = $true)][Int64]$DescriptorHandle
)

$TrustedPublicKey = "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEApfyngrNJm7UFl1H+vyoc3ov4nRFoxSkbYsrSYHEjhjE5cf7V/Pspl3sF6bPCdl98aufTxEvGqHsmSVXR6DaJ0re8+GuLFX/CBFcA77L0kX9EDWsHQlb9oS5lUV8r2YNuGHDi3O/c9NzleWrSRkWHJeL2YbpyLzaObvtuRFsX/sf6IkXQmtAfM+UyuiRztN1Cbes+S+TGY+MZ4ZoK5540oVAdMMBjCBxqRtRxMl5yS7KnnApjyIBwBbFtFnGjbSVF1In8NFW8L+6m5ZMdNbMYkjQXS1UMgSihEmqwgV7r8Wj70RX40PYpqSod0Qan4luURjBrcbUmArT346UoLmbxnwIDAQAB"
$DescriptorFields = @(
    "binding_generation",
    "contract",
    "domain_id",
    "expires_unix_ms",
    "issued_unix_ms",
    "job_handle",
    "process_id"
)
$DescriptorContract = "sc-dsh-windows-job-object-v2"
$MaxDescriptorBytes = 8192
$MaxLifetimeMs = 30000

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

function Read-SealedDescriptor([IntPtr]$Handle) {
    $safe = [Microsoft.Win32.SafeHandles.SafeFileHandle]::new($Handle, $false)
    $stream = [System.IO.FileStream]::new(
        $safe,
        [System.IO.FileAccess]::Read,
        1024,
        $false
    )
    $memory = [System.IO.MemoryStream]::new()
    try {
        $buffer = [byte[]]::new(1024)
        while ($true) {
            $read = $stream.Read($buffer, 0, $buffer.Length)
            if ($read -eq 0) { break }
            if (($memory.Length + $read) -gt $MaxDescriptorBytes) {
                Refuse-Provenance
            }
            $memory.Write($buffer, 0, $read)
        }
        return $memory.ToArray()
    } finally {
        $memory.Dispose()
        $stream.Dispose()
    }
}

try {
    Add-Type -TypeDefinition $nativeSource
    $job = [IntPtr]::new($JobHandle)
    $descriptor = [IntPtr]::new($DescriptorHandle)
    $inExpectedJob = $false
    if (-not [ScDshJobProbe]::IsProcessInJob(
            [ScDshJobProbe]::GetCurrentProcess(),
            $job,
            [ref]$inExpectedJob)) {
        Refuse-Provenance
    }
    if (-not $inExpectedJob) { Refuse-Provenance }

    $flags = [ScDshJobProbe]::LimitFlags($job)
    $breakaway = (
        [ScDshJobProbe]::JOB_OBJECT_LIMIT_BREAKAWAY_OK -bor
        [ScDshJobProbe]::JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
    )
    if (($flags -band $breakaway) -ne 0) { Refuse-Provenance }
    if ([ScDshJobProbe]::GetFileType($descriptor) -ne
            [ScDshJobProbe]::FILE_TYPE_PIPE) {
        Refuse-Provenance
    }
    $access = [ScDshJobProbe]::GrantedAccess($descriptor)
    if (($access -band [ScDshJobProbe]::FILE_READ_DATA) -eq 0) {
        Refuse-Provenance
    }
    if (($access -band [ScDshJobProbe]::FORBIDDEN_WRITE_ACCESS) -ne 0) {
        Refuse-Provenance
    }

    $envelopeBytes = Read-SealedDescriptor $descriptor
    $envelopeText = [System.Text.Encoding]::ASCII.GetString($envelopeBytes)
    $lines = $envelopeText.Split([char]10)
    if ($lines.Count -ne 4 -or
            $lines[0] -ne "SC-DSH-DESCRIPTOR-V2" -or
            $lines[3] -ne "") {
        Refuse-Provenance
    }
    $payloadBytes = [Convert]::FromBase64String($lines[1])
    $signature = [Convert]::FromBase64String($lines[2])
    if ([Convert]::ToBase64String($payloadBytes) -ne $lines[1] -or
            [Convert]::ToBase64String($signature) -ne $lines[2]) {
        Refuse-Provenance
    }

    $rsa = [System.Security.Cryptography.RSA]::Create()
    try {
        $keyBytes = [Convert]::FromBase64String($TrustedPublicKey)
        $bytesRead = 0
        $rsa.ImportSubjectPublicKeyInfo($keyBytes, [ref]$bytesRead)
        if ($bytesRead -ne $keyBytes.Length -or -not $rsa.VerifyData(
                $payloadBytes,
                $signature,
                [System.Security.Cryptography.HashAlgorithmName]::SHA256,
                [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)) {
            Refuse-Provenance
        }
    } finally {
        $rsa.Dispose()
    }

    $utf8 = [System.Text.UTF8Encoding]::new($false, $true)
    $payload = ($utf8.GetString($payloadBytes) | ConvertFrom-Json -ErrorAction Stop)
    $actualFields = @($payload.PSObject.Properties.Name | Sort-Object)
    if ((Compare-Object $DescriptorFields $actualFields).Count -ne 0) {
        Refuse-Provenance
    }
    if ($payload.contract -ne $DescriptorContract -or
            $payload.domain_id -notmatch "^[a-f0-9]{32}$") {
        Refuse-Provenance
    }
    if ([Int64]$payload.job_handle -ne $JobHandle -or
            [Int64]$payload.process_id -ne [ScDshJobProbe]::GetCurrentProcessId()) {
        Refuse-Provenance
    }
    if ([Int64]$payload.binding_generation -le 0) { Refuse-Provenance }
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $issued = [Int64]$payload.issued_unix_ms
    $expires = [Int64]$payload.expires_unix_ms
    if ($issued -gt $now -or $expires -le $now -or
            ($expires - $issued) -gt $MaxLifetimeMs) {
        Refuse-Provenance
    }

    @{
        contract = $DescriptorContract
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
