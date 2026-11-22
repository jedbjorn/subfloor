# Subfloor Manager

The optional manager discovers one host user's primary Subfloor checkouts directly under a chosen directory. It provides verified inventory links, repository lifecycle controls, local installation and guarded removal, and cross-project Token Analytics. Its service and installed payload are independent of every managed checkout.

## Install or enable

Requirements are Linux, Python 3.14, and a working systemd user manager. The default discovery root is `~/Repos`; the default loopback port is 18798. A second checkout does not create a second manager.

For fresh Subfloor setup, opt in only when wanted:

```sh
./sc install --manager
```

Omitting `--manager` leaves ordinary repo installation and lifecycle unchanged. To enable later from an updated existing primary checkout:

```sh
./sc manager install --root "$HOME/Repos" --port 18798
./sc manager password
./sc manager url
systemctl --user status subfloor-manager.service
```

Use the hidden prompts to set the password, then open the printed loopback URL and pair the browser as `operator`. Pairing uses the same long-lived session model as Subfloor's GUI: each browser receives an independent HttpOnly, SameSite=Strict cookie carrying a random token, and the Manager stores only its SHA-256 hash. Cookies are renewed with use for as long as the browser allows (about 400 days), including across Manager updates and restarts. Published HTTPS sessions also mark the cookie `Secure`. The password is needed only to pair or re-pair a browser.

The service serves its own copy of the application while individual repos are stopped. Repeating `./sc manager install` from another primary checkout reuses the existing root, port, password, paired browsers, and URL. Use **Paired browsers** in either page to inspect devices, revoke one without affecting the others, or sign out the current browser. On the host, `./sc manager sessions` lists devices and `./sc manager revoke <id>` revokes one (`--all` revokes every browser). Resetting the credential with `./sc manager password` also revokes every paired browser. Explicit Basic authorization remains available for command-line API clients.

A different publisher listing can be selected only at initial setup with `./sc manager install --urls /path/to/urls.json`. Local loopback access remains available without Tailscale.

## Inventory and repository controls

Each row shows the canonical checkout path, runtime, supervisor observation, GUI health, and publication state. After a runtime switch, the row shows both the backend still running and the configured backend for the next launch. If evidence is ambiguous, runtime is unknown. The repository name links to its GUI only when the destination identifies the selected checkout at `/api/health` and serves HTML directly or through its same-origin `/signin.html` flow. Arbitrary redirects are not accepted. Loopback and published authorities are checked independently: a local browser's project name uses the verified loopback GUI; a remote browser's uses the verified private URL. GUI health remains a status indicator rather than a separate link.

The manager reads `~/.local/state/subfloor-tailnet/urls.json` by default. A missing listing or failed publication does not remove an installation. An inactive supervisor and refused local connection identify a stopped service even if its published URL returns 502. An inaccessible supervisor leaves service state unknown.

Use the selected row's accessible actions menu for **Start**, **Stop**, **Restart**, **Update Subfloor**, or **Remove Subfloor from this repo**. Every operation requires a confirmation preview. Stop, restart, and updates of running repositories warn that active sessions will be interrupted. Update invokes the checkout's own `./sc update`, then verifies that a previously running repository is healthy and running or a previously stopped repository remains stopped. Operations are serialized per checkout and the page refreshes observed state after completion. Update output and engine-reported backup, rollback, and recovery guidance remain available under **Repository operations**.

Use **Add repository** to install into an existing local Git checkout or create a new local repository. Choose Host or Docker runtime and enter the Subfloor operator username. The preview shows the resolved target, engine source, stage, bootstrap files, and commit before Apply. Existing history, staged work, unstaged work, and project files are preserved. A retry resumes the recorded stage; an existing installation is reopened rather than installed again.

Removal first runs the target checkout's own `./sc remove --dry-run`. Cancel is side-effect free. To proceed, inspect the actual plan and recovery information, then type the displayed `REMOVE <name>` phrase exactly. Apply revalidates the checkout and plan before invoking the engine's supported removal command. The engine owns quiescence, backup, preflight, and interruption handling. The operation history retains command output and reported recovery locations even if a failed target disappears from inventory. Never repeat a destructive Apply blindly; inspect the result and make a fresh preview from observed state.

## Token Analytics

Open **Token Analytics** from the manager navigation. It uses the same paired-browser session and HTTPS bookmark as inventory. The collector reads local primary checkouts, including stopped installations, without SSH and stores aggregate history in manager-owned private state. Collected history remains visible while an instance is stopped.

The combined and per-project views include session project labels, usage filters and charts, and engine-update markers with revision details. **Refresh** requests immediate collection. Automatic collection runs every six hours and retries after fifteen minutes following failure. A failed project keeps its last good rows and shows its collection error; healthy projects can still advance. A collection that completes with warnings reports its actual collection time and warning state when cached token rows exist. Legacy databases without activity tables produce a readable warning, and Shell Hours reports unavailable hourly rates rather than zero-hour activity when no intervals are recorded.

## Private tailnet access

Leave the existing instance publisher running. Choose an unused private HTTPS port allowed for the operator's tailnet client. The manager adds one direct Serve mapping to its loopback listener; the publisher continues to own the default route and each instance mapping. An occupied port, including a Funnel mapping, is refused.

For example, if 18798 is available:

```sh
tailscale serve status
./sc manager publish --https-port 18798
tailscale serve status
./sc manager url
```

Open the printed private URL from the tailnet client, pair the browser as `operator`, and follow a healthy repository name to its GUI. Check the existing default URL and per-repo mappings against the pre-install listing. To take down only the manager bookmark:

