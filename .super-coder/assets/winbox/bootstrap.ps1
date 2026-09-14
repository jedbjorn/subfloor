# Subfloor winbox bootstrap — prepares a bring-your-own-licence Windows guest
# for `./sc vm adopt`. It installs no toolchain, receives no key, no host name
# and no token: adoption identity flows host-to-guest afterwards.
#
# Invocation (elevated PowerShell inside the guest, one line, the raw URL of this
# file at the engine's pinned ref - `./sc vm adopt` prints it, and the README next
# to this file spells it out):
#
#   powershell -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; irm <raw-url> | iex"
#
# The explicit `Tls12` is required because Windows PowerShell 5.1 on Windows 10
# still defaults ServicePointManager to SSL3/TLS1.0, which the raw content host
# refuses; without it `irm` fails with "The request was aborted: Could not create
# SSL/TLS secure channel" before a single line of this script runs.
#
# This file carries no URL, host name, key or token of its own on purpose: it is
# fork-agnostic and adoption identity flows host-to-guest in the next step.
#
# Because the `irm | iex` path cannot pass parameters, the optional AutoLogon step
# is reachable there through the environment variable SUBFLOOR_AUTOLOGON=1.
# When the file is copied into the guest and run directly, use -AutoLogon.
#
# Windows PowerShell 5.1 compatible. No external modules. Idempotent.

