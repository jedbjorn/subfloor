#!/usr/bin/env python3
"""Parse the executable Cartographer shape-notice contract."""
from __future__ import annotations

import re
from dataclasses import dataclass


SHAPE_RE = re.compile(
    r"shape: (?P<summary>.+) — paths: (?P<paths>.+); ref: (?P<reference>.+)"
)
FLAG_RE = re.compile(r"(?P<flag_id>[1-9][0-9]*)=(?P<name>[A-Za-z][A-Za-z0-9]*-[0-9]+)")
FINAL_LINE = "curate; verify and close each flag; mark this notice read last."


class ShapeNoticeError(ValueError):
    pass


@dataclass(frozen=True)
class NoticeFlag:
    flag_id: int
    name: str


@dataclass(frozen=True)
class ShapeNotice:
    summary: str
    paths: str
    reference: str
    flags: tuple[NoticeFlag, ...]


def parse_shape_notice(body: str) -> ShapeNotice:
    lines = body.splitlines()
    if len(lines) != 3:
        raise ShapeNoticeError("shape notice must contain exactly three lines")
    shape = SHAPE_RE.fullmatch(lines[0])
    if shape is None:
        raise ShapeNoticeError("malformed shape line")
    if not lines[1].startswith("flags: "):
        raise ShapeNoticeError("shape notice must include a flags line")
    raw_flags = lines[1].removeprefix("flags: ")
    flags: list[NoticeFlag] = []
    if raw_flags != "none":
        if not raw_flags:
            raise ShapeNoticeError("flags line is empty")
        for raw_pair in raw_flags.split(", "):
            pair = FLAG_RE.fullmatch(raw_pair)
            if pair is None:
                raise ShapeNoticeError(f"malformed flag identity: {raw_pair}")
            flags.append(NoticeFlag(int(pair["flag_id"]), pair["name"]))
        if len({flag.flag_id for flag in flags}) != len(flags):
            raise ShapeNoticeError("duplicate flag ID")
    if lines[2] != FINAL_LINE:
        raise ShapeNoticeError("shape notice must carry the mark-read-last directive")
    return ShapeNotice(
        summary=shape["summary"],
        paths=shape["paths"],
        reference=shape["reference"],
        flags=tuple(flags),
    )
