-- 0039 — reseed tailscale + windows_devkit: broker doc pointers that resolve in a fork
--
-- Upstream #239: both skills said "See docs/<broker>.md" — a path that exists
-- only in the super-coder repo. Forks have no docs/ dir, so the deep-dive
-- reference for both broker systems was unreachable from exactly the
-- environment the skills run in. The docs now materialize with the engine at
-- .super-coder/docs/ (added to ENGINE_PATHS); this reseed repoints the two
-- skills at the path that ships.
--
-- 0001 is regenerated from the assets for fresh builds; this forward reseed
-- carries the same bodies to already-installed forks (UPSERT by name; skill_id
-- + grants preserved).

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'tailscale',
  'Reach the fork''s hosts over the tailnet — read tailnet status and exec commands on a tailnet host through the host-side ts-broker, holding no tailnet credential yourself. The devops shell''s signature skill. Use when operating remote hosts, deploys, or backups over Tailscale.',
  'substrate',
  NULL,
  0,
  '# tailscale — driving the tailnet

How a sandboxed shell operates remote hosts over Tailscale: read tailnet status,
run commands on a tailnet host. This is **opt-in and link-only** — the operator
brings a host that is already `tailscale up`; you drive a scoped loop against the
tailnet. Grant is explicit, per-fork (`common=0`); it is the **devops** flavor''s
signature skill. You have it because someone granted it to your shell.

## You drive the tailnet through the host broker — not tailscale directly

You run inside the sandbox container. The container can''t join the tailnet (no
route, no TUN, no `NET_ADMIN`) and **must not** hold a tailnet credential. So you
do **not** run `tailscale` — you call the **host-side ts-broker** over a unix
socket in the bind-mounted repo. The broker owns the host''s `tailscale up` node
and does the work; the tailnet identity never enters the fork. (See
`.super-coder/docs/tailscale-broker.md`. It is the sibling of the Windows VM broker —
`windows_devkit` works the exact same way.)

The socket path comes from `sc ts-broker-sock`. Every verb is a `curl`:

```bash
SOCK="$(sc ts-broker-sock)"
curl -s --unix-socket "$SOCK" http://ts/health      # liveness check first
```

If the curl fails with "not reachable", the broker isn''t running — ask the
operator to start it on the host: `sc ts-broker-up`. You cannot start it
yourself (it runs on the host, not in your sandbox).

## Precondition — the link is configured

The tailnet lives in `.super-coder/instance.json` under the `ts` key. It carries
**no secret material** — the host node''s identity is the credential and it stays
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
| **status** | `curl -s --unix-socket "$SOCK" http://ts/status` → `{backend, self, peers[]}` from the host node''s view |
| **exec** | `curl -s --unix-socket "$SOCK" http://ts/exec -d ''{"host":"build-box","command":"uptime"}''` → `{ok, exit, stdout, stderr}` |

The broker runs `tailscale ssh <ssh_user>@<host>` non-interactively (tailnet ACLs
govern auth — no key, no prompt). You name a host + a command; you never hold a
key, and the host must be in `allowed_hosts`.

`status` returns each peer''s hostname, MagicDNS name, Tailscale IP, and online
state — use it to confirm a target is reachable before you `exec`.

## Mullvad ↔ Tailscale on Linux — the gotcha

Tailscale and the Mullvad app fight over the routing/firewall on Linux: both want
the default route + nftables, and running them together drops tailnet traffic.
They are **sequential-use** — bring one down before the other comes up. If
`exec`/`status` suddenly fail on a host that worked, check whether Mullvad came up
on the **host** (the broker''s node), not in your sandbox. This is a host-side
network-state problem, not a broker bug; surface it to the operator.

## Stance

- **Drive, don''t join.** You operate the tailnet through the broker; you never
  bring up `tailscaled` or hold an auth key. Link-only stays link-only — node
  provisioning (`tailscale up`) is the operator''s, host-side, once.
- **Declare your hosts.** `allowed_hosts` is the blast radius. Keep it to what the
  fork actually operates; widen it deliberately, not by default.
- **Status before exec.** Confirm the peer is online before you run against it —
  a timeout on a down host wastes a 2-minute exec window.
- **Lanes.** Operating hosts/deploys/backups is yours (devops). App features are
  dev''s; the super-coder engine is admin''s.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'windows_devkit',
  'Drive the linked Windows Test VM — push a build artifact, exec the installer/test over SSH, capture output + a screenshot, then reset to the clean snapshot. High-fidelity installer/system-level testing where Wine is useless. Use when building or verifying Windows software in a fork that has a configured VM.',
  'substrate',
  NULL,
  0,
  '# windows_devkit — driving the Windows Test VM

