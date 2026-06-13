"""Reference extractor: SQL CREATE TABLE/VIEW → dr_db_table + dr_db_column.

Parses `CREATE TABLE` / `CREATE VIEW` from the `.sql` files the map already
knows about. Column parse is best-effort: a top-level comma split of the column
body, skipping table-level constraints (PRIMARY KEY / FOREIGN KEY / UNIQUE /
CHECK / CONSTRAINT). ORM-defined schemas (Django, SQLAlchemy models) are NOT
covered — write a model extractor for those.

Optional config in `.sc-state/map.config.json`:
  "extractors": { "sqlite_schema": { "exclude_prefixes": ["vendor/", "test/"] } }
"""
import re

_CREATE_RE = re.compile(
    r"""CREATE\s+(TABLE|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?["'`\[]?(\w+)["'`\]]?""", re.I)
_CONSTRAINT_KW = ("primary", "foreign", "unique", "check", "constraint", "key")
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _strip_comments(text):
    """Drop SQL line + block comments so they aren't parsed as columns."""
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", text))


def _sql_files(con, cfg):
    excl = ((cfg.get("extractors", {}) or {}).get("sqlite_schema", {}) or {}).get(
        "exclude_prefixes", [])
    out = []
    for r in con.execute("SELECT path FROM dr_filepath WHERE lang='SQL' ORDER BY path"):
        if not any(r["path"].startswith(x) for x in excl):
            out.append(r["path"])
    return out


def _balanced(text, start):
    """Content between the first '(' at/after `start` and its matching ')'."""
    i = text.find("(", start)
    if i < 0:
        return None
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return text[i + 1:j]
    return None


def _columns(body):
    cols, depth, buf = [], 0, ""
    for ch in body:
        if ch == "(":
            depth += 1
            buf += ch
        elif ch == ")":
            depth -= 1
            buf += ch
        elif ch == "," and depth == 0:
            cols.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        cols.append(buf)
    out = []
    for c in cols:
        c = c.strip()
        if not c:
            continue
        first = c.split()[0].strip('"`[]')
        if first.lower() in _CONSTRAINT_KW:
            continue
        toks = c.split()
        typ = toks[1].strip('"`[],') if len(toks) > 1 else None
        pk = 1 if re.search(r"primary\s+key", c, re.I) else 0
        nn = 1 if re.search(r"not\s+null", c, re.I) else 0
        out.append((first, typ, pk, nn))
    return out


def extract(con, repo_root, cfg) -> str:
    con.execute("DELETE FROM dr_db_table")
    con.execute("DELETE FROM dr_db_column")
    nt = nc = 0
    for rel in _sql_files(con, cfg):
        try:
            text = _strip_comments((repo_root / rel).read_text(errors="ignore"))
        except OSError:
            continue
        for m in _CREATE_RE.finditer(text):
            kind, name = m.group(1).lower(), m.group(2)
            con.execute(
                "INSERT INTO dr_db_table (name, kind, source_file) VALUES (?,?,?)",
                (name, kind, rel))
            nt += 1
            if kind == "table":
                body = _balanced(text, m.end())
                if body:
                    for (cname, ctype, pk, nn) in _columns(body):
                        con.execute(
                            "INSERT INTO dr_db_column "
                            "(table_name, name, type, pk, not_null, source_file) "
                            "VALUES (?,?,?,?,?,?)", (name, cname, ctype, pk, nn, rel))
                        nc += 1
    return f"{nt} tables, {nc} columns"
