"""Real API authorization, opt-in grants, and generated browser guidance."""

import json
import sqlite3
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder/scripts"))
sys.path.insert(0, str(ROOT / ".super-coder/api"))
import browser
import feature
import seed_skills
import server
import skill

from tests.test_web_search import _build_file_db


@pytest.fixture
def api(tmp_path, monkeypatch):
    path = tmp_path / "engine.db"
    _build_file_db(path)

    def connect():
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        return con

    monkeypatch.setattr(server, "db", connect)

    def call(action, token=None, origin="http://127.0.0.1:8800", route="/api/browser"):
        raw = json.dumps({"action": action}).encode()
        headers = ["Host: 127.0.0.1:8800", f"Content-Length: {len(raw)}"]
        if origin:
            headers += ["Origin: " + origin, "Sec-Fetch-Site: same-origin"]
        if token:
            headers += ["Authorization: Bearer " + token]
        status, _, body = server.dispatch_http("POST", route, "\r\n".join(headers), raw)
        return status, json.loads(body)

    return call


@pytest.mark.parametrize(
    "action", ["arm", "disarm", "up", "down", "doctor", "link", "disable", "validate"]
)
def test_shell_cannot_mutate_browser(api, action):
    with (
        mock.patch.object(browser, "operate") as operation,
        mock.patch.object(browser, "link") as link,
    ):
        status, _ = api(action, token="shell-token")
        assert status == 403
        status, _ = api(action, token="shell-token", route="/_sc/browser")
        assert status == 403
        operation.assert_not_called()
        link.assert_not_called()


@pytest.mark.parametrize("action", ["validate", "link"])
def test_untrusted_setup_never_reaches_operator_paths(api, action):
    with mock.patch.object(browser, "link") as link:
        assert api(action, origin="https://attacker.test")[0] == 403
        assert api(action, token="unknown-token")[0] == 401
        link.assert_not_called()


def test_operator_origin_gate_and_action(api):
    with mock.patch.object(
        browser, "operate", return_value={"state": "disarmed"}
    ) as operation:
        assert api("disarm", origin="https://attacker.test")[0] == 403
        operation.assert_not_called()
        assert api("disarm") == (200, {"state": "disarmed"})
        operation.assert_called_once_with("disarm")


def test_status_requires_shell_auth_and_hides_configuration(api):
    with (
        mock.patch.object(browser, "status", return_value={"state": "absent"}),
        mock.patch.object(browser, "read", return_value=None),
    ):
        assert api("status", route="/_sc/browser")[0] == 401
        assert api("status", token="wrong", route="/_sc/browser")[0] == 401
        status, body = api("status", token="shell-token", route="/_sc/browser")
        assert status == 200
        assert body == {"state": "absent"}


@pytest.mark.parametrize("action", ["validate", "link", "arm"])
def test_container_seat_refuses_setup_with_the_seat_reason(api, monkeypatch, action):
    """The docker runtime serves this GUI from `sc-<repo>`; issue #1556 saw the
    operator's real profile reported as a missing file instead."""
    monkeypatch.setenv("SC_SANDBOX", "1")
    status, body = api(action)
    assert status == 400
    assert body == {"state": "failed", "error": browser.UNSUPPORTED_SEAT}


def test_feature_grants_survive_reseed_and_reverse(tmp_path, monkeypatch):
    path = tmp_path / "engine.db"
    _build_file_db(path)
    con = sqlite3.connect(path)
    monkeypatch.setattr(skill, "_persist_mutation", lambda *args: None)
    browser.set_grants(con, True)
    seed_skills.reconcile_standard_flavor_packs(con)
    query = "SELECT flavor FROM flavor_skills JOIN skills USING(skill_id) WHERE name='drive_browser'"
    assert {r[0] for r in con.execute(query)} == {"dev", "reviewer", "planner", "admin"}
    browser.set_grants(con, False)
    seed_skills.reconcile_standard_flavor_packs(con)
    assert not con.execute(query).fetchall()
    con.close()


def test_feature_enable_and_disable_use_same_grant_owner(monkeypatch):
    monkeypatch.setattr(feature, "_instance", dict)
    monkeypatch.delenv("SC_API_TOKEN", raising=False)
    with (
        mock.patch.object(feature, "_browser_grants") as grants,
        mock.patch.object(browser, "operate") as lifecycle,
    ):
        feature.cmd_enable("browser")
        feature.cmd_disable("browser")
        assert grants.call_args_list == [mock.call(True), mock.call(False)]
        lifecycle.assert_called_once_with("disable")


