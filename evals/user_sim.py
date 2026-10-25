"""A simulated product owner who answers the agent's requirement questions from a hidden oracle spec.

`arm_compare.prepare` installs it for a case or layer that sets `user_sim: {"oracle": "<path in the case dir>"}`:
`install` copies this file and the oracle into `<run>/user-sim/` (outside the project) and writes an executable
`<run>/user-sim/ask-user`. Every arm's prompt then says it may run that script; the agent's question goes to

    python3 -I <run>/user-sim/user_sim.py ask <run>/user-sim/config.json "<question>"

which makes one separate `claude -p` call (the controller's model, no tools, no settings, no session, an allowlisted
environment, a temporary empty working directory, JSON output, `BUDGET_USD` and `TIMEOUT` per call). The oracle text
is in its system prompt, after fixed rules: answer only what is asked, concisely and consistently with the oracle;
answer exactly `UNDECIDED` where the oracle does not decide; volunteer nothing; never mention files or that a spec
exists. The answer is printed on stdout. Every call is one row (question, answer, cost) in `<run>/user-sim.jsonl`.

Limits: the oracle copy sits in the run directory beside the project, as the hidden checks of other cases sit
elsewhere on the machine; `arm_compare.score` marks a run contaminated when a tool call names it. `count_questions`
counts question marks, at least one per non-empty call. Only the standard library is used, because the script runs
outside this repository.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

VERSION = "user-sim-v1"
TIMEOUT = 300
BUDGET_USD = 1.0
LOG = "user-sim.jsonl"
DIR = "user-sim"
SCRIPT = "ask-user"
ORACLE = "oracle.md"
CONFIG = "config.json"
UNDECIDED = "정해진 바 없습니다. 합리적으로 정해 주세요."
FAILED = "The product owner could not answer this time. Decide it reasonably yourself."

SYSTEM_PROMPT = f"""You are the product owner of a software product. A developer building it asks you questions \
about what you want. What you have decided is written below between <decided> and </decided>; that is all you know.

Rules:
- Answer only what is asked, concisely, and consistently with what you have decided.
- When what you have decided does not settle a question, answer that question with exactly: {UNDECIDED}
- Never volunteer requirements, details or hints that were not asked about.
- Never mention files, file names, documents or that your decisions are written down anywhere; answer as a person \
who simply knows what they want.
- When several questions are asked, answer each in turn, in the same order and numbering.
"""


def system_prompt(oracle: str) -> str:
    return f"{SYSTEM_PROMPT}\n<decided>\n{oracle.strip()}\n</decided>\n"


def argv(system: str, model: str) -> list[str]:
    # 질문은 stdin으로 넘긴다. `-`로 시작하는 질문이 옵션으로 읽히지 않는다. 도구도 설정도 MCP도 세션도 없다.
    return ["claude", "-p", "--model", model, "--output-format", "json", "--system-prompt", system,
            "--tools", "", "--strict-mcp-config", "--setting-sources", "",
            "--settings", json.dumps({"disableAllHooks": True}), "--no-session-persistence",
            "--max-budget-usd", f"{BUDGET_USD:g}"]


def allowlisted_env(parent: dict[str, str], env: dict) -> dict[str, str]:
    """The controller's allowlist and fixed extras only; the controller's own session variables do not reach it."""
    return {**{key: parent[key] for key in env["keys"] if key in parent}, **env["set"]}


def count_questions(text: str) -> int:
    if not text.strip():
        return 0
    return max(1, len(re.findall(r"[?？]", text)))


def _call(command: list[str], stdin: str, cwd: Path, env: dict[str, str],
          timeout: int) -> tuple[int | None, str, str, bool]:
    """(exit code, stdout, stderr, timed out). The whole process group is stopped at the timeout."""
    try:
        proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
    except OSError as exc:
        return None, "", str(exc), False
    try:
        out, err = proc.communicate(stdin.encode("utf-8"), timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        timed_out = True
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), timed_out


def ask(question: str, config: dict, *, call=_call, environ: dict[str, str] | None = None) -> dict:
    """One answer, logged to `<run_dir>/user-sim.jsonl`. `call` is injectable for tests."""
    oracle = Path(config["oracle"]).read_text(encoding="utf-8")
    env = allowlisted_env(dict(os.environ if environ is None else environ), config["env"])
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="user-sim-") as scratch:
        code, stdout, stderr, timed_out = call(argv(system_prompt(oracle), config["model"]), question, Path(scratch),
                                               env, TIMEOUT)
    try:
        payload = json.loads(stdout)
    except ValueError:
        payload = None
    payload = payload if isinstance(payload, dict) else {}
    answer = payload.get("result") if isinstance(payload.get("result"), str) else None
    ok = not timed_out and code == 0 and not payload.get("is_error") and bool(answer and answer.strip())
    row = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "version": VERSION,
           "model": config["model"], "question": question, "questions": count_questions(question),
           "answer": answer.strip() if ok else None, "undecided": bool(ok and UNDECIDED in answer),
           "ok": ok, "exit": code, "timed_out": timed_out, "seconds": round(time.monotonic() - started, 1),
           "cost_usd": payload.get("total_cost_usd"), "usage": payload.get("usage"),
           "oracle_sha256": hashlib.sha256(oracle.encode("utf-8")).hexdigest(),
           "error": None if ok else (("timed out" if timed_out else f"exit {code}") + f": {stderr[-400:]}")}
    with (Path(config["run_dir"]) / LOG).open("a", encoding="utf-8") as out:
        out.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def read_log(log: Path) -> list[dict]:
    if not log.is_file():
        return []
    rows = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def summary(log: Path) -> dict:
    """What `score` records: calls, questions, whether the agent asked at all, and the simulator's cost."""
    rows = read_log(log)
    costs = [row.get("cost_usd") for row in rows]
    return {"ask_user_calls": len(rows), "ask_user_questions": sum(row.get("questions") or 0 for row in rows),
            "asked_any": bool(rows), "ask_user_failed": sum(not row.get("ok") for row in rows),
            "ask_user_undecided": sum(bool(row.get("undecided")) for row in rows),
            # 호출이 없으면 0이다. 비용을 보고하지 않은 호출이 있으면 0이나 부분합이 아니라 null이다.
            "user_sim_cost_usd": (round(sum(costs), 4) if None not in costs else None) if rows else 0.0}


