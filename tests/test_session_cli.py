#!/usr/bin/env python3
"""Public ``sc session`` command contract."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SPEC = importlib.util.spec_from_file_location(
    "session_cli_public", ENGINE / "scripts" / "session_cli.py"
)
session_cli = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = session_cli
SPEC.loader.exec_module(session_cli)


def payload(state: str = "idle") -> dict:
    return {
        "binding": {
            "binding_id": 7, "harness": "codex", "native_session_id": "thread-7",
            "state": state, "managed": 1, "last_error": None,
        },
        "archive": {"session_id": "0007", "model": "gpt-test"},
        "jobs": {},
        "summary": {"queued": 0, "errors": 0, "owner": "none"},
    }


class PublicSessionCliTest(unittest.TestCase):
    def test_manage_requires_sprint_and_targets_shortname(self):
        parser = session_cli.build_operator_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["manage", "PLN1"])
        args = parser.parse_args(["manage", "PLN1", "--sprint", "21"])
        with mock.patch.object(
            session_cli, "operator_api", return_value=payload()
        ) as request, mock.patch.object(session_cli, "print_status"):
            self.assertEqual(0, args.fn(args))
        request.assert_called_once_with(
            "POST", "manage", "PLN1", {"sprint_ref": "21"}
        )

    def test_release_refuses_dispatch_without_after_turn(self):
        args = session_cli.build_operator_parser().parse_args(["release", "PLN1"])
        with mock.patch.object(
            session_cli, "operator_api", return_value=payload("dispatching")
        ):
            with self.assertRaises(SystemExit) as caught:
                args.fn(args)
        self.assertIn("pass --after-turn", str(caught.exception))

    def test_release_after_turn_waits_then_releases(self):
        args = session_cli.build_operator_parser().parse_args(
            ["release", "PLN1", "--after-turn"]
        )
        with mock.patch.object(
            session_cli, "operator_api",
            side_effect=[payload("dispatching"), payload("idle"), payload("released")],
        ) as request, mock.patch.object(session_cli.time, "sleep") as sleep, \
                mock.patch.object(session_cli, "print_status"):
            self.assertEqual(0, args.fn(args))
        sleep.assert_called_once_with(1)
        self.assertEqual([
            mock.call("GET", "status", "PLN1"),
            mock.call("GET", "status", "PLN1"),
            mock.call("POST", "release", "PLN1", {}),
        ], request.call_args_list)


if __name__ == "__main__":
    unittest.main()
