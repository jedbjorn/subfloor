param(
    [Parameter(Mandatory = $true)][Int64]$JobHandle,
    [Parameter(Mandatory = $true)][Int64]$DescriptorHandle
)

$nativeSource = @"
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

public static class ScDshJobProbe {
    public const int JobObjectBasicProcessIdList = 3;
    public const int JobObjectExtendedLimitInformation = 9;
    public const uint JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800;
    public const uint JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000;

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

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint GetFileType(IntPtr handle);

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
}
"@

Add-Type -TypeDefinition $nativeSource
$job = [IntPtr]::new($JobHandle)
$descriptor = [IntPtr]::new($DescriptorHandle)
$inExpectedJob = $false
if (-not [ScDshJobProbe]::IsProcessInJob(
        [ScDshJobProbe]::GetCurrentProcess(),
        $job,
        [ref]$inExpectedJob)) {
    throw "IsProcessInJob failed"
}
if (-not $inExpectedJob) {
    exit 77
}
$flags = [ScDshJobProbe]::LimitFlags($job)
$breakaway = (
    [ScDshJobProbe]::JOB_OBJECT_LIMIT_BREAKAWAY_OK -bor
    [ScDshJobProbe]::JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
)
if (($flags -band $breakaway) -ne 0) {
    exit 77
}
if ([ScDshJobProbe]::GetFileType($descriptor) -eq 0) {
    exit 77
}

@{
    contract = "sc-dsh-windows-job-object-v1"
    provenance = "managed"
    job_member = $true
    non_breakaway = $true
    inherited_descriptor = $true
} | ConvertTo-Json -Compress
