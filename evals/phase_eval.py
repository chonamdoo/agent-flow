"""Before/after model eval of phase skill delivery: code writing and code review.

For each stack case (`evals/phase-cases/<stack>/`), skill mode (clean/local/team) and
kit (before/after) this builds a throwaway project, installs that kit, and renders the
kit's own prompts with its own runner code:
- author: the full-feature `green` envelope after the red tests exist. The selected
  Claude or Codex session writes the code; `oracle.py` scores behavior (visible
  tests restored from the red phase first), plan (hidden slice-plan requirement)
  and norm (mode rule, static). Required reads use successful tool evidence,
  never self-report. `slice-plan.md`, `ddd-design.md` and
  `prd.md` are staged; `design-spec.md` is captured from `prd.md` by the kit's own
  ledger code, so the green envelope carries that kit's SPEC ledger block.
- review: the real `multi-review` reviewer jobs (every angle × selected providers)
  on a defect diff and a clean diff. Validity uses the runtime reviewer contract; a
  seeded defect counts only inside must-fix findings of a valid request-changes output.

Token usage comes from each CLI's own usage report. Model API calls: manual only.

    python evals/phase_eval.py --kit before=/tmp/af-kit-before --kit after=. \
        --scenario author,review --trials 1 --output /tmp/af-eval/run1

`--trials` sets the trial count of every scenario; `--review-trials` overrides it for review.
"""
from __future__ import annotations

import argparse
import concurrent.futures
from collections import Counter
import hashlib
import inspect
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CASES = HERE / "phase-cases"
MODES = ("clean", "local", "team")
# "Non-blocking"의 `blocking`을 차단 표지로 읽지 않는다.
FINDING_HEADING_RE = re.compile(r"must[- ]?fix|(?<!non)(?<!non-)(?<!non )blocking", re.I)
FINDING_TAG_RE = re.compile(r"must[- ]?fix|(?<!non)(?<!non-)(?<!non )blocking|<severity:\s*(?:high|med)>", re.I)
HEADING_RE = re.compile(r"^\s{0,3}(?:#{1,6}\s+\S|\*\*[^*]+\*\*:?\s*$)")
AUTHOR_TOOLS = (
    "Read Edit Write Glob Grep Bash(node:*) Bash(python3:*) Bash(python:*) "
    "Bash(dart:*) Bash(sh:*) Bash(ls:*) Bash(cat:*)"
)
AUTHOR_DENIED = "Bash(agent-flow:*) Bash(git:*) Bash(rm:*)"
PY = sys.executable
USAGE_KEYS = ("input", "uncached_input", "cached_input", "output")
REQUESTED_MODELS: dict[str, str | None] = {"claude": None, "codex": None}
REQUESTED_PROVIDERS: dict[str, str | list[str]] = {"author": "claude", "review": ["claude", "codex"]}
READ_EVIDENCE_PROMPT = (
    "\n\nEvaluation read-evidence contract: when reading a skill document, use the native Read tool "
    "if available. Otherwise use a standalone cat or sed print command with one literal file path "
    "per tool call. Do not bulk-read skill documents through Python, loops, pipes, or mixed shell "
    "commands: those reads cannot be attributed by this evaluator. This changes only evidence "
    "collection, not the task requirements or implementation."
)


def _token_count(usage: dict, key: str) -> int | None:
    value = usage.get(key) if isinstance(usage, dict) else None
    return value if type(value) is int and value >= 0 else None


def _token_sum(values: list[int | None]) -> int | None:
    return sum(value for value in values if value is not None) if values and all(value is not None for value in values) else None


def _run_group(args: list[str], *, timeout: int, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """새 세션으로 실행한다. timeout이면 그 명령이 띄운 installer·테스트까지 프로세스 그룹째 끝낸다.

    `subprocess.run(timeout=)`은 직접 띄운 프로세스만 죽여서 손자 프로세스가 임시 폴더에 계속 쓴다.
    """
    process = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        stderr = f"timeout after {timeout}s\n{stderr}"
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


# --------------------------------------------------------------------------- prepare
# Runs in a subprocess with the kit under test first on sys.path, so each kit renders
# with its own runner code.

def _git(cwd: Path, *args: str) -> None:
    # 바깥 세션의 GIT_DIR·GIT_WORK_TREE 등이 남아 있으면 임시 프로젝트가 아닌 저장소에 쓴다.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(
        ["git", "-c", "user.email=eval@example.com", "-c", "user.name=eval", *args],
        cwd=cwd, check=True, capture_output=True, env=env,
    )


def _overlay(src: Path, dst: Path) -> None:
    if not src.is_dir():
        return
    for path in src.rglob("*"):
        if path.is_file():
            target = dst / path.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def _document_manifest(resolution, prompt: str) -> list[dict]:
    encoded = prompt.encode("utf-8")
    return [
        {"path": item.document.path, "sha256": item.document.sha256, "bytes": item.document.bytes,
         "inline_in_prompt": delivery.inline and delivery.content in encoded}
        for delivery in resolution.delivery for item in delivery.documents
    ]


