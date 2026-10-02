"""Boundary tests for the maintainer exact-source GUI seat.

No live engine, native inference, browser profile, or systemd service is used
by these tests. Real Linux/browser acceptance is separately retained host evidence.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import socket
import subprocess
import sys
import tarfile
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("gui_experiment", ROOT / "maintainer/gui_experiment.py")
assert SPEC and SPEC.loader
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


@pytest.fixture
def seat(tmp_path, monkeypatch):
    registry = tmp_path / "registry"
    monkeypatch.setattr(fixture, "REGISTRY", registry)
    monkeypatch.delenv("SC_DEV_PORT", raising=False)
    return tmp_path


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def source_repo(seat):
    repo = seat / "source"
    repo.mkdir()
    git(repo, "init", "-q")
    for name in fixture.SOURCE_FILES:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("committed source\n")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", "fixture source")
    return repo


def marked(seat):
    fid = "a" * 32
    root = seat / f"{fixture.PREFIX}{fid}"
    root.mkdir(mode=0o700)
    info = root.stat()
    receipt = seat / "receipt.json"
    record = {
        "fixture_id": fid, "source_sha": "b" * 40, "archive_sha256": "c" * 64,
        "root": str(root), "root_device": info.st_dev, "root_inode": info.st_ino,
        "unit": f"{fixture.PREFIX}{fid}.service", "ownership_nonce": "d" * 64,
        "runtime": "none", "port": 8800, "limits": fixture.validate_limits(10, 64, 8),
        "bootstrap_sha256": hashlib.sha256(b"fixture bootstrap").hexdigest(),
        "receipt": str(receipt), "status": "preparing", "cleanup": {"complete": False},
    }
    fixture.write_json(root / fixture.MARKER, fixture.identity(record))
    (root / "fixture_bootstrap.py").write_bytes(b"fixture bootstrap")
    fixture.save(record, receipt)
    return record, root, receipt


def missing_state():
    return {"LoadState": "not-found", "ActiveState": "inactive", "MainPID": "0"}


def test_archive_excludes_dirty_edits_and_moving_ref(seat):
    repo = source_repo(seat)
    sha, archive = fixture.archive(repo, "HEAD")
    path = repo / fixture.SOURCE_FILES[0]
    path.write_text("dirty edit\n")
    # Movement after resolution cannot change bytes already materialized.
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", "moved ref")
    assert git(repo, "rev-parse", "HEAD") != sha
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        assert tar.extractfile(fixture.SOURCE_FILES[0]).read() == b"committed source\n"
    old_sha, old_archive = fixture.archive(repo, sha)
    assert old_sha == sha and old_archive == archive
    path.write_text("another dirty edit\n")
    new_sha, new_archive = fixture.archive(repo, "HEAD")
    assert new_sha != sha
    with tarfile.open(fileobj=io.BytesIO(new_archive)) as tar:
        assert tar.extractfile(fixture.SOURCE_FILES[0]).read() == b"dirty edit\n"


def test_unresolved_ref_and_missing_source_fail_without_service(seat, monkeypatch):
    repo = source_repo(seat)
    with pytest.raises(fixture.FixtureError, match="does not resolve"):
        fixture.archive(repo, "--bad-ref")
    (repo / fixture.SOURCE_FILES[0]).unlink()
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", "incomplete source")
    real_command = fixture.command
    launched = []

    def command(argv, **kwargs):
        if argv[0] == "systemctl":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "systemd-run":
            launched.append(argv)
        return real_command(argv, **kwargs)

    monkeypatch.setattr(fixture, "command", command)
    monkeypatch.setattr(fixture, "unit_state", lambda record: missing_state())
    receipt = seat / "failure.json"
    with pytest.raises(fixture.FixtureError, match="required source file"):
        fixture.start(repo, "HEAD", receipt, temp_parent=seat)
    record = fixture.read_json(receipt)
    assert record["status"] == "failed"
    assert record["error_code"] == "SOURCE_INVALID"
    assert record["cleanup"]["complete"] and not Path(record["root"]).exists()
    assert not launched


def test_environment_drops_all_control_plane_and_provider_state(monkeypatch):
    for key in ("SC_API_TOKEN", "SC_API_BASE", "SC_ROOT", "SC_SHELL_ID",
                "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CODEX_HOME", "HOME",
                "PYTHONPATH", "PYTHONHOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        monkeypatch.setenv(key, "unrelated-sentinel")
    monkeypatch.setenv("PATH", "/usr/bin")
    cleaned = fixture.clean_environment()
    assert cleaned["PATH"] == "/usr/bin"
    assert not any(key.startswith("SC_") for key in cleaned)
    assert "HOME" not in cleaned and "OPENAI_API_KEY" not in cleaned
    assert "PYTHONPATH" not in cleaned and "CODEX_HOME" not in cleaned


@pytest.mark.parametrize("changed", ["root", "unit", "source_sha", "ownership_nonce", "root_inode"])
def test_corrupt_receipt_cannot_redirect_cleanup(seat, changed):
    _, root, receipt = marked(seat)
    neighbor = seat / "unrelated"
    neighbor.mkdir()
    sentinel = neighbor / "keep"
    sentinel.write_text("unrelated")
    public = fixture.read_json(receipt)
    public[changed] = str(neighbor) if changed == "root" else "altered"
    fixture.write_json(receipt, public)
    with mock.patch.object(fixture, "command") as command:
        with pytest.raises(fixture.FixtureError, match="ownership ledger"):
            fixture.stop(receipt)
        command.assert_not_called()
    assert sentinel.read_text() == "unrelated" and root.exists()


def test_corrupt_or_symlink_marker_refuses_before_stop(seat):
    _, root, receipt = marked(seat)
    marker = root / fixture.MARKER
    marker.unlink()
    marker.symlink_to(receipt)
    with mock.patch.object(fixture, "command") as command:
        with pytest.raises(fixture.FixtureError, match="owner-only regular file"):
            fixture.stop(receipt)
        command.assert_not_called()
    assert root.exists()


def test_replaced_root_inode_refuses_cleanup(seat):
    _, root, receipt = marked(seat)
    root.rename(seat / "old-root")
    root.mkdir(mode=0o700)
    with mock.patch.object(fixture, "command") as command:
        with pytest.raises(fixture.FixtureError, match="root was replaced"):
            fixture.stop(receipt)
        command.assert_not_called()
    assert root.exists()


def test_symlink_root_and_traversal_identity_cannot_select_other_state(seat):
    _, root, receipt = marked(seat)
    root.rename(seat / "saved-root")
    root.symlink_to(seat / "saved-root", target_is_directory=True)
    with pytest.raises(fixture.FixtureError, match="root was replaced"):
        fixture.stop(receipt)
    public = fixture.read_json(receipt)
    public["fixture_id"] = "../../unrelated"
    fixture.write_json(receipt, public)
    with pytest.raises(fixture.FixtureError, match="invalid fixture identity"):
        fixture.stop(receipt)
    assert (seat / "saved-root").exists()


def test_copied_valid_marker_cannot_bootstrap_another_root(seat):
    _, root, _ = marked(seat)
    other = seat / "adjacent-root"
    other.mkdir(mode=0o700)
    fixture.write_json(other / fixture.MARKER, fixture.read_json(root / fixture.MARKER))
    with pytest.raises(fixture.FixtureError, match="caller root differs"):
        fixture.verified_bootstrap_root(other, other / "fixture_bootstrap.py")
    assert not (other / ".super-coder").exists()


def test_bootstrap_bytes_and_retained_root_are_bound(seat):
    _, root, _ = marked(seat)
    script = root / "fixture_bootstrap.py"
    verified, _ = fixture.verified_bootstrap_root(root, script)
    assert verified == root
    script.write_text("changed helper")
    with pytest.raises(fixture.FixtureError, match="helper identity"):
        fixture.verified_bootstrap_root(root, script)


def test_receipt_parent_alias_has_same_canonical_identity(seat):
    alias = seat / "alias"
    alias.symlink_to(seat, target_is_directory=True)
    assert fixture.canonical_receipt(alias / "new.json") == seat / "new.json"


def test_concurrent_starts_through_parent_alias_cannot_replace_receipt(seat):
    repo = source_repo(seat)
    alias = seat / "alias"
    alias.symlink_to(seat, target_is_directory=True)
    script = """
