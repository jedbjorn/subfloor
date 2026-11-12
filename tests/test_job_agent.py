"""Subagent command, result capture, worktree and environment (Feature #96, spec #273).

Acceptance criteria covered here: 1 (model and effort reach each harness by its
adapter's transport), 2 (refusals before anything starts), 3 (omitted effort
sends no effort setting), 4 (a subagent does not occupy its launching shell)
and 17 (Kimi and OpenCode match the recorded runs). The recordings are the
task #910 fixture: structure only, taken from the installed CLIs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))

import job_agent  # noqa: E402
import run as run_mod  # noqa: E402
import runs  # noqa: E402
import shell_factory  # noqa: E402
import shell_liveness  # noqa: E402
from conversation_launch import (  # noqa: E402
    ConversationLaunchError,
    ConversationLaunchPreparer,
)
from test_conversation_launch import launch_case, make_run  # noqa: E402,F401

FIXTURE = json.loads((ROOT / "tests/fixtures/job_agent/harness_controls.json").read_text())
HARNESSES = FIXTURE["harnesses"]
NONCE = FIXTURE["nonce"]
SESSION = "11111111-2222-4333-8444-555555555555"


def build(harness, model="model-x", effort=None, **kwargs):
    """The builder with the host taken out: every binary present, no MCP, no sandbox."""
    env = {k: v for k, v in os.environ.items() if k not in ("SC_SANDBOX", "SC_DISABLED_HARNESSES")}
    env.update(kwargs.pop("env", {}))
    with mock.patch.object(run_mod, "managed_mcp_injection", return_value=None), \
            mock.patch.dict(os.environ, env, clear=True), \
            mock.patch.object(job_agent.uuid, "uuid4", return_value=SESSION):
        return job_agent.build_command(
            harness, model, effort,
            which=kwargs.pop("which", lambda binary: f"/usr/bin/{binary}"),
            cli_version=lambda binary: f"{binary} 0.0.0",
            env=env, **kwargs,
        )


class CommandTransportTest(unittest.TestCase):
    """AC 1: argv and environment match each adapter's transport."""

    def test_claude_takes_effort_as_a_flag_and_the_brief_on_stdin(self):
        command = build("claude", "haiku", "high")
        self.assertEqual(
            command.argv,
            ["claude", "--model", "haiku", "--effort", "high",
             "--dangerously-skip-permissions", "--session-id", SESSION,
             "--output-format", "json", "-p"],
        )
        self.assertEqual((command.env, command.env_unset), ({}, ()))
        self.assertEqual(command.stdin, "{run_dir}/brief.md")
        self.assertEqual(command.session_ref, SESSION)
        self.assertEqual(
            (command.requested_model, command.requested_effort, command.effective_effort),
            ("haiku", "high", "high"))
        self.assertEqual(command.cli_version, "claude 0.0.0")

    def test_codex_takes_effort_as_a_config_key(self):
        command = build("codex", "model-x", "xhigh")
        self.assertEqual(
            command.argv,
            ["codex", "exec", "-m", "model-x", "-c", 'model_reasoning_effort="xhigh"',
             "--sandbox", "danger-full-access", "--json",
             "-o", "{run_dir}/last-message.md", "-"],
        )
        self.assertEqual((command.env, command.env_unset), ({}, ()))
        self.assertEqual(command.stdin, "{run_dir}/brief.md")
        self.assertIsNone(command.session_ref)

    def test_kimi_takes_effort_in_the_environment_and_the_brief_as_an_argument(self):
        command = build("kimi", "kimi-code/alias", "high")
        self.assertEqual(
            command.argv,
            ["kimi", "-m", "kimi-code/alias", "--output-format", "stream-json", "-p", "{brief}"],
        )
        self.assertEqual(command.env, {"KIMI_MODEL_THINKING_EFFORT": "high"})
        self.assertEqual(command.env_unset, ())
        self.assertIsNone(command.stdin)

    def test_opencode_takes_effort_as_a_variant(self):
        command = build("opencode", "provider/model-x", "max")
        self.assertEqual(
            command.argv,
            ["opencode", "run", "--model", "provider/model-x", "--variant", "max",
             "--format", "json"],
        )
        self.assertEqual((command.env, command.env_unset), ({}, ()))
        self.assertEqual(command.stdin, "{run_dir}/brief.md")

    def test_the_brief_never_enters_the_recorded_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "brief.md").write_text("check the spec sites\n")
            argv, stdin = build("kimi").materialize(run_dir)
            self.assertEqual(argv[-2:], ["-p", "check the spec sites\n"])
            self.assertIsNone(stdin)
            argv, stdin = build("codex").materialize(run_dir)
            self.assertEqual(argv[-3:], ["-o", f"{run_dir}/last-message.md", "-"])
            self.assertEqual(stdin, run_dir / "brief.md")
        for harness in ("claude", "codex", "kimi", "opencode"):
            self.assertNotIn("check the spec", " ".join(build(harness).argv))


