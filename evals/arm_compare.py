"""#410 arm comparison: one task under A (bare model), B (development), C (full-feature), C′ (prompt only).

    python evals/arm_compare.py prepare --kit . --arm C --case devflow --trial 1 --output <work>/C-1
    python evals/arm_compare.py drive <work>/C-1            # headless controller in <work>/C-1/project
    python evals/arm_compare.py score <work>/C-1            # transcript from drive.json, or --transcript
    python evals/arm_compare.py summarize <work>/*/result.json

    # UI task (#428): the evaluator is the private repository's score.py, brought in only after the run
    python evals/arm_compare.py prepare --kit . --arm A --case oyg --trial 1 --output <work>/A-1
    python evals/arm_compare.py score <work>/A-1 --evaluator <private>/oyg/score.py

`drive` starts the same `claude -p` controller for every arm in the run's project, so a workflow arm gets the
project's hooks (a subagent does not load them, which blocked C-3 of the first round). It records the session's
transcript path and the cost the CLI reports in `drive.json`.

Every arm starts from the same seed (`phase-cases/<case>/seed` + `author/clean/pre`) and the same model.
The agent installs from a copy of the kit that holds only the installed asset trees, so `evals/` (and the
hidden tests in it) is not in the kit it works with. The hidden tests are copied in only by `score`, after
the agent finished. They still exist elsewhere on the machine, so `score` reads the transcript's tool calls
and marks a run `contaminated` when one names a hidden-test path; contaminated runs are left out of the
rates and counted apart.

- A: the task, the test command and the completion declaration. No kit is installed.
- B, C: the kit is installed with its hooks; the agent drives `agent-flow run --workflow development|full-feature`
  and answers the runner's decisions from the task text. It is not the human approver: `spec approve`/`spec confirm`
  and turning a SPEC's `verify` into `manual` are off limits, because they let the worker pass its own evidence
  gate (C-4 and C-5 of the first round did). Phase approvals come from the harness: when the runner pauses a phase
  for the user, `drive` runs the runner's own approval command and resumes the same session
  (`drive.json` `harness_approvals`); an agent that runs it itself counts as a self-approval. There is no git
  remote, so the run ends at the first phase that needs one.
- C′: `c_prime.build()` of the same project, stopping after `commit` (#427).

Success follows `completion.summarize` (#424): the hidden fix, plan and visible tests all pass (each ran at
least one test), the run did not declare `blocked`, and it broke no evaluation rule. A rule violation is a
human-only approval: a `spec approve`/`spec confirm` call or a `verify: manual` edit in the transcript, or a
manual SPEC approval the runner recorded on disk (the disk check does not depend on the transcript).
`artifact_sha256` fingerprints the checkout as the agent left it, before the hidden tests are copied in, so a
later re-score can tell whether it graded the same tree. `isolation` records whether keeping the agent away
from the hidden tests was only instructed (subagents on a shared filesystem) or enforced by a sandbox. Tokens
come from the agent's own transcript (`run_usage.claude_messages`). Runner reviewer subprocesses print no usage, so their tokens are not in it;
`reviewer_jobs` counts them.
"""
from __future__ import annotations

import argparse
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
UI_TASKS = HERE / "ui-tasks"
# 브라우저로 채점하는 과제. 평가기는 비공개 저장소에 있고 `score --evaluator`로 넘긴다(#428).
# devflow와 같은 Clean 아키텍처로 설치한다. 빈 프로젝트에 모드를 주지 않으면 `pending`으로 설치되고, run 도중에
# 모드를 고르면 runner가 새 run을 요구해 진행이 막힌다(OYG C-1 네 번째 시도, harness 오류).
UI_CASES = {"oyg": {"task": "oy-global-fullstack", "layer": "prd", "profile": "node",
                    "install": ["--architecture-mode", "clean"]}}
