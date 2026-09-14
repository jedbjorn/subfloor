# Subfloor winbox provisioning — fork-agnostic guest provisioner driven by the
# resolved winbox.json the host ships next to this file.
#
# The host copies bootstrap.ps1 (optional), provision.ps1, winbox.json and the
# declared winget manifest into C:\SubfloorTest\ and runs, over SSH:
#
#   powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\SubfloorTest\provision.ps1
#
# Human progress goes to stdout as it happens; the LAST line on stdout is exactly
# one JSON object:
#
#   {"steps":[{"name":"...","ok":true,"detail":"..."}],"ok":true}
#
# Exit code is 0 when every step is ok, 1 otherwise.
# Windows PowerShell 5.1 compatible. No external modules. Idempotent.

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Continue'

$script:Steps = @()
$DetailLimit = 2000

function Get-Truncated {
    param([string]$Text)
    if ($null -eq $Text) { return '' }
    $flat = $Text.Trim()
    if ($flat.Length -gt $DetailLimit) {
        $flat = $flat.Substring(0, $DetailLimit) + ' ...[truncated]'
    }
    return $flat
}

function Add-StepResult {
    param(
        [string]$Name,
        [bool]$Ok,
        [string]$Detail
    )
    $clean = Get-Truncated -Text $Detail
    $script:Steps += ,([ordered]@{ name = $Name; ok = $Ok; detail = $clean })
    $marker = 'FAIL'
    if ($Ok) { $marker = ' ok ' }
    Write-Host ("[$marker] $Name - $clean")
}

function Invoke-Capture {
    # Runs a command line through cmd.exe, capturing stdout and stderr together.
    # Returns a hashtable with Output and ExitCode.
    param([string]$CommandLine)
    $output = & cmd.exe /c "$CommandLine" 2>&1 | Out-String
    $code = $LASTEXITCODE
    if ($null -eq $code) { $code = 0 }
    return @{ Output = $output; ExitCode = [int]$code }
}

function Update-PathFromRegistry {
    # winget-installed tools land on PATH in the registry, not in this already
    # running process. Refresh from Machine + User so later steps see them.
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $parts = @()
    foreach ($chunk in @($machine, $user)) {
        if ($chunk) { $parts += $chunk.Split(';') }
    }
    $seen = @{}
    $clean = @()
    foreach ($part in $parts) {
        $trimmed = $part.Trim()
        if (-not $trimmed) { continue }
        if ($seen.ContainsKey($trimmed.ToLower())) { continue }
        $seen[$trimmed.ToLower()] = $true
        $clean += $trimmed
    }
    $env:Path = ($clean -join ';')
}

function Test-CommandPresent {
    param([string]$Name)
    $found = Get-Command -Name $Name -ErrorAction SilentlyContinue
    return ($null -ne $found)
}

Write-Host 'Subfloor winbox provisioning'

# --- Configuration ----------------------------------------------------------
# The host resolves and validates .subfloor/winbox.json and ships the resolved
# copy here, so every key is present. Defaults below only cover a hand-linked
# guest where the file is missing or unreadable.

$configPath = Join-Path $PSScriptRoot 'winbox.json'
$config = $null
$configDetail = ''
if (Test-Path -LiteralPath $configPath) {
    try {
        $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
        $configDetail = "read $configPath"
    } catch {
        $config = $null
        $configDetail = "unreadable $configPath : $($_.Exception.Message)"
    }
} else {
    $configDetail = "no winbox.json next to provision.ps1; using defaults"
}

function Get-ConfigValue {
    param(
        [string]$Name,
        $Default
    )
    if ($null -eq $config) { return $Default }
    $names = @($config.PSObject.Properties.Name)
    if ($names -notcontains $Name) { return $Default }
    $value = $config.$Name
    if ($null -eq $value) { return $Default }
    return $value
}

$wingetManifest = Get-ConfigValue -Name 'winget_manifest' -Default $null
$checks = @(Get-ConfigValue -Name 'checks' -Default @())
$mcp = [bool](Get-ConfigValue -Name 'mcp' -Default $true)
$mcpPort = [int](Get-ConfigValue -Name 'mcp_port' -Default 8000)
$workspace = [string](Get-ConfigValue -Name 'workspace' -Default 'C:\SubfloorTest')

Write-Host "config: $configDetail"

# --- Step 1: execution policy and workspace ---------------------------------

