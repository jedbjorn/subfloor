"""Reference extractor: SvelteKit filesystem routes + components → dr_route / dr_component.

SvelteKit routes are the filesystem — no decorators. Files under a `routes/` dir
named `+page.svelte` / `+page.server.*` / `+server.*` / `+layout.svelte` map to a
URL derived from the directory path: `[param]` → `:param`, `[...rest]` → `*rest`,
`(group)` dropped. Every other `*.svelte` file is a component.

Adapt the `routes/` anchor + `+file` convention for Next.js (`app/` + `route.ts`,
`page.tsx`) or others.
"""
import re

_ROUTE_FILE = re.compile(r"\+(page|layout|server)\b")
_KIND = {"page": "page", "layout": "layout", "server": "endpoint"}


def _svelte_files(con):
    return [r["path"] for r in con.execute(
        "SELECT path FROM dr_filepath WHERE path LIKE '%.svelte' "
        "OR path LIKE '%/+server.%' OR path LIKE '%/+page.%' "
        "OR path LIKE '%/+layout.%' ORDER BY path")]


def _route_path(rel):
    if "routes/" not in rel:
        return None
    tail = rel.rsplit("routes/", 1)[1]
    segs = []
    for p in tail.split("/")[:-1]:  # drop the +file leaf
        if p.startswith("(") and p.endswith(")"):
            continue  # layout group — not part of the URL
        p = re.sub(r"\[\.\.\.(\w+)\]", r"*\1", p)
        p = re.sub(r"\[(\w+)\]", r":\1", p)
        segs.append(p)
    return "/" + "/".join(segs)


def extract(con, repo_root, cfg) -> str:
    con.execute("DELETE FROM dr_route WHERE framework='sveltekit'")
    con.execute("DELETE FROM dr_component WHERE framework='svelte'")
    nr = ncomp = 0
    for rel in _svelte_files(con):
        base = rel.rsplit("/", 1)[-1]
        m = _ROUTE_FILE.search(base)
        if m and "routes/" in rel:
            con.execute(
                "INSERT INTO dr_route (path, file, kind, framework) VALUES (?,?,?,?)",
                (_route_path(rel), rel, _KIND[m.group(1)], "sveltekit"))
            nr += 1
        elif base.endswith(".svelte"):
            con.execute(
                "INSERT INTO dr_component (name, path, framework) VALUES (?,?,?)",
                (base[:-len(".svelte")], rel, "svelte"))
            ncomp += 1
    return f"{nr} routes, {ncomp} components"
