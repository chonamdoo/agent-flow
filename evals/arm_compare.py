"""#410 arm comparison: one task under A (bare model), B (development), C (full-feature), C′ (prompt only).

    python evals/arm_compare.py prepare --kit . --arm C --case devflow --trial 1 --output <work>/C-1
    # an agent runs <work>/C-1/prompt.md with <work>/C-1/project as its working directory
    python evals/arm_compare.py score <work>/C-1 --transcript <the agent's transcript JSONL>
    python evals/arm_compare.py summarize <work>/*/result.json

Every arm starts from the same seed (`phase-cases/<case>/seed` + `author/clean/pre`) and the same model.
The agent installs from a copy of the kit that holds only the installed asset trees, so `evals/` (and the
hidden tests in it) is not in the kit it works with. The hidden tests are copied in only by `score`, after
the agent finished. They still exist elsewhere on the machine, so `score` reads the transcript's tool calls
and marks a run `contaminated` when one names a hidden-test path; contaminated runs are left out of the
rates and counted apart.

- A: the task, the test command and the completion declaration. No kit is installed.
- B, C: the kit is installed with its hooks; the agent drives `agent-flow run --workflow development|full-feature`
  and plays the user at pauses. There is no git remote, so the run ends at the first phase that needs one.
- C′: `c_prime.build()` of the same project, stopping after `commit` (#427).

Success follows `completion.summarize` (#424): the hidden fix, plan and visible tests all pass (each ran at
least one test) and the run did not declare `blocked`. Tokens come from the agent's own transcript
(`run_usage.claude_messages`). Runner reviewer subprocesses print no usage, so their tokens are not in it;
`reviewer_jobs` counts them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools" / "autoresearch"))
import c_prime  # noqa: E402
import completion  # noqa: E402
import run_usage  # noqa: E402
from cycle_report import fisher_greater  # noqa: E402

CASES = HERE / "phase-cases"
ARMS = ("A", "B", "C", "Cp")
WORKFLOWS = {"B": "development", "C": "full-feature"}
# 설치기가 읽는 트리만 복사한다(package.json `files`). evals/·tests/·maintainer/는 에이전트의 kit에 없다.
KIT_TREES = ("bin", "lib", "templates", "bootstrap", "skills", "scripts", ".Codex", ".claude", "src", "docs")
KIT_FILES = ("package.json", "pyproject.toml", "README.md")
HIDDEN_MARKERS = ("hidden_fix", "hidden_plan", "phase-cases", "author/clean/hidden")
# 에이전트가 스스로 SPEC 검증을 바꾸거나 승인한 호출. 증거 gate를 우회한 run을 결과와 따로 드러낸다.
SELF_APPROVAL = re.compile(r"agent-flow\S*\s+spec\s+(approve|confirm)\b")
BRANCH = "feat/arm-compare"
HANDBACK = "SubagentHandback"

DECLARATION = """## Completion declaration

End your final message with exactly one line:

    EVAL-STATUS: complete
    EVAL-STATUS: blocked: <reason>

Declare `complete` only when the change is done and works; otherwise declare `blocked` with the reason.
"""


def _git(args: list[str], cwd: Path) -> str:
    return c_prime._sh(["git", *args], cwd, check=True).stdout.strip()


def sanitized_kit(kit: Path, target: Path) -> Path:
    target.mkdir(parents=True)
    for name in KIT_TREES:
        if (kit / name).is_dir():
            shutil.copytree(kit / name, target / name, symlinks=True,
                            ignore=shutil.ignore_patterns("__pycache__", "node_modules"))
    for name in KIT_FILES:
        if (kit / name).is_file():
            shutil.copy2(kit / name, target / name)
    return target


def _seed_project(case_dir: Path, project: Path) -> None:
    shutil.copytree(case_dir / "seed", project)
    shutil.copytree(case_dir / "author" / "clean" / "pre", project, dirs_exist_ok=True)
    for args in (["init", "-q", "-b", "main"], ["config", "user.name", "eval"],
                 ["config", "user.email", "eval@example.com"], ["config", "commit.gpgsign", "false"],
                 ["add", "-A"], ["commit", "-qm", "seed"]):
        _git(args, project)


def _install(kit: Path, project: Path, case: dict, *, hooks: bool) -> None:
    argv = ["node", str(kit / "bin" / "agent-flow-kit.mjs"), "install", "--root", str(project),
            "--profile", case["profile"], *case["modes"]["clean"]["install"], *([] if hooks else ["--no-hooks"])]
    installed = c_prime._sh(argv, project)
    if installed.returncode != 0:
        raise SystemExit(f"install failed: {(installed.stderr or installed.stdout)[-400:]}")
    _git(["add", "-A"], project)
    _git(["commit", "-qm", "install", "--allow-empty"], project)


def driver_prompt(task: str, workflow: str) -> str:
    return f"""# Task

