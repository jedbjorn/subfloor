"""Spec #232 AC6 — no shipped surface names the retired winbox vocabulary.

`configure_winbox` and `windows_vm_gui` are tombstoned skills; `transfer_dir`
is the retired host-share field of the `vm` block, replaced by `scp` push/pull
into the guest `workspace`. Shipped surfaces must not name any of the three.

Excluded, deliberately: `assets/skill_tombstones.json` (the permanent name
reservation registry, which must keep naming the retired skills), the
migration ledger (historical SQL is never rewritten), and the tests
themselves.

The two exceptions to "the migration ledger is excluded" are the guest scripts
under `assets/winbox/` - shipped surfaces like any other - and the trailing
reseed `0264`, which carries the CURRENT skill bodies verbatim and would
therefore reintroduce a retired name into every fork's database. The historical
migrations before it stay out.
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"

COMPAT_MARKER = "retired-compat (spec #232)"
RETIRED_TOKENS = ("configure_winbox", "windows_vm_gui", "transfer_dir")

SEARCH_TREES = (
    ENGINE / "scripts",
    ENGINE / "api",
    ENGINE / "ui",
    ENGINE / "assets" / "skills",
    ENGINE / "assets" / "winbox",
    ENGINE / "docs",
)
SEARCH_FILES = (
    ENGINE / "scripts" / "dispatch.sh",
    # Not historical SQL: 0264 carries the live skill bodies, so a retired
    # name here reaches every fork's database on the next migrate.
    ENGINE / "migrations" / "0264_reseed_winbox_adoption_skills.sql",
)

EXCLUDED = {
    (ENGINE / "assets" / "skill_tombstones.json").resolve(),
}
EXCLUDED_DIR_NAMES = {"__pycache__", "node_modules", ".git"}
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".woff",
    ".woff2", ".ttf", ".pyc", ".so",
}


def _shipped_files() -> list[Path]:
    seen: dict[Path, None] = {}
    for tree in SEARCH_TREES:
        for path in sorted(tree.rglob("*")):
            if not path.is_file():
                continue
            if any(part in EXCLUDED_DIR_NAMES for part in path.parts):
                continue
            if path.suffix.lower() in BINARY_SUFFIXES:
                continue
            resolved = path.resolve()
            if resolved in EXCLUDED:
                continue
            seen[resolved] = None
    for path in SEARCH_FILES:
        if path.is_file():
            seen[path.resolve()] = None
    return list(seen)


class WinboxRetirementGrepTest(unittest.TestCase):
    def test_shipped_surfaces_are_searchable(self) -> None:
        """Guard the guard: the trees exist and the walk finds real files."""
        for tree in SEARCH_TREES:
            with self.subTest(tree=tree.name):
                self.assertTrue(tree.is_dir(), tree)
        for path in SEARCH_FILES:
            with self.subTest(file=path.name):
                self.assertTrue(path.is_file(), path)
        self.assertGreater(len(_shipped_files()), 50)

    def test_no_shipped_surface_names_a_retired_winbox_token(self) -> None:
        offenders: dict[str, list[str]] = {token: [] for token in RETIRED_TOKENS}
        for path in _shipped_files():
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            # The vm block must still recognise the retired field name in
            # order to strip it from old instance.json files; those lines
            # carry the retired-compat marker and are the only exemption.
            live = "\n".join(
                line for line in text.splitlines() if COMPAT_MARKER not in line
            )
            for token in RETIRED_TOKENS:
                if token in live:
                    offenders[token].append(str(path.relative_to(ROOT)))
        for token, hits in offenders.items():
            with self.subTest(token=token):
                self.assertEqual(hits, [], f"{token} still named by: {hits}")

    def test_tombstone_registry_still_reserves_the_retired_skill_names(self) -> None:
        """The exclusion above is only sound while the registry keeps them."""
        registry = (ENGINE / "assets" / "skill_tombstones.json").read_text()
        for name in ("configure_winbox", "windows_vm_gui"):
            with self.subTest(skill=name):
                self.assertIn(f'"{name}"', registry)


if __name__ == "__main__":
    unittest.main()
