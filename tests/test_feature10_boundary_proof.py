"""Executable traceability contract for Feature 10 / Spec 186 acceptance."""

from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROOF_PATH = ROOT / "tests" / "fixtures" / "feature10_boundary_proof.json"


def _proof() -> dict[str, object]:
    return json.loads(PROOF_PATH.read_text(encoding="utf-8"))


def _test_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def test_feature10_proof_binds_governing_spec_decision_and_merged_units() -> None:
    proof = _proof()

    assert proof["feature_id"] == 10
    assert proof["spec_id"] == 186
    assert proof["decision_id"] == 302
    assert proof["spec_revision_sha256"] == (
        "31542632d8eae0414b90f11369309b55eb6687713856637346d0cc1dcccf8fb1"
    )
    assert proof["prerequisite_prs"] == [1420, 1424, 1425, 1426, 1427]


def test_every_spec_acceptance_item_has_live_test_anchors() -> None:
    proof = _proof()
    verification = proof["verification"]
    assert isinstance(verification, list)
    assert [item["id"] for item in verification] == [
        f"V{index:02d}" for index in range(1, 16)
    ]

    parsed: dict[Path, set[str]] = {}
    for item in verification:
        assert item["claim"].strip()
        assert item["anchors"]
        for anchor in item["anchors"]:
            relative, separator, test_name = anchor.partition("#")
            assert separator == "#", anchor
            path = ROOT / relative
            assert path.is_file(), anchor
            names = parsed.setdefault(path, _test_names(path))
            assert test_name in names, anchor


def test_integrated_gate_and_weak_model_canary_contract_is_complete() -> None:
    proof = _proof()
    gates = proof["gates"]
    assert gates["full"] == "./sc test tests/ -q"
    assert gates["verify"] == "./sc verify"
    assert gates["render_check"] == "./sc render-check"
    assert len(gates["targeted"]) == len(set(gates["targeted"]))
    assert all((ROOT / path).is_file() for path in gates["targeted"])

    canary = proof["canary"]
    assert canary == {
        "decision_id": 298,
        "model": "halo/qwen3.8-27b-q8-mtp2",
        "variant": None,
        "flavors": ["admin", "cartographer", "planner", "dev", "reviewer"],
        "required_receipts": [
            "campaign.json",
            "admin/receipt.json",
            "cartographer/receipt.json",
            "planner/receipt.json",
            "dev/receipt.json",
            "reviewer/receipt.json",
            "synthesis.json",
            "final-baseline.json",
        ],
    }