class EngineTokenTest(unittest.TestCase):
    """Only the tokens the engine placed are replaced, never a caller's value."""

    def test_a_model_that_spells_a_token_is_passed_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "brief.md").write_text("the real brief")
            for harness in ("kimi", "codex", "claude", "opencode"):
                for model in ("{brief}", "{run_dir}/last-message.md", "x{run_dir}y"):
                    with self.subTest(harness=harness, model=model):
                        command = build(harness, model)
                        argv, _stdin = command.materialize(run_dir)
                        self.assertEqual(argv.count(model), 1)
                        self.assertEqual(argv.count("the real brief"),
                                         1 if harness == "kimi" else 0)
                        self.assertEqual(
                            len(argv), len(command.argv))
            resumed = build("opencode", "provider/m", resume_session="{brief}")
            self.assertIn("{brief}", resumed.materialize(run_dir)[0])

    def test_token_positions_are_recorded_at_build_time(self):
        kimi = build("kimi")
        self.assertEqual(kimi.argv[kimi.brief_index], "{brief}")
        self.assertEqual(kimi.run_dir_indexes, ())
        codex = build("codex")
        self.assertIsNone(codex.brief_index)
        self.assertEqual(
            [codex.argv[i] for i in codex.run_dir_indexes], ["{run_dir}/last-message.md"])
        for harness in ("claude", "opencode"):
            command = build(harness)
            self.assertEqual((command.brief_index, command.run_dir_indexes), (None, ()))
        # No assembly mark survives into the recorded command.
        for harness in ("claude", "codex", "kimi", "opencode"):
            self.assertFalse(any("\x1f" in arg for arg in build(harness).argv))


class OmittedEffortTest(unittest.TestCase):
    """AC 3: no effort flag, config key or variable; the default is recorded."""

    def test_no_harness_receives_an_effort_setting(self):
        for harness in ("claude", "codex", "kimi", "opencode"):
            with self.subTest(harness=harness):
                command = build(harness, env={"KIMI_MODEL_THINKING_EFFORT": "max"})
                text = " ".join(command.argv)
                for marker in ("--effort", "model_reasoning_effort", "--variant"):
                    self.assertNotIn(marker, text)
                self.assertEqual(command.env, {})
                self.assertIsNone(command.requested_effort)
                self.assertEqual(command.effective_effort, "default")
                # `sc run` would have fallen back to high here; a subagent does not.
                self.assertNotIn("high", command.argv)

    def test_an_inherited_effort_variable_is_cleared_from_the_child(self):
        command = build("kimi")
        self.assertEqual(command.env_unset, ("KIMI_MODEL_THINKING_EFFORT",))
        child = job_agent.child_environment(
            9, command, {"KIMI_MODEL_THINKING_EFFORT": "max", "PATH": "/bin"})
        self.assertNotIn("KIMI_MODEL_THINKING_EFFORT", child)
        chosen = job_agent.child_environment(
            9, build("kimi", effort="low"), {"KIMI_MODEL_THINKING_EFFORT": "max"})
        self.assertEqual(chosen["KIMI_MODEL_THINKING_EFFORT"], "low")


