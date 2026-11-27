"""Refresh rolling 14-day unique cloners and Subfloor's cumulative clone archive."""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Only these public projects have opted into publishing their traffic.
PROJECTS = (
    "anj-cloud", "drive-it", "fai-browser", "gantt-it", "md-converter", "pick-it",
    "plasma-live-wallpapers", "rst-c", "sc-cachy", "splash-it", "storybench", "subfloor",
)


def refresh(archive):
    destination = archive / "unique-cloners.json"
    figures = json.loads(destination.read_text()) if destination.exists() else {}
    checked_on = datetime.now(timezone.utc).date().isoformat()
    failures = []
    for project in PROJECTS:
        api = f"repos/jedbjorn/{project}"
        try:
            # A project may have changed visibility since its badge was added.
            visibility = subprocess.check_output(["gh", "api", api, "--jq", ".visibility"], text=True)
            if visibility.strip() != "public":
                raise ValueError("repository is no longer public")
            payload = subprocess.check_output(["gh", "api", f"{api}/traffic/clones?per=day"])
            snapshot = json.loads(payload)
            uniques = snapshot["uniques"]
            if type(uniques) is not int or not 0 <= uniques <= snapshot["count"]:
                raise ValueError("invalid window-wide unique cloner count")
            if project == "subfloor":
                merge = Path(__file__).with_name("update-clone-counts.py")
                subprocess.run([sys.executable, merge, archive / "counts.json"], input=payload, check=True)
            figures[project] = {"unique_cloners_14d": uniques, "checked_on_utc": checked_on}
        except (subprocess.CalledProcessError, ValueError, KeyError, TypeError) as error:
            print(f"{project}: {error}", file=sys.stderr)
            failures.append(project)

    if figures:
        destination.write_text(json.dumps(figures, indent=2, sort_keys=True) + "\n")
    if failures:
        sys.exit("Could not refresh: " + ", ".join(failures))


if __name__ == "__main__":
    refresh(Path(sys.argv[1]))