try {
    $notes = @()
    $policy = [string](Get-ExecutionPolicy -Scope LocalMachine)
    if ($policy -eq 'Bypass') {
        $notes += 'execution policy already Bypass'
    } else {
        Set-ExecutionPolicy -Scope LocalMachine Bypass -Force
        $notes += "execution policy $policy -> Bypass"
    }
    if (Test-Path -LiteralPath $workspace) {
        $notes += "$workspace exists"
    } else {
        New-Item -ItemType Directory -Path $workspace -Force | Out-Null
        $notes += "$workspace created"
    }
    Add-StepResult -Name 'workspace' -Ok $true -Detail ($notes -join '; ')
} catch {
    Add-StepResult -Name 'workspace' -Ok $false -Detail $_.Exception.Message
}

# --- Step 2: winget presence ------------------------------------------------

Update-PathFromRegistry
$wingetPresent = Test-CommandPresent -Name 'winget'
if ($wingetPresent) {
    $wingetVersion = Invoke-Capture -CommandLine 'winget --version'
    Add-StepResult -Name 'winget' -Ok $true -Detail ("present " + (Get-Truncated -Text $wingetVersion.Output))
} else {
    Add-StepResult -Name 'winget' -Ok $false -Detail 'winget not found. Install App Installer from the Microsoft Store (https://aka.ms/getwinget); the engine does not install winget because it needs the Store or an MSIX bundle.'
}

# --- Step 3: winget manifest import -----------------------------------------