def prepare(kit: Path, case_dir: Path, mode: str, scenario: str, variant: str, project: Path,
            *, author_provider: str = "claude", review_providers: tuple[str, ...] = ("claude", "codex")) -> dict:
    sys.path.insert(0, str(kit / "src"))
    host = author_provider if scenario == "author" else review_providers[0]
    os.environ["AGENT_FLOW_HOST"] = host
    case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    project.mkdir(parents=True)
    _overlay(case_dir / "seed", project)
    _overlay(case_dir / "modes" / mode / "overlay", project)
    if scenario == "author":
        _overlay(case_dir / "author" / mode / "setup", project)
    _git(project, "init", "-q", "-b", "main")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "seed")
    mode_config = case["modes"][mode]
    install_args = mode_config.get("author_install", mode_config["install"]) if scenario == "author" else mode_config["install"]
    command = ["node", str(kit / "bin" / "agent-flow-kit.mjs"), "install", "--root", str(project),
               "--profile", case["profile"], *install_args, "--no-hooks"]
    install = _run_group(command, timeout=600)
    if install.returncode != 0 and "ETIMEDOUT" in install.stderr + install.stdout:
        # 동시 실행 부하로 install 안의 python 하위 호출이 시간 초과된 경우다. kit 결함이 아니므로 한 번만 다시 설치한다.
        install = _run_group(command, timeout=600)
    if install.returncode != 0:
        raise RuntimeError(f"install failed: {install.stderr[-600:] or install.stdout[-600:]}")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "install agent-flow")
    _git(project, "checkout", "-q", "-b", "feat/orders")

    from agent_flow.adapters.hosted import HostedAdapter, _profile_base_branch, _reviewer_jobs, _write_review_input_snapshot
    from agent_flow.artifact import create_run
    from agent_flow.core.local_skills import changed_files
    from agent_flow.runner import Runner

    if scenario == "author":
        _overlay(case_dir / "author" / mode / "pre", project)
    else:
        _overlay(case_dir / "review" / mode / variant, project)
    run_dir = create_run(project, "full-feature", case["task"], run_id=f"eval-{scenario}")
    artifacts = case_dir / "author" / mode / "artifacts"
    _overlay(artifacts, run_dir)
    if scenario == "review":
        _overlay(case_dir / "review" / mode / "artifacts", run_dir)
    prd = run_dir / "prd.md"
    if prd.is_file():
        from agent_flow.core.design_ledger import capture_design_ledger

        # runner의 prd 완료 경로와 같은 함수로 원장을 굳힌다. kit마다 자기 코드로 쓴다.
        capture_design_ledger(run_dir, "prd", prd.read_text(encoding="utf-8"))
    runner = Runner(project, workflow="full-feature", run_dir=run_dir)
    adapter = HostedAdapter(host)
    adapter._profile_id = runner.profile_id
    adapter._profile_snapshot = runner.profile
    adapter._config_root = runner.config_root
    # runner와 같은 인자로 부른다. profile을 받는 kit이면 커밋된 브랜치 변경까지 라우팅에 들어간다.
    takes_profile = "profile" in inspect.signature(changed_files).parameters
    adapter._changed_files = changed_files(project, runner.profile) if takes_profile else changed_files(project)
    adapter._task_text = case["task"]
    if scenario == "author":
        phase = next(p for p in runner.phases if p.id == "green")
        resolution = adapter.phase_resolution(phase, project, skill_host=host)
        prompt = adapter.render_envelope(phase, run_dir, project, skill_host=host, resolution=resolution)
        return {
            "prompt": prompt + READ_EVIDENCE_PROMPT,
            "required": [
                {"name": s.name, "path": str(s.path) if s.path else ""}
                for s in resolution.required
            ],
            # 본문이 프롬프트에 인라인된 문서는 파일을 열 필요가 없다. 읽음률에서 뺀다.
            "inline": sorted({
                route.skill
                for delivery in resolution.delivery if delivery.inline
                for item in delivery.documents for route in item.routes
            }),
            "documents": _document_manifest(resolution, prompt),
        }
    # 실제 run에서 multi-review 앞 phase들은 테스트 실행 기록을 남긴다. 그것이 없으면 design-spec의
    # review 기한 항목을 본 리뷰어가 "실행 근거 없음"으로 변경을 요청한다. phase artifact(green.md)
    # 모양으로 두면 리뷰어가 그 phase의 Completion Gate까지 요구하므로, 하네스 기록임을 밝힌 별도
    # 파일에 전체 출력을 남긴다. 복사본에서 돌린다: `pubspec.lock` 같은 부산물이 리뷰 diff에 섞이면 안 된다.
    command = case.get("review_test_command", case["test_command"])
    with tempfile.TemporaryDirectory(prefix="af-phase-eval-tests-") as scratch:
        copy = Path(scratch) / "project"
        shutil.copytree(project, copy, ignore=shutil.ignore_patterns(".git"))
        tests = _run_group(command, cwd=copy, timeout=600)
    (run_dir / "test-evidence.md").write_text(
        "# Test run before review\n\n"
        "Recorded by the evaluation harness on the reviewed tree (the green phase's test evidence).\n\n"
        f"$ {' '.join(command)}\nexit {tests.returncode}\n\n```text\n"
        f"{(tests.stdout + tests.stderr).strip()[-20000:]}\n```\n",
        encoding="utf-8",
    )
    phase = next(p for p in runner.phases if p.id == "multi-review")
    review_input = _write_review_input_snapshot(
        project, run_dir, phase.id, base_branch=_profile_base_branch(adapter),
    )
    jobs, resolutions = _reviewer_jobs(phase, run_dir, project, adapter, review_input=review_input,
                             providers=review_providers)
    return {
        "jobs": [
            {"angle": job.angle_id, "provider": provider,
             "prompt": job.prompt_by_provider[provider] + READ_EVIDENCE_PROMPT +
             "\n\nWrite the review report in English. Keep code identifiers and exact workflow markers unchanged.",
             "documents": _document_manifest(resolutions[provider], job.prompt_by_provider[provider])}
            for job in jobs for provider in review_providers
        ],
    }


