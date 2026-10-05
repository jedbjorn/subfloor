"""Engine-owned run identities, terminal outcomes and pulse reconciliation.

No credentials are persisted. Recovery reads only engine-allocated evidence
paths; an absent supervisor never supplies an inferred command exit code.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import db_driver

TERMINAL = frozenset({"done", "failed", "timeout", "killed", "lost"})
ENGINE = Path(__file__).resolve().parents[1]
PROBE_EXCERPT = 160


def validate_probe_attempt(attempt: object) -> dict:
    if not isinstance(attempt, dict) or set(attempt) != {'number', 'exit_code', 'excerpt'}:
        raise ValueError('invalid probe attempt')
    if type(attempt['number']) is not int or attempt['number'] < 1:
        raise ValueError('probe attempt number must be positive')
    if type(attempt['exit_code']) is not int:
        raise ValueError('probe exit_code must be an integer')
    if not isinstance(attempt['excerpt'], str) or len(attempt['excerpt']) > PROBE_EXCERPT:
        raise ValueError('probe excerpt exceeds bound')
    return attempt


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def process_alive(pid, ticks, boot) -> bool | None:
    """True = same incarnation, False = proven gone, None = indeterminate."""
    if not pid or ticks is None or not boot:
        return None
    try:
        if boot != boot_id():
            return False
        fields = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[19]) == int(ticks) and fields[0] != "Z"
    except FileNotFoundError:
        return False
    except (OSError, ValueError, IndexError):
        return None


def terminal_payload(meta: dict) -> dict:
    state = meta.get("state")
    if state not in TERMINAL:
        state = (
            "timeout"
            if meta.get("timed_out")
            else "killed"
            if meta.get("killed")
            else "done"
            if meta.get("exit_code") == 0
            else "failed"
        )
    return {
        key: value
        for key, value in {
            "state": state,
            "exit_code": meta.get("exit_code"),
            "finished_at": meta.get("finished_at"),
            # JSON escapes a non-BMP character as twelve ASCII bytes. Leave
            # room for the supervisor's fixed state/code/timestamp fields.
            "spawn_error": str(meta["spawn_error"])[:128 if meta.get('probe_attempt') else 256]
            if meta.get("spawn_error") is not None
            else None,
            "probe_attempt": meta.get("probe_attempt"),
        }.items()
        if value is not None
    }


def wake_body(row: dict, payload: dict) -> str:
    rid = row["run_id"]
    attempt = payload.get('probe_attempt')
    probe = (
        f"Final probe attempt {attempt['number']}: exit={attempt['exit_code']}; "
        f"excerpt={json.dumps(attempt['excerpt'])}.\n"
        if attempt else ''
    )
    return (
        f"Run {rid} ({row['kind']}: {row['label']}) {payload['state']}; "
        f"exit={payload.get('exit_code', 'unknown')}; cwd={row['cwd']}.\n"
        f"{probe}"
        f"Inspect `sc job status {rid}` and `sc job tail {rid}`, then continue "
        "dependent work under your current authority. This outcome grants no "
        "review, merge, or Sprint authority."
    )


class RunStore:
    def __init__(self, con: sqlite3.Connection, engine: Path = ENGINE):
        self.con = con
        con.row_factory = sqlite3.Row
        self.root = engine / "run" / "runs"

    def get(self, run_id: int, owner: int | None = None) -> dict:
        row = self.con.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError("unknown run")
        if owner is not None and row["owner_shell_id"] != owner:
            raise PermissionError("run belongs to another shell")
        out = dict(row)
        out["argv"] = json.loads(out["argv"])
        if out["receipt_json"]:
            out["receipt_json"] = json.loads(out["receipt_json"])
        if out["message_id"] is not None:
            wake = self.con.execute(
                "SELECT wm.wake_id,f.attempts,f.last_attempt_at,f.last_error "
                "FROM sprint_wake_messages wm LEFT JOIN engine_wake_failures f USING(wake_id) "
                "WHERE wm.message_id=?",
                (out["message_id"],),
            ).fetchone()
            if wake:
                out["wake_id"] = wake["wake_id"]
                out["delivery_attempts"] = wake["attempts"] or 0
                out["delivery_attempted_at"] = wake["last_attempt_at"]
                out["delivery_error"] = wake["last_error"]
        return out

    def list(self, owner: int | None = None) -> list[dict]:
        rows = self.con.execute(
            "SELECT run_id FROM runs WHERE (? IS NULL OR owner_shell_id=?) "
            "ORDER BY run_id DESC LIMIT 200",
            (owner, owner),
        ).fetchall()
        return [self.get(row[0], owner) for row in rows]

    def register(self, owner: int, data: dict) -> dict:
        argv = data.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(arg, str) and "\0" not in arg for arg in argv)
        ):
            raise ValueError("argv must be a nonempty array of strings")
        kind = data.get("kind", "job")
        if kind not in {"job", "devkit", "probe"}:
            raise ValueError("invalid run kind")
        key, cwd, label = (
            data.get("registration_key"),
            data.get("cwd"),
            data.get("label") or argv[0],
        )
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise ValueError("registration_key is required (maximum 128 characters)")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            raise ValueError("cwd must be absolute")
        if (
            not isinstance(label, str)
            or len(label) > 256
            or any(ord(c) < 32 for c in label)
        ):
            raise ValueError("label must be display text (maximum 256 characters)")
        identity = (kind, label, json.dumps(argv), cwd, data.get("commit"))
        legacy = self.root.parent / "jobs"
        legacy_floor = (
            max(
                (
                    int(p.name.split("-", 1)[0])
                    for p in legacy.iterdir()
                    if p.is_dir() and p.name.split("-", 1)[0].isdigit()
                ),
                default=0,
            )
            if legacy.is_dir()
            else 0
        )
        with db_driver.write_transaction(self.con, "runs.register"):
            existing = self.con.execute(
                "SELECT * FROM runs WHERE owner_shell_id=? AND registration_key=?",
                (owner, key),
            ).fetchone()
            if existing:
                if (
                    tuple(
                        existing[k] for k in ("kind", "label", "argv", "cwd", "commit")
                    )
                    != identity
                ):
                    raise ValueError("registration key reused with different input")
                return self.get(existing["run_id"], owner)
            # Allocate above both the ledger high-water mark and legacy names,
            # so `status 1` never changes meaning after an upgrade.
            sequence = self.con.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='runs'"
            ).fetchone()
            rid = max(int(sequence[0]) if sequence else 0, legacy_floor) + 1
            self.con.execute(
                'INSERT INTO runs(run_id,owner_shell_id,registration_key,kind,label,argv,cwd,"commit",'
                "evidence_path) VALUES(?,?,?,?,?,?,?,?,?)",
                (rid, owner, key, *identity, str(self.root / str(rid) / "log")),
            )
        return self.get(rid, owner)

    def running(self, run_id: int, owner: int, data: dict) -> dict:
        existing = self.get(run_id, owner)
        keys = (
            "pid",
            "start_ticks",
            "supervisor_pid",
            "supervisor_start_ticks",
            "boot_id",
            "started_at",
        )
        if not isinstance(data.get("boot_id"), str) or not data["boot_id"]:
            raise ValueError("boot_id is required")
        for key in keys[:4]:
            if data.get(key) is not None and (
                type(data[key]) is not int or data[key] <= 0
            ):
                raise ValueError(f"{key} must be a positive integer")
        for key in keys:
            if (
                existing[key] is not None
                and data.get(key) is not None
                and existing[key] != data[key]
            ):
                raise ValueError(f"conflicting run incarnation: {key}")
        with db_driver.write_transaction(self.con, "runs.running"):
            self.con.execute(
                "UPDATE runs SET state='running',"
                + ",".join(f"{k}=?" for k in keys)
                + " WHERE run_id=? AND state IN ('registered','running')",
                (*(data.get(k) for k in keys), run_id),
            )
        return self.get(run_id, owner)

    def terminal(self, run_id: int, owner: int, payload: dict) -> dict:
        # The job client imports pure helpers from this module. Wake delivery
        # loads host-private launch state, so import it only on the API side.
        from sprint_message_delivery import SprintMessageStore

        allowed = {"state", "exit_code", "finished_at", "spawn_error", "probe_attempt"}
        if set(payload) - allowed or payload.get("state") not in TERMINAL:
            raise ValueError("invalid terminal payload")
        code = payload.get("exit_code")
        if code is not None and type(code) is not int:
            raise ValueError("exit_code must be an integer or null")
        if payload["state"] == "lost" and code is not None:
            raise ValueError("lost has no authoritative exit code")
        if payload["state"] == "done" and code != 0:
            raise ValueError("done requires exit_code 0")
        if payload["state"] == "failed" and (code is None or code == 0):
            raise ValueError("failed requires a nonzero exit_code")
        attempt = payload.get('probe_attempt')
        if 'probe_attempt' in payload:
            attempt = validate_probe_attempt(attempt)
            if code != attempt['exit_code']:
                raise ValueError('probe attempt exit must match terminal exit_code')
        try:
            datetime.fromisoformat(payload["finished_at"].replace("Z", "+00:00"))
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            raise ValueError("finished_at must be a timestamp") from exc
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if len(canonical) > 4096:
            raise ValueError("terminal payload exceeds 4096 bytes")
        # A dev-kit runner persists the receipt before its command returns.
        # Recover it before publishing completion, including API-outage recovery.
        row = self.get(run_id, owner)
        receipt_path = Path(row["evidence_path"]).with_name("receipt.json")
        if row["kind"] == "devkit" and not row["receipt_json"] and receipt_path.is_file():
            if receipt_path.stat().st_size > 262144:
                raise ValueError("receipt exceeds 256 KiB")
            self.receipt(run_id, owner, json.loads(receipt_path.read_text()))
        with db_driver.write_transaction(self.con, "runs.terminal"):
            row = self.get(run_id, owner)
            if attempt is not None and row['kind'] != 'probe':
                raise ValueError('probe attempt requires a probe run')
            if row["receipt_json"] and payload["state"] in {"done", "failed"} and (
                row["receipt_json"]["exit_status"] != code
            ):
                raise ValueError("terminal outcome conflicts with receipt exit status")
            if row["terminal_json"] is not None:
                if row["terminal_json"] != canonical:
                    raise ValueError("conflicting terminal outcome")
                return row
            body = wake_body(row, payload)
            receiver = self.con.execute(
                "SELECT 1 FROM shells WHERE shell_id=? AND COALESCE(is_deleted,0)=0",
                (owner,),
            ).fetchone()
            receipt = None
            inbox_id = None
            if receiver:
                inbox_id = self.con.execute(
                    "INSERT INTO shell_messages(from_shell_id,to_shell_id,kind,body,dedupe_key) "
                    "VALUES(?,?,'result',?,?)",
                    (owner, owner, body, f"run-{run_id}-terminal"),
                ).lastrowid
                receipt = SprintMessageStore(self.con).send_to_shell_in_transaction(
                    owner,
                    message_kind="result",
                    body=body,
                    idempotency_key=f"run-{run_id}-terminal",
                    declared_type="re-enter",
                )
            self.con.execute(
                "UPDATE runs SET state=?,exit_code=?,finished_at=?,terminal_json=?,wake_state=?,"
                "wake_id=?,message_id=?,inbox_message_id=?,last_error=? WHERE run_id=?",
                (
                    payload["state"],
                    code,
                    payload["finished_at"],
                    canonical,
                    "pending" if receipt else "blocked",
                    receipt.wake_id if receipt else None,
                    receipt.message_id if receipt else None,
                    inbox_id,
                    None
                    if receipt
                    else "owner unavailable; operator recovery required",
                    run_id,
                ),
            )
        return self.get(run_id, owner)

    def receipt(self, run_id: int, owner: int, data: dict) -> dict:
        row = self.get(run_id, owner)
        if row["kind"] != "devkit":
            raise ValueError("receipts require a devkit run")
        if not isinstance(data, dict) or len(json.dumps(data).encode()) > 262144:
            raise ValueError("receipt must be an object of at most 256 KiB")
        if data.get("hook") not in {"test", "lint", "typecheck", "deps"}:
            raise ValueError("invalid receipt hook")
        if type(data.get("run_id")) is not int or data["run_id"] != run_id or data.get("commit") != row["commit"]:
            raise ValueError("receipt run/commit conflict")
        if type(data.get("exit_status")) is not int:
            raise ValueError("receipt requires an exit_status")
        if type(data.get("duration_s")) not in (int, float) or not 0 <= data["duration_s"] < 1e12:
            raise ValueError("invalid receipt duration")
        argv = data.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(v, str) for v in argv):
            raise ValueError("invalid receipt argv")
        checkout, log = data.get("checkout"), data.get("log")
        if not isinstance(checkout, str) or not Path(checkout).is_absolute():
            raise ValueError("receipt checkout must be absolute")
        if not isinstance(log, str) or Path(log).is_absolute() or ".." in Path(log).parts:
            raise ValueError("receipt log must be checkout-relative")
        summary = data.get("summary")
        if summary is not None:
            if not isinstance(summary, dict) or summary.get("framework") != "pytest":
                raise ValueError("invalid receipt summary")
            if any(type(summary.get(k)) is not int or summary[k] < 0
                   for k in ("passed", "failed", "errors", "skipped")):
                raise ValueError("invalid pytest counts")
            if not isinstance(summary.get("failing"), list) or not all(
                isinstance(v, str) for v in summary["failing"]
            ):
                raise ValueError("invalid pytest failing ids")
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
        with db_driver.write_transaction(self.con, "runs.receipt"):
            row = self.get(run_id, owner)
            if row["state"] in {"done", "failed"} and row["exit_code"] != data["exit_status"]:
                raise ValueError("receipt exit status conflicts with terminal outcome")
            existing = row["receipt_json"]
            if existing is not None and existing != data:
                raise ValueError("conflicting receipt")
            self.con.execute("UPDATE runs SET receipt_json=? WHERE run_id=?", (canonical, run_id))
        return self.get(run_id, owner)

    def prune_evidence(self, run_id: int, owner: int) -> dict:
        row = self.get(run_id, owner)
        if row["state"] not in TERMINAL or row["receipt_json"] is None:
            raise ValueError("only completed receipt evidence may be pruned")
        with db_driver.write_transaction(self.con, "runs.prune_evidence"):
            self.con.execute("UPDATE runs SET evidence_pruned=1 WHERE run_id=?", (run_id,))
        return self.get(run_id, owner)

    def kill(self, run_id: int, owner: int | None) -> dict:
        row = self.get(run_id, owner)
        if row["state"] in TERMINAL:
            raise ValueError("run already terminal")
        if process_alive(row["pid"], row["start_ticks"], row["boot_id"]) is not True:
            raise ValueError("process incarnation is gone, recycled or unverifiable")
        # Marker is independent of meta.json so cancellation never races a
        # supervisor write. External signaling is outside the DB transaction.
        if (
            process_alive(
                row["supervisor_pid"], row["supervisor_start_ticks"], row["boot_id"]
            )
            is not True
        ):
            raise ValueError("supervisor incarnation is gone or unverifiable")
        Path(row["evidence_path"]).with_name("kill_requested").touch()
        # The still-owning supervisor sends TERM/KILL to its child group.
        # Signalling here could reap the leader before escalation is verified.
        return {"run_id": run_id, "kill_requested": True}

    def reconcile(self) -> None:
        rows = self.con.execute(
            "SELECT * FROM runs WHERE state IN ('registered','running') "
            "AND wake_state<>'blocked'"
        ).fetchall()
        for raw in rows:
            try:
                self._reconcile_row(dict(raw))
            except Exception as exc:  # noqa: BLE001 - row evidence must not halt engine wake delivery
                reason = f"{type(exc).__name__}: {exc}"[:512]
                with db_driver.write_transaction(self.con, "runs.reconcile_blocked"):
                    self.con.execute(
                        "UPDATE runs SET wake_state='blocked',last_error=? "
                        "WHERE run_id=? AND state IN ('registered','running')",
                        (
                            "reconciliation blocked; operator recovery required: "
                            + reason,
                            raw["run_id"],
                        ),
                    )

    def _reconcile_row(self, row: dict) -> None:
        path = Path(row["evidence_path"]).with_name("meta.json")
        try:
            meta = json.loads(path.read_text())
        except (OSError, ValueError):
            meta = {}
        if meta.get("run_id") != row["run_id"]:
            meta = {}
        if meta.get("finished_at"):
            self.terminal(row["run_id"], row["owner_shell_id"], terminal_payload(meta))
            return
        if meta.get("supervisor_pid"):
            self.running(row["run_id"], row["owner_shell_id"], meta)
            row.update(meta)
        alive = process_alive(
            row.get("supervisor_pid"),
            row.get("supervisor_start_ticks"),
            row.get("boot_id"),
        )
        if alive is True or (alive is None and row.get("supervisor_pid")):
            return
        age = (
            datetime.now(timezone.utc)
            - datetime.fromisoformat(row["created_at"]).replace(tzinfo=timezone.utc)
        ).total_seconds()
        if alive is None and age < 30:
            return
        # Re-read after proving the supervisor gone: it may have persisted
        # its terminal result between our first read and the liveness check.
        try:
            final = json.loads(path.read_text())
        except (OSError, ValueError):
            final = {}
        if self.get(row["run_id"])["state"] in TERMINAL:
            return
        if final.get("run_id") == row["run_id"] and final.get("finished_at"):
            self.terminal(row["run_id"], row["owner_shell_id"], terminal_payload(final))
            return
        self.terminal(
            row["run_id"],
            row["owner_shell_id"],
            {
                "state": "lost",
                "finished_at": datetime.now(timezone.utc).isoformat(),
            },
        )