class RefusalTest(unittest.TestCase):
    """AC 2: every refusal is raised by the builder, before a run is registered."""

    def refuse(self, fragment, *args, **kwargs):
        with self.assertRaises(job_agent.AgentRefusal) as caught:
            build(*args, **kwargs)
        self.assertIn(fragment, str(caught.exception))

    def test_model_is_required(self):
        for model in (None, "", "   "):
            with self.subTest(model=model):
                self.refuse("requires --model", "claude", model)

    def test_unsupported_harnesses_are_refused_by_name(self):
        self.refuse("harness 'vibe' has no headless adapter", "vibe")
        self.refuse("harness 'nosuch' is not shipped", "nosuch")
        self.refuse("requires --harness", "")
        self.refuse("harness 'codex' is disabled by SC_DISABLED_HARNESSES", "codex",
                    env={"SC_DISABLED_HARNESSES": "kimi, codex"})
        self.refuse("harness 'kimi' is not installed: `kimi` is not on PATH", "kimi",
                    which=lambda binary: None)
        self.refuse(
            "does not support one-shot", "codex",
            adapter={**run_mod.load_adapter("codex"),
                     "surfaces": {**run_mod.load_adapter("codex")["surfaces"],
                                  "one_shot": False}})

    def test_a_model_or_effort_the_harness_cannot_take_is_refused(self):
        adapter = run_mod.load_adapter("claude")
        headless = adapter["headless"]
        self.refuse(
            "harness 'claude' cannot apply requested model 'model-x'", "claude",
            adapter={**adapter, "headless": {k: v for k, v in headless.items() if k != "model_flag"}})
        self.refuse(
            "harness 'claude' cannot apply effort 'high'", "claude", "model-x", "high",
            adapter={**adapter, "headless": {k: v for k, v in headless.items() if k != "effort"}})
        self.refuse("effort must be a non-empty level", "claude", "model-x", " ")

    def test_model_effort_and_session_shape_is_checked_before_anything_starts(self):
        bad = ["-m", "--config=x", 'a"b', "a'b", "a`b", "a\\b", "a\nb", "a\tb", "a\x1fb",
               "a\x7fb", " lead", "trail ", "x" * 513]
        for value in bad:
            with self.subTest(model=value):
                self.refuse("model must", "claude", value)
        for value in bad[:-1] + ["x" * 65]:
            with self.subTest(effort=value):
                self.refuse("effort must", "codex", "model-x", value)
        # The case that reached a second config key inside -c before this check.
        self.refuse("effort must not contain quotes", "codex", "model-x",
                    'low"\nsandbox_mode="read-only')
        for value in ("-s", "--last", 'a"b', "a\nb", "", "  "):
            with self.subTest(session=value):
                self.refuse("no usable session reference", "claude", "haiku",
                            resume_session=value)
        for harness in ("Claude", "codex exec", "../codex", "co\ndex", "-x"):
            with self.subTest(harness=harness):
                self.refuse("harness must be a shipped harness name", harness)
        # Ordinary selectors still pass.
        for model in ("provider/model-x:1017", "kimi-code/k3.1-pro", "gpt-6-luna",
                      "openrouter/~vendor/name-latest", "claude-haiku-5-5[1m]"):
            with self.subTest(model=model):
                self.assertEqual(build("opencode", model, "x-high").requested_model, model)

    def test_an_adapter_without_the_agent_keys_is_refused_naming_the_key(self):
        adapter = run_mod.load_adapter("opencode")
        headless = adapter["headless"]
        self.refuse(
            "declares no `headless.agent` key", "opencode",
            adapter={**adapter, "headless": {k: v for k, v in headless.items() if k != "agent"}})
        for key in ("brief", "result", "session"):
            with self.subTest(key=key):
                agent = {k: v for k, v in headless["agent"].items() if k != key}
                self.refuse(
                    f"declares no `headless.agent.{key}` key", "opencode",
                    adapter={**adapter, "headless": {**headless, "agent": agent}})
        agent = {k: v for k, v in headless["agent"].items() if k != "resume"}
        without_resume = {**adapter, "headless": {**headless, "agent": agent}}
        self.assertEqual(build("opencode", adapter=without_resume).harness, "opencode")
        self.refuse("cannot continue a subagent", "opencode",
                    adapter=without_resume, resume_session="ses_1")

    def test_a_refusal_touches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(subprocess, "run") as spawn, \
                mock.patch.object(subprocess, "Popen") as popen:
            before = sorted(Path(tmp).rglob("*"))
            with self.assertRaises(job_agent.AgentRefusal):
                build("vibe")
            with self.assertRaises(job_agent.AgentRefusal):
                build("claude", None)
            self.assertEqual(sorted(Path(tmp).rglob("*")), before)
            spawn.assert_not_called()
            popen.assert_not_called()

    def test_brief_bounds(self):
        self.assertEqual(job_agent.validate_brief("x" * 65536), b"x" * 65536)
        for text, fragment in (("", "empty"), (" \n", "empty"),
                               ("x" * 65537, "65537 bytes; the limit is 65536"),
                               ("a\0b", "NUL")):
            with self.subTest(fragment=fragment), self.assertRaises(job_agent.AgentRefusal) as caught:
                job_agent.validate_brief(text)
            self.assertIn(fragment, str(caught.exception))

    def test_every_shipped_headless_harness_declares_the_agent_keys(self):
        for path in sorted((ENGINE / "adapters").glob("*/adapter.json")):
            adapter = json.loads(path.read_text())
            if not (adapter.get("headless") or {}).get("launch"):
                continue
            with self.subTest(harness=adapter["harness"]):
                agent = job_agent.agent_keys(adapter)
                self.assertEqual(set(agent), {"brief", "result", "session", "resume"})


