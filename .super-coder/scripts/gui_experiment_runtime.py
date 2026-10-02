"""Explicit disposable experiment seam; production runtime startup is unchanged.

Foundation alone starts no drivers/broker/reaper. The integration lane may bind
its named experimental API clients here. Fixture shutdown always stops each
registered owned native unit before its database/source root can be removed.
"""
from __future__ import annotations

from pathlib import Path


def start_fixture(*,database: Path,root: Path,fixture_id: str,supervisor):
    if database.resolve()!=root/'.super-coder/shell_db.db' or not fixture_id:
        raise ValueError('synthetic fixture identity required')
    def shutdown():
        for native in supervisor.inventory():
            if not native.get('os_cleanup',{}).get('complete'):
                supervisor.stop(native['generation_id'])
    return shutdown
