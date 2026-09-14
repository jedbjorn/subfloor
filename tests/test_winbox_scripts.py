"""Guest-side winbox scripts: content contract, 5.1 compatibility, syntax."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WINBOX = ROOT / ".super-coder" / "assets" / "winbox"
BOOTSTRAP = WINBOX / "bootstrap.ps1"
PROVISION = WINBOX / "provision.ps1"
README = WINBOX / "README.md"

SCRIPTS = (BOOTSTRAP, PROVISION)
RETIRED = ("configure_winbox", "windows_vm_gui", "transfer_dir")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def bootstrap() -> str:
    return _read(BOOTSTRAP)


@pytest.fixture(scope="module")
def provision() -> str:
    return _read(PROVISION)


@pytest.mark.parametrize("path", [BOOTSTRAP, PROVISION, README])
def test_files_exist_and_are_not_empty(path: Path) -> None:
    assert path.is_file(), f"{path} is missing"
    assert len(_read(path).strip()) > 200, f"{path} is empty or a stub"


def test_bootstrap_covers_every_spec_step(bootstrap: str) -> None:
    required = (
        "OpenSSH.Server~~~~0.0.1.0",
        "OpenSSH-Server-In-TCP",
        "PubkeyAuthentication yes",
        "PasswordAuthentication yes",
        "Set-ExecutionPolicy",
        r"C:\SubfloorTest",
        "Win32_OperatingSystem",
        "AutoAdminLogon",
        "DefaultUserName",
        "DefaultDomainName",
        "DefaultPassword",
        "Read-Host",
        "-AsSecureString",
        "SUBFLOOR_AUTOLOGON",
        "./sc vm adopt --domain",
    )
    for needle in required:
        assert needle in bootstrap, f"bootstrap.ps1 is missing {needle!r}"


def test_bootstrap_gates_on_elevation_and_build(bootstrap: str) -> None:
    assert "17763" in bootstrap
    assert "WindowsBuiltInRole]::Administrator" in bootstrap
    assert "BuildNumber" in bootstrap
    # Edition and release come from CIM plus the registry.
    assert "ReleaseId" in bootstrap
    assert "DisplayVersion" in bootstrap


def test_bootstrap_reports_each_step_and_the_next_command(bootstrap: str) -> None:
    for marker in ("[$Status] $Name", "'done'", "'skipped'", "'failed'"):
        assert marker in bootstrap
    assert "$env:USERDOMAIN\\$env:USERNAME" in bootstrap
    assert "Get-NetIPAddress" in bootstrap
    assert "InterfaceAlias" in bootstrap
    assert "169.254." in bootstrap, "APIPA addresses must not be offered as the host"
    assert "exit 1" in bootstrap and "exit 0" in bootstrap


def test_bootstrap_reports_a_capability_install_that_needs_a_reboot(
    bootstrap: str,
) -> None:
    """A discarded Add-WindowsCapability result meant every later step failed
    for a reason that never named the reboot."""
    assert "$added = Add-WindowsCapability -Online" in bootstrap
    assert "RestartNeeded" in bootstrap
    assert "reboot the guest, then re-run this line" in bootstrap
    assert "$script:RebootRequired" in bootstrap
    # The report prints before the early exit.
    reboot_block = bootstrap.split("if ($script:RebootRequired) {", 1)[1]
    assert "Write-Host 'Report'" in reboot_block.split("exit 1", 1)[0]


def test_bootstrap_waits_for_sshd_to_write_its_config(bootstrap: str) -> None:
    """A fresh OpenSSH install has no sshd_config until sshd's first start
    writes one, which happens after Start-Service returns."""
    assert "sshd_config_default" in bootstrap
    assert "AddSeconds(60)" in bootstrap
    assert "waited for sshd to write sshd_config" in bootstrap
    assert "seeded sshd_config from sshd_config_default" in bootstrap


def test_bootstrap_places_auth_settings_outside_the_match_block(bootstrap: str) -> None:
    """The stock Windows sshd_config ENDS with `Match Group administrators`,
    so an appended directive governs only that group."""
    assert "'^\\s*Match\\s'" in bootstrap
    assert "$placed" in bootstrap
    assert "if (-not $placed) { $rebuilt += $target }" in bootstrap
    # And only the first existing occurrence is rewritten.
    assert "break" in bootstrap.split("if ($lines[$i] -match $pattern) {", 1)[1]


def test_bootstrap_writes_the_config_as_utf8_without_a_bom(bootstrap: str) -> None:
    assert "[IO.File]::WriteAllText(" in bootstrap
    assert "New-Object System.Text.UTF8Encoding($false)" in bootstrap
    assert "-Encoding ASCII" not in bootstrap


def test_bootstrap_autologon_marshals_and_validates_the_password(
    bootstrap: str,
) -> None:
    """PtrToStringAuto stops at the first null and would store a TRUNCATED
    password; an empty one arms AutoAdminLogon to fail at every boot."""
    assert "::PtrToStringBSTR(" in bootstrap
    assert "::PtrToStringAuto(" not in bootstrap
    assert "[string]::IsNullOrEmpty($plain)" in bootstrap
    assert "AutoAdminLogon left unchanged" in bootstrap
    # The reported domain is the value actually written to the registry.
    assert "$logonDomain = $env:COMPUTERNAME" in bootstrap
    assert (
        "Set-ItemProperty -Path $winlogon -Name 'DefaultDomainName' "
        "-Value $logonDomain -Type String" in bootstrap
    )


def test_bootstrap_offers_the_default_route_address_first(bootstrap: str) -> None:
    """Sorting interface aliases picked a Hyper-V or loopback adapter on a
    guest with more than one NIC."""
    assert "Get-NetRoute -DestinationPrefix '0.0.0.0/0'" in bootstrap
    assert "RouteMetric" in bootstrap
    assert "InterfaceIndex" in bootstrap
    # The old rule survives as the fallback.
    assert "169.254." in bootstrap


def test_bootstrap_keeps_the_default_shell(bootstrap: str) -> None:
    # The engine's exec conventions assume cmd.exe; the bootstrap must not set
    # the OpenSSH DefaultShell registry value.
    assert "DefaultShell" not in bootstrap


def test_bootstrap_carries_no_identity_material(bootstrap: str) -> None:
    for retired in RETIRED:
        assert retired not in bootstrap, f"bootstrap.ps1 names retired {retired!r}"
    lowered = bootstrap.lower()
    for token in (
        "ssh-rsa",
        "ssh-ed25519",
        "-----begin",
        "authorized_keys",
        "known_hosts",
        "id_ed25519",
        "bearer ",
        "api_key",
        "token=",
    ):
        assert token not in lowered, f"bootstrap.ps1 contains key material: {token!r}"
    assert "://" not in bootstrap, "bootstrap.ps1 must not hard-code a host name"
    scrubbed = bootstrap
    for allowed in (
        "OpenSSH.Server~~~~0.0.1.0",
        "169.254.",      # the APIPA prefix the report skips
        "127.0.0.1",
        "0.0.0.0/0",     # the IPv4 default route, not an address
    ):
        scrubbed = scrubbed.replace(allowed, "")
    assert not re.search(
        r"\b\d{1,3}(?:\.\d{1,3}){3}\b", scrubbed
    ), "bootstrap.ps1 must not hard-code a guest or host address"


def test_provision_runs_the_spec_commands(provision: str) -> None:
    """The spec's commands, now as ARGUMENT ARRAYS (see the native-invocation
    test below) rather than command-line strings."""
    required = (
        "'import', '--import-file', $manifestPath",
        "'--accept-package-agreements', '--accept-source-agreements'",
        "'--disable-interactivity'",
        "'install', '--id', 'Python.Python.3.13', '-e'",
        "'install', '--id', 'astral-sh.uv', '-e'",
        "@('tool', 'install', '--upgrade', 'windows-mcp')",
        "'install', '--transport', 'streamable-http'",
        "'--host', '127.0.0.1', '--port', [string]$mcpPort",
        "Get-NetTCPConnection -State Listen",
        "ConvertTo-Json -Compress -Depth 4",
        "cmd.exe /c",
    )
    for needle in required:
        assert needle in provision, f"provision.ps1 is missing {needle!r}"


def test_provision_invokes_tools_natively_not_through_cmd(provision: str) -> None:
    r"""cmd.exe re-parses the line it is handed, so an argument carrying a
    quoted path (winget import --import-file "C:\dir\manifest.json") arrives
    with the quote characters still in it and the manifest is never found.
    Only the fork's own free-form `checks` go through cmd."""
    assert "function Invoke-Native" in provision
    assert "& $Exe @Arguments 2>&1 | Out-String" in provision
    # cmd is INVOKED exactly once, in the checks helper.
    assert provision.count("cmd.exe /c") == 1
    shell = provision.split("function Invoke-Shell", 1)[1].split("\n}", 1)[0]
    assert "cmd.exe /c $CommandLine" in shell
    # Every engine-built tool call goes through the native helper.
    for tool in ("'winget'", "'uv'", "'python'", "'windows-mcp'"):
        assert f"Invoke-Native -Exe {tool}" in provision, tool
    assert "Invoke-Capture" not in provision, "the cmd.exe helper is retired"
    # A missing executable stays a RESULT: `& 'python'` with no python throws.
    assert "is not recognized as a command" in provision
    assert "ExitCode = 9009" in provision


