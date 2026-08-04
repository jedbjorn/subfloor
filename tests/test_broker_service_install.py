#!/usr/bin/env python3
"""Systemd broker installs must rotate an already-running stale ExecStart."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SC = (ROOT / ".super-coder" / "scripts" / "dispatch.sh").read_text()


class BrokerServiceInstallContractTest(unittest.TestCase):
    def test_every_broker_install_enables_then_restarts(self) -> None:
        for kind, variable in (
            ("vm", "VM_BROKER_UNIT"),
            ("ts", "TS_BROKER_UNIT"),
            ("pm2", "PM2_BROKER_UNIT"),
            ("db", "DB_BROKER_UNIT"),
        ):
            with self.subTest(kind=kind):
                match = re.search(
                    rf"sc_{kind}_broker_install\(\) \{{(?P<body>.*?)\n\}}",
                    SC,
                    re.DOTALL,
                )
                self.assertIsNotNone(match)
                body = match.group("body")
                self.assertIn(
                    f'systemctl --user enable "${variable}"', body
                )
                self.assertIn(
                    f'systemctl --user restart "${variable}"', body
                )
                self.assertNotIn(
                    f'systemctl --user enable --now "${variable}"', body
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