class RecordedRunTest(unittest.TestCase):
    """AC 17, and the Claude half of AC 11's fixtures: the command, the final
    message and the session reference match what the installed CLI did."""

    def same_arguments(self, built, recorded, session):
        self.assertEqual(
            Counter(arg.replace(SESSION, session) for arg in built), Counter(recorded))

    def test_launch_matches_the_recorded_run(self):
        for harness in ("claude", "opencode"):
            record = HARNESSES[harness]
            with self.subTest(harness=harness):
                self.assertTrue(record["live"])
                command = build(harness, record["model"])
                launch = record["launch"]
                self.same_arguments(command.argv, launch["argv"], launch["session_ref"])
                self.assertEqual(
                    "stdin" if command.stdin else "argument", record["brief"])
                self.assertEqual(job_agent.read_result(command.result, launch["stdout"]), NONCE)
                self.assertEqual(
                    job_agent.read_session(
                        command.session, launch["stdout"], launch["session_ref"]),
                    launch["session_ref"])

    def test_kimi_launch_matches_the_recorded_stream(self):
        # The fresh Kimi run was recorded in text mode; its structured stream was
        # recorded on the resumed run. A launch is that command without the
        # session flag, and it is read the same way.
        record = HARNESSES["kimi"]
        resumed = record["resume"]
        command = build("kimi", record["model"])
        expected = [a for a in resumed["argv"] if a not in ("-S", resumed["session_ref"])]
        self.same_arguments(command.argv, expected, "")
        self.assertEqual(Counter(record["launch"]["argv"]) - Counter(command.argv), Counter())
        self.assertEqual(record["brief"], "argument")
        self.assertEqual(job_agent.read_result(command.result, resumed["stdout"]), NONCE)
        self.assertEqual(
            job_agent.read_session(command.session, resumed["stdout"]), resumed["session_ref"])
        self.assertEqual(resumed["session_ref"], record["launch"]["session_ref"])

    def test_kimi_accepts_a_brief_that_starts_with_dashes(self):
        # Verified on the installed CLI: the value of -p may start with dashes,
        # so the argument delivery needs no change. These were fresh launches in
        # the structured stream, which the builder's launch produces exactly.
        record = HARNESSES["kimi"]["dash_briefs"]
        command = build("kimi", HARNESSES["kimi"]["model"])
        self.assertEqual(command.argv, record["argv"])
        with tempfile.TemporaryDirectory() as tmp:
            for run in record["runs"]:
                with self.subTest(starts=run["brief_starts_with"]):
                    brief = run["brief_starts_with"] + "the rest of the brief"
                    (Path(tmp) / "brief.md").write_text(brief)
                    argv, stdin = command.materialize(Path(tmp))
                    self.assertEqual(argv[-2:], ["-p", brief])
                    self.assertIsNone(stdin)
                    self.assertEqual(run["exit_code"], 0)
                    self.assertEqual(job_agent.read_result(command.result, run["stdout"]), NONCE)
                    self.assertEqual(
                        job_agent.read_session(command.session, run["stdout"]),
                        run["session_ref"])

    def test_resume_matches_the_recorded_run_where_continue_works(self):
        for harness in ("claude", "kimi", "opencode"):
            record = HARNESSES[harness]
            with self.subTest(harness=harness):
                self.assertTrue(record["continue_works"])
                resumed = record["resume"]
                command = build(harness, record["model"], resume_session=resumed["session_ref"])
                self.same_arguments(command.argv, resumed["argv"], resumed["session_ref"])
                self.assertEqual(command.session_ref, resumed["session_ref"])
                self.assertEqual(command.resumes, resumed["session_ref"])
                self.assertEqual(job_agent.read_result(command.result, resumed["stdout"]), NONCE)
                self.assertEqual(
                    job_agent.read_session(
                        command.session, resumed["stdout"], command.session_ref),
                    resumed["session_ref"])

    def test_codex_launch_and_resume_match_the_recorded_runs(self):
        # The recordings were made outside a repository, so they carry one flag
        # a subagent's worktree never needs; everything else must be identical,
        # in order.
        record = HARNESSES["codex"]
        self.assertTrue(record["live"])
        self.assertTrue(record["continue_works"])
        scratch = set(record["scratch_only_flags"])
        for step, session in (("launch", None), ("resume", record["resume"]["session_ref"])):
            with self.subTest(step=step):
                run = record[step]
                command = build(
                    "codex", record["model"], record["effort"], resume_session=session)
                self.assertEqual(command.argv, [a for a in run["argv"] if a not in scratch])
                self.assertEqual(command.stdin, "{run_dir}/brief.md")
                self.assertEqual(
                    job_agent.read_session(command.session, run["stdout"]), run["session_ref"])
                with tempfile.TemporaryDirectory() as tmp:
                    self.assertIsNone(job_agent.read_result(command.result, "", Path(tmp)))
                    (Path(tmp) / "last-message.md").write_text(run["last_message_file"])
                    self.assertEqual(
                        job_agent.read_result(command.result, run["stdout"], Path(tmp)), NONCE)
        # The resumed run reports the session it continued.
        self.assertEqual(record["resume"]["session_ref"], record["launch"]["session_ref"])
        # `codex exec resume` has no --sandbox option; the policy travels as config.
        self.assertFalse(record["from_help"]["resume_has_sandbox_flag"])
        self.assertIn('sandbox_mode="danger-full-access"', record["resume"]["argv"])
        self.assertNotIn("--sandbox", record["resume"]["argv"])

    def test_a_run_without_a_final_message_has_none(self):
        for harness in ("claude", "kimi", "opencode"):
            command = build(harness)
            for stdout in ("", "not json\n", '{"type":"error"}\n', "[]"):
                with self.subTest(harness=harness, stdout=stdout):
                    self.assertIsNone(job_agent.read_result(command.result, stdout))
                    if command.session["source"] == "event":
                        self.assertIsNone(job_agent.read_session(command.session, stdout))

    def test_the_final_message_is_the_last_one_reported(self):
        lines = [
            {"type": "text", "sessionID": "ses_1", "part": {"text": "looking"}},
            {"type": "tool_use", "sessionID": "ses_1", "part": {"text": "ignored"}},
            {"type": "text", "sessionID": "ses_1", "part": {"text": "done"}},
        ]
        stdout = "noise\n" + "".join(json.dumps(line) + "\n" for line in lines)
        command = build("opencode")
        self.assertEqual(job_agent.read_result(command.result, stdout), "done")
        self.assertEqual(job_agent.read_session(command.session, stdout), "ses_1")


class WorktreeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.root / ".gitignore").write_text("CLAUDE.md\nAGENTS.md\n")
        (self.root / "tracked.txt").write_text("one\n")
        (self.root / "CLAUDE.md").write_text("the launching shell's boot file\n")
        self.git("add", ".gitignore", "tracked.txt")
        self.commit("first")
        self.base = self.git("rev-parse", "HEAD")
        (self.root / "tracked.txt").write_text("two\n")
        self.git("add", "tracked.txt")
        self.commit("second")

    def git(self, *args, cwd=None):
        done = subprocess.run(
            ["git", "-C", str(cwd or self.root), *args],
            capture_output=True, text=True, check=True)
        return done.stdout.strip()

    def commit(self, message):
        self.git("-c", "user.name=Test", "-c", "user.email=test@subfloor.local",
                 "commit", "-q", "-m", message)

    def test_detached_worktree_at_the_base_carries_no_boot_file(self):
        path = job_agent.create_worktree(7, self.base, root=self.root)
        self.assertEqual(path, self.root / ".sc-worktrees" / "agent-7")
        self.assertEqual(path, job_agent.worktree_path(7, self.root))
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=path), self.base)
        self.assertEqual(self.git("branch", "--show-current", cwd=path), "")
        self.assertEqual((path / "tracked.txt").read_text(), "one\n")
        self.assertFalse((path / "CLAUDE.md").exists())
        self.assertEqual(self.git("status", "--porcelain", cwd=path), "")
        with self.assertRaises(RuntimeError):
            job_agent.create_worktree(7, self.base, root=self.root)

    def test_named_branch_is_created_from_the_base(self):
        job_agent.validate_branch("feat/delegated", self.root)
        path = job_agent.create_worktree(8, self.base, "feat/delegated", root=self.root)
        self.assertEqual(self.git("branch", "--show-current", cwd=path), "feat/delegated")
        self.assertEqual(self.git("rev-parse", "feat/delegated"), self.base)
        for name, fragment in (("feat/delegated", "already exists"), ("main", "already exists"),
                               ("bad..name", "not a valid branch name"),
                               ("-b", "must be a branch name")):
            with self.subTest(name=name), self.assertRaises(job_agent.AgentRefusal) as caught:
                job_agent.validate_branch(name, self.root)
            self.assertIn(fragment, str(caught.exception))

    def test_base_resolves_to_a_full_commit_or_is_refused(self):
        self.assertEqual(job_agent.resolve_base("HEAD~1", self.root), self.base)
        self.assertEqual(job_agent.resolve_base("main", self.root), self.git("rev-parse", "main"))
        for ref in ("nosuch", "--all", ""):
            with self.subTest(ref=ref), self.assertRaises(job_agent.AgentRefusal):
                job_agent.resolve_base(ref, self.root)

    def test_a_failed_creation_names_the_reason_and_leaves_no_worktree(self):
        with self.assertRaises(RuntimeError) as caught:
            job_agent.create_worktree(9, "f" * 40, root=self.root)
        self.assertIn("could not create subagent worktree", str(caught.exception))
        self.assertFalse(job_agent.worktree_path(9, self.root).exists())


    def test_a_failed_creation_ends_the_registered_run_as_failed(self):
        leftover = job_agent.worktree_path(11, self.root)
        leftover.mkdir(parents=True)
        (leftover / "stale.txt").write_text("from an earlier run 11")
        submitted = []
        made = job_agent.create_worktree_or_fail(
            11, self.base, root=self.root, submit=submitted.append)
        self.assertIsNone(made)
        (payload,) = submitted
        self.assertEqual((payload["state"], payload["exit_code"]), ("failed", 127))
        self.assertIn("subagent not started", payload["spawn_error"])
        self.assertIn("already exists, left by an earlier run", payload["spawn_error"])
        self.assertLessEqual(len(payload["spawn_error"]), 256)
        self.assertEqual((leftover / "stale.txt").read_text(), "from an earlier run 11")
        # A bad base ends the run the same way; a good one submits nothing.
        self.assertIsNone(job_agent.create_worktree_or_fail(
            12, "f" * 40, root=self.root, submit=submitted.append))
        self.assertEqual(len(submitted), 2)
        self.assertEqual(
            job_agent.create_worktree_or_fail(
                13, self.base, root=self.root, submit=submitted.append),
            job_agent.worktree_path(13, self.root))
        self.assertEqual(len(submitted), 2)


