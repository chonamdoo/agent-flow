"""Before/after model eval of phase skill delivery: code writing and code review.

For each stack case (`evals/phase-cases/<stack>/`), skill mode (clean/local/team) and
kit (before/after) this builds a throwaway project, installs that kit, and renders the
kit's own prompts with its own runner code:
- author: the full-feature `green` envelope after the red tests exist. One Claude
  session writes the code; `oracle.py` scores behavior (visible tests restored
  from the red phase first), plan (hidden slice-plan requirement) and norm (mode
  rule, static). Required SKILL.md reads are the session's Read calls whose
  tool_result succeeded, never self-report. `slice-plan.md`, `ddd-design.md` and
  `prd.md` are staged; `design-spec.md` is captured from `prd.md` by the kit's own
  ledger code, so the green envelope carries that kit's SPEC ledger block.
- review: the real `multi-review` reviewer jobs (every angle × claude/codex) on a
  defect diff and a clean diff. Validity uses the runtime reviewer contract; a
  seeded defect counts only inside must-fix findings of a valid request-changes output.

Token usage comes from each CLI's own usage report. Model API calls: manual only.

    python evals/phase_eval.py --kit before=/tmp/af-kit-before --kit after=. \
        --scenario author,review --trials 1 --output /tmp/af-eval/run1
"""
from __future__ import annotations

import argparse
import concurrent.futures
import inspect
import json
import os
import re
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