def test_provision_always_emits_a_report(provision: str) -> None:
    """A host that gets no report line cannot tell a broken guest from a dead
    script, so an escaped terminating error becomes a failed step instead."""
    assert "Add-StepResult -Name 'script' -Ok $false" in provision
    assert "unhandled error: " in provision
    body = provision.split("Write-Host 'Subfloor winbox provisioning'", 1)[1]
    assert body.lstrip().startswith("try {")
    # The report assembly sits AFTER the catch, so it always runs.
    catch_at = body.index("} catch {")
    assert catch_at < body.index("$report = ")


def test_provision_uses_error_action_stop_inside_its_try_blocks(provision: str) -> None:
    """Under the script's Continue preference a non-terminating cmdlet failure
    never reaches `catch`, so the step reports ok with nothing done."""
    assert "$ErrorActionPreference = 'Continue'" in provision
    assert "$ErrorActionPreference = 'Stop'" not in provision
    for cmdlet in (
        "Get-ExecutionPolicy -Scope LocalMachine -ErrorAction Stop",
        "Set-ExecutionPolicy -Scope LocalMachine Bypass -Force -ErrorAction Stop",
        "New-Item -ItemType Directory -Path $workspace -Force -ErrorAction Stop",
        "Get-Content -LiteralPath $configPath -Raw -ErrorAction Stop",
    ):
        assert cmdlet in provision, cmdlet


