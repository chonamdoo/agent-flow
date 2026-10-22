"""Condition C′ for #410: full-feature's procedure text as one prompt, without the runner (#427).

    python evals/c_prime.py --kit <kit> --project <installed project> --task "<task>" --output prompt.md

C − C′ is the effect of code enforcement (evidence checks, markers, gate routing, round caps);
C′ − A is the effect of the procedure text. So C′ keeps every phase prompt the runner would hand
the author, in order, and removes only what talks to the runner. Model calls: none.

Fixed rules (`RULES_VERSION` changes when any of them does):

- Phases: the full-feature normal path, minus runner-owned `gates`, the runner's checkout phases
  (`worktree`, `run-start`: they verify a binding only the runner creates, so without it the agent
  can only stop — the first smoke did exactly that; #429 moves them into the runner), the conditional fix phases
  (`fix-loop`, `pr-comment-fix`, `pr-ci-fix`) and the multi-review phases (`multi-review`,
  `architecture-review`). Review stays C's effect; a prompt-level review arm would be C′+R, a
  separate condition not built here. In their place the prompt says the current checkout and branch
  are the isolated worktree the profile requires (the second smoke tried `git worktree add` instead).
- Runner talk is removed: the run id, the project root, `## When complete` (status / next_command),
  and every line naming an `agent-flow` command, an approval token or `run_command_prefix`.
  Artifact targets become paths under `cprime/`.
- Pauses: no phase waits. Where full-feature pauses for the user, the prompt tells the agent to
  record `approval: agent` in that artifact (#413), so self-approval is visible in the output.
- A section identical to one already emitted (the profile YAML, the same skill bodies) is replaced
  by a reference to its first phase. The text the model receives is otherwise unchanged.
- The completion declaration is the arm-common rule of #424 (`EVAL-STATUS: complete|blocked: …`).

The output is deterministic for a kit and task: absolute paths are replaced, and the SHA-256 of the
prompt is printed and written next to it as the fingerprint.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RULES_VERSION = 4
EXCLUDED_PHASES = frozenset({"gates", "worktree", "run-start", "fix-loop", "pr-comment-fix", "pr-ci-fix",
                             "multi-review", "architecture-review"})
RUNNER_LINE = re.compile(r"agent-flow\s+[a-z]|--approve\b|approval_command|run_command_prefix|next_command|"
                         r"^\*\*(Run id|Project root)\*\*|\brunner\b|record-command-run|blocks advancement", re.I)
SECTION = re.compile(r"(?m)^(?=## )")
DECLARATION = """## Completion declaration

End your final message with exactly one line:

    EVAL-STATUS: complete
    EVAL-STATUS: blocked: <reason>

Declare `complete` only when every phase above is done and the change works; otherwise declare
`blocked` with the reason. Nothing checks your phase artifacts for you in this run.
"""


def _filter(envelope: str, phase_id: str, artifact: str, root: Path, pauses: bool) -> list[tuple[str, str]]:
    """(heading, text) sections of one envelope with runner talk removed."""
    # agent는 프로젝트 루트에서 일한다. 절대경로를 상대경로로 바꾸면 결과가 실행 위치와 무관해진다.
    text = envelope.replace(f"{root}/", "").replace(str(root), ".")
    text = re.sub(r"(?m)^# agent-flow phase: .*$", f"# Phase: {phase_id}", text)
    text = re.sub(r"(\*\*Artifact target\*\*[^\n]*\n\s*)`[^`]*`", lambda m: f"{m.group(1)}`cprime/{artifact}`", text)
    sections = []
    for part in SECTION.split(text):
        if not part.strip():
            continue
        heading = part.splitlines()[0].strip()
        if heading.startswith("## When complete"):
            continue
        kept = "\n".join(line for line in part.splitlines() if not RUNNER_LINE.search(line)).strip()
        sections.append((heading, kept))
    if pauses:
        sections.append(("## Approval", "## Approval\n\nThis phase would pause for the user. Do not wait: continue, "
                         "and record `approval: agent` in this phase's artifact."))
    return sections


def build(kit: Path, project: Path, task: str, stop_after: str | None = None) -> str:
    """Render in a copy so the caller's project keeps no run state and the path is never in the prompt."""
    with tempfile.TemporaryDirectory(prefix="af-cprime-") as scratch:
        copy = Path(scratch) / "project"
        shutil.copytree(project, copy, symlinks=True)
        return _build(kit, copy.resolve(), task, stop_after)