# --------------------------------------------------------------------------- models

_slots: threading.Semaphore


def _run_cli(args: list[str], prompt: str, cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    """모델 CLI를 새 세션으로 띄운다. timeout이면 CLI가 띄운 셸·테스트까지 프로세스 그룹째 끝낸다."""
    with _slots:
        process = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, text=True, start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            stderr = f"timeout after {timeout}s\n{stderr}"
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)



def _call_evidence(args: list[str], prompt: str, cwd: Path, proc: subprocess.CompletedProcess,
                   evidence: Path | None) -> dict:
    record = {"argv": args, "cwd": str(cwd), "returncode": proc.returncode,
              "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
              "stdout_sha256": hashlib.sha256(proc.stdout.encode("utf-8")).hexdigest(),
              "stderr_sha256": hashlib.sha256(proc.stderr.encode("utf-8")).hexdigest()}
    if evidence is not None:
        evidence.mkdir(parents=True, exist_ok=False)
        (evidence / "command.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        for name, text in (("prompt.txt", prompt), ("stdout.raw", proc.stdout), ("stderr.raw", proc.stderr)):
            (evidence / name).write_text(text, encoding="utf-8")
        record["evidence"] = str(evidence)
    return record


def _claude(prompt: str, cwd: Path, *, author: bool, timeout: int, evidence: Path | None = None) -> dict:
    args = ["claude", "-p", "--safe-mode", "--no-session-persistence",
            "--output-format", "stream-json", "--verbose"]
    if REQUESTED_MODELS["claude"]:
        args += ["--model", REQUESTED_MODELS["claude"]]
    if author:
        args += ["--permission-mode", "acceptEdits",
                 "--allowedTools", *AUTHOR_TOOLS.split(" "), "--disallowedTools", *AUTHOR_DENIED.split(" ")]
    else:
        args += ["--permission-mode", "default", "--tools", "Read,Glob,Grep",
                 "--allowedTools", "Read", "Glob", "Grep"]
    started = time.time()
    proc = _run_cli(args, prompt, cwd, timeout)
    out: dict = {"rc": proc.returncode, "seconds": round(time.time() - started, 1), "reads": [], "text": ""}
    out["execution"] = _call_evidence(args, prompt, cwd, proc, evidence)
    events = []
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    read_calls: dict[str, str] = {}
    outcomes: dict[str, bool] = {}
    calls: dict[str, str] = {}
    for event in events:
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result" and block.get("tool_use_id"):
                outcomes[block["tool_use_id"]] = not block.get("is_error")
            elif block.get("type") == "tool_use":
                calls[str(block.get("id"))] = str(block.get("name"))
                tool_input = block.get("input") or {}
                out.setdefault("tool_inputs", []).append(json.dumps(tool_input)[:400])
                if block.get("name") == "Read" and tool_input.get("file_path"):
                    read_calls[str(block.get("id"))] = tool_input["file_path"]
                elif block.get("name") == "Bash":
                    out.setdefault("bash", []).append(str(tool_input.get("command", ""))[:200])
    # 성공한 tool_result가 짝지어진 Read만 읽음이다. 실패·미응답은 따로 보고한다.
    out["reads"] = [path for use_id, path in read_calls.items() if outcomes.get(use_id)]
    out["failed_reads"] = [path for use_id, path in read_calls.items() if not outcomes.get(use_id)]
    out["tool_calls"] = [{"id": use_id, "name": name, "succeeded": outcomes.get(use_id)}
                         for use_id, name in calls.items()]
    usage = result.get("usage") or {}
    out["text"] = str(result.get("result") or "")
    uncached = _token_sum([_token_count(usage, key) for key in ("input_tokens", "cache_creation_input_tokens")])
    cached = _token_count(usage, "cache_read_input_tokens")
    out["usage"] = {
        "input": _token_sum([uncached, cached]),
        "uncached_input": uncached,
        "cached_input": cached,
        "output": _token_count(usage, "output_tokens"),
    }
    out["cost_usd"] = result.get("total_cost_usd")
    model_usage = result.get("modelUsage")
    out["models_observed"] = sorted(model_usage) if isinstance(model_usage, dict) else None
    out["turns"] = result.get("num_turns")
    if proc.returncode != 0 or not result or result.get("is_error"):
        out["error"] = (proc.stderr or out["text"] or proc.stdout or "missing CLI result")[-400:]
    if not author and any(outcomes.get(use_id) and name not in {"Read", "Glob", "Grep"}
                          for use_id, name in calls.items()):
        out["error"] = "read-only reviewer used a tool outside Read/Glob/Grep"
    return out


def _read_command_paths(command: str, cwd: Path) -> list[str]:
    try:
        arguments = shlex.split(command)
        if (len(arguments) == 3 and Path(arguments[0]).name in {"sh", "bash", "zsh"}
                and arguments[1] in {"-c", "-lc"}):
            if "/" in arguments[0] and Path(arguments[0]).parent not in (Path("/bin"), Path("/usr/bin")):
                return []
            command = arguments[2]
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";|&<>()\n")
        lexer.commenters = ""
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        arguments = list(lexer)
    except ValueError:
        return []
    if not arguments:
        return []
    if arguments[0] == "cd":
        if (len(arguments) < 4 or arguments[2] != "&&" or arguments[1].startswith("-")
                or any(character in arguments[1] for character in "$`*?[]~{}")):
            return []
        cwd = (cwd / arguments[1]).resolve()
        arguments = arguments[3:]
    segments: list[list[str]] = [[]]
    for token in arguments:
        if token in (";", "&&", "\n"):
            if not segments[-1]:
                return []
            segments.append([])
        elif token and all(character in ";|&<>()\n" for character in token):
            return []
        else:
            segments[-1].append(token)
    if not segments[-1]:
        segments.pop()
    paths = []
    for segment in segments:
        if "/" in segment[0] and Path(segment[0]).parent not in (Path("/bin"), Path("/usr/bin")):
            return []
        reader, operands = Path(segment[0]).name, segment[1:]
        if reader == "sed":
            if operands and operands[0] == "-n":
                operands = operands[1:]
            if not operands or not re.fullmatch(r"(?:\d+|\$)?(?:,(?:\d+|\$))?p", operands[0]):
                return []
            operands = operands[1:]
        elif reader in {"head", "tail"}:
            if len(operands) >= 2 and operands[0] in {"-n", "-c"} and re.fullmatch(r"[+-]?\d+", operands[1]):
                operands = operands[2:]
            elif operands and re.fullmatch(r"-(?:[nc])?\d+", operands[0]):
                operands = operands[1:]
        elif reader != "cat":
            return []
        if operands and operands[0] == "--":
            operands = operands[1:]
        if (not operands or (reader != "cat" and len(operands) != 1)
                or any(operand.startswith(("-", "#")) or
                       any(character in operand for character in "$`*?[]~{}") for operand in operands)):
            return []
        paths.extend(str((cwd / operand).resolve()) for operand in operands)
    return list(dict.fromkeys(paths))


