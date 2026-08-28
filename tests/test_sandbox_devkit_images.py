#!/usr/bin/env python3
"""Behavioral coverage for fork-extension sandbox image identity."""
from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
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
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run as run_mod  # noqa: E402
import sandbox_devkit  # noqa: E402
from sandbox_devkit import (  # noqa: E402
    MOUNT_MARKER,
    ProvisionFailed,
    SandboxImageError,
    _emit_image_state,
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
        self.inputs: list[str | bytes | None] = []
        self.images: dict[str, dict] = {
            "python:3.14-slim": {
                "Id": "sha256:" + "a" * 64,
                "Created": "2026-08-01T00:00:00Z",
                "Config": {"Labels": {}},
            }
        }
        self.volumes: dict[str, dict] = {}
        self.containers: dict[str, str] = {}
        self.image_remove_failures: set[str] = set()
        self.build_counter = 0
        self.package_build_status = 0
        self.package_versions: dict[str, str] = {}
        self.package_architectures: dict[str, str] = {}
        self.package_statuses: dict[str, str] = {}
        self.hook_status = 0
        self.hook_delay = 0.0

    def __call__(
        self, command, *, check, text, capture_output=False, input=None, timeout=None
    ):
        self.assert_protocol(check, text)
        command = tuple(command)
        self.commands.append(command)
        self.inputs.append(input)
        if command[:2] == ("docker", "pull"):
            return subprocess.CompletedProcess(command, 0, "pulled\n", "")
        if command[:2] == ("docker", "build"):
            tag = command[command.index("-t") + 1]
            if "-package-layer-" in tag and self.package_build_status:
                return subprocess.CompletedProcess(
                    command, self.package_build_status, "", "package build failed"
                )
            labels = {}
            for index, value in enumerate(command):
                if value == "--label":
                    key, label_value = command[index + 1].split("=", 1)
                    labels[key] = label_value
            kind = (
                "b"
                if "-base:" in tag
                else "k"
                if "-package-layer-" in tag
                else "p"
                if "-packages-" in tag
                else "e"
            )
            image_id = "sha256:" + kind * 64
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
            if "/usr/bin/dpkg-query" in command:
                format_index = next(
                    index
                    for index, value in enumerate(command)
                    if value.startswith("--showformat=")
                )
                names = command[format_index + 1 :]
                rows = [
                    "\t".join((
                        name,
                        self.package_architectures.get(name, "amd64"),
                        self.package_versions.get(name, "1.0"),
                        self.package_statuses.get(name, "install ok installed"),
                    ))
                    for name in names
                ]
                return subprocess.CompletedProcess(command, 0, "\n".join(rows) + "\n", "")
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
            value = "true\t" + image if "State.Running" in command[3] else image
            return subprocess.CompletedProcess(command, 0, value + "\n", "")
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
        if check is not False or text not in {True, False}:
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
        packages: list[str] | None = None,
    ) -> None:
        self.root = parent / name
        self.engine = self.root / ".super-coder"
        self.subfloor = self.root / ".subfloor"
        self.state = self.root / ".sc-state"
        self.context = self.root / "container"
        self.engine.mkdir(parents=True)
        (self.engine / "assets").mkdir()
        self.subfloor.mkdir()
        self.state.mkdir()
        self.context.mkdir(parents=True)
        (self.engine / "Dockerfile").write_text("FROM scratch\n")
        (self.engine / "assets" / "github_known_hosts").write_text(
            "github.com ssh-ed25519 AAAATEST\n"
        )
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
            dockerfile = self.context / "Fork.Dockerfile"
            dockerfile.write_text(
                "ARG SC_BASE_IMAGE\n"
                "FROM busybox AS source\n"
                "RUN echo source > /payload\n"
                "FROM ${SC_BASE_IMAGE}\n"
                "COPY --from=source /payload /payload\n"
            )
            declaration["sandbox"] = {
                "dockerfile": "container/Fork.Dockerfile",
                "context": "container",
            }
            if mounts:
                declaration["sandbox"]["mounts"] = [
                    {"name": "python-env", "target": ".venv"}
                ]
        if packages is not None:
            declaration.setdefault("sandbox", {})["packages"] = {"apt": packages}
        (self.subfloor / "dev-kit.json").write_text(json.dumps(declaration))
        (self.root / ".gitignore").write_text("/.sc-state/local/\n")
        subprocess.run(("git", "init", "-q", str(self.root)), check=True)
        subprocess.run(("git", "-C", str(self.root), "add", "."), check=True)
        subprocess.run(
            (
                "git", "-C", str(self.root), "-c", "user.name=Test",
                "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture",
            ),
            check=True,
        )

    def plan(self):
        return image_plan(
            self.root,
            self.engine,
            "20260809T170000.000000Z",
            user="tester",
            uid="1000",
            gid="1000",
        )

    def commit(self, message: str) -> None:
        subprocess.run(("git", "-C", str(self.root), "add", "."), check=True)
        subprocess.run(
            (
                "git", "-C", str(self.root), "-c", "user.name=Test",
                "-c", "user.email=test@example.invalid", "commit", "-qm", message,
            ),
            check=True,
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
        dockerfile = fixture.context / "Fork.Dockerfile"
        for text in (
            "FROM python:3.12\n",
            "ARG SC_BASE_IMAGE\nFROM python:3.12\n",
            "FROM ${SC_BASE_IMAGE}\nARG SC_BASE_IMAGE\n",
        ):
            with self.subTest(text=text):
                dockerfile.write_text(text)
                with self.assertRaisesRegex(SandboxImageError, "SC_BASE_IMAGE"):
                    fixture.plan()

    def test_extension_base_arg_must_be_global_before_first_stage(self):
        fixture = ImageFixture(self.base, "stage-local-arg")
        dockerfile = fixture.context / "Fork.Dockerfile"
        dockerfile.write_text(
            "FROM busybox AS source\n"
            "ARG SC_BASE_IMAGE\n"
            "FROM ${SC_BASE_IMAGE}\n"
        )

        with self.assertRaisesRegex(
            SandboxImageError,
            r"ARG SC_BASE_IMAGE must be global \(before the first FROM\)",
        ):
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

    def test_build_uses_local_base_tag_exact_context_and_required_labels(self):
        fixture = ImageFixture(self.base, "fork")
        plan = fixture.plan()
        docker = FakeDocker()

        result = build_images(plan, runner=docker)

        self.assertEqual(result, plan.runtime_tag)
        self.assertEqual(len(docker.builds()), 2)
        base, extension = docker.builds()
        self.assertEqual(base[-1], str(fixture.root.resolve()))
        self.assertEqual(extension[-1], "-")
        extension_input = docker.inputs[docker.commands.index(extension)]
        self.assertIsInstance(extension_input, bytes)
        self.assertGreater(len(extension_input), 1024)
        trust = (fixture.engine / "assets" / "github_known_hosts").read_bytes()
        self.assertIn(
            "SC_GITHUB_HOST_TRUST_B64=" + base64.b64encode(trust).decode("ascii"),
            base,
        )
        self.assertIn(
            "SC_GITHUB_HOST_TRUST_SHA256=" + hashlib.sha256(trust).hexdigest(),
            base,
        )
        self.assertIn("SC_PARENT_IMAGE=python:3.14-slim", base)
        self.assertNotIn("SC_PARENT_IMAGE=sha256:" + "a" * 64, base)
        self.assertIn("sc.parent_id=sha256:" + "a" * 64, base)
        self.assertIn(f"SC_BASE_IMAGE={plan.base_tag}", extension)
        self.assertNotIn("SC_BASE_IMAGE=sha256:" + "b" * 64, extension)
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
                "sc.package_digest",
                "sc.build_identity",
                "sc.readiness_contract",
                "sc.package_contract",
                "sc.context_contract",
            },
        )

    def test_no_build_rejects_foreign_label_and_accepts_exact_image(self):
        plan = ImageFixture(self.base, "fork").plan()
        docker = FakeDocker()
        build_images(plan, runner=docker)
        exact_labels = dict(docker.images[plan.runtime_tag]["Config"]["Labels"])
        docker.images[plan.runtime_tag]["Config"]["Labels"]["sc.fork_identity"] = "other"
        with self.assertRaisesRegex(SandboxImageError, "stale or foreign"):
            preflight_image(plan, runner=docker)
        docker.images[plan.runtime_tag]["Config"]["Labels"] = exact_labels
        self.assertEqual(preflight_image(plan, runner=docker), plan.runtime_tag)

    def test_package_only_build_uses_canonical_argv_and_writes_exact_proof(self):
        fixture = ImageFixture(
            self.base,
            "packages",
            sandbox=False,
            packages=["jq=1.6-2.1", "curl"],
        )
        plan = fixture.plan()
        docker = FakeDocker()
        docker.package_versions.update({"curl": "7.88.1-10", "jq": "1.6-2.1"})

        selected = build_images(plan, runner=docker)

        self.assertEqual(selected, plan.package_tag)
        self.assertEqual([package.atom for package in plan.packages], ["curl", "jq=1.6-2.1"])
        package_build = next(
            command for command in docker.builds() if "-package-layer-" in command[command.index("-t") + 1]
        )
        self.assertIn(f"SC_BASE_IMAGE={plan.base_tag}", package_build)
        self.assertNotIn("SC_BASE_IMAGE=sha256:" + "b" * 64, package_build)
        dockerfile = docker.inputs[docker.commands.index(package_build)]
        self.assertEqual(
            dockerfile,
            "ARG SC_BASE_IMAGE\n"
            "FROM ${SC_BASE_IMAGE}\n"
            "USER root\n"
            'RUN ["apt-get", "update"]\n'
            'RUN ["apt-get","install","-y","--no-install-recommends","curl","jq=1.6-2.1"]\n'
            'RUN ["sh", "-c", "rm -rf /var/lib/apt/lists/*"]\n'
            "USER tester\n",
        )
        runtime_build = next(
            command for command in docker.builds() if "-packages-" in command[command.index("-t") + 1]
        )
        self.assertIn(f"SC_BASE_IMAGE={plan.package_layer_tag}", runtime_build)
        self.assertNotIn("SC_BASE_IMAGE=sha256:" + "k" * 64, runtime_build)
        self.assertIn("sc.package_layer_id=sha256:" + "k" * 64, runtime_build)
        proof_command = next(
            command
            for command in docker.commands
            if "/usr/bin/dpkg-query" in command
        )
        self.assertEqual(proof_command[-2:], ("curl", "jq"))
        artifact = next(
            (fixture.state / "local" / "dev-kit").glob("*/package-proof.json")
        )
        proof = json.loads(artifact.read_text())
        self.assertEqual(proof["requested"], ["curl", "jq=1.6-2.1"])
        self.assertEqual(
            [row["version"] for row in proof["observed"]],
            ["7.88.1-10", "1.6-2.1"],
        )
        receipt = json.loads(artifact.with_name("ready.json").read_text())
        self.assertEqual(receipt["format_version"], 2)
        self.assertTrue(receipt["source_tracked_clean"])
        self.assertEqual(preflight_image(plan, runner=docker), plan.package_tag)

    def test_extension_build_uses_local_package_layer_tag(self):
        fixture = ImageFixture(self.base, "package-extension", packages=["curl"])
        plan = fixture.plan()
        docker = FakeDocker()

        self.assertEqual(build_images(plan, runner=docker), plan.runtime_tag)

        extension_build = next(
            command
            for command in docker.builds()
            if command[command.index("-t") + 1] == plan.runtime_tag
        )
        self.assertIn(f"SC_BASE_IMAGE={plan.package_layer_tag}", extension_build)
        self.assertNotIn("SC_BASE_IMAGE=sha256:" + "k" * 64, extension_build)
        self.assertIn("sc.package_layer_id=sha256:" + "k" * 64, extension_build)

    def test_pinned_version_mismatch_falls_back_to_fresh_engine_baseline(self):
        fixture = ImageFixture(
            self.base,
            "pin-mismatch",
            sandbox=False,
            packages=["curl=8.0"],
        )
        plan = fixture.plan()
        docker = FakeDocker()
        docker.package_versions["curl"] = "7.0"

        selected = build_images(plan, runner=docker)

        self.assertEqual(selected, plan.base_tag)
        status_path = next(
            (fixture.state / "local" / "dev-kit").glob("*/status.json")
        )
        status = json.loads(status_path.read_text())
        self.assertEqual(status["core_runtime"], "ready")
        self.assertEqual(status["native_packages"], "advisory")
        self.assertEqual(status["selected_runtime"], "engine_baseline")
        self.assertEqual(status["cutover"], "baseline_fallback")
        self.assertIn("version mismatch", status["detail"])
        self.assertTrue(
            list((fixture.state / "local" / "runtime-flags" / "pending").glob("*.json"))
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _emit_image_state(plan, selected, "built")
        self.assertIn(
            "dev-kit image state: advisory — native_package_candidate; "
            f"selected engine_baseline {plan.base_tag}",
            output.getvalue(),
        )
        self.assertNotIn("image state: ready", output.getvalue())

    def test_package_failure_preserves_a_healthy_running_runtime_without_cutover(self):
        fixture = ImageFixture(
            self.base,
            "preserve-runtime",
            sandbox=False,
            packages=["curl"],
        )
        plan = fixture.plan()
        docker = FakeDocker()
        existing_id = "sha256:" + "9" * 64
        docker.containers["sandbox"] = existing_id
        docker.package_build_status = 100

        selected = build_images(plan, runner=docker, container="sandbox")

        self.assertEqual(selected, plan.runtime_tag)
        status_path = next(
            (fixture.state / "local" / "dev-kit").glob("*/status.json")
        )
        status = json.loads(status_path.read_text())
        self.assertEqual(status["selected_runtime"], "existing_unchanged")
        self.assertEqual(status["selected_image_id"], existing_id)
        self.assertEqual(status["cutover"], "unchanged")
        self.assertNotIn(("docker", "rm", "-f", "sandbox"), docker.commands)
        self.assertFalse(
            [
                command
                for command in docker.commands
                if command[:2] == ("docker", "run") and "--name" in command
            ]
        )

    def test_invalid_package_contract_is_advisory_after_baseline_proof(self):
        fixture = ImageFixture(
            self.base,
            "invalid-packages",
            sandbox=False,
            packages=["curl:amd64"],
        )
        plan = fixture.plan()
        self.assertTrue(plan.has_package_contract)
        self.assertEqual(plan.package_digest, "invalid")
        self.assertIn("name must match", plan.candidate_error)
        docker = FakeDocker()

        self.assertEqual(build_images(plan, runner=docker), plan.base_tag)
        self.assertEqual(len(docker.builds()), 1)
        self.assertTrue(
            [
                command
                for command in docker.commands
                if command[:2] == ("docker", "run") and "--network" in command
            ]
        )

    def test_no_build_ignores_untracked_context_but_rejects_tracked_drift(self):
        fixture = ImageFixture(self.base, "context-packages", packages=["curl"])
        plan = fixture.plan()
        docker = FakeDocker()
        build_images(plan, runner=docker)
        (fixture.context / "untracked.tmp").write_text("ignored by identity\n")
        self.assertEqual(preflight_image(plan, runner=docker), plan.runtime_tag)

        tracked = fixture.context / "Fork.Dockerfile"
        tracked.write_text(tracked.read_text() + "RUN echo changed\n")
        changed = fixture.plan()
        self.assertEqual(preflight_image(changed, runner=docker), changed.base_tag)
        status_path = next(
            (fixture.state / "local" / "dev-kit").glob("*/status.json")
        )
        status = json.loads(status_path.read_text())
        self.assertEqual(status["native_packages"], "advisory")
        self.assertEqual(status["classification"], "stale_no_build")

    def test_combined_packages_and_provision_require_both_receipt_layers(self):
        fixture = ImageFixture(
            self.base,
            "packages-provision",
            sandbox=False,
            packages=["curl"],
            provision=True,
        )
        plan = fixture.plan()
        docker = FakeDocker()

        selected = build_images(plan, runner=docker)
        status_path = next(
            (fixture.state / "local" / "dev-kit").glob("*/status.json")
        )
        self.assertEqual(json.loads(status_path.read_text())["package_receipt"], "pending")
        self.assertFalse(status_path.with_name("ready.json").exists())

        result = launch_container(
            plan,
            "sandbox",
            ("-d", "--name", "sandbox", MOUNT_MARKER, selected, "serve"),
            runner=docker,
            emit=False,
        )
        self.assertEqual(result["state"], "ready")
        receipt = json.loads(Path(result["receipt"]).read_text())
        self.assertEqual(receipt["packages"]["requested"], ["curl"])
        self.assertEqual(receipt["provision"]["name"], "deps")
        self.assertEqual(
            len([command for command in docker.commands if command[:2] == ("docker", "exec")]),
            1,
        )

    def test_package_advisory_skips_dependent_provision_but_runs_core_baseline(self):
        fixture = ImageFixture(
            self.base,
            "packages-provision-failure",
            sandbox=False,
            packages=["curl"],
            provision=True,
        )
        plan = fixture.plan()
        docker = FakeDocker()
        docker.package_build_status = 100
        docker.hook_status = 23

        self.assertEqual(build_images(plan, runner=docker), plan.base_tag)
        result = launch_container(
            plan,
            "sandbox",
            ("-d", "--name", "sandbox", MOUNT_MARKER, plan.base_tag, "serve"),
            runner=docker,
            emit=False,
        )
        self.assertEqual(result["state"], "advisory")
        self.assertEqual(result["core_runtime"], "ready")
        self.assertFalse(
            [command for command in docker.commands if command[:2] == ("docker", "exec")]
        )

    def test_package_evidence_persistence_failure_does_not_block_core(self):
        fixture = ImageFixture(
            self.base,
            "package-evidence-failure",
            sandbox=False,
            packages=["curl"],
        )
        (fixture.root / ".gitignore").write_text("# managed rule removed\n")
        fixture.commit("remove local evidence ignore rule")
        plan = fixture.plan()
        errors = io.StringIO()

        with contextlib.redirect_stderr(errors):
            selected = build_images(plan, runner=FakeDocker())

        self.assertEqual(selected, plan.base_tag)
        self.assertIn("advisory persistence", errors.getvalue())
        self.assertFalse((fixture.state / "local" / "dev-kit").exists())

    def test_malformed_advisory_api_success_remains_pending_without_blocking_core(
        self,
    ):
        fixture = ImageFixture(
            self.base,
            "malformed-advisory-api",
            sandbox=False,
            packages=["curl"],
        )
        (fixture.engine / "instance.json").write_text(json.dumps({"port": 8837}))
        plan = fixture.plan()
        docker = FakeDocker()
        docker.package_build_status = 100

        with mock.patch.object(
            sandbox_devkit.runtime_flags,
            "put_via_api",
            side_effect=sandbox_devkit.runtime_flags.RuntimeFlagError(
                "runtime advisory API returned a malformed success response"
            ),
        ):
            selected = build_images(plan, runner=docker)

        self.assertEqual(selected, plan.base_tag)
        pending = list(
            (fixture.state / "local" / "runtime-flags" / "pending").glob("*.json")
        )
        self.assertEqual(len(pending), 1)
        status_path = next(
            (fixture.state / "local" / "dev-kit").glob("*/status.json")
        )
        status = json.loads(status_path.read_text())
        self.assertEqual(status["core_runtime"], "ready")
        self.assertEqual(status["native_packages"], "advisory")
        self.assertEqual(status["advisory"]["state"], "pending")

    def test_success_after_advisory_writes_higher_generation_clearance_intent(self):
        fixture = ImageFixture(
            self.base,
            "package-clearance",
            sandbox=False,
            packages=["curl"],
        )
        plan = fixture.plan()
        docker = FakeDocker()
        docker.package_build_status = 100
        self.assertEqual(build_images(plan, runner=docker), plan.base_tag)

        docker.package_build_status = 0
        self.assertEqual(build_images(plan, runner=docker), plan.package_tag)

        pending = sorted(
            (fixture.state / "local" / "runtime-flags" / "pending").glob("*.json")
        )
        self.assertEqual(len(pending), 2)
        bodies = [json.loads(path.read_text())["body"] for path in pending]
        self.assertEqual([body["generation"] for body in bodies], [1, 2])
        self.assertEqual([body["state"] for body in bodies], ["open", "resolved"])
        self.assertEqual(bodies[1]["clearance"]["failed_generation"], 1)

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
        old_runtime_id = "sha256:" + "d" * 64
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
        docker.images[plan.base_tag] = {
            "Id": "sha256:" + "b" * 64,
            "Config": {"Labels": {
                **plan.base_labels,
                "sc.parent_id": "sha256:" + "a" * 64,
            }},
        }
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

    def test_provision_only_receipt_sets_and_restores_ready_status(self):
        fixture = ImageFixture(
            self.base,
            "provision-only-status",
            sandbox=False,
            provision=True,
        )
        plan = fixture.plan()
        docker = FakeDocker()
        selected = build_images(plan, runner=docker)
        status_path = next(
            (fixture.state / "local" / "dev-kit").glob("*/status.json")
        )
        before = json.loads(status_path.read_text())
        self.assertEqual(selected, plan.base_tag)
        self.assertEqual(before["fork_readiness"], "degraded")
        self.assertEqual(before["package_receipt"], "pending")
        docker.containers["sandbox"] = docker.images[plan.base_tag]["Id"]

        first = provision_checkout(plan, "sandbox", runner=docker, emit=False)
        ready = json.loads(status_path.read_text())
        self.assertFalse(first["reused"])
        self.assertEqual(ready["fork_readiness"], "ready")
        self.assertEqual(
            ready["package_receipt"],
            {"fingerprint": first["fingerprint"], "path": first["receipt"]},
        )

        ready["fork_readiness"] = "not_declared"
        status_path.write_text(json.dumps(ready))
        second = provision_checkout(plan, "sandbox", runner=docker, emit=False)
        restored = json.loads(status_path.read_text())
        self.assertTrue(second["reused"])
        self.assertEqual(restored["fork_readiness"], "ready")
        self.assertEqual(
            len(
                [
                    command
                    for command in docker.commands
                    if command[:2] == ("docker", "exec")
                ]
            ),
            1,
        )

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
        fixture.commit("reviewed input update")
        stale_plan = fixture.plan()
        self._seed_runtime_image(stale_plan, docker)
        stale = readiness(stale_plan, runner=docker)
        self.assertFalse(stale["ready"])
        provision_checkout(stale_plan, "sandbox", runner=docker, emit=False)
        self.assertTrue(readiness(stale_plan, runner=docker)["ready"])

    def test_boot_inventory_rejects_content_only_provision_input_drift(self):
        fixture = ImageFixture(
            self.base,
            "boot-input-drift",
            sandbox=False,
            provision=True,
        )
        plan = fixture.plan()
        docker = FakeDocker()
        build_images(plan, runner=docker)
        docker.containers["sandbox"] = docker.images[plan.base_tag]["Id"]
        provision_checkout(plan, "sandbox", runner=docker, emit=False)

        with mock.patch.object(run_mod, "ENGINE", fixture.engine):
            current = run_mod.collect_dev_tools(
                fixture.root, "container", environment={"PATH": "/usr/bin"}
            )
            self.assertEqual(current["state"], "ready")

            (fixture.root / "requirements.lock").write_text("changed bytes\n")
            stale = run_mod.collect_dev_tools(
                fixture.root, "container", environment={"PATH": "/usr/bin"}
            )
            self.assertEqual(stale["state"], "stale")

    def test_boot_inventory_rejects_ready_receipt_image_label_mismatch(self):
        fixture = ImageFixture(
            self.base,
            "boot-image-mismatch",
            sandbox=False,
            provision=True,
        )
        plan = fixture.plan()
        docker = FakeDocker()
        build_images(plan, runner=docker)
        docker.containers["sandbox"] = docker.images[plan.base_tag]["Id"]
        provision_checkout(plan, "sandbox", runner=docker, emit=False)
        ready_path = next(
            (fixture.root / ".sc-state" / "local" / "dev-kit").glob(
                "*/ready.json"
            )
        )
        receipt = json.loads(ready_path.read_text())
        receipt["image"]["labels"]["sc.engine_ref"] = "stale"
        ready_path.write_text(json.dumps(receipt))

        with mock.patch.object(run_mod, "ENGINE", fixture.engine):
            inventory = run_mod.collect_dev_tools(
                fixture.root, "container", environment={"PATH": "/usr/bin"}
            )
        self.assertEqual(inventory["state"], "stale")

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
        build_images(original, runner=docker)
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

        dockerfile = fixture.context / "Fork.Dockerfile"
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
        fixture.commit("reviewed lock update")
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
