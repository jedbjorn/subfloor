# Browser driving

Browser driving is an opt-in, bare-metal engine capability for Claude, Codex,
and OpenCode. Kimi, Vibe, and container seats are unsupported. It targets a
Chromium profile displayed as **Subfloor**, with the Playwright Extension
installed by the operator. Each new shell connection requires operator approval.
The engine does not create profiles, sign in, approve connections, or store an
extension credential.

## Link and use

1. Create a Chromium profile named Subfloor. Install the
   [Playwright Extension](https://chromewebstore.google.com/detail/mmlmfjhmonkocbjadbfplnigmagldckm)
   there and sign in only to accounts you intend to make available.
2. Run `sc feature enable browser` from the operator's host terminal. This grants
   `drive_browser` to dev, reviewer, planner, and admin. Cartographer and devops
   receive no grant. The profile remains operator-linked.
3. In Scripts → Browser, check and link the profile. The native executable is
   the Chromium binary (on Arch, `/usr/lib/chromium/chromium`), not a shell
   wrapper. Adjust the absolute executable/user-data paths for your installation.
   The engine resolves `Subfloor` to its actual profile directory using Local State.
4. Open Subfloor, give a shell a directive naming the site and actions, and
   approve that connection in the extension. Shells preflight with
   `sc browser status --json` and work in `Playwright · Subfloor <SHORTNAME>`.

`sc browser doctor --json` checks Node >=20, installs an absent package tree,
reports all resolved versions, and checks the profile and extension. It never
opens Chromium. If a tree is present but drifted, doctor fails instead of
silently accepting or replacing it. Operator repair removes only that private
browser package tree and runs doctor again. `sc launch` starts the linked,
armed browser service; it never fetches packages. No service starts for a
container browser seat.

`sc browser arm|disarm` and the GUI toggle are operator controls. Disarm stops
the upstream server but leaves the proxy available for a named refusal;
initialize and tools/list remain valid. Up/down control both processes without
changing the saved arm setting. `sc feature disable browser` stops both,
removes the block, and reverses feature-owned grants. Launch shells again after
enabling/disabling to refresh tool discovery. Launched shell credentials may
read status only, including Admin credentials; Admin performs repair under the
operator's named maintenance assignment.

## Mechanism and boundaries

The committed package manifest and lock resolve `@playwright/mcp@0.0.80`, with
exact overrides `playwright@1.63.0` and `playwright-core@1.63.0`. These overrides
supply `--profile-dir-name`. Doctor compares all three packages, the manifest,
the lockfile, and CLI flag support. There is no maintained upstream patch.

There are two persistent processes: a loopback Playwright MCP server and a
stdlib HTTP proxy. `ports.py` allocates `browser_port` and `browser_proxy_port`
in the 8800 band, avoiding API/dev and sibling assignments. The browser block
records both ports, native executable, channel, user-data directory, profile
directory, pinned versions, optional blocked origins, and armed state.

Upstream invokes one stdlib executable guard to open its extension connect
page. Before exec, the guard checks the Chromium SingletonLock host/PID, live
same-user executable identity, and Local State's last_active_profiles. Missing,
stale, malformed, or wrong-profile state refuses. Both checks are required;
a running Chromium on another profile is insufficient. This uses Chromium's
reported state at exec time; it is not an atomic browser-window lock. The
post-Sprint real-profile checks below establish its behavior on the installed
Chromium version. No headless/CDP fallback or unrestricted-file-access flag is
used. The server inherits a small environment allowlist, not upstream overrides.

The proxy exposes `/mcp/<SHORTNAME>`, substitutes `Subfloor <SHORTNAME>` in
clientInfo, and binds transport sessions to that path. Tool calls are refused
while disarmed or when the profile guard fails. An unapproved/disconnected
extension fails within the 30-second request budget (plus at most one second
for transport cleanup); actions are never retried. An overlapping request for
the same session is refused, not queued. Timeouts can have an unknown action
outcome; obtain a new directive and inspect before repeating a mutation.

SSE and JSON responses pass through. Each call records timestamp, shell, tool,
target, and outcome in private `browser/audit.jsonl` (0600). URL queries,
fragments, credentials, and typed text are omitted from the target. The
private `browser/sessions` output directory is 0700. Screenshots/session files
remain there; page bodies never enter the audit or memory DB. Status reports
observed successful shell connections, not a continuous extension heartbeat;
the next request detects a disconnect. Use Refresh status for a new snapshot.

Per-shell URLs are attribution, not host-process authentication. Loopback
access still requires an armed proxy and operator approval in the extension.
Skill directives bound use to the named task and tab group. Signing out or
revoking a site's session removes its reach. Origin blocklists provide friction,
not a security boundary. Non-Admin shell execution views mask private engine
state. Browser outputs are reported by their returned paths; no output is
published into a fork's tracked tree automatically.

## Verification

CI covers exact lock and drift rejection, launch decisions using synthetic
profile/process fixtures, arm refusal for shell credentials, proxy SSE/error/
timeout/identity/audit behavior, grant reversal and reseeding, adapters, GUI,
ports, inventory, and render-check. Those tests do not claim a real extension
connection, browser tab-group separation, or login behavior.

### Post-Sprint operator checklist

Run after Sprint completion with the real dedicated profile. This is not a CI
gate or a prerequisite for merging the implementation lane. Retain the
`sc browser doctor --json` receipt with the dated record below.

- [ ] Named-profile connection succeeds after the operator's approval click.
- [ ] Two shells appear as separately named tab groups.
- [ ] Disconnect one shell on the extension status page; the other still works.
- [ ] Close Chromium: the next attempted action fails within the request budget
      and does not launch Chromium or open Subfloor.
- [ ] Open only another profile: the next action fails without opening Subfloor.
- [ ] Audit/session paths contain only the intended task data and expected modes.

Verified on: **not yet run**. After the operator run, record the date, Chromium
and extension versions, doctor receipt location, each checkbox result, and any
limitations here. Do not replace this pending record with CI results.
