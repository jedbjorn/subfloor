#!/usr/bin/env python3
"""Coverage for sandbox harness freshness — the harness epoch.

The harness CLIs shells run are BAKED into the sandbox image (their binaries are
host-ABI artifacts and cannot be mounted in like creds). Docker serves those
installer layers from cache forever, so before this seam existed there was no
command anywhere that could make a shell's harness newer: `launch`/`restart` do
build, but from cache; `update-harnesses` ran the installers against the HOST,
which the container does not mount; `update` called ensure_harnesses(), which
skips anything already present; and every launch `docker rm -f`s the writable
layer an in-container install would land in. A claude one release short of the
model it needed survived all four.

These tests pin the way out: an epoch that expires those layers, rolled by the
update paths, passed to the build, and referenced inside the RUNs so the bust is
guaranteed rather than left to docker's arg-cache heuristics.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
INSTALL_PY = ENGINE / "scripts" / "install.py"
TODAY = date.today().isoformat()


def run_install(*args: str, epoch_file: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["SC_HARNESS_EPOCH_FILE"] = str(epoch_file)
    return subprocess.run([sys.executable, str(INSTALL_PY), *args],
                          capture_output=True, text=True, env=env, cwd=ROOT)


class HarnessEpochValue(unittest.TestCase):
    """The stored value, via the CLI seam `sc` actually shells out to."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.epoch_file = Path(self._tmp.name) / "state" / "harness-epoch"

    def test_unrolled_machine_reports_the_dockerfile_default(self):
        """"0" is the Dockerfile's own default, so a machine that has never
        rolled builds the exact image an un-instrumented build produced — the
        seam must not invalidate anyone's cache merely by existing."""
        result = run_install("--harness-epoch", epoch_file=self.epoch_file)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0")

    def test_roll_writes_today_and_creates_its_parent_directory(self):
        result = run_install("--roll-harness-epoch", epoch_file=self.epoch_file)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), TODAY)
        self.assertEqual(self.epoch_file.read_text().strip(), TODAY)

    def test_roll_is_idempotent_within_a_day(self):
        """The value is a DATE on purpose: every fork on a machine shares the
        image tag, so a second fork updating the same day must produce the same
        build arg and cache-hit instead of re-downloading five installers."""
        first = run_install("--roll-harness-epoch", epoch_file=self.epoch_file)
        second = run_install("--roll-harness-epoch", epoch_file=self.epoch_file)
        self.assertEqual(first.stdout.strip(), second.stdout.strip())

    def test_stored_value_reads_back(self):
        run_install("--roll-harness-epoch", epoch_file=self.epoch_file)
        result = run_install("--harness-epoch", epoch_file=self.epoch_file)
        self.assertEqual(result.stdout.strip(), TODAY)

    def test_unreadable_store_reads_as_unrolled_rather_than_failing(self):
        """A build must never be blocked by the freshness bookkeeping — an
        unreadable store means "not rolled here", not "stop"."""
        self.epoch_file.parent.mkdir(parents=True)
        self.epoch_file.mkdir()  # a directory where a file belongs
        result = run_install("--harness-epoch", epoch_file=self.epoch_file)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0")