import importlib.util, pathlib, subprocess, sys
spec=importlib.util.spec_from_file_location('gui_fixture',sys.argv[1])
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.REGISTRY=pathlib.Path(sys.argv[2])
actual=m.command
m.command=lambda argv,**kwargs: (subprocess.CompletedProcess(argv,0,'','')
    if argv[0]=='systemctl' else actual(argv,**kwargs))
m.unit_state=lambda _: {'LoadState':'not-found','ActiveState':'inactive','MainPID':'0'}
try:
    m.start(pathlib.Path(sys.argv[3]),'HEAD',pathlib.Path(sys.argv[4]),
            temp_parent=pathlib.Path(sys.argv[5]),runtime='experimental')
except m.FixtureError as exc:
    print(exc.code)
"""
    children = [subprocess.Popen([sys.executable, "-c", script,
                                 str(ROOT / "maintainer/gui_experiment.py"),
                                 str(fixture.REGISTRY), str(repo), str(receipt), str(seat)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for receipt in (seat / "same.json", alias / "same.json")]
    outcomes = []
    for child in children:
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr.decode()
        outcomes.append(stdout.decode().strip())
    assert sorted(outcomes) == ["INPUT_INVALID", "RUNTIME_UNAVAILABLE"]
    record = fixture.read_json(seat / "same.json")
    assert record["cleanup"]["complete"]
    assert len(list(fixture.REGISTRY.glob("*.json"))) == 1


def test_concurrent_stops_serialize_real_root_cleanup(seat):
    _, root, receipt = marked(seat)
    script = """
