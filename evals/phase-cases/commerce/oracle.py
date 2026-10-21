"""Deterministic oracle for the commerce phase case (nextjs + python in one repository).

    python oracle.py author --project <dir> --mode clean
    python oracle.py norm --project <dir> --mode clean

Prints {"behavior", "plan", "norm", "checks"} as JSON. `norm` only fills norm checks.
Python >= 3.9, stdlib only.
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
API_DOMAIN = Path("api/src/shop/cart/domain.py")
FORBIDDEN_API_DOMAIN_IMPORTS = ("http", "json", "shop.cart.http", "urllib", "flask", "fastapi", "django")
IMPORT_RE = re.compile(r"""(?:import|export)\s[^'"]*?from\s+['"]([^'"]+)['"]|import\s+['"]([^'"]+)['"]""")

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
    node_passed = sum(int(n) for n in NODE_PASS.findall(output))
    python_ran = sum(int(n) for n in re.findall(r"^Ran (\d+) tests? in ", output, re.MULTILINE))
    # Both runners must have executed tests; an empty suite on either side is not a pass.
    ok = result.returncode == 0 and node_passed > 0 and python_ran > 0
    return {"ok": ok, "detail": f"exit={result.returncode} node_pass={node_passed} python_ran={python_ran}"}


def behavior_check(project: Path, mode: str) -> Check:
    # Restore the visible tests so an author that edits them cannot pass by rewriting them.
    restored = _copy_tree(CASE_DIR / "author" / mode / "pre", project)
    result = _run(project, list(CASE["test_command"]))
    return _check("visible-tests", result["ok"], f"restored={len(restored)} {result['detail']}")


def plan_check(project: Path, mode: str) -> Check:
    copied = _copy_tree(CASE_DIR / "author" / mode / "hidden", project)
    result = _run(project, list(CASE["hidden_test_command"]))
    return _check("slice-plan-decisions", result["ok"], f"hidden={len(copied)} {result['detail']}")


def _python_imports(path: Path) -> List[str]:
    names = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def _ts_imports(path: Path) -> List[str]:
    text = path.read_text(encoding="utf-8")
    return [first or second for first, second in IMPORT_RE.findall(text)]


def _resolve(source: Path, specifier: str, project: Path) -> str:
    if not specifier.startswith("."):
        return specifier
    return (source.parent / specifier).resolve().relative_to(project.resolve()).as_posix()


def norm_checks(project: Path, mode: str) -> List[Check]:
    checks = []
    domain = project / API_DOMAIN
    if not domain.is_file():
        checks.append(_check("api-domain-exists", False, f"{API_DOMAIN} missing"))
    else:
        leaked = [name for name in _python_imports(domain)
                  if any(name == bad or name.startswith(bad + ".") for bad in FORBIDDEN_API_DOMAIN_IMPORTS)]
        checks.append(_check("api-domain-pure", not leaked, f"imports {leaked}" if leaked else "no adapter imports"))
    web_domain = sorted((project / "src" / "core" / "domain").rglob("*.ts"))
    outside = [f"{path.relative_to(project)} -> {target}" for path in web_domain for spec in _ts_imports(path)
               for target in [_resolve(path, spec, project)]
               if spec.startswith(".") and not target.startswith("src/core/domain/")]
    checks.append(_check("web-domain-inward", bool(web_domain) and not outside,
                         "; ".join(outside) if outside else f"{len(web_domain)} domain files"))
    presentation = sorted((project / "src" / "features").rglob("presentation/*.ts"))
    data_imports = [f"{path.relative_to(project)} -> {target}" for path in presentation for spec in _ts_imports(path)
                    for target in [_resolve(path, spec, project)]
                    if target.startswith("src/core/data/")]
    checks.append(_check("web-presentation-uses-port", bool(presentation) and not data_imports,
                         "; ".join(data_imports) if data_imports else f"{len(presentation)} presentation files"))
    return checks


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
