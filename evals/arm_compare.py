"""#410 arm comparison: one task under A (bare model), B (development), C (full-feature), D (default), C′ (prompt only).

    python evals/arm_compare.py prepare --kit . --arm C --case devflow --trial 1 --output <work>/C-1
    python evals/arm_compare.py drive <work>/C-1            # headless controller in <work>/C-1/project
    python evals/arm_compare.py score <work>/C-1            # transcript from drive.json, or --transcript
    python evals/arm_compare.py summarize <work>/*/result.json

    # follow-on change (#409 program v2 §3c): the fixed bare agent changes the code an arm run left
    python evals/arm_compare.py followon <work>/C-1 --followon-dir <private>/<task>/followon/F1 --output <work>/C-1.F1
    python evals/arm_compare.py drive <work>/C-1.F1
    python evals/arm_compare.py score <work>/C-1.F1 --evaluator <private>/<task>/followon/F1/score.py

    # chain step (H2-①, accumulation): the SAME arm develops the next step on its own previous result, new session
    python evals/arm_compare.py chain-next <work>/C-1 --case-dir <private>/<task>/steps/v1 --output <work>/C-1.s2
    python evals/arm_compare.py drive <work>/C-1.s2 && python evals/arm_compare.py score <work>/C-1.s2 --evaluator ...

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
- AP (A+P): A plus the fixed `PRINCIPLES` (8 code principles and a pre-completion check) as the project's CLAUDE.md,
  committed before the agent starts. The prompt is A's, word for word.
- PS (A+pstack): like AP with `PSTACK_PRINCIPLES` instead, lines taken word for word from pstack's principle and
  playbook skills (MIT, see `evals/third_party/pstack-LICENSE`). AP against PS tells which principles, A against
  either tells whether stating principles at all moves the endpoints.
- V (verdict only): A's prompt, word for word, plus a fixed completion gate as the project's Stop hook
  (`verify_gate.py`, #409 adversarial review: fix the adjudication, leave the procedure to the model). On each stop
  it runs the case's verification command and blocks while it fails (at most 3 times), then runs one independent
  read-only `claude -p` review against the task documents and blocks once with its findings; after the caps the
  stop goes through. `.claude/settings.json` is committed before the agent starts; the hook script and its record
  (`verify-gate.jsonl`) stay in the run directory, outside the agent's diff. The reviewer's cost is kept apart from
  the controller's and added to the run's `total_cost_usd`.
- VK (V+skills): V word for word (prompt, Stop-hook gate) plus the kit's skills as the project's Claude Code skills
  (`.claude/skills/<name>/`, committed with V's settings before the agent starts). Claude Code lists them by name and
  description and loads a body only when the model invokes it; nothing tells the agent to use them (no workflow, no
  runner, no CLAUDE.md). A skill that runs `agent-flow` commands, or works only inside the runner, is left out
  (`EXCLUDED_SKILLS`, recorded with its reason); `prepare` fails if a skill not on that list names an `agent-flow`
  command in a code span, unless `CLI_MENTIONS_KEPT` says why it works without the CLI. `arm.json` `skills` keeps each
  copied skill's sha256 and the exclusions. Every run's result has `skills_invoked`, the skills the transcript's Skill
  tool calls loaded.
- B, C: the kit is installed with its hooks; the agent drives `agent-flow run --workflow development|full-feature`
  and answers the runner's decisions from the task text. It is not the human approver: `spec approve`/`spec confirm`
  and turning a SPEC's `verify` into `manual` are off limits, because they let the worker pass its own evidence
  gate (C-4 and C-5 of the first round did). Phase approvals come from the harness: when the runner pauses a phase
  for the user, `drive` runs the runner's own approval command and resumes the same session
  (`drive.json` `harness_approvals`); an agent that runs it itself counts as a self-approval. There is no git
  remote, so the run ends at the first phase that needs one.
- S: a reduced-workflow search variant (#409 program v2 §5): the kit installed as for B, C and D, plus a variant of
  `full-feature` written into the project by `variants.py` (phases dropped from a fixed discrete set, reviewer count
  as a project profile override). `prepare --variant '<json>'`; the record keeps the variant spec, name and digests.
- D: like B and C with the kit's `default` workflow, the one it applies to every project and which says the agent
  sets each phase's depth per task (#409 v2.1, arm D).
- C′: `c_prime.build()` of the same project, stopping after `commit` (#427).
- R (router): the model chooses the workflow. After seeding, one read-only `claude -p` router (`router.py`; the
  controller's model, Read/Glob/Grep only, 10 minutes, $2) gets the task text and a fixed menu (`none` = A,
  `verify-only` = V, `verify-skills` = VK, `development` = B, `default` = D, `full-feature` = C, `compose` = S with a
  variant the router assembles, plus each `--menu-variant` S spec under its variant name) and answers
  `{"choice": ..., "reason": ...}` on its last line (`compose` adds `"spec"`, checked by `variants.normalize`; the
  router record keeps it with its `variant_name`); a bad answer is retried once, then `none` is used with
  `router.fallback: true`. The decision (choice, reason, cost, tokens, seconds, menu sha256, raw
  output paths) is written to `arm.json` `router` before the chosen arm is prepared in the same run directory, and
  `drive` starts a fresh controller on it. The router's cost is added to the run's `total_cost_usd`.

Layers and the simulated user (grill experiment): an external case may declare `layers` (for example `complete`
and `underspec`, `select_layer`), picked with `prepare --layer`; `arm.json` keeps `layer`. A case or layer with
`user_sim: {"oracle": "<path in the case dir>"}` gets `<run>/user-sim/ask-user` (`user_sim.py`, outside the
project; the oracle is a copy no project file comes from) and every arm's task text gains `ASK_USER_LINE`; without
`user_sim` no prompt changes. A workflow controller is told to put a phase's user questions to `ask-user` instead
of answering them itself, and before approving `domain-grill` the harness checks that the artifact's `user round`
decisions have at least as many `ask-user` calls behind them (`grill_check`; sent back once, then approved and
recorded in `drive.json` `grill_checks`). `score` reads `<run>/user-sim.jsonl` into `ask_user_calls`,
`ask_user_questions`, `asked_any` and `user_sim_cost_usd` (added to `total_cost_usd`), and marks a run contaminated
when a tool call names the oracle copy or the simulator's config. `arm.json` `user_sim` keeps the oracle's sha256.

Chains (H2-①, accumulation across sessions): a follow-on runs a fixed bare agent on an arm's code (maintainability);
`chain-next` runs the same arm again (its arm.json setup, R's frozen router decision reused without a router call,
the drive model and effort, which `drive` then requires) on the previous step's committed result plus its
uncommitted work, with the next step's task files as the seed commit. It refuses a previous run with no drive.json
or result.json, or one that is contaminated, infra-errored or a harness error. What the arm built up stays: its
history, a workflow arm's kit state (reinstalled from the same kit copy so hook paths name the new project) and its
earlier runner runs in the kit's archive; harness-owned config is this step's (V's gate, the simulated user). `arm.json`
`chain` keeps `step`, `prev_run`, `prev_head`, `root_run`; `score` also marks a run contaminated when a tool call
names an earlier step's `result.json` or `ui-score.json`, and `summarize` adds per-chain S/G and steps survived.

Every run's result carries `integrity` (`integrity.check`, #409 v2 §3a): deleted, disabled or weakened tests and
stubs in the diff from the start commit, a "tests pass" claim with no test command in the transcript, and changes
left uncommitted by a run that declared `complete`. It is recorded and summarized, not yet part of the success rule
(that waits for the preregistration v1 update, applied from the next cycle).

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
import variants  # noqa: E402
import integrity  # noqa: E402
import router  # noqa: E402
import user_sim  # noqa: E402
import verify_gate  # noqa: E402
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
                  "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "CODEX_HOME", "OPENAI_API_KEY",
                  # 실험 run의 effort(#409 v3.5). controller가 띄우는 reviewer·gate·router·시뮬레이터도 같은 값을 받는다.
                  "CLAUDE_CODE_EFFORT_LEVEL")
AGENT_FLOW_WORKTREES = Path.home() / ".agent-flow" / "worktrees"
CLAUDE_TOOLS = ("Bash", "Read", "Edit", "MultiEdit", "Write", "Glob", "Grep", "TodoWrite", "Task")
ARMS = ("A", "AP", "PS", "V", "VK", "B", "C", "D", "S", "Cp", "R")
# R: the router picks one of these arms' setups (`router.OPTIONS`, plus S variants); the record keeps `arm: "R"` and the
# picked `effective_arm`, and everything that depends on the setup reads `effective_arm(meta)`.
ROUTED_ARMS = ("R",)
DEFAULT_MODEL = "claude-opus-5-5"
# no kit runner; the agent gets the task prompt only (and, AP and PS, a CLAUDE.md; V, a Stop hook; VK, also skills)
BARE_ARMS = ("A", "AP", "PS", "V", "VK")
GATE_ARMS = ("V", "VK")  # the bare prompt plus `verify_gate.py` as the project's Stop hook
SKILL_ARMS = ("VK",)  # plus the kit's skills as project skills
WORKFLOWS = {"B": "development", "C": "full-feature", "D": "default", "S": variants.BASE}  # S: the variant's base
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


# A case or layer with `user_sim` gives every arm the same way to ask the product owner (`user_sim.py`). The line is
# part of the task text, so a workflow arm's runner, and so its domain-grill, sees it too. Without `user_sim` no
# prompt changes by a byte.
ASK_USER_LINE = ('If a requirement is unclear, you may ask the product owner by running `{script} "<question>"`; '
                 "answers come back on stdout.")
ANSWER_FROM_TASK = "answer it from the task text"
ANSWER_FROM_OWNER = ("ask the product owner with that `ask-user` command and record the answers as that user round; "
                     "do not answer it yourself")


def with_ask_user(task: str, ask_user: str | None) -> str:
    return task if ask_user is None else f"{task}\n\n{ASK_USER_LINE.format(script=ask_user)}"


def driver_prompt(task: str, workflow: str, ask_user: bool = False) -> str:
    answer = ANSWER_FROM_OWNER if ask_user else ANSWER_FROM_TASK
    return f"""# Task

{task}

Use Agent Flow, installed in this project, to do this task with the `{workflow}` workflow. Start it with:

    .agent-flow/bin/agent-flow run {shlex.quote(task)} --workflow {workflow}

Do each phase the runner prints and continue with the command it prints. When a phase asks the user a
question, {answer}. You are not the human approver: the evaluator approves phases. When the
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