def test_browser_skill_seed_matches_migration(tmp_path):
    path = tmp_path / "engine.db"
    _build_file_db(path)
    con = sqlite3.connect(path)
    row = con.execute(
        "SELECT content,common FROM skills WHERE name='drive_browser'"
    ).fetchone()
    spec = seed_skills.parse_skill(
        ROOT / ".super-coder/assets/skills/drive_browser/SKILL.md"
    )
    assert row[0].strip() == spec["content"].strip()
    assert row[1] == 0
    con.close()


def test_browser_gui_actions_execute_without_new_ui_dependencies():
    import subprocess

    source = (ROOT / ".super-coder/ui/app.js").read_text()
    functions = source[
        source.index("function browserStatusText(") : source.index(
            "// Windows Test VM wizard"
        )
    ]
    script = (
        r"""
const assert = require('node:assert/strict');
const calls = [];
let modal;
function el(tag, attrs, ...children) { return {tag, ...attrs, children, append(...xs) {this.children.push(...xs);}}; }
function openActionModal(spec) { modal = spec; return () => {}; }
async function api(path, method, body) {
  calls.push({path, method, body});
  if (!method) return {state:'declared', armed:true, config:{executable:'/bin/chromium',user_data_dir:'/fixture'}};
  return {ok:true, armed:body.action === 'arm', state:'ready'};
}
"""
        + functions
        + r"""
(async () => {
  await openBrowserModal(() => {});
  function walk(node) { return [node, ...(node.children || []).flatMap(x => typeof x === 'object' ? walk(x) : [])]; }
  const nodes = walk(modal.bodyNode);
  const button = text => nodes.find(n => n.tag === 'button' && n.textContent === text);
  await button('check setup').onclick();
  assert.equal(calls.at(-1).body.action, 'validate');
  assert.equal(calls.at(-1).body.config.profile_name, 'Subfloor');
  await button('disarm').onclick();
  assert.equal(calls.at(-1).body.action, 'disarm');
  await modal.actionNode.onclick();
  assert.equal(calls.at(-1).body.action, 'link');
  await button('disable browser').onclick();
  assert.equal(calls.at(-1).body.action, 'disable');
  assert(!nodes.some(n => n.type === 'password'));
})().catch(e => {console.error(e);process.exit(1)});
"""
    )
    result = subprocess.run(
        ["node"], input=script, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_browser_gui_offers_no_setup_on_an_unsupported_seat():
    import subprocess

    source = (ROOT / ".super-coder/ui/app.js").read_text()
    functions = source[
        source.index("function browserStatusText(") : source.index(
            "// Windows Test VM wizard"
        )
    ]
    script = (
        r"""
const assert = require('node:assert/strict');
const calls = [];
let modal;
function el(tag, attrs, ...children) { return {tag, ...attrs, children, append(...xs) {this.children.push(...xs);}}; }
function openActionModal(spec) { modal = spec; return () => {}; }
async function api(path, method, body) {
  calls.push({path, method, body});
  return {state:'failed', supported:false,
          detail:'unsupported seat: browser requires bare metal',
          config:null, defaults:{executable:'/usr/lib/chromium/chromium',user_data_dir:'/home/op/.config/chromium'}};
}
"""
        + functions
        + r"""
(async () => {
  assert.equal(browserStatusText({state:'failed', supported:false, detail:'unsupported seat: x'}),
               'unsupported seat: x');
  await openBrowserModal(() => {});
  function walk(node) { return [node, ...(node.children || []).flatMap(x => typeof x === 'object' ? walk(x) : [])]; }
  const nodes = walk(modal.bodyNode);
  const buttons = nodes.filter(n => n.tag === 'button');
  for (const text of ['check setup', 'arm', 'disable browser'])
    assert.equal(buttons.find(n => n.textContent === text).disabled, true, text);
  assert.equal(modal.actionNode.disabled, true);
  assert.equal(calls.length, 1, 'the modal only read status');
  assert(nodes.some(n => typeof n.children?.[0] === 'string'
                         && n.children[0].includes('needs the host runtime')));
})().catch(e => {console.error(e);process.exit(1)});
"""
    )
    result = subprocess.run(
        ["node"], input=script, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