class ReservedPrefixTest(unittest.TestCase):
    def test_a_shell_cannot_take_an_agent_worktree_name(self):
        for shortname in ("agent-7", "agent-12", " AGENT-12 "):
            with self.subTest(shortname=shortname), self.assertRaises(ValueError) as caught:
                shell_factory.create_shell(
                    mock.Mock(), flavor=None, name="Imposter", shortname=shortname)
            # Decision #428: the confusion it prevents comes first, in the
            # words subagent admission uses.
            self.assertTrue(str(caught.exception).startswith(
                f"refused — {runs.RESERVED_NAME_CONFUSION}: "))

    def test_only_the_run_number_form_is_reserved(self):
        # The refusal is for agent-<number> alone, the form a run's worktree
        # takes; other names that begin with agent- are a shell's to take.
        con = mock.Mock()
        con.execute.side_effect = RuntimeError("reached the insert")
        for shortname in ("agent-foo", "Agent-x", "agent-", "agent-12a", "agents-1"):
            with self.subTest(shortname=shortname), self.assertRaises(RuntimeError):
                shell_factory.create_shell(
                    con, flavor=None, name="Allowed", shortname=shortname)


class ChildEnvironmentTest(unittest.TestCase):
    SHELL = {
        "SC_API_TOKEN": "shell-token", "SC_API_BASE": "http://127.0.0.1:1",
        "SC_SHELL_ID": "9", "SC_SHELL_SHORTNAME": "PLN1", "SC_SHELL_FLAVOR": "planner",
        "SC_SHELL_WORKTREE": "/repo/.sc-worktrees/pln1", "SC_HARNESS": "claude",
        "SC_SEAT": "gui", "SC_DEVKIT_RUN_ID": "4",
        "GIT_AUTHOR_NAME": "Planning-01", "GIT_AUTHOR_EMAIL": "pln1@subfloor.local",
        "GIT_COMMITTER_NAME": "Planning-01", "GIT_COMMITTER_EMAIL": "pln1@subfloor.local",
        "PATH": "/usr/bin", "HOME": "/home/operator",
    }

    def test_identity_passes_through_and_only_the_two_mechanics_differ(self):
        child = job_agent.child_environment(31, None, self.SHELL)
        expected = {k: v for k, v in self.SHELL.items() if k not in ("SC_SEAT", "SC_DEVKIT_RUN_ID")}
        expected["SC_JOB_AGENT_RUN"] = "31"
        self.assertEqual(child, expected)
        self.assertEqual(self.SHELL["SC_SEAT"], "gui")  # the caller's mapping is untouched

    def test_the_run_id_is_what_a_nested_registration_carries(self):
        self.assertIsNone(job_agent.launched_from_run(self.SHELL))
        child = job_agent.child_environment(31, None, self.SHELL)
        self.assertEqual(job_agent.launched_from_run(child), "31")
        # A subagent's own subagent attempt would overwrite nothing: the marker stays.
        self.assertEqual(job_agent.child_environment(32, None, child)["SC_JOB_AGENT_RUN"], "32")

    def test_a_terminal_seat_shell_is_handled_the_same_way(self):
        shell = {**self.SHELL, "SC_SEAT": "tui"}
        self.assertNotIn("SC_SEAT", job_agent.child_environment(5, None, shell))