```sh
./sc manager unpublish
```

If the recorded route was repurposed, inspect `tailscale serve status --json` and resolve its ownership before removing anything.

## Update, recovery, and manager removal

After updating a checkout through the normal Subfloor update path, explicitly deploy its manager payload. Configuration, analytics history, the manager password, and paired-browser sessions persist across updates.

```sh
./sc manager update
systemctl --user status subfloor-manager.service
journalctl --user -u subfloor-manager.service -n 50
curl --fail --silent --show-error http://127.0.0.1:18798/health
```

For a failed service start, inspect the journal and loopback port, correct the reported error, then retry `./sc manager update`. To recover an earlier manager revision, run `./sc manager update` from a primary checkout carrying that known-good payload. Repository operation failures retain their output and engine-reported backup or manifest paths under **Repository operations**; use those actual paths for recovery rather than assuming a backup location.

To stop and uninstall the manager itself:

```sh
./sc manager remove
```

This removes its exact Serve mapping, service unit, installed application, and sign-in configuration, and revokes every paired browser. It prints the retained manager-history location. Managed Git repos, their Subfloor installations, their data, and the instance publisher remain independent.

## Halo acceptance handoff — pending Jed after Sprint 38

**Live Halo verification is pending. Jed owns these steps after the Sprint. The Sprint implementation did not deploy, publish, change routing, or remove anything on Halo.** Use the primary checkout carrying the merged manager revision. Existing Halo installations are never removal targets.

### 1. Install and inspect

```sh
cd "$HOME/Repos/subfloor"
git rev-parse HEAD
./sc update
./sc manager install --root "$HOME/Repos" --port 18798
./sc manager password
./sc manager url
systemctl --user status subfloor-manager.service
curl --fail --silent --show-error http://127.0.0.1:18798/health
```

Open `http://127.0.0.1:18798/` on Halo or use the private URL below. Confirm that inventory shows each installed primary checkout once, with independently observed runtime, service, GUI health, and publication badges. Open Token Analytics, refresh, switch projects, and compare one common time window and its revision markers with the independent Cachy reference view. Confirm that any collection error retains the affected project's last good rows.

### 2. Publish privately and verify existing routes

Record the existing routes before adding the manager mapping:

```sh
tailscale serve status
tailscale serve status --json > /tmp/subfloor-serve-before-manager.json
./sc manager publish --https-port 18798
tailscale serve status
tailscale serve status --json > /tmp/subfloor-serve-after-manager.json
./sc manager url
```

Derive the printed private authority for command-line verification:

```sh
TAILNET_NAME="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].lower().rstrip("."))')"
MANAGER_URL="https://${TAILNET_NAME}:18798"
curl --fail --silent --show-error --user operator "$MANAGER_URL/api/inventory" | python3 -m json.tool
```

`curl` prompts for the manager password. From Cachy, open `$MANAGER_URL`, pair the browser, follow an existing healthy repository name, and use Token Analytics. Compare the two Serve status listings: the only new mapping should be the dedicated manager route. Open the default dos-arch URL and existing per-instance URLs and confirm their destinations are unchanged. An inability to reach the manager from Cachy leaves published-access acceptance pending.

### 3. Use a sacrificial repo for installation and removal

Choose a new name and create a disposable existing Git checkout with a project file. Do not use any existing Halo installation:

```sh
SACRIFICIAL="$HOME/Repos/manager-removal-check"
test ! -e "$SACRIFICIAL"
mkdir "$SACRIFICIAL"
git -C "$SACRIFICIAL" init -b main
printf 'preserve this project file\n' > "$SACRIFICIAL/project.txt"
git -C "$SACRIFICIAL" add project.txt
git -C "$SACRIFICIAL" -c user.name=Jed -c user.email=jed@localhost commit -m 'Sacrificial project history'
```

In the manager, choose **Add repository → Existing Git checkout**, name `manager-removal-check`, Host runtime, and Jed's operator username. Inspect the target, files, and bootstrap commit in the preview, then Confirm. Wait for the operation result and a healthy inventory row, then follow its repository name to the correct GUI. Allow for the publisher's discovery interval; installation, launch, and publication are separate observations.

In that new row, choose **Remove Subfloor from this repo**. Inspect the real dry-run plan, canonical target, and recovery information, then Cancel and confirm that nothing changed. Preview again, type `REMOVE manager-removal-check`, and Apply once. Record the engine result and the actual backup or manifest location from **Repository operations → View output**. Then verify the project, Git history, manager, and neighboring installs:

```sh
test -f "$SACRIFICIAL/project.txt"
git -C "$SACRIFICIAL" log --oneline
test ! -e "$SACRIFICIAL/.super-coder"
curl --fail --silent --show-error --user operator "$MANAGER_URL/api/inventory" | python3 -m json.tool
```

Confirm the sacrificial row is gone, neighboring rows remain, and the manager is still reachable. Inspect the printed backup or manifest path itself. If removal is refused, interrupted, or partial, retain the operation output and recovery path, inspect the observed state, and require a fresh preview before any further action. Do not silently retry it.

### 4. Recovery checks and remaining live result

If the private bookmark must be withdrawn, run `./sc manager unpublish` and confirm that only the manager mapping disappears. If the manager payload must be replaced, run `./sc manager update` from the selected known-good checkout and repeat the local health and authenticated inventory checks.

Jed records the final live Halo result after completing these checks, including the sacrificial-repo removal. Until then, install, analytics parity against live data, inventory comparison, published access, and disposable removal on Halo remain **pending**.
