#!/usr/bin/env python3
"""H-24, code half: no engine read path resolves a rendered sprint artifact.

Roadmap #30 / decisions #71-#72 policy, applied to the sprint envelope: the DB
is the source of truth, the flat `_sc` mirror is a record for FnB review. Spec
#76 pins it by test, the same method as the #30 audit — pin the audited surface,
force a human to classify anything new.

The regression this catches is cheap to write and expensive to own: reading
`docs_sc/SPRINT-*.md` back is the single most convenient way to answer "what did
that sprint do", and it silently reintroduces prose as a data source — the same
family as the five `status:` parsers this sprint removed. A render is stale the
moment a row moves, and unlike the row it has no writer discipline.

Audited baseline, 2026-07-27, sprint 84 U6 (verified by hand, then pinned):
every engine reference to the mirror trees is a WRITE, a DRIFT-CHECK, an
EXCLUSION, or a docstring citation. Not one is a read of a render as input.

Run:
    python3 tests/test_sprint_no_render_read.py
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"

# the rendered flat mirror — never an engine input
MIRROR_TOKENS = ("docs_sc", "specs_sc", "skills_sc", "roadmap_sc")

# reading the mirror IS the job of exactly these two, by design
MIRROR_OWNERS = {
    "render/flat.py",          # writes the mirror
    "scripts/render_check.py",  # reads it back only to diff against the DB
}

# every other module that may NAME a mirror path, with the role that makes it
# legal. Naming is fine; resolving one as input is not.
AUDITED_REFERENCES = {
    "api/server.py": "render orchestration, help text, render-target list",
    "scripts/map_repo.py": "excludes the mirror from the repo map",
    "api/db_broker.py": "docstring spec citation",
    "scripts/dbq.py": "docstring spec citation",
    "scripts/job.py": "docstring spec citation",
    "scripts/watch.py": "docstring spec citation",
    "scripts/seed_dogfood.py": "stores render_path as a WRITE destination",
    "scripts/install.py": "adds generated mirror paths to the ignore policy",
    "scripts/update.py": "untracks legacy generated mirror paths on upgrade",
}

# filesystem reads. `open` is a bare Name call; the rest are attribute calls.
READ_ATTRS = ("read_text", "read_bytes", "glob", "rglob", "iterdir",
              "listdir", "scandir", "open")

# a realistic violation, in the shape it would actually be written: resolve the
# rendered sprint report by path and parse it back. The detector must see this.
INJECTED_VIOLATION = '''
from pathlib import Path
ROOT = Path("/tmp")
def summarize(doc_id):
    return (ROOT / "docs_sc" / f"SPRINT-{doc_id}.md").read_text()
'''


def engine_sources():
    for path in sorted(ENGINE.rglob("*.py")):
        yield path.relative_to(ENGINE).as_posix(), path.read_text(errors="ignore")


def render_reads(rel: str, text: str) -> list:
    """Reads whose TARGET EXPRESSION names a mirror tree.

    AST, not line matching, and deliberately: `SPEC.read_text(),
    "specs_sc/…"` puts a read and a mirror token on one line while reading an
    asset and merely recording a render_path. Line matching calls that a
    violation. Only the receiver of the read call decides.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:                      # not ours to police
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr in READ_ATTRS:
            target = ast.get_source_segment(text, node.func.value) or ""
        elif isinstance(node.func, ast.Name) and node.func.id == "open":
            target = " ".join(ast.get_source_segment(text, a) or ""
                              for a in node.args[:1])
        else:
            continue
        if any(tok in target for tok in MIRROR_TOKENS):
            out.append(f"{rel}:{node.lineno}: {target.strip()}")
    return out


class NoRenderReadPathTest(unittest.TestCase):
    def test_the_detector_catches_an_injected_violation(self):
        """Validate the instrument against a known positive before trusting a
        single empty result from it.

        Every assertion below is an ABSENCE claim, and an absence claim from a
        detector that cannot see its target is unmeasured, not clean. The
        control is a synthetic violation in the shape one would really be
        written, because that is what the detector must catch — not the mirror
        writer, whose own path composition it is not built to follow.
        """
        found = render_reads("<injected>", INJECTED_VIOLATION)
        self.assertEqual(len(found), 1, f"detector blind to a real violation: {found}")
        self.assertIn("docs_sc", found[0])

    def test_the_detector_does_not_fire_on_a_recorded_render_path(self):
        """The other half of a trustworthy instrument: storing a render_path as
        a WRITE destination is legal, and a detector that reds on it would be
        removed within a sprint for crying wolf."""
        legal = ('from pathlib import Path\n'
                 'SPEC = Path("assets/seed/x.md")\n'
                 'row = (SPEC.read_text(), "specs_sc/x.md")\n')
        self.assertEqual(render_reads("<legal>", legal), [])

    def test_no_module_resolves_a_render_as_input(self):
        """The property itself: outside the mirror's two owners, no engine
        module reads a rendered artifact off disk."""
        offenders = []
        for rel, text in engine_sources():
            if rel in MIRROR_OWNERS:
                continue
            offenders.extend(render_reads(rel, text))
        self.assertEqual(
            offenders, [],
            "an engine module resolves a rendered artifact as INPUT. The DB is "
            "the read path; the mirror is a record for FnB review (roadmap #30, "
            "decision #71). Read the row, not the render.")

    def test_reference_surface_is_the_audited_one(self):
        """Coarser net, and the one that catches a read this file's primitive
        list has not learned yet: a NEW module naming the mirror at all must be
        classified by a human, not absorbed silently."""
        referencing = {
            rel for rel, text in engine_sources()
            if any(tok in text for tok in MIRROR_TOKENS)
        } - MIRROR_OWNERS
        self.assertEqual(
            referencing, set(AUDITED_REFERENCES),
            "the set of engine modules naming the rendered mirror changed. "
            "Classify the new reference: if it WRITES or CITES, add it to "
            "AUDITED_REFERENCES with its role; if it READS, it violates H-24.")

    def test_no_sprint_render_path_is_composed(self):
        """Sprint-specific, because sprint docs and SPRINT REPORTs are what
        H-24 names: nothing in the engine builds a path to one."""
        offenders = []
        for rel, text in engine_sources():
            if rel in MIRROR_OWNERS:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if any(tok in line for tok in MIRROR_TOKENS) and "SPRINT" in line:
                    offenders.append(f"{rel}:{n}: {line.strip()}")
        self.assertEqual(offenders, [],
                         "a rendered SPRINT artifact is being addressed by path")


if __name__ == "__main__":
    unittest.main()