def install(run_dir: Path, oracle: Path, env: dict, model: str) -> dict:
    """`<run>/user-sim/` with this script, the oracle copy, the config and the `ask-user` wrapper; the record."""
    target = run_dir / DIR
    target.mkdir()
    script = target / "user_sim.py"
    shutil.copy2(Path(__file__), script)
    oracle_copy = target / ORACLE
    shutil.copy2(oracle, oracle_copy)
    config = {"version": VERSION, "run_dir": str(run_dir.resolve()), "oracle": str(oracle_copy.resolve()),
              "model": model, "env": env}
    (target / CONFIG).write_text(json.dumps(config, indent=1) + "\n", encoding="utf-8")
    wrapper = target / SCRIPT
    wrapper.write_text(f"#!/bin/sh\nexec python3 -I {shlex.quote(str(script.resolve()))} ask "
                       f"{shlex.quote(str((target / CONFIG).resolve()))} \"$@\"\n", encoding="utf-8")
    wrapper.chmod(0o755)
    return {"version": VERSION, "ask_user": str(wrapper.resolve()),
            "oracle_sha256": hashlib.sha256(oracle_copy.read_bytes()).hexdigest(),
            "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
            "budget_usd": BUDGET_USD, "timeout_seconds": TIMEOUT, "undecided_answer": UNDECIDED}


def set_model(run_dir: Path, model: str) -> None:
    """The simulator runs on the controller's model, which only `drive` knows."""
    path = run_dir / DIR / CONFIG
    config = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**config, "model": model}, indent=1) + "\n", encoding="utf-8")


def main(args: list[str]) -> int:
    if len(args) < 2 or args[0] != "ask":
        print("usage: user_sim.py ask <config.json> <question...>", file=sys.stderr)
        return 2
    config = json.loads(Path(args[1]).read_text(encoding="utf-8"))
    question = " ".join(args[2:]).strip() or sys.stdin.read().strip()
    if not question:
        print("ask-user: give the question as an argument", file=sys.stderr)
        return 2
    row = ask(question, config)
    if not row["ok"]:
        print(FAILED)
        return 1
    print(row["answer"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
