#!/bin/bash
# TUI and Admin are untouched, including stdin and evidence files.
[[ ${SC_SEAT:-} == gui && ${SC_SHELL_FLAVOR:-} != admin ]] || exit 0
exec "${SC_PYTHON:-python3}" "$(dirname "${BASH_SOURCE[0]}")/seat_convert.py" hook
