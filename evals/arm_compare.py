"""#410 arm comparison: one task under A (bare model), B (development), C (full-feature), D (default), C′ (prompt only).

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
- D: like B and C with the kit's `default` workflow, the one it applies to every project and which says the agent
  sets each phase's depth per task (#409 v2.1, arm D).
- C′: `c_prime.build()` of the same project, stopping after `commit` (#427).

Every workflow arm's result carries `phase_trace` (#409 v2.1): the phases the workflow declares, the ones the runner
entered and the ones it never entered, and per phase the wall-clock seconds and the controller's tokens and list-price
cost spent inside it. Spend is also split by phase category (plan, build, verify, deliver) plus `outside` (controller
time outside any phase window: reading the prompt, starting the runner, ending the turn for an approval);
`orchestration_share` is deliver + outside over the total. Runner reviewer subprocesses print no usage, so their
tokens are in neither; `reviewer_jobs` still counts them.

Success follows `completion.summarize` (#424): the hidden fix, plan and visible tests all pass (each ran at
least one test), the run did not declare `blocked`, and it broke no evaluation rule. A rule violation is a
human-only approval: a `spec approve`/`spec confirm` call or a `verify: manual` edit in the transcript, or a
manual SPEC approval the runner recorded on disk (the disk check does not depend on the transcript).
`artifact_sha256` fingerprints the checkout as the agent left it, before the hidden tests are copied in, so a
later re-score can tell whether it graded the same tree. `isolation` records whether keeping the agent away
from the hidden tests was only instructed (subagents on a shared filesystem) or enforced by a sandbox. Tokens
come from the agent's own transcript (`run_usage.claude_messages`) and its sub-agent transcripts
(`<session>/subagents/agent-*.jsonl`), which every transcript scan reads too; `transcript_cost_estimate_usd` prices
them at list price next to the CLI's own `controller_cost_usd`, so a gap shows usage the transcripts missed. Runner reviewer subprocesses print no usage, so their tokens are not in it;
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
# ANDROID_HOME·JAVA_HOME은 Android 과제(mobile-cart)의 Gradle 빌드에 필요하다.
CONTROLLER_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ", "ANDROID_HOME", "JAVA_HOME", "HTTPS_PROXY",
                  "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy", "NODE_EXTRA_CA_CERTS",
                  "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
                  "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "CODEX_HOME", "OPENAI_API_KEY")
AGENT_FLOW_WORKTREES = Path.home() / ".agent-flow" / "worktrees"
CLAUDE_TOOLS = ("Bash", "Read", "Edit", "MultiEdit", "Write", "Glob", "Grep", "TodoWrite", "Task")
ARMS = ("A", "B", "C", "D", "Cp")
WORKFLOWS = {"B": "development", "C": "full-feature", "D": "default"}
# 설치기가 읽는 트리만 복사한다(package.json `files`). evals/·tests/·maintainer/는 에이전트의 kit에 없다.
KIT_TREES = ("bin", "lib", "templates", "bootstrap", "skills", "scripts", ".Codex", ".claude", "src", "docs")
KIT_FILES = ("package.json", "pyproject.toml", "README.md")
HIDDEN_MARKERS = ("hidden_fix", "hidden_plan", "phase-cases", "author/clean/hidden")
# 에이전트가 스스로 SPEC 검증을 바꾸거나 승인한 호출. 증거 gate를 우회한 run을 결과와 따로 드러낸다.
SPEC_SELF_APPROVAL = re.compile(r"agent-flow\S*\s+spec\s+(approve|confirm)\b")
PHASE_SELF_APPROVAL = re.compile(r"agent-flow\S*\s+continue\b[^\n]*--approve\b")
SELF_APPROVAL = re.compile(f"{SPEC_SELF_APPROVAL.pattern}|{PHASE_SELF_APPROVAL.pattern}")
# 그 run이 받은 프롬프트의 승인 규칙. 1차(#447) 프롬프트는 에이전트에게 phase 승인을 맡겼다.
HARNESS_APPROVES = "harness-approves-phases"
AGENT_APPROVES = "agent-approves-phases"
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


def load_external_case(case_dir: Path) -> dict:
    """A case kept outside this repository (a holdout in the private evaluator repo): `<dir>/case.json`.

    It names a seed repository at a pinned commit, the task files to place in the project, the task text and the
    evaluator protocol. Only its name and sha256 go into the run record, never its path.
    """
    raw = (case_dir / "case.json").read_bytes()
    case = json.loads(raw)
    for key in ("name", "seed", "files", "task", "profile", "install", "score_keys"):
        if key not in case:
            raise SystemExit(f"external case is missing `{key}`")
    return {**case, "sha256": hashlib.sha256(raw).hexdigest()}


def _seed_external_project(case: dict, case_dir: Path, project: Path) -> None:
    """The seed repository at its pinned commit with no remote, then the task files in one commit.

    The clone's reflog names where it came from, and a seed inside the private checkout would tell the agent where the
    checks live, so the reflog is emptied along with the remote.
    """
    seed = case["seed"]
    c_prime._sh(["git", "clone", "-q", "--no-checkout", seed["repository"], str(project)], project.parent, check=True)
    for args in (["checkout", "-q", "-B", "main", seed["commit"]], ["remote", "remove", "origin"],
                 ["config", "user.name", "eval"], ["config", "user.email", "eval@example.com"],
                 ["config", "commit.gpgsign", "false"]):
        _git(args, project)
    # A later version of a task can start from an earlier version's reference build (`git format-patch` output).
    for patch in seed.get("patches", []):
        _git(["am", "-q", str((case_dir / patch).resolve())], project)
    _git(["reflog", "expire", "--expire=all", "--all"], project)
    for target, source in case["files"].items():
        (project / target).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(case_dir / source, project / target)
    _git(["add", "-A", *case["files"]], project)
    _git(["commit", "-qm", "task"], project)


def external_bare_prompt(case: dict) -> str:
    return f"""# Task

