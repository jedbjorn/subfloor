#!/usr/bin/env python3
"""Tests for the source-aware PROJECT vs ENGINE boot block (render/compose.py).

The contract: templates/boot.md carries exactly one `{{project_vs_engine}}`
slot; compose substitutes the fork block by default and the source block when
source_mode is set. A fork boot must pin the engine as a gitignored dependency
authored upstream; a source boot must say the opposite — you are upstream, the
engine is your work surface — and defuse the fork-language in engine skills.
Losing the slot (a template edit) or a constant would silently drop the whole
section from every boot doc; this pins both.

Run:
    python3 tests/test_boot_project_vs_engine.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "render"))
import compose  # noqa: E402

SLOT = "{{project_vs_engine}}"
DEV_TOOLS_SLOT = "{{dev_tools}}"


class ProjectVsEngineTest(unittest.TestCase):
    def setUp(self):
        self.template = compose.TEMPLATE_PATH.read_text()

    def test_template_carries_slot_exactly_once(self):
        self.assertEqual(self.template.count(SLOT), 1)
        self.assertEqual(self.template.count(DEV_TOOLS_SLOT), 1)

    def test_template_has_no_hardcoded_variant(self):
        # The block must come from the slot — a hardcoded copy in the template
        # would render alongside (or instead of) the mode-picked constant.
        self.assertNotIn("authored upstream", self.template)
        self.assertNotIn("you are upstream", self.template)

    def test_fork_block_keeps_the_dependency_stance(self):
        fork = compose.PROJECT_VS_ENGINE_FORK
        flat = " ".join(fork.split())
        self.assertIn("Subfloor engine dependency", fork)
        self.assertIn("not project source", fork)
        self.assertIn("authored upstream in subfloor", flat)
        self.assertIn("`./sc update`", fork)
        self.assertNotIn("you are upstream", fork)

    def test_source_block_inverts_it(self):
        source = compose.PROJECT_VS_ENGINE_SOURCE
        self.assertIn("you are upstream", source)
        self.assertIn("There is no upstream above you", source)
        self.assertIn("`./sc update`", source)  # the self-update loop
        self.assertIn("fork-language", source)  # the skills caveat
        self.assertNotIn("Subfloor engine dependency", source)

    def test_substitution_resolves_per_mode(self):
        fork_render = self.template.replace(
            SLOT, compose.render_project_vs_engine(False, False)
        )
        source_render = self.template.replace(
            SLOT, compose.render_project_vs_engine(True, True)
        )
        for render in (fork_render, source_render):
            self.assertNotIn(SLOT, render)
        self.assertIn("Subfloor engine dependency", fork_render)
        self.assertIn("you are upstream", source_render)
        self.assertNotIn("dev kit", fork_render.lower())
        self.assertNotIn("dev kit", source_render.lower())

    def test_dev_tools_render_only_for_developer_and_reviewer(self):
        inventory = {
            "state": "declared",
            "checkout": "/repo",
            "seat": "host",
            "declaration": "`.subfloor/dev-kit.json` (valid)",
            "hooks": {},
            "sandbox": "absent",
            "provision": "absent",
            "evidence": "/repo/.sc-state/local/dev-kit/",
            "baseline": {},
            "dev_port": "unavailable",
            "app_database": "unavailable",
        }
        for flavor in ("dev", "reviewer"):
            rendered = compose.render_dev_tools(flavor, inventory)
            self.assertIn("## DEV TOOLS", rendered)
            self.assertIn("**State:** `declared`", rendered)
            self.assertIn("`sc test` — unavailable (not declared)", rendered)
        for flavor in ("admin", "planner", "devops", "cartographer", None):
            self.assertEqual(compose.render_dev_tools(flavor, inventory), "")

    def test_dev_tools_reject_unknown_state(self):
        with self.assertRaisesRegex(ValueError, "unsupported dev-tool state"):
            compose.render_dev_tools("dev", {"state": "invented"})

    def test_repair_inventory_uses_canonical_recovery(self):
        repair = compose.render_dev_tools("dev", {"state": "repair"})
        self.assertIn("**State:** `repair`", repair)
        self.assertIn("Exit to the host", repair)
        self.assertIn("require `ready`", repair)

    def test_floor_and_declared_work_repo_render_as_separate_targets(self):
        lines = compose.render_target_freshness(
            "live_engine_checkout: `/substrate` · remote unverified",
            "shared_work_repo: `/work` · remote verified · behind 2",
        )
        self.assertEqual(
            lines,
            [
                "- floor: live_engine_checkout: `/substrate` · remote unverified",
                "- work repo: shared_work_repo: `/work` · remote verified · behind 2",
            ],
        )


if __name__ == "__main__":
    unittest.main()
