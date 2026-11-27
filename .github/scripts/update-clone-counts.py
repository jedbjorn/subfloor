"""Merge GitHub's daily clone traffic from stdin into the public count archive."""

import json
import sys
from datetime import date
from pathlib import Path

archive = Path(sys.argv[1])
days = json.loads(archive.read_text())["days"]
snapshot = json.load(sys.stdin)
incoming = {}
for row in snapshot["clones"]:
    day = date.fromisoformat(row["timestamp"].removesuffix("T00:00:00Z")).isoformat()
    count = row["count"]
    if type(count) is not int or count < 0 or day in incoming:
        raise ValueError(f"Invalid daily clone count for {day}")
    incoming[day] = count

if sum(incoming.values()) != snapshot["count"]:
    raise ValueError("Daily counts disagree with the GitHub snapshot total")

if incoming:
    # The oldest bucket can be partial at the edge of the rolling window.
    oldest = min(incoming)
    incoming[oldest] = max(days.get(oldest, 0), incoming[oldest])
days.update(incoming)
result = {"days": dict(sorted(days.items())), "total": sum(days.values())}
archive.write_text(json.dumps(result, indent=2) + "\n")