def test_provision_reads_winget_idempotence_from_the_exit_code(provision: str) -> None:
    """"already installed" is localized; the exit code is not."""
    assert "$WingetNoApplicableUpgrade = -1978335131" in provision
    assert "$import.ExitCode -eq $WingetNoApplicableUpgrade" in provision
    # The text stays only as a fallback for an older winget.
    assert "already installed" in provision


def test_provision_compares_python_versions_numerically(provision: str) -> None:
    """A digit-enumerating regex answers wrongly for 3.130 and for 4.x."""
    assert "function Test-PythonVersionOk" in provision
    assert provision.count("Test-PythonVersionOk -Text") == 2, (
        "both the first check and the post-install recheck must use it"
    )
    assert "Python\\s+3\\.(1[3-9]" not in provision


def test_provision_keeps_remembered_path_directories_across_refreshes(
    provision: str,
) -> None:
    """uv's tool bin dir is not in the registry PATH, so a plain refresh from
    the registry drops it again."""
    assert "$script:PathExtras" in provision
    assert "function Add-PathExtra" in provision
    extras = provision.split("function Update-PathFromRegistry", 1)[1]
    assert "foreach ($extra in $script:PathExtras) { $parts += $extra }" in extras
    assert "Add-PathExtra -Directory $binDir" in provision
    assert "Add-PathExtra -Directory $aliasDir" in provision


def test_provision_treats_an_unneeded_winget_as_satisfied(provision: str) -> None:
    assert "nothing declared needs it (no winget_manifest, mcp off)" in provision


def test_provision_always_emits_a_checks_step(provision: str) -> None:
    assert "-Name 'checks' -Ok $true -Detail 'no checks declared'" in provision


def test_provision_resolves_winget_again_after_registering_the_alias(
    provision: str,
) -> None:
    """The whole point of the re-registration block is that `winget` was not
    resolvable before it ran."""
    block = provision.split("Add-AppxPackage -DisableDevelopmentMode -Register $manifest", 1)[1]
    block = block.split("if ($wingetPresent) {", 1)[0]
    assert "$wingetPresent = Test-CommandPresent -Name 'winget'" in block


def test_provision_reads_its_config_from_script_root(provision: str) -> None:
    assert "Join-Path $PSScriptRoot 'winbox.json'" in provision
    for key in ("winget_manifest", "checks", "mcp", "mcp_port", "workspace"):
        assert f"'{key}'" in provision, f"provision.ps1 ignores winbox.json key {key!r}"


