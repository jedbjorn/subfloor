"""A stand-in `gh` for the hermetic `sc job watch pr` tests.

It answers the two reads the PR reader makes, from the recorded responses in
tests/fixtures/review/github_prs.json. `GH_STUB_MAP` names a JSON file mapping
a watched PR number to the recorded PR whose response it returns; the test
rewrites that file to move a PR from one recorded state to another.
"""
import json
import os
import sys
from pathlib import Path

RECORDED = Path(__file__).resolve().parents[1] / "review" / "github_prs.json"


def main() -> int:
    argv = sys.argv[1:]
    with open(os.environ["GH_STUB_CALLS"], "a") as handle:
        handle.write(json.dumps(argv) + "\n")
    if argv[:2] == ["run", "list"]:
        print("[]")
        return 0
    if argv[:2] != ["pr", "view"]:
        print(f"stub gh: unsupported read {argv[:2]}", file=sys.stderr)
        return 1
    mapping = json.loads(Path(os.environ["GH_STUB_MAP"]).read_text())
    source = mapping.get(argv[2])
    if source is None:
        print("GraphQL: Could not resolve to a PullRequest", file=sys.stderr)
        return 1
    for recorded in json.loads(RECORDED.read_text())["pull_requests"]:
        if recorded["number"] == source:
            print(json.dumps({**recorded, "number": int(argv[2])}))
            return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