def load_external_case(case_dir: Path, *, seeded: bool = True) -> dict:
    """A case kept outside this repository (a holdout in the private evaluator repo): `<dir>/case.json`.

    It names a seed repository at a pinned commit, the task files to place in the project, the task text and the
    evaluator protocol. Only its name and sha256 go into the run record, never its path. A chain step (`seeded`
    False) starts from the previous step's code, so its `seed` is optional and never used.
    """
    raw = (case_dir / "case.json").read_bytes()
    case = json.loads(raw)
    for key in ("name", *(("seed",) if seeded else ()), "task", "profile", "install", "score_keys"):
        if key not in case:
            raise SystemExit(f"external case is missing `{key}`")
    if "files" not in case and "layers" not in case:
        raise SystemExit("external case is missing `files`")
    return {**case, "sha256": hashlib.sha256(raw).hexdigest()}


def select_layer(case: dict, case_dir: Path, layer: str | None) -> dict:
    """The task files and the simulated user for one layer of an external case.

    `layers` maps a name to either a files map (`{"prd.md": "task/prd-full.md"}`) or `{"files": {...}, "user_sim":
    {"oracle": "<path in the case dir>"}}`. The layer's files go over the case's own `files`; its `user_sim`, or else the
    case's, names the oracle. A case with layers needs `--layer`, one without takes none. The oracle must be a file in
    the case directory that no project file is copied from, so the agent never gets it through the project.
    """
    layers = case.get("layers")
    if layers is None and layer is not None:
        raise SystemExit(f"case {case['name']} has no layers; --layer {layer} does not apply")
    if layers is not None and layer not in layers:
        raise SystemExit(f"case {case['name']} needs --layer, one of {sorted(layers)}")
    chosen = layers[layer] if layers is not None else {}
    structured = isinstance(chosen.get("files"), dict)
    files = {**case.get("files", {}), **(chosen["files"] if structured else chosen)}
    simulated = (chosen.get("user_sim") if structured else None) or case.get("user_sim")
    oracle = None
    if simulated is not None:
        if not isinstance(simulated, dict) or not isinstance(simulated.get("oracle"), str):
            raise SystemExit("`user_sim` must be {\"oracle\": \"<path in the case dir>\"}")
        oracle = (case_dir / simulated["oracle"]).resolve()
        if not oracle.is_file() or not oracle.is_relative_to(case_dir.resolve()):
            raise SystemExit(f"the oracle {simulated['oracle']} is not a file in the case directory")
        if any((case_dir / source).resolve() == oracle for source in files.values()):
            raise SystemExit(f"the oracle {simulated['oracle']} is also copied into the project")
    return {"layer": layer, "files": files, "oracle": oracle}


SCP_LIKE = re.compile(r"^[\w.-]+@[\w.-]+:")


def seed_repository(repository: str, case_dir: Path) -> str:
    """A relative seed path (`"seed.bundle"`) is the case directory's; absolute paths and URLs are left as written."""
    if "://" in repository or SCP_LIKE.match(repository) or Path(repository).is_absolute():
        return repository
    return str((case_dir / repository).resolve())


def _seed_external_project(case: dict, case_dir: Path, project: Path) -> None:
    """The seed repository at its pinned commit with no remote, then the task files in one commit.

    The clone's reflog names where it came from, and a seed inside the private checkout would tell the agent where the
    checks live, so the reflog is emptied along with the remote.
    """
    seed = case["seed"]
    c_prime._sh(["git", "clone", "-q", "--no-checkout", seed_repository(seed["repository"], case_dir), str(project)],
                project.parent, check=True)
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


# Arm A+P (#409 program v2): the bare arm plus these principles as the project's CLAUDE.md, so the effect of stating
# the principles can be told apart from the effect of the workflow's phases. The text is fixed; its digest is recorded.
PRINCIPLES_VERSION = "ap-v1"
PRINCIPLES = """# Engineering principles for this repository

Follow these in every change. Prefer the repository's existing patterns, utilities and architecture.

1. **Single responsibility.** A function, class or module has one reason to change. Split code that mixes concerns.
2. **Avoid side effects.** Keep logic pure where it can be; isolate I/O, state changes and framework calls at the edges.
3. **Don't repeat yourself.** Reuse what exists before writing new code; do not copy logic into a second place.
4. **Minimize parameters.** Keep parameter lists short; group values that travel together into a type.
5. **Fail fast.** Validate inputs and invariants early and fail loudly with a specific error instead of continuing.
6. **Guard clauses.** Handle the exceptional cases first and return early, rather than nesting the main path.
7. **Single level of abstraction.** Each function reads at one level; move lower-level detail into named helpers.
8. **Explicit receivers.** Make it obvious which object a call acts on; avoid implicit or ambient receivers.

Before declaring the work done: run the tests, check that the change stays in the task's scope, and re-read the diff
for regressions, duplication and code that does not match the surrounding style.
"""
PRINCIPLES_SHA256 = hashlib.sha256(PRINCIPLES.encode("utf-8")).hexdigest()

# Arm PS (#409, pstack review): the same slot filled from pstack (github.com/cursor/plugins, pstack/ at 8b3326d,
# MIT). Every bullet is pstack's own wording, from the principle skills, the code-quality lens and figure-it-out /
# autonomous-run; only "PR" became "change" in the file-size rule and examples meant for PRs or humans were cut.
# Lines that allow deleting tests (test-behavior's "rewrite or delete the rest", migrate-callers) are left out:
# integrity is an endpoint. Skills that need subagents, other models, a human or a PR are not represented.
PSTACK_PRINCIPLES_VERSION = "ps-v1"
PSTACK_PRINCIPLES = """# Engineering principles for this repository

Follow these in every change.

## Before writing code

- Read all affected files and understand the current design.
- State the exit condition as a checkable predicate before the first iteration.

## Structure

- Encode the real domain in a data structure instead of scattering it across conditionals.
- Do not force an abstraction. Prefer boring code if the current shape is already clear, local, and unlikely to grow.
- Place validation, type narrowing, and error handling at system boundaries. Trust internal code unconditionally.
  Business logic lives in pure functions. The shell is thin and mechanical.
- Do not re-export transport, storage, framework, or wire types through the public surface.
- Keep logic in the canonical layer and reuse existing helpers. Give each piece of state one owner. Keep one list and
  derive the others from it.
- Collapse layers that cost more than they save: wrappers with one caller, adapters with no second implementation,
  speculative indirection that was never needed.
- Shrink state scope: prefer pure functions (returns over mutations), locals over fields, fields over module state,
  and module state over globals. Derive instead of sync.
- Treat "weird if statements in random places" as a design problem, not a style nit.
- Do not let a change push a file from under 1k lines to over 1k lines without a very strong reason.
- Make the smallest change that solves the problem. No speculative validators, parsers, or guards beyond what the
  spec demands.

## Tests

- Derive the expected value from the requirement, not from a run of the code. Assert what the contract promises, no
  less and no more.
- Break the behavior. Make the code return a wrong value, skip an effect, or do the effect twice. If the test still
  passes, it proves nothing.
- Do not change tests merely to match a wrong implementation.
- Do not weaken existing assertions unless the expected behavior has genuinely changed and the reason is clear.

## When something fails

- Reproduce first.
- When stuck, instrument. Read the actual error and the actual state. Do not guess.
- Guards, retries, swallowed errors, clamped values, and discarded state are the usual ways to hide a bug.
- When two or more fixes that share one premise have failed the same gate, suspect the premise, not the fixes.

## Done

- Verify every task output by checking the real thing directly. Do not infer from proxies, self-reports, or "it
  compiles."
- When verification fails, suspect the observation method before suspecting the system.
- A verdict is VERIFIED, NOT VERIFIED, or INCONCLUSIVE. Inconclusive is not a pass. Don't hide a negative.
- Don't ask "should I do X?" Do X, explain why.
- Every claim carries its evidence or its label in the same sentence. Measured, inferred, or guess.
"""
PSTACK_PRINCIPLES_SHA256 = hashlib.sha256(PSTACK_PRINCIPLES.encode("utf-8")).hexdigest()

PRINCIPLE_SETS = {"AP": (PRINCIPLES_VERSION, PRINCIPLES, PRINCIPLES_SHA256),
                  "PS": (PSTACK_PRINCIPLES_VERSION, PSTACK_PRINCIPLES, PSTACK_PRINCIPLES_SHA256)}


def _write_principles(project: Path, arm: str) -> None:
    """AP's and PS's one difference from A: the arm's principles as CLAUDE.md, committed before the agent starts."""
    (project / "CLAUDE.md").write_text(PRINCIPLE_SETS[arm][1], encoding="utf-8")
    _git(["add", "--force", "CLAUDE.md"], project)  # the mobile seeds' .gitignore lists CLAUDE.md
    _git(["commit", "-qm", "principles"], project)


def principles_record(arm: str) -> dict | None:
    if arm not in PRINCIPLE_SETS:
        return None
    version, _, digest = PRINCIPLE_SETS[arm]
    return {"version": version, "sha256": digest}


# Arm V's gate. The verification command comes from the case, never from its hidden checks: an external case's
# `verify_command`, a phase case's visible `test_command`, or the gradle command an android case's `bare_note` names.
# Without one, the gate runs `npm test --silent` when the project's package.json has a test script by then.
GRADLE_COMMAND = re.compile(r"\./gradlew(?:\s+[\w:-]+)+")
GATE_SETTINGS = Path(".claude") / "settings.json"


def gate_verify_command(case: dict, kind: str) -> tuple[list[str] | None, str | None]:
    """(argv, where it came from)."""
    command = case.get("verify_command")
    if command:
        return (shlex.split(command) if isinstance(command, str) else list(command)), "case"
    if kind == "phase-case":
        return list(case["test_command"]), "test_command"
    found = GRADLE_COMMAND.search(case.get("bare_note") or "") if case.get("profile") == "android" else None
    return (shlex.split(found.group(0)), "bare_note") if found else (None, None)


def _install_verify_gate(output: Path, project: Path, task: str, documents: list[str], case: dict,
                         kind: str) -> dict:
    """V's one difference from A: the gate's Stop hook in the project, committed before the agent starts.

    The script and its config live in `<run>/gate/` and are named by absolute path, so the agent's diff never holds
    them. `drive` writes the controller's model into the config; the reviewer uses the same one.
    """
    gate = output / "gate"
    gate.mkdir()
    script = gate / "verify_gate.py"
    shutil.copy2(HERE / "verify_gate.py", script)
    argv, source = gate_verify_command(case, kind)
    config = {"version": verify_gate.VERSION, "project": str(project.resolve()), "run_dir": str(output.resolve()),
              "task": task, "documents": documents, "verify_command": argv, "model": "claude-opus-5-5",
              "env": {"keys": list(CONTROLLER_ENV), "set": controller_env({})}}
    (gate / "config.json").write_text(json.dumps(config, indent=1) + "\n", encoding="utf-8")
    settings_path = project / GATE_SETTINGS
    settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.is_file() else {}
    command = f"python3 -I {shlex.quote(str(script.resolve()))} {shlex.quote(str((gate / 'config.json').resolve()))}"
    settings.setdefault("hooks", {}).setdefault("Stop", []).append(
        {"hooks": [{"type": "command", "command": command, "timeout": verify_gate.HOOK_TIMEOUT}]})
    settings_path.parent.mkdir(exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=1) + "\n", encoding="utf-8")
    _git(["add", "--force", str(GATE_SETTINGS)], project)  # seed의 .gitignore가 `.claude`를 가릴 수 있다
    _git(["commit", "-qm", "verify gate"], project)
    return {"version": verify_gate.VERSION, "script": str(script.resolve()),
            "script_sha256": verify_gate.file_sha256(script), "settings_sha256": verify_gate.file_sha256(settings_path),
            "verify_command": argv, "verify_source": source, "documents": documents,
            "max_verify_blocks": verify_gate.MAX_VERIFY_BLOCKS, "max_review_rounds": verify_gate.MAX_REVIEW_ROUNDS,
            "review_budget_usd": verify_gate.REVIEW_BUDGET_USD, "review_timeout_seconds": verify_gate.REVIEW_TIMEOUT}


