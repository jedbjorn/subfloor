"""Reference extractor: decorator-style HTTP routes → dr_endpoint.

Covers FastAPI (`@app.get("/x")`, `@router.post("/x")`) and Flask-style
`@app.route("/x", methods=[...])`. Best-effort: routes with non-literal paths,
computed methods, or registered via `add_api_route()` / `add_url_rule()` are NOT
caught — by design (most, not 100%).

Adopt by copying into `.sc-state/map_extractors/`; change FRAMEWORK / the file
filter if your routers live somewhere specific.
"""
import re

FRAMEWORK = "fastapi"

# @app.get("/x")  ·  @router.post("/x")  — a method decorator with a literal path.
_METHOD_RE = re.compile(
    r"""@\s*[A-Za-z_][\w.]*\.(get|post|put|patch|delete|head|options)\s*\(\s*[frbu]*['"]([^'"]+)['"]""",
    re.I)
# @app.route("/x", methods=["GET", "POST"])  — Flask / generic.
_ROUTE_RE = re.compile(
    r"""@\s*[A-Za-z_][\w.]*\.route\s*\(\s*[frbu]*['"]([^'"]+)['"](.*)""", re.I)
_METHODS_RE = re.compile(r"""methods\s*=\s*\[([^\]]*)\]""", re.I)


def _py_files(con):
    return [r["path"] for r in con.execute(
        "SELECT path FROM dr_filepath WHERE lang='Python' ORDER BY path")]


def extract(con, repo_root, cfg) -> str:
    con.execute("DELETE FROM dr_endpoint WHERE framework=?", (FRAMEWORK,))
    n = 0
    for rel in _py_files(con):
        try:
            text = (repo_root / rel).read_text(errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            m = _METHOD_RE.search(line)
            if m:
                con.execute(
                    "INSERT INTO dr_endpoint (method, path, handler, framework, source_file) "
                    "VALUES (?,?,?,?,?)",
                    (m.group(1).upper(), m.group(2), f"{rel}:{i}", FRAMEWORK, rel))
                n += 1
                continue
            r = _ROUTE_RE.search(line)
            if r:
                methods = "GET"
                mm = _METHODS_RE.search(r.group(2))
                if mm:
                    parts = [s.strip(" '\"") for s in mm.group(1).split(",") if s.strip()]
                    methods = ",".join(parts) or "GET"
                con.execute(
                    "INSERT INTO dr_endpoint (method, path, handler, framework, source_file) "
                    "VALUES (?,?,?,?,?)",
                    (methods.upper(), r.group(1), f"{rel}:{i}", FRAMEWORK, rel))
                n += 1
    return f"{n} endpoints"
