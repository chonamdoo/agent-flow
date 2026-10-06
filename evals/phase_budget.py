"""Measure what each full-feature phase actually hands a model, per profile and mode.

Static, model-free. For every profile × architecture mode × change condition this
installs the kit under test into a throwaway project, renders the real author
envelope for every full-feature phase and the real reviewer jobs for every
multi-review phase, and records bytes, required skills and reviewer fan-out.

It answers "what does the runner deliver", not "what does a model read": whether a
model opens every listed file is only observable in a live run.

    python evals/phase_budget.py --kit /path/to/kit --output budget.json
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
from pathlib import Path

MODES = ("clean", "stack", "local", "pending")
CONDITIONS = ("none", "baseline", "layer", "committed")

# Detection manifest, a file the profile's baseline group routes, and a file inside
# an architecture layer path. Paths are chosen to match the profile globs.
PROFILES: dict[str, dict[str, object]] = {
    "android": {
        "files": {
            "settings.gradle.kts": 'include(":app")\n',
            "app/build.gradle.kts": 'plugins { id("com.android.application") }\n',
        },
        "baseline": "app/src/main/java/com/example/App.kt",
        "layer": "core/domain/src/main/kotlin/com/example/Order.kt",
    },
    "flutter": {
        "files": {"pubspec.yaml": "name: sample\ndependencies:\n  flutter:\n    sdk: flutter\n"},
        "baseline": "lib/main.dart",
        "layer": "lib/features/orders/order_screen.dart",
    },
    "ios": {
        "files": {"Package.swift": "// swift-tools-version:5.9\nimport PackageDescription\n"},
        "baseline": "App/AppDelegate.swift",
        "layer": "Sources/Features/Orders/OrderView.swift",
    },
    "ktor": {
        "files": {"build.gradle.kts": 'plugins { kotlin("jvm") version "2.0.0" }\n'
                  'dependencies { implementation("io.ktor:ktor-server-core:2.3.0") }\n'},
        "baseline": "src/main/kotlin/com/example/Application.kt",
        "layer": "src/main/kotlin/com/example/domain/Order.kt",
    },
    "nextjs": {
        "files": {"package.json": json.dumps({"name": "s", "dependencies": {"next": "15.0.0", "react": "19.0.0"}})},
        "baseline": "app/page.tsx",
        "layer": "src/features/orders/OrderList.tsx",
    },
    "node": {
        "files": {"package.json": json.dumps({"name": "s", "dependencies": {"express": "4.0.0"}})},
        "baseline": "src/index.js",
        "layer": "src/domain/order.js",
    },
    "python": {
        "files": {"pyproject.toml": '[project]\nname = "s"\nversion = "0"\n'},
        "baseline": "app/main.py",
        "layer": "src/domain/order.py",
    },
    "react-native": {
        "files": {"package.json": json.dumps({"name": "s", "dependencies": {"react-native": "0.76.0", "react": "18.3.1"}})},
        "baseline": "App.tsx",
        "layer": "src/features/orders/OrderScreen.tsx",
    },
    "spring": {
        "files": {"build.gradle.kts": 'plugins {\n    kotlin("jvm") version "2.0.0"\n'
                  '    id("org.springframework.boot") version "3.3.0"\n}\n'},
        "baseline": "src/main/kotlin/com/example/App.kt",
        "layer": "src/main/kotlin/com/example/domain/Order.kt",
    },
    "typescript": {
        "files": {"package.json": json.dumps({"name": "s", "devDependencies": {"typescript": "5.0.0"}})},
        "baseline": "src/index.ts",
        "layer": "src/domain/order.ts",
    },
    "generic": {"files": {"README.md": "sample\n"}, "baseline": "src/main.txt", "layer": "src/domain/order.txt"},
}

LOCAL_CONTRACT = """---
name: architecture
description: Project-local architecture contract for the evaluation fixture.
---

# Project architecture