# Arm VK's skills. Claude Code reads project skills from `.claude/skills/<name>/SKILL.md`; a skill's other files are
# its bundled references and scripts. `agents/` holds Codex's `openai.yaml`, host metadata Claude Code does not read.
SKILLS_VERSION = "vk-v1"
PROJECT_SKILLS = Path(".claude") / "skills"
SKILL_HOST_METADATA = ("agents",)
# A skill left out because it does not work without the kit's CLI or runner. The reason is recorded in `arm.json`.
EXCLUDED_SKILLS = {
    "agent-flow": "drives the runner: `.agent-flow/bin/agent-flow status`, `run`, `spec`, `worktree create`",
    "agent-flow-diagnosing-bugs": "runs inside the runner's phase: `agent-flow status`, `agent-flow run`, "
                                  "`agent-flow worktree create`",
    "full-feature-workflow": "drives the runner: `agent-flow status` and the `next_command` it prints",
    "push-watch": "`agent-flow status`, `agent-flow pr-watch` and `agent-flow abort` on a pushed PR; the runner routes",
    "architecture-reviewer": "aggregates the artifacts of the runner's reviewer subprocesses in the full-feature "
                             "architecture-review phase, which its description names",
    "comment-checker": "configures the kit's comment-checker hooks, which this arm does not install",
}
# A kept skill whose files name an `agent-flow` command in a code span, and why it works without the CLI.
CLI_MENTIONS_KEPT = {
    "qa": "`agent-flow run-command` is used only when a phase supplies it; otherwise the skill records the command "
          "and exit it saw",
    "code-generation-discipline": "`agent-flow skills sync` is named only as the owner of installation; the skill "
                                  "says to continue with the skills available",
}
CLI_COMMAND = re.compile(r"`[^`\n]*\bagent-flow\s+[a-z][a-z-]*[^`\n]*`")
SKILL_TOOL = "Skill"


def skill_files(skill: Path) -> list[Path]:
    """A skill's files, relative and sorted: SKILL.md and what it bundles, without host metadata or caches."""
    return sorted(path.relative_to(skill) for path in skill.rglob("*")
                  if path.is_file() and path.relative_to(skill).parts[0] not in SKILL_HOST_METADATA
                  and not {"__pycache__", "node_modules"} & set(path.relative_to(skill).parts))


def skill_cli_commands(skill: Path) -> list[str]:
    """The code spans in a skill's files that name an `agent-flow` command."""
    found: list[str] = []
    for relative in skill_files(skill):
        found += CLI_COMMAND.findall((skill / relative).read_text(encoding="utf-8", errors="replace"))
    return found


