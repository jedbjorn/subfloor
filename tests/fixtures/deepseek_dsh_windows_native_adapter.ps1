Set-StrictMode -Version Latest

$pipeProbeSource = @"
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

public static class ScDshPipeProbe {
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool PeekNamedPipe(
        IntPtr handle,
        IntPtr buffer,
        uint bufferSize,
        out uint bytesRead,
        out uint bytesAvailable,
        out uint bytesLeft
    );

    public static uint AvailableBytes(IntPtr handle) {
        uint read;
        uint available;
        uint left;
        if (!PeekNamedPipe(handle, IntPtr.Zero, 0, out read, out available, out left)) {
            throw new Win32Exception(Marshal.GetLastWin32Error());
        }
        return available;
    }
}
"@
if (-not ("ScDshPipeProbe" -as [Type])) {
    Add-Type -TypeDefinition $pipeProbeSource
}

function ConvertTo-ScDshNativeFacts {
    param(
        [Parameter(Mandatory = $true)]$Policy,
        [Parameter(Mandatory = $true)][Boolean]$MembershipQuerySucceeded,
        [Parameter(Mandatory = $true)][Boolean]$JobMember,
        [Parameter(Mandatory = $true)][UInt32]$LimitFlags,
        [Parameter(Mandatory = $true)][UInt32]$FileType,
        [Parameter(Mandatory = $true)][UInt32]$GrantedAccess
    )
    $native = $Policy.native_adapter
    $breakawayMask = (
        [UInt32]$native.job_object_limit_breakaway_ok -bor
        [UInt32]$native.job_object_limit_silent_breakaway_ok
    )
    [PSCustomObject]@{
        job_member = $MembershipQuerySucceeded -and $JobMember
        breakaway = (($LimitFlags -band $breakawayMask) -ne 0)
        handle_type = if ($FileType -eq [UInt32]$native.file_type_pipe) {
            "pipe"
        } else {
            "other"
        }
        readable = (
            ($GrantedAccess -band [UInt32]$native.file_read_data) -ne 0
        )
        descriptor_writable = (
            ($GrantedAccess -band [UInt32]$native.forbidden_write_access) -ne 0
        )
        signature_valid = $false
    }
}

function Read-ScDshFramedDescriptor {
    param(
        [Parameter(Mandatory = $true)][IntPtr]$Handle,
        [Parameter(Mandatory = $true)][Int64]$MaxBytes,
        [Parameter(Mandatory = $true)][Int32]$TimeoutMs,
        [Parameter(Mandatory = $true)][String]$EnvelopeHeader
    )
    $safe = [Microsoft.Win32.SafeHandles.SafeFileHandle]::new($Handle, $false)
    $stream = [System.IO.FileStream]::new(
        $safe,
        [System.IO.FileAccess]::Read,
        1024,
        $false
    )
    $memory = [System.IO.MemoryStream]::new()
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $buffer = [byte[]]::new(1024)
        while ($true) {
            if ($watch.ElapsedMilliseconds -ge $TimeoutMs) {
                throw [System.TimeoutException]::new("descriptor read timed out")
            }
            $available = [ScDshPipeProbe]::AvailableBytes($Handle)
            if ($available -eq 0) {
                Start-Sleep -Milliseconds 5
                continue
            }
            if (($memory.Length + $available) -gt $MaxBytes) {
                throw [System.IO.InvalidDataException]::new(
                    "descriptor exceeds the byte ceiling"
                )
            }
            $readLength = [Math]::Min($buffer.Length, [Int32]$available)
            $read = $stream.Read($buffer, 0, $readLength)
            if ($read -le 0) {
                throw [System.IO.InvalidDataException]::new(
                    "descriptor pipe became unreadable"
                )
            }
            $memory.Write($buffer, 0, $read)
            $candidate = [System.Text.Encoding]::ASCII.GetString(
                $memory.GetBuffer(),
                0,
                [Int32]$memory.Length
            )
            if (($candidate.ToCharArray() | Where-Object { $_ -eq "`n" }).Count -ge 3) {
                $lines = $candidate.Split([char]10)
                if ($lines.Count -ne 4 -or
                        $lines[0] -cne $EnvelopeHeader -or
                        $lines[3] -cne "") {
                    throw [System.IO.InvalidDataException]::new(
                        "descriptor framing is malformed"
                    )
                }
                return $memory.ToArray()
            }
        }
    } finally {
        $watch.Stop()
        $memory.Dispose()
        $stream.Dispose()
    }
}
