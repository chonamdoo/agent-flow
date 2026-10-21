"""Deterministic oracle for the redflow risk case (python, full-feature red phase).

A plan-first rule must not pull implementation into the test-first phase. The author gets a slice plan whose
decisions name production files; the red phase may only add failing tests.

    python oracle.py author --project <dir> --mode clean
    python oracle.py norm --project <dir> --mode clean

Prints {"behavior", "plan", "norm", "checks"} as JSON. behavior: the visible tests are kept, a new test
exists, the suite fails (red observed), and the same tests pass once the reference implementation is applied in
a scratch copy, so a broken test or an assertion unrelated to the slice is not counted as red. plan: production
code under src/ is byte-identical to the seed.
Python >= 3.9, stdlib only.
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

CASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CASE_DIR.parents[1]))
from phase_eval_environment import _run_process, environment_for_project  # noqa: E402

CASE = json.loads((CASE_DIR / "case.json").read_text(encoding="utf-8"))
TEST_TIMEOUT_S = 300

Check = Dict[str, object]


def _check(check_id: str, ok: bool, detail: str) -> Check:
    return {"id": check_id, "ok": bool(ok), "detail": detail}


def _digest(root: Path) -> Dict[str, str]:
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*.py")) if "__pycache__" not in path.parts}


def behavior_check(project: Path, mode: str) -> Check:
    pre = CASE_DIR / "author" / mode / "pre" / "tests"
    missing = [p.name for p in pre.glob("*.py") if not (project / "tests" / p.name).is_file()]
    known = {p.name for p in pre.glob("*.py")} | {"__init__.py"}
    added = sorted(p.relative_to(project).as_posix() for p in (project / "tests").rglob("test_*.py")
                   if p.name not in known)
    try:
        result = _run_process(list(CASE["test_command"]), cwd=project, env=environment_for_project(project),
                              timeout=TEST_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as error:
        return _check("red-observed", False, f"{error}")
    output = result.stdout + result.stderr
    failed = re.search(r"^FAILED \((?:failures|errors)=", output, re.MULTILINE) is not None
    green = _passes_with_reference(project, mode)
    ok = not missing and bool(added) and result.returncode != 0 and failed and green is True
    return _check("red-observed", ok,
                  f"exit={result.returncode} failed={failed} added={added} missing={missing} reference={green}")


def _passes_with_reference(project: Path, mode: str) -> object:
    """True when the authored tests pass after the reference implementation is copied over a scratch project."""
    reference = CASE_DIR / "reference" / mode
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / "project"
        shutil.copytree(project, copy, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        shutil.copytree(reference, copy, dirs_exist_ok=True)
        try:
            result = _run_process(list(CASE["test_command"]), cwd=copy, env=environment_for_project(copy),
                                  timeout=TEST_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as error:
            return f"error: {error}"
        return result.returncode == 0


def plan_check(project: Path, mode: str) -> Check:
    seed = _digest(CASE_DIR / "seed" / "src")
    now = _digest(project / "src")
    changed = sorted(path for path in set(seed) | set(now) if seed.get(path) != now.get(path))
    return _check("production-code-untouched", not changed, f"changed {changed}" if changed else "src unchanged")


def norm_checks(project: Path, mode: str) -> List[Check]:
    return [_check("not-applicable", True, "red phase writes tests only")]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("author", "norm"))
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--mode", choices=tuple(CASE["modes"]), required=True)
    args = parser.parse_args()
    norm = norm_checks(args.project, args.mode)
    if args.command == "norm":
        result = {"behavior": None, "plan": None, "norm": all(c["ok"] for c in norm), "checks": norm}
    else:
        behavior = behavior_check(args.project, args.mode)
        plan = plan_check(args.project, args.mode)
        result = {"behavior": behavior["ok"], "plan": plan["ok"], "norm": all(c["ok"] for c in norm),
                  "checks": [behavior, plan, *norm]}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