if (-not $wingetManifest) {
    Add-StepResult -Name 'winget-import' -Ok $true -Detail 'no winget_manifest declared; skipped'
} elseif (-not $wingetPresent) {
    Add-StepResult -Name 'winget-import' -Ok $true -Detail 'skipped: winget is not available'
} else {
    $manifestPath = Join-Path $PSScriptRoot ([string]$wingetManifest)
    if (-not (Test-Path -LiteralPath $manifestPath)) {
        Add-StepResult -Name 'winget-import' -Ok $false -Detail "manifest not found at $manifestPath"
    } else {
        Write-Host "importing winget manifest $manifestPath ..."
        $import = Invoke-Capture -CommandLine "winget import --import-file `"$manifestPath`" --accept-package-agreements --accept-source-agreements --disable-interactivity"
        # winget exits non-zero when every package is already installed; that is
        # the idempotent case, not a failure.
        $text = [string]$import.Output
        $alreadyInstalled = ($text -match 'already installed')
        if ($import.ExitCode -eq 0 -or $alreadyInstalled) {
            Add-StepResult -Name 'winget-import' -Ok $true -Detail ("exit $($import.ExitCode): " + $text)
        } else {
            Add-StepResult -Name 'winget-import' -Ok $false -Detail ("exit $($import.ExitCode): " + $text)
        }
    }
}

# --- Step 4: Windows-MCP ----------------------------------------------------

if (-not $mcp) {
    Add-StepResult -Name 'windows-mcp' -Ok $true -Detail 'mcp disabled in winbox.json; skipped'
} elseif (-not $wingetPresent) {
    Add-StepResult -Name 'windows-mcp' -Ok $false -Detail 'skipped: winget is required to install Python and uv'
} else {
    $mcpOk = $true
    $mcpNotes = @()

    # Python 3.13+ (windows-mcp requires it).
    $pythonOk = $false
    $pythonVersion = Invoke-Capture -CommandLine 'python --version'
    $versionText = [string]$pythonVersion.Output
    if ($pythonVersion.ExitCode -eq 0 -and $versionText -match 'Python\s+(\d+)\.(\d+)') {
        $major = [int]$Matches[1]
        $minor = [int]$Matches[2]
        if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 13)) { $pythonOk = $true }
    }
    if ($pythonOk) {
        $mcpNotes += ('python ok: ' + $versionText.Trim())
    } else {
        Write-Host 'installing Python 3.13 ...'
        $install = Invoke-Capture -CommandLine 'winget install --id Python.Python.3.13 -e --accept-package-agreements --accept-source-agreements --disable-interactivity'
        Update-PathFromRegistry
        $recheck = Invoke-Capture -CommandLine 'python --version'
        if ($recheck.ExitCode -eq 0 -and ([string]$recheck.Output) -match 'Python\s+3\.(1[3-9]|[2-9][0-9])') {
            $mcpNotes += ('python installed: ' + ([string]$recheck.Output).Trim())
        } else {
            $mcpOk = $false
            $mcpNotes += ('python 3.13+ unavailable after install: ' + [string]$install.Output)
        }
    }

    # uv
    if ($mcpOk) {
        if (Test-CommandPresent -Name 'uv') {
            $mcpNotes += 'uv present'
        } else {
            Write-Host 'installing uv ...'
            $install = Invoke-Capture -CommandLine 'winget install --id astral-sh.uv -e --accept-package-agreements --accept-source-agreements --disable-interactivity'
            Update-PathFromRegistry
            if (Test-CommandPresent -Name 'uv') {
                $mcpNotes += 'uv installed'
            } else {
                $mcpOk = $false
                $mcpNotes += ('uv unavailable after install: ' + [string]$install.Output)
            }
        }
    }

    # windows-mcp tool
    if ($mcpOk) {
        Write-Host 'installing windows-mcp ...'
        $tool = Invoke-Capture -CommandLine 'uv tool install --upgrade windows-mcp'
        Update-PathFromRegistry
        if ($tool.ExitCode -ne 0) {
            $mcpOk = $false
            $mcpNotes += ('uv tool install windows-mcp failed: ' + [string]$tool.Output)
        } else {
            $mcpNotes += 'uv tool install windows-mcp ok'
            # uv puts tool shims in its own bin dir (%USERPROFILE%\.local\bin),
            # which an SSH session never has on PATH (observed on halo: the
            # shim exists but 'windows-mcp' is not recognised). Prepend it.
            $uvBin = Invoke-Capture -CommandLine 'uv tool dir --bin'
            if ($uvBin.ExitCode -eq 0) {
                $binDir = ([string]$uvBin.Output).Trim()
                if ($binDir -and (Test-Path -LiteralPath $binDir)) {
                    $env:Path = $binDir + ';' + $env:Path
                    $mcpNotes += ('uv bin on PATH: ' + $binDir)
                }
            }
        }
    }

    # Register and start the per-user login task on the guest loopback only.
    if ($mcpOk) {
        Write-Host "registering windows-mcp on 127.0.0.1:$mcpPort ..."
        $register = Invoke-Capture -CommandLine "windows-mcp install --transport streamable-http --host 127.0.0.1 --port $mcpPort"
        if ($register.ExitCode -ne 0) {
            $mcpOk = $false
            $mcpNotes += ('windows-mcp install failed: ' + [string]$register.Output)
        } else {
            $mcpNotes += 'windows-mcp install ok'
        }
    }

    # Confirm the listener; retry for about a minute.
    if ($mcpOk) {
        $listening = $false
        $deadline = (Get-Date).AddSeconds(60)
        while (-not $listening -and (Get-Date) -lt $deadline) {
            try {
                $conn = @(Get-NetTCPConnection -State Listen -LocalPort $mcpPort -ErrorAction SilentlyContinue |
                    Where-Object { $_.LocalAddress -eq '127.0.0.1' })
                if ($conn.Count -gt 0) { $listening = $true; break }
            } catch {
                $listening = $false
            }
            Start-Sleep -Seconds 5
        }
        if ($listening) {
            $mcpNotes += "listener on 127.0.0.1:$mcpPort"
        } else {
            $mcpOk = $false
            $mcpNotes += 'installed but no listener yet; a desktop login session may be required'
        }
    }

    Add-StepResult -Name 'windows-mcp' -Ok $mcpOk -Detail ($mcpNotes -join '; ')
}

# --- Step 5: declared checks ------------------------------------------------

Update-PathFromRegistry
if ($checks.Count -eq 0) {
    Add-StepResult -Name 'checks' -Ok $true -Detail 'no checks declared'
} else {
    foreach ($check in $checks) {
        $line = [string]$check
        if (-not $line) { continue }
        Write-Host "check: $line"
        $result = Invoke-Capture -CommandLine $line
        $detail = "exit $($result.ExitCode): " + [string]$result.Output
        Add-StepResult -Name ("check: " + $line) -Ok ($result.ExitCode -eq 0) -Detail $detail
    }
}

# --- Report -----------------------------------------------------------------
# Exactly one JSON line, last on stdout. Each step is serialized on its own so a
# single-step run still emits a JSON array under Windows PowerShell 5.1.

$allOk = $true
foreach ($step in $script:Steps) {
    if (-not $step.ok) { $allOk = $false }
}

$encoded = @()
foreach ($step in $script:Steps) {
    $encoded += ($step | ConvertTo-Json -Compress -Depth 4)
}
$okLiteral = 'false'
if ($allOk) { $okLiteral = 'true' }
$report = '{"steps":[' + ($encoded -join ',') + '],"ok":' + $okLiteral + '}'

Write-Host ''
Write-Output $report

if ($allOk) { exit 0 }
exit 1
