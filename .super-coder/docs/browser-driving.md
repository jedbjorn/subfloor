# Browser driving

Browser driving is an opt-in, bare-metal engine capability for Claude, Codex,
and OpenCode. Kimi, Vibe, and container seats are unsupported. It targets an
existing Chromium profile displayed as **Subfloor**, with the Playwright
Extension installed by the operator. Each new shell connection requires
operator approval. The engine does not create profiles, sign in, approve
connections, or store an extension credential.

## Setup and use

1. Create a Chromium profile named Subfloor. Install the
   [Playwright Extension](https://chromewebstore.google.com/detail/mmlmfjhmonkocbjadbfplnigmagldckm)
   there and sign in only to accounts you intend to make available.
2. Run `sc feature enable browser` from the operator's host terminal. This grants
   `drive_browser` to dev, reviewer, planner, and admin.
3. Run `sc browser setup --json`, or use Scripts → Browser → link profile.
   Setup discovers standard Chromium/Chrome user-data roots and their PATH
   launchers, resolves Subfloor to its profile directory, installs the private
   MCP dependencies if needed, and starts the managed service. It accepts
   Chromium launch wrappers; no native binary or process inspection is needed.
   If multiple installations contain Subfloor, select the installation explicitly:
   `sc browser setup --user-data-dir /absolute/path --executable /absolute/launcher`.
   The GUI offers the same optional path overrides. It refuses ambiguous profiles.
4. Give a shell a directive naming the site and actions. It can run
   `sc browser open --json` to open the linked profile, then connect through
   the managed MCP and work in `Playwright · Subfloor <SHORTNAME>`.
   Approve that connection in the extension yourself.

A live window is not a prerequisite. Local State is used only to identify the
existing dedicated profile, never as live-window evidence. Closing a window
does not suspend access: **disarm** to suspend access. The launcher checks that
the linked profile still exists and is named Subfloor, then explicitly selects
its user-data directory and profile on every launch. It never silently switches
to Default or creates a missing profile. Chromium handles its own process lock.
An MCP connection can also open the linked profile to show its approval page.

Setup can link an existing profile before the extension is installed; it reports
the missing extension. Use Open Subfloor, install it there, then run doctor.
Profile creation, extension installation, account sign-in, and approval remain
operator steps.

The API server must run on the host. On the docker runtime it runs inside
`sc-<repo>` without the host profile or graphical session. Setup/open/arm refuse
with `unsupported seat: browser requires bare metal`; the GUI disables controls.

## Packages and diagnostics

The manifest requests `@playwright/mcp@latest` without an engine lockfile or
Playwright overrides. Upstream owns its dependency selection. The engine accepts
an existing installation when Node can resolve the MCP/Playwright/core packages
and the executable CLI supports every flag the engine uses. Versions are
reported as observations, not admission gates. Old saved version fields remain
readable and are discarded from the normalized configuration.

Setup, doctor, and service startup bootstrap missing or incompatible packages.
A fresh installation is staged privately and checked before replacing the old
tree. Download or capability failures preserve the previous tree and report the
failure. A working tree is reused without a network fetch on each launch.
Node >=20 and npm must be installed on the host; failures name that prerequisite.
No globally installed Node packages or browser state are changed.

`sc browser doctor --json` (also Diagnose / repair in the GUI) reports:

- `packages`: executable capability result and resolved versions.
- `checks`: existing named profile and extension installation evidence.
- `setup_ready`: packages, profile, and extension are present and usable.
- `connection_ready`: the proxy has observed a successful shell tool call.
- `ok`: setup is usable, services are running and armed, and a connection was
  observed. Setup alone does not mean the extension has approved a connection.

Connection status is an observation, not a heartbeat. The next request detects
a disconnect. Doctor never opens Chromium or approves a connection, and returns
nonzero until end-to-end readiness has been observed. An extension directory or Preferences registration
proves installation evidence exists, not that Chrome has enabled the extension.

`sc browser arm|disarm`, setup, doctor, up/down, and disable are operator
controls. Disarm stops the upstream server and leaves the proxy available for a
named refusal; initialize and tools/list remain valid. Up/down do not change the
saved arm setting. `sc feature disable browser` stops both processes, removes
the block, and reverses feature-owned grants. Relaunch shells after feature
enabling/disabling to refresh tool discovery. Launched shell credentials can
read status and, with the `drive_browser` grant, open the saved profile; they
cannot supply paths, link a different profile, repair packages, or arm access.

## Mechanism and boundaries

Two persistent processes run on loopback: Playwright MCP and a stdlib HTTP proxy.
`ports.py` allocates distinct ports in the 8800 band. The configuration records
browser executable/launcher, user-data and profile directories, ports, optional
blocked origins, and armed state. There is no maintained upstream patch,
headless/CDP fallback, or unrestricted-file-access flag. The MCP process inherits
a small environment allowlist without upstream token/config overrides.

The proxy exposes `/mcp/<SHORTNAME>`, substitutes `Subfloor <SHORTNAME>` in
clientInfo, and binds sessions to that path. Tool calls refuse while disarmed or
when the linked profile becomes unavailable. An unapproved/disconnected extension
fails within the 30-second request budget (plus at most one second for cleanup);
actions are never retried. Overlapping requests for one session refuse rather
than queue. Timeouts can have an unknown action outcome: obtain a new directive
and inspect before repeating a mutation.

SSE and JSON responses pass through. Each call records timestamp, shell, tool,
target, and outcome in private `browser/audit.jsonl` (0600). URL queries,
fragments, credentials, and typed text are omitted. Screenshots/session files
remain in private `browser/sessions` (0700); page bodies never enter the memory
DB. Per-shell URLs provide attribution, not host-process authentication.
Loopback access still requires armed state and extension approval. Site account
reach belongs to the dedicated profile; signing out/revoking sessions removes
it. Origin blocklists provide friction, not a security boundary.

## Verification

CI exercises the current npm package through real MCP initialize/tools discovery,
plus capability admission, failed-repair retention, profile identity with empty
or stale active-window lists, explicit launch, shell authorization, GUI actions,
proxy timeout/identity/audit behavior, adapters, and grant/reseed behavior.
Fixtures never launch or mutate the operator's real profile.

The real extension approval remains an operator acceptance check after updating:

- [ ] `sc browser setup` detects and links the existing Subfloor profile.
- [ ] With Chromium closed, `sc browser open` opens Subfloor successfully.
- [ ] With only another profile open, the verb opens Subfloor without targeting
      that other profile. An empty `last_active_profiles` list does not refuse.
- [ ] First managed tool call connects after the operator's approval click.
- [ ] Doctor separates setup readiness from the observed connection.
- [ ] Two shells appear in separate named tab groups; disconnecting one leaves
      the other usable.
- [ ] Disarming refuses both profile open and managed tool calls.
- [ ] Audit/session files contain only intended task data and expected modes.

Verified on: **not yet run**. Record Chromium/extension versions and the doctor
receipt with the dated operator result; CI does not prove a real approval click.