param(
    [switch]$AutoLogon
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:Steps = @()
$script:Failed = $false

function Write-StepResult {
    param(
        [string]$Name,
        [string]$Status,
        [string]$Detail
    )
    $line = "[$Status] $Name"
    if ($Detail) { $line = "$line - $Detail" }
    Write-Host $line
    $script:Steps += ,@{ name = $Name; status = $Status; detail = $Detail }
    if ($Status -eq 'failed') { $script:Failed = $true }
}

function Test-Elevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-WindowsRelease {
    $key = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
    $release = ''
    try {
        $props = Get-ItemProperty -Path $key -ErrorAction Stop
        if ($props.PSObject.Properties.Name -contains 'DisplayVersion' -and $props.DisplayVersion) {
            $release = [string]$props.DisplayVersion
        } elseif ($props.PSObject.Properties.Name -contains 'ReleaseId' -and $props.ReleaseId) {
            $release = [string]$props.ReleaseId
        }
    } catch {
        $release = ''
    }
    return $release
}

Write-Host ''
Write-Host 'Subfloor winbox bootstrap'
Write-Host '-------------------------'

# --- Step 1: elevation and OS gate -----------------------------------------

$MinimumBuild = 17763  # Windows 10 1809, first build shipping the OpenSSH.Server capability

if (-not (Test-Elevated)) {
    Write-StepResult -Name 'elevation' -Status 'failed' -Detail 'not running as Administrator; re-open PowerShell with "Run as administrator"'
    Write-Host ''
    Write-Host 'Aborted: an elevated prompt is required.'
    exit 1
}
Write-StepResult -Name 'elevation' -Status 'done' -Detail 'running elevated'

$os = Get-CimInstance -ClassName Win32_OperatingSystem
$caption = [string]$os.Caption
$build = 0
try { $build = [int]$os.BuildNumber } catch { $build = 0 }
$release = Get-WindowsRelease
$osDetail = "$caption build $build"
if ($release) { $osDetail = "$caption $release build $build" }

if ($build -lt $MinimumBuild) {
    Write-StepResult -Name 'os-gate' -Status 'failed' -Detail "$osDetail is older than Windows 10 1809 (build $MinimumBuild)"
    Write-Host ''
    Write-Host 'Aborted: Windows 10 1809 (build 17763) or newer is required for the OpenSSH server capability.'
    exit 1
}
Write-StepResult -Name 'os-gate' -Status 'done' -Detail $osDetail

# --- Step 2: OpenSSH server capability, service, firewall -------------------

$script:RebootRequired = $false
try {
    $capability = Get-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'
    if ([string]$capability.State -eq 'Installed') {
        Write-StepResult -Name 'openssh-capability' -Status 'skipped' -Detail 'already installed'
    } else {
        # The result is not decoration: a capability install that sets
        # RestartNeeded has NOT finished, and every step after this one
        # (service, config, key install from the host) then fails in a way
        # that does not name the reboot. Report it and stop.
        $added = Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'
        $restart = $false
        if ($added -and ($added.PSObject.Properties.Name -contains 'RestartNeeded')) {
            $restart = [bool]$added.RestartNeeded
        }
        if ($restart) {
            $script:RebootRequired = $true
            Write-StepResult -Name 'openssh-capability' -Status 'failed' -Detail 'OpenSSH.Server~~~~0.0.1.0 installed but a reboot is required - reboot the guest, then re-run this line'
        } else {
            Write-StepResult -Name 'openssh-capability' -Status 'done' -Detail 'OpenSSH.Server~~~~0.0.1.0 installed'
        }
    }
} catch {
    Write-StepResult -Name 'openssh-capability' -Status 'failed' -Detail $_.Exception.Message
}

if ($script:RebootRequired) {
    Write-Host ''
    Write-Host 'Report'
    Write-Host '------'
    foreach ($step in $script:Steps) {
        Write-Host ('[' + $step.status + '] ' + $step.name + ' - ' + $step.detail)
    }
    Write-Host ''
    Write-Host 'Reboot the guest, then run this line again. Nothing else was changed.'
    exit 1
}

try {
    $sshd = Get-Service -Name 'sshd' -ErrorAction Stop
    $changed = @()
    if ((Get-Service -Name 'sshd').StartType -ne 'Automatic') {
        Set-Service -Name 'sshd' -StartupType Automatic
        $changed += 'startup=Automatic'
    }
    if ((Get-Service -Name 'sshd').Status -ne 'Running') {
        Start-Service -Name 'sshd'
        $changed += 'started'
    }
    # A FRESH OpenSSH install has no C:\ProgramData\ssh\sshd_config: sshd
    # generates it (and the host keys) on its first start, which happens
    # AFTER Start-Service returns. Without this wait the next step reported
    # the file missing and the whole bootstrap exited 1 on exactly the guest
    # it was written for.
    $configPath = Join-Path $env:ProgramData 'ssh\sshd_config'
    if (-not (Test-Path -LiteralPath $configPath)) {
        $deadline = (Get-Date).AddSeconds(60)
        while (-not (Test-Path -LiteralPath $configPath) -and (Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 2
        }
        if (-not (Test-Path -LiteralPath $configPath)) {
            # Still nothing: seed it from the shipped default, which is what
            # sshd would have copied. Beats failing a guest that is otherwise
            # ready.
            $default = Join-Path $env:SystemRoot 'System32\OpenSSH\sshd_config_default'
            if (Test-Path -LiteralPath $default) {
                $parent = Split-Path -Path $configPath -Parent
                if (-not (Test-Path -LiteralPath $parent)) {
                    New-Item -ItemType Directory -Path $parent -Force | Out-Null
                }
                Copy-Item -LiteralPath $default -Destination $configPath -Force
                $changed += 'seeded sshd_config from sshd_config_default'
            }
        } else {
            $changed += 'waited for sshd to write sshd_config'
        }
    }
    if ($changed.Count -eq 0) {
        Write-StepResult -Name 'sshd-service' -Status 'skipped' -Detail 'Automatic and running'
    } else {
        Write-StepResult -Name 'sshd-service' -Status 'done' -Detail ($changed -join ', ')
    }
} catch {
    Write-StepResult -Name 'sshd-service' -Status 'failed' -Detail $_.Exception.Message
}

try {
    $ruleName = 'OpenSSH-Server-In-TCP'
    $rule = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
    if ($null -eq $rule) {
        New-NetFirewallRule -Name $ruleName -DisplayName 'OpenSSH Server (sshd)' -Description 'Subfloor winbox: inbound OpenSSH' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
        Write-StepResult -Name 'firewall-rule' -Status 'done' -Detail "$ruleName created (TCP 22 inbound allow)"
    } elseif ([string]$rule.Enabled -ne 'True') {
        Enable-NetFirewallRule -Name $ruleName
        Write-StepResult -Name 'firewall-rule' -Status 'done' -Detail "$ruleName enabled"
    } else {
        Write-StepResult -Name 'firewall-rule' -Status 'skipped' -Detail "$ruleName present and enabled"
    }
} catch {
    Write-StepResult -Name 'firewall-rule' -Status 'failed' -Detail $_.Exception.Message
}

# --- Step 3: sshd_config authentication settings ----------------------------
# The stock file ships both settings commented out, which already means yes. We
# make them explicit so `adopt` can flip PasswordAuthentication off later by
# rewriting a real line rather than guessing at a default. The default shell is left as
# cmd.exe on purpose: every `./sc vm exec` convention assumes it.

try {
    $configPath = Join-Path $env:ProgramData 'ssh\sshd_config'
    if (-not (Test-Path -LiteralPath $configPath)) {
        Write-StepResult -Name 'sshd-config' -Status 'failed' -Detail "$configPath not found; is the OpenSSH server installed?"
    } else {
        $lines = @(Get-Content -LiteralPath $configPath)
        $wanted = @(
            @{ key = 'PubkeyAuthentication'; value = 'yes' },
            @{ key = 'PasswordAuthentication'; value = 'yes' }
        )
        $changedKeys = @()
        foreach ($item in $wanted) {
            $key = [string]$item.key
            $target = "$key $([string]$item.value)"
            $pattern = '^\s*#?\s*' + $key + '\s+\S+'
            $matched = $false
            for ($i = 0; $i -lt $lines.Count; $i++) {
                if ($lines[$i] -match $pattern) {
                    # Only the FIRST occurrence: sshd honours the first
                    # directive it reads, so rewriting later ones changes
                    # nothing and can rewrite a Match-scoped override.
                    $matched = $true
                    if ($lines[$i] -ne $target) {
                        $lines[$i] = $target
                        if ($changedKeys -notcontains $key) { $changedKeys += $key }
                    }
                    break
                }
            }
            if (-not $matched) {
                # The stock Windows sshd_config ENDS with `Match Group
                # administrators`, so a directive appended to the end lands
                # inside that block and applies only to that group. Insert it
                # before the first Match line instead; append only when the
                # file has no Match block at all.
                $rebuilt = @()
                $placed = $false
                foreach ($line in $lines) {
                    if (-not $placed -and $line -match '^\s*Match\s') {
                        $rebuilt += $target
                        $placed = $true
                    }
                    $rebuilt += $line
                }
                if (-not $placed) { $rebuilt += $target }
                $lines = $rebuilt
                if ($changedKeys -notcontains $key) { $changedKeys += $key }
            }
        }
        if ($changedKeys.Count -eq 0) {
            Write-StepResult -Name 'sshd-config' -Status 'skipped' -Detail 'PubkeyAuthentication yes and PasswordAuthentication yes already explicit'
        } else {
            # UTF-8 without a BOM, and the file's own CRLF endings. ASCII
            # would replace any non-ASCII byte already in the file with `?`,
            # and PowerShell 5.1's UTF8 encoding writes a BOM that sshd reads
            # as part of the first directive.
            [IO.File]::WriteAllText(
                $configPath,
                (($lines -join "`r`n") + "`r`n"),
                (New-Object System.Text.UTF8Encoding($false))
            )
            Restart-Service -Name 'sshd'
            Write-StepResult -Name 'sshd-config' -Status 'done' -Detail ('set ' + ($changedKeys -join ', ') + '; sshd restarted')
        }
    }
} catch {
    Write-StepResult -Name 'sshd-config' -Status 'failed' -Detail $_.Exception.Message
}

# --- Step 4: execution policy and workspace ---------------------------------

try {
    $policy = [string](Get-ExecutionPolicy -Scope LocalMachine)
    if ($policy -eq 'Bypass') {
        Write-StepResult -Name 'execution-policy' -Status 'skipped' -Detail 'LocalMachine already Bypass'
    } else {
        Set-ExecutionPolicy -Scope LocalMachine Bypass -Force
        Write-StepResult -Name 'execution-policy' -Status 'done' -Detail "LocalMachine $policy -> Bypass"
    }
} catch {
    Write-StepResult -Name 'execution-policy' -Status 'failed' -Detail $_.Exception.Message
}

try {
    $workspace = 'C:\SubfloorTest'
    if (Test-Path -LiteralPath $workspace) {
        Write-StepResult -Name 'workspace' -Status 'skipped' -Detail "$workspace exists"
    } else {
        New-Item -ItemType Directory -Path $workspace -Force | Out-Null
        Write-StepResult -Name 'workspace' -Status 'done' -Detail "$workspace created"
    }
} catch {
    Write-StepResult -Name 'workspace' -Status 'failed' -Detail $_.Exception.Message
}

# --- Step 5: optional AutoLogon ---------------------------------------------
# Throwaway test guests only. The password is stored by Windows inside the guest
# and never crosses to the host; it exists so a reset to an offline snapshot boots
# straight into a desktop session for GUI driving.

$wantAutoLogon = $false
if ($AutoLogon) { $wantAutoLogon = $true }
if ($env:SUBFLOOR_AUTOLOGON -eq '1') { $wantAutoLogon = $true }

if (-not $wantAutoLogon) {
    Write-StepResult -Name 'autologon' -Status 'skipped' -Detail 'not requested (-AutoLogon or SUBFLOOR_AUTOLOGON=1)'
} else {
    $plain = $null
    try {
        $winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
        Write-Host ''
        Write-Host "AutoLogon: enter the password for $env:COMPUTERNAME\$env:USERNAME (throwaway guest only)."
        $secure = Read-Host -Prompt 'Password' -AsSecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try {
            # PtrToStringBSTR, not PtrToStringAuto: a BSTR carries its own
            # length prefix, so it round-trips a password containing an
            # embedded null or a lone surrogate. PtrToStringAuto stops at the
            # first null and would silently store a TRUNCATED password, which
            # then fails at logon with no sign of why.
            $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
        if ([string]::IsNullOrEmpty($plain)) {
            # An empty DefaultPassword does not mean "no password"; it means
            # AutoAdminLogon is armed and will fail at every boot.
            Write-StepResult -Name 'autologon' -Status 'failed' -Detail 'no password entered; AutoAdminLogon left unchanged'
        } else {
            # The SAME value goes into the registry and into the report, so
            # the operator can see what account the guest will log in as.
            $logonDomain = $env:COMPUTERNAME
            Set-ItemProperty -Path $winlogon -Name 'AutoAdminLogon' -Value '1' -Type String
            Set-ItemProperty -Path $winlogon -Name 'DefaultUserName' -Value $env:USERNAME -Type String
            Set-ItemProperty -Path $winlogon -Name 'DefaultDomainName' -Value $logonDomain -Type String
            Set-ItemProperty -Path $winlogon -Name 'DefaultPassword' -Value $plain -Type String
            Write-StepResult -Name 'autologon' -Status 'done' -Detail ("AutoAdminLogon=1 for " + $logonDomain + "\\" + $env:USERNAME)
        }
    } catch {
        Write-StepResult -Name 'autologon' -Status 'failed' -Detail $_.Exception.Message
    } finally {
        $plain = $null
        [GC]::Collect()
    }
}

# --- Step 6: report ---------------------------------------------------------

$account = "$env:USERDOMAIN\$env:USERNAME"
$addresses = @()
try {
    $addresses = @(
        Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object { $_.IPAddress -ne '127.0.0.1' } |
            Sort-Object -Property InterfaceAlias
    )
} catch {
    $addresses = @()
}

# The address `adopt` should be told about is the one on the interface that
# carries the IPv4 default route - the NIC that actually talks to the world.
# Sorting interface aliases and taking the first non-APIPA picked the Hyper-V
# or loopback-adapter address on a guest with more than one NIC.
$primary = '<guest-ip>'
$routed = ''
try {
    $route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction Stop |
        Sort-Object -Property RouteMetric |
        Select-Object -First 1
    if ($route) {
        foreach ($address in $addresses) {
            if ([string]$address.InterfaceIndex -eq [string]$route.InterfaceIndex) {
                $routed = [string]$address.IPAddress
                break
            }
        }
    }
} catch {
    $routed = ''
}
if ($routed -and $routed -notlike '169.254.*') {
    $primary = $routed
} else {
    foreach ($address in $addresses) {
        $ip = [string]$address.IPAddress
        if ($ip -notlike '169.254.*') { $primary = $ip; break }
    }
}

Write-Host ''
Write-Host 'Report'
Write-Host '------'
Write-Host "OS:      $osDetail"
Write-Host "Account: $account"
if ($addresses.Count -eq 0) {
    Write-Host 'IPv4:    (none found)'
} else {
    foreach ($address in $addresses) {
        Write-Host ("IPv4:    " + [string]$address.IPAddress + "  (" + [string]$address.InterfaceAlias + ")")
    }
}
Write-Host ''
Write-Host 'Next, on the Subfloor host:'
Write-Host ''
Write-Host "  ./sc vm adopt --domain <libvirt-domain> --ssh-user $env:USERNAME --ssh-host $primary"
Write-Host ''

if ($script:Failed) {
    Write-Host 'One or more steps failed; see [failed] lines above.'
    exit 1
}
exit 0
