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

    def test_an_unlinked_broker_up_says_it_did_not_start_and_what_links_a_seat(
        self,
    ) -> None:
        # `vm status` used to answer "broker unreachable" and point here, and
        # this then printed "nothing to serve" and exited 0: a no-op hint.
        for kind, commands in (
            ("vm", ("./sc vm adopt", "./sc vm init", "./sc remote add")),
            ("ts", ("./sc ts init",)),
        ):
            with self.subTest(kind=kind):
                match = re.search(
                    rf"sc_{kind}_broker_up\(\) \{{(?P<body>.*?)\n\}}", SC, re.DOTALL
                )
                self.assertIsNotNone(match)
                unlinked = match.group("body").split("\n  fi\n", 1)[0]
                self.assertIn("configured; then", unlinked)
                self.assertIn("NOT started", unlinked)
                for command in commands:
                    self.assertIn(command, unlinked)
                # Still not a failure: `launch` calls this on every fork.
                self.assertIn("return 0", unlinked)


if __name__ == "__main__":
    unittest.main(verbosity=2)
