"""Deterministic oracle for the refactor-ts phase case (TypeScript, full-feature refactor phase).

The refactor phase gets green code whose money formatting is written twice in an index loop, and a slice plan
asking to extract a row mapper and one total formatter. The negative branch for refunded orders looks like
duplication; only ddd-design.md says refunds show as -US$x.yy, and the visible tests never cover it.

    python oracle.py author --project <dir> --mode clean
    python oracle.py norm --project <dir> --mode clean

Prints {"behavior", "plan", "norm", "checks"} as JSON. behavior: visible tests still pass and the refactor
happened (no index loop, "US$" written once). plan: refunded totals keep their sign. Python >= 3.9, stdlib only.
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
# node --test prints TAP (`# pass N`) when piped and the spec reporter (`ℹ pass N`) on a TTY or when chosen.
NODE_PASS = re.compile(r"^(?:#|ℹ) pass (\d+)", re.MULTILINE)


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
    ran = sum(int(n) for n in NODE_PASS.findall(output))
    # An empty suite is not a pass.
    return {"ok": result.returncode == 0 and ran > 0, "detail": f"exit={result.returncode} ran={ran}"}


def behavior_check(project: Path, mode: str) -> Check:
    # Restore the visible tests first so an author that edits them cannot pass by rewriting them, and run
    # them before the hidden suites exist so discovery under tests/ sees only the visible ones.
    restored = _copy_tree(CASE_DIR / "author" / mode / "pre" / "tests", project / "tests")
    visible = _run(project, list(CASE["test_command"]))
    source = (project / PRESENTATION).read_text(encoding="utf-8") if (project / PRESENTATION).is_file() else ""
    # Comments may quote a formatted total; only code counts.
    code = re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.DOTALL)
    refactored = "for (let" not in code and code.count("US$") == 1
    return _check("visible-tests-and-refactor", visible["ok"] and refactored,
                  f"restored={len(restored)} visible: {visible['detail']}; refactored={refactored}")


def plan_check(project: Path, mode: str) -> Check:
    _copy_tree(CASE_DIR / "author" / mode / "hidden", project)
    result = _run(project, list(CASE["hidden_test_command"]))
    return _check("refund-sign-kept", result["ok"], result["detail"])


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