def prepare(kit: Path, case_dir: Path, mode: str, scenario: str, variant: str, project: Path) -> dict:
    sys.path.insert(0, str(kit / "src"))
    os.environ["AGENT_FLOW_HOST"] = "claude"
    case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    project.mkdir(parents=True)
    _overlay(case_dir / "seed", project)
    _overlay(case_dir / "modes" / mode / "overlay", project)
    _git(project, "init", "-q", "-b", "main")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "seed")
    command = ["node", str(kit / "bin" / "agent-flow-kit.mjs"), "install", "--root", str(project),
               "--profile", case["profile"], *case["modes"][mode]["install"], "--no-hooks"]
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
    # full-feature에서 red/green과 multi-review는 모두 prd·ddd-design·slice-plan 뒤에 온다. 두 시나리오에 같은 산출물을 둔다.
    artifacts = case_dir / "author" / mode / "artifacts"
    _overlay(artifacts, run_dir)
    prd = artifacts / "prd.md"
    if prd.is_file():
        from agent_flow.core.design_ledger import capture_design_ledger

        # runner의 prd 완료 경로와 같은 함수로 원장을 굳힌다. kit마다 자기 코드로 쓴다.
        capture_design_ledger(run_dir, "prd", prd.read_text(encoding="utf-8"))
    runner = Runner(project, workflow="full-feature", run_dir=run_dir)
    adapter = HostedAdapter("claude")
    adapter._profile_id = runner.profile_id
    adapter._profile_snapshot = runner.profile
    adapter._config_root = runner.config_root
    # runner와 같은 인자로 부른다. profile을 받는 kit이면 커밋된 브랜치 변경까지 라우팅에 들어간다.
    takes_profile = "profile" in inspect.signature(changed_files).parameters
    adapter._changed_files = changed_files(project, runner.profile) if takes_profile else changed_files(project)
    adapter._task_text = case["task"]
    if scenario == "author":
        phase = next(p for p in runner.phases if p.id == "green")
        resolution = adapter.phase_resolution(phase, project, skill_host="claude")
        prompt = adapter.render_envelope(phase, run_dir, project, skill_host="claude", resolution=resolution)
        return {
            "prompt": prompt,
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
    jobs, _ = _reviewer_jobs(phase, run_dir, project, adapter, review_input=review_input,
                             providers=("claude", "codex"))
    return {
        "jobs": [
            {"angle": job.angle_id, "provider": provider, "prompt": job.prompt_by_provider[provider]}
            for job in jobs for provider in ("claude", "codex")
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



def _claude(prompt: str, cwd: Path, *, author: bool, timeout: int) -> dict:
    args = ["claude", "-p", "--safe-mode", "--no-session-persistence"]
    if author:
        args += ["--output-format", "stream-json", "--verbose", "--permission-mode", "acceptEdits",
                 "--allowedTools", *AUTHOR_TOOLS.split(" "), "--disallowedTools", *AUTHOR_DENIED.split(" ")]
    else:
        args += ["--output-format", "json", "--permission-mode", "plan"]
    started = time.time()
    proc = _run_cli(args, prompt, cwd, timeout)
    out: dict = {"rc": proc.returncode, "seconds": round(time.time() - started, 1), "reads": [], "text": ""}
    events = []
    if author:
        for line in proc.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        result = next((e for e in reversed(events) if e.get("type") == "result"), {})
        read_calls: dict[str, str] = {}
        outcomes: dict[str, bool] = {}
        for event in events:
            message = event.get("message") if isinstance(event, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result" and block.get("tool_use_id"):
                    outcomes[block["tool_use_id"]] = not block.get("is_error")
                elif block.get("type") == "tool_use":
                    tool_input = block.get("input") or {}
                    out.setdefault("tool_inputs", []).append(json.dumps(tool_input)[:400])
                    if block.get("name") == "Read" and tool_input.get("file_path"):
                        read_calls[str(block.get("id"))] = tool_input["file_path"]
                    elif block.get("name") == "Bash":
                        out.setdefault("bash", []).append(str(tool_input.get("command", ""))[:200])
        # 성공한 tool_result가 짝지어진 Read만 읽음이다. 실패·미응답은 따로 보고한다.
        out["reads"] = [path for use_id, path in read_calls.items() if outcomes.get(use_id)]
        out["failed_reads"] = [path for use_id, path in read_calls.items() if not outcomes.get(use_id)]
    else:
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError:
            result = {}
    usage = result.get("usage") or {}
    out["text"] = str(result.get("result") or "")
    out["usage"] = {
        "input": usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0),
        "uncached_input": usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0),
        "output": usage.get("output_tokens", 0),
    }
    out["cost_usd"] = result.get("total_cost_usd")
    out["turns"] = result.get("num_turns")
    if proc.returncode != 0 or not result:
        out["error"] = (proc.stderr or proc.stdout)[-400:]
    return out


def _codex(prompt: str, cwd: Path, *, timeout: int) -> dict:
    args = ["codex", "exec", "--ephemeral", "--ignore-user-config", "--sandbox", "read-only",
            "--cd", str(cwd), "--json", "--skip-git-repo-check", "-"]
    started = time.time()
    proc = _run_cli(args, prompt, cwd, timeout)
    texts, usage = [], {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") or {}
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            texts.append(item.get("text", ""))
        if event.get("type") == "turn.completed":
            for key in usage:
                usage[key] += (event.get("usage") or {}).get(key, 0)
    out = {
        "rc": proc.returncode, "seconds": round(time.time() - started, 1), "text": texts[-1] if texts else "",
        "usage": {"input": usage["input_tokens"], "uncached_input": usage["input_tokens"] - usage["cached_input_tokens"],
                  "output": usage["output_tokens"]},
    }
    if proc.returncode != 0 or not texts:
        out["error"] = (proc.stderr or proc.stdout)[-400:]
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
    if result_error(result) is not None:
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
        [PY, __file__, "--prepare", json.dumps([str(kit), str(case_dir), mode, scenario, variant, str(project)])],
        # prepare 안의 설치(600초, ETIMEDOUT이면 1회 재시도)와 테스트(600초)는 각자 자기 그룹을
        # 끝낸다. 바깥 한도는 그 합(1800초)보다 크게 둔다.
        timeout=2400,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"prepare failed: {proc.stderr[-1200:]}")
    return json.loads(proc.stdout)


def run_author(kit_name: str, kit: Path, case_dir: Path, mode: str, trial: int, work: Path, timeout: int) -> dict:
    project = work / f"author-{case_dir.name}-{mode}-{kit_name}-{trial}"
    row = {"scenario": "author", "stack": case_dir.name, "mode": mode, "kit": kit_name, "trial": trial}
    try:
        prepared = _prepare_subprocess(kit, case_dir, mode, "author", "", project)
    except Exception as exc:
        return {**row, "error": str(exc)[-1200:]}
    row["prompt_bytes"] = len(prepared["prompt"].encode())
    row["required"] = [r["name"] for r in prepared["required"]]
    result = _claude(prepared["prompt"], project, author=True, timeout=timeout)
    read_real = {os.path.realpath(p) for p in result["reads"]}
    inline = set(prepared.get("inline", []))
    required_paths = {
        r["name"]: os.path.realpath(r["path"]) for r in prepared["required"] if r["path"] and r["name"] not in inline
    }
    row["inline_required"] = sorted(inline)
    row["required_read"] = sorted(n for n, p in required_paths.items() if p in read_real)
    row["required_unread"] = sorted(n for n, p in required_paths.items() if p not in read_real)
    row["required_read_rate"] = round(len(row["required_read"]) / len(required_paths), 3) if required_paths else None
    row["other_skill_reads"] = sorted(
        p for p in read_real if p.endswith("SKILL.md") and p not in set(required_paths.values())
    )
    touched = "\n".join(result.get("tool_inputs", []))
    row["plan_artifacts_touched"] = sorted(
        name for name in ("slice-plan.md", "ddd-design.md", "prd.md", "design-spec.md") if name in touched
    )
    row["failed_reads"] = sorted(result.get("failed_reads", []))
    row.update({k: result.get(k) for k in ("usage", "cost_usd", "turns", "seconds", "error")})
    row["oracle"] = _oracle(case_dir, "author", "--project", str(project), "--mode", mode)
    return row


def run_review(kit_name: str, kit: Path, case_dir: Path, mode: str, variant: str, trial: int,
               work: Path, timeout: int) -> dict:
    project = work / f"review-{case_dir.name}-{mode}-{variant}-{kit_name}-{trial}"
    row = {"scenario": "review", "stack": case_dir.name, "mode": mode, "variant": variant,
           "kit": kit_name, "trial": trial}
    try:
        prepared = _prepare_subprocess(kit, case_dir, mode, "review", variant, project)
    except Exception as exc:
        return {**row, "error": str(exc)[-1200:]}
    jobs = prepared["jobs"]
    with concurrent.futures.ThreadPoolExecutor(len(jobs)) as pool:
        futures = [
            pool.submit(
                (lambda j: _claude(j["prompt"], project, author=False, timeout=timeout))
                if job["provider"] == "claude" else
                (lambda j: _codex(j["prompt"], project, timeout=timeout)),
                job,
            )
            for job in jobs
        ]
        outputs = [f.result() for f in futures]
    angles, full_texts = [], []
    for job, out in zip(jobs, outputs):
        verdict = _valid_verdict(out, f"{job['angle']}-{job['provider']}")
        full_texts.append(out["text"])
        angles.append({
            "angle": job["angle"], "provider": job["provider"], "prompt_bytes": len(job["prompt"].encode()),
            "verdict": verdict, "usage": out["usage"], "seconds": out["seconds"],
            "rc": out.get("rc"), "error": out.get("error"), "text": out["text"],
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
    findings = [
        (a, _findings(text)) for a, text in zip(angles, full_texts) if a["verdict"] == "request-changes"
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
        key: sum(a["usage"].get(key, 0) for a in angles) for key in ("input", "uncached_input", "output")
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
    parser.add_argument("--concurrency", type=int, default=8, help="parallel model CLI processes")
    parser.add_argument("--timeout", type=int, default=1500)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    if args.prepare:
        kit, case_dir, mode, scenario, variant, project = json.loads(args.prepare)
        print(json.dumps(prepare(Path(kit), Path(case_dir), mode, scenario, variant, Path(project))))
        return 0
    if args.output is None or not args.kit:
        parser.error("--output and --kit are required")
    args.output.mkdir(parents=True, exist_ok=False)
    _slots = threading.Semaphore(args.concurrency)
    kits = {name: Path(path).resolve() for name, path in (k.split("=", 1) for k in args.kit)}
    work = Path(tempfile.mkdtemp(prefix="af-phase-eval-"))
    scenarios = args.scenario.split(",")
    units = []
    for trial in range(1, args.trials + 1):
        for stack in args.stacks.split(","):
            case_dir = CASES / stack
            for mode in args.modes.split(","):
                for kit_name, kit in kits.items():
                    if "author" in scenarios:
                        units.append(("author", kit_name, kit, case_dir, mode, "", trial))
                    if "review" in scenarios:
                        for variant in ("defect", "clean"):
                            units.append(("review", kit_name, kit, case_dir, mode, variant, trial))
    results_path = args.output / "results.jsonl"
    lock = threading.Lock()

    def execute(unit):
        scenario, kit_name, kit, case_dir, mode, variant, trial = unit
        try:
            if scenario == "author":
                row = run_author(kit_name, kit, case_dir, mode, trial, work, args.timeout)
            else:
                row = run_review(kit_name, kit, case_dir, mode, variant, trial, work, args.timeout)
        except Exception as exc:  # timeout 등은 결과 행으로 남겨 invalid로 센다.
            row = {"scenario": scenario, "stack": case_dir.name, "mode": mode, "variant": variant,
                   "kit": kit_name, "trial": trial, "error": f"{type(exc).__name__}: {exc}"[-1200:]}
        with lock:
            with results_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"[{time.strftime('%H:%M:%S')}] {scenario} {case_dir.name}/{mode}/{variant or '-'} "
                  f"{kit_name} t{trial}: {row.get('error') and 'ERROR' or 'ok'}", flush=True)
        return row

    meta = {
        "kits": {k: str(v) for k, v in kits.items()},
        "claude": subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout.strip(),
        "codex": subprocess.run(["codex", "--version"], capture_output=True, text=True).stdout.strip(),
        "units": len(units), "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "work": str(work),
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