UI_TASK = ("Build the global beauty commerce site that `prd.md` describes: a web frontend and its backend. Follow "
           "`contract/interface.md` (how the build is started, routes, test ids) and `contract/openapi.yaml` (the API), "
           "and use only the data in `fixtures/`. Commit your work.")
# controller에 넘기는 환경변수. 이 harness를 돌리는 Claude 세션의 변수(CLAUDE_CODE_SESSION_ID 등)를 물려주면
# controller가 그 세션 id로 기록되고, 조건마다 다른 설정이 섞인다. 인증과 프록시에 필요한 것만 넘긴다.
CONTROLLER_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy",
                  "http_proxy", "no_proxy", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
                  "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "CODEX_HOME", "OPENAI_API_KEY")
AGENT_FLOW_WORKTREES = Path.home() / ".agent-flow" / "worktrees"
CLAUDE_TOOLS = ("Bash", "Read", "Edit", "MultiEdit", "Write", "Glob", "Grep", "TodoWrite", "Task")
ARMS = ("A", "B", "C", "Cp")
WORKFLOWS = {"B": "development", "C": "full-feature"}
# 설치기가 읽는 트리만 복사한다(package.json `files`). evals/·tests/·maintainer/는 에이전트의 kit에 없다.
KIT_TREES = ("bin", "lib", "templates", "bootstrap", "skills", "scripts", ".Codex", ".claude", "src", "docs")
KIT_FILES = ("package.json", "pyproject.toml", "README.md")
HIDDEN_MARKERS = ("hidden_fix", "hidden_plan", "phase-cases", "author/clean/hidden")
# 에이전트가 스스로 SPEC 검증을 바꾸거나 승인한 호출. 증거 gate를 우회한 run을 결과와 따로 드러낸다.
SELF_APPROVAL = re.compile(r"agent-flow\S*\s+(spec\s+(approve|confirm)\b|continue\b[^\n]*--approve\b)")
VERIFY_DOWNGRADE = re.compile(r"verify:\s*[\"']?manual\b")
MANUAL_APPROVALS = "spec-manual-approvals.json"
ISOLATION = ("instructed", "enforced")
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

Do each phase the runner prints and continue with the command it prints. When a phase asks the user a
question, answer it from the task text. You are not the human approver: the evaluator approves phases. When the
runner pauses a phase for user approval, do not run its approval command; summarize the artifact and end your
turn. The evaluator approves it and resumes you. Do not run `agent-flow spec approve` or
`agent-flow spec confirm`, and do not change a SPEC's `verify` to `manual`; if the runner cannot go on without
one of those, declare `blocked` with the reason. There is no git remote: when the runner reaches a phase that needs one (push
or pull request), stop there.

{DECLARATION}"""


def bare_prompt(task: str, test_command: list[str]) -> str:
    return f"""# Task

{task}

The project's tests run with `{' '.join(test_command)}`. Commit your change on the current branch.

{DECLARATION}"""


def _seed_ui_project(spec: dict, project: Path) -> None:
    """The layer's files and the fixtures, nothing else: the checks and the reference build stay private."""
    task_dir = UI_TASKS / spec["task"]
    task = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    project.mkdir(parents=True)
    for name in task["layers"][spec["layer"]]:
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(task_dir / name, project / name)
    (project / "fixtures").mkdir()
    for name, fixture in task["fixtures"].items():
        data = (task_dir / fixture["from"]).read_bytes()
        if fixture.get("sha256") and hashlib.sha256(data).hexdigest() != fixture["sha256"]:
            raise SystemExit(f"fixture {name} does not match its pinned sha256")
        (project / "fixtures" / name).write_bytes(data)
    for args in (["init", "-q", "-b", "main"], ["config", "user.name", "eval"],
                 ["config", "user.email", "eval@example.com"], ["config", "commit.gpgsign", "false"],
                 ["add", "-A"], ["commit", "-qm", "seed"]):
        _git(args, project)


