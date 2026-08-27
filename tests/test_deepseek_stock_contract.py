"""Provider-free compatibility proof for the pinned stock DSH Host contract."""
from __future__ import annotations

import importlib
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))
deepseek_host = importlib.import_module("deepseek_host")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_ready(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 12
    body = b'{"type":"client-request","rpcId":"ready","method":"workspace.list","payload":{}}'
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"stock dsh exited: {process.stdout.read()}")
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/workspace.list",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=0.2) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("stock dsh Host did not become ready")


def test_pinned_stock_dsh_workspace_session_archive_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the unmodified 0.1.1-rc.2 Host, not a fake transport."""
    dsh = shutil.which("dsh")
    if dsh is None:
        pytest.fail("pinned dsh 0.1.1-rc.2 is required for the stock contract")
    version = subprocess.check_output([dsh, "--version"], text=True).strip()
    assert version == "0.1.1-rc.2"

    port = _free_port()
    dsh_home = tmp_path / "dsh-home"
    workspace = tmp_path / "workspace-a"
    other_workspace = tmp_path / "workspace-b"
    workspace.mkdir()
    other_workspace.mkdir()
    environment = {
        **os.environ,
        "DSH_HOME": str(dsh_home),
        "NO_COLOR": "1",
    }
    process = subprocess.Popen(
        [dsh, "web", "--host", "127.0.0.1", "--port", str(port), "--no-open"],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_ready(port, process)
        monkeypatch.setenv("SC_DEEPSEEK_HOST_PORT", str(port))
        client = deepseek_host.DeepSeekHostClient(
            timeout=2,
            endpoint=deepseek_host.configured_host_url(),
        )
        created = client.call("workspace.create", {"path": str(workspace)})
        workspace_id = created["workspace"]["workspaceId"]
        second = client.call("workspace.create", {"path": str(other_workspace)})
        second_workspace_id = second["workspace"]["workspaceId"]
        assert second["workspace"]["path"] == str(other_workspace)
        assert second_workspace_id != workspace_id
        session_id = "sc-stock-contract"
        first_session = client.call(
            "session.create",
            {
                "workspaceId": workspace_id,
                "sessionId": session_id,
                "agentPreset": "standard",
            },
        )
        retry_session = client.call(
            "session.create",
            {
                "workspaceId": workspace_id,
                "sessionId": session_id,
                "agentPreset": "standard",
            },
        )
        assert first_session == {"sessionId": session_id, "agentPreset": "standard"}
        assert retry_session == first_session

        listed = client.call("workspace.list", {})
        row = next(item for item in listed["items"] if item["workspaceId"] == workspace_id)
        assert row["path"] == str(workspace)
        assert row["sessionIds"] == [session_id]
        session_rows = client.call("session.list", {"workspaceId": workspace_id})
        assert len(session_rows["items"]) == 1
        assert session_rows["items"][0]["sessionId"] == session_id
        assert session_rows["items"][0]["cwd"] == str(workspace)

        archived = client.call("workspace.archiveSession", {"sessionId": session_id})
        assert archived["archivedSessionIds"] == [session_id]
        listed_after_archive = client.call("workspace.list", {})
        assert session_id in listed_after_archive["archivedSessionIds"]
        row_after_archive = next(
            item for item in listed_after_archive["items"] if item["workspaceId"] == workspace_id
        )
        # Stock DSH preserves the workspace membership row while publishing the
        # archive set separately; callers must check both facts before reuse.
        assert row_after_archive["sessionIds"] == [session_id]
        archived_rows = client.call("session.list", {"workspaceId": workspace_id})
        assert archived_rows["items"] == session_rows["items"]

        with pytest.raises(deepseek_host.HostRpcError) as conflict:
            client.call(
                "session.create",
                {
                    "workspaceId": second_workspace_id,
                    "sessionId": session_id,
                    "agentPreset": "standard",
                },
            )
        assert conflict.value.code == "HARNESS_HOST_RPC_SESSION_CONFLICT"

        unknown = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/workspace.unarchiveSession",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as absent:
            urllib.request.urlopen(unknown, timeout=2)
        assert absent.value.code == 404

        # The unmodified native Web client allocates this separate opaque
        # namespace when the browser opens a chat without a caller-supplied
        # ID.  Gateway activity must accept it without weakening the strict
        # managed-session reservation format.
        native = client.call(
            "session.create",
            {"workspaceId": second_workspace_id, "agentPreset": "standard"},
        )
        native_id = native["sessionId"]
        assert native["agentPreset"] == "standard"
        assert native_id.startswith("session-")
        assert uuid.UUID(native_id.removeprefix("session-")).version == 4
        native_rows = client.call("session.list", {"workspaceId": second_workspace_id})
        native_matches = [
            row for row in native_rows["items"] if row["sessionId"] == native_id
        ]
        assert len(native_matches) == 1
        native_row = native_matches[0]
        assert native_row["sessionId"] == native_id
        assert native_row["cwd"] == str(other_workspace)
        assert native_row["running"] is False
        assert native_row["blank"] is True
        assert native_row["sessionId"] != session_id

        second_native = client.call(
            "session.create",
            {"workspaceId": second_workspace_id, "agentPreset": "standard"},
        )
        second_native_id = second_native["sessionId"]
        before_reorder = next(
            row for row in client.call("workspace.list", {})["items"]
            if row["workspaceId"] == second_workspace_id
        )
        assert before_reorder["sessionIds"] == [second_native_id, native_id]
        reordered = client.call(
            "workspace.insertSessionBefore",
            {
                "workspaceId": second_workspace_id,
                "sessionId": native_id,
                "beforeSessionId": second_native_id,
            },
        )
        assert reordered["workspace"]["workspaceId"] == second_workspace_id
        assert reordered["workspace"]["sessionIds"] == [native_id, second_native_id]
        assert session_id not in reordered["workspace"]["sessionIds"]
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