def _codex_read_paths(item: dict, cwd: Path) -> tuple[list[str], list[str]]:
    command = item.get("command")
    paths = _read_command_paths(command, cwd) if isinstance(command, str) else []
    if type(item.get("exit_code")) is not int or item["exit_code"] != 0 or item.get("status") not in (None, "completed"):
        return [], paths if len(paths) == 1 else []
    output = item.get("aggregated_output")
    if not isinstance(output, str) or not output:
        return [], []
    contents = {}
    for path in paths:
        try:
            contents[path] = {line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines()
                              if len(line.strip()) >= 20}
        except (OSError, UnicodeError):
            continue
    occurrences = Counter(line for lines in contents.values() for line in lines)
    output_lines = {line.strip() for line in output.splitlines()}
    reads = [path for path, lines in contents.items()
             if any(occurrences[line] == 1 and line in output_lines for line in lines)]
    return reads, []


def _codex(prompt: str, cwd: Path, *, author: bool = False, timeout: int, evidence: Path | None = None) -> dict:
    args = ["codex", "exec", "--ephemeral", "--ignore-user-config", "--sandbox",
            "workspace-write" if author else "read-only",
            "--cd", str(cwd), "--json", "--skip-git-repo-check", "-"]
    if REQUESTED_MODELS["codex"]:
        args[2:2] = ["--model", REQUESTED_MODELS["codex"]]
    started = time.time()
    proc = _run_cli(args, prompt, cwd, timeout)
    execution = _call_evidence(args, prompt, cwd, proc, evidence)
    texts, reports, failures, warnings = [], [], [], []
    reads, failed_reads, tool_calls, tool_inputs = set(), set(), [], []
    observation_complete = True
    terminal_complete = False
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "error":
            warnings.append(str(event.get("message") or "provider error"))
            terminal_complete = False
        if event.get("type") == "turn.failed":
            failures.append(str(event.get("error") or "provider turn failed"))
        item = event.get("item") or {}
        if event.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "command_execution":
            observed, failed = _codex_read_paths(item, cwd)
            reads.update(observed)
            failed_reads.update(failed)
            command = item.get("command")
            paths = _read_command_paths(command, cwd) if isinstance(command, str) else []
            observation_complete = observation_complete and bool(paths) and set(paths) <= set(observed + failed)
            tool_calls.append({"id": item.get("id"), "name": "command_execution",
                               "succeeded": type(item.get("exit_code")) is int and item["exit_code"] == 0
                               and item.get("status") in (None, "completed")})
            tool_inputs.append(json.dumps({"command": item.get("command")}))
        if event.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
            texts.append(item.get("text", ""))
        if event.get("type") == "turn.completed":
            terminal_complete = True
            reports.append(event.get("usage") or {})
    usage = {
        key: _token_sum([_token_count(report, key) for report in reports])
        for key in ("input_tokens", "cached_input_tokens", "output_tokens")
    }
    uncached = _token_sum([
        total - cached if total is not None and cached is not None and cached <= total else None
        for report in reports
        for total, cached in [(_token_count(report, "input_tokens"), _token_count(report, "cached_input_tokens"))]
    ])
    out = {
        "rc": proc.returncode, "seconds": round(time.time() - started, 1), "text": texts[-1] if texts else "",
        "execution": execution,
        "reads": sorted(reads) if tool_calls else None, "failed_reads": sorted(failed_reads),
        "tool_calls": tool_calls or None, "tool_inputs": tool_inputs,
        "read_observation_complete": bool(tool_calls) and observation_complete,
        "provider_warnings": warnings,
        "usage": {"input": usage["input_tokens"], "uncached_input": uncached,
                  "cached_input": usage["cached_input_tokens"],
                  "output": usage["output_tokens"]},
    }
    if proc.returncode != 0 or not texts:
        out["error"] = (proc.stderr or proc.stdout or "missing CLI result")[-400:]
    if failures:
        out["error"] = "\n".join(failures)[-400:]
    if warnings and not terminal_complete:
        out["error"] = "\n".join(warnings)[-400:]
    return out


