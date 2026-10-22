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
  separate condition not built here.
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

RULES_VERSION = 2
EXCLUDED_PHASES = frozenset({"gates", "worktree", "run-start", "fix-loop", "pr-comment-fix", "pr-ci-fix",
                             "multi-review", "architecture-review"})
RUNNER_LINE = re.compile(r"agent-flow\s+[a-z]|--approve\b|approval_command|run_command_prefix|next_command|"
                         r"^\*\*(Run id|Project root)\*\*", re.I)
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
    runner = Runner(project, workflow="full-feature", run_dir=run_dir)
    adapter = HostedAdapter("claude")
    adapter._profile_id = runner.profile_id
    adapter._profile_snapshot = runner.profile
    adapter._config_root = runner.config_root
    adapter._changed_files = changed_files(project, runner.profile)
    adapter._task_text = task
    seen: dict[str, str] = {}
    parts = [f"# Procedure\n\nWork through these phases in order for this task: {task}\n"
             "Write each phase's artifact under `cprime/` before starting the next phase."]
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


def _sh(args: list[str], cwd: Path, **kwargs) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=env, **kwargs)


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
    for args in (["git", "init", "-q"], ["git", "add", "-A"],
                 ["git", "-c", "user.name=eval", "-c", "user.email=eval@example.com", "commit", "-qm", "seed"]):
        _sh(args, project, check=True)
    installed = _sh(["node", str(kit / "bin" / "agent-flow-kit.mjs"), "install", "--root", str(project),
                     "--profile", case["profile"], *case["modes"]["clean"]["install"], "--no-hooks"], project)
    if installed.returncode != 0:
        return {"error": f"install failed: {(installed.stderr or installed.stdout)[-400:]}"}
    _sh(["git", "add", "-A"], project, check=True)
    _sh(["git", "-c", "user.name=eval", "-c", "user.email=eval@example.com", "commit", "-qm", "install", "--allow-empty"], project, check=True)
    # A·B·C와 같은 출발점: profile이 요구하는 feature branch 위에서 시작한다.
    _sh(["git", "checkout", "-qb", "feat/cprime-smoke"], project, check=True)
    prompt = build(kit, project, case["task"], stop_after)
    started = time.time()
    run = _sh(["codex", "exec", "--ephemeral", "--sandbox", "workspace-write", "--cd", str(project), "--json",
               "--skip-git-repo-check", "-"], project, input=prompt, timeout=timeout)
    (work / "codex.jsonl").write_text(run.stdout, encoding="utf-8")
    shutil.copytree(SMOKE_CASE / "author" / "clean" / "hidden", project, dirs_exist_ok=True)
    hidden = {name: _sh(case[key], project, timeout=300).returncode == 0
              for name, key in (("fix", "fix_test_command"), ("plan", "hidden_test_command"), ("visible", "test_command"))}
    message = _codex_final_message(run.stdout)
    return {"exit": run.returncode, "seconds": round(time.time() - started), "prompt_sha256":
            hashlib.sha256(prompt.encode("utf-8")).hexdigest(), "hidden_tests": hidden,
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
