"""YAML-safe scalars for skill frontmatter: one writer, one reader.

A rendered SKILL.md is read by harness YAML parsers, not only by the engine.
A free-text value written bare is theirs to reinterpret: ` #` starts a
comment, `: ` opens a mapping, a leading quote/bracket/`*`/`&`/`!` is an
indicator, `true`/`12` are not strings. `emit_scalar` writes a value bare
only when a YAML parser reads the bare form back as that exact string, and
double-quotes it otherwise, so renders of ordinary prose stay byte-stable.

`parse_scalar` is the reader for a fork-local draft (`sc skill put`). It
accepts the quoted forms and the legacy bare form: a bare value is taken
whole, comment characters included, because that is how every value was
stored before quoting existed. Engine assets are not read through it: an
asset value is taken literally (`seed_skills.parse_skill`), so a reseed never
changes a stored value.
"""

from __future__ import annotations

import re

_LEADING_INDICATORS = frozenset("!&*|>'\"%@`{}[],#")
_BLOCK_INDICATORS = ("- ", "? ", ": ")
_RESERVED_WORDS = frozenset(
    {"null", "~", "true", "false", "yes", "no", "on", "off", "y", "n", "<<", "="}
)
# A single token that opens like a number or special float may resolve to a
# non-string type under some YAML 1.1 or 1.2 schema; quoting every such token
# is cheap because prose is never a lone numeric token.
_NUMERIC_TOKEN = re.compile(r"[-+.]?[0-9]|[-+]?\.(inf|nan)$", re.IGNORECASE)
# The YAML 1.1 implicit resolvers that match a whole value and may contain
# spaces, so the single-token rule above cannot see them: timestamps
# (`2001-12-14 21:59:43`) and sexagesimal numbers are the live cases; the
# rest are listed so the rule reads as the resolver table it mirrors.
_IMPLICIT_TYPES = re.compile(
    r"""^(?:
      yes|no|true|false|on|off|y|n                                  # bool
    | ~|null|                                                       # null
    | <<|=                                                          # merge, value
    | [-+]?(?:0b[0-1_]+|0x[0-9a-f_]+|0o?[0-7_]+|[0-9][0-9_]*)       # int
    | [-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+(?:\.[0-9_]*)?              # sexagesimal
    | [-+]?(?:[0-9][0-9_]*)?\.?[0-9_]*(?:e[-+]?[0-9]+)?             # float
    | [-+]?\.(?:inf|nan)                                            # special float
    | [0-9]{4}-[0-9]{1,2}-[0-9]{1,2}                                # date
      (?:(?:t|[ \t]+)[0-9]{1,2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]*)?
         (?:[ \t]*(?:z|[-+][0-9]{1,2}(?::[0-9]{2})?))?)?            # timestamp
    )$""",
    re.IGNORECASE | re.VERBOSE,
)
_UNSAFE_CHARS = re.compile(
    "[\x00-\x1f\x7f-\x9f  ﻿\ud800-\udfff￾￿]"
)

_ESCAPES = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n",
    "v": "\v", "f": "\f", "r": "\r", "e": "\x1b", " ": " ", '"': '"',
    "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0", "L": " ",
    "P": " ",
}
_HEX_ESCAPES = {"x": 2, "u": 4, "U": 8}
_SHORT_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\t": "\\t", "\r": "\\r"}


def needs_quoting(value: str) -> bool:
    """Whether a YAML parser would read the bare value as anything else."""
    if not value or value != value.strip():
        return True
    if value[0] in _LEADING_INDICATORS or value.startswith(_BLOCK_INDICATORS):
        return True
    if value in ("-", "?", ":") or value.startswith(("---", "...")):
        return True
    if ": " in value or " #" in value or value.endswith(":"):
        return True
    if _UNSAFE_CHARS.search(value):
        return True
    if _IMPLICIT_TYPES.match(value):
        return True
    if not any(ch.isspace() for ch in value):
        if value.lower() in _RESERVED_WORDS or _NUMERIC_TOKEN.match(value):
            return True
    return False


def _escape_char(ch: str) -> str:
    code = ord(ch)
    if code <= 0xFF:
        return f"\\x{code:02x}"
    if code <= 0xFFFF:
        return f"\\u{code:04x}"
    return f"\\U{code:08x}"


def emit_scalar(value: object) -> str:
    """Render one frontmatter value: bare when safe, double-quoted otherwise."""
    text = "" if value is None else str(value)
    if not needs_quoting(text):
        return text
    out = []
    for ch in text:
        if ch in _SHORT_ESCAPES:
            out.append(_SHORT_ESCAPES[ch])
        elif _UNSAFE_CHARS.match(ch):
            out.append(_escape_char(ch))
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _decode_double_quoted(inner: str) -> str | None:
    out = []
    index = 0
    while index < len(inner):
        ch = inner[index]
        if ch == '"':
            return None
        if ch != "\\":
            out.append(ch)
            index += 1
            continue
        index += 1
        if index >= len(inner):
            return None
        code = inner[index]
        index += 1
        if code in _ESCAPES:
            out.append(_ESCAPES[code])
            continue
        width = _HEX_ESCAPES.get(code)
        digits = inner[index:index + width] if width else ""
        if not width or len(digits) != width:
            return None
        try:
            out.append(chr(int(digits, 16)))
        except (ValueError, OverflowError):
            return None
        index += width
    return "".join(out)


def _decode_single_quoted(inner: str) -> str | None:
    if "'" in inner.replace("''", ""):
        return None
    return inner.replace("''", "'")


def parse_scalar(raw: str) -> str:
    """Read one frontmatter value written bare, double-, or single-quoted.

    A value that only looks quoted (`"a" and "b"`) is not a quoted scalar and
    is returned whole, like any other legacy bare value.
    """
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        inner = text[1:-1]
        decoded = (
            _decode_double_quoted(inner)
            if text[0] == '"'
            else _decode_single_quoted(inner)
        )
        if decoded is not None:
            return decoded
    return text
