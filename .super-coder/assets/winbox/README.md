# winbox guest scripts

Two PowerShell scripts that turn a bring-your-own-licence Windows 10/11 guest
into a Subfloor test VM. Both are Windows PowerShell 5.1 compatible, use no
external modules, and are idempotent. Neither carries key material, host names,
tokens or anything fork-specific — one copy serves every fork.

## `bootstrap.ps1`

Run once by the operator in an **elevated** PowerShell inside the guest, before
`./sc vm adopt`. One line, printed by `adopt` and by the runbook:

```powershell
powershell -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; irm https://raw.githubusercontent.com/jedbjorn/subfloor/<ref>/.super-coder/assets/winbox/bootstrap.ps1 | iex"
```

`<ref>` is the engine's pinned commit (`.super-coder/engine.ref` when present,
else `main`); `adopt --bootstrap-url` overrides it. The explicit `Tls12` is
required: Windows PowerShell 5.1 still negotiates SSL3/TLS1.0 by default and
`raw.githubusercontent.com` refuses that. A guest with no internet can have the
file copied in by any means; the script does not care how it arrived.

What it does, each step reported as `[done]`, `[skipped]` or `[failed]`:

1. Refuses unless elevated; refuses Windows older than 10 1809 (build 17763,
   the first build shipping the OpenSSH server capability). Reports edition,
   release and build.
2. `Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0` when not
   already installed; `sshd` to Automatic and running; firewall rule
   `OpenSSH-Server-In-TCP` present and enabled (created as TCP 22 inbound allow
   when missing).
3. Makes `PubkeyAuthentication yes` and `PasswordAuthentication yes` explicit in
   `C:\ProgramData\ssh\sshd_config`, rewriting the file and restarting `sshd`
   only when something actually changed. The default shell stays `cmd.exe`.
4. `Set-ExecutionPolicy -Scope LocalMachine Bypass -Force`; creates
   `C:\SubfloorTest`.
5. Optional AutoLogon (`-AutoLogon` when run as a file, or `SUBFLOOR_AUTOLOGON=1`
   on the `irm | iex` path): prompts once with `Read-Host -AsSecureString` and
   writes the Winlogon `AutoAdminLogon`, `DefaultUserName`, `DefaultDomainName`
   and `DefaultPassword` values so a reset to an offline snapshot boots into a
   desktop session for GUI driving. Throwaway guests only; the password stays
   inside the guest and never crosses to the host.
6. Prints the account, every non-loopback IPv4 address with its interface, and
   the next host command:
   `./sc vm adopt --domain <libvirt-domain> --ssh-user <account> --ssh-host <ip>`.

Exit code is 0 when no step failed, 1 otherwise; the report prints either way.

## `provision.ps1`

Run by the host over SSH as the adopting admin, non-interactively:

```
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\SubfloorTest\provision.ps1
```

It reads `winbox.json` from its **own directory** (`$PSScriptRoot`), prints
human progress lines, and emits exactly one JSON object as the last line of
stdout. Steps:

1. `workspace` — execution policy Bypass and the workspace directory.
2. `winget` — presence check; when absent the step fails with the App Installer
   hint and the manifest import is skipped. The engine never installs winget.
3. `winget-import` — `winget import --import-file <manifest>
   --accept-package-agreements --accept-source-agreements
   --disable-interactivity` when `winget_manifest` is set. "already installed"
   output is treated as success.
4. `windows-mcp` — when `mcp` is true: Python 3.13+
   (`winget install --id Python.Python.3.13 -e ...` when missing or older),
   `uv` (`winget install --id astral-sh.uv -e ...`),
   `uv tool install --upgrade windows-mcp`, then
   `windows-mcp install --transport streamable-http --host 127.0.0.1 --port <mcp_port>`,
   then a listener confirmation on `127.0.0.1:<mcp_port>` retried for about 60s.
   PATH is refreshed from the Machine and User registry values after each
   winget install so later steps see the new tools. Loopback only; the broker
   tunnel is the route in.
5. `check: <command>` — one step per `checks` entry, run through `cmd /c` with
   stdout and stderr captured; non-zero exit fails that step.

Exit code is 0 when every step is ok, 1 otherwise.

## What the host ships to the guest

`adopt` copies these into the guest workspace (default `C:\SubfloorTest\`) over
`scp` before running the provisioner:

| Guest path | Source |
|---|---|
| `C:\SubfloorTest\provision.ps1` | `.super-coder/assets/winbox/provision.ps1` |
| `C:\SubfloorTest\winbox.json` | resolved from the fork's `.subfloor/winbox.json` |
| `C:\SubfloorTest\<winget_manifest>` | the manifest named by `winbox.json`, resolved from the fork repo |

`bootstrap.ps1` is not shipped by `adopt`; the operator fetches it in the guest
from the public repository, or copies it in by hand.

## `winbox.json`

Fork-tracked at `.subfloor/winbox.json`, every key optional. The host resolves
defaults, validates the shape, and ships a **resolved** copy with every key
present, so the guest never reads fork files:

```json
{
  "winget_manifest": "winget-manifest.json",
  "checks": ["dotnet --version", "git --version"],
  "mcp": true,
  "mcp_port": 8000,
  "workspace": "C:\\SubfloorTest"
}
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `winget_manifest` | string or null | `winget-manifest.json` when it exists at the repo root, else null | filename of the manifest, shipped next to `provision.ps1` |
| `checks` | list of strings | `[]` | commands run through `cmd /c`; also drive the engine's toolchain check |
| `mcp` | bool | `true` | install and register Windows-MCP |
| `mcp_port` | int | `8000` | loopback port for the Windows-MCP listener |
| `workspace` | string | `C:\SubfloorTest` | guest working directory |

In the resolved copy the guest receives, `winget_manifest` is a bare filename
(resolved against `$PSScriptRoot`) or `null`; `checks` is always a list;
`mcp`, `mcp_port` and `workspace` are always present.

## Report contract

`provision.ps1` emits, as the final stdout line:

```json
{"steps":[{"name":"workspace","ok":true,"detail":"..."}],"ok":true}
```

`steps` is always a JSON array (each step is serialized separately, because
Windows PowerShell 5.1 `ConvertTo-Json` collapses a one-element array).
`ok` is the conjunction of every step's `ok`. `detail` carries the captured
command output, truncated to about 2000 characters. Nothing is printed after
this line, so the host parses the last non-empty stdout line as JSON.
