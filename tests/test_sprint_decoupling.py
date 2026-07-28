"""Conductor Step 3: sprint runtime truth has no Interface binding dependency."""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"

RUNTIME_SURFACE = (
    ENGINE / "api" / "server.py",
    ENGINE / "api" / "sprint_routes.py",
    ENGINE / "render" / "compose.py",
    ENGINE / "scripts" / "activity_readers.py",
    ENGINE / "scripts" / "pr_poller.py",
    ENGINE / "scripts" / "run.py",
    ENGINE / "scripts" / "sprint.py",
    ENGINE / "scripts" / "sprint_state.py",
    ENGINE / "scripts" / "sprint_units.py",
)

RETIRED_TABLES = (
    "sprint_planner_bindings",
    "planner_wake_batches",
    "planner_wake_items",
    "planner_action_receipts",
)


def executable_strings(path: Path):
    tree = ast.parse(path.read_text())
    docstrings = {
        text
        for node in ast.walk(tree)
        if isinstance(
            node,
            (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        )
        for text in (ast.get_docstring(node, clean=False),)
        if text is not None
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value not in docstrings
        ):
            # Function/module/class docstrings explain the retirement and are
            # not executable reads. SQL and route strings remain in the walk.
            yield node.value


class SprintDecouplingTest(unittest.TestCase):
    def test_runtime_executes_no_sql_against_retired_binding_or_wake_tables(self):
        offenders = []
        sql_verbs = ("SELECT", "INSERT", "UPDATE", "DELETE", "JOIN", "FROM")
        for path in RUNTIME_SURFACE:
            for value in executable_strings(path):
                upper = value.upper()
                if not any(verb in upper for verb in sql_verbs):
                    continue
                for table in RETIRED_TABLES:
                    if table in value:
                        offenders.append(f"{path.relative_to(ROOT)}: {table}")
        self.assertEqual(
            [],
            offenders,
            "a sprint runtime path still executes against retired Interface "
            "truth; use documents/sprint_units/messages or wait for directives",
        )

    def test_cli_exposes_only_record_and_render_verbs(self):
        source = (ENGINE / "scripts" / "sprint.py").read_text()
        tree = ast.parse(source)
        parser_verbs = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "add_parser" or not node.args:
                continue
            value = node.args[0]
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parser_verbs.add(value.value)
        self.assertEqual(
            {"unit", "board", "add", "set", "state", "list"},
            parser_verbs,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