{task}

Use Agent Flow, installed in this project, to do this task with the `{workflow}` workflow. Start it with:

    .agent-flow/bin/agent-flow run {shlex.quote(task)} --workflow {workflow}

Do each phase the runner prints and continue with the command it prints. You are also the user: when a
phase asks the user to decide or approve, answer from the task text, approve, and run the printed approval
command. There is no git remote: when the runner reaches a phase that needs one (push or pull request),
stop there.

{DECLARATION}"""


def bare_prompt(task: str, test_command: list[str]) -> str:
    return f"""# Task

{task}

The project's tests run with `{' '.join(test_command)}`. Commit your change on the current branch.

{DECLARATION}"""


def prepare(kit: Path, arm: str, case_name: str, trial: int, output: Path) -> dict:
    case_dir = CASES / case_name
    case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    output.mkdir(parents=True)
    agent_kit = sanitized_kit(kit, output / "kit")
    project = output / "project"
    _seed_project(case_dir, project)
    if arm == "A":
        _git(["checkout", "-qb", BRANCH], project)
        prompt = bare_prompt(case["task"], case["test_command"])
    elif arm == "Cp":
        _install(agent_kit, project, case, hooks=False)
        _git(["checkout", "-qb", BRANCH], project)
        prompt = c_prime.build(agent_kit, project, case["task"], "commit")
    else:
        _install(agent_kit, project, case, hooks=True)
        prompt = driver_prompt(case["task"], WORKFLOWS[arm])
    (output / "prompt.md").write_text(prompt, encoding="utf-8")
    meta = {"arm": arm, "case": case_name, "trial": trial, "project": str(project.resolve()),
            "start_commit": _git(["rev-parse", "HEAD"], project),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "kit_commit": c_prime._sh(["git", "rev-parse", "HEAD"], kit).stdout.strip() or None,
            "cprime_rules": c_prime.RULES_VERSION if arm == "Cp" else None}
    (output / "arm.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    return meta


def worked_checkout(project: Path) -> Path:
    """The checkout the agent's work ended in: the newest linked worktree (B, C), else the project."""
    listing = c_prime._sh(["git", "worktree", "list", "--porcelain"], project).stdout
    linked = [Path(line.split(" ", 1)[1]) for line in listing.splitlines()
              if line.startswith("worktree ") and Path(line.split(" ", 1)[1]).resolve() != project.resolve()]
    linked = [path for path in linked if path.is_dir()]
    return max(linked, key=lambda path: path.stat().st_mtime) if linked else project


def _transcript_rows(path: Path | None) -> list[dict]:
    return run_usage._jsonl(path) if path is not None and path.is_file() else []


def final_message(rows: list[dict]) -> str | None:
    last = None
    for row in rows:
        message = row.get("message")
        if row.get("type") != "assistant" or not isinstance(message, dict):
            continue
        # 서브에이전트는 최종 보고를 `SubagentHandback` 도구 호출로 넘긴다. 그 message도 최종 메시지다.
        texts = [block.get("text") if block.get("type") == "text" else (block.get("input") or {}).get("message")
                 for block in message.get("content") or []
                 if isinstance(block, dict) and (block.get("type") == "text" or
                                                 (block.get("type") == "tool_use" and block.get("name") == HANDBACK))]
        texts = [text for text in texts if isinstance(text, str) and text]
        if texts:
            last = "\n".join(texts)
    return last