class HarnessEpochDockerfile(unittest.TestCase):
    """The image side of the contract, asserted on the Dockerfile text."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = (ENGINE / "Dockerfile").read_text()
        sys.path.insert(0, str(ENGINE / "scripts"))
        import install as install_mod  # noqa: PLC0415 — engine scripts are path-loaded
        # The harness set, from the one place that owns it — the same dict the
        # Dockerfile's header comment says to keep these installers in sync with.
        # Selecting by NAME, never by installer hostname: matching a bare domain
        # substring is the url-sanitization anti-pattern CodeQL flags, and it
        # would silently stop finding a layer the day a vendor moves its host.
        cls.harness_names = tuple(install_mod.HARNESS_INSTALL)

    def folded(self) -> str:
        """The Dockerfile with line continuations joined, so each instruction is
        one line and a position comparison is a comparison of instructions."""
        return self.text.replace("\\\n", " ")

    def harness_runs(self) -> list[str]:
        """The RUN instructions that install harness CLIs — one line each."""
        return [line for line in self.folded().splitlines()
                if line.startswith("RUN ") and "curl" in line
                and any(name in line for name in self.harness_names)]

    def test_both_harness_layers_exist(self):
        self.assertEqual(len(self.harness_runs()), 2,
                         "expected the kimi RUN and the claude/opencode/codex/vibe RUN")

    def test_every_harness_the_engine_installs_is_baked(self):
        """A harness added to HARNESS_INSTALL but not to the image would be
        absent from every sandbox while `./sc install` reported it present."""
        baked = " ".join(self.harness_runs())
        for name in self.harness_names:
            with self.subTest(harness=name):
                self.assertIn(name, baked)

    def test_every_harness_layer_references_the_epoch(self):
        """Declaring the ARG near a RUN is not enough — docker only reliably
        invalidates a layer whose COMMAND changes. Each harness RUN must expand
        the epoch, or a roll silently buys nothing and the freeze comes back."""
        for run in self.harness_runs():
            with self.subTest(run=run[:60]):
                self.assertIn("SC_HARNESS_EPOCH", run)

    def test_epoch_arg_is_declared_before_the_layers_it_expires(self):
        """An ARG is in scope from its declaration to the end of the stage — a
        harness RUN above it would expand the epoch to nothing and never bust."""
        folded = self.folded()
        arg_at = folded.index("ARG SC_HARNESS_EPOCH")
        first_harness_at = min(folded.index(run) for run in self.harness_runs())
        self.assertLess(arg_at, first_harness_at)

    def test_epoch_arg_defaults_to_unrolled(self):
        self.assertRegex(self.text, r"ARG SC_HARNESS_EPOCH=0\b")

    def test_image_records_the_epoch_it_was_built_with(self):
        """`harness-status` compares this label against the stored epoch to say
        whether a build is owed; without it the answer would be a guess."""
        self.assertIn('LABEL sc.harness_epoch="${SC_HARNESS_EPOCH}"', self.text)

    def test_epoch_sits_below_the_expensive_layers(self):
        """Rolling must cost the harness downloads and nothing else — put the
        ARG above node/playwright/pip and every roll rebuilds the world."""
        arg_at = self.text.index("ARG SC_HARNESS_EPOCH")
        for costly in ("playwright", "nodesource"):
            with self.subTest(layer=costly):
                self.assertLess(self.text.index(costly), arg_at)


class ScFixture:
    """A throwaway fork tree with a fake docker, enough to drive `sc`'s harness
    commands and read back exactly what it asked docker to do."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "fork"
        self.engine = self.root / ".super-coder"
        self.scripts = self.engine / "scripts"
        self.fakebin = Path(self._tmp.name) / "bin"
        self.log = Path(self._tmp.name) / "calls.log"
        self.epoch_file = Path(self._tmp.name) / "state" / "harness-epoch"
        self.scripts.mkdir(parents=True)
        self.fakebin.mkdir()
        self.log.touch()
        shutil.copy2(ROOT / "sc", self.root / "sc")
        # cli_entry.py is not optional in a synthetic fork: every entrypoint
        # imports it from its __main__ block (SIGPIPE hygiene, #384).
        for script in ("install.py", "conductor_policy.py",
                       "engine_manifest.py", "ports.py",
                       "artifact_policy.py", "harness_versions.py",
                       "cli_entry.py"):
            shutil.copy2(ENGINE / "scripts" / script, self.scripts / script)
        (self.engine / "Dockerfile").write_text("FROM scratch\n")
        self._write_fake_docker()
        # Stub curl too. The no-docker branch of update-harnesses runs the real
        # vendor installers; a regression that took that branch under docker
        # would otherwise pipe the live internet into bash on the test machine.
        self._write_executable("curl", """\
            #!/bin/sh
            printf 'curl' >> "$SC_TEST_LOG"
            printf ' %s' "$@" >> "$SC_TEST_LOG"
            printf '\\n' >> "$SC_TEST_LOG"
            exit 0
            """)
        self.env = os.environ.copy()
        # This fixture exercises host-side Docker orchestration even when the
        # test runner itself lives inside a super-coder sandbox.
        self.env.pop("SC_SANDBOX", None)
        self.env.update({
            "PATH": f"{self.fakebin}:{self.env['PATH']}",
            "SC_PYTHON": sys.executable,
            "SC_HARNESS_EPOCH_FILE": str(self.epoch_file),
            "SC_TEST_LOG": str(self.log),
            "SC_TEST_LABEL": "",
            "SC_TEST_RUNNING": "1",
            "NO_COLOR": "1",
        })

    def close(self) -> None:
        self._tmp.cleanup()

    def _write_executable(self, name: str, body: str) -> None:
        path = self.fakebin / name
        path.write_text(textwrap.dedent(body))
        path.chmod(0o755)

    def _write_fake_docker(self) -> None:
        path = self.fakebin / "docker"
        path.write_text(textwrap.dedent(
            """\
            #!/bin/sh
            printf 'docker' >> "$SC_TEST_LOG"
            printf ' %s' "$@" >> "$SC_TEST_LOG"
            printf '\\n' >> "$SC_TEST_LOG"
            case "$1" in
              info) exit 0 ;;
              build) exit 0 ;;
              image) printf '%s\\n' "$SC_TEST_LABEL"; exit 0 ;;
              inspect) [ -n "$SC_TEST_RUNNING" ] && echo true || echo false; exit 0 ;;
              exec)
                # `docker exec <name> claude --version` — the launch banner probe.
                for a in "$@"; do
                  if [ "$a" = claude ]; then echo "9.9.9 (Claude Code)"; exit 0; fi
                done
                echo "  claude    9.9.9 (Claude Code)"; exit 0 ;;
            esac
            exit 0
            """
        ))
        path.chmod(0o755)

    def calls(self) -> list[str]:
        return [line for line in self.log.read_text().splitlines() if line.strip()]

    def build_args(self) -> dict[str, str]:
        """SC_* build args from the last `docker build`, as a dict."""
        build = [c for c in self.calls() if c.startswith("docker build ")][-1]
        return dict(re.findall(r"--build-arg (\w+)=(\S*)", build))

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        environ = {**self.env, **env}
        return subprocess.run([str(self.root / "sc"), *args], capture_output=True,
                              text=True, env=environ, cwd=self.root)


