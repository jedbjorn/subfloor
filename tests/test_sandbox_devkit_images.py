#!/usr/bin/env python3
"""Behavioral coverage for fork-extension sandbox image identity."""
from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from sandbox_devkit import (  # noqa: E402
    MOUNT_MARKER,
    ProvisionFailed,
    SandboxImageError,
    build_images,
    cleanup_owned_resources,
    docker_run,
    image_plan,
    launch_container,
    preflight_image,
    provision_checkout,
    provisioning_fingerprint,
    provisioning_payload,
    readiness,
    retire_superseded_base_images,
    volume_plans,
)


class FakeDocker:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.images: dict[str, dict] = {}
        self.volumes: dict[str, dict] = {}
        self.containers: dict[str, str] = {}
        self.image_remove_failures: set[str] = set()
        self.build_counter = 0
        self.hook_status = 0
        self.hook_delay = 0.0

    def __call__(self, command, *, check, text, capture_output=False):
        self.assert_protocol(check, text)
        command = tuple(command)
        self.commands.append(command)
        if command[:2] == ("docker", "build"):
            tag = command[command.index("-t") + 1]
            labels = {}
            for index, value in enumerate(command):
                if value == "--label":
                    key, label_value = command[index + 1].split("=", 1)
                    labels[key] = label_value
            image_id = "sha256:" + ("b" if "-base:" in tag else "e") * 64
            self.build_counter += 1
            self.images[tag] = {
                "Id": image_id,
                "Created": f"2026-08-12T12:00:{self.build_counter:02d}Z",
                "Config": {"Labels": labels},
            }
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ("docker", "image", "inspect"):
            image = self.images.get(command[3])
            if image is None:
                image = next(
                    (
                        candidate
                        for candidate in self.images.values()
                        if candidate["Id"] == command[3]
                    ),
                    None,
                )
            if image is None:
                return subprocess.CompletedProcess(command, 1, "", "not found")
            return subprocess.CompletedProcess(command, 0, json.dumps([image]), "")
        if command[:3] == ("docker", "volume", "inspect"):
            volume = self.volumes.get(command[3])
            if volume is None:
                return subprocess.CompletedProcess(command, 1, "", "not found")
            return subprocess.CompletedProcess(command, 0, json.dumps([volume]), "")
        if command[:3] == ("docker", "volume", "ls"):
            filters = [
                command[index + 1].removeprefix("label=")
                for index, value in enumerate(command)
                if value == "--filter"
            ]
            names = []
            for name, volume in self.volumes.items():
                labels = volume.get("Labels") or {}
                if all(labels.get(key) == expected for key, expected in (
                    item.split("=", 1) for item in filters
                )):
                    names.append(name)
            return subprocess.CompletedProcess(command, 0, "\n".join(names), "")
        if command[:3] == ("docker", "volume", "create"):
            labels = {}
            for index, value in enumerate(command):
                if value == "--label":
                    key, label_value = command[index + 1].split("=", 1)
                    labels[key] = label_value
            name = command[-1]
            self.volumes.setdefault(name, {"Name": name, "Labels": labels})
            return subprocess.CompletedProcess(command, 0, name + "\n", "")
        if command[:3] == ("docker", "volume", "rm"):
            self.volumes.pop(command[3], None)
            return subprocess.CompletedProcess(command, 0, command[3] + "\n", "")
        if command[:3] == ("docker", "image", "ls"):
            filters = [
                command[index + 1].removeprefix("label=")
                for index, value in enumerate(command)
                if value == "--filter"
            ]
            ids = []
            for image in self.images.values():
                labels = image.get("Config", {}).get("Labels") or {}
                if all(labels.get(key) == expected for key, expected in (
                    item.split("=", 1) for item in filters
                )):
                    ids.append(image["Id"])
            return subprocess.CompletedProcess(command, 0, "\n".join(ids), "")
        if command[:3] == ("docker", "image", "rm"):
            image_id = command[3]
            if image_id in self.image_remove_failures:
                return subprocess.CompletedProcess(command, 1, "", "image is in use")
            self.images = {
                tag: image for tag, image in self.images.items() if image["Id"] != image_id
            }
            return subprocess.CompletedProcess(command, 0, image_id + "\n", "")
        if command[:2] == ("docker", "run"):
            if "--name" in command:
                name = command[command.index("--name") + 1]
                image = next(
                    (self.images[value]["Id"] for value in command if value in self.images),
                    None,
                )
                if image is None:
                    raise AssertionError(f"run image missing from command: {command}")
                self.containers[name] = image
            return subprocess.CompletedProcess(command, 0, "container-id\n", "")
        if command[:3] == ("docker", "rm", "-f"):
            self.containers.pop(command[3], None)
            return subprocess.CompletedProcess(command, 0, command[3] + "\n", "")
        if command[:3] == ("docker", "inspect", "--format"):
            image = self.containers.get(command[4])
            if image is None:
                return subprocess.CompletedProcess(command, 1, "", "not found")
            return subprocess.CompletedProcess(command, 0, image + "\n", "")
        if command[:2] == ("docker", "exec"):
            if self.hook_delay:
                time.sleep(self.hook_delay)
            return subprocess.CompletedProcess(
                command,
                self.hook_status,
                "provision stdout\n",
                "" if self.hook_status == 0 else "provision stderr\n",
            )
        raise AssertionError(f"unexpected command: {command}")

    @staticmethod
    def assert_protocol(check, text) -> None:
        if check is not False or text is not True:
            raise AssertionError("runner protocol changed")

    def builds(self) -> list[tuple[str, ...]]:
        return [command for command in self.commands if command[:2] == ("docker", "build")]


