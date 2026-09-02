-- 0247 — restore the tailscale skill: re-upstream it into the global catalogue
-- and hand it back to the devops flavor pack.
--
-- 0241 tombstoned `tailscale` in the global catalogue simplification. It is
-- restored here: the asset lives again at assets/skills/tailscale/SKILL.md (the
-- regenerated 0001 seeds it for fresh builds and per-update syncs upsert it in
-- tracking forks); this forward reseed lands the row immediately for in-place
-- updates whose ledger already stamped 0001, and grants it where the devops
-- pack expects it. The tombstone registry no longer lists `tailscale`, so
-- runtime tombstone reconciliation stops deleting it.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'tailscale',
  'Reach the fork''s hosts over the tailnet — read tailnet status and exec commands on a tailnet host through the host-side ts-broker, holding no tailnet credential yourself. The devops shell''s signature skill. Use when operating remote hosts, deploys, or backups over Tailscale.',
  'substrate',
  NULL,
  0,
  '# tailscale — driving the tailnet

Operate remote hosts over Tailscale from a sandboxed shell: read tailnet
status, run commands on tailnet hosts. Opt-in + link-only — the operator
brings a host that is already `tailscale up`; you drive a scoped loop against
it. Grant is explicit, per-fork (`common=0`); the **devops** flavor''s
signature skill.

## Drive through the host broker — never `tailscale` directly

The sandbox can''t join the tailnet (no route, no TUN, no `NET_ADMIN`) and
must not hold a tailnet credential. NEVER run `tailscale` or bring up
`tailscaled` — call the host-side **ts-broker** over its unix socket in the
bind-mounted repo; the broker owns the host''s `tailscale up` node, and the
tailnet identity never enters the fork. (Detail:
`.super-coder/docs/tailscale-broker.md` — sibling of the Windows VM broker;
`windows_devkit` was retired from the global catalogue — same broker shape,
see `.super-coder/docs/windows-vm-broker.md`.)

```bash
SOCK="$(sc ts-broker-sock)"
curl -s --unix-socket "$SOCK" http://ts/health      # liveness check first
```

curl fails "not reachable" → broker down → ask the operator to run
`sc ts-broker-up` on the host. You cannot start it yourself (host process,
not sandbox).

## Precondition — the link is configured

Tailnet config = `ts` key in `.super-coder/instance.json`. Carries no secret
material — the host node''s identity is the credential and it stays host-side:

```json
"ts": { "ssh_user": "tester", "allowed_hosts": ["build-box","deploy-target"],
        "tailscale_bin": "tailscale" }
```

No `ts` block → no tailnet linked: stop + ask the operator to set it
(hand-edit or `PUT /api/ts`). `allowed_hosts` = fail-closed allow-list:
`exec` only reaches hosts listed there; empty/absent list denies all. Declare
only the hosts the fork operates — widen deliberately, never by default.

## The verbs

| Verb | Call |
|---|---|
| **status** | `curl -s --unix-socket "$SOCK" http://ts/status` → `{backend, self, peers[]}` from the host node''s view |
| **exec** | `curl -s --unix-socket "$SOCK" http://ts/exec -d ''{"host":"build-box","command":"uptime"}''` → `{ok, exit, stdout, stderr}` |

The broker runs `tailscale ssh <ssh_user>@<host>` non-interactively (tailnet
ACLs govern auth — no key, no prompt). You name a host + a command; the host
must be in `allowed_hosts`.

`status` before `exec`: peers carry hostname, MagicDNS name, Tailscale IP,
and online state — confirm the target is online first; a timeout on a down
host wastes a 2-minute exec window.

## Mullvad ↔ Tailscale on Linux

Tailscale and the Mullvad app fight over the default route + nftables on
Linux; running both drops tailnet traffic. Sequential-use only: bring one
down before the other comes up. `exec`/`status` suddenly fail on a host that
worked → check whether Mullvad came up on the HOST (the broker''s node), not
in your sandbox — host-side network state, not a broker bug; surface it to
the operator.

## Lanes

Operating hosts / deploys / backups = yours (devops). App features = dev''s;
the super-coder engine = admin''s.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT OR IGNORE INTO flavor_skills (flavor, skill_id)
SELECT 'devops', skill_id FROM skills WHERE name='tailscale' AND is_deleted=0;

COMMIT;
