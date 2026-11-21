# Subfloor Manager

From a primary checkout on the Linux host, enable the optional service for the current user. The manager discovers that user's primary checkouts directly under the chosen directory. The default is `~/Repos`.

```sh
./sc manager install --root "$HOME/Repos" --port 18798
./sc manager url
./sc manager password
```

Use the hidden prompts to set the password, then open the printed loopback URL and sign in as `operator`. To reset the password, run `./sc manager password` again in the host terminal. A working systemd user manager and Python 3.14 are required. The manager serves its own copy of the application while individual repos are stopped. Running setup from a second checkout returns the existing URL and keeps the original root, port and password.

For fresh setup, `./sc install --manager` enables the manager after the repo installation completes, using that repo's parent as the discovery root. Omitting the option leaves the manager alone. If enabling the manager fails, correct the reported error and retry `./sc manager install` from the installed checkout.

## Inventory

Each row shows the canonical checkout path, runtime, supervisor observation, GUI health and publication state. After a runtime switch, the row shows the backend still running and the configured backend for the next launch. If both backends are active, the runtime is ambiguous. Open becomes available when the destination identifies the checkout at `/api/health` and serves an HTML page at `/`. A local browser can use a verified loopback GUI without Tailscale. From a remote browser, Open uses the verified private URL.

The manager reads the publisher's `~/.local/state/subfloor-tailnet/urls.json`. Supply `--urls /path/to/urls.json` at initial manager setup for a different listing. If the list disappears or a published URL stops answering, the repo still appears. An inactive or absent supervisor and a refused local connection identify a stopped service. An inaccessible supervisor leaves service state unknown.

## Token Analytics

When the installed payload includes Token Analytics, open it from the manager navigation. It uses the same sign-in and HTTPS bookmark as the inventory. The manager collects from local primary checkouts, including stopped installations, and stores aggregate history in its private state directory. Refresh on the analytics page requests a new collection.

## Private access

On Halo, leave the existing instance publisher running. Choose an unused private HTTPS port allowed for the operator's tailnet client. From the host terminal, inspect the current routes, add the dedicated manager mapping, then compare the routes. For example, if 18798 is available in Serve:

```sh
tailscale serve status
./sc manager publish --https-port 18798
tailscale serve status
./sc manager url
```

The publisher's gateway continues to own each instance mapping. The manager adds one direct mapping to its loopback listener. An occupied port, including a Funnel mapping, is refused. A private route also requires the operator password. From Cachy, open the printed HTTPS URL, sign in and follow Open on a healthy repo. Check the existing default URL and per-repo mappings against the pre-install listing.

## Update and recovery

After updating the checkout through the normal Subfloor update path, explicitly deploy its manager payload. Configuration and the manager password persist across updates.

```sh
./sc manager update
systemctl --user status subfloor-manager.service
journalctl --user -u subfloor-manager.service -n 50
```

For a failed service start, inspect the journal and loopback port, then retry setup. To recover an earlier manager revision, run `./sc manager update` from a primary checkout carrying that known-good payload. To take down the manager bookmark, run `./sc manager unpublish`. If the recorded route was repurposed, inspect `tailscale serve status --json` and resolve its ownership before removing it.

To stop and uninstall the manager, run `./sc manager remove` from a primary checkout. This removes its mapping, service unit, installed application and sign-in configuration. Manager history is retained in the reported state directory. The managed Git repos, their Subfloor installations and their publisher continue to operate independently.
