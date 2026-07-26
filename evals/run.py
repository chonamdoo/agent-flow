#!/usr/bin/env python3
"""eval 러너.

`(case × config × trial)`마다 격리된 임시 프로젝트를 만들고, host CLI에 과제를
주고, 기계 oracle로 채점한다. agent가 남긴 어떤 자기신고도 점수에 들어가지 않는다.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CASES = ROOT / "cases"
RESULTS = ROOT / "results"

sys.path.insert(0, str(ROOT))
from configs import CONFIGS  # noqa: E402


HOSTS = {
    # host마다 "비대화형으로 과제 하나를 끝까지 수행" 형태가 다르다.
    "claude": lambda task: ("claude", "-p", task, "--permission-mode", "acceptEdits", "--output-format", "text"),
    "codex": lambda task: ("codex", "exec", "--full-auto", task),
}


def load_oracle(case: Path):
    spec = importlib.util.spec_from_file_location(f"eval_{case.name}", case / "check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.score


def run_once(case: Path, config: str, host: str, timeout_s: int) -> dict:
    task = (case / "task.md").read_text(encoding="utf-8").strip()
    with tempfile.TemporaryDirectory() as raw:
        project = Path(raw) / "project"
        shutil.copytree(case / "seed", project)
        CONFIGS[config](project)
        started = time.time()
        try:
            completed = subprocess.run(
                HOSTS[host](task), cwd=project, capture_output=True, text=True, timeout=timeout_s
            )
            host_ok = completed.returncode == 0
        except subprocess.TimeoutExpired:
            host_ok = False
        elapsed = round(time.time() - started, 1)
        # host가 죽어도 채점한다. 부분 편집이 남았을 수 있고, "안 돌았다"와
        # "돌았는데 틀렸다"를 점수에서 구분하지 않는 편이 정직하다.
        scores = load_oracle(case)(project)
    return {"case": case.name, "config": config, "host_ok": host_ok, "seconds": elapsed, **scores}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="claude", choices=sorted(HOSTS))
    parser.add_argument("--case", action="append", default=None)
    parser.add_argument("--config", action="append", default=None)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    cases = [CASES / name for name in (args.case or sorted(p.name for p in CASES.iterdir() if p.is_dir()))]
    configs = args.config or list(CONFIGS)
    rows: list[dict] = []
    for case in cases:
        for config in configs:
            for trial in range(args.trials):
                row = run_once(case, config, args.host, args.timeout)
                row["trial"] = trial
                rows.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)

    RESULTS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    report = {"host": args.host, "trials": args.trials, "rows": rows, "summary": summarize(rows)}
    (RESULTS / f"{stamp}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(render(report["summary"]))
    return 0


def summarize(rows: list[dict]) -> list[dict]:
    axes = ("behavior", "norm")
    summary = []
    for config in dict.fromkeys(row["config"] for row in rows):
        group = [row for row in rows if row["config"] == config]
        entry = {"config": config, "n": len(group)}
        for axis in axes:
            entry[axis] = round(statistics.mean(1.0 if row[axis] else 0.0 for row in group), 3)
        entry["both"] = round(
            statistics.mean(1.0 if all(row[axis] for axis in axes) else 0.0 for row in group), 3
        )
        summary.append(entry)
    return summary


def render(summary: list[dict]) -> str:
    header = f"{'config':22s} {'n':>3s} {'behavior':>9s} {'norm':>6s} {'both':>6s}"
    lines = [header, "-" * len(header)]
    for entry in summary:
        lines.append(
            f"{entry['config']:22s} {entry['n']:3d} {entry['behavior']:9.0%} {entry['norm']:6.0%} {entry['both']:6.0%}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