# --------------------------------------------------------------------------- scoring

_contract_cache: tuple | None = None


def _reviewer_contract():
    """하네스 저장소의 runtime reviewer 계약. --prepare 자식이 kit src를 먼저 쓰도록 지연 import한다."""
    global _contract_cache
    if _contract_cache is None:
        sys.path.insert(0, str(HERE.parent / "src"))
        from agent_flow.core.review_evidence import reviewer_output_verdict
        from agent_flow.multi_review import reviewer_result_error
        from agent_flow.subprocess_pool import SubprocessResult

        _contract_cache = (reviewer_output_verdict, reviewer_result_error, SubprocessResult)
    return _contract_cache


def _valid_verdict(out: dict, job_id: str) -> str | None:
    """프로세스가 성공하고 runtime 계약(provenance·단일 verdict)이 받아들인 출력만 판정이다."""
    output_verdict, result_error, result_type = _reviewer_contract()
    if out.get("rc") != 0 or out.get("error"):
        return None
    result = result_type(job_id=job_id, stdout=out.get("text", ""), stderr="", returncode=0)
    out["output_contract_error"] = result_error(result)
    if out["output_contract_error"] is not None:
        return None
    return output_verdict(result.stdout)


NONBLOCKING_HEADING_RE = re.compile(
    r"should[- ]?fix|suggest|\bnits?\b|notes?\b|optional|minor|non[- ]?blocking|calibration|got right", re.I,
)


def _findings(text: str) -> str:
    """must-fix/blocking 제목 아래 본문과, 비차단 구간 밖의 must-fix·blocking·high/med 태그 줄만 모은다.

    Should-fix·Notes 같은 비차단 구간 안의 severity 태그는 선택적 지적이라 탐지로 세지 않는다.
    하위 제목(`## Must-fix` 아래 `### 결함 이름`)은 상위 구간의 성격을 물려받는다.
    """
    kept: list[str] = []
    # (제목 깊이, "in" | "non" | None). `**제목**`은 가장 깊은 제목으로 본다.
    stack: list[tuple[int, str | None]] = []
    for line in text.splitlines():
        if HEADING_RE.match(line):
            stripped = line.lstrip()
            level = len(stripped) - len(stripped.lstrip("#")) or 7
            while stack and stack[-1][0] >= level:
                stack.pop()
            if NONBLOCKING_HEADING_RE.search(line):
                state = "non"
            elif FINDING_HEADING_RE.search(line):
                state = "in"
            else:
                state = stack[-1][1] if stack else None
            stack.append((level, state))
            if state == "in" and not FINDING_HEADING_RE.search(line):
                kept.append(line)  # 하위 제목 자체가 결함 이름인 경우가 많다.
            continue
        state = stack[-1][1] if stack else None
        if state == "in" or (state != "non" and FINDING_TAG_RE.search(line)):
            kept.append(line)
    return "\n".join(kept)


