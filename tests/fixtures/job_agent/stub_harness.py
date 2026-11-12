"""A stand-in harness CLI for the hermetic `sc job agent` tests.

Invoked as `stub_harness.py <harness-name> <harness argv...>` by a wrapper the
test puts on PATH under the harness's own binary name. It reads the brief the
way that harness does, records what it was started with, follows the
instructions in the brief, and reports in that harness's output shape.

Instructions are brief lines of the form `NAME:value`:
  RESULT:<text>   the final message          BIG:<n>      a final message of n bytes
  NORESULT:1      report no final message    NOSESSION:1  report no session reference
  SLEEP:<s>       wait before reporting      EXIT:<code>  exit status
  WRITE:<name>    leave an uncommitted file  COMMIT:<name> commit a new file
  SPAWN:1         leave a child in the process group
  DETACH:1        leave a child in its own process group and session
  STDERR:<text>   a line on standard error  SESSION:<ref> report this session reference
  REPLAY:1        print the output recorded for this harness in harness_controls.json
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

CONTROLS = Path(__file__).with_name("harness_controls.json")
SESSION = {"codex": "thr-stub-0001", "kimi": "session_stub-0001", "opencode": "ses_stub0001"}


def option(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


def main() -> int:
    name, argv = sys.argv[1], sys.argv[2:]
    if argv == ["--version"]:
        print(f"{name} 0.0.0-stub")
        return 0
    brief = option(argv, "-p") if name == "kimi" else sys.stdin.read()
    instructions = {}
    for line in (brief or "").splitlines():
        head, _, value = line.partition(":")
        if head.isupper() and value:
            instructions[head] = value
    record = {
        "harness": name, "argv": argv, "cwd": os.getcwd(), "brief": brief,
        "pid": os.getpid(), "pgid": os.getpgid(0),
        "worktree_listing": sorted(p.name for p in Path.cwd().iterdir()),
        "env": {key: os.environ.get(key) for key in (
            "SC_JOB_AGENT_RUN", "SC_SEAT", "SC_API_TOKEN", "KIMI_MODEL_THINKING_EFFORT")},
    }
    if "SPAWN" in instructions:
        record["spawned"] = subprocess.Popen(["sleep", "300"]).pid
    if "DETACH" in instructions:
        # As Codex runs its commands: outside the harness's group and session.
        record["detached"] = subprocess.Popen(["sleep", "300"], start_new_session=True).pid
    with open(os.environ["STUB_RECORD"], "a") as handle:
        handle.write(json.dumps(record) + "\n")
    if "STDERR" in instructions:
        print(instructions["STDERR"], file=sys.stderr)
    if "WRITE" in instructions:
        Path(instructions["WRITE"]).write_text("left by the subagent\n")
    if "COMMIT" in instructions:
        Path(instructions["COMMIT"]).write_text("committed by the subagent\n")
        subprocess.run(["git", "add", instructions["COMMIT"]], check=True)
        subprocess.run(
            ["git", "-c", "user.name=Stub", "-c", "user.email=stub@subfloor.local",
             "commit", "-q", "-m", "subagent work"], check=True)
    if "SLEEP" in instructions:
        time.sleep(float(instructions["SLEEP"]))

    if "REPLAY" in instructions:
        recorded = json.loads(CONTROLS.read_text())["harnesses"][name]
        resumed = bool({"--resume", "-S", "-s", "resume"} & set(argv))
        run = recorded["resume" if resumed else "launch"]
        if name == "kimi" and not resumed:
            run = recorded["dash_briefs"]["runs"][0]  # the stream-json launch
        sys.stdout.write(run["stdout"])
        if option(argv, "-o"):
            Path(option(argv, "-o")).write_text(run.get("last_message_file", ""))
        return 0

    text = instructions.get("RESULT", "stub result")
    if "BIG" in instructions:
        text = "r" * int(instructions["BIG"])
    if "NORESULT" in instructions:
        text = None
    session = None if "NOSESSION" in instructions else SESSION.get(name)
    session = instructions.get("SESSION", session)
    if name == "claude":
        print(json.dumps({"type": "result", "result": text} if text else {"type": "result"}))
    elif name == "codex":
        if session:
            print(json.dumps({"type": "thread.started", "thread_id": session}))
        if text and option(argv, "-o"):
            Path(option(argv, "-o")).write_text(text)
    elif name == "kimi":
        if text:
            print(json.dumps({"role": "assistant", "content": text}))
        if session:
            print(json.dumps(
                {"role": "meta", "type": "session.resume_hint", "session_id": session}))
    else:
        event = {"type": "step_start", **({"sessionID": session} if session else {})}
        print(json.dumps(event))
        if text:
            print(json.dumps({**event, "type": "text", "part": {"text": text}}))
    return int(instructions.get("EXIT", 0))


if __name__ == "__main__":
    sys.exit(main())