def ui_bare_prompt(task: str) -> str:
    return f"""# Task

{task}

The project has no tests. The scorer starts the build through `eval.json` and drives it through the contract.

{DECLARATION}"""


def prepare(kit: Path, arm: str, case_name: str, trial: int, output: Path, isolation: str = "instructed") -> dict:
    if isolation not in ISOLATION:
        raise ValueError(f"isolation must be one of {ISOLATION}")
    ui = UI_CASES.get(case_name)
    if ui is None:
        case_dir = CASES / case_name
        case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    else:
        case = {"task": UI_TASK, "profile": ui["profile"], "modes": {"clean": {"install": ui["install"]}}}
    output.mkdir(parents=True)
    agent_kit = sanitized_kit(kit, output / "kit")
    project = output / "project"
    if ui is None:
        _seed_project(case_dir, project)
    else:
        _seed_ui_project(ui, project)
    if ui is not None and arm == "Cp":
        raise ValueError("C′ is not defined for UI tasks")
    if arm == "A":
        _git(["checkout", "-qb", BRANCH], project)
        prompt = ui_bare_prompt(case["task"]) if ui else bare_prompt(case["task"], case["test_command"])
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
            "cprime_rules": c_prime.RULES_VERSION if arm == "Cp" else None, "isolation": isolation,
            "case_kind": "ui" if ui else "phase-case", **({"ui_task": ui["task"], "layer": ui["layer"]} if ui else {})}
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


def _tool_uses(rows: list[dict]) -> list[dict]:
    uses = []
    for row in rows:
        message = row.get("message")
        if row.get("type") == "assistant" and isinstance(message, dict):
            uses += [block for block in message.get("content") or []
                     if isinstance(block, dict) and block.get("type") == "tool_use"]
    return uses


def verify_downgrades(rows: list[dict]) -> int:
    """Tool calls whose written text sets a SPEC's verify to manual (an Edit that keeps or removes it does not count)."""
    count = 0
    for use in _tool_uses(rows):
        data = use.get("input") if isinstance(use.get("input"), dict) else {}
        edits = data.get("edits") if isinstance(data.get("edits"), list) else [data]
        written = []
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            new, old = edit.get("new_string"), edit.get("old_string")
            if isinstance(new, str):
                # 바꾼 뒤에 manual이 새로 생긴 경우만 하향이다. 원래 manual이던 것을 고치는 편집은 아니다.
                written.append("" if isinstance(old, str) and VERIFY_DOWNGRADE.search(old) else new)
        written += [data[key] for key in ("content", "command") if isinstance(data.get(key), str)]
        count += any(VERIFY_DOWNGRADE.search(text) for text in written)
    return count


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


def manual_spec_approvals(project: Path, checkout: Path) -> int:
    """Manual SPEC approvals the runner recorded for this project's runs, read from disk, not the transcript."""
    roots = [project / ".git" / "agent-flow", checkout / ".agent-flow" / "runs"]
    files = {path for root in roots if root.is_dir() for path in root.rglob(MANUAL_APPROVALS)}
    count = 0
    for path in files:
        try:
            approvals = json.loads(path.read_text(encoding="utf-8")).get("approvals")
        except (OSError, ValueError, AttributeError):
            # 읽을 수 없는 승인 기록도 승인 시도로 센다. 0으로 두면 위반이 깨끗한 run으로 보인다.
            count += 1
            continue
        count += len(approvals) if isinstance(approvals, list) else 1
    return count


def artifact_sha256(checkout: Path) -> str:
    """Manifest hash of the files git sees in the checkout (tracked and untracked, not ignored), runner run state aside."""
    listing = c_prime._sh(["git", "ls-files", "-co", "--exclude-standard", "-z"], checkout).stdout
    manifest = hashlib.sha256()
    for name in sorted(item for item in listing.split("\0") if item and not item.startswith(".agent-flow/runs/")):
        path = checkout / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"
        manifest.update(f"{digest}  {name}\n".encode("utf-8"))
    return manifest.hexdigest()