def _oracle(case_dir: Path, *args: str) -> dict:
    # oracle 안의 테스트 실행은 각자 자기 프로세스 그룹을 한도(최대 300초 × 2회) 안에 끝낸다.
    # 바깥 한도가 그 합보다 커야 안쪽 정리가 먼저 돌고, 바깥 killpg가 놓치는 자손이 없다.
    process = _run_group([PY, str(case_dir / "oracle.py"), *args], timeout=900)
    try:
        return json.loads(process.stdout)
    except json.JSONDecodeError:
        return {"error": (process.stderr or process.stdout)[-600:]}


def _prepare_subprocess(kit: Path, case_dir: Path, mode: str, scenario: str, variant: str, project: Path) -> dict:
    proc = _run_group(
        [PY, __file__, "--prepare", json.dumps([str(kit), str(case_dir), mode, scenario, variant, str(project)]),
         "--author-provider", REQUESTED_PROVIDERS["author"],
         "--review-providers", ",".join(REQUESTED_PROVIDERS["review"])],
        # prepare 안의 설치(600초, ETIMEDOUT이면 1회 재시도)와 테스트(600초)는 각자 자기 그룹을
        # 끝낸다. 바깥 한도는 그 합(1800초)보다 크게 둔다.
        timeout=2400,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"prepare failed: {proc.stderr[-1200:]}")
    return json.loads(proc.stdout)


def run_author(kit_name: str, kit: Path, case_dir: Path, mode: str, trial: int, work: Path, timeout: int,
               evidence: Path | None = None) -> dict:
    project = work / f"author-{case_dir.name}-{mode}-{kit_name}-{trial}"
    row = {"scenario": "author", "stack": case_dir.name, "mode": mode, "kit": kit_name, "trial": trial,
           "provider": REQUESTED_PROVIDERS["author"]}
    try:
        prepared = _prepare_subprocess(kit, case_dir, mode, "author", "", project)
    except Exception as exc:
        return {**row, "error": str(exc)[-1200:]}
    row["prompt_bytes"] = len(prepared["prompt"].encode())
    row["required"] = [r["name"] for r in prepared["required"]]
    invoke = _claude if row["provider"] == "claude" else _codex
    try:
        result = invoke(prepared["prompt"], project, author=True, timeout=timeout, evidence=evidence)
    except Exception as exc:
        return {**row, "error": f"{type(exc).__name__}: {exc}"[-1200:]}
    read_real = {os.path.realpath(p) for p in result["reads"] or []}
    inline = set(prepared.get("inline", []))
    required_paths = {
        r["name"]: os.path.realpath(r["path"]) for r in prepared["required"] if r["path"] and r["name"] not in inline
    }
    row["inline_required"] = sorted(inline)
    row["required_read"] = sorted(n for n, p in required_paths.items() if p in read_real)
    complete = result.get("read_observation_complete", result["reads"] is not None)
    failed = {os.path.realpath(p) for p in result.get("failed_reads", [])}
    unobserved = sorted(n for n, p in required_paths.items()
                        if p not in read_real and p not in failed and not complete)
    row["required_unobserved"] = unobserved
    unread = sorted(n for n, p in required_paths.items() if p not in read_real and (p in failed or complete))
    row["required_unread"] = unread if unread or not unobserved else None
    row["required_read_rate"] = (round(len(row["required_read"]) / len(required_paths), 3)
                                 if required_paths and not unobserved else None)
    row["required_read_rate_scope"] = "Successful SKILL.md read paths, not full-body or reference coverage"
    row["other_skill_reads"] = sorted(
        p for p in read_real if p.endswith("SKILL.md") and p not in set(required_paths.values())
    )
    touched = "\n".join(result.get("tool_inputs", []))
    row["plan_artifacts_touched"] = sorted(
        name for name in ("slice-plan.md", "ddd-design.md", "prd.md", "design-spec.md") if name in touched
    )
    row["failed_reads"] = sorted(result.get("failed_reads", []))
    row.update({k: result.get(k) for k in ("usage", "cost_usd", "turns", "seconds", "rc", "error",
                                        "execution", "models_observed", "provider_warnings")})
    row["documents"] = prepared.get("documents", [])
    row["norm_scope"] = "case-specific fixture contract; general architecture correctness is unverified"
    row["oracle"] = _oracle(case_dir, "author", "--project", str(project), "--mode", mode)
    return row


