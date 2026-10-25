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

Pre-registration v3.14 (the harness changes of the pre-flight audits):

- Run sandbox (`sandbox.py`): `drive` runs every controller turn and every harness call of the runner in a PID and
  mount namespace of its own: the host read-only, a private /tmp, the run's own HOME (`<run>/sandbox/home`, which holds
  the runner's worktrees and the CLI's transcripts), the private evaluator repository, the harness checkout and other
  runs hidden. What a turn leaves running dies with its namespace; a survivor, a sandbox setup error or a changed
  system directory (tripwire) is recorded (`sandbox`, `tripwire_changed`) and is a harness error or contamination.
  `ARM_COMPARE_SANDBOX=off` exists for the tests only.
- Attempts (`attempts.py`): `prepare --runs-root` names a run directory with a random id and keeps the assignment in a
  ledger beside it; a run is driven once, never moved (`check_not_moved`), and a run left out of the rates carries a
  code of the fixed exclusion taxonomy.
- Pins: the controller, the kit's reviewers (claude only, declared in the project's profile override), arm V's gate,
  arm R's router and the simulated user run on Opus 5.5 low; `drive` checks the CLI version first and every recorded
  call after (`verify_pins`). No self-update, no auto memory, one `--tools` list for every arm.
- Kit reviewer usage (`claude_usage.py`): a transparent `claude` first on the sandbox's PATH execs the real CLI for
  every call but the kit's reviewers, which it runs with `--output-format json`, printing the same text and sending a
  usage row to the run's `kit-reviewer-usage.jsonl`; the kit is unchanged.
- Budget and time: one ledger of every call (`budget_ledger`; null total while any part is unpriced), the cap checked
  between resumes and before the classifier, one deadline from the router to the snapshot; `end_to_end_seconds` and
  `controller_seconds` are reported apart.
- Graded tree (`snapshot.py`): `drive` freezes the working tree as the run left it (same rule for every arm) with a
  manifest; `score` and `chain-next` read only that snapshot, the submission runs in its own sandbox while the private
  evaluator grades it (`wrap_submission`), scoring takes an exclusive lock against drives, and evaluators get
  `--browser-repeat 2` and every log as `--scan`.
- Conditions: the case's notes are in every arm's task text, the declaration is one text, the harness polls with
  read-only `agent-flow status`, and arm V's unjudged reviews are `gate-error`s.
