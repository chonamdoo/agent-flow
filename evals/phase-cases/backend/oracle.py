"""Deterministic oracle for the backend (python profile) phase case.

    python oracle.py author --project <dir> --mode <clean|local|team>
    python oracle.py norm --project <dir> --mode <clean|local|team>

Prints {"behavior", "plan", "norm", "checks"} as JSON. `norm` only fills norm checks;
behavior and plan are reported as false there because they were not run.
Python >= 3.9, stdlib only.
"""

import argparse
import ast
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

CASE_DIR = Path(__file__).resolve().parent
CASE = json.loads((CASE_DIR / "case.json").read_text(encoding="utf-8"))
MODES = tuple(CASE["modes"])
TEST_TIMEOUT_S = 120
IGNORED_DIRS = {"__pycache__"}

Check = Dict[str, object]


def _check(check_id: str, ok: bool, detail: str) -> Check:
    return {"id": check_id, "ok": bool(ok), "detail": detail}


# ---------------------------------------------------------------- test runs


def _ran_count(output: str) -> int:
    match = re.search(r"^Ran (\d+) tests? in ", output, re.MULTILINE)
    return int(match.group(1)) if match else 0


def _run_tests(project: Path, argv: List[str]) -> Tuple[bool, str]:
    # 새 세션으로 띄워 timeout이면 테스트가 띄운 자손까지 그룹째 끝낸다.
    try:
        process = subprocess.Popen(
            argv, cwd=str(project), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
    except OSError as error:
        return False, f"{' '.join(argv)}: {error}"
    try:
        stdout, stderr = process.communicate(timeout=TEST_TIMEOUT_S)
    except subprocess.TimeoutExpired as error:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        return False, f"{' '.join(argv)}: {error}"
    result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    output = result.stdout + result.stderr
    ran = _ran_count(output)
    ok = result.returncode == 0 and ran > 0
    tail = output.strip().splitlines()[-1] if output.strip() else ""
    return ok, f"exit={result.returncode} ran={ran} last={tail!r}"


def _copy_tree(source: Path, target: Path) -> List[Path]:
    copied = []
    for path in sorted(source.rglob("*")):
        if path.is_file() and not (set(path.relative_to(source).parts) & IGNORED_DIRS):
            destination = target / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
            copied.append(path.relative_to(source))
    return copied


def behavior_check(project: Path, mode: str) -> Check:
    # Restore the visible tests so an author that edits them cannot pass by rewriting them.
    restored = _copy_tree(CASE_DIR / "author" / mode / "pre" / "tests", project / "tests")
    ok, detail = _run_tests(project, list(CASE["test_command"]))
    return _check("visible-tests", ok, f"restored={len(restored)} {detail}")


def plan_check(project: Path, mode: str) -> Check:
    hidden_root = CASE_DIR / "author" / mode / "hidden"
    copied = _copy_tree(hidden_root, project)
    modules = [
        ".".join(relative.with_suffix("").parts)
        for relative in copied if relative.suffix == ".py" and relative.name.startswith("test_")
    ]
    if not modules:
        return _check("hidden-tests", False, f"no hidden test modules under {hidden_root}")
    ok, detail = _run_tests(project, [CASE["test_command"][0], "-m", "unittest", *modules])
    return _check("hidden-tests", ok, detail)


# ---------------------------------------------------------------- source model


class Module:
    def __init__(self, src: Path, path: Path) -> None:
        self.path = path
        self.relative = path.relative_to(src)
        parts = list(self.relative.with_suffix("").parts)
        self.is_package = parts[-1] == "__init__"
        if self.is_package:
            parts = parts[:-1]
        self.name = ".".join(parts)
        self.tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def imports(self) -> List[str]:
        """Absolute dotted names this module imports (relative imports resolved)."""
        package = self.name.split(".") if self.is_package else self.name.split(".")[:-1]
        names: List[str] = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
                    prefix = ".".join(base + ([node.module] if node.module else []))
                else:
                    prefix = node.module or ""
                names.append(prefix)
                names.extend(f"{prefix}.{alias.name}" for alias in node.names if alias.name != "*")
        return [name for name in names if name]

    def classes(self) -> List[ast.ClassDef]:
        return [node for node in ast.walk(self.tree) if isinstance(node, ast.ClassDef)]


def _base_names(node: ast.ClassDef) -> List[str]:
    names = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.append(base.id)
        elif isinstance(base, ast.Attribute):
            names.append(base.attr)
        elif isinstance(base, ast.Subscript):
            value = base.value
            names.append(value.id if isinstance(value, ast.Name) else getattr(value, "attr", ""))
    return names


def _source_modules(project: Path) -> Tuple[List[Module], List[str]]:
    src = project / "src"
    modules, errors = [], []
    for path in sorted(src.rglob("*.py")) if src.is_dir() else ():
        if set(path.relative_to(src).parts) & IGNORED_DIRS:
            continue
        try:
            modules.append(Module(src, path))
        except SyntaxError as error:
            errors.append(f"{path.relative_to(project)}: {error}")
    return modules, errors


def _in_package(name: str, *roots: str) -> bool:
    return any(name == root or name.startswith(root + ".") for root in roots)


# ---------------------------------------------------------------- clean norm

DOMAIN_ROOTS = ("domain", "core.domain")
DATA_ROOTS = ("data", "core.data")
COMPOSITION_ROOTS = ("app",)
INBOUND_ROOTS = ("api", "features")
DOMAIN_FORBIDDEN_IMPORTS = (
    "sqlite3", "json", "http", "urllib", "requests", "httpx", "flask", "fastapi", "django",
    "starlette", "sqlalchemy", "pydantic", "os", "io", "socket",
)


def clean_checks(project: Path, modules: List[Module]) -> List[Check]:
    domain = [module for module in modules if _in_package(module.name, *DOMAIN_ROOTS)]
    data = [module for module in modules if _in_package(module.name, *DATA_ROOTS)]
    checks = [
        _check(
            "clean-layers-present", bool(domain) and bool(data),
            f"domain modules={len(domain)} data modules={len(data)} (src/domain|src/core/domain, src/data|src/core/data)",
        ),
    ]

    impure = [
        f"{module.relative}: {name}"
        for module in domain for name in module.imports()
        if _in_package(name, *DATA_ROOTS, *COMPOSITION_ROOTS, *INBOUND_ROOTS)
        or _in_package(name, *DOMAIN_FORBIDDEN_IMPORTS)
    ]
    checks.append(_check("domain-imports-inward-only", not impure, "; ".join(impure) or "domain imports only domain/stdlib policy"))

    ports = [
        f"{module.relative}:{node.name}"
        for module in domain for node in module.classes()
        if "Repository" in node.name and set(_base_names(node)) & {"Protocol", "ABC"}
    ]
    checks.append(_check("repository-port-in-domain", bool(ports), ", ".join(ports) or "no Protocol/ABC *Repository class in domain"))

    impls = [
        f"{module.relative}:{node.name}"
        for module in data for node in module.classes()
        if "Repository" in node.name and not set(_base_names(node)) & {"Protocol", "ABC"}
    ]
    impls_use_domain = any(
        _in_package(name, *DOMAIN_ROOTS) for module in data for name in module.imports()
    )
    checks.append(_check(
        "repository-impl-in-data", bool(impls) and impls_use_domain,
        f"impls={', '.join(impls) or 'none'} data-imports-domain={impls_use_domain}",
    ))

    leaks = [
        f"{module.relative}: {name}"
        for module in modules
        if not _in_package(module.name, *COMPOSITION_ROOTS, *DATA_ROOTS)
        for name in module.imports() if _in_package(name, *DATA_ROOTS)
    ]
    checks.append(_check("data-wired-only-in-composition-root", not leaks, "; ".join(leaks) or "only app/ and data/ import data"))

    outward = [
        f"{module.relative}: {name}"
        for module in data for name in module.imports()
        if _in_package(name, *COMPOSITION_ROOTS, *INBOUND_ROOTS)
    ]
    checks.append(_check("data-no-inbound-imports", not outward, "; ".join(outward) or "data imports no app/api"))
    return checks


# ---------------------------------------------------------------- local norm

LAYER_DIR_NAMES = {
    "domain", "data", "app", "api", "core", "features", "application", "infrastructure",
    "infra", "adapters", "ports", "repositories", "presentation", "usecases", "use_cases",
}


def local_checks(project: Path, modules: List[Module]) -> List[Check]:
    src = project / "src"
    directories = [
        path.relative_to(src) for path in sorted(src.rglob("*"))
        if path.is_dir() and not (set(path.relative_to(src).parts) & IGNORED_DIRS)
    ] if src.is_dir() else []
    nested = [str(path) for path in directories if len(path.parts) > 1]
    layers = [str(path) for path in directories if set(path.parts) & LAYER_DIR_NAMES]
    feature = src / "orders"
    checks = [
        _check("flat-feature-package", feature.is_dir() and not nested,
               f"src/orders exists={feature.is_dir()} nested={nested or 'none'}"),
        _check("no-layer-packages", not layers, ", ".join(layers) or "no layer package under src/"),
    ]

    ports = [
        f"{module.relative}:{node.name}"
        for module in modules for node in module.classes()
        if node.name.endswith(("Repository", "Port"))
        or set(_base_names(node)) & {"Protocol", "ABC"}
    ]
    checks.append(_check("no-repository-ports", not ports, ", ".join(ports) or "no port/repository classes"))

    feature_modules = [module for module in modules if module.relative.parts[:1] == ("orders",) and not module.is_package]
    unprefixed = [str(module.relative) for module in feature_modules if not module.relative.name.startswith("ord_")]
    checks.append(_check(
        "ord-prefix-modules", bool(feature_modules) and not unprefixed,
        f"modules={len(feature_modules)} unprefixed={unprefixed or 'none'}",
    ))
    return checks


# ---------------------------------------------------------------- team norm


def team_checks(project: Path, modules: List[Module]) -> List[Check]:
    banned = []
    for module in modules:
        banned.extend(f"{module.relative}: import {name}" for name in module.imports() if _in_package(name, "dataclasses"))
        for node in module.classes():
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
                if name == "dataclass":
                    banned.append(f"{module.relative}: @dataclass {node.name}")
    tuples = [
        f"{module.relative}:{node.name}"
        for module in modules for node in module.classes() if "NamedTuple" in _base_names(node)
    ]
    return [
        _check("no-dataclasses", not banned, "; ".join(banned) or "no dataclasses under src/"),
        _check("namedtuple-value-types", bool(tuples), ", ".join(tuples) or "no NamedTuple value type under src/"),
    ]


NORMS: Dict[str, Callable[[Path, List[Module]], List[Check]]] = {
    "clean": clean_checks,
    "local": local_checks,
    "team": team_checks,
}


def norm_checks(project: Path, mode: str) -> List[Check]:
    modules, errors = _source_modules(project)
    checks = [_check("source-parses", not errors and bool(modules), "; ".join(errors) or f"{len(modules)} modules")]
    return checks + NORMS[mode](project, modules)


# ---------------------------------------------------------------- entry


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("author", "norm"))
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=MODES)
    args = parser.parse_args(argv)
    project = args.project.resolve()

    norms = norm_checks(project, args.mode)
    result: Dict[str, object] = {"behavior": False, "plan": False, "norm": all(check["ok"] for check in norms)}
    checks: List[Check] = []
    if args.command == "author":
        behavior = behavior_check(project, args.mode)
        plan = plan_check(project, args.mode)
        result["behavior"], result["plan"] = behavior["ok"], plan["ok"]
        checks.extend((behavior, plan))
    result["checks"] = checks + norms
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
