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
CONTROLLED = "deepseek-v4-flash:cloud"
CONTROLLED_SELECTOR = "ollama-cloud/deepseek-v4-flash"


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


def test_admin_interactive_preserves_explicit_controlled_model_in_native_argv() -> None:
    model, route = run_mod.resolve_interactive_model(
        harness="opencode",
        flavor_model="ollama-cloud/glm-5.2",
        requested_model=CONTROLLED,
        host_admin=True,
    )

    assert route == run_mod.ControlledOpenCodeRoute(
        CONTROLLED, CONTROLLED_SELECTOR
    )
    assert model == CONTROLLED
    assert run_mod.controlled_opencode_model_args(
        run_mod.load_adapter("opencode"), route
    ) == ["--model", CONTROLLED_SELECTOR]
    notice = run_mod.controlled_opencode_launch_notice(route)
    assert f"requested model route: {CONTROLLED}" in notice
    assert f"OpenCode selector: {CONTROLLED_SELECTOR}" in notice
    assert "launch pending runtime observation" in notice
    assert "model route observed:" not in notice
    template = json.loads(
        (ENGINE / "adapters" / "opencode" / "opencode.json").read_text()
    )
    assert template["plugin"][-1].endswith("/enforce-model-route.js")

    ordinary_model, ordinary_route = run_mod.resolve_interactive_model(
        harness="opencode",
        flavor_model="ordinary/default",
        requested_model=CONTROLLED,
        host_admin=False,
    )
    assert (ordinary_model, ordinary_route) == ("ordinary/default", None)

    with pytest.raises(ValueError, match="must be a provider/model selector"):
        run_mod.resolve_interactive_model(
            harness="opencode",
            flavor_model="ollama-cloud/glm-5.2",
            requested_model="uncontrolled-name",
            host_admin=True,
        )


def test_controlled_route_preflight_requires_exact_selector() -> None:
    adapter = run_mod.load_adapter("opencode")
    route = run_mod.ControlledOpenCodeRoute(CONTROLLED, CONTROLLED_SELECTOR)
    completed = subprocess.CompletedProcess(
        ["opencode", "models", "ollama-cloud"], 0,
        stdout=f"{CONTROLLED_SELECTOR}\nollama-cloud/glm-5.2\n", stderr="",
    )
    runner = mock.Mock(return_value=completed)

    run_mod.preflight_controlled_opencode_route(adapter, route, run=runner)

    runner.assert_called_once_with(
        ["opencode", "models", "ollama-cloud"],
        text=True, capture_output=True, check=False, timeout=20,
    )


def test_controlled_route_preflight_refuses_unavailable_selector() -> None:
    route = run_mod.ControlledOpenCodeRoute(CONTROLLED, CONTROLLED_SELECTOR)
    completed = subprocess.CompletedProcess(
        ["opencode", "models", "ollama-cloud"], 0,
        stdout="ollama-cloud/glm-5.2\n", stderr="",
    )

    with pytest.raises(ValueError, match="route unavailable before launch") as refused:
        run_mod.preflight_controlled_opencode_route(
            run_mod.load_adapter("opencode"), route,
            run=lambda *_args, **_kwargs: completed,
        )

    assert f"requested={CONTROLLED}" in str(refused.value)
    assert f"selector={CONTROLLED_SELECTOR}" in str(refused.value)


def run_route_guard(requested: str | None, provider: str, model: str) -> dict:
    plugin = ENGINE / "adapters" / "opencode" / "enforce-model-route.js"
    script = f"""
import fs from "node:fs";
const source = fs.readFileSync({json.dumps(str(plugin))}, "utf8");
const loaded = await import(`data:text/javascript;base64,${{Buffer.from(source).toString("base64")}}`);
const hooks = await loaded.EnforceModelRoute();
if ({json.dumps(requested)} === null) delete process.env.SC_OPENCODE_ENFORCED_MODEL;
else process.env.SC_OPENCODE_ENFORCED_MODEL = JSON.stringify({{
  requested: {json.dumps(CONTROLLED)}, selector: {json.dumps(requested)},
}});
let dispatched = false;
try {{
  await hooks["chat.params"]({{
    model: {{providerID: {json.dumps(provider)}, id: {json.dumps(model)}}},
  }});
  dispatched = true;
  console.log(JSON.stringify({{accepted: true, dispatched}}));
}} catch (error) {{
  console.log(JSON.stringify({{accepted: false, dispatched, error: error.message}}));
}}
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return {
        **json.loads(completed.stdout.strip().splitlines()[-1]),
        "stderr": completed.stderr,
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_controlled_route_observes_exact_runtime_model_before_dispatch() -> None:
    result = run_route_guard(
        CONTROLLED_SELECTOR, "ollama-cloud", "deepseek-v4-flash"
    )

    assert result["accepted"] is True
    assert result["dispatched"] is True
    assert (
        f"requested={CONTROLLED} observed={CONTROLLED_SELECTOR}"
        in result["stderr"]
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_controlled_route_refuses_wrong_model_without_false_success() -> None:
    result = run_route_guard(CONTROLLED_SELECTOR, "ollama-cloud", "glm-5.2")

    assert result["accepted"] is False
    assert result["dispatched"] is False
    assert f"requested={CONTROLLED}" in result["error"]
    assert "observed=ollama-cloud/glm-5.2" in result["error"]
    assert "model route observed" not in result["stderr"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_controlled_route_refuses_unavailable_observation() -> None:
    result = run_route_guard(CONTROLLED_SELECTOR, "", "")

    assert result["accepted"] is False
    assert result["dispatched"] is False
    assert f"requested={CONTROLLED}" in result["error"]
    assert "observed=unavailable" in result["error"]
    assert "model route observed" not in result["stderr"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_route_guard_is_inert_without_admin_controlled_request() -> None:
    result = run_route_guard(None, "ollama-cloud", "glm-5.2")

    assert result["accepted"] is True
    assert result["dispatched"] is True
    assert result["stderr"] == ""


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
