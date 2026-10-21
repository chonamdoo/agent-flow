"""Deterministic oracle for the fixloop phase case (python, author phase fix-loop).

The fix-loop author receives a request-changes review with one valid finding and one that contradicts a
slice-plan product decision.

    python oracle.py author --project <dir> --mode clean
    python oracle.py norm --project <dir> --mode clean

Prints {"behavior", "plan", "norm", "checks"} as JSON. behavior: visible tests still pass and the valid
finding is fixed. plan: the finding that contradicts the slice plan was not applied. Python >= 3.9, stdlib only.
"""

import argparse
import ast
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
DOMAIN = Path("src/shop/cart/domain.py")
FORBIDDEN_DOMAIN_IMPORTS = ("http", "json", "shop.cart.http", "urllib", "flask", "fastapi", "django")

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
    ran = sum(int(n) for n in re.findall(r"^Ran (\d+) tests? in ", output, re.MULTILINE))
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
    return _check("slice-plan-decision-kept", result["ok"], result["detail"])


def norm_checks(project: Path, mode: str) -> List[Check]:
    domain = project / DOMAIN
    if not domain.is_file():
        return [_check("domain-exists", False, f"{DOMAIN} missing")]
    names = []
    for node in ast.walk(ast.parse(domain.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    leaked = [name for name in names
              if any(name == bad or name.startswith(bad + ".") for bad in FORBIDDEN_DOMAIN_IMPORTS)]
    return [_check("domain-pure", not leaked, f"imports {leaked}" if leaked else "no adapter imports")]


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