class RunMarkerScopeTest(unittest.TestCase):
    """The run id marks one subagent's process tree, not what is started from it."""

    def test_a_shell_session_launch_clears_the_marker_for_every_seat(self):
        import run_seat

        for seat in ("gui", "tui", None):
            with self.subTest(seat=seat):
                env = {"SC_JOB_AGENT_RUN": "31", "SC_SEAT": "gui", "SC_HARNESS": "codex"}
                run_seat.inject(env, seat)
                self.assertNotIn("SC_JOB_AGENT_RUN", env)
                self.assertIsNone(job_agent.launched_from_run(env))
                self.assertEqual(env.get("SC_SEAT"), seat)
                self.assertEqual(env["SC_HARNESS"], "codex")

    def test_both_shell_launch_paths_build_their_environment_through_inject(self):
        source = (ENGINE / "scripts" / "run.py").read_text()
        self.assertEqual(source.count("run_seat.inject(env, seat)"), 2)

    def test_the_api_server_drops_the_marker_at_start(self):
        import server

        class Stop(Exception):
            pass

        with mock.patch.dict(os.environ, {"SC_JOB_AGENT_RUN": "31"}), \
                mock.patch.object(server.log_lines, "install", side_effect=Stop):
            with self.assertRaises(Stop):
                server.main([])
            self.assertNotIn("SC_JOB_AGENT_RUN", os.environ)


