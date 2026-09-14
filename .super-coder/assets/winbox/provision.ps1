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
# The report is emitted even when a step throws: everything between reading the
# config and the last check runs inside one try/catch, and an escaped error
# becomes a failed step named `script`. A host that got no report line cannot
# tell "the guest is broken" from "the script died", so it always gets one.
#
# Exit code is 0 when every step is ok, 1 otherwise.
# Windows PowerShell 5.1 compatible. No external modules. Idempotent.

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Continue'
# Defined up front: under StrictMode, reading $LASTEXITCODE before any native
# command has run is an error.
$global:LASTEXITCODE = 0

$script:Steps = @()
# Directories a tool was installed into that the registry PATH does not name
# (uv's tool bin dir is the standing example). Re-applied after every refresh.
$script:PathExtras = @()
$DetailLimit = 2000

# winget's "no applicable upgrade / already installed" result. The exit code is
# invariant; the English sentence it prints is not, so the code is the primary
# signal and the text only a fallback for an older winget.
$WingetNoApplicableUpgrade = -1978335131

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

function Invoke-Native {
    # Run one executable DIRECTLY with an argument ARRAY, capturing stdout and
    # stderr together. Never through cmd.exe: cmd re-parses the line it is
    # handed, so an argument carrying a quoted path (winget import
    # --import-file "C:\dir\manifest.json") reaches the program with the quote
    # characters still in it and the file is never found. Passing an array to
    # the call operator hands each argument over intact - no quoting, no
    # re-parsing, no escaping rules to get wrong.
    param(
        [string]$Exe,
        [string[]]$Arguments = @()
    )
    # An absent executable is a RESULT here, not an exception: `& 'python'`
    # with no python installed throws CommandNotFoundException, whereas the
    # cmd.exe route this replaced simply returned 9009. Callers branch on the
    # exit code, so keep that shape.
    # Prefer a real file over the Store's app-execution-alias stubs: the stubs
    # under %LOCALAPPDATA%\Microsoft\WindowsApps are zero-byte reparse points
    # (python.exe there opens the Store and exits 9009).
    $resolved = $null
    foreach ($candidate in @(Get-Command -Name $Exe -All -ErrorAction SilentlyContinue)) {
        $source = [string]$candidate.Source
        if ($source -and (Test-Path -LiteralPath $source)) {
            $item = Get-Item -LiteralPath $source -ErrorAction SilentlyContinue
            if ($item -and $item.Length -eq 0) { continue }
        }
        $resolved = $candidate
        break
    }
    if ($null -eq $resolved) {
        $resolved = Get-Command -Name $Exe -ErrorAction SilentlyContinue | Select-Object -First 1
    }
    if ($null -eq $resolved) {
        return @{ Output = "'$Exe' is not recognized as a command"; ExitCode = 9009 }
    }
    $target = $Exe
    if ($resolved.Source) { $target = [string]$resolved.Source }
    $global:LASTEXITCODE = 0
    $output = & $target @Arguments 2>&1 | Out-String
    $code = $LASTEXITCODE
    if ($null -eq $code) { $code = 0 }
    return @{ Output = $output; ExitCode = [int]$code }
}

function Invoke-Shell {
    # A free-form command line through cmd.exe, for the fork's declared
    # `checks` ONLY. Those are operator-written command lines meant to be
    # interpreted (`dotnet --version`, `where git & git --version`), which is
    # exactly what cmd is for. They must not contain a double quote: the line
    # is re-parsed by cmd and quoting survives no better here than anywhere
    # else. Write a .cmd file and name that instead.
    param([string]$CommandLine)
    $global:LASTEXITCODE = 0
    $output = & cmd.exe /c $CommandLine 2>&1 | Out-String
    $code = $LASTEXITCODE
    if ($null -eq $code) { $code = 0 }
    return @{ Output = $output; ExitCode = [int]$code }
}