class ScHarnessCommands(unittest.TestCase):
    def setUp(self) -> None:
        # Pin the ambient condition that exposed the leak; CI usually runs
        # outside the sandbox and would otherwise miss this regression.
        with mock.patch.dict(os.environ, {"SC_SANDBOX": "1"}):
            self.fx = ScFixture()
        self.addCleanup(self.fx.close)

    def test_build_passes_the_stored_epoch(self):
        result = self.fx.run("build")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fx.build_args().get("SC_HARNESS_EPOCH"), "0")

    def test_build_harnesses_rolls_first_so_the_layers_expire(self):
        result = self.fx.run("build", "--harnesses")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fx.build_args().get("SC_HARNESS_EPOCH"), TODAY)
        self.assertEqual(self.fx.epoch_file.read_text().strip(), TODAY)

    def test_plain_build_does_not_roll(self):
        """`build` is on the launch/restart path — it must stay a cache-warm
        no-op, or every restart would re-download five installers."""
        self.fx.run("build")
        self.assertFalse(self.fx.epoch_file.exists())

    def test_build_rejects_an_unknown_flag_before_building(self):
        result = self.fx.run("build", "--harness")  # a plausible typo
        self.assertEqual(result.returncode, 2)
        self.assertFalse([c for c in self.fx.calls() if c.startswith("docker build")])

    def test_update_harnesses_rolls_and_rebuilds_on_the_docker_path(self):
        result = self.fx.run("update-harnesses")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fx.build_args().get("SC_HARNESS_EPOCH"), TODAY)

    def test_update_harnesses_does_not_install_onto_the_host_when_docker_runs(self):
        """The old behavior: run the installers here, report success, change
        nothing a shell can see. The container mounts creds, never binaries.
        Asserted on the stubbed curl rather than on output text, so a regression
        is caught by what the command TRIED to do — and cannot reach the network
        from a test run either way."""
        result = self.fx.run("update-harnesses")
        self.assertEqual([c for c in self.fx.calls() if c.startswith("curl ")], [],
                         "ran a harness installer against a host no shell can see")
        self.assertNotIn("Updating harness CLIs", result.stdout)

    def test_update_harnesses_names_the_restart_that_runs_them(self):
        """A rebuilt image is not a refreshed sandbox — the running container
        keeps the old one until it is bounced. Say so, or the operator reads
        'rebuilt' as 'done' (which is how this bug was reported)."""
        result = self.fx.run("update-harnesses")
        self.assertIn("./sc restart", result.stdout)

    def test_harness_status_reports_the_versions_inside_the_sandbox(self):
        result = self.fx.run("harness-status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("harness CLIs (in the sandbox — what shells run):",
                      result.stdout)
        self.assertIn("9.9.9 (Claude Code)", result.stdout)
        self.assertNotIn("harness CLIs (this runtime):", result.stdout)
        self.assertEqual(
            [call for call in self.fx.calls()
             if call.startswith("docker exec ")],
            [f"docker exec sc-fork python3 "
             f"{self.fx.scripts / 'harness_versions.py'}"],
        )

    def test_harness_status_flags_an_image_that_owes_a_rebuild(self):
        self.fx.run("build", "--harnesses")          # epoch rolled to today…
        result = self.fx.run("harness-status", SC_TEST_LABEL="2020-01-01")
        self.assertIn("predates the stored epoch", result.stdout)

    def test_harness_status_is_quiet_when_the_image_matches(self):
        self.fx.run("build", "--harnesses")
        result = self.fx.run("harness-status", SC_TEST_LABEL=TODAY)
        self.assertNotIn("predates the stored epoch", result.stdout)

    def test_harness_status_does_not_claim_currency_for_an_unlabelled_image(self):
        """An image built before the seam existed has no label. Unknown must not
        read as current — that is precisely the state this bug shipped in."""
        self.fx.run("build", "--harnesses")
        result = self.fx.run("harness-status", SC_TEST_LABEL="")
        self.assertIn("predates the stored epoch", result.stdout)