def liveness_with_harness_in(root: Path, worktree: str) -> dict:
    """The real liveness scan over a /proc fixture holding one harness process."""
    with tempfile.TemporaryDirectory() as proc_dir:
        proc = Path(proc_dir)
        process = proc / "4242"
        process.mkdir()
        (process / "comm").write_text("codex\n")
        (process / "cwd").symlink_to(root / ".sc-worktrees" / worktree)
        with mock.patch.object(shell_liveness, "PROC", proc), \
                mock.patch.object(shell_liveness, "REPO_ROOT", root), \
                mock.patch.object(shell_liveness, "harness_binaries", return_value={"codex"}), \
                mock.patch.object(shell_liveness, "_shell_labels", return_value={}), \
                mock.patch.object(shell_liveness, "_launch_claims", return_value={}), \
                mock.patch.object(shell_liveness, "_browser_processes", return_value={}), \
                mock.patch.object(shell_liveness, "_self_harness_pid", return_value=None), \
                mock.patch.object(shell_liveness, "_start_ticks", return_value=1), \
                mock.patch.object(shell_liveness, "_orphan_verdict", return_value=None):
            return shell_liveness.compute()


def prepared(worktree: Path):
    return SimpleNamespace(
        cwd=str(worktree), archive_id=42, harness="codex", model="gpt-test",
        effort="high", env={"SC_API_TOKEN": "shell-token"}, boot_content="boot")


def test_a_subagent_does_not_occupy_its_launching_shell(launch_case):
    """AC 4: a harness in `agent-<id>` is not the shell's session; a send is accepted."""
    db_path, worktree = launch_case
    root = worktree.parents[1]
    (root / ".sc-worktrees" / "agent-7").mkdir()
    snapshot = liveness_with_harness_in(root, "agent-7")

    assert snapshot["worktree_sessions"] == {"agent-7": [4242]}
    assert not shell_liveness.is_active("dev", snapshot)
    assert shell_liveness.session_state("dev", snapshot) is None
    assert shell_liveness.cli_holders("dev", snapshot) == []
    assert [p["shortname"] for p in snapshot["processes"]] == ["agent-7"]
    # The Admin cleanup gate still sees a running subagent, as it must.
    assert not snapshot["safe_to_clean_all"]

    preparer = ConversationLaunchPreparer(
        db_path, prepare_launch=lambda **_kwargs: prepared(worktree),
        liveness=lambda: snapshot, liveness_retries=0)
    context, archive_id = preparer(make_run(worktree))
    assert archive_id == 42
    assert context.worktree == worktree


def test_the_same_harness_in_the_shells_own_worktree_is_refused(launch_case):
    """The control for AC 4: this is the collision the separate worktree avoids."""
    db_path, worktree = launch_case
    snapshot = liveness_with_harness_in(worktree.parents[1], "dev")

    assert shell_liveness.session_state("dev", snapshot) == "busy"
    preparer = ConversationLaunchPreparer(
        db_path, prepare_launch=lambda **_kwargs: prepared(worktree),
        liveness=lambda: snapshot, liveness_retries=0)
    with pytest.raises(ConversationLaunchError) as caught:
        preparer(make_run(worktree))
    assert caught.value.code == "SHELL_BUSY"


if __name__ == "__main__":
    unittest.main()
