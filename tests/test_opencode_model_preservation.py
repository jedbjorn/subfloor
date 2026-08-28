"""OpenCode-owned model routes preserve exact provider-native identity."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))

import model_catalog  # noqa: E402
import route_bindings  # noqa: E402
import route_transport  # noqa: E402
import run as run_mod  # noqa: E402
from conversation_adapters.base import AdapterError, ConversationContext  # noqa: E402
from conversation_adapters.opencode import OpenCodeAdapter, connected_models  # noqa: E402


PROVIDER = "Ollama-Cloud"
MODEL = "DeepSeek-V4-Flash:Q4"
SELECTOR = f"{PROVIDER}/{MODEL}"
OPTION = "Reasoning/MAX.Future"


def opencode_state(*, options: list[str] | None = None) -> dict:
    option_ids = [OPTION] if options is None else options
    return {
        "_sc_cli_version": "1.18.23",
        "connected": [PROVIDER],
        "all": [{
            "id": PROVIDER,
            "models": {
                MODEL: {
                    "family": "deepseek",
                    "variants": {
                        option_id: {"opaque": ["provider-owned", option_id]}
                        for option_id in option_ids
                    },
                },
            },
        }],
    }


def live_binding(option_id: str | None = OPTION) -> tuple[dict, str]:
    return route_bindings.live_native_v3_binding(
        "opencode", SELECTOR, MODEL, option_id
    )


def test_catalogue_keeps_exact_deepseek_route_owned_by_opencode(
    tmp_path: Path,
) -> None:
    projected = connected_models(opencode_state())
    with mock.patch.object(
        model_catalog, "CACHE", tmp_path / "model-catalog.json"
    ):
        catalogue = model_catalog.catalog(
            refresh=True,
            fetch=lambda _url, headers=None: {
                "ollama-cloud": {"models": {}},
            },
            env={},
            run=None,
            opencode_provider=lambda: projected,
        )

    models = catalogue["harnesses"]["opencode"]["models"]
    assert len(models) == 1
    assert models[0]["id"] == SELECTOR
    assert models[0]["provider"] == PROVIDER
    assert models[0]["provider_model"] == MODEL
    assert models[0]["native_option_ids"] == [OPTION]
    assert models[0]["native_variant_ids"] == {OPTION: OPTION}
    assert "adapter_metadata" not in models[0]
    assert "native_default_option_id" not in models[0]
    assert catalogue["sources"].count("opencode-provider-api") == 1


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_ui_round_trips_exact_opencode_deepseek_model_and_option() -> None:
    app = (ENGINE / "ui" / "app.js").read_text()
    helper = app[
        app.index("function nativeOptionLabel"):
        app.index("function dmModelPicker")
    ]
    script = r"""
function el(_tag, attrs = {}) { return {...attrs}; }
const control = {
  children: [], disabled: false, title: "",
  replaceChildren() { this.children = []; },
  append(value) { this.children.push(value); },
};
""" + helper + f"""
const model = {{
  id: {json.dumps(SELECTOR)},
  availability: "available",
  native_option_ids: [{json.dumps(OPTION)}],
}};
const catalog = {{stale: false, harnesses: {{opencode: {{
  authority: "harness-live", stale: false, models: [model],
}}}}}};
const state = renderNativeOptionControl(
  control, "opencode", catalog, model.id, {json.dumps(OPTION)});
console.log(JSON.stringify({{
  values: control.children.map((item) => item.value),
  labels: control.children.map((item) => item.textContent),
  selected: state.selected,
}}));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "values": ["", OPTION],
        "labels": ["Harness default", OPTION],
        "selected": OPTION,
    }


def test_one_shot_uses_exact_opencode_model_and_native_variant(
    tmp_path: Path,
) -> None:
    binding, digest = live_binding()
    projection = route_transport.project(
        binding,
        digest,
        expected_harness="opencode",
        worktree=tmp_path,
        interface="headless",
    )
    command = run_mod.headless_command(
        run_mod.load_adapter("opencode"),
        "answer once",
        transport=projection,
    )

    assert command == [
        "opencode", "run", "--model", SELECTOR,
        "--variant", OPTION, "answer once",
    ]
    assert projection.model == SELECTOR
    assert projection.native_variant_id == OPTION
    assert projection.route_agent is None
    assert not (tmp_path / "opencode.json").exists()