- Contamination rule (`attempts.contaminated`, from the result's `contamination_evidence`): in a run whose sandbox
  was healthy (host read-only inside, no hidden leaks) only a canary hit or the evaluator's `read` verdict makes it
  contaminated; hidden-path reads, oracle or chain reads, phase-case markers and the tripwire are diagnostics. A run
  without a healthy sandbox keeps the path-based rule (`contamination_rule` `content` / `path`).
- Metrics: content-based contamination (`scan_contamination`), completeness from the hidden checks for every arm,
  no-start, false complete beside false block, strata never pooled, the archive left out of chain metrics, steps
  survived by the success definition.

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
`orchestration_share` is deliver + outside over the total. Runner reviewer subprocesses are not in the controller's
transcript, so their tokens are in neither; `reviewer_jobs` counts them and `budget_ledger` prices them from the usage
wrapper's rows.

Success follows `completion.summarize` (#424): the hidden fix, plan and visible tests all pass (each ran at
least one test), the run did not declare `blocked`, and it broke no evaluation rule. A rule violation is a
human-only approval: a `spec approve`/`spec confirm` call or a `verify: manual` edit in the transcript, or a
manual SPEC approval the runner recorded on disk (the disk check does not depend on the transcript).
`artifact_sha256` fingerprints the checkout as the agent left it, before the hidden tests are copied in, so a
later re-score can tell whether it graded the same tree. `isolation` records whether keeping the agent away
from the hidden tests was only instructed (subagents on a shared filesystem) or enforced by a sandbox. Tokens
come from the agent's own transcript (`run_usage.claude_messages`) and its sub-agent transcripts
(`<session>/subagents/agent-*.jsonl`), which every transcript scan reads too; `transcript_cost_estimate_usd` prices
them at list price next to the CLI's own `controller_cost_usd`, so a gap shows usage the transcripts missed. Runner
reviewer subprocesses are not in it; `reviewer_jobs` counts them and the usage wrapper's rows price them.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import secrets
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
import attempts  # noqa: E402
import c_prime  # noqa: E402
import claude_usage  # noqa: E402
import completion  # noqa: E402
import sandbox  # noqa: E402
import snapshot  # noqa: E402
import variants  # noqa: E402
import integrity  # noqa: E402
import router  # noqa: E402
import outage  # noqa: E402
import user_sim  # noqa: E402
import verify_gate  # noqa: E402
import run_usage  # noqa: E402
import review_findings as finding_split  # noqa: E402 - the module; `review_findings` below is a function
from cycle_report import fisher_greater  # noqa: E402

CASES = HERE / "phase-cases"
UI_TASKS = HERE / "ui-tasks"
# 브라우저로 채점하는 과제. 평가기는 비공개 저장소에 있고 `score --evaluator`로 넘긴다(#428).
# devflow와 같은 Clean 아키텍처로 설치한다. 빈 프로젝트에 모드를 주지 않으면 `pending`으로 설치되고, run 도중에
# 모드를 고르면 runner가 새 run을 요구해 진행이 막힌다(OYG C-1 네 번째 시도, harness 오류).
# `canaries`: the private evaluator's canary for the task (the text exists only in its hidden files, #409 v3.14 C).
UI_CASES = {"oyg": {"task": "oy-global-fullstack", "layer": "prd", "profile": "node",
                    "install": ["--architecture-mode", "clean"], "canaries": ["OYG-CANARY-c899b7587bb72532bdcde083"]}}
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
# Pinned identity of every participating call (#409 v3.14 B): the controller, the kit's reviewers (claude only), arm V's
# gate reviewer, arm R's router and the simulated user run on this model and effort; the CLI is this version. `drive`
# stops before the controller starts when the CLI differs, and marks the run a harness error (`model-pin`) when any
# call's record shows another model or effort.
PINNED_MODEL = "claude-opus-5-5"
PINNED_EFFORT = "low"
PINNED_CLI_VERSION = "2.1.310"
# What the controller's environment always sets: no self-update, no auto memory, the kit's reviewers on claude only,
# and every model alias (sub-agents included) on the pinned model.
PINNED_ENV = {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "AGENT_FLOW_REVIEWERS": "claude",
              "CLAUDE_CODE_EFFORT_LEVEL": PINNED_EFFORT, "CLAUDE_CODE_SUBAGENT_MODEL": PINNED_MODEL,
              "ANTHROPIC_DEFAULT_OPUS_MODEL": PINNED_MODEL, "ANTHROPIC_DEFAULT_SONNET_MODEL": PINNED_MODEL,
              "ANTHROPIC_DEFAULT_HAIKU_MODEL": PINNED_MODEL}
# The kit's reviewers, declared in the project's profile override for every phase and angle.
REVIEWER_PIN = {"provider": "claude", "model": PINNED_MODEL, "effort": PINNED_EFFORT}
# Inside the run sandbox HOME is the run's own (`sandbox.HOME_TARGET`), so the runner's worktrees have the same path
# whether drive or the harness looks (`sandbox.to_host` maps them to `<run>/sandbox/home/...`).
AGENT_FLOW_WORKTREES = Path(sandbox.HOME_TARGET) / ".agent-flow" / "worktrees"
# One tool list for every arm (`--tools`, also pre-approved): files, shell, sub-agents, skills and the task list. No web,
# no scheduling, no worktree switching (it needs an approval a headless run cannot give).
CLAUDE_TOOLS = ("Bash", "Read", "Edit", "Write", "Glob", "Grep", "Task", "Skill", "TaskCreate", "TaskGet", "TaskList",
                "TaskUpdate", "TaskStop")
# The CLI's own skills, which every arm sees (Claude Code 2.1.310, HOME the run's own, project and local settings only).
# A project's `.claude/skills` add to them (the kit's for B/C/D/S/R, all kit skills for VK: their treatment). Any other
# difference is a harness error (`base-skills`).
BASE_SKILLS = ("batch", "claude-api", "code-review", "dataviz", "debug", "deep-research", "design", "design-sync",
               "doctor", "fewer-permission-prompts", "loop", "plugin-authoring", "run", "run-skill-generator",
               "schedule", "simplify", "update-config", "verify", "workflow-authoring")
# `off` only for tests that fake the CLI on the host; experiment runs need the sandbox (`required`).
SANDBOX_ENV = "ARM_COMPARE_SANDBOX"
# drive holds this lock shared, score exclusive: no run is graded while any drive is running (serial scoring).
LOCK_ENV = "ARM_COMPARE_LOCK"
DEFAULT_LOCK = Path(tempfile.gettempdir()) / "arm-compare.lock"
BROWSER_REPEAT = 2
# What the claude CLI reads to authenticate in this environment (the agent proxy supplies the rest), shown read-only
# inside the sandbox at the same path. Nothing else of the host's HOME or the remote session's state is.
AUTH_FILES_ENV = "ARM_COMPARE_AUTH_FILES"
AUTH_FILES = ("/home/claude/.claude/remote/.oauth_token", "/home/claude/.claude/remote/.api_key")


def auth_exposes() -> list[dict]:
    listed = os.environ.get(AUTH_FILES_ENV)
    names = [item for item in listed.split(":") if item] if listed is not None else list(AUTH_FILES)
    return [{"path": name, "mode": "ro"} for name in names if Path(name).is_file()]
ARMS = ("A", "AP", "PS", "V", "VK", "B", "C", "D", "S", "Cp", "R")
# R: the router picks one of these arms' setups (`router.OPTIONS`, plus S variants); the record keeps `arm: "R"` and the
# picked `effective_arm`, and everything that depends on the setup reads `effective_arm(meta)`.
ROUTED_ARMS = ("R",)
DEFAULT_MODEL = PINNED_MODEL
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

Declare `complete` only when the change is done and works; otherwise declare `blocked` with the reason. There is no
git remote: work that waits only for a push or a pull request counts as done.
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


def task_prompt(task: str) -> str:
    """The bare arms' prompt: the task text (which carries the case's notes) and the completion declaration."""
    return f"""# Task

{task}

{DECLARATION}"""


# What a case adds to its task for every arm (#409 v3.14 D): the phase case's test command and commit rule, the UI
# task's scorer note, an external case's `bare_note`. Bare and workflow arms get the same task text, and so does the
# runner (`agent-flow run <task>`), arm V's reviewer and arm R's router.
def phase_task(case: dict) -> str:
    return (f"{case['task']}\n\nThe project's tests run with `{' '.join(case['test_command'])}`. Commit your change "
            "on the current branch.")


UI_NOTE = "The project has no tests. The scorer starts the build through `eval.json` and drives it through the contract."


def ui_task(task: str) -> str:
    return f"{task}\n\n{UI_NOTE}"


def external_task(case: dict) -> str:
    note = (case.get("bare_note") or "").strip()
    return f"{case['task']}\n\n{note}" if note else case["task"]


def bare_prompt(task: str, test_command: list[str]) -> str:
    return task_prompt(phase_task({"task": task, "test_command": test_command}))


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


def _seed_external_project(case: dict, case_dir: Path, project: Path) -> dict:
    """An export of the seed repository's pinned commit as the project's first commit, then the task files in one commit.

    Only the pinned tree comes in (#409 v3.14 E): no history, tags, other refs or objects of the seed repository, so a
    later build kept in it is not reachable from the project. A later version of a task can start from an earlier
    version's reference build (`seed.patches`, `git format-patch` output), applied as commits on top.
    """
    seed = case["seed"]
    with tempfile.TemporaryDirectory(prefix="arm-seed-") as scratch:
        source = Path(scratch) / "source"
        c_prime._sh(["git", "clone", "-q", "--no-checkout", seed_repository(seed["repository"], case_dir),
                     str(source)], Path(scratch), check=True)
        tree = _git(["rev-parse", f"{seed['commit']}^{{tree}}"], source)
        project.mkdir(parents=True)
        _git(["--work-tree", str(project.resolve()), "checkout", "-f", seed["commit"], "--", "."], source)
    for args in (["init", "-q", "-b", "main"], ["config", "user.name", "eval"],
                 ["config", "user.email", "eval@example.com"], ["config", "commit.gpgsign", "false"],
                 ["add", "-A", "-f"], ["commit", "-qm", "seed"]):
        _git(args, project)
    if _git(["rev-parse", "HEAD^{tree}"], project) != tree:
        raise SystemExit("the seed export does not match the pinned commit's tree")
    for patch in seed.get("patches", []):
        _git(["am", "-q", str((case_dir / patch).resolve())], project)
    for target, source_file in case["files"].items():
        (project / target).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(case_dir / source_file, project / target)
    _git(["add", "-A", *case["files"]], project)
    _git(["commit", "-qm", "task"], project)
    return {"commit": seed["commit"], "tree": tree, "patches": len(seed.get("patches", []))}


def canaries(case: dict) -> list[str]:
    """The case's canary strings (`case.json` `canaries`): text that exists only in its hidden files."""
    found = case.get("canaries") or []
    if not isinstance(found, list) or not all(isinstance(item, str) and item.strip() for item in found):
        raise SystemExit("`canaries` must be a list of non-empty strings")
    return [item.strip() for item in found]


def canaries_sha256(case: dict) -> str | None:
    """Only the digest goes into `arm.json`: the run directory is readable by the agent, the canaries must not be."""
    found = canaries(case)
    return hashlib.sha256("\0".join(sorted(found)).encode("utf-8")).hexdigest() if found else None


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _run_identity(output: Path) -> dict:
    """The run's opaque id (its directory name) and where it was prepared. A run directory is never moved."""
    return {"run_id": output.name, "run_dir": str(output.resolve())}


def check_not_moved(run: Path, meta: dict) -> None:
    recorded = meta.get("run_dir") or (str(Path(meta["project"]).parent) if meta.get("project") else None)
    if recorded and Path(recorded).resolve() != run.resolve():
        raise SystemExit(f"moved: the run was prepared at {recorded}, not {run}; a run directory is never moved "
                         "(score it where it was prepared, or record it as a harness error `moved`)")


def pin_reviewers(project: Path, profile: str) -> dict:
    """Every kit reviewer on the pinned claude model and effort, declared in the project's profile override."""
    return variants.pin_reviewers(project, profile, REVIEWER_PIN)


def external_bare_prompt(case: dict) -> str:
    return task_prompt(external_task(case))


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


def kit_managed_paths(prev_project: Path) -> list[str]:
    """The paths the kit's installer recorded as its own in `.git/info/exclude` (no globs, no negations)."""
    common = _common_dir(prev_project)
    exclude = (common / "info" / "exclude") if common is not None else prev_project / ".git" / "info" / "exclude"
    lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.is_file() else []
    return sorted({line.strip().strip("/") for line in lines
                   if line.strip() and not line.lstrip().startswith(("#", "!"))
                   and not any(char in line for char in "*?[") and line.strip().strip("/")})


def _carry_kit_state(prev_project: Path, project: Path) -> list[str]:
    """Only what the kit manages in the previous project (#409 v3.14 E): the paths its installer recorded in
    `.git/info/exclude` (its install, project settings, local skills, handoffs), without build outputs or live run
    state. A bare arm carries none of these, so neither arm carries anything else git ignores (`.env`, data, build
    output). `_install` then rewrites the paths the kit state holds."""
    common = _common_dir(prev_project)
    if common is not None and (common / "info" / "exclude").is_file():
        (project / ".git" / "info").mkdir(parents=True, exist_ok=True)
        shutil.copy2(common / "info" / "exclude", project / ".git" / "info" / "exclude")
    listing = [name for name in kit_managed_paths(prev_project) if (prev_project / name).exists()
               or (prev_project / name).is_symlink()]
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
    check_not_moved(prev_run, prev)
    prev_project = Path(prev["project"])
    arm = effective_arm(prev)
    try:
        frozen = snapshot.verify(prev_run)
    except snapshot.SnapshotError as failure:
        raise SystemExit(f"the previous run has no graded snapshot to build on: {failure}") from failure
    if prev_result.get("artifact_sha256") and frozen["tree_sha256"] != prev_result["artifact_sha256"]:
        raise SystemExit("the previous run's graded snapshot is not the one it was scored on")
    scratch = Path(tempfile.mkdtemp(prefix="arm-chain-"))
    checkout = snapshot.materialize(prev_run, scratch / "checkout")
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
    task_text = external_task(case)
    task = with_ask_user(task_text, simulated["ask_user"] if simulated else None)
    routed = {}
    if prev["arm"] in ROUTED_ARMS:
        decision = prev["router"]
        routed = {"effective_arm": arm, "router_frozen_at": prev.get("router_frozen_at"),
                  "router": {**decision, "cost_usd": 0.0, "reused": True, "reused_from": str(prev_run),
                             "original_cost_usd": decision.get("cost_usd")}}
    searched, gate, skills, pinned = None, None, None, None
    if arm in BARE_ARMS:
        if arm in GATE_ARMS:
            _drop_gate_hooks(project)
            gate = _install_verify_gate(output, project, task_text, sorted(case["files"]), case, "external")
        if arm in SKILL_ARMS:
            skills = prev.get("skills")  # committed in the previous step's history, as the arm left them
        _git(["checkout", "-qb", BRANCH], project)
        prompt = task_prompt(task)
    else:
        carried["kit_state_carried"] = _carry_kit_state(prev_project, project)
        _install(agent_kit, project, {"profile": case["profile"], "modes": {"clean": {"install": case["install"]}}},
                 hooks=True)
        pinned = pin_reviewers(project, case["profile"])
        carried["archived_runs"] = _archive_previous_runs(prev_project, project, step)
        spec = (prev["router"].get("variant") if prev["arm"] in ROUTED_ARMS
                else (prev.get("variant") or {}).get("spec")) if arm == "S" else None
        searched = _write_variant(agent_kit, project, case["profile"], spec)
        prompt = driver_prompt(task, searched["name"] if searched else WORKFLOWS[arm], ask_user=simulated is not None)
    (output / "prompt.md").write_text(prompt, encoding="utf-8")
    shutil.rmtree(scratch, ignore_errors=True)
    prior = [*chained.get("prior_runs", []), str(prev_run)]
    meta = {"arm": prev["arm"], **routed, "case": case["name"], "trial": prev.get("trial"),
            "project": str(project.resolve()), **_run_identity(output), "prepared_at": _now(),
            "reviewer_pin": pinned, "canaries_sha256": canaries_sha256(case),
            "start_commit": _git(["rev-parse", "HEAD"], project),
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
    return task_prompt(ui_task(task))


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

    reports: list[dict] = []
    call = (_sandboxed_call([{"path": str(project.resolve()), "mode": "ro"}, *auth_exposes()], reports, "router")
            if sandbox_mode() == "required" else None)
    decision = router.route(task, project, output / router.OUT_DIR, model, controller_env(dict(os.environ)),
                            menu_variants, check_spec, effort=PINNED_EFFORT, call=call)
    if call is not None:
        survivors = [item for report in reports for item in report.get("survivors") or []]
        decision = {**decision, "sandbox": {"calls": len(reports), "survivors": survivors,
                                            "errors": [report["error"] for report in reports if report.get("error")]}}
        if survivors:
            raise SystemExit(f"sandbox: {len(survivors)} router process(es) outlived their namespace")
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
    if ui is not None and arm == "Cp":
        raise ValueError("C′ is not defined for UI tasks")
    prepared_at = _now()
    output.mkdir(parents=True)
    agent_kit = sanitized_kit(kit, output / "kit")
    project = output / "project"
    if ui is None:
        _seed_project(case_dir, project)
    else:
        _seed_ui_project(ui, project)
    task = ui_task(case["task"]) if ui else phase_task(case)
    routed, requested = {}, arm
    if arm in ROUTED_ARMS:
        routed = _route(output, project, task, model, menu_variants,
                        {"arm": arm, "case": case_name, "trial": trial, "project": str(project.resolve()),
                         **_run_identity(output)}, agent_kit, case["profile"])
        arm, variant = routed["effective_arm"], routed["router"].get("variant")
    searched, gate, skills, pinned = None, None, None, None
    if arm in BARE_ARMS:
        if arm in PRINCIPLE_SETS:
            _write_principles(project, arm)
        if arm in GATE_ARMS:
            gate = _install_verify_gate(output, project, task, _ui_documents(ui) if ui else [], case,
                                        "ui" if ui else "phase-case")
        if arm in SKILL_ARMS:
            skills = _install_skills(agent_kit, project)
        _git(["checkout", "-qb", BRANCH], project)
        prompt = task_prompt(task)
    elif arm == "Cp":
        _install(agent_kit, project, case, hooks=False)
        _git(["checkout", "-qb", BRANCH], project)
        prompt = c_prime.build(agent_kit, project, task, "commit")
    else:
        _install(agent_kit, project, case, hooks=True)
        pinned = pin_reviewers(project, case["profile"])
        searched = _write_variant(agent_kit, project, case["profile"], variant)
        prompt = driver_prompt(task, searched["name"] if searched else WORKFLOWS[arm])
    (output / "prompt.md").write_text(prompt, encoding="utf-8")
    meta = {"arm": requested, **routed, "case": case_name, "trial": trial, "project": str(project.resolve()),
            **_run_identity(output), "prepared_at": prepared_at, "reviewer_pin": pinned,
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
    prepared_at = _now()
    output.mkdir(parents=True)
    agent_kit = sanitized_kit(kit, output / "kit")
    project = output / "project"
    seeded = _seed_external_project(case, case_dir, project)
    simulated = _install_user_sim(output, selected["oracle"], model)
    task_text = external_task(case)
    task = with_ask_user(task_text, simulated["ask_user"] if simulated else None)
    routed, requested = {}, arm
    if arm in ROUTED_ARMS:
        routed = _route(output, project, task, model, menu_variants,
                        {"arm": arm, "case": case["name"], "trial": trial, "project": str(project.resolve()),
                         **_run_identity(output)}, agent_kit, case["profile"])
        arm, variant = routed["effective_arm"], routed["router"].get("variant")
    searched, gate, skills, pinned = None, None, None, None
    if arm in BARE_ARMS:
        if arm in PRINCIPLE_SETS:
            _write_principles(project, arm)
        if arm in GATE_ARMS:
            gate = _install_verify_gate(output, project, task_text, sorted(case["files"]), case, "external")
        if arm in SKILL_ARMS:
            skills = _install_skills(agent_kit, project)
        _git(["checkout", "-qb", BRANCH], project)
        prompt = task_prompt(task)
    else:
        _install(agent_kit, project, {"profile": case["profile"], "modes": {"clean": {"install": case["install"]}}},
                 hooks=True)
        pinned = pin_reviewers(project, case["profile"])
        searched = _write_variant(agent_kit, project, case["profile"], variant)
        prompt = driver_prompt(task, searched["name"] if searched else WORKFLOWS[arm], ask_user=simulated is not None)
    (output / "prompt.md").write_text(prompt, encoding="utf-8")
    meta = {"arm": requested, **routed, "case": case["name"], "trial": trial, "project": str(project.resolve()),
            **_run_identity(output), "prepared_at": prepared_at, "reviewer_pin": pinned, "seed_export": seeded,
            "canaries_sha256": canaries_sha256(case),
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


def linked_worktrees(project: Path, home: Path | None = None) -> list[Path]:
    """Linked worktrees git lists for the project (`--porcelain -z`, so no path is split on a newline), as host paths.

    `home` is the run sandbox's HOME on the host: git records a worktree the runner made inside the sandbox at its
    inside path (`/root/.agent-flow/worktrees/<name>`), which `sandbox.to_host` maps back into the run directory.
    A listed path counts only while it still belongs to this repository. A stale registration can name a path that
    another project's worktree now occupies (a project moved aside and re-prepared at the same path makes the runner
    create its worktree there again), and grading it would credit that project's work to this run.
    """
    listing = c_prime._sh(["git", "worktree", "list", "--porcelain", "-z"], project).stdout
    paths = [Path(field.split(" ", 1)[1]) for field in listing.split("\0") if field.startswith("worktree ")]
    if home is not None:
        paths = [sandbox.to_host(path, home) for path in paths]
    common = _common_dir(project)
    return [path for path in paths if path.resolve() != project.resolve() and path.is_dir()
            and _common_dir(path) == common]


def runner_worktrees(project: Path, home: Path | None = None) -> list[Path]:
    """Linked worktrees the runner owns: they have run state in `<git-common-dir>/agent-flow/worktrees/<name>/`."""
    state = _common_dir(project)
    return [path for path in linked_worktrees(project, home)
            if state is not None and (state / "agent-flow" / "worktrees" / path.name / ".agent-flow" / "runs").is_dir()]


def select_checkout(project: Path, arm: str, home: Path | None = None) -> tuple[Path, dict]:
    """Which checkout to grade, chosen from run state, never from modification times. `drive` asks this once, when
    the run ends, records the answer (`drive.json` `checkout`: path, HEAD) and freezes that tree; `score` and
    `chain-next` read only the frozen snapshot (`recorded_checkout`).

    A workflow arm's work is in the worktree its runner created; an agent can add worktrees of its own
    (`git worktree add` came up in the C′ smoke, #444), and the newest one need not be the one it worked in. The
    other arms work in the project itself. When a workflow arm has linked worktrees but not exactly one with run
    state, the choice is not decidable and the run is a harness error.
    """
    linked = linked_worktrees(project, home)
    info = {"linked_worktrees": len(linked), "checkout_selected_by": None, "checkout_error": None}
    if arm not in WORKFLOWS:
        return project, {**info, "checkout_selected_by": "project"}
    owned = runner_worktrees(project, home)
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


def archived(path: Path) -> bool:
    """A file in the kit's archive of earlier runs (`.git/agent-flow/archive/**`, a chain step's history)."""
    parts = path.parts
    return any(parts[index:index + 2] == ("agent-flow", "archive") for index in range(len(parts) - 1))


def state_files(project: Path, checkout: Path, pattern: str) -> list[Path]:
    """Runner state files of this run (`.git/agent-flow`, the checkout's `.agent-flow/runs`), never the archive: a
    chain step's metrics must not count an earlier step's phases, reviewers, gates or approvals (#409 v3.14 E)."""
    roots = [project / ".git" / "agent-flow", checkout / ".agent-flow" / "runs"]
    return sorted({path for root in roots if root.is_dir() for path in root.rglob(pattern) if not archived(path)})


def gate_record(project: Path, checkout: Path) -> dict:
    """What the runner's `gates` phase ran locally and what it only deferred to CI.

    A profile gate with `execution: ci` (python's required `test`) is written to `deferred_ci_checks` and not run;
    the harness has no remote, so a deferred gate is never run at all. Read from each run's canonical
    `artifacts/gate-results.json`; the last one written is the run's final gate ledger.
    """
    ledgers = sorted(state_files(project, checkout, "artifacts/gate-results.json"),
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
    events = state_files(project, checkout, "events.jsonl")
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
    events = []
    for path in state_files(project, checkout, "events.jsonl"):
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
    This bound is the cap's fallback only: `budget_ledger` prices the reviewers from the usage wrapper's rows
    (`claude_usage`), one per reviewer call.
    """
    files = state_files(project, checkout, "review-metrics-*.json")
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
    files = state_files(project, checkout, MANUAL_APPROVALS)
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
    """The graded snapshot's fingerprint of a tree: path, type, mode, symlink target and content of every entry, by the
    snapshot rule (`snapshot.entries`: no `.git`, dependency folders or runner state)."""
    return snapshot.entries_sha256(snapshot.entries(checkout))


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
    """A hidden or visible test command on the grading copy; in the submission's own sandbox when the sandbox is on
    (the copy writable, nothing else of the machine: no other run, no evaluator repository, a private /tmp)."""
    if sandbox_mode() == "required":
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "HOME": str(cwd)}
        try:
            result = sandbox.run(sandbox.submission_spec(cwd, env, 300, []), command, capture=True)
        except sandbox.SandboxError:
            return False
        stderr, code = result["stderr"], result["returncode"]
    else:
        done = c_prime._sh(command, cwd, timeout=300)
        stderr, code = done.stderr, done.returncode
    ran = re.search(r"^Ran (\d+) tests?", stderr, re.M)
    return code == 0 and bool(ran) and int(ran.group(1)) > 0 and "\nOK" in stderr


SUBMISSION_WRAPPED = "gradlew.unsandboxed"


def wrap_submission(build: Path, workdir: Path) -> dict:
    """Makes a graded build start in its own sandbox when the evaluator runs it (#409 v3.14 C): every `eval.json`
    command and `./gradlew` go through `sandbox.py exec`, which exposes only the evaluator's working copy of the build,
    its HOME and DATA_DIR, with its own PID namespace and /tmp. The evaluator itself, its checks and the private
    repository stay outside, invisible to the submission. Applied to the throwaway checkout given to the evaluator."""
    launcher = [sys.executable, "-I", str((HERE / "sandbox.py").resolve()), "exec"]
    config = workdir / "submission-sandbox.json"
    log = workdir / "submission-sandbox.jsonl"
    config.write_text(json.dumps({"timeout": 3600, "log": str(log)}), encoding="utf-8")
    prefix = " ".join(shlex.quote(item) for item in [*launcher, "--spec", str(config), "--"])
    wrapped: dict = {"eval_json": [], "gradlew": False, "log": str(log)}
    spec_path = build / "eval.json"
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8")) if spec_path.is_file() else None
    except (OSError, ValueError):
        spec = None
    if isinstance(spec, dict):
        for key in ("install", "start", "build", "test"):
            if isinstance(spec.get(key), str) and spec[key].strip():
                spec[key] = f"{prefix} sh -c {shlex.quote(spec[key])}"
                wrapped["eval_json"].append(key)
        spec_path.write_text(json.dumps(spec, indent=1) + "\n", encoding="utf-8")
    gradlew = build / "gradlew"
    if gradlew.is_file() and not gradlew.is_symlink():
        gradlew.rename(build / SUBMISSION_WRAPPED)
        gradlew.write_text(f'#!/bin/sh\nexec {prefix} "$(dirname "$0")/{SUBMISSION_WRAPPED}" "$@"\n', encoding="utf-8")
        gradlew.chmod(0o755)
        wrapped["gradlew"] = True
    return wrapped


def evaluator_supports(evaluator: Path, flag: str) -> bool:
    try:
        return flag in evaluator.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _grade_ui(checkout: Path, scans: list[Path] | Path | None, evaluator: Path, repeat: int | None, out: Path,
              browser_repeat: int | None = None, reference_build: bool = False) -> dict:
    """Runs the private evaluator's `score.py` on the checkout. It copies the build itself and never writes to it.

    `repeat` is None for an evaluator that takes no `--repeat` (an external case says which it is); `browser_repeat` is
    passed only to an evaluator that has `--browser-repeat`. Every log in `scans` is passed with its own `--scan`.
    """
    if isinstance(scans, Path):
        scans = [scans]
    argv = [sys.executable, "-I", str(evaluator), "--build", str(checkout),
            *(["--repeat", str(repeat)] if repeat else []),
            *(["--browser-repeat", str(browser_repeat)] if browser_repeat else []),
            *(["--reference-build"] if reference_build else []), "--output", str(out)]
    for path in scans or []:
        argv += ["--scan", str(path)]
    graded = subprocess.run(argv, capture_output=True, text=True, timeout=7200)
    if graded.returncode != 0 or not out.is_file():
        return {"S": 0, "error": f"evaluator exited {graded.returncode}: {graded.stderr[-400:]}"}
    return json.loads(out.read_text(encoding="utf-8"))


# Content-based contamination (#409 v3.14 C): every log of a run is read for the case's canaries (text that exists only
# in its hidden files), and every tool call is read for paths under the hidden roots. A hidden path a tool call names
# whose result came back with content is a `read`; a hidden path seen only in some output (an `ls` of a parent) is a
# `listing`, reported but not contamination.
HIDDEN_ROOTS_ENV = "ARM_COMPARE_HIDDEN_ROOTS"
# `/tmp`는 sandbox 안에서 run 전용 tmpfs라, 그 아래 경로는 에이전트 자신의 작업 파일이다(CLI의 task output 등).
# 운영자의 scratch처럼 가려야 할 `/tmp` 아래 자리는 runner가 `ARM_COMPARE_HIDDEN_ROOTS`로 정확히 넘긴다.
DEFAULT_HIDDEN_ROOTS = ("/home/user/agent-flow-evals-private", "/home/user/agent-flow")
ABSOLUTE_PATH = re.compile(r"(?<![\w.~-])(/[\w.@%+=,~-]+(?:/[\w.@%+=,~-]*)*)")
EMPTY_RESULT = re.compile(r"^\s*(|.*(No such file|not found|does not exist|Permission denied|cannot access).*)\s*$",
                          re.I | re.S)


def hidden_roots() -> list[str]:
    extra = [item for item in os.environ.get(HIDDEN_ROOTS_ENV, "").split(":") if item]
    return [*DEFAULT_HIDDEN_ROOTS, *extra]


def _hidden_paths(text: str, roots: list[str], own: list[str]) -> list[str]:
    found = []
    for path in ABSOLUTE_PATH.findall(text):
        if any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots) and not any(
                path == mine or path.startswith(mine.rstrip("/") + "/") or mine.startswith(path.rstrip("/") + "/")
                for mine in own):
            found.append(path)
    return found


def _tool_results(rows: list[dict]) -> dict[str, tuple[str, bool]]:
    results = {}
    for row in rows:
        message = row.get("message")
        if row.get("type") != "user" or not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content = block.get("content")
                text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                results[block.get("tool_use_id")] = (text, bool(block.get("is_error")))
    return results


def run_logs(run: Path, transcript: Path | None, project: Path, checkout: Path) -> list[Path]:
    """Every log a run leaves: the controller's transcript and its sub-agents', the stream, the kit's reviewer
    outputs (state and archive copies), arm V's gate log, arm R's router outputs and the simulated user's log."""
    logs: list[Path] = []
    if transcript is not None and transcript.is_file():
        logs.append(transcript)
        logs += run_usage.subagent_transcripts(transcript)[0]
    logs += [run / name for name in ("controller.jsonl", "controller.stderr", verify_gate.LOG, user_sim.LOG)]
    logs += sorted((run / router.OUT_DIR).glob("attempt-*")) if (run / router.OUT_DIR).is_dir() else []
    logs += sorted((run / "review-archive").glob("*")) if (run / "review-archive").is_dir() else []
    logs += [path for path in state_files(project, checkout, "*.md")]
    logs += [path for path in state_files(project, checkout, "*review-results.json")]
    return [path for path in dict.fromkeys(logs) if path.is_file()]


def scan_contamination(run: Path, rows: list[dict], logs: list[Path], found_canaries: list[str],
                       disclosed: str = "") -> dict:
    """Canary hits per log, and hidden-path reads (named by a tool call that got content back) apart from listings."""
    effective = [canary for canary in found_canaries if canary not in disclosed]
    hits: dict[str, int] = {}
    for path in logs:
        try:
            text = path.read_bytes().decode("utf-8", "replace")
        except OSError:
            hits[f"{path.name} (unreadable)"] = hits.get(f"{path.name} (unreadable)", 0)
            continue
        count = sum(text.count(canary) for canary in effective)
        if count:
            hits[str(path)] = count
    roots, own = hidden_roots(), [str(run.resolve())]
    results = _tool_results(rows)
    reads, named, listed, examples = 0, 0, 0, []
    used_ids = set()
    for use in _tool_uses(rows):
        text = json.dumps(use.get("input"), ensure_ascii=False)
        paths = _hidden_paths(text, roots, own)
        used_ids.add(use.get("id"))
        if not paths:
            continue
        named += 1
        result, is_error = results.get(use.get("id"), ("", False))
        if not is_error and not EMPTY_RESULT.match(result):
            reads += 1
            examples.append(paths[0])
    for tool_id, (result, _) in results.items():
        if _hidden_paths(result, roots, own):
            listed += 1
    return {"canaries": len(effective), "canaries_disclosed_by_oracle": len(found_canaries) - len(effective),
            "logs_scanned": len(logs), "canary_hits": hits, "hidden_path_named": named, "hidden_path_reads": reads,
            "hidden_path_listed": listed, "read_examples": examples[:5],
            "contaminated": bool(hits) or reads > 0}


def _case_canaries(meta: dict, case_dir: Path | None, evaluator: Path | None) -> list[str]:
    """The canaries of the run's case, from its `case.json` (never from the run directory); checked against the
    digest `prepare` recorded."""
    expected = meta.get("canaries_sha256")
    if meta.get("case_kind") == "ui":
        return list(UI_CASES.get(meta.get("case"), {}).get("canaries") or [])
    if not expected:
        return []
    candidates = [case_dir] if case_dir is not None else []
    if evaluator is not None:
        candidates.append(evaluator.parent)
    for directory in candidates:
        if directory is not None and (directory / "case.json").is_file():
            case = json.loads((directory / "case.json").read_text(encoding="utf-8"))
            if canaries_sha256(case) == expected:
                return canaries(case)
    raise SystemExit("the run's case has canaries: pass --case-dir with the case.json it was prepared from")


def score(run: Path, transcript: Path | None, evaluator: Path | None = None, repeat: int = 3,
          harness_error: str | None = None, case_dir: Path | None = None, reference_build: bool = False) -> dict:
    """Grades the frozen snapshot (`snapshot.verify`, then a throwaway `snapshot.materialize`), never the live tree.

    An undriven run (no `drive.json`) is frozen here first; a driven run without a snapshot cannot be graded and is a
    harness error. Scoring holds the exclusive lock, so it never overlaps a drive."""
    meta = json.loads((run / "arm.json").read_text(encoding="utf-8"))
    check_not_moved(run, meta)
    if harness_error:
        try:
            attempts.check_reason(harness_error)
        except ValueError as failure:
            raise SystemExit(str(failure)) from failure
    project = Path(meta["project"])
    arm = effective_arm(meta)
    driven = json.loads((run / "drive.json").read_text(encoding="utf-8")) if (run / "drive.json").is_file() else {}
    harness_error = harness_error or driven.get("harness_error")
    if transcript is None and driven.get("transcript"):
        transcript = Path(driven["transcript"])
    main_rows, rows, read = session_rows(transcript)
    # A v3.14 drive (`driven_at`) freezes the tree and records the checkout itself. A run with no drive, or with a
    # drive.json from an older harness, is frozen here and says so (`legacy_drive`).
    current = bool(driven.get("driven_at"))
    if snapshot.load(run) is None:
        if current:
            raise SystemExit("snapshot: the driven run has no graded snapshot; record it with "
                             "--harness-error 'snapshot: ...' after checking drive.json")
        frozen_checkout, _ = select_checkout(project, arm)
        snapshot.freeze(frozen_checkout, run, meta.get("start_commit"),
                        "score: legacy drive" if driven else "score: undriven run")
    try:
        manifest = snapshot.verify(run)
    except snapshot.SnapshotError as failure:
        raise SystemExit(f"snapshot: {failure}") from failure
    if current:
        live, chosen = recorded_checkout(run, meta, driven)
        chosen = {key: chosen.get(key) for key in ("linked_worktrees", "checkout_selected_by", "checkout_error")}
    else:
        live, chosen = select_checkout(project, arm)
    harness_error = harness_error or chosen.get("checkout_error")
    message = final_message(main_rows)
    declaration = completion.classify(message if main_rows else None)
    found_canaries = _case_canaries(meta, case_dir, evaluator)
    oracle_text = ""
    if meta.get("user_sim") and (run / user_sim.DIR / user_sim.ORACLE).is_file():
        oracle_text = (run / user_sim.DIR / user_sim.ORACLE).read_text(encoding="utf-8", errors="replace")
    logs = run_logs(run, transcript, project, live)
    scanned = scan_contamination(run, rows, logs, found_canaries, oracle_text)
    submission = None
    with _Lock(exclusive=True), tempfile.TemporaryDirectory(prefix="arm-score-") as scratch:
        checkout = snapshot.materialize(run, Path(scratch) / "checkout")
        commits = c_prime._sh(["git", "rev-list", "--count", f"{meta['start_commit']}..HEAD"], checkout).stdout.strip()
        checked = integrity.check(checkout, meta["start_commit"], shell_commands(rows), message, declaration)
        gate = gate_result(run, meta, checkout, driven) if arm in GATE_ARMS else None
        sandboxed = sandbox_mode() == "required"
        if meta.get("case_kind") in ("external", "ui"):
            if evaluator is None:
                raise SystemExit("an external case or UI task needs --evaluator <its score.py>")
            if sandboxed:
                submission = wrap_submission(checkout, Path(scratch))
            scans = [path for path in logs if not (path == run / user_sim.LOG and any(c in oracle_text
                                                                                      for c in found_canaries))]
            browser = BROWSER_REPEAT if evaluator_supports(evaluator, "--browser-repeat") else None
            if meta.get("case_kind") == "external":
                ui = _grade_ui(checkout, scans, evaluator, repeat if meta.get("evaluator_repeat") else None,
                               run / "ui-score.json", browser, reference_build)
                graded = {"ui": {key: ui.get(key) for key in (*meta["score_keys"], "error")}}
            else:
                ui = _grade_ui(checkout, scans, evaluator, repeat, run / "ui-score.json", browser, reference_build)
                graded = {"ui": {key: ui.get(key) for key in ("S", "G", "API", "V", "E", "error", "failed_browser",
                                                                 "failed_api", "evaluator_sha256", "build_sha256",
                                                                 "ledger", "isolation")}}
            graded.update({"oracle": ui.get("S") == 1, "evaluator_read": bool(ui.get("contamination")),
                           "evaluator_contamination_detail": ui.get("contamination_detail"),
                           "evaluator_browser_repeat": browser})
            if submission is not None:
                submission["runs"] = [json.loads(line) for line in Path(submission["log"]).read_text(
                    encoding="utf-8").splitlines() if line.strip()] if Path(submission["log"]).is_file() else []
                submission["survivors"] = sum(len(item.get("survivors") or []) for item in submission["runs"])
                submission.pop("log", None)
            failures = finding_split.failures_from(ui, graded["ui"])
            graded["hidden_failures"] = {key: failures[key] for key in ("missing", "wrong", "unclear", "no_build",
                                                                        "failed_count")}
            graded["no_start"] = failures["no_build"]
            graded["complete_build"] = not failures["no_build"] and failures["missing"] == 0
        else:
            graded = _grade_phase_case(meta, checkout)
            graded["phase_hidden_markers"] = any(marker in item for item in tool_inputs(rows)
                                                 for marker in HIDDEN_MARKERS)
            graded["no_start"], graded["complete_build"] = None, None
    graded["contamination_scan"] = scanned
    simulated = user_sim.summary(run / user_sim.LOG) if meta.get("user_sim") else None
    if simulated is not None:
        simulated["oracle_reads"] = oracle_reads(rows, run)
    if meta.get("chain"):
        graded["chain_reads"] = chain_reads(rows, meta)
    healthy = attempts.sandbox_healthy(driven.get("sandbox"))
    evidence = {"sandbox_healthy": healthy, "canary_hits": sum(scanned["canary_hits"].values()),
                "evaluator_read": bool(graded.pop("evaluator_read", False)),
                "hidden_path_reads": scanned["hidden_path_reads"],
                "phase_hidden_markers": bool(graded.pop("phase_hidden_markers", False)),
                "oracle_reads": (simulated or {}).get("oracle_reads", 0), "chain_reads": graded.get("chain_reads", 0),
                "tripwire_changed": bool(driven.get("tripwire_changed"))}
    graded["contamination_evidence"] = evidence
    graded["contamination_rule"] = attempts.contamination_rule(evidence)
    graded["contaminated"] = attempts.contaminated(evidence)
    if gate is not None and not gate["integrity_intact"]:
        harness_error = harness_error or "gate-integrity: the gate script or config changed after prepare/drive"
    routed = meta.get("router")
    ledger = budget_ledger(run, meta, driven.get("cost_usd"), driven.get("review_classifier_cost_usd") or 0.0,
                           project, live)
    sandboxed_drive = (driven.get("sandbox") or {}).get("mode") == "required"
    result = {"isolation": "instructed", **meta, "task_kind": "normal",
              **({"isolation": "enforced"} if sandboxed_drive else {}),
              "checkout": str(live), "graded_tree": str(run / snapshot.DIR / "tree"),
              "legacy_drive": bool(driven) and not current,
              "snapshot": snapshot.record(manifest), "uncommitted": manifest["has_uncommitted"], **graded,
              "submission_sandbox": submission,
              "commits": int(commits) if commits.isdigit() else None,
              "final_message": message, "declaration": declaration, "integrity": checked,
              "spec_self_approvals": sum(bool(SPEC_SELF_APPROVAL.search(item)) for item in tool_inputs(rows)),
              "phase_self_approvals": sum(bool(PHASE_SELF_APPROVAL.search(item)) for item in tool_inputs(rows)),
              "verify_downgrades": verify_downgrades(rows), **skills_invoked(rows),
              "manual_spec_approvals": manual_spec_approvals(project, live), "artifact_sha256": manifest["tree_sha256"],
              "transcript": str(transcript) if transcript else None, **_measured_usage(rows, read), **read,
              "controller_cost_usd": driven.get("cost_usd"), "total_cost_usd": ledger["total_cost_usd"],
              "cost_complete": ledger["cost_complete"], "cost_components": ledger["components"],
              "cost_unpriced": ledger["unpriced"], "cost_enforced_usd": ledger["enforced_usd"],
              "end_to_end_seconds": driven.get("end_to_end_seconds"),
              "controller_seconds": driven.get("controller_seconds"),
              **({key: driven[key] for key in ("review_classifier_cost_usd", "review_stop", "fix_extensions",
                                               "review_classifier_failures", "budget_stop", "deadline_stop",
                                               "pins", "base_skills", "tripwire_changed") if key in driven}),
              **(simulated or {}), **({"grill_checks": driven["grill_checks"]} if "grill_checks" in driven else {}),
              **({"router_cost_usd": routed.get("cost_usd")} if routed else {}),
              **({"verify_gate": gate} if gate is not None else {}), "drive_exit": driven.get("exit"),
              "drive_timed_out": driven.get("timed_out"), "drive_is_error": driven.get("is_error"),
              "fix_loop_reapprovals": len(driven.get("fix_loop_reapprovals") or []),
              "fix_loop_reapproval_log": driven.get("fix_loop_reapprovals") or [],
              "harness_error": harness_error, "exclusion": attempts.exclusion_code(harness_error), **chosen,
              **(_runner_trace(project, live) if arm in WORKFLOWS else {}),
              **(gate_record(project, live) if arm in WORKFLOWS else {}),
              **({"reviewers": reviewer_record(project, live)} if arm in WORKFLOWS else {}),
              **({"phase_trace": phase_trace(project, live, meta.get("workflow") or WORKFLOWS[arm], rows,
                                             measured=read.get("subagents") != "unmeasured")}
                 if arm in WORKFLOWS else {}),
              "scored_at": datetime.now().isoformat(timespec="seconds")}
    (run / "result.json").write_text(json.dumps(result, indent=1, default=str) + "\n", encoding="utf-8")
    return result


def total_cost(controller: float | None, gate: dict | None, routed: dict | None = None,
               simulator: float | None = 0.0, classifier: float | None = 0.0, kit: float | None = 0.0) -> float | None:
    """The controller's cost plus arm V's reviewer cost, arm R's router cost, the simulated user's cost, the
    harness's review-finding classifier cost and the kit's reviewers; null when any of them is unknown."""
    reviewer = gate["reviewer_cost_usd"] if gate is not None else 0.0
    chooser = routed.get("cost_usd") if routed else 0.0
    parts = (controller, reviewer, chooser, simulator, classifier, kit)
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
    argv = ["claude", "-p", prompt, "--model", model, "--effort", PINNED_EFFORT, "--output-format", "stream-json",
            "--verbose", "--tools", ",".join(CLAUDE_TOOLS),
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
    """The allowlist from the operator's environment, plus the fixed settings every participating call runs with."""
    return {**{key: parent[key] for key in CONTROLLER_ENV if key in parent}, "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
            **PINNED_ENV}


def _stream_init(stream: Path) -> dict | None:
    """What the CLI said it started with (first `init` row): model, tools, skills, version, permission mode."""
    for row in run_usage._jsonl(stream):
        if row.get("type") == "system" and row.get("subtype") == "init":
            return {key: row.get(key) for key in ("model", "tools", "skills", "claude_code_version", "permissionMode",
                                                  "cwd", "plugins", "mcp_servers")}
    return None


def _stream_summary(stream: Path) -> dict:
    session, result = None, {}
    for row in run_usage._jsonl(stream):
        if row.get("type") == "system" and row.get("subtype") == "init":
            session = row.get("session_id")
        elif row.get("type") == "result":
            result = row
    return {"session_id": session, "cost_usd": result.get("total_cost_usd"), "is_error": result.get("is_error"),
            "init": _stream_init(stream),
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
# v3.12: at a cap stop the harness labels the open findings with the frozen review_findings classifier (no tools, run by
# the harness, unseen by the agent). An `incomplete` finding ends the run there (a completeness failure). Otherwise the
# run gets up to MAX_FIX_EXTENSIONS more fix rounds on exactly those findings. A classifier failure, or no parsed
# findings, falls back to the re-review above and is recorded.
MAX_FIX_EXTENSIONS = 2
OPEN_FINDINGS_SHOWN = 20
RESUME_TO_FIX_FINDINGS = (
    "The evaluator, as the user, lets the run continue past the fix-loop limit at phase `{phase}` so that these open "
    "review findings get fixed:\n{findings}\nFix them in the code, run the tests, run the `{phase}` reviewers again on "
    "the new code, replace the phase artifact with their new aggregate, then run the next command the runner prints.")
REVIEW_CLASSIFIER_BUDGET_USD = 2.0


def sandbox_mode() -> str:
    """`required` (the default: every drive and grading call runs in the run sandbox) or `off` (tests only)."""
    mode = os.environ.get(SANDBOX_ENV, "required")
    if mode not in ("required", "off"):
        raise SystemExit(f"{SANDBOX_ENV} must be required or off, not {mode!r}")
    return mode


class _Lock:
    """drive: shared; score: exclusive. Scoring waits until no drive runs, and a drive waits for a scoring to end."""

    def __init__(self, exclusive: bool):
        self.exclusive = exclusive
        self.path = Path(os.environ.get(LOCK_ENV) or DEFAULT_LOCK)

    def __enter__(self):
        self.handle = self.path.open("a+")
        fcntl.flock(self.handle, fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()


def usage_bin(run: Path) -> Path:
    """Where `drive` installs the kit reviewer usage wrapper (`claude_usage.install`): in the run directory, which the
    sandbox shows read-only, outside the project and its worktrees (the kit refuses a provider binary inside them)."""
    return run / "sandbox" / "bin"


def install_usage_wrapper(run: Path) -> str:
    """The wrapper and a fresh collector socket name for this drive."""
    name = f"arm-compare-usage-{secrets.token_hex(8)}"
    claude_usage.install(usage_bin(run), name)
    return name


def run_box(run: Path, meta: dict, cwd: Path | str, timeout: float, label: str) -> dict:
    """The run sandbox for one call inside a run: the controller, or the harness's runner status and approvals.

    The run directory is read-only; its project, and the logs the gate and the simulator append to, are writable;
    HOME is the run's own (`<run>/sandbox/home`, so the runner's worktrees and the CLI's transcripts stay in the run).
    PATH starts with the kit reviewer usage wrapper when `drive` installed one (`usage_bin`).
    """
    layout = sandbox.prepare_layout(run)
    expose = [{"path": str(run.resolve()), "mode": "ro"}, {"path": str(Path(meta["project"]).resolve()), "mode": "rw"},
              *auth_exposes()]
    for name, needed in ((verify_gate.LOG, effective_arm(meta) in GATE_ARMS), (user_sim.LOG, bool(meta.get("user_sim")))):
        if needed:
            (run / name).touch()
            expose.append({"path": str((run / name).resolve()), "mode": "rw"})
    env = claude_usage.with_wrapper({**controller_env(dict(os.environ)), "HOME": sandbox.HOME_TARGET}, usage_bin(run))
    return sandbox.spec(expose=expose, cwd=str(cwd), env=env, timeout=timeout, home=str(layout["home"]),
                        overlay_root=str(layout["overlay"]), label=label)


def _sandboxed_call(expose: list[dict], reports: list[dict], label: str):
    """A `(argv, cwd, env, timeout) -> (exit, stdout, stderr, timed out)` launcher that runs in a fresh sandbox with an
    empty HOME (arm R's router at prepare)."""
    def call(command: list[str], cwd: Path, env: dict[str, str], timeout: int):
        box = sandbox.spec(expose=expose, cwd=str(cwd), env={**env, "HOME": sandbox.HOME_TARGET}, timeout=timeout,
                           home=None, overlay_root=None, label=label)
        try:
            result = sandbox.run(box, command, capture=True)
        except sandbox.SandboxError as failure:
            reports.append({"label": label, "error": str(failure)})
            return None, "", str(failure), False
        reports.append({key: value for key, value in result.items() if key not in ("stdout", "stderr")})
        return result["returncode"], result["stdout"], result["stderr"], result["timed_out"]
    return call


def runner_call(run: Path | None, meta: dict | None, argv: list[str], cwd: Path | str, timeout: float,
                reports: list[dict] | None = None, label: str = "runner") -> subprocess.CompletedProcess:
    """A harness call of the kit's runner (`status`, the approval `continue`): the controller's environment, a timeout
    from the run's remaining time, and, when the sandbox is on, the run sandbox (same paths as the controller sees)."""
    timeout = max(1.0, timeout)
    if run is None or sandbox_mode() == "off":
        try:
            return c_prime._sh(argv, Path(cwd), env=controller_env(dict(os.environ)), timeout=timeout)
        except subprocess.TimeoutExpired as expired:
            return subprocess.CompletedProcess(argv, 124, expired.stdout or "", "timeout")
    try:
        result = sandbox.run(run_box(run, meta, cwd, timeout, label), argv, capture=True)
    except sandbox.SandboxError as failure:
        if reports is not None:
            reports.append({"label": label, "error": str(failure)})
        return subprocess.CompletedProcess(argv, 125, "", str(failure))
    if reports is not None:
        reports.append({key: value for key, value in result.items() if key not in ("stdout", "stderr")})
    code = result["returncode"] if isinstance(result["returncode"], int) else 125
    return subprocess.CompletedProcess(argv, 124 if result["timed_out"] else code, result["stdout"], result["stderr"])


def runner_status(project: Path, home: Path | None = None, call=None) -> dict | None:
    """What the read-only `agent-flow status` reports for the run's worktree, read by the harness, not by the agent.

    `call(argv, cwd)` runs it (the run sandbox in `drive`). A stop at the fix-loop limit is read from the runner's
    state (`fix_loop_cap_from_state`), since only `continue` prints the limit line and polling must not advance the run.
    """
    owned = runner_worktrees(project, home)
    if len(owned) != 1:
        return None
    worktree = owned[0]
    inside = sandbox.to_inside(worktree, home) if home is not None else worktree
    argv = [str(project / ".agent-flow" / "bin" / "agent-flow"), "status", "--root", str(project),
            "--worktree", worktree.name]
    shown = call(argv, inside) if call is not None else runner_call(None, None, argv, inside, 120)
    status = parse_runner_status(shown.stdout or "", inside)
    if status is None:
        return None
    status["worktree_host"] = str(worktree)
    if status.get("fix_loop_cap") is None:
        status["fix_loop_cap"] = fix_loop_cap_from_state(worktree, status)
    return status


def fix_loop_cap_from_state(worktree: Path, status: dict) -> dict | None:
    """`{rounds, phase, target}` when the run is blocked on a route and a fix collector reached the round limit."""
    if status.get("reason") != "route_blocked":
        return None
    directory = run_state_dir(worktree, status)
    try:
        meta = json.loads((directory / "meta.json").read_text(encoding="utf-8")) if directory else {}
    except (OSError, ValueError):
        return None
    rounds = meta.get("fix_loop_rounds")
    if not isinstance(rounds, dict):
        return None
    capped = [(target, count) for target, count in rounds.items()
              if isinstance(count, int) and count >= RUNNER_FIX_LOOP_MAX_ROUNDS]
    if not capped:
        return None
    target, count = max(capped, key=lambda item: item[1])
    return {"rounds": RUNNER_FIX_LOOP_MAX_ROUNDS, "phase": status.get("current_phase"), "target": target,
            "source": "runner-state"}


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


def _status_worktree(status: dict) -> Path:
    """The worktree as the harness reads its files (the host path; the same path when no sandbox maps it)."""
    return Path(status.get("worktree_host") or status["worktree"])


def run_state_dir(worktree: Path, status: dict) -> Path | None:
    """`<git-common-dir>/agent-flow/worktrees/<name>/.agent-flow/runs/<run>/`, where the runner keeps a run's state."""
    run_id = str(status.get("run") or "").rsplit("/", 1)[-1]
    common = _common_dir(worktree)
    return (common / "agent-flow" / "worktrees" / worktree.name / ".agent-flow" / "runs" / run_id
            if common and run_id else None)


def phase_artifact(worktree: Path, status: dict, phase: str) -> Path | None:
    """A phase's artifact: `<run state>/<phase>.md`, where the runner writes it (`Runner._artifact_rel`); a phase that
    declares another artifact name keeps it next to that, so the directory is searched for the phase's own name only.
    (The harness looked in `<run>/artifacts/<phase>.md` before v3.14, where the runner keeps only ledgers, so the
    re-review evidence and the grill check never found an artifact.)"""
    directory = run_state_dir(worktree, status)
    return directory / f"{phase}.md" if directory is not None else None


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


def open_findings(worktree: Path, status: dict) -> list[dict]:
    """Must-fix and should-fix items of the reviewers that asked for changes in the capped phase's last round, read
    from the per-reviewer artifacts that `<phase>-review-results.json` lists next to the phase artifact."""
    phase = status["fix_loop_cap"]["phase"]
    artifact = phase_artifact(worktree, status, phase)
    if artifact is None:
        return []
    results = artifact.parent / f"{phase}-review-results.json"
    try:
        outcomes = json.loads(results.read_text(encoding="utf-8")).get("outcomes") or []
    except (OSError, ValueError):
        return []
    items: list[dict] = []
    for outcome in outcomes:
        if outcome.get("verdict") != "request-changes" or not outcome.get("artifact"):
            continue
        path = results.parent / outcome["artifact"]
        if not path.is_file():
            continue
        for item in finding_split.parse_findings(path.read_text(encoding="utf-8", errors="replace")):
            if item["section"] in ("must-fix", "should-fix"):
                items.append({"job_id": outcome.get("job_id"), "section": item["section"],
                              "severity": item["severity"], "text": item["text"],
                              "sha": finding_split.finding_sha(item["text"])})
    return list({item["sha"]: item for item in items}.values())


def label_open_findings(findings: list[dict], timeout: float, budget: float = None,
                        env: dict[str, str] | None = None) -> dict:
    """Labels from the frozen classifier, its cost, and the error if it failed. Never raises.

    The classifier is a scoring tool (Opus 5.5 medium, no tools), run with the controller's environment and at most
    `budget` (what the run's budget ledger has left, capped at REVIEW_CLASSIFIER_BUDGET_USD) and `timeout`."""
    if not findings:
        return {"findings": [], "cost_usd": 0.0, "error": "no-open-findings"}
    limit = REVIEW_CLASSIFIER_BUDGET_USD if budget is None else min(REVIEW_CLASSIFIER_BUDGET_USD, budget)
    if limit <= 0 or timeout <= 0:
        return {"findings": [], "cost_usd": 0.0, "error": "no-budget-or-time-left"}

    def runner(prompt: str, system: str) -> dict:
        return finding_split.run_claude(prompt, system, timeout=int(max(1, min(600, timeout))), env=env)

    classifier = finding_split.Classifier(None, runner=runner, budget=limit)
    try:
        labels = classifier.label(findings)
        error = None if all(labels[item["sha"]]["label"] for item in findings) else "unlabelled-findings"
    except Exception as failure:  # noqa: BLE001 - any classifier failure falls back to the old re-review
        labels, error = {}, f"{type(failure).__name__}: {failure}"[:300]
    labelled = [{**item, **labels.get(item["sha"], {"label": None, "why": None})} for item in findings]
    return {"findings": labelled, "cost_usd": round(classifier.spent, 4), "error": error,
            "prompt_sha256": finding_split.prompt_sha(finding_split.LABEL_PROMPT)}


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


def approve_phase(project: Path, status: dict, call=None) -> bool:
    """Runs the runner's own approval command as the user would. The agent never runs it (#409 P4). Advancing the
    run is the point of this call, so it is the one place the harness runs `continue`."""
    command = status.get("approval_command")
    if not isinstance(command, str) or not command.startswith("agent-flow continue ") or "--approve" not in command:
        return False
    argv = [str(project / ".agent-flow" / "bin" / "agent-flow"), *shlex.split(command)[1:]]
    cwd = Path(status["worktree"])
    done = call(argv, cwd) if call is not None else runner_call(None, None, argv, cwd, 600)
    return done.returncode == 0


def _controller_once(argv: list[str], project: Path, stream, err, timeout: float, box: dict | None = None,
                     reports: list[dict] | None = None, wrapper: Path | None = None) -> tuple[int, bool]:
    """One controller process. With `box` it is the namespace's only child; when it ends, everything it left running
    is killed with the namespace (`reports` keeps what was killed and any survivor)."""
    if box is not None:
        result = sandbox.run({**box, "timeout": max(timeout, 1)}, argv, stdout=stream, stderr=err)
        if reports is not None:
            reports.append(result)
        code = result["returncode"] if isinstance(result["returncode"], int) else 125
        return code, result["timed_out"]
    env = claude_usage.with_wrapper(controller_env(dict(os.environ)), wrapper)
    proc = subprocess.Popen(argv, cwd=project, env=env, stdout=stream, stderr=err,
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


def cli_version() -> str | None:
    """The claude CLI's version number (`2.1.310` of `2.1.310 (Claude Code)`)."""
    try:
        shown = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=60,
                               env=controller_env(dict(os.environ)), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    words = (shown.stdout or "").split()
    return words[0] if shown.returncode == 0 and words else None


def kit_reviewer_cost(run: Path, reviewers: dict | None) -> dict:
    """The kit's reviewers from the usage wrapper's rows (`claude_usage`) against the jobs the kit's metrics list.

    `cost_usd` is the rows' summed cost when every claude reviewer job has exactly one priced row (counts match) and
    there is no other provider's job; otherwise None (unpriced). `seen_usd` is what the rows that arrived cost, a lower
    bound either way."""
    usage = claude_usage.summary(claude_usage.read_rows(run / claude_usage.LOG))
    jobs = reviewers["jobs"] if reviewers else 0
    claude_jobs = ((reviewers or {}).get("providers") or {}).get("claude", {}).get("jobs", 0)
    matched = usage["rows"] == jobs == claude_jobs and not usage["unpriced"]
    return {"jobs": jobs, "rows": usage["rows"], "rows_unpriced": usage["unpriced"], "matched": matched,
            "cost_usd": round(usage["cost_usd"], 4) if matched else (0.0 if jobs == usage["rows"] == 0 else None),
            "seen_usd": round(usage["cost_usd"], 4), "models": usage["models"], "efforts": usage["efforts"],
            "text_inexact": usage["text_inexact"]}


def budget_ledger(run: Path, meta: dict, controller: float | None, classifier: float, project: Path,
                  checkout: Path | None) -> dict:
    """Every participating call's cost (#409 v3.14 B): the controller, the kit's reviewers, arm V's gate reviewer,
    arm R's router, the simulated user and the harness's review classifier. The kit's reviewers are priced from the
    usage wrapper's ledger (`kit_reviewer_cost`); `total_cost_usd` is null and `cost_complete` false when any part is
    unpriced (a reviewer job without its row); `enforced_usd` is what the run cap is checked against: every priced part
    plus, while the reviewers are unpriced, the larger of their rows' cost and their byte lower bound."""
    arm = effective_arm(meta)
    gate = verify_gate.summary(run / verify_gate.LOG)["reviewer_cost_usd"] if arm in GATE_ARMS else 0.0
    routed = meta.get("router") or {}
    router_cost = routed.get("cost_usd") if routed else 0.0
    simulator = user_sim.summary(run / user_sim.LOG)["user_sim_cost_usd"] if meta.get("user_sim") else 0.0
    reviewers = reviewer_record(project, checkout or project) if arm in WORKFLOWS else None
    kit_usage = kit_reviewer_cost(run, reviewers)
    kit = kit_usage["cost_usd"]
    kit_floor = max(reviewers["priced_cost_lower_bound_usd"] if reviewers else 0.0, kit_usage["seen_usd"])
    parts = {"controller": controller, "kit_reviewers": kit, "verify_gate": gate, "router": router_cost,
             "simulator": simulator, "review_classifier": classifier}
    unpriced = sorted(key for key, value in parts.items() if value is None)
    known = sum(value for value in parts.values() if value is not None)
    return {"components": parts, "unpriced": unpriced, "kit_reviewer_jobs": kit_usage["jobs"],
            "kit_reviewer_rows": kit_usage["rows"], "kit_reviewer_rows_unpriced": kit_usage["rows_unpriced"],
            "kit_reviewer_ledger_usd": kit_usage["seen_usd"],
            "kit_reviewer_cost_lower_bound_usd": kit_floor, "cost_complete": not unpriced,
            "total_cost_usd": None if unpriced else round(known, 4),
            "enforced_usd": round(known + (kit_floor if kit is None else 0.0), 4)}


PROVENANCE = re.compile(r"^- (provider|model|effort): (\S+)\s*$", re.M)


def kit_review_identities(project: Path, checkout: Path) -> list[dict]:
    """The provider, model and effort of every kit reviewer: from each review artifact's provenance header and each
    `review-metrics-*.json` job (the archive of earlier chain steps left out)."""
    found = []
    for path in state_files(project, checkout, "*.md"):
        text = path.read_text(encoding="utf-8", errors="replace")
        values = dict(PROVENANCE.findall(text[:4000]))
        if "provider" in values:
            found.append({"source": path.name, "provider": values.get("provider"), "model": values.get("model"),
                          "effort": values.get("effort")})
    for path in state_files(project, checkout, "review-metrics-*.json"):
        try:
            jobs = json.loads(path.read_text(encoding="utf-8")).get("jobs") or []
        except (OSError, ValueError):
            found.append({"source": path.name, "provider": None, "model": None, "effort": None})
            continue
        found += [{"source": f"{path.name}:{job.get('job_id')}", "provider": job.get("provider"),
                   "model": job.get("model"), "effort": job.get("effort")} for job in jobs if isinstance(job, dict)]
    return found


def verify_pins(run: Path, meta: dict, project: Path, checkout: Path, init: dict | None, rows: list[dict],
                pinned_model: str = PINNED_MODEL, pinned_effort: str = PINNED_EFFORT) -> dict:
    """Every participating call's model and effort against the pin; `mismatches` non-empty is a harness error."""
    arm = effective_arm(meta)
    mismatches: list[str] = []

    def expect(where: str, model, effort) -> None:
        if model != pinned_model or (effort is not None and effort != pinned_effort):
            mismatches.append(f"{where}: {model}/{effort}")

    if init is not None:
        expect("controller", init.get("model"), None)
    for model in sorted({message["model"] for message in run_usage.claude_messages(rows)
                         if message.get("model") and message["model"] != "<synthetic>"}):
        expect("transcript", model, None)
    reviewers = kit_review_identities(project, checkout) if arm in WORKFLOWS else []
    for item in reviewers:
        if item["provider"] != "claude":
            mismatches.append(f"kit reviewer {item['source']}: provider {item['provider']}")
        expect(f"kit reviewer {item['source']}", item["model"], item["effort"] or "unspecified")
    calls = [row for row in claude_usage.read_rows(run / claude_usage.LOG) if row.get("kind") == "kit-reviewer"]
    for number, row in enumerate(calls, start=1):
        expect(f"kit reviewer call {number}", row.get("requested_model"), row.get("effort") or "unspecified")
        for used in row.get("models") or []:
            expect(f"kit reviewer call {number} (served)", used, None)
    if arm in GATE_ARMS:
        config = json.loads((run / "gate" / "config.json").read_text(encoding="utf-8"))
        expect("verify gate config", config.get("model"), config.get("effort") or "unspecified")
        for row in verify_gate.read_events(run / verify_gate.LOG):
            if row.get("step") == "review":
                expect("verify gate review", row.get("model"), row.get("effort") or "unspecified")
    routed = meta.get("router")
    if routed:
        expect("router", routed.get("model"), routed.get("effort") or "unspecified")
    if meta.get("user_sim"):
        config = json.loads((run / user_sim.DIR / user_sim.CONFIG).read_text(encoding="utf-8"))
        expect("simulator config", config.get("model"), config.get("effort") or "unspecified")
        for number, row in enumerate(user_sim.read_log(run / user_sim.LOG), start=1):
            expect(f"simulator call {number}", row.get("model"), row.get("effort") or "unspecified")
            for used in row.get("models_used") or []:
                expect(f"simulator call {number} (served)", used, None)
    return {"model": pinned_model, "effort": pinned_effort, "kit_reviewers_checked": len(reviewers),
            "kit_reviewer_calls_checked": len(calls), "mismatches": mismatches}


def base_skills(init: dict | None, project: Path) -> dict | None:
    """The CLI's own skills the controller listed, after the project's `.claude/skills` (an arm's treatment)."""
    if not init or not isinstance(init.get("skills"), list):
        return None
    local = {path.parent.name for path in (project / PROJECT_SKILLS).glob("*/SKILL.md")}
    listed = sorted(str(name) for name in init["skills"])
    base = sorted(name for name in listed if name not in local)
    # 같은 이름의 project 스킬이 CLI 기본 스킬을 가린 것은 arm의 처치다(VK의 kit `code-review` 등). 빠진 것으로 세지 않는다.
    shadowed = sorted(set(BASE_SKILLS) & local & set(listed))
    missing = sorted(set(BASE_SKILLS) - set(base) - set(shadowed))
    return {"listed": len(listed), "project": sorted(local & set(listed)), "base": base, "shadowed": shadowed,
            "matches_pin": not missing and not (set(base) - set(BASE_SKILLS)),
            "missing": missing, "extra": sorted(set(base) - set(BASE_SKILLS))}


def recorded_checkout(run: Path, meta: dict, driven: dict) -> tuple[Path, dict]:
    """The checkout `drive` recorded for a workflow arm (host path and HEAD); SystemExit when it is missing, so a
    workflow run is never graded on the leader project by default."""
    project = Path(meta["project"])
    if effective_arm(meta) not in WORKFLOWS:
        return project, {"checkout_selected_by": "project"}
    record = driven.get("checkout")
    if not isinstance(record, dict) or not record.get("host"):
        raise SystemExit("checkout: the workflow run's drive.json records no checkout (worktree path and HEAD)")
    return Path(record["host"]), record


def outage_seen(run: Path, meta: dict, stream: Path, classifier_failures: list[dict]) -> str | None:
    """A usage-limit or service outage any participating call reported (`outage`): the controller's stream and
    stderr, the kit's reviewers (usage wrapper rows), arm R's router, the simulated user, the review classifier."""
    found = outage.from_stream(run_usage._jsonl(stream))
    if found:
        return f"controller {found}"
    try:
        found = outage.from_text((run / "controller.stderr").read_text(encoding="utf-8", errors="replace")[-20000:])
    except OSError:
        found = None
    if found:
        return f"controller {found}"
    sources = [("kit reviewer", row.get("error_text")) for row in claude_usage.read_rows(run / claude_usage.LOG)]
    sources += [("router", error) for error in (meta.get("router") or {}).get("errors") or []]
    if meta.get("user_sim"):
        sources += [("simulator", row.get("error")) for row in user_sim.read_log(run / user_sim.LOG)]
    sources += [("review classifier", item.get("error")) for item in classifier_failures]
    for where, text in sources:
        found = outage.from_text(text)
        if found:
            return f"{where} {found}"
    return None


def drive(run: Path, model: str, timeout: int, budget: float | None, cli: str | None = PINNED_CLI_VERSION) -> dict:
    """Runs the controller on `prompt.md` in the run sandbox; at a phase approval the harness approves and resumes the
    same session.

    At a stop at the fix-loop limit the harness labels the open findings (`label_open_findings`). An `incomplete`
    finding ends the run (`review_stop`). Otherwise the same session is resumed to fix exactly those findings, at most
    `MAX_FIX_EXTENSIONS` times (`fix_extensions`). When the classifier fails or no findings parse, it falls back to
    the old rule: at most `MAX_FIX_LOOP_REAPPROVALS` re-reviews without code changes (`RESUME_AFTER_FIX_LOOP_CAP`).

    One deadline covers the run from prepare's router call to the snapshot (`timeout` seconds of active time: the
    router's seconds, then every controller turn and harness call). One budget covers every participating call
    (`budget_ledger`): it is checked before each resume and before the classifier, and the controller's own cap is what
    the other parts left. When the run ends the harness freezes the graded tree (`snapshot.freeze`), checks every call's
    model and effort (`verify_pins`), the system tripwire and the sandbox reports.
    """
    meta = json.loads((run / "arm.json").read_text(encoding="utf-8"))
    check_not_moved(run, meta)
    project = Path(meta["project"])
    prompt = (run / "prompt.md").read_text(encoding="utf-8")
    stream = run / "controller.jsonl"
    arm = effective_arm(meta)
    routed = meta.get("router")
    if routed and routed.get("model") != model:
        raise SystemExit(f"arm R's router ran on {routed.get('model')}; drive the controller on the same model")
    if model != PINNED_MODEL:
        raise SystemExit(f"the controller runs on the pinned {PINNED_MODEL}, not {model}")
    chained = meta.get("chain") or {}
    if chained.get("model") and chained["model"] != model:
        raise SystemExit(f"chain step {chained['step']} continues a run driven on {chained['model']}; "
                         "drive it on the same model")
    if chained and chained.get("effort") not in (None, PINNED_EFFORT):
        raise SystemExit(f"chain step {chained['step']} continues a run with effort {chained.get('effort')}; "
                         f"this harness drives at {PINNED_EFFORT}")
    if (run / "drive.json").exists() or (run / snapshot.DIR).exists():
        raise SystemExit("this run was driven already; a run directory is driven once (prepare a new attempt)")
    observed = cli_version()
    if cli is not None and observed != cli:
        raise SystemExit(f"cli-version: claude is {observed}, the pinned version is {cli}; stop before the drive")
    mode = sandbox_mode()
    if mode == "required" and not sandbox.available():
        raise SystemExit("sandbox: the run sandbox (unshare, root) is not available here")
    layout = sandbox.prepare_layout(run) if mode == "required" else None
    home = layout["home"] if layout else None
    if arm in GATE_ARMS:
        set_gate_model(run, model)
    simulated = meta.get("user_sim")
    if simulated:
        user_sim.set_model(run, model)
    gate_config_sha = verify_gate.file_sha256(run / "gate" / "config.json") if arm in GATE_ARMS else None
    tripwire_before = sandbox.tripwire()
    usage_socket = install_usage_wrapper(run)
    reports: list[dict] = []
    router_seconds = float(routed.get("seconds") or 0) if routed and not routed.get("reused") else 0.0
    started = time.monotonic()

    def remaining() -> float:
        return timeout - router_seconds - (time.monotonic() - started)

    def call(argv: list[str], cwd: Path):
        return runner_call(run if mode == "required" else None, meta, argv, cwd, min(600.0, remaining()), reports)

    grill_checks: list[dict] = []
    approvals: list[str] = []
    reapprovals: list[dict] = []
    extensions: list[dict] = []
    classifier_failures: list[dict] = []
    review_stop: dict | None = None
    classifier_cost = 0.0
    cap_stop = budget_stop = deadline_stop = False
    controller_seconds = 0.0
    session, spent, exit_code, timed_out, summary = None, 0.0, None, False, {}
    sandbox_error = outage_detail = None
    with _Lock(exclusive=False), claude_usage.Collector(usage_socket, run / claude_usage.LOG), \
            stream.open("wb") as out, (run / "controller.stderr").open("wb") as err:
        message = prompt
        while True:
            ledger = budget_ledger(run, meta, spent, classifier_cost, project, project)
            if budget is not None and ledger["enforced_usd"] >= budget:
                budget_stop = True
                break
            left = remaining()
            if left <= 0:
                deadline_stop = True
                break
            # 재개한 세션의 total_cost_usd는 세션 누계다(확인함). controller의 상한은 다른 부분이 쓰고 남은 몫이다.
            others = ledger["enforced_usd"] - (spent or 0.0)
            argv = controller_argv(message, model, None if budget is None else max(budget - others, 0.01))
            if session:
                argv += ["--resume", session]
            box = run_box(run, meta, project, left, "controller") if mode == "required" else None
            turn_started = time.monotonic()
            try:
                exit_code, timed_out = _controller_once(argv, project, out, err, left, box=box, reports=reports,
                                                        wrapper=usage_bin(run))
            except sandbox.SandboxError as failure:
                sandbox_error = str(failure)
                break
            finally:
                controller_seconds += time.monotonic() - turn_started
            out.flush()
            summary = _stream_summary(stream)
            session = summary["session_id"] or session
            spent = summary["cost_usd"] or spent
            err.flush()
            outage_detail = outage_seen(run, meta, stream, classifier_failures)
            if outage_detail:
                break
            if timed_out or arm not in WORKFLOWS or len(approvals) >= MAX_HARNESS_APPROVALS:
                break
            # 완료를 선언한 run은 건드리지 않는다. 관측은 읽기 전용 `status`로 한다.
            if completion.classify(summary.get("final_text")) == completion.COMPLETE:
                _settle_reapproval(reapprovals, passed=True)
                _settle_reapproval(extensions, passed=True)
                cap_stop = False
                break
            status = runner_status(project, home, call)
            cap = (status or {}).get("fix_loop_cap")
            if status is not None:
                _settle_reapproval(reapprovals, passed=not cap)
                _settle_reapproval(extensions, passed=not cap)
            if cap:
                cap_stop = True
                worktree = _status_worktree(status)
                ledger = budget_ledger(run, meta, spent, classifier_cost, project, project)
                labelled = label_open_findings(open_findings(worktree, status), remaining(),
                                               None if budget is None else budget - ledger["enforced_usd"],
                                               env={**controller_env(dict(os.environ)),
                                                    "CLAUDE_CODE_EFFORT_LEVEL": finding_split.EFFORT})
                classifier_cost += labelled["cost_usd"]
                if labelled["error"] == "no-budget-or-time-left":
                    budget_stop = remaining() > 0
                    deadline_stop = not budget_stop
                    break
                if labelled["error"] is None:
                    findings = labelled["findings"]
                    unfinished = [item for item in findings if item["label"] == "incomplete"]
                    if unfinished:
                        review_stop = {"verdict": "incomplete", **cap, "findings": findings,
                                       "incomplete": len(unfinished)}
                        break
                    if len(extensions) >= MAX_FIX_EXTENSIONS:
                        break
                    extensions.append({**cap, "findings": findings, "passed": None})
                    shown = "\n".join(f"- [{item['label']}] {item['text']}" for item in findings[:OPEN_FINDINGS_SHOWN])
                    message = RESUME_TO_FIX_FINDINGS.format(phase=cap["phase"], findings=shown)
                    continue
                classifier_failures.append({**cap, "error": labelled["error"], "findings": len(labelled["findings"])})
                if len(reapprovals) >= MAX_FIX_LOOP_REAPPROVALS:
                    break
                reapprovals.append({**cap, **review_findings(worktree, status), "passed": None})
                message = RESUME_AFTER_FIX_LOOP_CAP.format(phase=cap["phase"])
                continue
            cap_stop = False
            if not status or status.get("reason") != "phase_approval_required":
                break
            if simulated and status.get("current_phase") == GRILL_PHASE:
                check = grill_check(_status_worktree(status), status, run / user_sim.LOG)
                grill_checks.append(check)
                if check["passed"] is False and sum(item["passed"] is False for item in grill_checks) <= MAX_GRILL_RECHECKS:
                    message = RESUME_AFTER_GRILL_CHECK.format(rounds=check["user_rounds"], calls=check["ask_user_calls"],
                                                              script=simulated["ask_user"])
                    continue
            if not approve_phase(project, status, call):
                break
            approvals.append(str(status.get("current_phase")))
            message = RESUME_AFTER_APPROVAL.format(phase=status.get("current_phase"))
    transcript = session_transcript(session, layout["projects"]) if layout else session_transcript(session)
    init = summary.pop("init", None) or _stream_init(stream)
    summary.pop("final_text", None)
    checkout, chosen = select_checkout(project, arm, home)
    head = _git(["rev-parse", "HEAD"], checkout) if (checkout / ".git").exists() else None
    checkout_record = {"host": str(checkout), "inside": str(sandbox.to_inside(checkout, home) if home else checkout),
                       "head": head or None, **chosen}
    frozen, freeze_error = None, None
    if not chosen["checkout_error"]:
        try:
            frozen = snapshot.freeze(checkout, run, meta.get("start_commit"), checkout_record["inside"])
        except (snapshot.SnapshotError, OSError) as failure:
            freeze_error = str(failure)
    _, rows, _ = session_rows(transcript)
    pins = verify_pins(run, meta, project, checkout, init, rows)
    skills = base_skills(init, project)
    changed = sandbox.tripwire_changed(tripwire_before, sandbox.tripwire())
    survivors = [item for report in reports for item in report.get("survivors") or []]
    setup_errors = [report["error"] for report in reports if report.get("error")]
    outage_detail = outage_detail or outage_seen(run, meta, stream, classifier_failures)
    harness_error = next((reason for reason in (
        f"infra-outage: {outage_detail}" if outage_detail else None,
        f"sandbox: {sandbox_error}" if sandbox_error else None,
        f"sandbox: {len(survivors)} process(es) outlived their namespace" if survivors else None,
        f"sandbox: {setup_errors[0]}" if setup_errors else None,
        f"model-pin: {'; '.join(pins['mismatches'][:5])}" if pins["mismatches"] else None,
        f"checkout: {chosen['checkout_error']}" if chosen["checkout_error"] else None,
        f"snapshot: {freeze_error}" if freeze_error else None,
        (f"base-skills: missing {skills['missing']}, extra {skills['extra']}"
         if skills is not None and not skills["matches_pin"] and mode == "required" else None)) if reason), None)
    ledger = budget_ledger(run, meta, spent or None, classifier_cost, project, checkout)
    end_to_end = router_seconds + (time.monotonic() - started)
    record = {"model": model, "effort": PINNED_EFFORT, "timeout_seconds": timeout, "budget_usd": budget,
              "exit": exit_code, "timed_out": timed_out or deadline_stop, "deadline_stop": deadline_stop,
              "budget_stop": budget_stop, "seconds": round(end_to_end), "end_to_end_seconds": round(end_to_end),
              "controller_seconds": round(controller_seconds), "router_seconds": round(router_seconds),
              **summary, "session_id": session, "init": init,
              "cost_usd": spent or None, "budget_ledger": ledger,
              "kit_reviewer_usage": {"version": claude_usage.VERSION, "log": claude_usage.LOG,
                                     **claude_usage.summary(claude_usage.read_rows(run / claude_usage.LOG))},
              "harness_approvals": approvals,
              "fix_loop_reapprovals": reapprovals, "fix_loop_cap_stop": cap_stop,
              **({"fix_extensions": extensions, "review_stop": review_stop,
                  "review_classifier_failures": classifier_failures,
                  "review_classifier_cost_usd": round(classifier_cost, 4)} if arm in WORKFLOWS else {}),
              **({"grill_checks": grill_checks} if simulated else {}),
              **({"verify_gate": verify_gate.summary(run / verify_gate.LOG),
                  "gate_config_sha256": gate_config_sha} if arm in GATE_ARMS else {}),
              "transcript": str(transcript) if transcript else None,
              "argv_sha256": hashlib.sha256(json.dumps(controller_argv(prompt, model, budget)).encode()).hexdigest(),
              "tools": list(CLAUDE_TOOLS), "cli_version": observed, "cli_version_pinned": cli,
              "sandbox": {"mode": mode, "version": sandbox.VERSION, "calls": len(reports),
                          "killed_at_exit": [item for report in reports for item in report.get("killed_at_exit") or []],
                          "survivors": survivors, "errors": setup_errors,
                          "checks": next((report.get("checks") for report in reports if report.get("checks")), None)},
              "tripwire_changed": changed, "checkout": checkout_record, "snapshot": frozen, "pins": pins,
              "base_skills": skills, "harness_error": harness_error, "outage": outage_detail, "driven_at": _now()}
    (run / "drive.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    if outage_detail:
        record["outage_marker"] = str(outage.mark(run.parent, run, outage_detail))
    return record


def set_gate_model(run: Path, model: str) -> None:
    """Arm V's reviewer runs on the controller's model and the pinned effort, which only `drive` sets."""
    path = run / "gate" / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**config, "model": model, "effort": PINNED_EFFORT}, indent=1) + "\n", encoding="utf-8")


def gate_result(run: Path, meta: dict, checkout: Path, driven: dict | None = None) -> dict:
    """What arm V's gate did, read from `verify-gate.jsonl`; whether the agent left its hook wiring as installed; and
    whether the gate's script and config are the ones prepare and drive wrote (`integrity_intact`)."""
    settings = checkout / GATE_SETTINGS
    intact = settings.is_file() and verify_gate.file_sha256(settings) == meta["verify_gate"]["settings_sha256"]
    script = Path(meta["verify_gate"]["script"])
    config = run / "gate" / "config.json"
    expected_config = (driven or {}).get("gate_config_sha256")
    script_ok = script.is_file() and verify_gate.file_sha256(script) == meta["verify_gate"]["script_sha256"]
    config_ok = expected_config is None or (config.is_file() and verify_gate.file_sha256(config) == expected_config)
    return {**verify_gate.summary(run / verify_gate.LOG), "wiring_intact": intact,
            "integrity_intact": script_ok and config_ok, "script_intact": script_ok, "config_intact": config_ok}


def _settle_reapproval(reapprovals: list[dict], *, passed: bool) -> None:
    """The last re-approval passed if the run did not stop at the cap again after it."""
    if reapprovals and reapprovals[-1]["passed"] is None:
        reapprovals[-1]["passed"] = passed


def run_status(row: dict) -> str:
    """Why a run cannot count as an ordinary attempt, or `ok`.

    - harness-error: the harness itself was broken (marked with `score --harness-error`). The attempt was re-run
      under the fixed harness, so it leaves the denominator and is listed apart.
    - contaminated, infra-error (the controller or the evaluator failed), timed-out, gate-error (arm V's review could
      not judge, so the treatment was not applied, #409 v3.14 D): assigned attempts. They stay in the denominator and
      count as failures (PREREGISTRATION.md, "제외").
    """
    if row.get("harness_error"):
        return "harness-error"
    if attempts.contaminated(row):
        return "contaminated"
    if row.get("drive_timed_out"):
        return "timed-out"
    if row.get("drive_is_error") or (row.get("drive_exit") not in (None, 0)) or (row.get("ui") or {}).get("error"):
        return "infra-error"
    if (row.get("verify_gate") or {}).get("gate_errors"):
        return "gate-error"
    return "ok"


def _succeeded(row: dict) -> bool:
    """PREREGISTRATION.md: S=1 and a completion declaration and no rule violation and not contaminated."""
    return (bool(row["oracle"]) and row["declaration"] == completion.COMPLETE and not rule_violation(row)
            and run_status(row) == "ok")


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


STATUS_COLUMNS = ("contaminated", "infra-error", "timed-out")


def stratum(row: dict) -> tuple[str, str, int]:
    """(case, layer, chain step): results of different strata are never pooled (#409 v3.14 E)."""
    return (str(row.get("case")), str(row.get("layer") or "-"), int((row.get("chain") or {}).get("step") or 1))


def summarize(results: list[dict], assignment: list[dict] | None = None) -> str:
    """One table set per stratum (case × layer × chain step); the Fisher tests compare arms within a stratum only.
    With `assignment` (the pre-registered list of attempts) the missing, duplicate and unassigned attempts follow."""
    strata = sorted({stratum(row) for row in results})
    if len(strata) <= 1:
        text = _summarize_stratum(results)
    else:
        parts = []
        for key in strata:
            group = [row for row in results if stratum(row) == key]
            parts.append(f"## case {key[0]}, layer {key[1]}, chain step {key[2]}\n\n" + _summarize_stratum(group))
        text = "\n".join(parts)
    lines = (_chain_lines(results) + _exclusion_lines(results)
             + (attempts.check_lines(assignment, results) if assignment is not None else []))
    return text + ("\n".join(lines) + "\n" if lines else "")


def _exclusion_lines(results: list[dict]) -> list[str]:
    excluded = [row for row in results if run_status(row) == "harness-error"]
    if not excluded:
        return []
    lines = ["", "Exclusions by attempt (fixed taxonomy, `attempts.EXCLUSIONS`):", "",
             "| attempt | case / layer / step | arm | code | reason |", "|---|---|---|---|---|"]
    for row in sorted(excluded, key=attempts.attempt_id):
        key = stratum(row)
        lines.append(f"| {attempts.attempt_id(row)} | {key[0]} / {key[1]} / {key[2]} | {row.get('arm')} | "
                     f"{attempts.exclusion_code(row['harness_error'])} | {row['harness_error']} |")
    return lines


def _summarize_stratum(results: list[dict]) -> str:
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
             "skills invoked (runs that invoked any: name×calls) | false complete / false block | "
             "no-start / complete builds (no missing check) | gate errors | cost complete |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
             "---|---|"]
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
    return "\n".join(lines) + "\n"


def _run_dirs(row: dict) -> set[str]:
    """The run directory a result belongs to, as recorded and, for a moved run, as scored."""
    return {str(Path(row[key]).parent) for key in ("project", "project_recorded") if row.get(key)}


def chain_survived(steps: dict[int, dict]) -> int:
    """Consecutive successful steps from step 1, by the success definition (`_succeeded`: S=1, declared complete,
    no rule violation, an ordinary run), so a step that met the oracle but declared blocked or broke a rule ends it."""
    survived = 0
    while (row := steps.get(survived + 1)) is not None and _succeeded(row):
        survived += 1
    return survived


def chain_functional_survived(steps: dict[int, dict]) -> int:
    """Consecutive steps with S=1 in an ordinary run, whatever was declared: the functional survival, named apart."""
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
             "| root run | arm | steps | per-step S / G | steps survived (success) | functional survival (S=1) |",
             "|---|---|---|---|---|---|"]
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
                     f"{chain_survived(steps)}/{max(steps)} | {chain_functional_survived(steps)}/{max(steps)} |")
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
        _reviewer_cell(group) if workflow else "-", _skills_cell(group), _false_cell(group), _build_cell(group),
        str(sum(run_status(row) == "gate-error" for row in group)) if gated else "-",
        f"{sum(bool(row.get('cost_complete')) for row in group)}/{len(group)}"]) + " |"


def _false_cell(group: list[dict]) -> str:
    """False complete (declared complete, S=0) over declared complete, always beside false block (declared blocked,
    S=1) over declared blocked: a blocked declaration avoids a false complete for free (#409 v3.14 E)."""
    complete = [row for row in group if row.get("declaration") == completion.COMPLETE]
    blocked = [row for row in group if row.get("declaration") == completion.BLOCKED]
    wrong = sum(not row.get("oracle") for row in complete)
    held = sum(bool(row.get("oracle")) for row in blocked)
    return f"{wrong}/{len(complete)} / {held}/{len(blocked)}"


def _build_cell(group: list[dict]) -> str:
    measured = [row for row in group if row.get("no_start") is not None]
    if not measured:
        return "-"
    return (f"{sum(bool(row['no_start']) for row in measured)} / "
            f"{sum(bool(row.get('complete_build')) for row in measured)} of {len(measured)}")


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


def score_serial(run: Path, evaluator: Path | None, repeat: int = 3, case_dir: Path | None = None) -> dict:
    """One run of a serial scoring pass: `score` takes the exclusive lock, so runs are graded one at a time and never
    while a drive holds it; the evaluator gets `--browser-repeat BROWSER_REPEAT` where it has one."""
    return score(run, None, evaluator, repeat, None, case_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--kit", type=Path, required=True)
    prep.add_argument("--arm", choices=ARMS, required=True)
    prep.add_argument("--case", default="devflow", help=f"a phase case, or a UI task: {', '.join(UI_CASES)}")
    prep.add_argument("--trial", type=int, default=1)
    where = prep.add_mutually_exclusive_group(required=True)
    where.add_argument("--output", type=Path)
    where.add_argument("--runs-root", type=Path,
                       help="put the run in <root>/<random id> and record its assignment in <root>/ledger.jsonl")
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
    dr.add_argument("--cli-version", default=PINNED_CLI_VERSION, help="the claude CLI version the drive requires")
    sc = sub.add_parser("score")
    sc.add_argument("run", type=Path)
    sc.add_argument("--transcript", type=Path)
    sc.add_argument("--evaluator", type=Path, help="the private evaluator's score.py, for UI tasks")
    sc.add_argument("--repeat", type=int, default=3)
    sc.add_argument("--harness-error", metavar="CODE[: DETAIL]",
                    help=f"the harness itself broke this run (one of {', '.join(attempts.EXCLUSIONS)}); it is re-run "
                         "and listed apart from the denominator")
    sc.add_argument("--case-dir", type=Path, help="the case.json directory, for its canaries")
    sc.add_argument("--reference-build", action="store_true",
                    help="validation of a reference or mutant build: passed to the evaluator")
    ser = sub.add_parser("score-serial", help="score runs one after another, never while a drive runs")
    ser.add_argument("runs", type=Path, nargs="+")
    ser.add_argument("--evaluator", type=Path)
    ser.add_argument("--case-dir", type=Path)
    ser.add_argument("--repeat", type=int, default=3)
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
    summ.add_argument("--manifest", type=Path, help="the pre-registered assignment (JSON list of attempts)")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        name = (json.loads((args.case_dir / "case.json").read_text(encoding="utf-8"))["name"] if args.case_dir
                else args.case)
        output = args.output or attempts.new_run(args.runs_root.resolve(), case=name, layer=args.layer, arm=args.arm,
                                                 trial=args.trial)
        print(json.dumps(prepare(args.kit.resolve(), args.arm, args.case, args.trial, output.resolve(),
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
        record = drive(args.run.resolve(), args.model, args.timeout, args.budget, args.cli_version)
        print(json.dumps(record, indent=1))
        if record.get("outage"):
            # a usage limit or service outage: the run is an `infra-outage` exclusion and the campaign should pause
            print(f"infra-outage: {record['outage']}; marker {record['outage_marker']}", file=sys.stderr)
            return outage.EXIT_CODE
    elif args.command == "score-serial":
        for run in args.runs:
            result = score_serial(run.resolve(), args.evaluator, args.repeat, args.case_dir)
            print(json.dumps({"run": str(run), **{key: result.get(key) for key in ("arm", "oracle", "declaration",
                                                                                    "contaminated", "harness_error")}}))
    elif args.command == "score":
        result = score(args.run.resolve(), args.transcript, args.evaluator, args.repeat, args.harness_error,
                       args.case_dir.resolve() if args.case_dir else None, args.reference_build)
        print(json.dumps({key: result.get(key) for key in ("arm", "hidden_tests", "oracle", "declaration",
                                                             "contaminated", "commits", "usage", "seconds",
                                                             "artifact_sha256")}))
    else:
        assignment = json.loads(args.manifest.read_text(encoding="utf-8")) if args.manifest else None
        print(summarize([json.loads(path.read_text(encoding="utf-8")) for path in args.results], assignment), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
