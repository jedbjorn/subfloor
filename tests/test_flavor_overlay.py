#!/usr/bin/env python3
"""Tests for fork-local flavor overlays (shell_factory.py).

The engine's templates/shells/*.json are materialized — overwritten on every
`./sc update` — so a fork cannot durably edit what a new shell of a flavor
gets. The overlay (`.sc-state/flavors/<flavor>.json`, tracked, fork-owned)
must: adjust the skill list via skills_add/skills_remove (riding upstream's
evolving list, not replacing it), override scalars but never `flavor` itself,
apply in BOTH load_flavor() and flavors() (creation + GUI listing), and fail
loud on bad JSON rather than silently creating an un-overlaid shell.

Run:
    python3 tests/test_flavor_overlay.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))
import shell_factory as sf  # noqa: E402

TPL = {"flavor": "dev", "abbr": "DEV", "role": "Dev shell",
       "mandate": "Build in {{repo}}.",
       "skills": ["docs", "git", "test_authoring", "test_authoring_pg"]}


class ApplyOverlayTest(unittest.TestCase):
    def test_skills_add_remove(self):
        # The dos-arch case: swap the engine testing skill for the fork's own.
        out = sf._apply_overlay(TPL, {
            "skills_add": ["test_authoring_dosarch"],
            "skills_remove": ["test_authoring", "test_authoring_pg"]})
        self.assertEqual(out["skills"],
                         ["docs", "git", "test_authoring_dosarch"])
        self.assertEqual(TPL["skills"],
                         ["docs", "git", "test_authoring", "test_authoring_pg"],
                         "overlay must not mutate the input template")

    def test_add_is_idempotent_against_engine_list(self):
        out = sf._apply_overlay(TPL, {"skills_add": ["git", "flags"]})
        self.assertEqual(out["skills"].count("git"), 1)
        self.assertIn("flags", out["skills"])

    def test_scalar_override_but_never_flavor(self):
        out = sf._apply_overlay(TPL, {"role": "Fork dev", "flavor": "hijack"})
        self.assertEqual(out["role"], "Fork dev")
        self.assertEqual(out["flavor"], "dev",
                         "an overlay is keyed BY flavor — it cannot rename it")

    def test_empty_overlay_is_identity(self):
        self.assertEqual(sf._apply_overlay(TPL, {}), TPL)


class OverlayFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.templates = root / "templates"
        self.overlays = root / "flavors"
        self.templates.mkdir()
        self.overlays.mkdir()
        (self.templates / "dev.json").write_text(json.dumps(TPL))
        self._orig = (sf.SHELL_TEMPLATES, sf.FORK_FLAVOR_OVERLAYS)
        sf.SHELL_TEMPLATES = self.templates
        sf.FORK_FLAVOR_OVERLAYS = self.overlays

    def tearDown(self):
        sf.SHELL_TEMPLATES, sf.FORK_FLAVOR_OVERLAYS = self._orig
        self.tmp.cleanup()

    def test_no_overlay_file_loads_plain_template(self):
        self.assertEqual(sf.load_flavor("dev")["skills"], TPL["skills"])

    def test_load_flavor_applies_overlay(self):
        (self.overlays / "dev.json").write_text(json.dumps(
            {"skills_add": ["test_authoring_dosarch"],
             "skills_remove": ["test_authoring", "test_authoring_pg"]}))
        got = sf.load_flavor("dev")["skills"]
        self.assertIn("test_authoring_dosarch", got)
        self.assertNotIn("test_authoring", got)

    def test_flavors_listing_applies_overlay(self):
        (self.overlays / "dev.json").write_text(json.dumps({"role": "Fork dev"}))
        listed = {f["flavor"]: f for f in sf.flavors()}
        self.assertEqual(listed["dev"]["role"], "Fork dev",
                         "the GUI's flavor listing must show the overlaid shape")

    def test_bad_overlay_json_fails_loud(self):
        (self.overlays / "dev.json").write_text("{broken")
        with self.assertRaises(ValueError):
            sf.load_flavor("dev")


if __name__ == "__main__":
    unittest.main()
