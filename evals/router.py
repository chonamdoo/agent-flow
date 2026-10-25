"""Arm R's router: the model chooses the workflow (#409 program v2).

`arm_compare.prepare --arm R` seeds the project as every arm does, then runs this router once in it: one read-only
`claude -p` call (Read, Glob and Grep only) that gets the task text, is told to inspect the repository, and picks one
option from a fixed menu. Each option is an existing arm's setup; the menu text is fixed here and its sha256 is
recorded. The decision is frozen into `arm.json` before the chosen option is prepared, and the controller that then
does the task starts with a fresh context (the router keeps no session).

The answer's last non-empty line must be exactly a JSON object `{"choice": "<option>", "reason": "..."}`; the
`compose` option adds `"spec": {...}`, a point in `variants`' space (the dimensions on or off and the reviewer count)
that `variants.normalize` must accept (and the caller's `check_spec`, which tries it on the run's kit and profile),
and is prepared as arm S with that variant. An answer that does not parse, names
an option that is not on the menu, carries an invalid spec, or a call that fails is retried once; after a second
failure the router falls back to `none` and records `fallback: true`. Only the standard library and `variants` are used.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import variants

VERSION = "router-v3"
TIMEOUT = 600
BUDGET_USD = 2.0
MAX_ATTEMPTS = 2
FALLBACK = "none"
TOOLS = ("Read", "Glob", "Grep")
DENIED_TOOLS = ("Bash", "Edit", "MultiEdit", "Write", "NotebookEdit", "Task", "WebFetch", "WebSearch")
OUT_DIR = "router"

# The fixed options in menu order: name -> (the arm whose setup it is, its description). The descriptions say what
# each option does and give no cost or time figures: the only figures this harness has come from one small task, and
# options it has not measured would read as unknown next to measured ones, which would tilt the choice.
OPTIONS = {
    "none": ("A", (
        "No workflow. You get the task, the project's test command if it has one, and a completion declaration, "
        "and you work however you see fit. Nothing is installed in the project and there are no phases, reviewers "
        "or gates.")),
    "verify-only": ("V", (
        "No workflow, with a fixed completion check. You get the same prompt as `none`; when you try to finish, a "
        "hook runs the project's verification command and sends you back while it fails (at most 3 times), then one "
        "independent read-only reviewer compares the project with the task once and sends you back once with any "
        "unmet requirements. There are no phases.")),
    "verify-skills": ("VK", (
        "No workflow, with the same completion check as `verify-only`, and the Agent Flow kit's skills available as "
        "the project's Claude Code skills: each is listed by name and description, and you may load any of them when "
        "you judge it useful. Nothing tells you to use them, and there are no phases.")),
    "development": ("B", (
        "The Agent Flow kit installed with its `development` workflow, driven through its runner: explore, "
        "implement, review (independent reviewer subprocesses), qa, fix-loop and handoff.")),
    "default": ("D", (
        "The Agent Flow kit installed with its `default` workflow, driven through its runner: design, slice-plan, "
        "worktree, implement, comment-authoring, final-review, gates and fix-loop, then delivery phases (commit, "
        "push and pull request); the agent sets each phase's depth for the task. This run has no git remote, so it "
        "ends at the first phase that needs one.")),
    "full-feature": ("C", (
        "The Agent Flow kit installed with its `full-feature` workflow, driven through its runner: domain-grill, "
        "product-brief, prd, slice-plan, plan-review, ddd-design, worktree, red, green, refactor, "
        "comment-authoring, multi-review (several independent reviewers), architecture-review, gates and fix-loop, "
        "then delivery phases. This run has no git remote, so it ends at the first phase that needs one.")),
    "compose": ("S", None),  # its text is built from `variants`' space by `compose_description`
}
COMPOSE = "compose"

DIMENSION_TEXT = {"grill": "domain-grill", "planning": "planning phases", "design": "ddd-design",
                  "tdd": "red (tests are written with the code in green)", "architecture_review": "architecture-review"}
# 각 차원이 무엇을 하려고 있는지. 비용·시간 수치는 넣지 않는다(위 OPTIONS 주석과 같은 이유).
DIMENSION_PURPOSE = {
    "grill": ("interviews the user, in rounds, to settle requirements the task leaves undefined or ambiguous; useful "
              "when the spec is incomplete, redundant when it is complete"),
    "planning": ("writes a product brief and a PRD with checkable spec items, splits the work into slices and reviews "
                 "that plan; useful when the scope or the acceptance criteria still have to be written down or the "
                 "work split, redundant when the task already states them"),
    "design": ("models the domain terms, boundaries and layers before code; useful when the change adds or reshapes "
               "domain concepts, redundant when it fits the existing design"),
    "tdd": "writes failing tests before the code; without it, green writes the tests with the code",
    "architecture_review": ("reviews the change's structure against the design after multi-review; useful when the "
                            "change spans several modules or layers, redundant for a local change"),
}

PROMPT = """You are choosing how a coding task will be carried out in the repository in the current directory. \
You do not do the task yourself, and another agent with a fresh context will do it.

