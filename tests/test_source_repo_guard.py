#!/usr/bin/env python3
"""The installed-mode identity adapter must survive the source/fork overlap.

This install's origin IS jedbjorn/subfloor.git — an origin whose basename sits
in the engine's SOURCE_REPO_NAMES. The bare engine check therefore reads this
repo as the engine SOURCE repo, which would (a) let `update` reconcile in
place instead of materializing the pinned engine, and (b) flip every boot to
the wrong "you are upstream" PROJECT vs ENGINE stance. The one protection is
the adapter layer (scripts_sc/installed_update.py + installed_run.py): it must
pin installed-mode identity on BOTH modules regardless of origin, name the
sc-engine-local remote, and tolerate an unavailable sandbox epoch.

The engine's own canonical-name tests live upstream (subfloor); this suite
pins what keeps THIS install out of source-mode.

Run:
    python3 tests/test_source_repo_guard.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ROOT / "scripts_sc"))
import install  # noqa: E402
import installed_update  # noqa: E402


class InstalledIdentityAdapterTest(unittest.TestCase):
    def setUp(self):
        self.update = installed_update.load_installed_updater()

    def test_engine_baseline_still_recognizes_the_source_names(self):
        # The ENGINE's canonical set is upstream's decision — pin it so a
        # rename there surfaces here instead of silently changing stance.
        self.assertIn("super-coder", install.SOURCE_REPO_NAMES)
        self.assertIn("subfloor", install.SOURCE_REPO_NAMES)

    def test_this_origin_basename_lands_in_the_engine_source_set(self):
        # The trap itself: this install's origin basename reads as source.
        self.assertEqual(install.origin_basename(), "subfloor")
        self.assertTrue(install.is_source_repo())

    def test_adapter_pins_installed_mode_on_both_modules(self):
        installed_update.load_installed_updater()
        import update as update_mod

        self.assertFalse(self.update.is_source_repo())
        self.assertFalse(update_is_source_repo_after_adapter())

    def test_adapter_names_the_engine_remote(self):
        installed_update.load_installed_updater()
        self.assertEqual(self.update.super_coder_remote(), "sc-engine-local")

    def test_engine_remote_matcher_accepts_renamed_url(self):
        orig = self.update.git
        try:
            self.update.git = lambda *a, **k: SimpleNamespace(
                stdout="origin\tgit@github.com:me/my-fork.git (fetch)\n"
                       "sc-engine-local\thttps://github.com/jedbjorn/subfloor.git (fetch)\n",
                returncode=0)
            # probe the ENGINE's own matcher, bypassing the adapter override
            import update as update_mod

            engine_fn = update_mod.__dict__.get("_engine_super_coder_remote")
            self.assertIsNotNone(engine_fn)
            self.assertEqual(engine_fn(), "sc-engine-local")
        finally:
            self.update.git = orig

    def test_engine_remote_matcher_rejects_fork_name_containing_source_name(self):
        orig = self.update.git
        try:
            self.update.git = lambda *a, **k: SimpleNamespace(
                stdout="origin\thttps://github.com/jedbjorn/subfloor-marketing.git (fetch)\n"
                       "sc-engine-local\tgit@github.com:jedbjorn/subfloor.git (fetch)\n",
                returncode=0,
            )
            import update as update_mod

            engine_fn = update_mod.__dict__.get("_engine_super_coder_remote")
            self.assertIsNotNone(engine_fn)
            self.assertEqual(engine_fn(), "sc-engine-local")
        finally:
            self.update.git = orig

    def test_adapter_tolerates_unavailable_sandbox_epoch_state(self):
        import io
        from contextlib import redirect_stdout
        from unittest import mock

        update = installed_update.load_installed_updater()
        warning = io.StringIO()
        with mock.patch.object(
            update.install_mod,
            "roll_harness_epoch",
            side_effect=OSError("read-only machine config"),
        ), redirect_stdout(warning):
            self.assertIsNone(update.expire_sandbox_harnesses())
        self.assertIn("sandbox harness epoch not updated", warning.getvalue())


def update_is_source_repo_after_adapter() -> bool:
    """Read update.is_source_repo through a FRESH import — the adapter mutates
    the module attribute, so a clean re-import probes the patched state only if
    the module is shared. sys.modules caching makes this the same object."""
    import update  # noqa: PLC0415

    return update.is_source_repo()


if __name__ == "__main__":
    unittest.main(verbosity=2)