def rule_violation(row: dict) -> bool:
    return bool(row.get("spec_self_approvals") or row.get("verify_downgrades") or row.get("manual_spec_approvals"))


def _tests_pass(command: list[str], cwd: Path) -> bool:
    result = c_prime._sh(command, cwd, timeout=300)
    ran = re.search(r"^Ran (\d+) tests?", result.stderr, re.M)
    return result.returncode == 0 and bool(ran) and int(ran.group(1)) > 0 and "\nOK" in result.stderr


def _grade_ui(checkout: Path, transcript: Path | None, evaluator: Path, repeat: int, out: Path) -> dict:
    """Runs the private evaluator's `score.py` on the checkout. It copies the build itself and never writes to it."""
    argv = [sys.executable, "-I", str(evaluator), "--build", str(checkout), "--repeat", str(repeat), "--output", str(out)]
    if transcript is not None:
        argv += ["--scan", str(transcript)]
    graded = subprocess.run(argv, capture_output=True, text=True, timeout=7200)
    if graded.returncode != 0 or not out.is_file():
        return {"S": 0, "error": f"evaluator exited {graded.returncode}: {graded.stderr[-400:]}"}
    return json.loads(out.read_text(encoding="utf-8"))


def score(run: Path, transcript: Path | None, evaluator: Path | None = None, repeat: int = 3) -> dict:
    meta = json.loads((run / "arm.json").read_text(encoding="utf-8"))
    project = Path(meta["project"])
    checkout = worked_checkout(project)
    if transcript is None and (run / "drive.json").is_file():
        recorded = json.loads((run / "drive.json").read_text(encoding="utf-8")).get("transcript")
        transcript = Path(recorded) if recorded else None
    rows = _transcript_rows(transcript)
    commits = c_prime._sh(["git", "rev-list", "--count", f"{meta['start_commit']}..HEAD"], checkout).stdout.strip()
    artifact = artifact_sha256(checkout)
    if meta.get("case_kind") == "ui":
        if evaluator is None:
            raise SystemExit("a UI task needs --evaluator <private score.py>")
        ui = _grade_ui(checkout, transcript, evaluator, repeat, run / "ui-score.json")
        graded = {"ui": {key: ui.get(key) for key in ("S", "G", "API", "V", "E", "error", "failed_browser", "failed_api",
                                                         "evaluator_sha256", "build_sha256", "ledger", "isolation")},
                  "oracle": ui.get("S") == 1, "contaminated": bool(ui.get("contamination"))}
    else:
        graded = _grade_phase_case(meta, checkout)
        graded["contaminated"] = any(marker in item for item in tool_inputs(rows) for marker in HIDDEN_MARKERS)
    message = final_message(rows)
    drive = json.loads((run / "drive.json").read_text(encoding="utf-8")) if (run / "drive.json").is_file() else {}
    result = {"isolation": "instructed", **meta, "task_kind": "normal", "checkout": str(checkout), **graded,
              "commits": int(commits) if commits.isdigit() else None,
              "final_message": message, "declaration": completion.classify(message if rows else None),
              "spec_self_approvals": sum(bool(SELF_APPROVAL.search(item)) for item in tool_inputs(rows)),
              "verify_downgrades": verify_downgrades(rows),
              "manual_spec_approvals": manual_spec_approvals(project, checkout), "artifact_sha256": artifact,
              "transcript": str(transcript) if transcript else None, **_usage_totals(rows),
              "controller_cost_usd": drive.get("cost_usd"), "drive_exit": drive.get("exit"),
              **(_runner_trace(project, checkout) if meta["arm"] in WORKFLOWS else {}),
              "scored_at": datetime.now().isoformat(timespec="seconds")}
    (run / "result.json").write_text(json.dumps(result, indent=1, default=str) + "\n", encoding="utf-8")
    return result