def run_review(kit_name: str, kit: Path, case_dir: Path, mode: str, variant: str, trial: int,
               work: Path, timeout: int, evidence: Path | None = None) -> dict:
    project = work / f"review-{case_dir.name}-{mode}-{variant}-{kit_name}-{trial}"
    row = {"scenario": "review", "stack": case_dir.name, "mode": mode, "variant": variant,
           "kit": kit_name, "trial": trial}
    try:
        prepared = _prepare_subprocess(kit, case_dir, mode, "review", variant, project)
    except Exception as exc:
        return {**row, "error": str(exc)[-1200:]}
    jobs = prepared["jobs"]

    def invoke_review(job):
        if job["provider"] not in REQUESTED_PROVIDERS["review"]:
            raise ValueError(f"unselected reviewer provider: {job['provider']}")
        invoke = _claude if job["provider"] == "claude" else _codex
        return invoke(job["prompt"], project, author=False, timeout=timeout,
                      evidence=evidence / f"{job['angle']}-{job['provider']}" if evidence else None)

    with concurrent.futures.ThreadPoolExecutor(len(jobs)) as pool:
        futures = [pool.submit(invoke_review, job) for job in jobs]
        outputs = []
        for future in futures:
            try:
                outputs.append(future.result())
            except Exception as exc:
                outputs.append({"text": "", "seconds": None, "error": str(exc)[-1200:],
                                "usage": {key: None for key in USAGE_KEYS}})
    angles, full_texts = [], []
    for job, out in zip(jobs, outputs):
        verdict = _valid_verdict(out, f"{job['angle']}-{job['provider']}")
        full_texts.append(out["text"])
        angles.append({
            "angle": job["angle"], "provider": job["provider"], "prompt_bytes": len(job["prompt"].encode()),
            "verdict": verdict, "usage": out["usage"], "seconds": out["seconds"],
            "rc": out.get("rc"), "error": out.get("error"), "text": out["text"],
            "output_contract_error": out.get("output_contract_error"),
            "read_paths_observed": out.get("reads"), "failed_reads": out.get("failed_reads"),
            "tool_calls": out.get("tool_calls"),
            "provider_warnings": out.get("provider_warnings"),
            "execution": out.get("execution"), "models_observed": out.get("models_observed"),
            "cost_usd": out.get("cost_usd"),
            "documents": [
                {**document, "read_path_observed": (
                    True if os.path.realpath(document["path"]) in {os.path.realpath(path) for path in out.get("reads") or []}
                    else False if os.path.realpath(document["path"]) in
                    {os.path.realpath(path) for path in out.get("failed_reads") or []}
                    or out.get("read_observation_complete", out.get("reads") is not None) else None
                )}
                for document in job.get("documents", [])
            ],
        })
    case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    spec = case["review"][mode][variant]
    verdicts = [a["verdict"] for a in angles]
    # 무효 reviewer가 하나라도 있으면 그 시행은 채점하지 않는다(incomplete).
    overall = "incomplete" if not all(verdicts) else (
        "request-changes" if "request-changes" in verdicts else "approve"
    )
    row["angles"] = angles
    row["overall"] = overall
    # `expect: null`: 이 mode에서 정답 판정이 정해지지 않는다(pending mode의 신규 구조 등). 판정은 채점하지 않는다.
    row["expected"] = spec["expect"]
    row["valid"] = overall != "incomplete"
    row["correct"] = overall == spec["expect"] if row["valid"] and spec["expect"] else None
    # 잘린 사본이 아니라 전문에서, 유효한 request-changes 출력의 finding 안에서만 찾는다.
    # 단일 줄 inline-code 표기는 구간·문단 경계를 유지한 채 의미 패턴과 비교한다.
    findings = [
        (a, re.sub(r"(?<!`)(`+)([^`\n]+)\1(?!`)", r"\2", _findings(text)))
        for a, text in zip(angles, full_texts) if a["verdict"] == "request-changes"
    ]
    row["defects"] = {}
    row["defect_detectors"] = {}
    for defect in spec.get("defects", []):
        detectors = [
            f"{a['angle']}/{a['provider']}" for a, found in findings
            if re.search(defect["pattern"], found, re.I | re.M)
        ]
        row["defects"][defect["id"]] = bool(detectors)
        row["defect_detectors"][defect["id"]] = detectors
    row["false_request_changes"] = sum(1 for v in verdicts if v == "request-changes") if spec["expect"] == "approve" else None
    row["invalid_reviewers"] = sum(1 for a in angles if a["verdict"] is None)
    row["usage"] = {
        key: _token_sum([a["usage"].get(key) for a in angles]) for key in USAGE_KEYS
    }
    row["usage_observed"] = {
        key: sum(a["usage"].get(key) or 0 for a in angles) for key in USAGE_KEYS
    }
    row["usage_missing_reviewers"] = {
        key: sum(a["usage"].get(key) is None for a in angles) for key in USAGE_KEYS
    }
    row["prompt_bytes_total"] = sum(a["prompt_bytes"] for a in angles)
    row["subprocesses"] = len(angles)
    return row


# --------------------------------------------------------------------------- main

