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
    for allowed in ("OpenSSH.Server~~~~0.0.1.0", "169.254.", "127.0.0.1"):
        scrubbed = scrubbed.replace(allowed, "")
    assert not re.search(
        r"\b\d{1,3}(?:\.\d{1,3}){3}\b", scrubbed
    ), "bootstrap.ps1 must not hard-code a guest or host address"


def test_provision_runs_the_spec_commands(provision: str) -> None:
    required = (
        "winget import --import-file",
        "--accept-package-agreements",
        "--accept-source-agreements",
        "--disable-interactivity",
        "winget install --id Python.Python.3.13 -e",
        "winget install --id astral-sh.uv -e",
        "uv tool install --upgrade windows-mcp",
        "windows-mcp install --transport streamable-http --host 127.0.0.1 --port",
        "Get-NetTCPConnection -State Listen",
        "ConvertTo-Json -Compress -Depth 4",
        "cmd.exe /c",
    )
    for needle in required:
        assert needle in provision, f"provision.ps1 is missing {needle!r}"


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
    assert (
        "installed but no listener yet; a desktop login session may be required"
        in provision
    )
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
        "null-conditional (?.)": r"\?\.",
        "ForEach-Object -Parallel": r"-Parallel\b",
        "ternary inside a subexpression": r"\$\([^()\n]*\?[^()\n]*:[^()\n]*\)",
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
