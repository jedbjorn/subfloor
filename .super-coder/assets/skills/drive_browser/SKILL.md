---
name: drive_browser
description: Drive the FnB's dedicated Chromium Subfloor profile through the managed Playwright extension for a named browser task or logged-in dev-preview check. Bare metal with Claude, Codex, or OpenCode only; Admin uses the same surface for diagnosis and repair.
category: substrate
command: sc browser
common: false
---

# drive_browser

Use only under an FnB directive naming the task, site/app, and allowed actions.
Restate that reach before the first browser call. If no directive exists, ask.
Do not expand it to other accounts or sites. Credential/account changes,
payments, public posting, and downloads outside the output directory require
explicit coverage in the directive. Never enter credentials when a site asks
to sign in; stop and report.

Run `sc browser status --json`. If absent, disarmed, unsupported, or failed,
report the returned state and stop. Use the managed MCP connection; never edit
harness configuration, start a substitute server, or arm the feature yourself.
The returned `proxy_url` is yours and the tab group is
`Playwright · Subfloor <SHORTNAME>`.

Run `sc browser open --json` to launch the linked, existing Subfloor profile.
No live window is required. The FnB still approves each new shell connection
in the Playwright Extension; never click that approval control yourself. Work
only in your tab group, without moving or touching other groups' tabs.

Snapshot before acting, preferring accessibility snapshots over screenshots.
Save screenshots/downloads only in the returned output directory. Close the
tabs you opened unless directed to leave them. Report the result, tab group,
the session path returned by Playwright, and anything left open.

`extension not connected` means the extension is unavailable or the connection
is unapproved; it does not prove the profile window is closed. Report and stop; never retry in a loop. The proxy
sets no action deadline — it waits for Playwright's own action or navigation
result and keeps the approved session alive, so a slow action is still running:
wait for its result rather than replaying it. Confirmed transport loss ends the
connection and is reported as such. `disarmed` requires the FnB to arm it.

Admin diagnosis/repair stays under the operator's named assignment. The FnB's
host terminal can run `sc browser setup --json` to detect and link the existing
profile, or `doctor`, `up`, `down`, `arm`, and `disarm`. Launched shell credentials
permit `status` and (with this grant) `open`; they cannot set paths or arm. Doctor
checks actual package capabilities and repairs missing or incompatible private
packages without version pins. It reports `setup_ready` separately from
`connection_ready`; `ok` requires both and running, armed services. Creating profiles, installing the extension, logins,
and approval clicks remain human steps. See the engine's
`.super-coder/docs/browser-driving.md` for the setup and live acceptance record.