def main() -> int:
    global _slots
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare")
    parser.add_argument("--kit", action="append", default=[], help="name=path")
    parser.add_argument("--stacks", default="web,rn,app,backend")
    parser.add_argument("--modes", default=",".join(MODES))
    parser.add_argument("--scenario", default="author,review")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--review-trials", type=int, help="review trial count; defaults to --trials")
    parser.add_argument("--concurrency", type=int, default=8, help="parallel model CLI processes")
    parser.add_argument("--timeout", type=int, default=1500)
    parser.add_argument("--author-provider", choices=("claude", "codex"), default="claude")
    parser.add_argument("--review-providers", default="claude,codex", help="comma-separated claude/codex subset")
    parser.add_argument("--claude-model")
    parser.add_argument("--codex-model")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    trials = {"author": args.trials,
              "review": args.trials if args.review_trials is None else args.review_trials}
    if min(trials.values()) < 1:
        parser.error("--trials and --review-trials must be positive")
    reviewers = args.review_providers.split(",")
    if (not reviewers or any(provider not in {"claude", "codex"} for provider in reviewers)
            or len(reviewers) != len(set(reviewers))):
        parser.error("--review-providers requires distinct, nonempty claude/codex selections")
    REQUESTED_PROVIDERS.update(author=args.author_provider, review=reviewers)
    if args.prepare:
        kit, case_dir, mode, scenario, variant, project = json.loads(args.prepare)
        print(json.dumps(prepare(Path(kit), Path(case_dir), mode, scenario, variant, Path(project),
                                 author_provider=args.author_provider, review_providers=tuple(reviewers))))
        return 0
    if args.output is None or not args.kit:
        parser.error("--output and --kit are required")
    args.output.mkdir(parents=True, exist_ok=False)
    enabled = {args.author_provider, *reviewers}
    REQUESTED_MODELS.update({
        provider: getattr(args, f"{provider}_model") if provider in enabled else None
        for provider in ("claude", "codex")
    })
    _slots = threading.Semaphore(args.concurrency)
    kits = {name: Path(path).resolve() for name, path in (k.split("=", 1) for k in args.kit)}
    work = Path(tempfile.mkdtemp(prefix="af-phase-eval-"))
    scenarios = args.scenario.split(",")
    units = []
    for trial in range(1, max(trials.values()) + 1):
        for stack in args.stacks.split(","):
            case_dir = CASES / stack
            for mode in args.modes.split(","):
                for kit_name, kit in kits.items():
                    if "author" in scenarios and trial <= trials["author"]:
                        units.append(("author", kit_name, kit, case_dir, mode, "", trial))
                    if "review" in scenarios and trial <= trials["review"]:
                        for variant in ("defect", "clean"):
                            units.append(("review", kit_name, kit, case_dir, mode, variant, trial))
    results_path = args.output / "results.jsonl"
    lock = threading.Lock()

    def execute(unit):
        scenario, kit_name, kit, case_dir, mode, variant, trial = unit
        unit_id = hashlib.sha256(json.dumps([scenario, kit_name, case_dir.name, mode, variant, trial]).encode()).hexdigest()
        evidence = args.output / "evidence" / unit_id
        try:
            if scenario == "author":
                row = run_author(kit_name, kit, case_dir, mode, trial, work, args.timeout, evidence=evidence)
            else:
                row = run_review(kit_name, kit, case_dir, mode, variant, trial, work, args.timeout, evidence=evidence)
        except Exception as exc:  # timeout 등은 결과 행으로 남겨 invalid로 센다.
            row = {"scenario": scenario, "stack": case_dir.name, "mode": mode, "variant": variant,
                   "kit": kit_name, "trial": trial, "error": f"{type(exc).__name__}: {exc}"[-1200:]}
            if scenario == "author":
                row["provider"] = REQUESTED_PROVIDERS["author"]
        with lock:
            with results_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"[{time.strftime('%H:%M:%S')}] {scenario} {case_dir.name}/{mode}/{variant or '-'} "
                  f"{kit_name} t{trial}: {row.get('error') and 'ERROR' or 'ok'}", flush=True)
        return row

    # Content fingerprint of each measured kit; the release check matches it to the tag tree.
    sys.path.insert(0, str(HERE.parent / "src"))
    from agent_flow.core.kit_digest import kit_source_digest

    meta = {
        "kits": {k: str(v) for k, v in kits.items()},
        "kit_digests": {k: kit_source_digest(v) for k, v in kits.items()},
        **{
            provider: subprocess.run([provider, "--version"], capture_output=True, text=True).stdout.strip()
            if provider in enabled else None
            for provider in ("claude", "codex")
        },
        "units": len(units), "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "work": str(work),
        "requested_models": REQUESTED_MODELS.copy(),
        "requested_providers": {"author": args.author_provider, "review": reviewers},
        "review_report_language": "en",
        "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "case_files": {
            path.relative_to(CASES).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(CASES.rglob("*")) if path.is_file() and "__pycache__" not in path.parts
        },
    }
    (args.output / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    # 한 unit 안에서도 리뷰어가 병렬이므로, unit 동시성은 CLI 슬롯으로만 제한한다.
    with concurrent.futures.ThreadPoolExecutor(max(4, args.concurrency)) as pool:
        list(pool.map(execute, units))
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
