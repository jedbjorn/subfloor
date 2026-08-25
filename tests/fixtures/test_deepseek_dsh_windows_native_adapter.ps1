param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$FixtureRoot = $PSScriptRoot
$Policy = (Get-Content -Raw (
    Join-Path $FixtureRoot "deepseek_dsh_windows_provenance_policy.json"
) | ConvertFrom-Json)
. (Join-Path $FixtureRoot "deepseek_dsh_windows_native_adapter.ps1")

function Assert-Equal($Actual, $Expected, [String]$Label) {
    if ($Actual -cne $Expected) {
        throw "$Label expected '$Expected', got '$Actual'"
    }
}

function Native-Facts {
    param(
        [Boolean]$Query = $true,
        [Boolean]$Member = $true,
        [UInt32]$Flags = 0,
        [UInt32]$Type = 3,
        [UInt32]$Access = 1
    )
    $arguments = @{
        Policy = $Policy
        MembershipQuerySucceeded = $Query
        JobMember = $Member
        LimitFlags = $Flags
        FileType = $Type
        GrantedAccess = $Access
    }
    ConvertTo-ScDshNativeFacts @arguments
}

$valid = Native-Facts
Assert-Equal $valid.job_member $true "valid membership"
Assert-Equal $valid.breakaway $false "valid breakaway"
Assert-Equal $valid.handle_type "pipe" "valid handle type"
Assert-Equal $valid.readable $true "valid read access"
Assert-Equal $valid.descriptor_writable $false "valid write access"
Assert-Equal (Native-Facts -Query $false).job_member $false "failed membership API"
Assert-Equal (Native-Facts -Member $false).job_member $false "negative membership result"
Assert-Equal (Native-Facts -Flags 2048).breakaway $true "breakaway flag"
Assert-Equal (Native-Facts -Flags 4096).breakaway $true "silent breakaway flag"
Assert-Equal (Native-Facts -Type 1).handle_type "other" "disk handle"
Assert-Equal (Native-Facts -Access 0).readable $false "missing read access"
Assert-Equal (Native-Facts -Access 1343029527).descriptor_writable $true "write access"

function Invoke-ReadCase {
    param(
        [String]$Label,
        [Byte[]]$Bytes,
        [Boolean]$CloseWriter,
        [Boolean]$ShouldSucceed,
        [Int64]$MaxBytes = 8192
    )
    $pipeName = "sc-dsh-$([Guid]::NewGuid().ToString('N'))"
    $server = [System.IO.Pipes.NamedPipeServerStream]::new(
        $pipeName,
        [System.IO.Pipes.PipeDirection]::Out,
        1,
        [System.IO.Pipes.PipeTransmissionMode]::Byte,
        [System.IO.Pipes.PipeOptions]::Asynchronous
    )
    $client = [System.IO.Pipes.NamedPipeClientStream]::new(
        ".",
        $pipeName,
        [System.IO.Pipes.PipeDirection]::In,
        [System.IO.Pipes.PipeOptions]::Asynchronous
    )
    $connection = $server.WaitForConnectionAsync()
    $client.Connect(1000)
    $connection.GetAwaiter().GetResult()
    try {
        if ($Bytes.Length -gt 0) {
            $server.Write($Bytes, 0, $Bytes.Length)
            $server.Flush()
        }
        if ($CloseWriter) { $server.Dispose() }
        $watch = [System.Diagnostics.Stopwatch]::StartNew()
        $succeeded = $false
        try {
            $arguments = @{
                Handle = $client.SafePipeHandle.DangerousGetHandle()
                MaxBytes = $MaxBytes
                TimeoutMs = 100
                EnvelopeHeader = $Policy.envelope_header
            }
            $result = Read-ScDshFramedDescriptor @arguments
            $succeeded = $true
        } catch {
            $result = $null
        } finally {
            $watch.Stop()
        }
        Assert-Equal $succeeded $ShouldSucceed $Label
        if ($ShouldSucceed) {
            Assert-Equal (
                [System.Text.Encoding]::ASCII.GetString($result)
            ) ([System.Text.Encoding]::ASCII.GetString($Bytes)) "$Label content"
        }
        if ($watch.ElapsedMilliseconds -ge 2000) {
            throw "$Label exceeded bounded refusal: $($watch.ElapsedMilliseconds)ms"
        }
    } finally {
        $client.Dispose()
        $server.Dispose()
    }
}

$ascii = [System.Text.Encoding]::ASCII
$header = $Policy.envelope_header
Invoke-ReadCase "valid-held-open" $ascii.GetBytes("$header`nYQ==`nYQ==`n") $false $true
Invoke-ReadCase "no-data-held-open" ([byte[]]::new(0)) $false $false
Invoke-ReadCase "partial-held-open" $ascii.GetBytes("$header`nYWJj") $false $false
Invoke-ReadCase "empty-eof" ([byte[]]::new(0)) $true $false
Invoke-ReadCase "malformed" $ascii.GetBytes("WRONG`nYQ==`nYQ==`n") $false $false
Invoke-ReadCase "overlong" $ascii.GetBytes(("x" * 64)) $false $false 32

Write-Output "windows native adapter and bounded pipe framing passed"