import importlib.util, pathlib, sys
spec=importlib.util.spec_from_file_location('gui_fixture',sys.argv[1])
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.REGISTRY=pathlib.Path(sys.argv[2])
m.unit_state=lambda _: {'LoadState':'not-found','ActiveState':'inactive','MainPID':'0'}
assert m.stop(pathlib.Path(sys.argv[3]))['cleanup']['complete']
"""
    children = [subprocess.Popen([sys.executable, "-c", script,
                                 str(ROOT / "maintainer/gui_experiment.py"),
                                 str(fixture.REGISTRY), str(receipt)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for _ in range(2)]
    for child in children:
        _, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr.decode()
    assert not root.exists() and fixture.read_json(receipt)["cleanup"]["complete"]


def test_foreign_unit_refuses_before_systemctl_stop(seat, monkeypatch):
    _, root, receipt = marked(seat)
    monkeypatch.setattr(fixture, "unit_state", lambda _: {
        "LoadState": "loaded", "ActiveState": "active", "Description": "foreign service"})
    with mock.patch.object(fixture, "command") as command:
        with pytest.raises(fixture.FixtureError, match="unit identity differs"):
            fixture.stop(receipt)
        command.assert_not_called()
    assert root.exists()


def test_stopped_cleanup_and_repeated_stop_keep_verified_receipts(seat, monkeypatch):
    _, root, receipt = marked(seat)
    monkeypatch.setattr(fixture, "unit_state", lambda _: missing_state())
    first = fixture.stop(receipt)
    assert first["cleanup"]["complete"] and not root.exists()
    assert first["cleanup"]["cgroup_empty"]
    second = fixture.stop(receipt)
    assert second["cleanup"]["complete"] and second["cleanup"]["already_stopped"]
    assert fixture.ledger_path(first["fixture_id"]).exists() and receipt.exists()


def test_surviving_process_keeps_state_and_failure_evidence(seat, monkeypatch):
    record, root, receipt = marked(seat)
    record.update({"main_pid": os.getpid(), "main_pid_start_ticks": fixture.process_start_ticks(os.getpid())})
    fixture.save(record, receipt)
    monkeypatch.setattr(fixture, "unit_state", lambda _: missing_state())
    monkeypatch.setattr(fixture.time, "monotonic", mock.Mock(side_effect=[0, 11]))
    with pytest.raises(fixture.FixtureError, match="did not become inactive"):
        fixture.stop(receipt)
    assert root.exists()
    assert not fixture.read_json(receipt)["cleanup"]["complete"]
    assert not fixture.read_json(receipt)["cleanup"]["recorded_process_exited"]


def test_bind_conflict_never_creates_resources(seat):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with pytest.raises(fixture.FixtureError, match="already occupied"):
            fixture.start(seat, "HEAD", seat / "bind.json", port=listener.getsockname()[1])
    assert not (seat / "bind.json").exists()
    assert not list(seat.glob(fixture.PREFIX + "*"))


def test_assigned_port_is_used_and_invalid_port_refused(seat, monkeypatch):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        assigned = listener.getsockname()[1]
    monkeypatch.setenv("SC_DEV_PORT", str(assigned))
    assert fixture.select_port(None) == assigned
    monkeypatch.setenv("SC_DEV_PORT", "bad")
    with pytest.raises(fixture.FixtureError, match="SC_DEV_PORT is invalid"):
        fixture.select_port(None)


@pytest.mark.parametrize("kwargs", [{"runtime": "unknown"}, {"lifetime": 1801},
                                   {"memory_mib": 513}, {"tasks": 65}])
def test_unsupported_modes_and_enlarged_limits_fail_before_work(seat, kwargs):
    with mock.patch.object(fixture, "command") as command:
        with pytest.raises(fixture.FixtureError):
            fixture.start(seat, "HEAD", seat / "receipt.json", **kwargs)
        command.assert_not_called()
    assert not list(seat.glob(fixture.PREFIX + "*"))


def test_missing_experimental_runtime_cleans_partial_preparation(seat, monkeypatch):
    repo = source_repo(seat)
    real_command = fixture.command

    def command(argv, **kwargs):
        if argv[0] == "systemctl":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "systemd-run":
            pytest.fail("missing runtime must fail before any service starts")
        return real_command(argv, **kwargs)

    monkeypatch.setattr(fixture, "command", command)
    monkeypatch.setattr(fixture, "unit_state", lambda _: missing_state())
    receipt = seat / "missing-runtime.json"
    with pytest.raises(fixture.FixtureError, match="no experimental fixture runtime"):
        fixture.start(repo, "HEAD", receipt, temp_parent=seat, runtime="experimental")
    record = fixture.read_json(receipt)
    assert record["error_code"] == "RUNTIME_UNAVAILABLE" and record["cleanup"]["complete"]
    assert not Path(record["root"]).exists()