function Add-PathExtra {
    # Remember a directory that must stay on PATH across later refreshes.
    param([string]$Directory)
    if (-not $Directory) { return }
    $known = @($script:PathExtras | Where-Object { $_ -eq $Directory })
    if ($known.Count -eq 0) { $script:PathExtras += $Directory }
    Update-PathFromRegistry
}

function Update-PathFromRegistry {
    # winget-installed tools land on PATH in the registry, not in this already
    # running process. Refresh from Machine + User so later steps see them -
    # and re-prepend every remembered extra, because a refresh built purely
    # from the registry would otherwise drop a directory that is not in it
    # (uv's tool bin dir being the one that bites).
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    # Extras go AFTER the registry PATH. The per-user WindowsApps directory is
    # one of them and it holds the Store's zero-byte python.exe alias stub;
    # prepended, that stub shadowed a freshly installed Python 3.13 (observed
    # on halo: winget reported success, the recheck ran the stub).
    $parts = @()
    foreach ($chunk in @($machine, $user)) {
        if ($chunk) { $parts += $chunk.Split(';') }
    }
    foreach ($extra in $script:PathExtras) { $parts += $extra }
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

function Test-PythonVersionOk {
    # True when the `python --version` text names 3.13 or newer. A NUMERIC
    # comparison, never a version-shaped regex: `Python 3.130` and `Python 4.1`
    # both have to answer correctly, and a pattern enumerating digits does not.
    param([string]$Text)
    if (-not $Text) { return $false }
    if ($Text -notmatch 'Python\s+(\d+)\.(\d+)') { return $false }
    $major = [int]$Matches[1]
    $minor = [int]$Matches[2]
    if ($major -gt 3) { return $true }
    return ($major -eq 3 -and $minor -ge 13)
}

Write-Host 'Subfloor winbox provisioning'

try {

# --- Configuration ----------------------------------------------------------
# The host resolves and validates .subfloor/winbox.json and ships the resolved
# copy here, so every key is present. Defaults below only cover a hand-linked
# guest where the file is missing or unreadable.

$configPath = Join-Path $PSScriptRoot 'winbox.json'
$config = $null
$configDetail = ''
if (Test-Path -LiteralPath $configPath) {
    try {
        $config = Get-Content -LiteralPath $configPath -Raw -ErrorAction Stop | ConvertFrom-Json
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
    $policy = [string](Get-ExecutionPolicy -Scope LocalMachine -ErrorAction Stop)
    if ($policy -eq 'Bypass') {
        $notes += 'execution policy already Bypass'
    } else {
        Set-ExecutionPolicy -Scope LocalMachine Bypass -Force -ErrorAction Stop
        $notes += "execution policy $policy -> Bypass"
    }
    if (Test-Path -LiteralPath $workspace) {
        $notes += "$workspace exists"
    } else {
        New-Item -ItemType Directory -Path $workspace -Force -ErrorAction Stop | Out-Null
        $notes += "$workspace created"
    }
    Add-StepResult -Name 'workspace' -Ok $true -Detail ($notes -join '; ')
} catch {
    Add-StepResult -Name 'workspace' -Ok $false -Detail $_.Exception.Message
}

# --- Step 2: winget presence ------------------------------------------------

Update-PathFromRegistry
$wingetNotes = @()
$wingetPresent = Test-CommandPresent -Name 'winget'
if (-not $wingetPresent) {
    # App Installer is a per-user Store package. An account that has never
    # signed in interactively (observed on halo: a freshly created local admin
    # adopted over SSH) has the package provisioned on the machine but no
    # winget.exe execution alias yet, so 'winget' is not recognised even though
    # the package is installed. Re-registering the package for this user
    # creates the alias without the Store or an MSIX download.
    $pkg = Get-AppxPackage -Name 'Microsoft.DesktopAppInstaller' -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $pkg) {
        $pkg = Get-AppxPackage -AllUsers -Name 'Microsoft.DesktopAppInstaller' -ErrorAction SilentlyContinue | Select-Object -First 1
    }
    if ($pkg -and $pkg.InstallLocation) {
        $manifest = Join-Path $pkg.InstallLocation 'AppxManifest.xml'
        try {
            Add-AppxPackage -DisableDevelopmentMode -Register $manifest -ErrorAction Stop
            $wingetNotes += ('re-registered App Installer ' + $pkg.Version + ' for ' + $env:USERNAME)
        } catch {
            $wingetNotes += ('App Installer re-register failed: ' + $_.Exception.Message)
        }
        $aliasDir = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps'
        Add-PathExtra -Directory $aliasDir
        for ($attempt = 1; $attempt -le 6; $attempt++) {
            if (Test-Path -LiteralPath (Join-Path $aliasDir 'winget.exe')) { break }
            Start-Sleep -Seconds 5
        }
        # Resolve again AFTER the registration and the PATH change: the whole
        # point of the block above is that `winget` was not resolvable before.
        $wingetPresent = Test-CommandPresent -Name 'winget'
    } else {
        $wingetNotes += 'App Installer package not present on this machine'
    }
}
if ($wingetPresent) {
    # Same per-user gap for the source index: Microsoft.Winget.Source is
    # provisioned machine-wide but a first-time account has no registration,
    # and winget then fails every search with 0x8a15000f ('Data required by
    # the source is missing'). Register the machine copy for this user.
    $srcPkg = Get-AppxPackage -Name 'Microsoft.Winget.Source' -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $srcPkg) {
        $srcAll = Get-AppxPackage -AllUsers -Name 'Microsoft.Winget.Source' -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($srcAll -and $srcAll.InstallLocation) {
            try {
                Add-AppxPackage -DisableDevelopmentMode -Register (Join-Path $srcAll.InstallLocation 'AppxManifest.xml') -ErrorAction Stop
                $wingetNotes += ('registered winget source index ' + $srcAll.Version + ' for ' + $env:USERNAME)
            } catch {
                $wingetNotes += ('winget source index register failed: ' + $_.Exception.Message)
            }
        }
    }
    $wingetVersion = Invoke-Native -Exe 'winget' -Arguments @('--version')
    $detail = 'present ' + (Get-Truncated -Text $wingetVersion.Output)
    if ($wingetNotes.Count -gt 0) { $detail = $detail + '; ' + ($wingetNotes -join '; ') }
    Add-StepResult -Name 'winget' -Ok $true -Detail $detail
} elseif ((-not $wingetManifest) -and (-not $mcp)) {
    # Nothing declared in winbox.json needs winget, so its absence is not a
    # failure - reporting one would fail an adoption that has everything the
    # fork actually asked for.
    $detail = 'not present; nothing declared needs it (no winget_manifest, mcp off)'
    if ($wingetNotes.Count -gt 0) { $detail = $detail + ' (' + ($wingetNotes -join '; ') + ')' }
    Add-StepResult -Name 'winget' -Ok $true -Detail $detail
} else {
    $detail = 'winget not found. Install App Installer from the Microsoft Store (https://aka.ms/getwinget); the engine does not install winget because it needs the Store or an MSIX bundle.'
    if ($wingetNotes.Count -gt 0) { $detail = $detail + ' (' + ($wingetNotes -join '; ') + ')' }
    Add-StepResult -Name 'winget' -Ok $false -Detail $detail
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
        $import = Invoke-Native -Exe 'winget' -Arguments @(
            'import', '--import-file', $manifestPath,
            '--accept-package-agreements', '--accept-source-agreements',
            '--disable-interactivity'
        )
        # winget exits non-zero when every package is already installed; that is
        # the idempotent case, not a failure. The EXIT CODE is the signal - the
        # sentence winget prints is localized.
        $text = [string]$import.Output
        $alreadyInstalled = (
            $import.ExitCode -eq $WingetNoApplicableUpgrade -or
            $text -match 'already installed'
        )
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
    $listenerMissing = $false

    # Python 3.13+ (windows-mcp requires it).
    $pythonVersion = Invoke-Native -Exe 'python' -Arguments @('--version')
    $versionText = [string]$pythonVersion.Output
    $pythonOk = ($pythonVersion.ExitCode -eq 0 -and (Test-PythonVersionOk -Text $versionText))
    if ($pythonOk) {
        $mcpNotes += ('python ok: ' + $versionText.Trim())
    } else {
        Write-Host 'installing Python 3.13 ...'
        $install = Invoke-Native -Exe 'winget' -Arguments @(
            'install', '--id', 'Python.Python.3.13', '-e',
            '--accept-package-agreements', '--accept-source-agreements',
            '--disable-interactivity'
        )
        Update-PathFromRegistry
        $recheck = Invoke-Native -Exe 'python' -Arguments @('--version')
        if ($recheck.ExitCode -eq 0 -and (Test-PythonVersionOk -Text ([string]$recheck.Output))) {
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
            $install = Invoke-Native -Exe 'winget' -Arguments @(
                'install', '--id', 'astral-sh.uv', '-e',
                '--accept-package-agreements', '--accept-source-agreements',
                '--disable-interactivity'
            )
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
        $tool = Invoke-Native -Exe 'uv' -Arguments @('tool', 'install', '--upgrade', 'windows-mcp')
        if ($tool.ExitCode -ne 0) {
            $mcpOk = $false
            $mcpNotes += ('uv tool install windows-mcp failed: ' + [string]$tool.Output)
        } else {
            $mcpNotes += 'uv tool install windows-mcp ok'
            # uv puts tool shims in its own bin dir (%USERPROFILE%\.local\bin),
            # which an SSH session never has on PATH (observed on halo: the
            # shim exists but 'windows-mcp' is not recognised). Remember it so
            # a later registry refresh cannot drop it again.
            $uvBin = Invoke-Native -Exe 'uv' -Arguments @('tool', 'dir', '--bin')
            if ($uvBin.ExitCode -eq 0) {
                $binDir = ([string]$uvBin.Output).Trim()
                if ($binDir -and (Test-Path -LiteralPath $binDir)) {
                    Add-PathExtra -Directory $binDir
                    $mcpNotes += ('uv bin on PATH: ' + $binDir)
                }
            }
        }
    }

    # Register and start the per-user login task on the guest loopback only.
    if ($mcpOk) {
        Write-Host "registering windows-mcp on 127.0.0.1:$mcpPort ..."
        $register = Invoke-Native -Exe 'windows-mcp' -Arguments @(
            'install', '--transport', 'streamable-http',
            '--host', '127.0.0.1', '--port', [string]$mcpPort
        )
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
            # NOT a failure. `windows-mcp install` registers a per-user LOGON
            # task, so on a guest with no interactive desktop session the tool
            # is installed and correct and has simply never started. Adoption
            # continues and the host reports this as a warning; the recovery
            # is a console login, or re-running the install over `./sc vm exec`
            # once a session exists.
            $listenerMissing = $true
            $mcpNotes += 'WARNING: installed but no listener yet; a desktop login session may be required'
        }
    }

    Add-StepResult -Name 'windows-mcp' -Ok $mcpOk -Detail ($mcpNotes -join '; ')
    if ($listenerMissing) { Write-Host 'windows-mcp: no listener yet (see the warning above)' }
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
        $result = Invoke-Shell -CommandLine $line
        $detail = "exit $($result.ExitCode): " + [string]$result.Output
        Add-StepResult -Name ("check: " + $line) -Ok ($result.ExitCode -eq 0) -Detail $detail
    }
}

} catch {
    # Anything that escaped a step's own handling: the host still gets a report,
    # and the failure is named rather than inferred from a missing line.
    Add-StepResult -Name 'script' -Ok $false -Detail ('unhandled error: ' + $_.Exception.Message)
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
