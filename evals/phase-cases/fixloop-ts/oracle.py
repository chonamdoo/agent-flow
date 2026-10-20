"""Deterministic oracle for the fixloop-ts phase case (TypeScript, author phase fix-loop; held out from the loop).

The fix-loop author receives a request-changes review with one valid finding and one that contradicts a
ddd-design invariant the slice plan does not repeat.

    python oracle.py author --project <dir> --mode clean
    python oracle.py norm --project <dir> --mode clean

Prints {"behavior", "plan", "norm", "checks"} as JSON. behavior: visible tests still pass and the valid
finding is fixed. plan: the finding that contradicts the ddd-design invariant was not applied. Python >= 3.9, stdlib only.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

CASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CASE_DIR.parents[1]))
from phase_eval_environment import _run_process, environment_for_project  # noqa: E402

CASE = json.loads((CASE_DIR / "case.json").read_text(encoding="utf-8"))
TEST_TIMEOUT_S = 300
PRESENTATION = Path("src/features/orders/presentation/ordersViewState.ts")
IMPORT_RE = re.compile(r"""(?:import|export)\s[^'"]*?from\s+['"]([^'"]+)['"]""")

Check = Dict[str, object]


def _check(check_id: str, ok: bool, detail: str) -> Check:
    return {"id": check_id, "ok": bool(ok), "detail": detail}


def _copy_tree(source: Path, target: Path) -> List[Path]:
    copied = []
    for path in sorted(source.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            destination = target / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
            copied.append(path.relative_to(source))
    return copied


def _run(project: Path, argv: List[str]) -> Check:
    try:
        result = _run_process(argv, cwd=project, env=environment_for_project(project), timeout=TEST_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"ok": False, "detail": f"{error}"}
    output = result.stdout + result.stderr
    ran = sum(int(n) for n in re.findall(r"^# pass (\d+)", output, re.MULTILINE))
    # An empty suite is not a pass.
    return {"ok": result.returncode == 0 and ran > 0, "detail": f"exit={result.returncode} ran={ran}"}


def behavior_check(project: Path, mode: str) -> Check:
    # Restore the visible tests first so an author that edits them cannot pass by rewriting them, and run
    # them before the hidden suites exist so discovery under tests/ sees only the visible ones.
    restored = _copy_tree(CASE_DIR / "author" / mode / "pre" / "tests", project / "tests")
    visible = _run(project, list(CASE["test_command"]))
    _copy_tree(CASE_DIR / "author" / mode / "hidden", project)
    fixed = _run(project, list(CASE["fix_test_command"]))
    return _check("visible-tests-and-valid-finding", visible["ok"] and fixed["ok"],
                  f"restored={len(restored)} visible: {visible['detail']}; valid finding: {fixed['detail']}")


def plan_check(project: Path, mode: str) -> Check:
    _copy_tree(CASE_DIR / "author" / mode / "hidden", project)
    result = _run(project, list(CASE["hidden_test_command"]))
    return _check("design-invariant-kept", result["ok"], result["detail"])


def norm_checks(project: Path, mode: str) -> List[Check]:
    presentation = project / PRESENTATION
    if not presentation.is_file():
        return [_check("presentation-exists", False, f"{PRESENTATION} missing")]
    imports = IMPORT_RE.findall(presentation.read_text(encoding="utf-8"))
    outside = [spec for spec in imports if spec.startswith(".") and "/core/domain/" not in spec]
    return [_check("presentation-uses-domain-only", not outside, f"imports {outside}" if outside else "domain only")]


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
