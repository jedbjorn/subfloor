"""Explicit disposable experiment seam; production runtime startup is unchanged.

Foundation alone starts no drivers/broker/reaper. The integration lane may bind
its named experimental API clients here. API shutdown releases only clients;
the fixture ledger owner stops all registered units before deleting any state.
"""
from __future__ import annotations

from pathlib import Path


def start_fixture(*,database: Path,root: Path,fixture_id: str,supervisor):
    if database.resolve()!=root/'.super-coder/shell_db.db' or not fixture_id:
        raise ValueError('synthetic fixture identity required')
    def shutdown():
        # A stopped/restarted API must not end an open chat's controller.
        # The independent finite supervisor deadline and maintainer stop own
        # OS cleanup; task870 binds API consumer release here.
        return None
    return shutdown