class RecordingTransport:
    def __init__(self, state: dict) -> None:
        self.state = state
        self.calls: list[tuple[str, str, dict | None]] = []

    def request(self, method, path, *, query=None, body=None):
        self.calls.append((method, path, body))
        if (method, path) == ("GET", "/provider"):
            return self.state
        return {}

    def stream(self, *_args, **_kwargs):
        return iter(())


def browser_context(tmp_path: Path) -> ConversationContext:
    binding, digest = live_binding()
    return ConversationContext(
        worktree=tmp_path,
        provider=PROVIDER,
        model=SELECTOR,
        effort=OPTION,
        route_binding=binding,
        binding_digest=digest,
    )


def test_browser_dispatches_exact_route_and_refuses_disappeared_option(
    tmp_path: Path,
) -> None:
    context = browser_context(tmp_path)
    transport = RecordingTransport(opencode_state())
    adapter = OpenCodeAdapter(
        transport=transport,
        shell_runtime_dir=tmp_path / "opencode-shells",
    )

    adapter._prepare_live_route(context)
    adapter._prompt("native-session", context, "browser turn")

    assert transport.calls == [
        ("GET", "/provider", None),
        (
            "POST",
            "/session/native-session/message",
            {
                "parts": [{"type": "text", "text": "browser turn"}],
                "model": {"providerID": PROVIDER, "modelID": MODEL},
                "variant": OPTION,
            },
        ),
    ]
    assert not (tmp_path / "opencode.json").exists()

    disappeared = RecordingTransport(opencode_state(options=["Other.Option"]))
    refused_adapter = OpenCodeAdapter(
        transport=disappeared,
        shell_runtime_dir=tmp_path / "other-opencode-shells",
    )
    with pytest.raises(AdapterError) as refused:
        refused_adapter._prepare_live_route(context)

    assert refused.value.code == "native_route_unavailable"
    assert disappeared.calls == [("GET", "/provider", None)]


def sprint_binding_connection() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE sprints (
          sprint_id INTEGER PRIMARY KEY,
          lifecycle TEXT NOT NULL
        );
        CREATE TABLE sprint_participants (
          participant_id INTEGER PRIMARY KEY,
          sprint_id INTEGER NOT NULL,
          active_route_binding_id INTEGER
        );
        CREATE TABLE sprint_participant_route_bindings (
          binding_id INTEGER PRIMARY KEY AUTOINCREMENT,
          participant_id INTEGER NOT NULL,
          route_revision INTEGER NOT NULL,
          contract_version INTEGER NOT NULL,
          control_state TEXT NOT NULL,
          harness TEXT NOT NULL,
          requested_model TEXT,
          provider_model TEXT,
          requested_effort TEXT,
          effective_effort TEXT,
          native_variant_id TEXT,
          native_option_id TEXT,
          transport TEXT NOT NULL,
          catalogue_generation TEXT,
          evidence_digest TEXT,
          selector_binding TEXT,
          adapter_metadata TEXT NOT NULL,
          binding_json TEXT NOT NULL,
          binding_digest TEXT NOT NULL,
          source_fingerprint TEXT,
          harness_version TEXT,
          harness_evidence_format TEXT,
          harness_support_state TEXT
        );
        INSERT INTO sprints VALUES (29, 'prepared');
        INSERT INTO sprint_participants VALUES (133, 29, NULL);
        """
    )
    return con


def test_sprint_binding_persists_exact_opencode_identity_without_translation() -> None:
    binding, digest = live_binding()
    with closing(sprint_binding_connection()) as con:
        receipt = route_bindings.ParticipantRouteBindingStore(con).bind(
            133,
            binding,
            digest,
            transition="arm",
        )
        rows = con.execute(
            "SELECT * FROM sprint_participant_route_bindings"
        ).fetchall()
        active = con.execute(
            "SELECT active_route_binding_id FROM sprint_participants "
            "WHERE participant_id=133"
        ).fetchone()[0]

    assert len(rows) == 1
    row = rows[0]
    assert active == receipt["binding_id"] == row["binding_id"]
    assert row["harness"] == "opencode"
    assert row["requested_model"] == SELECTOR
    assert row["provider_model"] == MODEL
    assert row["native_option_id"] == OPTION
    assert row["native_variant_id"] is None
    assert row["transport"] == route_bindings.TRANSPORTS["opencode"]
    assert json.loads(row["binding_json"]) == binding
    assert json.loads(row["adapter_metadata"]) == {}
    assert row["source_fingerprint"] is None
    assert row["harness_version"] is None
    assert row["harness_evidence_format"] == "harness-live-v1"
    assert row["harness_support_state"] is None