def _build(kit: Path, project: Path, task: str, stop_after: str | None) -> str:
    sys.path.insert(0, str(kit / "src"))
    os.environ["AGENT_FLOW_HOST"] = "claude"
    from agent_flow.adapters.hosted import HostedAdapter
    from agent_flow.artifact import create_run
    from agent_flow.core.local_skills import changed_files
    from agent_flow.runner import Runner

    run_dir = create_run(project, "full-feature", task, run_id="cprime")
    if stop_after is not None and stop_after not in {phase.id for phase in Runner(project, workflow="full-feature", run_dir=run_dir).phases}:
        raise ValueError(f"unknown phase for --stop-after: {stop_after}")
    if stop_after in EXCLUDED_PHASES:
        # 빠진 phase에서는 멈출 자리가 없다. 조용히 지나치면 그 뒤 phase가 전부 들어간다.
        raise ValueError(f"--stop-after names a phase C′ leaves out: {stop_after}")
    runner = Runner(project, workflow="full-feature", run_dir=run_dir)
    adapter = HostedAdapter("claude")
    adapter._profile_id = runner.profile_id
    adapter._profile_snapshot = runner.profile
    adapter._config_root = runner.config_root
    adapter._changed_files = changed_files(project, runner.profile)
    adapter._task_text = task
    seen: dict[str, str] = {}
    parts = [f"# Procedure\n\nWork through these phases in order for this task: {task}\n"
             "Write each phase's artifact under `cprime/` before starting the next phase. Every artifact "
             "path a phase names (`artifacts/…`, `prd.md`, `slice-plan.md`, …) is relative to `cprime/`.\n"
             "This checkout, on its current branch, is the isolated worktree the profile requires: work and "
             "commit here, and do not create another worktree or branch."]
    for phase in runner.phases:
        if phase.id in EXCLUDED_PHASES:
            continue
        envelope = adapter.render_envelope(phase, run_dir, project, skill_host="claude")
        artifact = phase.artifact or f"{phase.id}.md"
        for heading, section in _filter(envelope, phase.id, artifact, project.resolve(), phase.pause_after):
            digest = hashlib.sha256(section.encode("utf-8")).hexdigest()
            # 승인 안내는 phase마다 같은 문장이지만 그 phase에 대한 지시라 접지 않는다.
            if heading.startswith("## ") and heading != "## Approval" and digest in seen:
                parts.append(f"{heading}\n\n(Same as in phase `{seen[digest]}`.)")
                continue
            seen.setdefault(digest, phase.id)
            parts.append(section)
        if phase.id == stop_after:
            break
    parts.append(DECLARATION)
    return "\n\n".join(parts).rstrip() + "\n"


SMOKE_CASE = Path(__file__).resolve().parent / "phase-cases" / "devflow"


def _sh(args: list[str], cwd: Path, extra_env: dict[str, str] | None = None, **kwargs) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(extra_env or {})
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=env, **kwargs)


def _event_item(line: str) -> dict:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return {}
    item = event.get("item") if isinstance(event, dict) else None
    return item if isinstance(item, dict) else {}


def _codex_final_message(stdout: str) -> str | None:
    """The last `agent_message` of `codex exec --json`; None when the run never produced one."""
    last = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if event.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
            last = item.get("text")
    return last if isinstance(last, str) else None