class UpdateExpiresSandboxHarnesses(unittest.TestCase):
    """`./sc update` (dos-u) must move the harnesses shells run, not just the
    host's — the gap that let an update ship a new floor on frozen CLIs."""

    def setUp(self) -> None:
        sys.path.insert(0, str(ENGINE / "scripts"))
        import update as update_mod  # noqa: PLC0415 — engine scripts are path-loaded
        self.update_mod = update_mod
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.epoch_file = Path(self._tmp.name) / "harness-epoch"
        os.environ["SC_HARNESS_EPOCH_FILE"] = str(self.epoch_file)
        self.addCleanup(os.environ.pop, "SC_HARNESS_EPOCH_FILE", None)
        self.docker = {"state": "rootless"}
        real = self.update_mod.install_mod.docker_status
        self.update_mod.install_mod.docker_status = lambda: self.docker
        self.addCleanup(setattr, self.update_mod.install_mod, "docker_status", real)

    def test_update_rolls_the_epoch(self):
        self.assertEqual(self.update_mod.expire_sandbox_harnesses(), TODAY)
        self.assertEqual(self.epoch_file.read_text().strip(), TODAY)

    def test_no_docker_rolls_silently_instead_of_describing_a_missing_runtime(self):
        self.docker = {"state": "absent"}
        self.assertIsNone(self.update_mod.expire_sandbox_harnesses())
        self.assertEqual(self.epoch_file.read_text().strip(), TODAY)


if __name__ == "__main__":
    unittest.main()