The task the agent will receive:

{task}

Inspect the repository (read-only: Read, Glob, Grep) as much as you need to judge the task's size and risk. Then \
choose exactly one of these options:

{menu}

End your answer with one line that is exactly a JSON object, with nothing after it:

{{"choice": "<option name>", "reason": "<at most 3 sentences>"}}

For `compose`, the object also has `"spec"`, as that option says.
"""


def compose_description() -> str:
    """The `compose` option's text: the same dimensions and reviewer counts as `variants`, in fixed words."""
    keys = "\n".join(f"  - `{key}` ({', '.join(phases)}): {DIMENSION_PURPOSE[key]}."
                      for key, phases in variants.DROPS.items())
    counts = " or ".join(str(count) for count in variants.REVIEWER_COUNTS)
    return ("The Agent Flow kit installed with a reduced form of `full-feature` that you assemble, driven through its "
            "runner. Choose it by adding `\"spec\": {...}` to your JSON line. Each of these keys is true to keep "
            f"those phases or false to drop them:\n{keys}\n"
            f"  - `reviewers` ({counts}): how many independent reviewers multi-review runs; leave it out for the "
            "profile's default number.\n"
            "A key you leave out keeps the `full-feature` value, except `grill`, which then takes the value you give "
            "`planning`; give `grill` to decide it on its own. Every other phase, the gates and the fix-loop are as in "
            "`full-feature`. This run has no git remote, so it ends at the first phase that needs one.")


def variant_description(spec: dict) -> str:
    """A menu variant's description, built from its spec in fixed words (no free text)."""
    values = variants.effective(spec)
    spec = variants.normalize(spec)
    dropped = [f"{DIMENSION_TEXT[key]} ({', '.join(variants.DROPS[key])})" if key == "planning" else DIMENSION_TEXT[key]
               for key in variants.DROPS if not values[key]]
    parts = [f"drops {', '.join(dropped)}" if dropped else "drops no phase"]
    if "reviewers" in spec:
        parts.append(f"runs {spec['reviewers']} reviewers in multi-review")
    return ("The Agent Flow kit installed with a reduced form of `full-feature`, driven through its runner: it "
            f"{' and '.join(parts)}; every other phase, the gates and the fix-loop are as in `full-feature`. This run "
            "has no git remote, so it ends at the first phase that needs one.")


def menu(variant_specs: list[dict] | None = None) -> list[dict]:
    """[{name, arm, description, variant?}] in menu order; a variant is named by `variants.name`."""
    items = [{"name": name, "arm": arm, "description": text if text is not None else compose_description()}
             for name, (arm, text) in OPTIONS.items()]
    for spec in variant_specs or []:
        name = variants.name(spec)
        if any(item["name"] == name for item in items):
            raise ValueError(f"menu variant {name} is listed twice")
        items.append({"name": name, "arm": "S", "description": variant_description(spec),
                      "variant": variants.normalize(spec)})
    return items


def menu_text(items: list[dict]) -> str:
    return "\n\n".join(f"- `{item['name']}`: {item['description']}" for item in items)


def menu_sha256(items: list[dict]) -> str:
    return hashlib.sha256(menu_text(items).encode("utf-8")).hexdigest()


def prompt(task: str, items: list[dict]) -> str:
    return PROMPT.format(task=task.strip(), menu=menu_text(items))


def argv(text: str, model: str) -> list[str]:
    # 어떤 설정 파일도 읽지 않고 세션도 남기지 않는다. 그래서 작업할 controller가 이 대화를 이어받을 수 없다.
    return ["claude", "-p", text, "--model", model, "--output-format", "json",
            "--tools", ",".join(TOOLS), "--allowedTools", ",".join(TOOLS),
            "--disallowedTools", ",".join(DENIED_TOOLS), "--permission-mode", "auto",
            "--setting-sources", "", "--settings", json.dumps({"disableAllHooks": True}),
            "--no-session-persistence", "--max-budget-usd", f"{BUDGET_USD:g}"]