def test_provision_emits_one_json_report_last(provision: str) -> None:
    assert '\'{"steps":[\'' in provision
    assert '\'],"ok":\'' in provision
    body = provision.split('$report = ')[-1]
    # Nothing but the report write and the exit codes may follow the assembly.
    assert "Write-Output $report" in body
    tail = body.split("Write-Output $report", 1)[1]
    assert "Write-Host" not in tail, "nothing may print after the JSON report line"
    assert "Write-Output" not in tail


def test_provision_handles_the_honest_mcp_outcome(provision: str) -> None:
    # The listener warning is prefixed so the host can carry it as a warning
    # rather than a failure: `windows-mcp install` registers a per-user LOGON
    # task, which never runs on a guest with no desktop session.
    assert (
        "WARNING: installed but no listener yet; a desktop login session may be required"
        in provision
    )
    # And the listener probe counts, rather than comparing an array to $null.
    assert "$conn.Count -gt 0" in provision
    assert "-ne $null" not in provision
    assert "aka.ms/getwinget" in provision, "winget absence must carry the hint"
    assert "Environment]::GetEnvironmentVariable('Path', 'Machine')" in provision
    assert "Environment]::GetEnvironmentVariable('Path', 'User')" in provision


def test_provision_truncates_captured_output(provision: str) -> None:
    assert "$DetailLimit = 2000" in provision
    assert "Out-String" in provision
    assert "Substring(0, $DetailLimit)" in provision


@pytest.mark.parametrize("path", SCRIPTS)
def test_scripts_use_lf_line_endings(path: Path) -> None:
    assert b"\r\n" not in path.read_bytes(), f"{path} has CRLF line endings"


@pytest.mark.parametrize("path", SCRIPTS)
def test_scripts_avoid_powershell_7_only_syntax(path: Path) -> None:
    text = _read(path)
    forbidden = {
        "null-coalescing (??)": r"\?\?",
        "null-coalescing assignment (??=)": r"\?\?=",
        "null-conditional (?. / ?[])": r"\$\w+\?(?:\.|\[)",
        "ForEach-Object -Parallel": r"-Parallel\b",
        "ternary inside a subexpression": r"\$\([^()\n]*\?[^()\n]*:[^()\n]*\)",
        "$PSStyle": r"\$PSStyle\b",
        "-AsByteStream": r"-AsByteStream\b",
        "utf8NoBOM / utf8BOM encodings": r"-Encoding\s+utf8(?:NoBOM|BOM)\b",
        "Test-Json": r"\bTest-Json\b",
        "ConvertFrom-Json -AsHashtable": r"-AsHashtable\b",
        "Get-Error": r"\bGet-Error\b",
        "Join-String": r"\bJoin-String\b",
        "&& / || pipeline chains": r"(?<![&|])(?:&&|\|\|)(?![&|])",
        "ConvertTo-Json -EnumsAsStrings": r"-EnumsAsStrings\b",
    }
    for label, pattern in forbidden.items():
        match = re.search(pattern, text)
        assert match is None, (
            f"{path.name} uses PowerShell 7-only syntax ({label}): "
            f"{text[max(0, match.start() - 40):match.end() + 40]!r}"
        )
    # A bare ternary outside a subexpression is equally 5.1-hostile.
    assert not re.search(r"^\s*\$\w+\s*=\s*[^=\n]+\s\?\s[^:\n]+\s:\s", text, re.MULTILINE)


@pytest.mark.parametrize("path", SCRIPTS)
def test_scripts_parse(path: Path) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("pwsh is not on PATH; PowerShell parse check skipped")
    command = (
        "$errors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{path}', [ref]$null, [ref]$errors) | Out-Null; "
        "if ($errors -and $errors.Count -gt 0) { "
        "$errors | ForEach-Object { Write-Output $_.ToString() }; exit 1 } "
        "else { exit 0 }"
    )
    result = subprocess.run(
        [pwsh, "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"{path.name} failed to parse:\n{result.stdout}"


def test_readme_documents_the_shipped_files_and_report(bootstrap: str) -> None:
    readme = _read(README)
    for needle in (
        "bootstrap.ps1",
        "provision.ps1",
        "winbox.json",
        "winget_manifest",
        "mcp_port",
        "workspace",
        r"C:\SubfloorTest\provision.ps1",
        '{"steps":[{"name":"workspace","ok":true,"detail":"..."}],"ok":true}',
    ):
        assert needle in readme, f"README.md is missing {needle!r}"