{case["task"]}

{case.get("bare_note", "")}

{DECLARATION}"""


def ui_bare_prompt(task: str) -> str:
    return f"""# Task

{task}

The project has no tests. The scorer starts the build through `eval.json` and drives it through the contract.

{DECLARATION}"""


def prepare(kit: Path, arm: str, case_name: str, trial: int, output: Path, isolation: str = "instructed",
            case_dir: Path | None = None) -> dict:
    if isolation not in ISOLATION:
        raise ValueError(f"isolation must be one of {ISOLATION}")
    if case_dir is not None:
        return _prepare_external(kit, arm, load_external_case(case_dir), case_dir, trial, output, isolation)
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
            "approval_rules": HARNESS_APPROVES if arm in WORKFLOWS else None,
            "case_kind": "ui" if ui else "phase-case", **({"ui_task": ui["task"], "layer": ui["layer"]} if ui else {})}
    (output / "arm.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    return meta


def _prepare_external(kit: Path, arm: str, case: dict, case_dir: Path, trial: int, output: Path,
                      isolation: str) -> dict:
    if arm not in ("A", *WORKFLOWS):
        raise ValueError("an external case defines the A, B, C and D arms only")
    output.mkdir(parents=True)
    agent_kit = sanitized_kit(kit, output / "kit")
    project = output / "project"
    _seed_external_project(case, case_dir, project)
    if arm == "A":
        _git(["checkout", "-qb", BRANCH], project)
        prompt = external_bare_prompt(case)
    else:
        _install(agent_kit, project, {"profile": case["profile"], "modes": {"clean": {"install": case["install"]}}},
                 hooks=True)
        prompt = driver_prompt(case["task"], WORKFLOWS[arm])
    (output / "prompt.md").write_text(prompt, encoding="utf-8")
    meta = {"arm": arm, "case": case["name"], "trial": trial, "project": str(project.resolve()),
            "start_commit": _git(["rev-parse", "HEAD"], project),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "kit_commit": c_prime._sh(["git", "rev-parse", "HEAD"], kit).stdout.strip() or None,
            "cprime_rules": None, "isolation": isolation,
            "approval_rules": HARNESS_APPROVES if arm in WORKFLOWS else None, "case_kind": "external",
            "case_sha256": case["sha256"], "seed_commit": case["seed"]["commit"], "score_keys": case["score_keys"],
            "evaluator_repeat": bool(case.get("evaluator_repeat"))}
    (output / "arm.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    return meta


def _common_dir(path: Path) -> Path | None:
    out = c_prime._sh(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], path).stdout.strip()
    return Path(out).resolve() if out else None


def linked_worktrees(project: Path) -> list[Path]:
    """Linked worktrees git lists for the project (`--porcelain -z`, so no path is split on a newline).

    A listed path counts only while it still belongs to this repository. A stale registration can name a path that
    another project's worktree now occupies (a project moved aside and re-prepared at the same path makes the runner
    create its worktree there again), and grading it would credit that project's work to this run.
    """
    listing = c_prime._sh(["git", "worktree", "list", "--porcelain", "-z"], project).stdout
    paths = [Path(field.split(" ", 1)[1]) for field in listing.split("\0") if field.startswith("worktree ")]
    common = _common_dir(project)
    return [path for path in paths if path.resolve() != project.resolve() and path.is_dir()
            and _common_dir(path) == common]


def runner_worktrees(project: Path) -> list[Path]:
    """Linked worktrees the runner owns: they have run state in `<git-common-dir>/agent-flow/worktrees/<name>/`."""
    state = _common_dir(project)
    return [path for path in linked_worktrees(project)
            if state is not None and (state / "agent-flow" / "worktrees" / path.name / ".agent-flow" / "runs").is_dir()]


def select_checkout(project: Path, arm: str) -> tuple[Path, dict]:
    """Which checkout to grade, chosen from run state, never from modification times.

    A workflow arm's work is in the worktree its runner created; an agent can add worktrees of its own
    (`git worktree add` came up in the C′ smoke, #444), and the newest one need not be the one it worked in. The
    other arms work in the project itself. When a workflow arm has linked worktrees but not exactly one with run
    state, the choice is not decidable and the run is a harness error.
    """
    linked = linked_worktrees(project)
    info = {"linked_worktrees": len(linked), "checkout_selected_by": None, "checkout_error": None}
    if arm not in WORKFLOWS:
        return project, {**info, "checkout_selected_by": "project"}
    owned = runner_worktrees(project)
    if len(owned) == 1:
        return owned[0], {**info, "checkout_selected_by": "runner-state"}
    if not linked:
        return project, {**info, "checkout_selected_by": "project"}
    return project, {**info, "checkout_error": f"{len(owned)} runner worktrees among {len(linked)} linked worktrees"}


def _transcript_rows(path: Path | None) -> list[dict]:
    return run_usage._jsonl(path) if path is not None and path.is_file() else []


def session_rows(path: Path | None) -> tuple[list[dict], list[dict], dict]:
    """(main transcript rows, main + sub-agent rows, what was read).

    The final message comes from the main transcript only; every scan (contamination, self-approval, verify
    downgrades) and the token totals use the sub-agent transcripts too (`run_usage.subagent_transcripts`).
    """
    main = _transcript_rows(path)
    versions = sorted({row["version"] for row in main if isinstance(row.get("version"), str)})
    if path is None or not path.is_file():
        return main, main, {"subagent_files": 0, "subagents": "none", "cli_versions": versions}
    files, state = run_usage.subagent_transcripts(path)
    rows = list(main)
    for item in files:
        rows += run_usage._jsonl(item)
    return main, rows, {"subagent_files": len(files), "subagents": "unmeasured" if state == "unreadable" else state,
                        "cli_versions": versions}


# USD per million tokens, list prices. Source: the claude-api skill bundled with Claude Code 2.1.308 (model table
# cached 2026-10-06; cache writes 1.25x input for the 5-minute TTL and 2x for the 1-hour TTL, reads 0.05x on Opus 5.5).
PRICES = {"claude-opus-5-5": {"fresh_input": 4.0, "cache_write_5m": 5.0, "cache_write_1h": 8.0, "cache_read": 0.20,
                              "output": 20.0}}


def message_cost(model: str | None, parts: dict) -> float | None:
    """List-price cost of one response, or None when the model is unpriced or a part is missing."""
    price = PRICES.get(model or "")
    if price is None or any(parts.get(key) is None for key in ("fresh_input", "cache_read", "output", "cache_write")):
        return None
    writes = {"cache_write_5m": parts.get("cache_write_5m"), "cache_write_1h": parts.get("cache_write_1h")}
    if any(value is None for value in writes.values()):
        if parts["cache_write"]:
            return None
        writes = {key: 0 for key in writes}
    tokens = {"fresh_input": parts["fresh_input"], "cache_read": parts["cache_read"], "output": parts["output"], **writes}
    return sum(tokens[key] * price[key] for key in tokens) / 1_000_000


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
    parts = run_usage.empty_components()
    costs = []
    for message in messages:
        run_usage._add(total, message["usage"])
        run_usage.add_components(parts, message["components"])
        costs.append(message_cost(message["model"], message["components"]))
    if not messages:
        # 관측된 사용량이 없으면 0이 아니라 null이다. 0으로 두면 평균에서 공짜 run처럼 섞인다.
        total = {key: None for key in total}
        parts = {key: None for key in parts}
    estimate = round(sum(costs), 4) if costs and None not in costs else None
    stamps = [stamp for stamp in (run_usage._timestamp(row.get("timestamp")) for row in rows) if stamp]
    return {"usage": total, "usage_components": parts, "transcript_cost_estimate_usd": estimate,
            "api_calls": len(messages),
            "models": sorted({message["model"] for message in messages if message["model"]}),
            "seconds": round((max(stamps) - min(stamps)).total_seconds()) if stamps else None}


def _measured_usage(rows: list[dict], read: dict) -> dict:
    totals = _usage_totals(rows)
    if read.get("subagents") == "unmeasured":
        # 읽지 못한 sub-agent 기록이 있으면 합계가 작게 나온다. 0이나 부분합 대신 null로 둔다(#411).
        totals.update({"usage": {key: None for key in totals["usage"]},
                       "usage_components": {key: None for key in totals["usage_components"]},
                       "transcript_cost_estimate_usd": None})
    return totals


def gate_record(project: Path, checkout: Path) -> dict:
    """What the runner's `gates` phase ran locally and what it only deferred to CI.

    A profile gate with `execution: ci` (python's required `test`) is written to `deferred_ci_checks` and not run;
    the harness has no remote, so a deferred gate is never run at all. Read from each run's canonical
    `artifacts/gate-results.json`; the last one written is the run's final gate ledger.
    """
    roots = [project / ".git" / "agent-flow", checkout / ".agent-flow" / "runs"]
    ledgers = sorted({path for root in roots if root.is_dir() for path in root.rglob("artifacts/gate-results.json")},
                     key=lambda path: path.stat().st_mtime_ns)
    if not ledgers:
        return {"gate_ledgers": 0, "local_gates": None, "deferred_ci_checks": None, "required_local_gates_ran": None}
    try:
        payload = json.loads(ledgers[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"gate_ledgers": len(ledgers), "local_gates": None, "deferred_ci_checks": None,
                "required_local_gates_ran": None}
    results = [item for item in payload.get("results") or [] if isinstance(item, dict)]
    return {"gate_ledgers": len(ledgers),
            "local_gates": [[item.get("gate_id"), bool(item.get("required")), bool(item.get("passed"))]
                            for item in results],
            "deferred_ci_checks": list(payload.get("deferred_ci_checks") or []),
            "required_local_gates_ran": sum(bool(item.get("required")) for item in results)}


def _runner_trace(project: Path, checkout: Path) -> dict:
    # `glob`의 `**`는 `.agent-flow` 같은 숨은 폴더를 건너뛴다. rglob은 건너뛰지 않는다.
    roots = [project / ".git" / "agent-flow", checkout / ".agent-flow" / "runs"]
    events = sorted({path for root in roots if root.is_dir() for path in root.rglob("events.jsonl")})
    entered: list[str] = []
    reviewer_jobs = 0
    cap_stop = False
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
        cap_stop = cap_stop or stopped_at_fix_loop_cap(run_dir / "meta.json")
    return {"phases_entered": entered, "reviewer_jobs": reviewer_jobs, "event_logs": len(events),
            "fix_loop_cap_stop": cap_stop}


# runner.py `FIX_LOOP_MAX_ROUNDS`. harness는 kit 코드를 import하지 않으므로 값을 옮겨 적는다.
RUNNER_FIX_LOOP_MAX_ROUNDS = 3


def stopped_at_fix_loop_cap(meta_path: Path) -> bool:
    """The run's last state is a blocked route with a fix collector at its round limit (read from runner state).

    This separates "stopped at the limit" from "the code was wrong" in the results, for runs recorded before the
    harness logged its own detection too (cycle 1 C-1, C-2).
    """
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    rounds = meta.get("fix_loop_rounds")
    counts = rounds.values() if isinstance(rounds, dict) else [rounds] if isinstance(rounds, int) else []
    return (meta.get("phase_blocked_reason") == "route_blocked"
            and any(isinstance(count, int) and count >= RUNNER_FIX_LOOP_MAX_ROUNDS for count in counts))


# Phase categories for the spend split (#409 v2.1). `deliver` is the bookkeeping around the work: worktree, run start,
# commit, push and PR steps, merge, cleanup and handoff. A phase id not listed here is reported as `unknown`.
PHASE_CATEGORY = {
    "plan": ("design", "explore", "reproduce", "domain-grill", "product-brief", "prd", "slice-plan", "plan-review",
             "ddd-design"),
    "build": ("implement", "implement-fix", "red", "green", "refactor", "comment-authoring"),
    "verify": ("review", "qa", "final-review", "multi-review", "architecture-review", "gates", "fix-loop"),
    "deliver": ("worktree", "run-start", "commit", "push-pr", "pr-watch", "pr-comment-fix", "pr-ci-fix",
                "merge-approval", "merge", "cleanup", "handoff"),
}
CATEGORY_OF = {phase: category for category, phases in PHASE_CATEGORY.items() for phase in phases}
SPEND_KEYS = (*PHASE_CATEGORY, "unknown", "outside")


def declared_phases(project: Path, checkout: Path, workflow: str) -> list[str] | None:
    """Phase ids of the workflow the run used: a project-local override first, as the runner resolves it."""
    for root in (checkout, project):
        for path in (root / ".agent-flow" / "workflows" / f"{workflow}.yaml",
                     root / ".agent-flow" / "runtime" / "python" / "agent_flow" / "workflows" / f"{workflow}.yaml"):
            if path.is_file():
                return re.findall(r"^  - id:\s*([\w-]+)", path.read_text(encoding="utf-8"), re.M) or None
    return None


def phase_windows(project: Path, checkout: Path) -> list[dict]:
    """[{phase, start, end}] in time order from the runner's events. Entering a phase closes the open one."""
    roots = [project / ".git" / "agent-flow", checkout / ".agent-flow" / "runs"]
    events = []
    for path in sorted({path for root in roots if root.is_dir() for path in root.rglob("events.jsonl")}):
        for row in run_usage._jsonl(path):
            details = row.get("details") if isinstance(row.get("details"), dict) else {}
            at = run_usage._timestamp(row.get("ts"))
            # A blocked transition still writes `phase_exited` (blocked: true) but the run stays in the phase, so
            # the work repairing the blocker belongs to it (as in `run_usage.phase_timeline`).
            if row.get("event") == "phase_exited" and details.get("blocked"):
                continue
            if at and isinstance(details.get("phase"), str) and row.get("event") in ("phase_entered", "phase_exited"):
                events.append((at, row["event"], details["phase"]))
    windows: list[dict] = []
    for at, event, phase in sorted(events, key=lambda item: item[0]):
        current = windows[-1] if windows and windows[-1]["end"] is None else None
        if event == "phase_entered":
            if current and current["phase"] == phase:
                continue  # the runner sometimes records one entry twice in a row
            if current:
                current["end"] = at
            windows.append({"phase": phase, "start": at, "end": None})
        elif current and current["phase"] == phase:
            current["end"] = at
    return windows


def phase_trace(project: Path, checkout: Path, workflow: str, rows: list[dict], measured: bool = True) -> dict:
    """`measured` is False when a sub-agent transcript could not be read: costs and the share are then null, as the
    run's own totals are (`_measured_usage`), instead of a partial sum presented as measured."""
    windows = phase_windows(project, checkout)
    messages = run_usage.claude_messages(rows)
    last = max([message["at"] for message in messages] + [window["start"] for window in windows], default=None)
    phases: dict[str, dict] = {}
    for window in windows:
        item = phases.setdefault(window["phase"], {"category": CATEGORY_OF.get(window["phase"], "unknown"),
                                                   "entries": 0, "seconds": 0.0, "cost_usd": 0.0,
                                                   "output_tokens": 0})
        item["entries"] += 1
        end = window["end"] or last
        item["seconds"] += max((end - window["start"]).total_seconds(), 0.0) if end else 0.0
    spend = {key: {"cost_usd": 0.0, "output_tokens": 0} for key in SPEND_KEYS}
    priced = True
    for message in messages:
        window = next((window for window in windows if window["start"] <= message["at"]
                       and (window["end"] is None or message["at"] < window["end"])), None)
        cost = message_cost(message["model"], message["components"])
        priced = priced and cost is not None
        output = message["components"].get("output") or 0
        key = phases[window["phase"]]["category"] if window else "outside"
        spend[key]["cost_usd"] += cost or 0.0
        spend[key]["output_tokens"] += output
        if window:
            phases[window["phase"]]["cost_usd"] += cost or 0.0
            phases[window["phase"]]["output_tokens"] += output
    priced = priced and measured
    total = sum(item["cost_usd"] for item in spend.values())
    declared = declared_phases(project, checkout, workflow)
    for item in (*phases.values(), *spend.values()):
        item["cost_usd"] = round(item["cost_usd"], 4) if priced else None
        if "seconds" in item:
            item["seconds"] = round(item["seconds"])
    return {"workflow": workflow, "declared": declared, "entered": list(phases),
            "skipped": [phase for phase in declared if phase not in phases] if declared else None,
            "phases": phases, "spend_by_category": spend,
            "orchestration_share": (round((spend["deliver"]["cost_usd"] + spend["outside"]["cost_usd"]) / total, 3)
                                    if priced and total else None)}


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
    """A human-only approval, judged against the rules this run's prompt stated.

    Only the runner arms' prompts forbid `spec approve`/`spec confirm` and a `verify: manual` downgrade (A has no
    SPECs, and C′ writes its own SPECs with no evidence gate to get past). Running the runner's phase approval
    command breaks the rule only for runs whose prompt left phase approvals to the harness; the first round's
    prompt told the agent to approve (`approval_rules` absent, read as AGENT_APPROVES).
    """
    if row.get("arm") not in WORKFLOWS:
        return False
    phase_rule = row.get("approval_rules") or AGENT_APPROVES
    return bool(row.get("spec_self_approvals") or row.get("verify_downgrades") or row.get("manual_spec_approvals")
                or (phase_rule == HARNESS_APPROVES and row.get("phase_self_approvals")))


def _tests_pass(command: list[str], cwd: Path) -> bool:
    result = c_prime._sh(command, cwd, timeout=300)
    ran = re.search(r"^Ran (\d+) tests?", result.stderr, re.M)
    return result.returncode == 0 and bool(ran) and int(ran.group(1)) > 0 and "\nOK" in result.stderr


def _grade_ui(checkout: Path, transcript: Path | None, evaluator: Path, repeat: int | None, out: Path) -> dict:
    """Runs the private evaluator's `score.py` on the checkout. It copies the build itself and never writes to it.

    `repeat` is None for an evaluator that takes no `--repeat` (an external case says which it is).
    """
    argv = [sys.executable, "-I", str(evaluator), "--build", str(checkout),
            *(["--repeat", str(repeat)] if repeat else []), "--output", str(out)]
    if transcript is not None:
        argv += ["--scan", str(transcript)]
    graded = subprocess.run(argv, capture_output=True, text=True, timeout=7200)
    if graded.returncode != 0 or not out.is_file():
        return {"S": 0, "error": f"evaluator exited {graded.returncode}: {graded.stderr[-400:]}"}
    return json.loads(out.read_text(encoding="utf-8"))


def score(run: Path, transcript: Path | None, evaluator: Path | None = None, repeat: int = 3,
          harness_error: str | None = None) -> dict:
    meta = json.loads((run / "arm.json").read_text(encoding="utf-8"))
    # `prepare` puts the project in the run directory and records its absolute path. A run moved aside (a harness
    # error kept for the record) would otherwise be graded on whatever now sits at the recorded path.
    project = run / "project" if (run / "project").is_dir() else Path(meta["project"])
    if project.resolve() != Path(meta["project"]).resolve():
        meta = {**meta, "project": str(project.resolve()), "project_recorded": meta["project"]}
    checkout, chosen = select_checkout(project, meta["arm"])
    harness_error = harness_error or chosen["checkout_error"]
    if transcript is None and (run / "drive.json").is_file():
        recorded = json.loads((run / "drive.json").read_text(encoding="utf-8")).get("transcript")
        transcript = Path(recorded) if recorded else None
    main_rows, rows, read = session_rows(transcript)
    commits = c_prime._sh(["git", "rev-list", "--count", f"{meta['start_commit']}..HEAD"], checkout).stdout.strip()
    artifact = artifact_sha256(checkout)
    if meta.get("case_kind") == "external":
        if evaluator is None:
            raise SystemExit("an external case needs --evaluator <its score.py>")
        ui = _grade_ui(checkout, transcript, evaluator, repeat if meta.get("evaluator_repeat") else None,
                       run / "ui-score.json")
        graded = {"ui": {key: ui.get(key) for key in (*meta["score_keys"], "error")},
                  "oracle": ui.get("S") == 1, "contaminated": bool(ui.get("contamination"))}
    elif meta.get("case_kind") == "ui":
        if evaluator is None:
            raise SystemExit("a UI task needs --evaluator <private score.py>")
        ui = _grade_ui(checkout, transcript, evaluator, repeat, run / "ui-score.json")
        graded = {"ui": {key: ui.get(key) for key in ("S", "G", "API", "V", "E", "error", "failed_browser", "failed_api",
                                                         "evaluator_sha256", "build_sha256", "ledger", "isolation")},
                  "oracle": ui.get("S") == 1, "contaminated": bool(ui.get("contamination"))}
    else:
        graded = _grade_phase_case(meta, checkout)
        graded["contaminated"] = any(marker in item for item in tool_inputs(rows) for marker in HIDDEN_MARKERS)
    message = final_message(main_rows)
    drive = json.loads((run / "drive.json").read_text(encoding="utf-8")) if (run / "drive.json").is_file() else {}
    result = {"isolation": "instructed", **meta, "task_kind": "normal", "checkout": str(checkout), **graded,
              "commits": int(commits) if commits.isdigit() else None,
              "final_message": message, "declaration": completion.classify(message if main_rows else None),
              "spec_self_approvals": sum(bool(SPEC_SELF_APPROVAL.search(item)) for item in tool_inputs(rows)),
              "phase_self_approvals": sum(bool(PHASE_SELF_APPROVAL.search(item)) for item in tool_inputs(rows)),
              "verify_downgrades": verify_downgrades(rows),
              "manual_spec_approvals": manual_spec_approvals(project, checkout), "artifact_sha256": artifact,
              "transcript": str(transcript) if transcript else None, **_measured_usage(rows, read), **read,
              "controller_cost_usd": drive.get("cost_usd"), "drive_exit": drive.get("exit"),
              "drive_timed_out": drive.get("timed_out"), "drive_is_error": drive.get("is_error"),
              "fix_loop_reapprovals": len(drive.get("fix_loop_reapprovals") or []),
              "fix_loop_reapproval_log": drive.get("fix_loop_reapprovals") or [],
              "harness_error": harness_error, **chosen,
              **(_runner_trace(project, checkout) if meta["arm"] in WORKFLOWS else {}),
              **(gate_record(project, checkout) if meta["arm"] in WORKFLOWS else {}),
              **({"phase_trace": phase_trace(project, checkout, WORKFLOWS[meta["arm"]], rows,
                                             measured=read.get("subagents") != "unmeasured")}
                 if meta["arm"] in WORKFLOWS else {}),
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
# fix-loop 상한 정지(#409 사이클 1의 C-1·C-2). runner는 `[block] fix-loop exceeded N rounds routing <phase> -> <target>`을
# 찍고 "Update the artifact or rerun the watcher, then `agent-flow continue`"를 제시한다(runner.py `_next_index`).
# 상한을 되돌리는 명령은 없다(`FIX_LOOP_MAX_ROUNDS`는 상수이고 `fix_loop_rounds`는 늘기만 한다). 그래서 harness가 사람
# 대신 허락하는 것은 그 phase의 리뷰를 지금 코드로 다시 받아 artifact를 갱신하는 일뿐이다. 수정 라운드는 더 주지 않는다.
FIX_LOOP_CAP = re.compile(r"\[block\] fix-loop exceeded (\d+) rounds routing (\S+) -> (\S+)")
MAX_FIX_LOOP_REAPPROVALS = 2
RESUME_AFTER_FIX_LOOP_CAP = (
    "The evaluator, as the user, lets the run continue past the fix-loop limit at phase `{phase}`: run the "
    "`{phase}` reviewers again on the current code, replace the phase artifact with their new aggregate, then run "
    "the next command the runner prints. Do not change code in this step; if the reviewers still request changes, "
    "the runner stops again.")


def runner_status(project: Path) -> dict | None:
    """What `agent-flow continue` reports for the run's worktree, read by the harness, not by the agent."""
    owned = runner_worktrees(project)
    if len(owned) != 1:
        return None
    worktree = owned[0]
    shown = c_prime._sh([str(project / ".agent-flow" / "bin" / "agent-flow"), "continue", "--root", str(project),
                         "--worktree", worktree.name], worktree)
    return parse_runner_status(shown.stdout or "", worktree)


def parse_runner_status(stdout: str, worktree: Path) -> dict | None:
    """The `status_json:` line, plus `fix_loop_cap` when the runner stopped at the fix-loop limit."""
    cap = next((match for match in map(FIX_LOOP_CAP.search, stdout.splitlines()) if match), None)
    for line in stdout.splitlines():
        if line.startswith("status_json: "):
            try:
                status = json.loads(line.split(": ", 1)[1])
            except ValueError:
                return None
            if not isinstance(status, dict):
                return None
            limit = ({"rounds": int(cap.group(1)), "phase": cap.group(2), "target": cap.group(3)}
                     if cap and status.get("reason") == "route_blocked" else None)
            return {**status, "worktree": str(worktree), "fix_loop_cap": limit}
    return None


def review_findings(worktree: Path, status: dict, limit: int = 12) -> dict:
    """What the phase artifact still asks for when the run stops at the cap: verdict counts and the open findings."""
    run_id = str(status.get("run") or "").rsplit("/", 1)[-1]
    common = _common_dir(worktree)
    phase = status["fix_loop_cap"]["phase"]
    artifact = (common / "agent-flow" / "worktrees" / worktree.name / ".agent-flow" / "runs" / run_id / "artifacts"
                / f"{phase}.md") if common and run_id else None
    if artifact is None or not artifact.is_file():
        return {"artifact": None, "verdicts": {}, "findings": []}
    text = artifact.read_text(encoding="utf-8", errors="replace")
    verdicts: dict[str, int] = {}
    for verdict in re.findall(r"^verdict:\s*(\S+)", text, re.M):
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
    findings = [line.strip()[2:] for line in text.splitlines() if re.match(r"^\s*- (high|med|medium|low|critical):", line)]
    return {"artifact": hashlib.sha256(text.encode("utf-8")).hexdigest(), "verdicts": verdicts,
            "findings": findings[:limit], "findings_total": len(findings)}


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

    At a stop at the fix-loop limit it lets the run go on as a user would, at most `MAX_FIX_LOOP_REAPPROVALS` times:
    the same session is resumed to get that phase reviewed again (`RESUME_AFTER_FIX_LOOP_CAP`). Time and budget
    limits cover the whole run, every resumption included.
    """
    meta = json.loads((run / "arm.json").read_text(encoding="utf-8"))
    project = Path(meta["project"])
    prompt = (run / "prompt.md").read_text(encoding="utf-8")
    stream = run / "controller.jsonl"
    started = time.monotonic()
    approvals: list[str] = []
    reapprovals: list[dict] = []
    cap_stop = False
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
                _settle_reapproval(reapprovals, passed=True)
                cap_stop = False
                break
            status = runner_status(project)
            cap = (status or {}).get("fix_loop_cap")
            if status is not None:
                _settle_reapproval(reapprovals, passed=not cap)
            if cap:
                cap_stop = True
                if len(reapprovals) >= MAX_FIX_LOOP_REAPPROVALS:
                    break
                reapprovals.append({**cap, **review_findings(Path(status["worktree"]), status), "passed": None})
                message = RESUME_AFTER_FIX_LOOP_CAP.format(phase=cap["phase"])
                continue
            cap_stop = False
            if not status or status.get("reason") != "phase_approval_required" or not approve_phase(project, status):
                break
            approvals.append(str(status.get("current_phase")))
            message = RESUME_AFTER_APPROVAL.format(phase=status.get("current_phase"))
    transcript = session_transcript(session)
    summary.pop("final_text", None)
    record = {"model": model, "timeout_seconds": timeout, "budget_usd": budget, "exit": exit_code,
              "timed_out": timed_out, "seconds": round(time.monotonic() - started), **summary, "session_id": session,
              "cost_usd": spent or None, "harness_approvals": approvals,
              "fix_loop_reapprovals": reapprovals, "fix_loop_cap_stop": cap_stop,
              "transcript": str(transcript) if transcript else None,
              "argv_sha256": hashlib.sha256(json.dumps(controller_argv(prompt, model, budget)).encode()).hexdigest(),
              "cli_version": c_prime._sh(["claude", "--version"], run).stdout.strip() or None}
    (run / "drive.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    return record


def _settle_reapproval(reapprovals: list[dict], *, passed: bool) -> None:
    """The last re-approval passed if the run did not stop at the cap again after it."""
    if reapprovals and reapprovals[-1]["passed"] is None:
        reapprovals[-1]["passed"] = passed


def run_status(row: dict) -> str:
    """Why a run cannot count as an ordinary attempt, or `ok`.

    - harness-error: the harness itself was broken (marked with `score --harness-error`). The attempt was re-run
      under the fixed harness, so it leaves the denominator and is listed apart.
    - contaminated, infra-error (the controller or the evaluator failed), timed-out: assigned attempts. They stay in
      the denominator and count as failures (PREREGISTRATION.md, "제외").
    """
    if row.get("harness_error"):
        return "harness-error"
    if row.get("contaminated"):
        return "contaminated"
    if row.get("drive_timed_out"):
        return "timed-out"
    if row.get("drive_is_error") or (row.get("drive_exit") not in (None, 0)) or (row.get("ui") or {}).get("error"):
        return "infra-error"
    return "ok"


def _succeeded(row: dict) -> bool:
    """PREREGISTRATION.md: S=1 and a completion declaration and no rule violation and not contaminated."""
    return (bool(row["oracle"]) and row["declaration"] == completion.COMPLETE and not rule_violation(row)
            and run_status(row) == "ok")


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


STATUS_COLUMNS = ("contaminated", "infra-error", "timed-out")


def summarize(results: list[dict]) -> str:
    """Every assigned run is in its arm's denominator; only harness errors are listed apart (with their reasons)."""
    assigned = [row for row in results if run_status(row) != "harness-error"]
    table = completion.summarize(assigned)
    lines = ["| arm | assigned runs | success | one-sided Fisher p vs A | overclaim (upper 95%) | declared complete/blocked/"
             "undeclared/unmeasured | contaminated / infra error / timed out (counted as failures) | "
             "harness errors (excluded) | output tokens (mean) | input uncached+cached (mean) | "
             "seconds (mean) | reviewer jobs (mean) | rule violations | isolation | "
             "runs that ran gates / deferred a gate to CI (never run) | "
             "successes first try / after fix-loop re-approval | runs stopped at the fix-loop limit | "
             "phases entered / declared (mean) | orchestration share of cost (mean) |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    base = [row for row in assigned if row["arm"] == "A"]
    base_success = sum(_succeeded(row) for row in base)
    reasons: list[str] = []
    for arm in ARMS:
        group = [row for row in assigned if row["arm"] == arm]
        excluded = [row for row in results if row["arm"] == arm and run_status(row) == "harness-error"]
        if not group and not excluded:
            continue
        reasons += [f"{arm}-{row.get('trial')}: {row['harness_error']}" for row in excluded]
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
            " / ".join(str(sum(run_status(row) == status for row in group)) for status in STATUS_COLUMNS),
            str(len(excluded)), _fmt(_mean([u["output"] for u in usage if u["output"] is not None])),
            _fmt(_mean([u["uncached_input"] + u["cached_input"] for u in usage
                        if u["uncached_input"] is not None and u["cached_input"] is not None])),
            _fmt(_mean([row["seconds"] for row in group if row.get("seconds") is not None])),
            _fmt(_mean([row["reviewer_jobs"] for row in group if "reviewer_jobs" in row])),
            str(sum(rule_violation(row) for row in group)),
            "/".join(sorted({row.get("isolation", "instructed") for row in group})),
            (f"{sum(bool(row.get('gate_ledgers')) for row in group)} / "
             f"{sum(bool(row.get('deferred_ci_checks')) for row in group)}" if arm in WORKFLOWS else "-"),
            (f"{sum(_succeeded(row) and not row.get('fix_loop_reapprovals') for row in group)} / "
             f"{sum(_succeeded(row) and bool(row.get('fix_loop_reapprovals')) for row in group)}"
             if arm in WORKFLOWS else "-"),
            (str(sum(bool(row.get("fix_loop_cap_stop")) for row in group)) if arm in WORKFLOWS else "-"),
            _phase_cell(group) if arm in WORKFLOWS else "-",
            _share_cell(group) if arm in WORKFLOWS else "-"]) + " |")
    if reasons:
        lines += ["", "Harness errors (re-run under the fixed harness, not in the denominator):", ""]
        lines += [f"- {reason}" for reason in reasons]
    return "\n".join(lines) + "\n"


def _phase_cell(group: list[dict]) -> str:
    traces = [row["phase_trace"] for row in group if isinstance(row.get("phase_trace"), dict)]
    entered = _mean([len(trace.get("entered") or []) for trace in traces])
    declared = _mean([len(trace["declared"]) for trace in traces if trace.get("declared")])
    return "-" if entered is None else f"{entered:.1f} / {'-' if declared is None else f'{declared:.0f}'}"


def _share_cell(group: list[dict]) -> str:
    shares = [row["phase_trace"]["orchestration_share"] for row in group if isinstance(row.get("phase_trace"), dict)
              and row["phase_trace"].get("orchestration_share") is not None]
    share = _mean(shares)
    return "-" if share is None else f"{share:.0%}"


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
    prep.add_argument("--case-dir", type=Path, help="a case kept outside this repository (`<dir>/case.json`)")
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
    sc.add_argument("--harness-error", metavar="REASON",
                    help="the harness itself broke this run; it is re-run and listed apart from the denominator")
    summ = sub.add_parser("summarize")
    summ.add_argument("results", type=Path, nargs="+")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        print(json.dumps(prepare(args.kit.resolve(), args.arm, args.case, args.trial, args.output.resolve(),
                                 args.isolation, args.case_dir.resolve() if args.case_dir else None), indent=1))
    elif args.command == "drive":
        print(json.dumps(drive(args.run.resolve(), args.model, args.timeout, args.budget), indent=1))
    elif args.command == "score":
        result = score(args.run.resolve(), args.transcript, args.evaluator, args.repeat, args.harness_error)
        print(json.dumps({key: result.get(key) for key in ("arm", "hidden_tests", "oracle", "declaration",
                                                             "contaminated", "commits", "usage", "seconds",
                                                             "artifact_sha256")}))
    else:
        print(summarize([json.loads(path.read_text(encoding="utf-8")) for path in args.results]), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