def skill_sha256(skill: Path) -> str:
    lines = [f"{relative.as_posix()}\0{verify_gate.file_sha256(skill / relative)}\n" for relative in skill_files(skill)]
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def _install_skills(agent_kit: Path, project: Path) -> dict:
    """VK's one difference from V: the kit's skills in `.claude/skills/`, committed before the agent starts."""
    source = agent_kit / "skills"
    items, excluded, mentions = [], [], []
    for skill in sorted(path for path in source.iterdir() if (path / "SKILL.md").is_file()):
        if skill.name in EXCLUDED_SKILLS:
            excluded.append({"name": skill.name, "reason": EXCLUDED_SKILLS[skill.name]})
            continue
        commands = skill_cli_commands(skill)
        if commands and skill.name not in CLI_MENTIONS_KEPT:
            raise SystemExit(f"skill {skill.name} names agent-flow commands {sorted(set(commands))}: add it to "
                             "EXCLUDED_SKILLS or CLI_MENTIONS_KEPT with the reason")
        if skill.name in CLI_MENTIONS_KEPT:
            mentions.append({"name": skill.name, "reason": CLI_MENTIONS_KEPT[skill.name]})
        target = project / PROJECT_SKILLS / skill.name
        for relative in skill_files(skill):
            (target / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(skill / relative, target / relative)
        items.append({"name": skill.name, "sha256": skill_sha256(target),
                      "skill_md_sha256": verify_gate.file_sha256(target / "SKILL.md"),
                      "files": len(skill_files(target))})
    _git(["add", "--force", str(PROJECT_SKILLS)], project)  # seed의 .gitignore가 `.claude`를 가릴 수 있다
    _git(["commit", "-qm", "skills"], project)
    listing = "".join(f"{item['name']}\0{item['sha256']}\n" for item in items)
    return {"version": SKILLS_VERSION, "path": PROJECT_SKILLS.as_posix(), "source": "kit skills/",
            "sha256": hashlib.sha256(listing.encode("utf-8")).hexdigest(), "items": items,
            "names": [item["name"] for item in items], "excluded": excluded, "cli_mentions_kept": mentions}


def skills_invoked(rows: list[dict]) -> dict:
    """The Skill tool calls in the transcript: the names loaded (call order, repeats kept) and the calls that failed.

    A call counts as loaded unless its tool result is an error (a denied or unknown skill).
    """
    failed_ids = set()
    for row in rows:
        message = row.get("message")
        if row.get("type") != "user" or not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        failed_ids |= {block.get("tool_use_id") for block in message["content"]
                       if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error")}
    invoked, failed = [], []
    for use in _tool_uses(rows):
        if use.get("name") != SKILL_TOOL:
            continue
        data = use.get("input") if isinstance(use.get("input"), dict) else {}
        name = data.get("skill") if isinstance(data.get("skill"), str) else data.get("command")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip().lstrip("/")
        (failed if use.get("id") in failed_ids else invoked).append(name)
    return {"skills_invoked": invoked, "skill_calls_failed": failed}


def _ui_documents(spec: dict) -> list[str]:
    task = json.loads((UI_TASKS / spec["task"] / "task.json").read_text(encoding="utf-8"))
    return sorted([*task["layers"][spec["layer"]], *(f"fixtures/{name}" for name in task["fixtures"])])


# What a workflow arm's kit leaves in the project. A follow-on starts from the code alone, so the fixed follow-on agent
# cannot tell which arm wrote it and gets no kit instructions (the seed of every external case has none of these).
# The installer also lists every path it adds in `.git/info/exclude`; `_strip_kit` removes those too.
KIT_TRACES = (".agent-flow", ".agents", ".omp", ".claude", ".codex", ".Codex", "CLAUDE.md", "AGENTS.md")
BUILD_OUTPUTS = (".git", "build", ".gradle", ".kotlin", "local.properties", "node_modules", ".pytest_cache",
                 "__pycache__")


def load_followon_case(case_dir: Path) -> dict:
    """A follow-on change kept in the private evaluator repo: `<dir>/case.json` (name, files, task, score_keys)."""
    raw = (case_dir / "case.json").read_bytes()
    case = json.loads(raw)
    for key in ("name", "files", "task", "score_keys"):
        if key not in case:
            raise SystemExit(f"follow-on case is missing `{key}`")
    return {**case, "sha256": hashlib.sha256(raw).hexdigest()}


def _strip_kit(base_project: Path, project: Path) -> None:
    """Removes the paths the kit's installer recorded in the base's `.git/info/exclude`, then links into the kit or
    left dangling by the removal (skill links point into `.agent-flow`)."""
    exclude = base_project / ".git" / "info" / "exclude"
    lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.is_file() else []
    for line in lines:
        entry = line.strip().strip("/")
        if not entry or line.lstrip().startswith(("#", "!")) or any(char in entry for char in "*?["):
            continue
        target = project / entry
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
    for path in sorted(project.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink() and (".agent-flow" in os.readlink(path) or not path.exists()):
            path.unlink()


def prepare_followon(base_run: Path, case_dir: Path, trial: int, output: Path) -> dict:
    """A bare-model run of one follow-on change on top of the code an arm run left (#409 program v2 §3c).

    The project is a fresh repository holding the base checkout as the arm left it (committed or not), without the
    kit's traces or build outputs, plus the follow-on's task files, in one commit. It is an A run in every other
    respect, so `drive` and `score` treat it like one; `followon_of` names the base run and its artifact fingerprint.
    """
    base = json.loads((base_run / "arm.json").read_text(encoding="utf-8"))
    base_project = base_run / "project" if (base_run / "project").is_dir() else Path(base["project"])
    checkout, chosen = select_checkout(base_project, effective_arm(base))
    if chosen["checkout_error"]:
        raise SystemExit(f"base run has no gradable checkout: {chosen['checkout_error']}")
    case = load_followon_case(case_dir)
    output.mkdir(parents=True)
    project = output / "project"
    shutil.copytree(checkout, project, symlinks=True, ignore=shutil.ignore_patterns(*KIT_TRACES, *BUILD_OUTPUTS))
    _strip_kit(base_project, project)
    for target, source in case["files"].items():
        (project / target).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(case_dir / source, project / target)
    for args in (["init", "-q", "-b", "main"], ["config", "user.name", "eval"],
                 ["config", "user.email", "eval@example.com"], ["config", "commit.gpgsign", "false"],
                 ["add", "-A"], ["commit", "-qm", "base"], ["checkout", "-qb", BRANCH]):
        _git(args, project)
    prompt = external_bare_prompt(case)
    (output / "prompt.md").write_text(prompt, encoding="utf-8")
    meta = {"arm": "A", "case": case["name"], "trial": trial, "project": str(project.resolve()),
            "start_commit": _git(["rev-parse", "HEAD"], project),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(), "kit_commit": None,
            "cprime_rules": None, "isolation": "instructed", "approval_rules": None, "case_kind": "external",
            "case_sha256": case["sha256"], "seed_commit": None, "score_keys": case["score_keys"],
            "evaluator_repeat": False,
            "followon_of": {"arm": base["arm"], "effective_arm": effective_arm(base), "case": base["case"],
                            "trial": base["trial"],
                            "artifact_sha256": artifact_sha256(checkout)}}
    (output / "arm.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    return meta


# A chain step (H2-①, accumulation across sessions): the same arm develops the next step of a task on top of its own
# previous result, in a new session. Unlike a follow-on, the arm's kit, hooks, skills and what it wrote stay.
CHAIN_REFUSED = ("harness-error", "contaminated", "infra-error")
# The kit's live run state in `.agent-flow/`: a chain step starts no run in progress. Finished runs are archived.
KIT_RUNTIME_STATE = ("runs", "state", "install-recovery", "install.lock")
CHAIN_SEED = "chain step {step}: task"


def _chain_previous(prev_run: Path) -> tuple[dict, dict, dict]:
    """(arm.json, result.json, drive.json) of the run a chain step builds on, or SystemExit with the reason."""
    for name, why in (("arm.json", "it is not a prepared run"), ("drive.json", "drive it first"),
                      ("result.json", "score it first: a chain step builds only on a run known to be clean")):
        if not (prev_run / name).is_file():
            raise SystemExit(f"the previous run {prev_run} has no {name}; {why}")
    meta, result, driven = (json.loads((prev_run / name).read_text(encoding="utf-8"))
                            for name in ("arm.json", "result.json", "drive.json"))
    if meta.get("case_kind") != "external" or "followon_of" in meta:
        raise SystemExit("a chain builds on an external-case arm run (prepare --case-dir) or an earlier chain step")
    status = run_status(result)
    if status in CHAIN_REFUSED:
        raise SystemExit(f"the previous run is {status}; a chain does not build on it")
    return meta, result, driven


def _start_from_previous(checkout: Path, project: Path) -> str:
    """A new repository whose `main` is the previous checkout's HEAD, with its history and no remote or other refs."""
    project.mkdir(parents=True)
    for args in (["init", "-q", "-b", "main"], ["config", "user.name", "eval"],
                 ["config", "user.email", "eval@example.com"], ["config", "commit.gpgsign", "false"]):
        _git(args, project)
    head = _git(["rev-parse", "HEAD"], checkout)
    _git(["fetch", "-q", "--no-tags", str(checkout), "HEAD"], project)
    _git(["checkout", "-q", "-B", "main", head], project)
    _git(["reflog", "expire", "--expire=all", "--all"], project)
    return head


def _carry_uncommitted(checkout: Path, project: Path) -> list[str]:
    """The follow-on rule: the checkout as the arm left it, committed or not. Uncommitted work is one commit."""
    listed = c_prime._sh(["git", "ls-files", "-co", "--exclude-standard", "-z"], checkout).stdout.split("\0")
    for name in (item for item in listed if item and not item.startswith(".agent-flow/runs/")):
        source, target = checkout / name, project / name
        if source.is_symlink() or source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink() or target.is_file():
                target.unlink()
            shutil.copy2(source, target, follow_symlinks=False)
    for name in c_prime._sh(["git", "ls-files", "-d", "-z"], checkout).stdout.split("\0"):
        if name and (project / name).is_file():
            (project / name).unlink()
    _git(["add", "-A"], project)
    changed = [item for item in _git(["diff", "--cached", "--name-only", "-z"], project).split("\0") if item]
    if changed:
        _git(["commit", "-qm", "uncommitted work of the previous step"], project)
    return changed


def _carry_kit_state(prev_project: Path, project: Path) -> list[str]:
    """What the kit keeps out of git in the previous project (its install, project settings, local skills,
    handoffs), without build outputs or live run state. `_install` then rewrites the paths it holds."""
    common = _common_dir(prev_project)
    if common is not None and (common / "info" / "exclude").is_file():
        (project / ".git" / "info").mkdir(parents=True, exist_ok=True)
        shutil.copy2(common / "info" / "exclude", project / ".git" / "info" / "exclude")
    listing = c_prime._sh(["git", "ls-files", "-o", "-i", "--exclude-standard", "--directory", "-z"],
                          prev_project).stdout.split("\0")
    kit_dir = (prev_project / ".agent-flow").resolve()

    def ignore(directory: str, names: list[str]) -> set[str]:
        skipped = {name for name in names if name in BUILD_OUTPUTS}
        return skipped | (set(names) & set(KIT_RUNTIME_STATE) if Path(directory).resolve() == kit_dir else set())

    copied: list[str] = []
    for name in sorted(item.rstrip("/") for item in listing if item.strip("/")):
        if set(Path(name).parts) & set(BUILD_OUTPUTS) or any(name.startswith(f"{done}/") for done in copied):
            continue
        if name.startswith(".agent-flow/") and Path(name).parts[1] in KIT_RUNTIME_STATE:
            continue
        source, target = prev_project / name, project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink() or source.is_file():
            shutil.copy2(source, target, follow_symlinks=False)
        elif source.is_dir():
            shutil.copytree(source, target, symlinks=True, ignore=ignore, dirs_exist_ok=True)
        copied.append(name)
    return copied


def _archive_previous_runs(prev_project: Path, project: Path, step: int) -> list[str]:
    """The previous step's runner runs (artifacts, plans, ledgers) where the kit itself keeps finished runs of a
    removed worktree (`<git-common-dir>/agent-flow/archive/worktrees/`), without their active markers, plus the
    archive the previous step carried. No live worktree state is copied, so the new run starts fresh."""
    prev_state, state = _common_dir(prev_project) / "agent-flow", project / ".git" / "agent-flow"
    if (prev_state / "archive").is_dir():
        shutil.copytree(prev_state / "archive", state / "archive", symlinks=True, dirs_exist_ok=True)
    sources = [(path.name, path / ".agent-flow" / "runs") for path in sorted((prev_state / "worktrees").glob("*"))]
    sources.append(("leader", prev_project / ".agent-flow" / "runs"))
    archived = []
    for name, runs in sources:
        for run in sorted(runs.glob("*")) if runs.is_dir() else []:
            if not run.is_dir():
                continue
            target = state / "archive" / "worktrees" / f"{name}-step{step - 1}" / ".agent-flow" / "runs" / run.name
            shutil.copytree(run, target, symlinks=True)
            for marker in ("active", "active.lock"):
                (target / marker).unlink(missing_ok=True)
            archived.append(target.relative_to(state).as_posix())
    return archived


def _drop_gate_hooks(project: Path) -> None:
    """Removes the previous step's verify-gate Stop hook; `_install_verify_gate` then adds this step's."""
    path = project / GATE_SETTINGS
    if not path.is_file():
        return
    settings = json.loads(path.read_text(encoding="utf-8"))
    stops = [entry for entry in (settings.get("hooks") or {}).get("Stop") or []
             if not any("verify_gate.py" in str(hook.get("command")) for hook in entry.get("hooks") or [])]
    if stops:
        settings["hooks"]["Stop"] = stops
    elif "Stop" in (settings.get("hooks") or {}):
        del settings["hooks"]["Stop"]
    path.write_text(json.dumps(settings, indent=1) + "\n", encoding="utf-8")


def prepare_chain_next(prev_run: Path, case_dir: Path, output: Path, layer: str | None = None) -> dict:
    """The next step of a chain: the previous run's arm, unchanged, on the previous run's code, in a new session.

    The project's `main` is the previous checkout's HEAD (`chain.prev_head`, history kept), plus its uncommitted work
    as one commit, plus the step's task files (`select_layer`, as `prepare`) as the seed commit. A workflow arm keeps
    the kit state it had (reinstalled from the same kit copy so its hook paths name the new project) and its earlier
    runner runs in the kit's archive; arm V/VK gets this step's gate; a layer with `user_sim` gets this step's
    simulator. Arm R reuses the previous step's frozen decision (`router.reused`, no router call, no router cost).
    `drive` and `score` work on the new run as on any external-case run.
    """
    prev_run, output = prev_run.resolve(), output.resolve()
    prev, prev_result, prev_drive = _chain_previous(prev_run)
    prev_project = prev_run / "project" if (prev_run / "project").is_dir() else Path(prev["project"])
    arm = effective_arm(prev)
    checkout, chosen = select_checkout(prev_project, arm)
    if chosen["checkout_error"]:
        raise SystemExit(f"the previous run has no gradable checkout: {chosen['checkout_error']}")
    if prev_result.get("artifact_sha256") and artifact_sha256(checkout) != prev_result["artifact_sha256"]:
        raise SystemExit("the previous run's checkout changed after it was scored; score it again first")
    case = load_external_case(case_dir, seeded=False)
    selected = select_layer(case, case_dir, layer)
    case = {**case, "files": selected["files"]}
    chained = prev.get("chain") or {}
    step = int(chained.get("step", 1)) + 1
    output.mkdir(parents=True)
    agent_kit = output / "kit"
    shutil.copytree(prev_run / "kit", agent_kit, symlinks=True)
    project = output / "project"
    prev_head = _start_from_previous(checkout, project)
    carried = {"uncommitted_carried": _carry_uncommitted(checkout, project)}
    for target, source in case["files"].items():
        (project / target).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(case_dir / source, project / target)
    _git(["add", "--force", *case["files"]], project)
    _git(["commit", "-qm", CHAIN_SEED.format(step=step), "--allow-empty"], project)
    simulated = _install_user_sim(output, selected["oracle"], prev_drive.get("model") or DEFAULT_MODEL)
    task = with_ask_user(case["task"], simulated["ask_user"] if simulated else None)
    routed = {}
    if prev["arm"] in ROUTED_ARMS:
        decision = prev["router"]
        routed = {"effective_arm": arm, "router_frozen_at": prev.get("router_frozen_at"),
                  "router": {**decision, "cost_usd": 0.0, "reused": True, "reused_from": str(prev_run),
                             "original_cost_usd": decision.get("cost_usd")}}
    searched, gate, skills = None, None, None
    if arm in BARE_ARMS:
        if arm in GATE_ARMS:
            _drop_gate_hooks(project)
            gate = _install_verify_gate(output, project, case["task"], sorted(case["files"]), case, "external")
        if arm in SKILL_ARMS:
            skills = prev.get("skills")  # committed in the previous step's history, as the arm left them
        _git(["checkout", "-qb", BRANCH], project)
        prompt = external_bare_prompt({**case, "task": task})
    else:
        carried["kit_state_carried"] = _carry_kit_state(prev_project, project)
        _install(agent_kit, project, {"profile": case["profile"], "modes": {"clean": {"install": case["install"]}}},
                 hooks=True)
        carried["archived_runs"] = _archive_previous_runs(prev_project, project, step)
        spec = (prev["router"].get("variant") if prev["arm"] in ROUTED_ARMS
                else (prev.get("variant") or {}).get("spec")) if arm == "S" else None
        searched = _write_variant(agent_kit, project, case["profile"], spec)
        prompt = driver_prompt(task, searched["name"] if searched else WORKFLOWS[arm], ask_user=simulated is not None)
    (output / "prompt.md").write_text(prompt, encoding="utf-8")
    prior = [*chained.get("prior_runs", []), str(prev_run)]
    meta = {"arm": prev["arm"], **routed, "case": case["name"], "trial": prev.get("trial"),
            "project": str(project.resolve()), "start_commit": _git(["rev-parse", "HEAD"], project),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(), "kit_commit": prev.get("kit_commit"),
            "cprime_rules": None, "isolation": prev.get("isolation", "instructed"),
            "principles": principles_record(arm), **_workflow_record(arm, searched),
            "approval_rules": HARNESS_APPROVES if arm in WORKFLOWS else None, "verify_gate": gate,
            **({"skills": skills} if skills is not None else {}),
            "case_kind": "external", "case_sha256": case["sha256"], "seed_commit": None,
            "score_keys": case["score_keys"], "evaluator_repeat": bool(case.get("evaluator_repeat")),
            **({"layer": selected["layer"]} if selected["layer"] is not None else {}),
            **({"user_sim": simulated} if simulated is not None else {}),
            "chain": {"step": step, "prev_run": str(prev_run), "prev_head": prev_head,
                      "root_run": chained.get("root_run", str(prev_run)), "prior_runs": prior,
                      "prev_case": prev.get("case"), "prev_artifact_sha256": prev_result.get("artifact_sha256"),
                      "model": prev_drive.get("model"), "effort": prev_drive.get("effort"), **carried}}
    (output / "arm.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    return meta


def chain_reads(rows: list[dict], meta: dict) -> int:
    """Tool calls that name an earlier step's grading output (its result or the evaluator's report)."""
    markers = [str(Path(run) / name) for run in (meta.get("chain") or {}).get("prior_runs", [])
               for name in ("result.json", "ui-score.json")]
    return sum(any(marker in item for marker in markers) for item in tool_inputs(rows))


def ui_bare_prompt(task: str) -> str:
    return f"""# Task

{task}

The project has no tests. The scorer starts the build through `eval.json` and drives it through the contract.

{DECLARATION}"""


def _write_variant(kit: Path, project: Path, profile: str, variant: dict | None) -> dict | None:
    """Arm S: the search variant written into the installed project (gitignored `.agent-flow/`, so no commit)."""
    return variants.write(kit, project, profile, variant) if variant is not None else None


def effective_arm(meta: dict) -> str | None:
    """The arm whose setup the run used: the router's pick for arm R, the arm itself otherwise."""
    return meta.get("effective_arm") or meta.get("arm")


def _route(output: Path, project: Path, task: str, model: str, menu_variants: list[dict] | None,
           frozen: dict, kit: Path, profile: str) -> dict:
    """Arm R's decision, written to `arm.json` before the chosen arm's preparation touches the project.

    A `compose` spec is tried on the run's kit and profile first (the workflow and the reviewer override), so a spec
    that would fail in `variants.write` is a bad answer, retried and then replaced by `none`, not a crash."""
    def check_spec(spec: dict) -> None:
        variants.workflow(kit, spec)
        variants.profile_override(kit, profile, spec)

    decision = router.route(task, project, output / router.OUT_DIR, model, controller_env(dict(os.environ)),
                            menu_variants, check_spec)
    if _git(["status", "--porcelain"], project):
        raise SystemExit("the router changed the project")
    frozen = {**frozen, "effective_arm": decision["effective_arm"], "router": decision,
              "router_frozen_at": datetime.now().isoformat(timespec="seconds")}
    (output / "arm.json").write_text(json.dumps(frozen, indent=1) + "\n", encoding="utf-8")
    return frozen


def _workflow_record(arm: str, searched: dict | None) -> dict:
    if arm not in WORKFLOWS:
        return {}
    return {"workflow": searched["name"] if searched else WORKFLOWS[arm], **({"variant": searched} if searched else {})}


def prepare(kit: Path, arm: str, case_name: str, trial: int, output: Path, isolation: str = "instructed",
            case_dir: Path | None = None, variant: dict | None = None, menu_variants: list[dict] | None = None,
            model: str = DEFAULT_MODEL, layer: str | None = None) -> dict:
    """`model` is arm R's router model; `drive` must run the controller on the same one. `layer` picks one of an
    external case's layers (`select_layer`)."""
    if isolation not in ISOLATION:
        raise ValueError(f"isolation must be one of {ISOLATION}")
    if (arm == "S") != (variant is not None):
        raise ValueError("arm S needs --variant, and only arm S takes one")
    if menu_variants and arm not in ROUTED_ARMS:
        raise ValueError("only arm R takes --menu-variant")
    if arm in ROUTED_ARMS:
        router.menu(menu_variants)  # a bad menu variant fails before anything is seeded
    if case_dir is not None:
        return _prepare_external(kit, arm, load_external_case(case_dir), case_dir, trial, output, isolation, variant,
                                 menu_variants, model, layer)
    if layer is not None:
        raise ValueError("--layer applies to an external case (--case-dir) with layers")
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
    routed, requested = {}, arm
    if arm in ROUTED_ARMS:
        routed = _route(output, project, case["task"], model, menu_variants,
                        {"arm": arm, "case": case_name, "trial": trial, "project": str(project.resolve())},
                        agent_kit, case["profile"])
        arm, variant = routed["effective_arm"], routed["router"].get("variant")
    searched, gate, skills = None, None, None
    if arm in BARE_ARMS:
        if arm in PRINCIPLE_SETS:
            _write_principles(project, arm)
        if arm in GATE_ARMS:
            gate = _install_verify_gate(output, project, case["task"], _ui_documents(ui) if ui else [], case,
                                        "ui" if ui else "phase-case")
        if arm in SKILL_ARMS:
            skills = _install_skills(agent_kit, project)
        _git(["checkout", "-qb", BRANCH], project)
        prompt = ui_bare_prompt(case["task"]) if ui else bare_prompt(case["task"], case["test_command"])
    elif arm == "Cp":
        _install(agent_kit, project, case, hooks=False)
        _git(["checkout", "-qb", BRANCH], project)
        prompt = c_prime.build(agent_kit, project, case["task"], "commit")
    else:
        _install(agent_kit, project, case, hooks=True)
        searched = _write_variant(agent_kit, project, case["profile"], variant)
        prompt = driver_prompt(case["task"], searched["name"] if searched else WORKFLOWS[arm])
    (output / "prompt.md").write_text(prompt, encoding="utf-8")
    meta = {"arm": requested, **routed, "case": case_name, "trial": trial, "project": str(project.resolve()),
            "start_commit": _git(["rev-parse", "HEAD"], project),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "kit_commit": c_prime._sh(["git", "rev-parse", "HEAD"], kit).stdout.strip() or None,
            "cprime_rules": c_prime.RULES_VERSION if arm == "Cp" else None, "isolation": isolation,
            "principles": principles_record(arm), **_workflow_record(arm, searched),
            "approval_rules": HARNESS_APPROVES if arm in WORKFLOWS else None, "verify_gate": gate,
            **({"skills": skills} if skills is not None else {}),
            "case_kind": "ui" if ui else "phase-case", **({"ui_task": ui["task"], "layer": ui["layer"]} if ui else {})}
    (output / "arm.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    return meta


def _install_user_sim(output: Path, oracle: Path | None, model: str) -> dict | None:
    """The `ask-user` script and its oracle copy in `<run>/user-sim/`, outside the project; None without an oracle."""
    if oracle is None:
        return None
    return user_sim.install(output, oracle, {"keys": list(CONTROLLER_ENV), "set": controller_env({})}, model)


def _prepare_external(kit: Path, arm: str, case: dict, case_dir: Path, trial: int, output: Path,
                      isolation: str, variant: dict | None = None, menu_variants: list[dict] | None = None,
                      model: str = DEFAULT_MODEL, layer: str | None = None) -> dict:
    if arm not in (*BARE_ARMS, *WORKFLOWS, *ROUTED_ARMS):
        raise ValueError("an external case defines the A, AP, PS, V, VK, B, C, D, S and R arms only")
    selected = select_layer(case, case_dir, layer)
    case = {**case, "files": selected["files"]}
    output.mkdir(parents=True)
    agent_kit = sanitized_kit(kit, output / "kit")
    project = output / "project"
    _seed_external_project(case, case_dir, project)
    simulated = _install_user_sim(output, selected["oracle"], model)
    task = with_ask_user(case["task"], simulated["ask_user"] if simulated else None)
    routed, requested = {}, arm
    if arm in ROUTED_ARMS:
        routed = _route(output, project, task, model, menu_variants,
                        {"arm": arm, "case": case["name"], "trial": trial, "project": str(project.resolve())},
                        agent_kit, case["profile"])
        arm, variant = routed["effective_arm"], routed["router"].get("variant")
    searched, gate, skills = None, None, None
    if arm in BARE_ARMS:
        if arm in PRINCIPLE_SETS:
            _write_principles(project, arm)
        if arm in GATE_ARMS:
            gate = _install_verify_gate(output, project, case["task"], sorted(case["files"]), case, "external")
        if arm in SKILL_ARMS:
            skills = _install_skills(agent_kit, project)
        _git(["checkout", "-qb", BRANCH], project)
        prompt = external_bare_prompt({**case, "task": task})
    else:
        _install(agent_kit, project, {"profile": case["profile"], "modes": {"clean": {"install": case["install"]}}},
                 hooks=True)
        searched = _write_variant(agent_kit, project, case["profile"], variant)
        prompt = driver_prompt(task, searched["name"] if searched else WORKFLOWS[arm], ask_user=simulated is not None)
    (output / "prompt.md").write_text(prompt, encoding="utf-8")
    meta = {"arm": requested, **routed, "case": case["name"], "trial": trial, "project": str(project.resolve()),
            "start_commit": _git(["rev-parse", "HEAD"], project),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "kit_commit": c_prime._sh(["git", "rev-parse", "HEAD"], kit).stdout.strip() or None,
            "cprime_rules": None, "isolation": isolation,
            "principles": principles_record(arm), **_workflow_record(arm, searched),
            "approval_rules": HARNESS_APPROVES if arm in WORKFLOWS else None, "verify_gate": gate,
            **({"skills": skills} if skills is not None else {}),
            "case_kind": "external",
            "case_sha256": case["sha256"], "seed_commit": case["seed"]["commit"], "score_keys": case["score_keys"],
            "evaluator_repeat": bool(case.get("evaluator_repeat")),
            **({"layer": selected["layer"]} if selected["layer"] is not None else {}),
            **({"user_sim": simulated} if simulated is not None else {})}
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


def shell_commands(rows: list[dict]) -> list[str]:
    """The `command` of every Bash tool call: the only inputs that execute anything."""
    commands = []
    for row in rows:
        message = row.get("message")
        if row.get("type") != "assistant" or not isinstance(message, dict):
            continue
        for block in message.get("content") or []:
            if (isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Bash"
                    and isinstance((block.get("input") or {}).get("command"), str)):
                commands.append(block["input"]["command"])
    return commands


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


BYTES_PER_TOKEN = 4  # a generous bytes-per-token ratio, so the token figure below stays a lower bound


def reviewer_record(project: Path, checkout: Path) -> dict:
    """What the runner's reviewer subprocesses cost, as far as it can be seen (#409 program v2.2).

    The runner starts each reviewer with `--no-session-persistence` (claude) or `--ephemeral` (codex) inside a sandbox
    whose scratch is deleted afterwards, so no reviewer usage survives. `review-metrics-*.json` keeps, per job, the
    provider, model, effort, status, seconds and the prompt and output sizes. From those this reports a **lower bound**:
    the prompt and output bytes of jobs that finished `ok` / BYTES_PER_TOKEN (a failed job may never have reached the
    model, so its prompt is kept apart as `attempted_prompt_bytes`), with every file a reviewer read on its own left
    out. A cost is given only for a claude job whose model the runner recorded and is priced here; `unspecified` is the
    CLI's own default, which is not necessarily the controller's model, so it stays unpriced, as do codex jobs.
    """
    roots = [project / ".git" / "agent-flow", checkout / ".agent-flow" / "runs"]
    files = sorted({path for root in roots if root.is_dir() for path in root.rglob("review-metrics-*.json")})
    providers: dict[str, dict] = {}
    for path in files:
        try:
            jobs = json.loads(path.read_text(encoding="utf-8")).get("jobs") or []
        except (OSError, ValueError):
            continue
        for job in jobs:
            if not isinstance(job, dict):
                continue
            name = str(job.get("provider") or "unknown")
            item = providers.setdefault(name, {"jobs": 0, "failed": 0, "seconds": 0.0, "prompt_bytes": 0,
                                               "output_bytes": 0, "attempted_prompt_bytes": 0, "models": set(),
                                               "efforts": set()})
            ok = job.get("status") == "ok"
            item["jobs"] += 1
            item["failed"] += not ok
            item["seconds"] += float(job.get("duration_s") or 0)
            item["attempted_prompt_bytes"] += int(job.get("prompt_utf8_bytes") or 0)
            if ok:
                item["prompt_bytes"] += int(job.get("prompt_utf8_bytes") or 0)
                item["output_bytes"] += int(job.get("stdout_utf8_bytes") or 0)
            item["models"].add(str(job.get("model") or "unspecified"))
            item["efforts"].add(str(job.get("effort") or "unspecified"))
    cost = 0.0
    priced = bool(providers)
    for name, item in providers.items():
        item["token_lower_bound"] = (item["prompt_bytes"] + item["output_bytes"]) // BYTES_PER_TOKEN
        model = next(iter(item["models"]), None) if len(item["models"]) == 1 else None
        price = PRICES.get(model or "") if name == "claude" else None
        if price is None:
            item["cost_lower_bound_usd"] = None
            priced = False
        else:
            item["cost_lower_bound_usd"] = round((item["prompt_bytes"] / BYTES_PER_TOKEN * price["fresh_input"]
                                                  + item["output_bytes"] / BYTES_PER_TOKEN * price["output"])
                                                 / 1_000_000, 4)
            cost += item["cost_lower_bound_usd"]
        item["models"], item["efforts"] = sorted(item["models"]), sorted(item["efforts"])
        item["seconds"] = round(item["seconds"])
    return {"status": "lower-bound", "waves": len(files), "jobs": sum(item["jobs"] for item in providers.values()),
            "providers": providers,
            "cost_lower_bound_usd": round(cost, 4) if priced else None,
            "priced_cost_lower_bound_usd": round(cost, 4)}


def preflight(models: list[tuple[str, str]], timeout: int = 180) -> list[dict]:
    """Calls each (cli, model) once with a one-word prompt in the eval environment and records whether it answered.

    A model a planned arm needs must pass here before the arm runs; a failure is reported, never replaced by another
    model.
    """
    rows = []
    for cli, model in models:
        # `default` checks the CLI's own default model (what a reviewer with model `unspecified` gets).
        pick = [] if model == "default" else (["--model", model] if cli == "claude" else ["-m", model])
        argv = (["claude", "-p", *pick, "Reply with the single word OK."] if cli == "claude" else
                ["codex", "exec", "--skip-git-repo-check", *pick, "Reply with the single word OK."])
        started = time.monotonic()
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                                  env=controller_env(dict(os.environ)), cwd=tempfile.gettempdir())
            answered = done.returncode == 0 and "OK" in done.stdout.upper()
            error = None if answered else (done.stderr or done.stdout)[-300:]
        except (OSError, subprocess.TimeoutExpired) as exc:
            answered, error = False, str(exc)[-300:]
        rows.append({"cli": cli, "model": model, "available": answered, "seconds": round(time.monotonic() - started, 1),
                     "error": error, "checked_at": datetime.now().isoformat(timespec="seconds")})
    return rows


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
    if effective_arm(row) not in WORKFLOWS:
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
    arm = effective_arm(meta)
    checkout, chosen = select_checkout(project, arm)
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
    simulated = user_sim.summary(run / user_sim.LOG) if meta.get("user_sim") else None
    if simulated is not None:
        simulated["oracle_reads"] = oracle_reads(rows, run)
        graded["contaminated"] = graded["contaminated"] or bool(simulated["oracle_reads"])
    if meta.get("chain"):
        graded["chain_reads"] = chain_reads(rows, meta)
        graded["contaminated"] = graded["contaminated"] or bool(graded["chain_reads"])
    message = final_message(main_rows)
    declaration = completion.classify(message if main_rows else None)
    drive = json.loads((run / "drive.json").read_text(encoding="utf-8")) if (run / "drive.json").is_file() else {}
    gate = gate_result(run, meta, checkout) if arm in GATE_ARMS else None
    routed = meta.get("router")
    result = {"isolation": "instructed", **meta, "task_kind": "normal", "checkout": str(checkout), **graded,
              "commits": int(commits) if commits.isdigit() else None,
              "final_message": message, "declaration": declaration,
              "integrity": integrity.check(checkout, meta["start_commit"], shell_commands(rows), message, declaration),
              "spec_self_approvals": sum(bool(SPEC_SELF_APPROVAL.search(item)) for item in tool_inputs(rows)),
              "phase_self_approvals": sum(bool(PHASE_SELF_APPROVAL.search(item)) for item in tool_inputs(rows)),
              "verify_downgrades": verify_downgrades(rows), **skills_invoked(rows),
              "manual_spec_approvals": manual_spec_approvals(project, checkout), "artifact_sha256": artifact,
              "transcript": str(transcript) if transcript else None, **_measured_usage(rows, read), **read,
              "controller_cost_usd": drive.get("cost_usd"),
              "total_cost_usd": total_cost(drive.get("cost_usd"), gate, routed,
                                           simulated["user_sim_cost_usd"] if simulated else 0.0),
              **(simulated or {}), **({"grill_checks": drive["grill_checks"]} if "grill_checks" in drive else {}),
              **({"router_cost_usd": routed.get("cost_usd")} if routed else {}),
              **({"verify_gate": gate} if gate is not None else {}), "drive_exit": drive.get("exit"),
              "drive_timed_out": drive.get("timed_out"), "drive_is_error": drive.get("is_error"),
              "fix_loop_reapprovals": len(drive.get("fix_loop_reapprovals") or []),
              "fix_loop_reapproval_log": drive.get("fix_loop_reapprovals") or [],
              "harness_error": harness_error, **chosen,
              **(_runner_trace(project, checkout) if arm in WORKFLOWS else {}),
              **(gate_record(project, checkout) if arm in WORKFLOWS else {}),
              **({"reviewers": reviewer_record(project, checkout)}
                 if arm in WORKFLOWS else {}),
              **({"phase_trace": phase_trace(project, checkout, meta.get("workflow") or WORKFLOWS[arm], rows,
                                             measured=read.get("subagents") != "unmeasured")}
                 if arm in WORKFLOWS else {}),
              "scored_at": datetime.now().isoformat(timespec="seconds")}
    (run / "result.json").write_text(json.dumps(result, indent=1, default=str) + "\n", encoding="utf-8")
    return result


def total_cost(controller: float | None, gate: dict | None, routed: dict | None = None,
               simulator: float | None = 0.0) -> float | None:
    """The controller's cost plus arm V's reviewer cost, arm R's router cost and the simulated user's cost; null when
    any of them is unknown."""
    reviewer = gate["reviewer_cost_usd"] if gate is not None else 0.0
    chooser = routed.get("cost_usd") if routed else 0.0
    parts = (controller, reviewer, chooser, simulator)
    return None if None in parts else round(sum(parts), 4)


def oracle_reads(rows: list[dict], run: Path) -> int:
    """Tool calls that name the simulator's oracle copy or config. Running `ask-user` itself names neither."""
    target = (run / user_sim.DIR).resolve()
    markers = (str(target / user_sim.ORACLE), str(target / user_sim.CONFIG), f"{user_sim.DIR}/{user_sim.ORACLE}",
               f"{user_sim.DIR}/{user_sim.CONFIG}", f"{user_sim.DIR}/user_sim.py")
    return sum(any(marker in item for marker in markers) for item in tool_inputs(rows))


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


def phase_artifact(worktree: Path, status: dict, phase: str) -> Path | None:
    """`<git-common-dir>/agent-flow/worktrees/<name>/.agent-flow/runs/<run>/artifacts/<phase>.md`, where the runner
    keeps the run's artifacts."""
    run_id = str(status.get("run") or "").rsplit("/", 1)[-1]
    common = _common_dir(worktree)
    return (common / "agent-flow" / "worktrees" / worktree.name / ".agent-flow" / "runs" / run_id / "artifacts"
            / f"{phase}.md") if common and run_id else None


def review_findings(worktree: Path, status: dict, limit: int = 12) -> dict:
    """What the phase artifact still asks for when the run stops at the cap: verdict counts and the open findings."""
    artifact = phase_artifact(worktree, status, status["fix_loop_cap"]["phase"])
    if artifact is None or not artifact.is_file():
        return {"artifact": None, "verdicts": {}, "findings": []}
    text = artifact.read_text(encoding="utf-8", errors="replace")
    verdicts: dict[str, int] = {}
    for verdict in re.findall(r"^verdict:\s*(\S+)", text, re.M):
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
    findings = [line.strip()[2:] for line in text.splitlines() if re.match(r"^\s*- (high|med|medium|low|critical):", line)]
    return {"artifact": hashlib.sha256(text.encode("utf-8")).hexdigest(), "verdicts": verdicts,
            "findings": findings[:limit], "findings_total": len(findings)}


# domain-grill의 사용자 라운드(#409 grill 실험). 시뮬레이터가 있는 run에서 harness는 grill을 승인하기 전에, artifact의
# `## Decision Sources`에서 `user round <n>`이 출처인 결정의 라운드 수가 `ask-user` 호출 수를 넘지 않는지 본다. 넘으면
# controller가 사용자 대신 답한 것이므로 한 번 되돌려 보내고, 그래도 넘으면 승인하되 기록에 남긴다.
# 한계: 라운드와 호출을 수로만 맞춘다. 어떤 답이 어떤 결정이 됐는지, 출처를 `task`로 적은 결정이 정말 과제에 있었는지는
# 보지 않는다.
GRILL_PHASE = "domain-grill"
USER_ROUND = re.compile(r"\buser round\s*(\d+)", re.I)
MAX_GRILL_RECHECKS = 1
RESUME_AFTER_GRILL_CHECK = (
    "The evaluator, as the user, has not approved `domain-grill`: domain-grill.md records {rounds} user round(s) under "
    "`## Decision Sources`, but the product owner was asked {calls} time(s). Ask the product owner every question of "
    "those rounds with `{script}`, update domain-grill.md from their answers, then end your turn for approval.")


def grill_check(worktree: Path, status: dict, log: Path) -> dict:
    """Whether every `user round <n>` decision in the grill artifact can have come through `ask-user`."""
    artifact = phase_artifact(worktree, status, GRILL_PHASE)
    calls = user_sim.summary(log)["ask_user_calls"]
    if artifact is None or not artifact.is_file():
        return {"artifact": None, "decisions": None, "user_round_decisions": None, "user_rounds": None,
                "ask_user_calls": calls, "passed": None}
    text = artifact.read_text(encoding="utf-8", errors="replace")
    section = re.search(r"^## Decision Sources\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    lines = [line.strip() for line in (section.group(1) if section else "").splitlines()
             if line.strip().startswith("- ")]
    rounds = [match for match in (USER_ROUND.search(line.rsplit(":", 1)[-1]) for line in lines) if match]
    distinct = len({int(match.group(1)) for match in rounds})
    return {"artifact": hashlib.sha256(text.encode("utf-8")).hexdigest(), "decisions": len(lines),
            "user_round_decisions": len(rounds), "user_rounds": distinct, "ask_user_calls": calls,
            "passed": distinct <= calls}


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
    arm = effective_arm(meta)
    routed = meta.get("router")
    if routed and routed.get("model") != model:
        raise SystemExit(f"arm R's router ran on {routed.get('model')}; drive the controller on the same model")
    chained = meta.get("chain") or {}
    if chained.get("model") and chained["model"] != model:
        raise SystemExit(f"chain step {chained['step']} continues a run driven on {chained['model']}; "
                         "drive it on the same model")
    if chained and chained.get("effort") != os.environ.get("CLAUDE_CODE_EFFORT_LEVEL"):
        raise SystemExit(f"chain step {chained['step']} continues a run with CLAUDE_CODE_EFFORT_LEVEL="
                         f"{chained.get('effort')}; drive it with the same effort")
    if arm in GATE_ARMS:
        set_gate_model(run, model)
    simulated = meta.get("user_sim")
    if simulated:
        user_sim.set_model(run, model)
    grill_checks: list[dict] = []
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
            if timed_out or arm not in WORKFLOWS or len(approvals) >= MAX_HARNESS_APPROVALS:
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
            if not status or status.get("reason") != "phase_approval_required":
                break
            if simulated and status.get("current_phase") == GRILL_PHASE:
                check = grill_check(Path(status["worktree"]), status, run / user_sim.LOG)
                grill_checks.append(check)
                if check["passed"] is False and sum(item["passed"] is False for item in grill_checks) <= MAX_GRILL_RECHECKS:
                    message = RESUME_AFTER_GRILL_CHECK.format(rounds=check["user_rounds"], calls=check["ask_user_calls"],
                                                              script=simulated["ask_user"])
                    continue
            if not approve_phase(project, status):
                break
            approvals.append(str(status.get("current_phase")))
            message = RESUME_AFTER_APPROVAL.format(phase=status.get("current_phase"))
    transcript = session_transcript(session)
    summary.pop("final_text", None)
    record = {"model": model, "timeout_seconds": timeout, "budget_usd": budget, "exit": exit_code,
              "timed_out": timed_out, "seconds": round(time.monotonic() - started), **summary, "session_id": session,
              "cost_usd": spent or None, "harness_approvals": approvals,
              "fix_loop_reapprovals": reapprovals, "fix_loop_cap_stop": cap_stop,
              **({"grill_checks": grill_checks} if simulated else {}),
              **({"verify_gate": verify_gate.summary(run / verify_gate.LOG)} if arm in GATE_ARMS else {}),
              "transcript": str(transcript) if transcript else None,
              "argv_sha256": hashlib.sha256(json.dumps(controller_argv(prompt, model, budget)).encode()).hexdigest(),
              "cli_version": c_prime._sh(["claude", "--version"], run).stdout.strip() or None,
              "effort": os.environ.get("CLAUDE_CODE_EFFORT_LEVEL")}
    (run / "drive.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    return record


def set_gate_model(run: Path, model: str) -> None:
    """Arm V's reviewer runs on the controller's model, which only `drive` knows."""
    path = run / "gate" / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**config, "model": model}, indent=1) + "\n", encoding="utf-8")


def gate_result(run: Path, meta: dict, checkout: Path) -> dict:
    """What arm V's gate did, read from `verify-gate.jsonl`, and whether the agent left its hook wiring as installed."""
    settings = checkout / GATE_SETTINGS
    intact = settings.is_file() and verify_gate.file_sha256(settings) == meta["verify_gate"]["settings_sha256"]
    return {**verify_gate.summary(run / verify_gate.LOG), "wiring_intact": intact}


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
    """Every assigned run is in its arm's denominator; only harness errors are listed apart (with their reasons).

    Arm R gets one row over all its runs and one row per chosen option (`R: <option>`); the option rows have no
    Fisher p, because the router, not the assignment, put each run there. A "Router choices" table follows, and a
    "By layer" table when any run records a case layer or had a simulated user: one row per arm and layer (`-` for
    none), with the runs that asked, the `ask-user` calls and questions, and the simulator's cost.
    """
    assigned = [row for row in results if run_status(row) != "harness-error"]
    lines = ["| arm | assigned runs | success | one-sided Fisher p vs A | overclaim (upper 95%) | declared complete/blocked/"
             "undeclared/unmeasured | contaminated / infra error / timed out (counted as failures) | "
             "harness errors (excluded) | output tokens (mean) | input uncached+cached (mean) | "
             "seconds (mean) | controller cost / total incl. verify-gate reviewer and router (mean) | "
             "verify-gate blocks / review findings / reviewer cost (mean) | "
             "reviewer jobs (mean) | rule violations | isolation | "
             "runs that ran gates / deferred a gate to CI (never run) | "
             "successes first try / after fix-loop re-approval | runs stopped at the fix-loop limit | "
             "phases entered / declared (mean) | orchestration share of cost (mean) | "
             "integrity flags (runs flagged) | reviewer jobs / seconds / token and cost lower bound (mean) | "
             "skills invoked (runs that invoked any: name×calls) |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    base = [row for row in assigned if row["arm"] == "A"]
    reasons: list[str] = []
    for arm in ARMS:
        group = [row for row in assigned if row["arm"] == arm]
        excluded = [row for row in results if row["arm"] == arm and run_status(row) == "harness-error"]
        if not group and not excluded:
            continue
        reasons += [f"{arm}-{row.get('trial')}: {row['harness_error']}" for row in excluded]
        lines.append(_summary_line(arm, group, excluded, base, compare=arm != "A"))
        if arm in ROUTED_ARMS:
            for option in _router_options(group + excluded):
                lines.append(_summary_line(
                    f"{arm}: {option}", [row for row in group if _router_choice(row) == option],
                    [row for row in excluded if _router_choice(row) == option], base, compare=False))
    if reasons:
        lines += ["", "Harness errors (re-run under the fixed harness, not in the denominator):", ""]
        lines += [f"- {reason}" for reason in reasons]
    routed = [row for row in assigned if row["arm"] in ROUTED_ARMS]
    if routed:
        lines += ["", "Router choices (arm R, assigned runs):", "",
                  "| option | effective arm | runs | success | router fallbacks | router cost (mean) |",
                  "|---|---|---|---|---|---|"]
        for option in _router_options(routed, with_menu=True):
            group = [row for row in routed if _router_choice(row) == option]
            arms = sorted({str(effective_arm(row)) for row in group}) or [_menu_arm(routed, option)]
            costs = [row["router"].get("cost_usd") for row in group if row["router"].get("cost_usd") is not None]
            lines.append(f"| {option} | {'/'.join(arms)} | {len(group)} | {sum(_succeeded(row) for row in group)}/"
                         f"{len(group)} | {sum(bool(row['router'].get('fallback')) for row in group)} | "
                         f"{_usd(_mean(costs))} |")
    layered = [row for row in assigned if row.get("layer") is not None or "ask_user_calls" in row]
    if layered:
        lines += ["", "By layer (assigned runs):", "",
                  "| arm | layer | runs | success | simulated user: runs that asked / ask-user calls / questions / "
                  "cost (mean) | total cost (mean) |", "|---|---|---|---|---|---|"]
        for arm in ARMS:
            for layer in sorted({str(row.get("layer") or "-") for row in layered if row["arm"] == arm}):
                group = [row for row in layered if row["arm"] == arm and str(row.get("layer") or "-") == layer]
                total = _mean([row["total_cost_usd"] for row in group if row.get("total_cost_usd") is not None])
                lines.append(f"| {arm} | {layer} | {len(group)} | {sum(_succeeded(row) for row in group)}/{len(group)}"
                             f" | {_ask_cell(group)} | {_usd(total)} |")
    return "\n".join(lines + _chain_lines(results)) + "\n"


def _run_dirs(row: dict) -> set[str]:
    """The run directory a result belongs to, as recorded and, for a moved run, as scored."""
    return {str(Path(row[key]).parent) for key in ("project", "project_recorded") if row.get(key)}


def chain_survived(steps: dict[int, dict]) -> int:
    """Consecutive steps from step 1 with S=1 in an ordinary run (not contaminated, errored or timed out)."""
    survived = 0
    while (row := steps.get(survived + 1)) is not None and row.get("oracle") and run_status(row) == "ok":
        survived += 1
    return survived


def _chain_lines(results: list[dict]) -> list[str]:
    """One row per chain (`chain.root_run`): each step's S and G, and the steps survived. Step 1 is the result whose
    run directory is the root; a step whose result was not given shows `missing` and ends the survived count."""
    chained = [row for row in results if row.get("chain")]
    if not chained:
        return []
    lines = ["", "Chains (the same arm, each step a new session on its own previous result):", "",
             "| root run | arm | steps | per-step S / G | steps survived |", "|---|---|---|---|---|"]
    for root in sorted({row["chain"]["root_run"] for row in chained}):
        steps = {int(row["chain"]["step"]): row for row in chained if row["chain"]["root_run"] == root}
        first = next((row for row in results if not row.get("chain") and root in _run_dirs(row)), None)
        if first is not None:
            steps[1] = first
        cells = []
        for number in range(1, max(steps) + 1):
            row = steps.get(number)
            if row is None:
                cells.append(f"{number}: missing")
                continue
            status = run_status(row)
            g = (row.get("ui") or {}).get("G")
            cells.append(f"{number}: S={int(bool(row.get('oracle')))} G={'-' if g is None else f'{g:g}'}"
                         + ("" if status == "ok" else f" ({status})"))
        sample = steps.get(1) or next(iter(steps.values()))
        arm = str(sample["arm"]) + (f"→{effective_arm(sample)}" if effective_arm(sample) != sample["arm"] else "")
        lines.append(f"| {Path(root).name} | {arm} | {max(steps)} | {'; '.join(cells)} | "
                     f"{chain_survived(steps)}/{max(steps)} |")
    return lines


def _router_choice(row: dict) -> str | None:
    return (row.get("router") or {}).get("choice")


def _router_options(rows: list[dict], with_menu: bool = False) -> list[str]:
    """Chosen options in menu order (then any other chosen name); with_menu adds every option a menu offered."""
    order: list[str] = []
    for row in rows:
        for name in (row.get("router") or {}).get("menu") or []:
            if name not in order:
                order.append(name)
    chosen = {_router_choice(row) for row in rows} - {None}
    listed = [name for name in order if with_menu or name in chosen]
    return listed + sorted(chosen - set(listed))


def _menu_arm(rows: list[dict], option: str) -> str:
    """The arm an option stands for when no run chose it: `router.OPTIONS`, or S for a menu variant."""
    return router.OPTIONS[option][0] if option in router.OPTIONS else "S"


def _summary_line(label: str, group: list[dict], excluded: list[dict], base: list[dict], *, compare: bool) -> str:
    """One table row. A setup-specific column is filled when any run in the group used that setup."""
    setups = {effective_arm(row) for row in group}
    workflow = bool(setups & set(WORKFLOWS))
    gated = bool(setups & set(GATE_ARMS))
    item = completion.summarize(group).get((group[0]["arm"], "normal"), {}) if group else {}
    success = sum(_succeeded(row) for row in group)
    p = (f"{fisher_greater(sum(_succeeded(row) for row in base), len(base), success, len(group)):.3f}"
         if compare and base and group else "-")
    states = item.get("states", {})
    usage = [row["usage"] for row in group]
    upper = item.get("overclaim_upper95")
    return " | ".join([
        f"| {label}", str(len(group)), f"{success}/{len(group)}", p,
        f"{completion._cell(item.get('overclaim'))} ({completion._cell(upper)})",
        "/".join(str(states.get(state, 0)) for state in
                 (completion.COMPLETE, completion.BLOCKED, completion.UNDECLARED, completion.UNMEASURED)),
        " / ".join(str(sum(run_status(row) == status for row in group)) for status in STATUS_COLUMNS),
        str(len(excluded)), _fmt(_mean([u["output"] for u in usage if u["output"] is not None])),
        _fmt(_mean([u["uncached_input"] + u["cached_input"] for u in usage
                    if u["uncached_input"] is not None and u["cached_input"] is not None])),
        _fmt(_mean([row["seconds"] for row in group if row.get("seconds") is not None])),
        _cost_cell(group), _gate_cell(group) if gated else "-",
        _fmt(_mean([row["reviewer_jobs"] for row in group if "reviewer_jobs" in row])),
        str(sum(rule_violation(row) for row in group)),
        "/".join(sorted({row.get("isolation", "instructed") for row in group})),
        (f"{sum(bool(row.get('gate_ledgers')) for row in group)} / "
         f"{sum(bool(row.get('deferred_ci_checks')) for row in group)}" if workflow else "-"),
        (f"{sum(_succeeded(row) and not row.get('fix_loop_reapprovals') for row in group)} / "
         f"{sum(_succeeded(row) and bool(row.get('fix_loop_reapprovals')) for row in group)}"
         if workflow else "-"),
        (str(sum(bool(row.get("fix_loop_cap_stop")) for row in group)) if workflow else "-"),
        _phase_cell(group) if workflow else "-",
        _share_cell(group) if workflow else "-",
        _integrity_cell(group),
        _reviewer_cell(group) if workflow else "-", _skills_cell(group)]) + " |"


def _usd(value: float | None) -> str:
    return "-" if value is None else f"${value:.2f}"


def _cost_cell(group: list[dict]) -> str:
    controller = _mean([row["controller_cost_usd"] for row in group if row.get("controller_cost_usd") is not None])
    total = _mean([row["total_cost_usd"] for row in group if row.get("total_cost_usd") is not None])
    return f"{_usd(controller)} / {_usd(total)}"


def _gate_cell(group: list[dict]) -> str:
    gates = [row["verify_gate"] for row in group if isinstance(row.get("verify_gate"), dict)]
    if not gates:
        return "-"
    costs = [gate["reviewer_cost_usd"] for gate in gates if gate.get("reviewer_cost_usd") is not None]
    return (f"{_mean([gate['blocks'] for gate in gates]):.1f} / {_mean([gate['review_findings'] for gate in gates]):.1f}"
            f" / {_usd(_mean(costs))}")


def _reviewer_cell(group: list[dict]) -> str:
    records = [row["reviewers"] for row in group if isinstance(row.get("reviewers"), dict)]
    if not records:
        return "-"
    jobs = _mean([record["jobs"] for record in records])
    seconds = _mean([sum(item["seconds"] for item in record["providers"].values()) for record in records])
    tokens = _mean([sum(item["token_lower_bound"] for item in record["providers"].values()) for record in records])
    costs = [record["cost_lower_bound_usd"] for record in records]
    cost = "unpriced" if None in costs else f"≥${_mean(costs):.2f}"
    return f"{jobs:.1f} / {seconds:,.0f} / ≥{tokens:,.0f} tok, {cost}"


def _skills_cell(group: list[dict]) -> str:
    """`-` when no run recorded Skill calls (results scored before `skills_invoked` existed)."""
    recorded = [row["skills_invoked"] for row in group if isinstance(row.get("skills_invoked"), list)]
    if not recorded:
        return "-"
    counts: dict[str, int] = {}
    for names in recorded:
        for name in names:
            counts[name] = counts.get(name, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    listed = ", ".join(f"{name}×{count}" for name, count in ranked)
    return f"{sum(bool(names) for names in recorded)}/{len(recorded)}: {listed or 'none'}"


def _ask_cell(group: list[dict]) -> str:
    """`-` when no run in the group had a simulated user."""
    simulated = [row for row in group if "ask_user_calls" in row]
    if not simulated:
        return "-"
    costs = [row["user_sim_cost_usd"] for row in simulated if row.get("user_sim_cost_usd") is not None]
    return (f"{sum(bool(row.get('asked_any')) for row in simulated)}/{len(simulated)} / "
            f"{_fmt1(_mean([row['ask_user_calls'] for row in simulated]))} / "
            f"{_fmt1(_mean([row['ask_user_questions'] for row in simulated]))} / {_usd(_mean(costs))}")


def _fmt1(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def _integrity_cell(group: list[dict]) -> str:
    flags = [row["integrity"]["flags"] for row in group if isinstance(row.get("integrity"), dict)]
    return "-" if not flags else f"{sum(flags)} ({sum(bool(flag) for flag in flags)})"


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
    prep.add_argument("--variant", type=json.loads, help="arm S: the search variant as JSON (see evals/variants.py)")
    prep.add_argument("--menu-variant", type=json.loads, action="append", dest="menu_variants", metavar="SPEC",
                      help="arm R: an S variant spec (JSON) to add to the router's menu; repeat for each")
    prep.add_argument("--model", default=DEFAULT_MODEL,
                      help="arm R: the router's model; `drive --model` must be the same")
    prep.add_argument("--layer", help="an external case's layer (case.json `layers`), e.g. complete or underspec")
    dr = sub.add_parser("drive")
    dr.add_argument("run", type=Path)
    dr.add_argument("--model", default=DEFAULT_MODEL)
    dr.add_argument("--timeout", type=int, default=7200)
    dr.add_argument("--budget", type=float, default=30.0)
    sc = sub.add_parser("score")
    sc.add_argument("run", type=Path)
    sc.add_argument("--transcript", type=Path)
    sc.add_argument("--evaluator", type=Path, help="the private evaluator's score.py, for UI tasks")
    sc.add_argument("--repeat", type=int, default=3)
    sc.add_argument("--harness-error", metavar="REASON",
                    help="the harness itself broke this run; it is re-run and listed apart from the denominator")
    pre = sub.add_parser("preflight", help="check that each model an arm needs answers in this environment")
    pre.add_argument("--model", action="append", required=True, metavar="CLI:MODEL",
                     help="e.g. claude:claude-opus-5-5, codex:default (the CLI's own default); repeat for each")
    pre.add_argument("--output", type=Path)
    fol = sub.add_parser("followon", help="prepare a follow-on change on top of an arm run's code")
    fol.add_argument("base", type=Path, help="the arm run directory whose code the follow-on starts from")
    fol.add_argument("--followon-dir", type=Path, required=True, help="the follow-on case (`<dir>/case.json`)")
    fol.add_argument("--trial", type=int, default=1)
    fol.add_argument("--output", type=Path, required=True)
    nxt = sub.add_parser("chain-next", help="prepare the next chain step: the same arm on its own previous result")
    nxt.add_argument("prev", type=Path, help="the previous step's run directory (driven and scored)")
    nxt.add_argument("--case-dir", type=Path, required=True, help="the next step's case (`<dir>/case.json`)")
    nxt.add_argument("--layer", help="the next step case's layer (case.json `layers`)")
    nxt.add_argument("--output", type=Path, required=True)
    summ = sub.add_parser("summarize")
    summ.add_argument("results", type=Path, nargs="+")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        print(json.dumps(prepare(args.kit.resolve(), args.arm, args.case, args.trial, args.output.resolve(),
                                 args.isolation, args.case_dir.resolve() if args.case_dir else None,
                                 args.variant, args.menu_variants, args.model, args.layer), indent=1))
    elif args.command == "preflight":
        rows = preflight([tuple(item.split(":", 1)) for item in args.model])
        text = json.dumps(rows, indent=1)
        if args.output:
            args.output.write_text(text + "\n", encoding="utf-8")
        print(text)
        return 0 if all(row["available"] for row in rows) else 1
    elif args.command == "followon":
        print(json.dumps(prepare_followon(args.base.resolve(), args.followon_dir.resolve(), args.trial,
                                          args.output.resolve()), indent=1))
    elif args.command == "chain-next":
        print(json.dumps(prepare_chain_next(args.prev.resolve(), args.case_dir.resolve(), args.output.resolve(),
                                            args.layer), indent=1))
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
