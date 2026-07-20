#!/usr/bin/env python3
"""Tests for the source-aware PROJECT vs ENGINE boot block (render/compose.py).

The contract: templates/boot.md carries exactly one `{{project_vs_engine}}`
slot; compose substitutes the fork block by default and the source block when
source_mode is set. A fork boot must keep the never-edit-engine rule and name
its upstream; a source boot must say the opposite — you are upstream, the
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


class ProjectVsEngineTest(unittest.TestCase):
    def setUp(self):
        self.template = compose.TEMPLATE_PATH.read_text()

    def test_template_carries_slot_exactly_once(self):
        self.assertEqual(self.template.count(SLOT), 1)

    def test_template_has_no_hardcoded_variant(self):
        # The block must come from the slot — a hardcoded copy in the template
        # would render alongside (or instead of) the mode-picked constant.
        self.assertNotIn("authored upstream", self.template)
        self.assertNotIn("you are upstream", self.template)

    def test_fork_block_keeps_the_dependency_stance(self):
        fork = compose.PROJECT_VS_ENGINE_FORK
        self.assertIn("do not treat it as the project or edit", fork)
        self.assertIn("authored upstream in subfloor", fork)
        self.assertIn("`./sc update`", fork)
        self.assertNotIn("you are upstream", fork)

    def test_source_block_inverts_it(self):
        source = compose.PROJECT_VS_ENGINE_SOURCE
        self.assertIn("you are upstream", source)
        self.assertIn("There is no upstream above you", source)
        self.assertIn("`./sc update`", source)  # the self-update loop
        self.assertIn("fork-language", source)  # the skills caveat
        self.assertNotIn("do not treat it as the project", source)

    def test_substitution_resolves_per_mode(self):
        fork_render = self.template.replace(SLOT, compose.PROJECT_VS_ENGINE_FORK)
        source_render = self.template.replace(SLOT, compose.PROJECT_VS_ENGINE_SOURCE)
        for render in (fork_render, source_render):
            self.assertNotIn(SLOT, render)
        self.assertIn("gitignored dependency", fork_render)
        self.assertIn("you are upstream", source_render)


if __name__ == "__main__":
    unittest.main()