def tool_inputs(rows: list[dict]) -> list[str]:
    inputs = []
    for row in rows:
        message = row.get("message")
        if row.get("type") != "assistant" or not isinstance(message, dict):
            continue
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                inputs.append(json.dumps(block.get("input"), ensure_ascii=False))
    return inputs


def _usage_totals(rows: list[dict]) -> dict:
    messages = run_usage.claude_messages(rows)
    total = run_usage._empty()
    for message in messages:
        run_usage._add(total, message["usage"])
    if not messages:
        # 관측된 사용량이 없으면 0이 아니라 null이다. 0으로 두면 평균에서 공짜 run처럼 섞인다.
        total = {key: None for key in total}
    stamps = [stamp for stamp in (run_usage._timestamp(row.get("timestamp")) for row in rows) if stamp]
    return {"usage": total, "api_calls": len(messages),
            "models": sorted({message["model"] for message in messages if message["model"]}),
            "seconds": round((max(stamps) - min(stamps)).total_seconds()) if stamps else None}


def _runner_trace(project: Path, checkout: Path) -> dict:
    # `glob`의 `**`는 `.agent-flow` 같은 숨은 폴더를 건너뛴다. rglob은 건너뛰지 않는다.
    roots = [project / ".git" / "agent-flow", checkout / ".agent-flow" / "runs"]
    events = sorted({path for root in roots if root.is_dir() for path in root.rglob("events.jsonl")})
    entered: list[str] = []
    reviewer_jobs = 0
    for path in events:
        for row in run_usage._jsonl(path):
            details = row.get("details") if isinstance(row.get("details"), dict) else {}
            # 한 phase 진입이 연달아 두 번 기록되기도 한다. 이어진 중복은 하나로 센다.
            if (row.get("event") == "phase_entered" and isinstance(details.get("phase"), str)
                    and entered[-1:] != [details["phase"]]):
                entered.append(details["phase"])
        # 이벤트는 `<run>/context/events.jsonl`에, 리뷰 계측은 `<run>/review-metrics-*.json`에 있다.
        run_dir = path.parent.parent if path.parent.name == "context" else path.parent
        reviewer_jobs += run_usage.reviewer_usage(run_dir)["observed_jobs"]
    return {"phases_entered": entered, "reviewer_jobs": reviewer_jobs, "event_logs": len(events)}


def _tests_pass(command: list[str], cwd: Path) -> bool:
    result = c_prime._sh(command, cwd, timeout=300)
    ran = re.search(r"^Ran (\d+) tests?", result.stderr, re.M)
    return result.returncode == 0 and bool(ran) and int(ran.group(1)) > 0 and "\nOK" in result.stderr