Real Windows, for the testing Wine can''t fake: MSI installers, services, the
registry, system-level behavior. This is **opt-in and link-only** — the operator
runs the VM; you drive a verified loop against it. Devs build and test; the
reviewer independently verifies a candidate build with **exec → capture →
reset**. Grant is explicit, per-fork (`common=0`); you have it because someone
granted it to your shell.

## Precondition — the link is configured

The VM lives in `.super-coder/instance.json` under the `vm` key (set via the GUI
Scripts → **Windows Test VM** wizard, which live-tests every field before save):

```json
"vm": { "domain": "win-test", "ssh_host": "127.0.0.1", "ssh_port": 22,
        "ssh_user": "tester", "ssh_key_path": "~/.ssh/sc_win_test",
        "transfer_dir": "/var/sc/win-xfer", "snapshot": "clean",
        "libvirt_uri": "qemu:///system" }
```

`libvirt_uri` is optional — set it when the domain is system-scope (the default
`qemu:///session` can''t see it); omit it otherwise.

No `vm` block → no VM linked: stop and ask the operator to run the wizard. The
admin `configure_winbox` skill must also have run, or the box has no toolchain
(the `toolchain` check in the wizard is how you confirm it did).

`ssh_key_path` is a **path**, never key material — and your shell never reads it
anyway: the key lives host-side with the broker (below). You never hold it.

## You drive the VM through the host broker — not ssh/virsh directly

You run inside the sandbox container. The VM lives on the host''s libvirt NAT,
unreachable from here, and the container has no `ssh`, no `virsh`, and no key.
So you do **not** shell out — you call the **host-side vm-broker** over a unix
socket in the bind-mounted repo. The broker holds the key + libvirt and does the
work; nothing about your isolation changes. (See `.super-coder/docs/windows-vm-broker.md`.)

The socket path comes from `sc vm-broker-sock`. Every verb is a `curl`:

```bash
SOCK="$(sc vm-broker-sock)"
curl -s --unix-socket "$SOCK" http://vm/health      # liveness check first
```

If the curl fails with "not reachable", the broker isn''t running — ask the
operator to start it on the host: `sc vm-broker-up`. You cannot start it
yourself (it must run on the host, not in your sandbox).

## The loop — push → exec → capture → reset

Every run starts from the clean snapshot, so installer side-effects never leak
between runs. That property is the whole point — without the reset, system-level
testing isn''t trustworthy.

| Verb | Call |
|---|---|
| **push** | `curl -s --unix-socket "$SOCK" http://vm/push -d ''{"src":"<repo path to artifact>"}''` — stages it into `transfer_dir` (the guest''s share) |
| **exec** | `curl -s --unix-socket "$SOCK" http://vm/exec -d ''{"command":"<installer / test cmd>"}''` → `{ok, exit, stdout, stderr}` |
| **capture** | `curl -s --unix-socket "$SOCK" http://vm/capture -d ''{"command":"<optional cmd>"}''` → stdout + a base64 `virsh screenshot` for GUI state |
| **reset** (start) | `curl -s --unix-socket "$SOCK" http://vm/reset -X POST` — revert to clean and **boot** it; begin a run from a clean box |
| **reset** (done) | `curl -s --unix-socket "$SOCK" http://vm/reset -d ''{"running":false}''` — revert to clean and leave it **powered OFF** |

The broker runs ssh non-interactively and uses the saved `vm` block — you name a
command, never a host or a key.

**Bracket every run with reset.** Start with `/reset` (boots a clean box); when
you''re done — even on failure — end with `/reset {"running":false}` so the box
returns to clean **and powers off**. The VM is large (~12 GB RAM); leaving it
running idles that on the host. Powering off at the end frees it, and the next
run''s start-reset boots a fresh clean box anyway.

> [!class4]
> **Why a flag, not two verbs.** The clean snapshot is OFFLINE (this CPU''s
> non-migratable `invtsc` flag refuses a live snapshot). So a bare revert already
> lands powered-off; `--running` (the default, `{"running":true}`) boots it.
> Ending with `{"running":false}` is therefore clean **and** off in a single op —
> no wasteful boot-just-to-shut-down.

## Stance

- **Reset is not optional.** Test from clean, return to clean — and powered off
  when you''re done (`/reset {"running":false}`). A dirty box makes the next run''s
  result a lie; an idle running box wastes ~12 GB of the host''s RAM.
- **You drive, you don''t provision.** Missing toolchain → that''s the admin''s
  `configure_winbox` + re-snapshot, not an install from here. Never `winget
  install` from this loop; it would poison the clean snapshot.
- **The reviewer verifies, doesn''t build.** Reviewer uses exec → capture → reset
  on the dev''s candidate artifact to confirm the claim independently.
- **Link-only stays link-only.** If a field is wrong, fix it in the wizard (it
  re-validates); don''t hand-edit secrets into config.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
