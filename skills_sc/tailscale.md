---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# tailscale

Reach the fork's hosts over the tailnet — read tailnet status and exec commands on a tailnet host through the host-side ts-broker, holding no tailnet credential yourself. The devops shell's signature skill. Use when operating remote hosts, deploys, or backups over Tailscale.

**Category:** substrate

---

# tailscale — driving the tailnet

How a sandboxed shell operates remote hosts over Tailscale: read tailnet status,
run commands on a tailnet host. This is **opt-in and link-only** — the operator
brings a host that is already `tailscale up`; you drive a scoped loop against the
tailnet. Grant is explicit, per-fork (`common=0`); it is the **devops** flavor's
signature skill. You have it because someone granted it to your shell.

## You drive the tailnet through the host broker — not tailscale directly

You run inside the sandbox container. The container can't join the tailnet (no
route, no TUN, no `NET_ADMIN`) and **must not** hold a tailnet credential. So you
do **not** run `tailscale` — you call the **host-side ts-broker** over a unix
socket in the bind-mounted repo. The broker owns the host's `tailscale up` node
and does the work; the tailnet identity never enters the fork. (See
`.super-coder/docs/tailscale-broker.md`. It is the sibling of the Windows VM broker —
`windows_devkit` works the exact same way.)

The socket path comes from `sc ts-broker-sock`. Every verb is a `curl`:

```bash
SOCK="$(sc ts-broker-sock)"
curl -s --unix-socket "$SOCK" http://ts/health      # liveness check first
```

If the curl fails with "not reachable", the broker isn't running — ask the
operator to start it on the host: `sc ts-broker-up`. You cannot start it
yourself (it runs on the host, not in your sandbox).

## Precondition — the link is configured

The tailnet lives in `.super-coder/instance.json` under the `ts` key. It carries
**no secret material** — the host node's identity is the credential and it stays
host-side:

```json
"ts": { "ssh_user": "tester", "allowed_hosts": ["build-box","deploy-target"],
        "tailscale_bin": "tailscale" }
```

No `ts` block → no tailnet linked: stop and ask the operator to set it (hand-edit,
or `PUT /api/ts`). `allowed_hosts` is a **fail-closed allow-list** — you may only
`exec` against hosts listed there; an empty/absent list denies all. Declare the
hosts the fork needs; a wider list is a wider blast radius.

## The verbs

| Verb | Call |
|---|---|
| **status** | `curl -s --unix-socket "$SOCK" http://ts/status` → `{backend, self, peers[]}` from the host node's view |
| **exec** | `curl -s --unix-socket "$SOCK" http://ts/exec -d '{"host":"build-box","command":"uptime"}'` → `{ok, exit, stdout, stderr}` |

The broker runs `tailscale ssh <ssh_user>@<host>` non-interactively (tailnet ACLs
govern auth — no key, no prompt). You name a host + a command; you never hold a
key, and the host must be in `allowed_hosts`.

`status` returns each peer's hostname, MagicDNS name, Tailscale IP, and online
state — use it to confirm a target is reachable before you `exec`.

## Mullvad ↔ Tailscale on Linux — the gotcha

Tailscale and the Mullvad app fight over the routing/firewall on Linux: both want
the default route + nftables, and running them together drops tailnet traffic.
They are **sequential-use** — bring one down before the other comes up. If
`exec`/`status` suddenly fail on a host that worked, check whether Mullvad came up
on the **host** (the broker's node), not in your sandbox. This is a host-side
network-state problem, not a broker bug; surface it to the operator.

## Stance

- **Drive, don't join.** You operate the tailnet through the broker; you never
  bring up `tailscaled` or hold an auth key. Link-only stays link-only — node
  provisioning (`tailscale up`) is the operator's, host-side, once.
- **Declare your hosts.** `allowed_hosts` is the blast radius. Keep it to what the
  fork actually operates; widen it deliberately, not by default.
- **Status before exec.** Confirm the peer is online before you run against it —
  a timeout on a down host wastes a 2-minute exec window.
- **Lanes.** Operating hosts/deploys/backups is yours (devops). App features are
  dev's; the super-coder engine is admin's.