def score(run: Path, transcript: Path | None) -> dict:
    meta = json.loads((run / "arm.json").read_text(encoding="utf-8"))
    case_dir = CASES / meta["case"]
    case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    project = Path(meta["project"])
    checkout = worked_checkout(project)
    rows = _transcript_rows(transcript)
    commits = c_prime._sh(["git", "rev-list", "--count", f"{meta['start_commit']}..HEAD"], checkout).stdout.strip()
    # 채점 전에 test 패키지를 seed 상태로 되돌린다. 그래야 비운 `tests/__init__.py`로 discover를 건너뛸 수 없다.
    shutil.copy2(case_dir / "seed" / "tests" / "__init__.py", checkout / "tests" / "__init__.py")
    shutil.copytree(case_dir / "author" / "clean" / "hidden", checkout, dirs_exist_ok=True)
    hidden = {name: _tests_pass(case[key], checkout)
              for name, key in (("fix", "fix_test_command"), ("plan", "hidden_test_command"), ("visible", "test_command"))}
    message = final_message(rows)
    result = {**meta, "task_kind": "normal", "checkout": str(checkout), "hidden_tests": hidden,
              "oracle": all(hidden.values()), "commits": int(commits) if commits.isdigit() else None,
              "final_message": message, "declaration": completion.classify(message if rows else None),
              "contaminated": any(marker in item for item in tool_inputs(rows) for marker in HIDDEN_MARKERS),
              "spec_self_approvals": sum(bool(SELF_APPROVAL.search(item)) for item in tool_inputs(rows)),
              "transcript": str(transcript) if transcript else None, **_usage_totals(rows),
              **(_runner_trace(project, checkout) if meta["arm"] in WORKFLOWS else {}),
              "scored_at": datetime.now().isoformat(timespec="seconds")}
    (run / "result.json").write_text(json.dumps(result, indent=1, default=str) + "\n", encoding="utf-8")
    return result


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize(results: list[dict]) -> str:
    clean = [row for row in results if not row.get("contaminated")]
    table = completion.summarize(clean)
    lines = ["| arm | runs | success | one-sided Fisher p vs A | overclaim (upper 95%) | declared complete/blocked/"
             "undeclared/unmeasured | contaminated | output tokens (mean) | input uncached+cached (mean) | "
             "seconds (mean) | reviewer jobs (mean) |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    base = [row for row in clean if row["arm"] == "A"]
    base_success = sum(row["oracle"] and row["declaration"] != completion.BLOCKED for row in base)
    for arm in ARMS:
        group = [row for row in clean if row["arm"] == arm]
        dirty = sum(row.get("contaminated", False) for row in results if row["arm"] == arm)
        if not group and not dirty:
            continue
        item = table.get((arm, "normal"), {})
        success = sum(row["oracle"] and row["declaration"] != completion.BLOCKED for row in group)
        p = (f"{fisher_greater(base_success, len(base), success, len(group)):.3f}"
             if arm != "A" and base and group else "-")
        states = item.get("states", {})
        usage = [row["usage"] for row in group]
        upper = item.get("overclaim_upper95")
        lines.append(" | ".join([
            f"| {arm}", str(len(group)), f"{success}/{len(group)}", p,
            f"{completion._cell(item.get('overclaim'))} ({completion._cell(upper)})",
            "/".join(str(states.get(state, 0)) for state in
                     (completion.COMPLETE, completion.BLOCKED, completion.UNDECLARED, completion.UNMEASURED)),
            str(dirty), _fmt(_mean([u["output"] for u in usage if u["output"] is not None])),
            _fmt(_mean([u["uncached_input"] + u["cached_input"] for u in usage
                        if u["uncached_input"] is not None and u["cached_input"] is not None])),
            _fmt(_mean([row["seconds"] for row in group if row.get("seconds") is not None])),
            _fmt(_mean([row["reviewer_jobs"] for row in group if "reviewer_jobs" in row])) + " |"]))
    return "\n".join(lines) + "\n"


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:,.0f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--kit", type=Path, required=True)
    prep.add_argument("--arm", choices=ARMS, required=True)
    prep.add_argument("--case", default="devflow")
    prep.add_argument("--trial", type=int, default=1)
    prep.add_argument("--output", type=Path, required=True)
    sc = sub.add_parser("score")
    sc.add_argument("run", type=Path)
    sc.add_argument("--transcript", type=Path)
    summ = sub.add_parser("summarize")
    summ.add_argument("results", type=Path, nargs="+")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        print(json.dumps(prepare(args.kit.resolve(), args.arm, args.case, args.trial, args.output.resolve()), indent=1))
    elif args.command == "score":
        result = score(args.run.resolve(), args.transcript)
        print(json.dumps({key: result.get(key) for key in ("arm", "hidden_tests", "oracle", "declaration",
                                                             "contaminated", "commits", "usage", "seconds")}))
    else:
        print(summarize([json.loads(path.read_text(encoding="utf-8")) for path in args.results]), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
