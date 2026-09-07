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

The FnB opens the dedicated Subfloor profile and approves each new shell
connection in the Playwright Extension. Ask the FnB if either step is missing;
never open the profile or click its approval control yourself. Work only in
your tab group, without moving or touching other groups' tabs.

Snapshot before acting, preferring accessibility snapshots over screenshots.
Save screenshots/downloads only in the returned output directory. Close the
tabs you opened unless directed to leave them. Report the result, tab group,
the session path returned by Playwright, and anything left open.

`extension not connected` means the profile is closed, mismatched, or the
connection is unapproved. Report and stop; never retry in a loop. A timed-out
action has an unknown outcome: inspect only after a new directive to resume,
rather than replaying a mutation. `disarmed` requires the FnB to arm it.

Admin diagnosis/repair stays under the operator's named assignment. The FnB's
host terminal can run `sc browser doctor --json`, `up`, `down`, `arm`, or
`disarm`; launched shell credentials only permit `status`. Doctor verifies the
exact MCP/Playwright/core versions and installs an absent package tree; drift
requires operator repair. Creating profiles, installing the extension, logins,
and approval clicks remain human steps. See the engine's
`.super-coder/docs/browser-driving.md` for the setup and live acceptance record.