def smoke(kit: Path, work: Path, stop_after: str, timeout: int) -> dict:
    """Run C′ once on the devflow task with Codex and score it with the case's hidden tests.

    The PR phases need a remote, so the smoke stops after `stop_after` (default `commit`). The hidden
    tests are copied in only after the agent finishes, so it never sees them.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from completion import classify

    case = json.loads((SMOKE_CASE / "case.json").read_text(encoding="utf-8"))
    project = work / "project"
    shutil.copytree(SMOKE_CASE / "seed", project)
    shutil.copytree(SMOKE_CASE / "author" / "clean" / "pre", project, dirs_exist_ok=True)
    for args in (["git", "init", "-q"], ["git", "config", "user.name", "eval"],
                 ["git", "config", "user.email", "eval@example.com"], ["git", "add", "-A"],
                 ["git", "commit", "-qm", "seed"]):
        _sh(args, project, check=True)
    installed = _sh(["node", str(kit / "bin" / "agent-flow-kit.mjs"), "install", "--root", str(project),
                     "--profile", case["profile"], *case["modes"]["clean"]["install"], "--no-hooks"], project)
    if installed.returncode != 0:
        return {"error": f"install failed: {(installed.stderr or installed.stdout)[-400:]}"}
    _sh(["git", "add", "-A"], project, check=True)
    _sh(["git", "commit", "-qm", "install", "--allow-empty"], project, check=True)
    installed_head = _sh(["git", "rev-parse", "HEAD"], project, check=True).stdout.strip()
    # A·B·C와 같은 출발점: profile이 요구하는 feature branch 위에서 시작한다.
    _sh(["git", "checkout", "-qb", "feat/cprime-smoke"], project, check=True)
    prompt = build(kit, project, case["task"], stop_after)
    # Codex의 workspace-write는 `.git`을 읽기 전용으로 둔다. 설치가 끝난 뒤 gitdir를 밖으로 옮기고
    # `--add-dir`로 연다(설치기는 분리된 gitdir에서 leader를 찾지 못한다).
    gitdir = work / "gitdir"
    _sh(["git", "init", "-q", f"--separate-git-dir={gitdir}"], project, check=True)
    started = time.time()
    (work / "prompt.md").write_text(prompt, encoding="utf-8")
    # 호스트 전역 git 설정(서명 등)은 sandbox 밖 자원을 부를 수 있다. 평가기의 Python(pytest 포함)을 PATH 앞에 둔다.
    argv = ["codex", "exec", "--ephemeral", "--sandbox", "workspace-write", "--add-dir", str(gitdir),
            "--cd", str(project), "--json", "--skip-git-repo-check", "-"]
    try:
        run = _sh(argv, project, input=prompt, timeout=timeout,
                  extra_env={"GIT_CONFIG_GLOBAL": os.devnull,
                             "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"})
        timed_out = False
    except subprocess.TimeoutExpired as expired:
        # 시간 초과도 실패한 시행이다. 그때까지의 trace를 남기고 같은 방식으로 채점한다.
        partial = expired.stdout or ""
        partial = partial.decode("utf-8", "replace") if isinstance(partial, bytes) else partial
        run = subprocess.CompletedProcess(argv, None, partial, "")
        timed_out = True
    (work / "codex.jsonl").write_text(run.stdout, encoding="utf-8")
    # 채점 전에 test 패키지를 seed 상태로 되돌린다. agent가 `tests/__init__.py`에서 바로 종료하면 discover가
    # 아무것도 돌리지 않고 0으로 끝난다. 그래서 통과는 "실제로 1개 이상 돌았고 OK"로만 센다.
    shutil.copy2(SMOKE_CASE / "seed" / "tests" / "__init__.py", project / "tests" / "__init__.py")
    shutil.copytree(SMOKE_CASE / "author" / "clean" / "hidden", project, dirs_exist_ok=True)
    hidden = {}
    for name, key in (("fix", "fix_test_command"), ("plan", "hidden_test_command"), ("visible", "test_command")):
        result = _sh(case[key], project, timeout=300)
        ran = re.search(r"^Ran (\d+) tests?", result.stderr, re.M)
        hidden[name] = result.returncode == 0 and bool(ran) and int(ran.group(1)) > 0 and "\nOK" in result.stderr
    # hidden test는 kit 저장소 안에 있어 sandbox가 읽기를 막지 못한다. 막을 수 없으니 드러낸다(#428의 비공개 평가기가 해법).
    commands = [item.get("command") or "" for line in run.stdout.splitlines() for item in [_event_item(line)]
                if item.get("type") == "command_execution"]
    contaminated = any(marker in command for command in commands for marker in ("phase-cases", "hidden_fix", "hidden_plan"))
    message = _codex_final_message(run.stdout)
    commits = _sh(["git", "rev-list", "--count", f"{installed_head}..HEAD"], project).stdout.strip()
    return {"exit": run.returncode, "timed_out": timed_out, "contaminated": contaminated, "seconds": round(time.time() - started), "prompt_sha256":
            hashlib.sha256(prompt.encode("utf-8")).hexdigest(), "hidden_tests": hidden,
            "commits": int(commits) if commits.isdigit() else None,
            "declaration": classify(message), "artifacts": sorted(p.name for p in (project / "cprime").rglob("*.md"))
            if (project / "cprime").is_dir() else []}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kit", type=Path, required=True)
    parser.add_argument("--project", type=Path, help="a project the kit is installed into")
    parser.add_argument("--task")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stop-after", help="last phase to include (the smoke defaults to commit)")
    parser.add_argument("--smoke", action="store_true", help="run C′ once on the devflow task with Codex")
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args(argv)
    if args.smoke:
        args.output.mkdir(parents=True, exist_ok=False)
        result = smoke(args.kit.resolve(), args.output.resolve(), args.stop_after or "commit", args.timeout)
        (args.output / "result.json").write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
        print(json.dumps(result))
        return 0 if "error" not in result else 1
    if args.project is None or args.task is None:
        parser.error("--project and --task are required unless --smoke")
    prompt = build(args.kit.resolve(), args.project.resolve(), args.task, args.stop_after)
    fingerprint = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    args.output.write_text(prompt, encoding="utf-8")
    args.output.with_suffix(".json").write_text(json.dumps(
        {"rules_version": RULES_VERSION, "sha256": fingerprint, "bytes": len(prompt.encode("utf-8")), "task": args.task},
        indent=1) + "\n", encoding="utf-8")
    print(fingerprint)
    return 0


if __name__ == "__main__":
    sys.exit(main())