- Keep domain code free of transport and UI imports.
"""

TASK = "Build the orders list screen from the plan: list, screen state, API integration"

_HEADING = re.compile(r"(?m)^(?=#{1,3} )")


def _git(cwd: Path, *args: str) -> None:
    # 바깥 세션의 GIT_DIR·GIT_WORK_TREE 등이 남아 있으면 임시 프로젝트가 아닌 저장소에 쓴다.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(
        ["git", "-c", "user.email=eval@example.com", "-c", "user.name=eval", *args],
        cwd=cwd, check=True, capture_output=True, env=env,
    )


def _file_read_bytes(resolution) -> int:
    """read plan이 파일로 읽게 하는 본문만 센다. 인라인 본문은 envelope 바이트에 이미 들어 있다."""
    return sum(len(d.content) for d in resolution.delivery if not d.inline)


def _sections(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for part in _HEADING.split(text):
        if not part.strip():
            continue
        title = part.strip().splitlines()[0]
        key = re.sub(r"\s+\d+$", " N", title)[:80]
        out[key] = out.get(key, 0) + len(part.encode())
    return out


def _run_group(args: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    """새 세션으로 실행한다. timeout이면 그 명령이 띄운 installer까지 프로세스 그룹째 끝낸다.

    `subprocess.run(timeout=)`은 직접 띄운 프로세스만 죽여서 손자 프로세스가 임시 폴더에 계속 쓴다.
    """
    process = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        stderr = f"timeout after {timeout}s\n{stderr}"
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def _install(kit: Path, project: Path, profile: str, mode: str) -> str | None:
    flags = ["--root", str(project), "--profile", profile, "--architecture-mode", mode, "--no-hooks"]
    if mode == "local":
        flags += ["--architecture-skill", "skills/architecture/SKILL.md"]
    result = _run_group(["node", str(kit / "bin" / "agent-flow-kit.mjs"), "install", *flags], timeout=600)
    if result.returncode != 0:
        return (result.stderr or result.stdout).strip().splitlines()[-1] if (result.stderr or result.stdout) else "failed"
    return None


def _measure_condition(project: Path, run_id: str) -> dict:
    from agent_flow.adapters.hosted import HostedAdapter, _reviewer_jobs
    from agent_flow.artifact import create_run
    from agent_flow.core.local_skills import changed_files
    from agent_flow.runner import Runner

    run_dir = create_run(project, "full-feature", TASK, run_id=run_id)
    runner = Runner(project, workflow="full-feature", run_dir=run_dir)
    adapter = HostedAdapter("claude")
    adapter._profile_id = runner.profile_id
    adapter._profile_snapshot = runner.profile
    adapter._config_root = runner.config_root
    # runner와 같은 인자로 부른다. profile을 받는 kit이면 커밋된 브랜치 변경까지 라우팅에 들어간다.
    takes_profile = "profile" in inspect.signature(changed_files).parameters
    adapter._changed_files = changed_files(project, runner.profile) if takes_profile else changed_files(project)
    adapter._task_text = TASK
    phases: dict[str, dict] = {}
    for phase in runner.phases:
        entry: dict = {}
        try:
            resolution_kwargs = (
                {"run_dir": run_dir} if "run_dir" in inspect.signature(adapter.phase_resolution).parameters else {}
            )
            resolution = adapter.phase_resolution(phase, project, skill_host="claude", **resolution_kwargs)
            envelope = adapter.render_envelope(phase, run_dir, project, skill_host="claude", resolution=resolution)
            entry["author_bytes"] = len(envelope.encode())
            entry["required"] = [s.name for s in resolution.required]
            # read plan이 없으면 이 phase의 모델은 required 문서를 읽으라는 지시를 받지 않는다.
            entry["read_plan_in_envelope"] = "## Required-read plan" in envelope
            entry["required_read_bytes"] = _file_read_bytes(resolution) if entry["read_plan_in_envelope"] else 0
            entry["roles_in_envelope"] = "\n  roles:\n" in envelope
            if phase.multi_review:
                providers = ("claude", "codex")
                jobs, _ = _reviewer_jobs(phase, run_dir, project, adapter, providers=providers)
                entry["angles"] = [job.angle_id for job in jobs]
                entry["reviewer_subprocesses"] = len(jobs) * len(providers)
                # provider마다 skill 해석과 envelope가 다를 수 있어 따로 잰다.
                entry["reviewer_prompt_bytes_by_provider"] = {
                    provider: [len(job.prompt_by_provider[provider].encode()) for job in jobs]
                    for provider in providers
                }
                entry["reviewer_read_bytes_by_provider"] = {
                    provider: _file_read_bytes(adapter.phase_resolution(phase, project, skill_host=provider))
                    for provider in providers
                }
                entry["reviewer_sections"] = _sections(jobs[0].prompt_by_provider["claude"]) if jobs else {}
        except Exception as exc:  # 한 phase의 실패가 전체 측정을 지우지 않게 기록만 한다.
            entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
        phases[phase.id] = entry
    shutil.rmtree(run_dir, ignore_errors=True)
    return {"changed_files": list(adapter._changed_files), "phases": phases}


def measure_combo(kit: Path, profile: str, mode: str, root: Path | None = None) -> dict:
    sys.path.insert(0, str(kit / "src"))
    os.environ["AGENT_FLOW_HOST"] = "claude"
    spec = PROFILES[profile]
    owned = root is None
    root = root or Path(tempfile.mkdtemp(prefix=f"af-budget-{profile}-{mode}-"))
    try:
        seed = dict(spec["files"])
        if mode == "local":
            seed["skills/architecture/SKILL.md"] = LOCAL_CONTRACT
        for rel, body in seed.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(body, encoding="utf-8")
        _git(root, "init", "-q", "-b", "main")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "seed")
        error = _install(kit, root, profile, mode)
        if error:
            return {"profile": profile, "mode": mode, "install_error": error}
        _git(root, "add", "-A")
        # git 저장소에서 install은 tracked 파일을 남기지 않을 수 있다(ignore 항목은 info/exclude).
        _git(root, "commit", "-q", "--allow-empty", "-m", "install")
        _git(root, "checkout", "-q", "-b", "feat/eval")
        results: dict[str, dict] = {}
        for condition in CONDITIONS:
            if condition == "baseline":
                target = root / str(spec["baseline"])
            elif condition == "layer":
                target = root / str(spec["layer"])
            else:
                target = None
            if target is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f"// {condition}\n", encoding="utf-8")
            if condition == "committed":
                for rel in (spec["baseline"], spec["layer"]):
                    path = root / str(rel)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("// committed\n", encoding="utf-8")
                _git(root, "add", "-A")
                _git(root, "commit", "-qm", "feature")
            try:
                results[condition] = _measure_condition(root, f"budget-{condition}")
            except Exception as exc:
                results[condition] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
            if condition in ("baseline", "layer") and target is not None:
                target.unlink()
        return {"profile": profile, "mode": mode, "conditions": results}
    finally:
        if owned:
            shutil.rmtree(root, ignore_errors=True)


def _worker(args: tuple[str, str, str]) -> dict:
    kit, profile, mode = args
    # 임시 폴더는 부모가 만들고 지운다. 자식이 timeout으로 죽으면 자식의 finally는 돌지 않는다.
    root = Path(tempfile.mkdtemp(prefix=f"af-budget-{profile}-{mode}-"))
    try:
        result = _run_group(
            [sys.executable, __file__, "--kit", kit, "--one", f"{profile}:{mode}", "--root", str(root)],
            timeout=1800,
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if result.returncode != 0:
        return {"profile": profile, "mode": mode, "worker_error": result.stderr[-800:]}
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kit", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--one")
    parser.add_argument("--root", type=Path, help="--one의 임시 프로젝트 폴더(부모 소유)")
    parser.add_argument("--profiles", default=",".join(PROFILES))
    parser.add_argument("--modes", default=",".join(MODES))
    parser.add_argument("--jobs", type=int, default=6)
    args = parser.parse_args()
    kit = args.kit.resolve()
    if args.one:
        profile, mode = args.one.split(":")
        print(json.dumps(measure_combo(kit, profile, mode, args.root)))
        return 0
    combos = [
        (str(kit), p, m) for p in args.profiles.split(",") for m in args.modes.split(",")
    ]
    with concurrent.futures.ThreadPoolExecutor(args.jobs) as pool:
        results = list(pool.map(_worker, combos))
    payload = {"kit": str(kit), "task": TASK, "results": results}
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
