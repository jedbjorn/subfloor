"""Task #168: retained systems have no Sprint-specific runtime branch."""
from __future__ import annotations

import ast
import inspect
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
MANIFEST = json.loads(
    (ROOT / "tests/fixtures/sprint_removal/manifest.json").read_text()
)

sys.path.insert(0, str(ENGINE / "render"))
sys.path.insert(0, str(ENGINE / "scripts"))
import compose  # noqa: E402
import run  # noqa: E402
import snapshot  # noqa: E402


FORBIDDEN_RUNTIME_MARKERS = (
    "sprint",
    "sprint_doc_id",
    "sprint_ref",
    "sc_sprint_",
    "pr_event",
    "sprint_assignment",
    "sprint_conversation_bindings",
    "sprint_cancellations",
    "sprint_lifecycle",
    "sprint_state",
    "sprint_units",
    "render_sprint",
    "render_slot_directive",
    "slot_context",
    "active_sprint_ids",
    "warn_live_state",
)


def executable_terms(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    terms: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            terms.append(node.id)
        elif isinstance(node, ast.Attribute):
            terms.append(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            terms.append(node.value)
    return terms


class RetainedSystemDecouplingTest(unittest.TestCase):
    def test_manifest_names_the_complete_retained_shared_surface(self):
        expected = {
            "sc",
            ".super-coder/api/server.py",
            ".super-coder/api/conversation_routes.py",
            ".super-coder/scripts/conversation_broker.py",
            ".super-coder/scripts/conversation_launch.py",
            ".super-coder/scripts/run.py",
            ".super-coder/scripts/mem.py",
            ".super-coder/scripts/job.py",
            ".super-coder/scripts/analytics.py",
            ".super-coder/scripts/snapshot.py",
            ".super-coder/scripts/rebuild.py",
            ".super-coder/scripts/update.py",
            ".super-coder/scripts/install.py",
            ".super-coder/render/compose.py",
            ".super-coder/render/flat.py",
        }
        inventoried = set(MANIFEST["retained_shared_system_files"])
        self.assertEqual(expected, inventoried)
        self.assertEqual(
            [],
            [relative for relative in sorted(inventoried) if not (ROOT / relative).is_file()],
        )

    def test_retained_python_executes_no_sprint_query_or_projection(self):
        offenders: list[str] = []
        for relative in MANIFEST["retained_shared_system_files"]:
            path = ROOT / relative
            if path.suffix != ".py":
                continue
            for term in executable_terms(path):
                lowered = term.lower()
                for marker in FORBIDDEN_RUNTIME_MARKERS:
                    if marker in lowered:
                        offenders.append(f"{relative}: {marker}")
        self.assertEqual([], sorted(set(offenders)))

    def test_cli_has_no_sprint_launch_or_message_context(self):
        source = (ROOT / "sc").read_text().lower()
        for marker in ("--sprint", "--slot", "sc_sprint_", "sprint_doc_id"):
            self.assertNotIn(marker, source)

    def test_generic_launch_and_render_signatures_have_no_slot_context(self):
        self.assertEqual(
            {"shell_id", "harness", "model", "effort", "headless_prompt"},
            set(inspect.signature(run.prepare_launch).parameters),
        )
        self.assertNotIn("slot_context", inspect.signature(compose.compose_boot).parameters)

    def test_snapshot_excludes_every_disposable_runtime_table(self):
        removed = set(MANIFEST["removed_tables"])
        persisted = set(snapshot.PER_INSTANCE_TABLES)
        filtered = set(snapshot.SNAPSHOT_ROW_FILTERS)
        self.assertEqual(set(), removed & persisted)
        self.assertEqual(set(), removed & filtered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