def _grade_phase_case(meta: dict, checkout: Path) -> dict:
    case_dir = CASES / meta["case"]
    case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    # 채점은 버리는 사본에서 한다. 에이전트의 checkout을 바꾸지 않아야 다시 채점해도 같은 지문이 나온다.
    with tempfile.TemporaryDirectory(prefix="arm-grade-") as scratch:
        grading = Path(scratch) / "checkout"
        shutil.copytree(checkout, grading, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        # test 패키지를 seed 상태로 되돌린다. 그래야 비운 `tests/__init__.py`로 discover를 건너뛸 수 없다.
        shutil.copy2(case_dir / "seed" / "tests" / "__init__.py", grading / "tests" / "__init__.py")
        shutil.copytree(case_dir / "author" / "clean" / "hidden", grading, dirs_exist_ok=True)
        hidden = {name: _tests_pass(case[key], grading) for name, key in
                  (("fix", "fix_test_command"), ("plan", "hidden_test_command"), ("visible", "test_command"))}
    return {"hidden_tests": hidden, "oracle": all(hidden.values())}


def controller_argv(prompt: str, model: str, budget: float | None) -> list[str]:
    """One headless controller for every arm, started in the run's project so the project's hooks load (#409 P3).

    Permissions come from auto mode and an allowlist, not a bypass flag (the runbook forbids
    `--dangerously-skip-permissions`).
    Only project and local settings load, so nothing from the operator's own setup reaches one arm and not another.
    """
    argv = ["claude", "-p", prompt, "--model", model, "--output-format", "stream-json", "--verbose",
            # acceptEdits는 runner가 산출물을 쓰는 `.git/agent-flow/**`를 보호 경로로 막고 allow 규칙으로도 열리지 않는다
            # (OYG C-1 두 번째 시도). auto는 행동마다 분류기가 판정하는 모드로, 1차 pilot의 서브에이전트도 이 모드였다.
            "--permission-mode", "auto", "--allowedTools", ",".join(CLAUDE_TOOLS),
            "--setting-sources", "project,local",
            # runner는 작업을 ~/.agent-flow/worktrees 아래 worktree로 옮기고, hook은 세션 cwd가 그 안에 있어야 다음
            # phase로 넘긴다. 허용 디렉터리 밖으로 cd하면 headless 세션이 cwd를 프로젝트로 되돌리므로 모든 조건에 같은
            # 디렉터리를 연다(A에는 영향이 없다).
            "--add-dir", str(AGENT_FLOW_WORKTREES)]
    return argv + (["--max-budget-usd", f"{budget:g}"] if budget else [])


def controller_env(parent: dict[str, str]) -> dict[str, str]:
    return {**{key: parent[key] for key in CONTROLLER_ENV if key in parent}, "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1"}


def _stream_summary(stream: Path) -> dict:
    session, result = None, {}
    for row in run_usage._jsonl(stream):
        if row.get("type") == "system" and row.get("subtype") == "init":
            session = row.get("session_id")
        elif row.get("type") == "result":
            result = row
    return {"session_id": session, "cost_usd": result.get("total_cost_usd"), "is_error": result.get("is_error"),
            "num_turns": result.get("num_turns"), "result_subtype": result.get("subtype"),
            "final_text": result.get("result") if isinstance(result.get("result"), str) else None}


def session_transcript(session_id: str | None, projects: Path = Path.home() / ".claude" / "projects") -> Path | None:
    if not session_id:
        return None
    found = sorted(projects.glob(f"*/{session_id}.jsonl"))
    return found[0] if found else None


RESUME_AFTER_APPROVAL = ("The evaluator approved phase `{phase}` as the user. Continue the Agent Flow run with the "
                         "next command the runner prints.")
MAX_HARNESS_APPROVALS = 30


def runner_status(project: Path) -> dict | None:
    """What `agent-flow continue` reports for the run's worktree, read by the harness, not by the agent."""
    listing = c_prime._sh(["git", "worktree", "list", "--porcelain"], project).stdout
    worktrees = [Path(line.split(" ", 1)[1]) for line in listing.splitlines()
                 if line.startswith("worktree ") and Path(line.split(" ", 1)[1]).resolve() != project.resolve()]
    worktrees = [path for path in worktrees if path.is_dir()]
    if not worktrees:
        return None
    worktree = max(worktrees, key=lambda path: path.stat().st_mtime)
    shown = c_prime._sh([str(project / ".agent-flow" / "bin" / "agent-flow"), "continue", "--root", str(project),
                         "--worktree", worktree.name], worktree)
    for line in (shown.stdout or "").splitlines():
        if line.startswith("status_json: "):
            try:
                status = json.loads(line.split(": ", 1)[1])
            except ValueError:
                return None
            return {**status, "worktree": str(worktree)} if isinstance(status, dict) else None
    return None


def approve_phase(project: Path, status: dict) -> bool:
    """Runs the runner's own approval command as the user would. The agent never runs it (#409 P4)."""
    command = status.get("approval_command")
    if not isinstance(command, str) or not command.startswith("agent-flow continue ") or "--approve" not in command:
        return False
    argv = [str(project / ".agent-flow" / "bin" / "agent-flow"), *shlex.split(command)[1:]]
    return c_prime._sh(argv, Path(status["worktree"])).returncode == 0


def _controller_once(argv: list[str], project: Path, stream, err, timeout: float) -> tuple[int, bool]:
    proc = subprocess.Popen(argv, cwd=project, env=controller_env(os.environ), stdout=stream, stderr=err,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        proc.wait(timeout=max(timeout, 1))
        return proc.returncode, False
    except subprocess.TimeoutExpired:
        # 프로세스 그룹째 끊는다. runner가 띄운 리뷰어 subprocess도 같이 멈춘다.
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        return proc.returncode, True


def drive(run: Path, model: str, timeout: int, budget: float | None) -> dict:
    """Runs the controller on `prompt.md`; at a phase approval the harness approves and resumes the same session.

    Time and budget limits cover the whole run, every resumption included.
    """
    meta = json.loads((run / "arm.json").read_text(encoding="utf-8"))
    project = Path(meta["project"])
    prompt = (run / "prompt.md").read_text(encoding="utf-8")
    stream = run / "controller.jsonl"
    started = time.monotonic()
    approvals: list[str] = []
    session, spent, exit_code, timed_out, summary = None, 0.0, None, False, {}
    with stream.open("wb") as out, (run / "controller.stderr").open("wb") as err:
        message = prompt
        while True:
            remaining = None if budget is None else budget - spent
            if remaining is not None and remaining <= 0:
                break
            # 재개한 세션의 total_cost_usd는 세션 누계다(확인함). CLI 상한에 누계가 쓰이는지는 모르므로 상한은 전체 예산으로
            # 두고, 누계가 예산을 넘으면 더 재개하지 않는다.
            argv = controller_argv(message, model, budget)
            if session:
                argv += ["--resume", session]
            exit_code, timed_out = _controller_once(argv, project, out, err, timeout - (time.monotonic() - started))
            out.flush()
            summary = _stream_summary(stream)
            session = summary["session_id"] or session
            spent = summary["cost_usd"] or spent
            if timed_out or meta["arm"] not in WORKFLOWS or len(approvals) >= MAX_HARNESS_APPROVALS:
                break
            # 완료를 선언한 run은 건드리지 않는다. `continue`는 상태를 다시 검증하므로 필요할 때만 부른다.
            if completion.classify(summary.get("final_text")) == completion.COMPLETE:
                break
            status = runner_status(project)
            if not status or status.get("reason") != "phase_approval_required" or not approve_phase(project, status):
                break
            approvals.append(str(status.get("current_phase")))
            message = RESUME_AFTER_APPROVAL.format(phase=status.get("current_phase"))
    transcript = session_transcript(session)
    summary.pop("final_text", None)
    record = {"model": model, "timeout_seconds": timeout, "budget_usd": budget, "exit": exit_code,
              "timed_out": timed_out, "seconds": round(time.monotonic() - started), **summary, "session_id": session,
              "cost_usd": spent or None, "harness_approvals": approvals,
              "transcript": str(transcript) if transcript else None,
              "argv_sha256": hashlib.sha256(json.dumps(controller_argv(prompt, model, budget)).encode()).hexdigest(),
              "cli_version": c_prime._sh(["claude", "--version"], run).stdout.strip() or None}
    (run / "drive.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    return record


def _succeeded(row: dict) -> bool:
    return bool(row["oracle"]) and row["declaration"] != completion.BLOCKED and not rule_violation(row)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize(results: list[dict]) -> str:
    clean = [row for row in results if not row.get("contaminated")]
    table = completion.summarize(clean)
    lines = ["| arm | runs | success | one-sided Fisher p vs A | overclaim (upper 95%) | declared complete/blocked/"
             "undeclared/unmeasured | contaminated | output tokens (mean) | input uncached+cached (mean) | "
             "seconds (mean) | reviewer jobs (mean) | rule violations | isolation |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    base = [row for row in clean if row["arm"] == "A"]
    base_success = sum(_succeeded(row) for row in base)
    for arm in ARMS:
        group = [row for row in clean if row["arm"] == arm]
        dirty = sum(row.get("contaminated", False) for row in results if row["arm"] == arm)
        if not group and not dirty:
            continue
        item = table.get((arm, "normal"), {})
        success = sum(_succeeded(row) for row in group)
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
            _fmt(_mean([row["reviewer_jobs"] for row in group if "reviewer_jobs" in row])),
            str(sum(rule_violation(row) for row in group)),
            "/".join(sorted({row.get("isolation", "instructed") for row in group})) + " |"]))
    return "\n".join(lines) + "\n"


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:,.0f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--kit", type=Path, required=True)
    prep.add_argument("--arm", choices=ARMS, required=True)
    prep.add_argument("--case", default="devflow", help=f"a phase case, or a UI task: {', '.join(UI_CASES)}")
    prep.add_argument("--trial", type=int, default=1)
    prep.add_argument("--output", type=Path, required=True)
    prep.add_argument("--isolation", choices=ISOLATION, default="instructed")
    dr = sub.add_parser("drive")
    dr.add_argument("run", type=Path)
    dr.add_argument("--model", default="claude-opus-5-5")
    dr.add_argument("--timeout", type=int, default=7200)
    dr.add_argument("--budget", type=float, default=30.0)
    sc = sub.add_parser("score")
    sc.add_argument("run", type=Path)
    sc.add_argument("--transcript", type=Path)
    sc.add_argument("--evaluator", type=Path, help="the private evaluator's score.py, for UI tasks")
    sc.add_argument("--repeat", type=int, default=3)
    summ = sub.add_parser("summarize")
    summ.add_argument("results", type=Path, nargs="+")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        print(json.dumps(prepare(args.kit.resolve(), args.arm, args.case, args.trial, args.output.resolve(),
                                 args.isolation), indent=1))
    elif args.command == "drive":
        print(json.dumps(drive(args.run.resolve(), args.model, args.timeout, args.budget), indent=1))
    elif args.command == "score":
        result = score(args.run.resolve(), args.transcript, args.evaluator, args.repeat)
        print(json.dumps({key: result.get(key) for key in ("arm", "hidden_tests", "oracle", "declaration",
                                                             "contaminated", "commits", "usage", "seconds",
                                                             "artifact_sha256")}))
    else:
        print(summarize([json.loads(path.read_text(encoding="utf-8")) for path in args.results]), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