def parse_choice(text: str | None, names: list[str],
                 check_spec: Callable[[dict], object] | None = None) -> tuple[dict | None, str | None]:
    """(decision, None) when the last non-empty line is exactly {"choice": <a menu name>, "reason": <text>}, plus
    `"spec"` that `variants.normalize` (and `check_spec`, which raises ValueError) accepts when the choice is `compose`
    (and only then)."""
    if not isinstance(text, str) or not text.strip():
        return None, "empty answer"
    last = text.strip().splitlines()[-1].strip()
    try:
        decision = json.loads(last)
    except ValueError:
        return None, "last line is not JSON"
    if not isinstance(decision, dict):
        return None, "last line is not an object with exactly `choice` and `reason`"
    composed = decision.get("choice") == COMPOSE and COMPOSE in names
    if set(decision) != ({"choice", "reason", "spec"} if composed else {"choice", "reason"}):
        return None, ("`compose` needs exactly `choice`, `reason` and `spec`" if composed else
                      "last line is not an object with exactly `choice` and `reason`")
    if not isinstance(decision["reason"], str) or not decision["reason"].strip():
        return None, "`reason` is not a non-empty string"
    if decision["choice"] not in names:
        return None, f"`choice` {decision['choice']!r} is not on the menu"
    if composed:
        if not isinstance(decision["spec"], dict):
            return None, "`spec` is not an object"
        try:
            variants.normalize(decision["spec"])
            if check_spec is not None:
                check_spec(decision["spec"])
        except ValueError as exc:
            return None, f"`spec` is invalid: {exc}"
    return decision, None


def _call(command: list[str], cwd: Path, env: dict[str, str], timeout: int) -> tuple[int | None, str, str, bool]:
    """(exit code, stdout, stderr, timed out). The whole process group is stopped at the timeout."""
    try:
        proc = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        return None, "", str(exc), False
    try:
        out, err = proc.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        timed_out = True
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), timed_out


def _attempt(number: int, text: str, model: str, project: Path, env: dict[str, str], out_dir: Path,
             names: list[str], check_spec: Callable[[dict], object] | None) -> dict:
    started = time.monotonic()
    code, stdout, stderr, timed_out = _call(argv(text, model), project, env, TIMEOUT)
    raw = out_dir / f"attempt-{number}.json"
    raw.write_text(stdout, encoding="utf-8")
    (out_dir / f"attempt-{number}.stderr").write_text(stderr, encoding="utf-8")
    try:
        payload = json.loads(stdout)
    except ValueError:
        payload = None
    payload = payload if isinstance(payload, dict) else {}
    answer = payload.get("result") if isinstance(payload.get("result"), str) else None
    if timed_out or code != 0 or payload.get("is_error") or answer is None:
        decision, error = None, ("timed out" if timed_out else f"call failed (exit {code})")
    else:
        decision, error = parse_choice(answer, names, check_spec)
    return {"attempt": number, "exit": code, "timed_out": timed_out, "seconds": round(time.monotonic() - started, 1),
            "cost_usd": payload.get("total_cost_usd"), "usage": payload.get("usage"),
            "num_turns": payload.get("num_turns"), "raw_output": str(raw.resolve()), "decision": decision,
            "error": error}


def route(task: str, project: Path, out_dir: Path, model: str, env: dict[str, str],
          variant_specs: list[dict] | None = None, check_spec: Callable[[dict], object] | None = None) -> dict:
    """Runs the router (at most `MAX_ATTEMPTS` calls) and returns the record `arm.json` keeps as `router`.

    `check_spec` raises ValueError for a `compose` spec the run cannot build (5 reviewers on a profile with fewer
    angles); that answer is retried like any other invalid one."""
    items = menu(variant_specs)
    names = [item["name"] for item in items]
    text = prompt(task, items)
    out_dir.mkdir(parents=True)
    (out_dir / "prompt.md").write_text(text, encoding="utf-8")
    attempts: list[dict] = []
    for number in range(1, MAX_ATTEMPTS + 1):
        attempts.append(_attempt(number, text, model, project, env, out_dir, names, check_spec))
        if attempts[-1]["decision"] is not None:
            break
    decision = attempts[-1]["decision"]
    choice = decision["choice"] if decision else FALLBACK
    chosen = next(item for item in items if item["name"] == choice)
    if choice == COMPOSE:
        chosen = {**chosen, "variant": variants.normalize(decision["spec"])}
    costs = [item["cost_usd"] for item in attempts]
    usage: dict[str, int] = {}
    for item in attempts:
        for key, value in (item["usage"] or {}).items():
            if isinstance(value, int):
                usage[key] = usage.get(key, 0) + value
    return {"version": VERSION, "model": model, "choice": choice, "effective_arm": chosen["arm"],
            **({"variant": chosen["variant"]} if "variant" in chosen else {}),
            **({"spec": decision["spec"], "variant_name": variants.name(decision["spec"])}
               if choice == COMPOSE else {}),
            "reason": decision["reason"] if decision else None, "fallback": decision is None,
            "attempts": len(attempts), "errors": [item["error"] for item in attempts if item["error"]],
            # 한 번이라도 비용을 보고하지 않았으면 합계는 0이나 부분합이 아니라 null이다.
            "cost_usd": round(sum(costs), 4) if None not in costs else None, "usage": usage or None,
            "seconds": round(sum(item["seconds"] for item in attempts), 1),
            "menu": names, "menu_sha256": menu_sha256(items),
            "prompt_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "raw_outputs": [item["raw_output"] for item in attempts], "attempt_log": attempts,
            "timeout_seconds": TIMEOUT, "budget_usd": BUDGET_USD, "tools": list(TOOLS)}
