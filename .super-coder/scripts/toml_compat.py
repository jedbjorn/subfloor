#!/usr/bin/env python3
"""Optional standard-library TOML parsing across the Python 3.9+ host floor."""
from __future__ import annotations

try:
    import tomllib as _tomllib
except ImportError:  # Python 3.9/3.10: TOML enrichment is advisory.
    _tomllib = None


AVAILABLE = _tomllib is not None


def loads(text: str) -> dict:
    """Parse TOML when the running interpreter provides a parser.

    Callers own best-effort behavior and should skip TOML-derived enrichment
    when ``AVAILABLE`` is false. Keeping the failure explicit prevents an
    accidental empty parse from being mistaken for valid TOML.
    """
    if _tomllib is None:
        raise RuntimeError("TOML parsing is unavailable on this interpreter")
    return _tomllib.loads(text)
