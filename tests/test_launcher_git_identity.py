"""Launcher git identity — commits attribute to the booted shell (issue #1494).

The sandbox launcher used to export one repo-wide GIT_AUTHOR_*/GIT_COMMITTER_*
identity (whatever the shared .git/config last carried) into every shell, so
commits landed as whichever shell booted most recently — or failed outright
with "empty ident name" when that value was blank. The fix: run.py derives the
ident from the booted shell's own DB row at the exec seam, overriding anything
inherited, and dispatch.sh exports nothing repo-wide.

Run:
    python3 tests/test_launcher_git_identity.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run  # noqa: E402, RUF100  (engine scripts dir is prepended above)


def _shell(display_name: str | None, shortname: str | None):
    return {"display_name": display_name, "shortname": shortname}


class ShellGitIdentityTest(unittest.TestCase):
    def test_identity_pairs_display_name_with_shortname_email(self) -> None:
        name, email = run.shell_git_identity(_shell("Code-01", "DEV3"))
        self.assertEqual((name, email), ("Code-01", "DEV3@subfloor.local"))

    def test_ident_env_pins_all_four_variables(self) -> None:
        env = run.shell_git_ident_env(_shell("Code-01", "DEV3"))
        self.assertEqual(env, {
            "GIT_AUTHOR_NAME": "Code-01",
            "GIT_AUTHOR_EMAIL": "DEV3@subfloor.local",
            "GIT_COMMITTER_NAME": "Code-01",
            "GIT_COMMITTER_EMAIL": "DEV3@subfloor.local",
        })

    def test_identity_is_whitespace_insensitive(self) -> None:
        env = run.shell_git_ident_env(_shell("  Code-01 ", " dev3 "))
        self.assertEqual(env["GIT_AUTHOR_NAME"], "Code-01")
        self.assertEqual(env["GIT_AUTHOR_EMAIL"], "dev3@subfloor.local")

    def test_missing_name_or_shortname_refuses_the_launch(self) -> None:
        for broken in (_shell("", "DEV3"), _shell("Code-01", None),
                       _shell(None, "DEV3"), _shell("  ", "DEV3")):
            with self.subTest(display_name=broken["display_name"],
                              shortname=broken["shortname"]), \
                    self.assertRaises(run.LaunchError):
                run.shell_git_identity(broken)

    def test_inherited_wrong_shell_identity_is_overridden(self) -> None:
        # The exact #1494 shape: the environment carries another shell's
        # (or an empty) ident; the exec seam must replace all four values.
        plan_env = {
            "GIT_AUTHOR_NAME": "Arch-Planner-01",
            "GIT_AUTHOR_EMAIL": "noreply@super-coder.local",
            "GIT_COMMITTER_NAME": "",
            "GIT_COMMITTER_EMAIL": "",
        }
        plan_env.update(run.shell_git_ident_env(_shell("Code-01", "DEV3")))
        self.assertEqual(plan_env["GIT_AUTHOR_NAME"], "Code-01")
        self.assertEqual(plan_env["GIT_AUTHOR_EMAIL"], "DEV3@subfloor.local")
        self.assertEqual(plan_env["GIT_COMMITTER_NAME"], "Code-01")
        self.assertEqual(plan_env["GIT_COMMITTER_EMAIL"], "DEV3@subfloor.local")
        self.assertNotIn("Planner", "".join(plan_env.values()))


class ExecSeamWiringTest(unittest.TestCase):
    """Both exec seams derive the ident from the booted shell's row."""

    def test_interactive_and_prepared_launches_pin_the_ident(self) -> None:
        source = (SCRIPTS / "run.py").read_text()
        self.assertEqual(source.count("env.update(shell_git_ident_env(full))"), 2)

    def test_derivation_never_reads_repo_git_config(self) -> None:
        source = (SCRIPTS / "run.py").read_text()
        self.assertNotIn("config user.name", source)
        self.assertNotIn("config user.email", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)