class ImageFixture:
    def __init__(
        self,
        parent: Path,
        name: str,
        *,
        sandbox: bool = True,
        mounts: bool = False,
        provision: bool = False,
    ) -> None:
        self.root = parent / name
        self.engine = self.root / ".super-coder"
        self.subfloor = self.root / ".subfloor"
        self.state = self.root / ".sc-state"
        self.context = self.root / "container" / "context"
        self.engine.mkdir(parents=True)
        self.subfloor.mkdir()
        self.state.mkdir()
        self.context.mkdir(parents=True)
        (self.engine / "Dockerfile").write_text("FROM scratch\n")
        (self.state / "engine.ref").write_text("a" * 40 + "\n")
        declaration: dict = {"version": 1}
        if provision:
            hook = self.subfloor / "provision"
            hook.write_text("#!/bin/sh\nexit 0\n")
            hook.chmod(0o755)
            (self.root / "requirements.lock").write_text("first\n")
            declaration["hooks"] = {
                "deps": {"argv": ["./.subfloor/provision"], "cwd": "."}
            }
            declaration["provision"] = {
                "hook": "deps",
                "inputs": ["requirements.lock"],
            }
        if sandbox:
            dockerfile = self.root / "container" / "Fork.Dockerfile"
            dockerfile.write_text(
                "ARG SC_BASE_IMAGE\n"
                "FROM busybox AS source\n"
                "RUN echo source > /payload\n"
                "FROM ${SC_BASE_IMAGE}\n"
                "COPY --from=source /payload /payload\n"
            )
            declaration["sandbox"] = {
                "dockerfile": "container/Fork.Dockerfile",
                "context": "container/context",
            }
            if mounts:
                declaration["sandbox"]["mounts"] = [
                    {"name": "python-env", "target": ".venv"}
                ]
        (self.subfloor / "dev-kit.json").write_text(json.dumps(declaration))
        if provision:
            (self.root / ".gitignore").write_text("/.sc-state/local/\n")
            subprocess.run(("git", "init", "-q", str(self.root)), check=True)

    def plan(self):
        return image_plan(
            self.root,
            self.engine,
            "20260809T170000.000000Z",
            user="tester",
            uid="1000",
            gid="1000",
        )


class SandboxImagePlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def test_two_installations_share_exact_base_but_not_extension_tag(self):
        left = ImageFixture(self.base, "left").plan()
        right = ImageFixture(self.base, "right").plan()
        self.assertEqual(left.base_tag, right.base_tag)
        self.assertNotEqual(left.runtime_tag, right.runtime_tag)
        self.assertNotEqual(
            left.runtime_labels["sc.fork_identity"],
            right.runtime_labels["sc.fork_identity"],
        )

    def test_extension_contract_is_validated_before_any_docker_command(self):
        fixture = ImageFixture(self.base, "invalid")
        dockerfile = fixture.root / "container" / "Fork.Dockerfile"
        for text in (
            "FROM python:3.12\n",
            "ARG SC_BASE_IMAGE\nFROM python:3.12\n",
            "FROM ${SC_BASE_IMAGE}\nARG SC_BASE_IMAGE\n",
        ):
            with self.subTest(text=text):
                dockerfile.write_text(text)
                with self.assertRaisesRegex(SandboxImageError, "SC_BASE_IMAGE"):
                    fixture.plan()

    def test_missing_docker_is_a_host_prerequisite_error_not_invalid_state(self):
        fixture = ImageFixture(self.base, "missing-docker")
        completed = subprocess.run(
            (
                sys.executable,
                str(SCRIPTS / "sandbox_devkit.py"),
                "build",
                str(fixture.root),
                str(fixture.engine),
                "20260809T170000.000000Z",
                "tester",
                "1000",
                "1000",
            ),
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": str(self.base / "empty-path")},
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("dev-kit prerequisite error", completed.stderr)
        self.assertIn("cannot run docker", completed.stderr)
        self.assertNotIn("state: invalid", completed.stderr)

    def test_preflight_preserves_missing_docker_as_a_prerequisite_error(self):
        fixture = ImageFixture(self.base, "missing-docker-preflight")
        completed = subprocess.run(
            (
                sys.executable,
                str(SCRIPTS / "sandbox_devkit.py"),
                "preflight",
                str(fixture.root),
                str(fixture.engine),
                "20260809T170000.000000Z",
                "tester",
                "1000",
                "1000",
            ),
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": str(self.base / "empty-path")},
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("dev-kit prerequisite error", completed.stderr)
        self.assertIn("cannot run docker", completed.stderr)
        self.assertNotIn("state: invalid", completed.stderr)
        self.assertNotIn("--no-build cannot reuse", completed.stderr)

    def test_build_uses_immutable_base_id_exact_context_and_required_labels(self):
        fixture = ImageFixture(self.base, "fork")
        plan = fixture.plan()
        docker = FakeDocker()

        result = build_images(plan, runner=docker)

        self.assertEqual(result, plan.runtime_tag)
        self.assertEqual(len(docker.builds()), 2)
        base, extension = docker.builds()
        self.assertEqual(base[-1], str(fixture.root.resolve()))
        self.assertEqual(extension[-1], str(fixture.context.resolve()))
        self.assertIn("SC_BASE_IMAGE=sha256:" + "b" * 64, extension)
        for key, value in plan.runtime_labels.items():
            self.assertIn(f"{key}={value}", extension)
        self.assertEqual(
            set(plan.runtime_labels),
            {
                "sc.image_kind",
                "sc.engine_ref",
                "sc.harness_epoch",
                "sc.declaration_digest",
                "sc.fork_identity",
                "sc.dockerfile_digest",
            },
        )

    def test_no_build_rejects_foreign_label_and_accepts_exact_image(self):
        plan = ImageFixture(self.base, "fork").plan()
        docker = FakeDocker()
        build_images(plan, runner=docker)
        docker.images[plan.runtime_tag]["Config"]["Labels"]["sc.fork_identity"] = "other"
        with self.assertRaisesRegex(SandboxImageError, "stale or foreign"):
            preflight_image(plan, runner=docker)
        docker.images[plan.runtime_tag]["Config"]["Labels"] = dict(plan.runtime_labels)
        self.assertEqual(preflight_image(plan, runner=docker), plan.runtime_tag)

    def test_declaration_without_sandbox_runs_on_one_labeled_base(self):
        plan = ImageFixture(self.base, "baseline", sandbox=False).plan()
        docker = FakeDocker()
        result = build_images(plan, runner=docker)
        self.assertEqual(result, plan.base_tag)
        self.assertEqual(len(docker.builds()), 1)
        self.assertEqual(plan.runtime_labels["sc.image_kind"], "engine-base")

    def test_build_retires_only_old_owned_base_generations(self):
        plan = ImageFixture(self.base, "retention", sandbox=False).plan()
        docker = FakeDocker()
        prior_ids = []
        for index in range(4):
            image_id = "sha256:" + str(index + 1) * 64
            prior_ids.append(image_id)
            docker.images[f"super-coder-base:prior-{index}"] = {
                "Id": image_id,
                "Created": f"2026-08-0{index + 1}T12:00:00Z",
                "Config": {"Labels": {
                    **plan.base_labels,
                    "sc.engine_ref": str(index + 1) * 40,
                }},
            }
        foreign_id = "sha256:" + "f" * 64
        docker.images["super-coder-base:foreign"] = {
            "Id": foreign_id,
            "Created": "2026-07-01T12:00:00Z",
            "Config": {"Labels": {
                **plan.base_labels,
                "sc.build_identity": "foreign",
            }},
        }

        self.assertEqual(build_images(plan, runner=docker), plan.base_tag)

        remaining_ids = {image["Id"] for image in docker.images.values()}
        current_id = "sha256:" + "b" * 64
        self.assertIn(current_id, remaining_ids)
        self.assertNotIn(prior_ids[0], remaining_ids)
        self.assertNotIn(prior_ids[1], remaining_ids)
        self.assertIn(prior_ids[2], remaining_ids)
        self.assertIn(prior_ids[3], remaining_ids)
        self.assertIn(foreign_id, remaining_ids)
        removals = [
            command for command in docker.commands
            if command[:3] == ("docker", "image", "rm")
        ]
        self.assertEqual({command[3] for command in removals}, set(prior_ids[:2]))
        self.assertNotIn(current_id, {command[3] for command in removals})
        listing = next(
            command for command in docker.commands
            if command[:3] == ("docker", "image", "ls")
        )
        self.assertIn("label=sc.image_kind=engine-base", listing)
        self.assertIn(
            f"label=sc.build_identity={plan.base_labels['sc.build_identity']}",
            listing,
        )

    def test_retention_keeps_in_use_image_and_does_not_fail_build(self):
        plan = ImageFixture(self.base, "retention-race", sandbox=False).plan()
        docker = FakeDocker()
        old_id = "sha256:" + "1" * 64
        for index, marker in enumerate(("1", "2", "3"), start=1):
            image_id = "sha256:" + marker * 64
            docker.images[f"super-coder-base:race-{marker}"] = {
                "Id": image_id,
                "Created": f"2026-07-0{index}T12:00:00Z",
                "Config": {"Labels": dict(plan.base_labels)},
            }
        docker.image_remove_failures.add(old_id)
        errors = io.StringIO()

        with contextlib.redirect_stderr(errors):
            result = build_images(plan, runner=docker)

        self.assertEqual(result, plan.base_tag)
        self.assertIn("image is in use", errors.getvalue())
        self.assertIn(old_id, {image["Id"] for image in docker.images.values()})

    def test_extension_build_retires_prior_fork_image(self):
        plan = ImageFixture(self.base, "extension-retention").plan()
        docker = FakeDocker()
        old_runtime_id = "sha256:" + "a" * 64
        docker.images["super-coder-sandbox-old:latest"] = {
            "Id": old_runtime_id,
            "Created": "2026-07-01T12:00:00Z",
            "Config": {"Labels": dict(plan.runtime_labels)},
        }
        old_base_ids = []
        for index, marker in enumerate(("1", "2", "3"), start=1):
            image_id = "sha256:" + marker * 64
            old_base_ids.append(image_id)
            docker.images[f"super-coder-base:extension-prior-{marker}"] = {
                "Id": image_id,
                "Created": f"2026-07-0{index}T12:00:00Z",
                "Config": {"Labels": dict(plan.base_labels)},
            }

        self.assertEqual(build_images(plan, runner=docker), plan.runtime_tag)

        remaining_ids = {image["Id"] for image in docker.images.values()}
        self.assertNotIn(old_runtime_id, remaining_ids)
        self.assertNotIn(old_base_ids[0], remaining_ids)
        removals = [
            command[3] for command in docker.commands
            if command[:3] == ("docker", "image", "rm")
        ]
        self.assertEqual(removals, [old_runtime_id, old_base_ids[0]])
        self.assertNotIn("sha256:" + "e" * 64, removals)
        self.assertNotIn("sha256:" + "b" * 64, removals)

    def test_retention_skips_malformed_owned_image(self):
        plan = ImageFixture(self.base, "malformed-retention").plan()
        docker = FakeDocker()
        malformed_id = "sha256:" + "9" * 64
        docker.images["super-coder-base:malformed"] = {
            "Id": malformed_id,
            "Config": {"Labels": dict(plan.base_labels)},
        }
        errors = io.StringIO()

        with contextlib.redirect_stderr(errors):
            removed = retire_superseded_base_images(
                plan,
                "sha256:" + "b" * 64,
                keep_prior=0,
                runner=docker,
            )

        self.assertEqual(removed, [])
        self.assertIn("no creation timestamp", errors.getvalue())
        self.assertIn(malformed_id, {image["Id"] for image in docker.images.values()})

    def test_retention_rejects_negative_history(self):
        plan = ImageFixture(self.base, "negative-retention", sandbox=False).plan()
        with self.assertRaisesRegex(ValueError, "non-negative"):
            retire_superseded_base_images(
                plan,
                "sha256:" + "b" * 64,
                keep_prior=-1,
                runner=FakeDocker(),
            )

    def test_declared_volumes_are_install_and_target_scoped(self):
        left_fixture = ImageFixture(self.base, "left", mounts=True)
        right_fixture = ImageFixture(self.base, "right", mounts=True)
        left = volume_plans(left_fixture.plan())[0]
        right = volume_plans(right_fixture.plan())[0]
        self.assertNotEqual(left.volume_name, right.volume_name)
        self.assertEqual(left.logical_name, "python-env")
        self.assertEqual(left.target, (left_fixture.root / ".venv").resolve())
        declaration = json.loads(
            (left_fixture.subfloor / "dev-kit.json").read_text()
        )
        declaration["sandbox"]["mounts"][0]["target"] = "cache/python"
        (left_fixture.subfloor / "dev-kit.json").write_text(json.dumps(declaration))
        moved = volume_plans(left_fixture.plan())[0]
        self.assertNotEqual(left.volume_name, moved.volume_name)

    def test_docker_run_creates_labeled_volume_and_mounts_only_declared_target(self):
        fixture = ImageFixture(self.base, "fork", mounts=True)
        plan = fixture.plan()
        docker = FakeDocker()
        self._seed_runtime_image(plan, docker)

        docker_run(
            plan,
            ("-d", "--name", "sandbox", MOUNT_MARKER, plan.runtime_tag, "serve"),
            runner=docker,
        )

        volume = volume_plans(plan)[0]
        create = next(
            command
            for command in docker.commands
            if command[:3] == ("docker", "volume", "create")
        )
        for key, value in volume.labels.items():
            self.assertIn(f"{key}={value}", create)
        initialization = next(
            command
            for command in docker.commands
            if command[:2] == ("docker", "run") and "--entrypoint" in command
        )
        self.assertIn("0:0", initialization)
        self.assertEqual(initialization[-2:], ("1000:1000", "/sc-devkit-volume"))
        run = next(
            command
            for command in docker.commands
            if command[:2] == ("docker", "run") and "-d" in command
        )
        mount = run[run.index("--mount") + 1]
        self.assertEqual(
            mount,
            f"type=volume,src={volume.volume_name},dst={volume.target}",
        )
        self.assertNotIn(f"{fixture.root}:{fixture.root}", mount)
        self.assertNotIn(MOUNT_MARKER, run)

    def test_foreign_existing_volume_blocks_container_start(self):
        plan = ImageFixture(self.base, "fork", mounts=True).plan()
        volume = volume_plans(plan)[0]
        docker = FakeDocker()
        docker.volumes[volume.volume_name] = {
            "Name": volume.volume_name,
            "Labels": {**volume.labels, "sc.fork_identity": "foreign"},
        }
        with self.assertRaisesRegex(SandboxImageError, "is foreign"):
            docker_run(
                plan,
                (MOUNT_MARKER, plan.runtime_tag, "serve"),
                runner=docker,
            )
        self.assertFalse(
            [
                command
                for command in docker.commands
                if command[:2] == ("docker", "run") and "-d" in command
            ]
        )

    @staticmethod
    def _seed_runtime_image(plan, docker: FakeDocker, image_id: str = "e") -> None:
        docker.images[plan.runtime_tag] = {
            "Id": "sha256:" + image_id * 64,
            "Config": {"Labels": dict(plan.runtime_labels)},
        }
        docker.containers["sandbox"] = "sha256:" + image_id * 64

    def test_provision_receipt_reuses_exact_fingerprint_without_rerunning(self):
        fixture = ImageFixture(self.base, "fork", provision=True)
        plan = fixture.plan()
        docker = FakeDocker()
        self._seed_runtime_image(plan, docker)
        before = subprocess.run(
            ("git", "-C", str(fixture.root), "status", "--short"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout

        first = provision_checkout(plan, "sandbox", runner=docker, emit=False)
        second = provision_checkout(plan, "sandbox", runner=docker, emit=False)

        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(
            len([c for c in docker.commands if c[:2] == ("docker", "exec")]),
            1,
        )
        receipt = json.loads(Path(first["receipt"]).read_text())
        self.assertEqual(receipt["state"], "ready")
        self.assertEqual(receipt["fingerprint"], first["fingerprint"])
        after = subprocess.run(
            ("git", "-C", str(fixture.root), "status", "--short"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual(after, before)

    def test_failed_provision_retains_evidence_but_never_writes_success(self):
        fixture = ImageFixture(self.base, "fork", provision=True)
        plan = fixture.plan()
        docker = FakeDocker()
        docker.hook_status = 23
        self._seed_runtime_image(plan, docker)

        with self.assertRaises(ProvisionFailed) as raised:
            provision_checkout(plan, "sandbox", runner=docker, emit=False)

        self.assertEqual(raised.exception.status, 23)
        artifact = fixture.root / ".sc-state" / "local" / "dev-kit"
        self.assertFalse(list(artifact.glob("*/ready.json")))
        attempts = list(artifact.glob("*/attempts/*.json"))
        self.assertEqual(len(attempts), 1)
        metadata = json.loads(attempts[0].read_text())
        self.assertEqual(metadata["status"], 23)
        self.assertEqual(metadata["classification"], "hook_failure")
        log = attempts[0].with_suffix(".log").read_text()
        self.assertIn("provision stdout", log)
        self.assertIn("provision stderr", log)

    def test_unignored_artifact_root_fails_before_evidence_or_hook(self):
        fixture = ImageFixture(self.base, "fork", provision=True)
        (fixture.root / ".gitignore").write_text("# missing managed rule\n")
        plan = fixture.plan()
        docker = FakeDocker()
        self._seed_runtime_image(plan, docker)
        with self.assertRaisesRegex(SandboxImageError, "is not ignored"):
            provision_checkout(plan, "sandbox", runner=docker, emit=False)
        self.assertFalse((fixture.root / ".sc-state" / "local").exists())
        self.assertFalse(
            [command for command in docker.commands if command[:2] == ("docker", "exec")]
        )

    def test_fingerprint_covers_declared_automatic_image_seat_and_checkout_inputs(self):
        fixture = ImageFixture(self.base, "fork", provision=True)
        docker = FakeDocker()

        def fingerprint(current_fixture, *, seat="docker", image_id="e"):
            plan = current_fixture.plan()
            self._seed_runtime_image(plan, docker, image_id)
            return provisioning_fingerprint(
                provisioning_payload(plan, seat=seat, runner=docker)
            )

        values = {fingerprint(fixture)}
        (fixture.root / "requirements.lock").write_text("second\n")
        values.add(fingerprint(fixture))
        (fixture.subfloor / "provision").write_text("#!/bin/sh\necho changed\n")
        values.add(fingerprint(fixture))
        declaration_path = fixture.subfloor / "dev-kit.json"
        declaration = json.loads(declaration_path.read_text())
        (fixture.root / "work").mkdir()
        declaration["hooks"]["deps"]["argv"] = [
            "../.subfloor/provision",
            "--exact",
        ]
        declaration["hooks"]["deps"]["cwd"] = "work"
        declaration_path.write_text(json.dumps(declaration))
        values.add(fingerprint(fixture))
        values.add(fingerprint(fixture, seat="host"))
        values.add(fingerprint(fixture, image_id="f"))
        other = ImageFixture(self.base, "other", provision=True)
        values.add(fingerprint(other))
        self.assertEqual(len(values), 7)

    def test_concurrent_provisioners_execute_the_hook_once(self):
        fixture = ImageFixture(self.base, "fork", provision=True)
        plan = fixture.plan()
        docker = FakeDocker()
        docker.hook_delay = 0.2
        self._seed_runtime_image(plan, docker)
        results = []
        errors = []

        def provision():
            try:
                results.append(
                    provision_checkout(plan, "sandbox", runner=docker, emit=False)
                )
            except Exception as exc:  # noqa: BLE001 - captured for thread assertion
                errors.append(exc)

        threads = [threading.Thread(target=provision) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(result["reused"] for result in results), [False, True])
        self.assertEqual(
            len([c for c in docker.commands if c[:2] == ("docker", "exec")]),
            1,
        )

    def test_concurrent_launches_serialize_replacement_and_execute_setup_once(self):
        fixture = ImageFixture(self.base, "fork", mounts=True, provision=True)
        plan = fixture.plan()
        docker = FakeDocker()
        docker.hook_delay = 0.2
        self._seed_runtime_image(plan, docker)
        arguments = (
            "-d",
            "--name",
            "sandbox",
            MOUNT_MARKER,
            plan.runtime_tag,
            "serve",
        )
        results = []
        errors = []

        def launch():
            try:
                results.append(
                    launch_container(
                        plan, "sandbox", arguments, runner=docker, emit=False
                    )
                )
            except Exception as exc:  # noqa: BLE001 - captured for thread assertion
                errors.append(exc)

        threads = [threading.Thread(target=launch) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(result["reused"] for result in results), [False, True])
        self.assertEqual(
            len([c for c in docker.commands if c[:2] == ("docker", "exec")]),
            1,
        )
        main_runs = [
            command
            for command in docker.commands
            if command[:2] == ("docker", "run") and "-d" in command
        ]
        self.assertEqual(len(main_runs), 2)

    def test_readiness_requires_current_receipt_and_recovers_after_retry(self):
        fixture = ImageFixture(self.base, "fork", provision=True)
        plan = fixture.plan()
        docker = FakeDocker()
        self._seed_runtime_image(plan, docker)
        absent = readiness(plan, runner=docker)
        self.assertFalse(absent["ready"])
        provision_checkout(plan, "sandbox", runner=docker, emit=False)
        current = readiness(plan, runner=docker)
        self.assertTrue(current["ready"])
        (fixture.root / "requirements.lock").write_text("stale\n")
        stale_plan = fixture.plan()
        self._seed_runtime_image(stale_plan, docker)
        stale = readiness(stale_plan, runner=docker)
        self.assertFalse(stale["ready"])
        provision_checkout(stale_plan, "sandbox", runner=docker, emit=False)
        self.assertTrue(readiness(stale_plan, runner=docker)["ready"])

    def test_readiness_rejects_container_running_a_retagged_old_image(self):
        fixture = ImageFixture(self.base, "fork", provision=True)
        plan = fixture.plan()
        docker = FakeDocker()
        self._seed_runtime_image(plan, docker, "e")
        provision_checkout(plan, "sandbox", runner=docker, emit=False)
        docker.images[plan.runtime_tag] = {
            "Id": "sha256:" + "f" * 64,
            "Config": {"Labels": dict(plan.runtime_labels)},
        }
        with self.assertRaisesRegex(SandboxImageError, "runs .* current image"):
            readiness(plan, "sandbox", runner=docker)

    def test_cleanup_removes_only_exact_install_owned_extensions_and_volumes(self):
        left_fixture = ImageFixture(self.base, "left", mounts=True)
        right_fixture = ImageFixture(self.base, "right", mounts=True)
        left = left_fixture.plan()
        right = right_fixture.plan()
        docker = FakeDocker()
        for plan, image_id in ((left, "e"), (right, "f")):
            self._seed_runtime_image(plan, docker, image_id)
            docker_run(
                plan,
                (MOUNT_MARKER, plan.runtime_tag, "serve"),
                runner=docker,
            )
        base_id = "sha256:" + "b" * 64
        docker.images["shared-base"] = {
            "Id": base_id,
            "Config": {"Labels": {"sc.image_kind": "engine-base"}},
        }

        removed = cleanup_owned_resources(left_fixture.engine, runner=docker)

        self.assertEqual(len(removed), 2)
        self.assertNotIn(left.runtime_tag, docker.images)
        self.assertIn(right.runtime_tag, docker.images)
        self.assertIn("shared-base", docker.images)
        self.assertNotIn(volume_plans(left)[0].volume_name, docker.volumes)
        self.assertIn(volume_plans(right)[0].volume_name, docker.volumes)

    def test_no_build_rejects_every_image_identity_invalidator(self):
        fixture = ImageFixture(self.base, "fork")
        original = fixture.plan()
        docker = FakeDocker()
        self._seed_runtime_image(original, docker)
        self.assertEqual(preflight_image(original, runner=docker), original.runtime_tag)

        (fixture.state / "engine.ref").write_text("c" * 40 + "\n")
        changed_ref = fixture.plan()
        with self.assertRaises(SandboxImageError):
            preflight_image(changed_ref, runner=docker)
        (fixture.state / "engine.ref").write_text("a" * 40 + "\n")

        changed_epoch = image_plan(
            fixture.root,
            fixture.engine,
            "20260809T180000.000000Z",
            user="tester",
            uid="1000",
            gid="1000",
        )
        with self.assertRaises(SandboxImageError):
            preflight_image(changed_epoch, runner=docker)

        dockerfile = fixture.root / "container" / "Fork.Dockerfile"
        dockerfile.write_text(dockerfile.read_text() + "RUN echo changed\n")
        with self.assertRaises(SandboxImageError):
            preflight_image(fixture.plan(), runner=docker)

        dockerfile.write_text(dockerfile.read_text().removesuffix("RUN echo changed\n"))
        declaration = json.loads((fixture.subfloor / "dev-kit.json").read_text())
        declaration["hooks"] = {"test": {"argv": ["true"]}}
        (fixture.subfloor / "dev-kit.json").write_text(json.dumps(declaration))
        with self.assertRaises(SandboxImageError):
            preflight_image(fixture.plan(), runner=docker)

    def test_lock_timeout_is_non_ready_and_owner_release_allows_retry(self):
        fixture = ImageFixture(self.base, "fork", provision=True)
        plan = fixture.plan()
        docker = FakeDocker()
        self._seed_runtime_image(plan, docker)
        provision_checkout(plan, "sandbox", runner=docker, emit=False)
        (fixture.root / "requirements.lock").write_text("stale\n")
        stale = fixture.plan()
        self._seed_runtime_image(stale, docker)
        lock = next(
            (fixture.root / ".sc-state" / "local" / "dev-kit").glob(
                "*/provision.lock"
            )
        )
        descriptor = lock.open("r+")
        fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaisesRegex(SandboxImageError, "timed out waiting"):
                provision_checkout(
                    stale,
                    "sandbox",
                    runner=docker,
                    lock_timeout=0.01,
                    emit=False,
                )
        finally:
            fcntl.flock(descriptor.fileno(), fcntl.LOCK_UN)
            descriptor.close()
        retried = provision_checkout(stale, "sandbox", runner=docker, emit=False)
        self.assertFalse(retried["reused"])
        self.assertTrue(readiness(stale, runner=docker)["ready"])

    def test_failure_classification_matrix_never_creates_ready_receipt(self):
        expected = {
            23: "hook_failure",
            64: "invalid_configuration",
            78: "not_configured",
            126: "start_failure",
            137: "hook_failure",
        }
        for status, classification in expected.items():
            with self.subTest(status=status):
                fixture = ImageFixture(
                    self.base, f"failure-{status}", provision=True
                )
                plan = fixture.plan()
                docker = FakeDocker()
                docker.hook_status = status
                self._seed_runtime_image(plan, docker)
                with self.assertRaises(ProvisionFailed):
                    provision_checkout(plan, "sandbox", runner=docker, emit=False)
                artifact = fixture.root / ".sc-state" / "local" / "dev-kit"
                metadata_path = next(artifact.glob("*/attempts/*.json"))
                metadata = json.loads(metadata_path.read_text())
                self.assertEqual(metadata["classification"], classification)
                self.assertFalse(list(artifact.glob("*/ready.json")))

    def test_malformed_receipt_is_never_reused(self):
        fixture = ImageFixture(self.base, "fork", provision=True)
        plan = fixture.plan()
        docker = FakeDocker()
        self._seed_runtime_image(plan, docker)
        first = provision_checkout(plan, "sandbox", runner=docker, emit=False)
        Path(first["receipt"]).write_text("{broken")
        second = provision_checkout(plan, "sandbox", runner=docker, emit=False)
        self.assertFalse(second["reused"])
        self.assertEqual(
            len([c for c in docker.commands if c[:2] == ("docker", "exec")]),
            2,
        )

    def test_undeclared_files_do_not_affect_fingerprint_or_trigger_inference(self):
        fixture = ImageFixture(self.base, "fork", provision=True)
        plan = fixture.plan()
        docker = FakeDocker()
        self._seed_runtime_image(plan, docker)
        before = provisioning_fingerprint(
            provisioning_payload(plan, seat="docker", runner=docker)
        )
        (fixture.root / "package-lock.json").write_text('{"changed": true}\n')
        after = provisioning_fingerprint(
            provisioning_payload(fixture.plan(), seat="docker", runner=docker)
        )
        self.assertEqual(after, before)
        implementation = (SCRIPTS / "sandbox_devkit.py").read_text()
        for inferred_policy in ("requirements.txt", "package-lock", ".glob(", ".rglob("):
            self.assertNotIn(inferred_policy, implementation)

    def test_escaping_local_artifact_symlink_fails_before_any_write(self):
        fixture = ImageFixture(self.base, "fork", provision=True)
        outside = self.base / "outside-artifacts"
        outside.mkdir()
        (fixture.state / "local").symlink_to(outside, target_is_directory=True)
        plan = fixture.plan()
        docker = FakeDocker()
        self._seed_runtime_image(plan, docker)
        with self.assertRaisesRegex(SandboxImageError, "escapes the checkout"):
            provision_checkout(plan, "sandbox", runner=docker, emit=False)
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
