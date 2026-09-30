"""Oracle for the React Native orders-list phase case.

`author` runs the visible tests, copies in and runs the hidden slice-plan tests,
then statically checks the selected mode's rule. `norm` runs only the static
checks. Norm checks read source files only, never agent reports.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

CASE_DIR = Path(__file__).resolve().parent
CASE = json.loads((CASE_DIR / "case.json").read_text(encoding="utf-8"))
SOURCE_SUFFIXES = (".ts", ".tsx")
TIMEOUT_SECONDS = 180

IMPORT_PATTERNS = (
    re.compile(r"""(?:^|[\s;])(?:import|export)\s+(?:type\s+)?(?:[^'";]*?\s+from\s+)?['"]([^'"]+)['"]""", re.M),
    re.compile(r"""import\(\s*['"]([^'"]+)['"]\s*\)"""),
    re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)"""),
)
BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
LINE_COMMENT = re.compile(r"(^|[^:'\"\w])//[^\n]*")


class Source:
    def __init__(self, project: Path, path: Path) -> None:
        self.path = path
        self.rel = path.relative_to(project).as_posix()
        raw = path.read_text(encoding="utf-8", errors="replace")
        self.code = LINE_COMMENT.sub(r"\1", BLOCK_COMMENT.sub("", raw))
        self.imports: List[str] = []
        self.bare_imports: List[str] = []
        for pattern in IMPORT_PATTERNS:
            for spec in pattern.findall(self.code):
                if spec.startswith("."):
                    self.imports.append(_resolve(project, path.parent, spec))
                else:
                    self.bare_imports.append(spec)


def _resolve(project: Path, base: Path, spec: str) -> str:
    target = Path(os.path.normpath(base / spec))
    candidates = [target] + [Path(f"{target}{suffix}") for suffix in SOURCE_SUFFIXES]
    candidates += [target / f"index{suffix}" for suffix in SOURCE_SUFFIXES]
    chosen = next((candidate for candidate in candidates if candidate.is_file()), target)
    try:
        return chosen.relative_to(project).as_posix()
    except ValueError:
        return chosen.as_posix()


def _sources(project: Path) -> List[Source]:
    root = project / "src"
    if not root.is_dir():
        return []
    return [
        Source(project, path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix in SOURCE_SUFFIXES and "node_modules" not in path.parts
    ]


def _under(sources: List[Source], prefix: str) -> List[Source]:
    return [source for source in sources if source.rel.startswith(prefix)]


def _check(check_id: str, problems: List[str], ok_detail: str) -> Dict[str, object]:
    return {"id": check_id, "ok": not problems, "detail": "; ".join(problems) if problems else ok_detail}


def _src_dirs(project: Path) -> List[str]:
    root = project / "src"
    if not root.is_dir():
        return []
    return sorted(path.relative_to(project).as_posix() for path in root.rglob("*") if path.is_dir())


# ---------------------------------------------------------------- clean mode
CLEAN_DOMAIN_FORBIDDEN = re.compile(r"\b\w*(?:Dto|ApiClient|View|Screen)\w*\b|\bgetJson\b|\bfetch\s*\(")
CLEAN_FEATURE_FORBIDDEN = re.compile(r"\b\w*(?:Dto|ApiClient|DataSource|Entity|RepositoryImpl)\w*\b|\bgetJson\b|\bfetch\s*\(")
CLEAN_FEATURE_API_FORBIDDEN = re.compile(r"\b\w*(?:Screen|ViewModel|RepositoryImpl|ApiClient)\w*\b")


def _clean_norm(project: Path) -> List[Dict[str, object]]:
    sources = _sources(project)
    domain = _under(sources, "src/core/domain/")
    data = _under(sources, "src/core/data/")
    features = _under(sources, "src/features/")
    checks = []

    problems = []
    for source in domain:
        problems += [f"{source.rel} imports package {spec}" for spec in source.bare_imports]
        problems += [
            f"{source.rel} imports {target}"
            for target in source.imports
            if not target.startswith("src/core/domain/")
        ]
    if not domain:
        problems.append("no domain modules under src/core/domain/")
    checks.append(_check("clean-domain-imports", problems, "domain imports only domain"))

    problems = [
        f"{source.rel} uses {match.group(0)!r}"
        for source in domain
        for match in [CLEAN_DOMAIN_FORBIDDEN.search(source.code)]
        if match
    ]
    checks.append(_check("clean-domain-forbidden-names", problems, "no Dto/ApiClient/View/Screen/network in domain"))

    port = re.compile(r"export\s+(?:interface\s+\w*Repository\b|type\s+\w*Repository\s*=)")
    ports = [source.rel for source in domain if port.search(source.code)]
    checks.append(_check(
        "clean-repository-port-in-domain",
        [] if ports else ["no exported *Repository interface under src/core/domain/"],
        f"port in {', '.join(ports)}",
    ))

    implementation = re.compile(r"class\s+\w+\s+implements\s+[\w\s,]*\w*Repository\b")
    impls = [
        source.rel for source in data
        if implementation.search(source.code)
        and any(target.startswith("src/core/domain/") for target in source.imports)
    ]
    checks.append(_check(
        "clean-repository-impl-in-data",
        [] if impls else ["no class under src/core/data/ implements a domain *Repository port"],
        f"implementation in {', '.join(impls)}",
    ))

    problems = []
    for source in features:
        problems += [
            f"{source.rel} imports {target}"
            for target in source.imports
            if target.startswith("src/core/data/") or target == "src/shared/http.ts"
        ]
        match = CLEAN_FEATURE_FORBIDDEN.search(source.code)
        if match:
            problems.append(f"{source.rel} uses {match.group(0)!r}")
    checks.append(_check("clean-presentation-isolation", problems, "features depend on domain only"))

    ui_state = re.compile(r"(?:type|interface)\s+\w*UiState\b")
    holders = [source.rel for source in _under(sources, "src/features/orders/presentation/") if ui_state.search(source.code)]
    checks.append(_check(
        "clean-presentation-ui-state",
        [] if holders else ["no *UiState type under src/features/orders/presentation/"],
        f"UiState in {', '.join(holders)}",
    ))

    api = _under(sources, "src/features/orders/api/")
    problems = [] if api else ["no feature-api module under src/features/orders/api/ (profile pair_with)"]
    for source in api:
        problems += [
            f"{source.rel} imports {target}"
            for target in source.imports
            if "/presentation/" in target or target.startswith("src/core/data/")
        ]
        match = CLEAN_FEATURE_API_FORBIDDEN.search(source.code)
        if match:
            problems.append(f"{source.rel} uses {match.group(0)!r}")
    checks.append(_check("clean-feature-api-entry", problems, "feature-api exposes a contract only"))
    return checks


# ---------------------------------------------------------------- local mode
LAYER_SEGMENTS = {"domain", "data", "presentation"}


def _local_norm(project: Path) -> List[Dict[str, object]]:
    sources = _sources(project)
    checks = []

    problems = [
        directory for directory in _src_dirs(project)
        if directory in {"src/core", "src/features"} or LAYER_SEGMENTS & set(directory.split("/"))
        or directory.startswith("src/orders/")
    ]
    problems += [
        f"{source.rel} outside a flat feature folder"
        for source in sources
        if not source.rel.startswith("src/shared/") and source.rel.count("/") != 2
    ]
    if not _under(sources, "src/orders/"):
        problems.append("no src/orders/ feature folder")
    checks.append(_check("local-flat-feature-folder", problems, "orders code is flat in src/orders/"))

    name = re.compile(r"^orders\.[a-z]+\.tsx?$")
    problems = [
        source.rel for source in _under(sources, "src/orders/")
        if not name.match(source.path.name)
    ]
    checks.append(_check("local-feature-file-names", problems, "files follow orders.<role>.ts(x)"))

    ceremony = re.compile(r"\b(?:class|interface|type)\s+(\w*(?:Repository|UseCase)\w*)")
    problems = [
        f"{source.rel} declares {match}"
        for source in sources
        for match in ceremony.findall(source.code)
    ]
    checks.append(_check("local-no-ceremony-types", problems, "no Repository/UseCase declarations"))

    exported = re.compile(r"\bexport\s+(?:declare\s+)?(?:type|interface)\s+(\w+)")
    feature = [source for source in sources if not source.rel.startswith("src/shared/")]
    names = [(source.rel, match) for source in feature for match in exported.findall(source.code)]
    problems = [f"{rel} exports type {match}" for rel, match in names if not match.endswith("Shape")]
    if not any(match == "OrderShape" for _, match in names):
        problems.append("OrderShape is not exported")
    checks.append(_check("local-shape-type-names", problems, "exported types end with Shape"))
    return checks


# ---------------------------------------------------------------- team mode
def _team_norm(project: Path) -> List[Dict[str, object]]:
    sources = _sources(project)
    features = _under(sources, "src/features/")
    checks = []

    fetch_call = re.compile(r"\bfetch\s*\(")
    problems = []
    for source in features:
        io = "src/shared/http.ts" in source.imports or fetch_call.search(source.code)
        if io and not source.rel.endswith(".remote.ts"):
            problems.append(f"{source.rel} performs I/O outside a .remote.ts module")
    checks.append(_check("team-remote-only-io", problems, "only .remote.ts modules touch http/fetch"))

    remote = next((source for source in features if source.rel == "src/features/orders/orders.remote.ts"), None)
    remote_problems = []
    if remote is None:
        remote_problems.append("src/features/orders/orders.remote.ts is missing")
    elif "src/shared/http.ts" not in remote.imports and not fetch_call.search(remote.code):
        remote_problems.append("orders.remote.ts does not read remote data")
    elif not any(remote.rel in source.imports for source in features if source is not remote):
        remote_problems.append("no feature module consumes orders.remote.ts")
    checks.append(_check("team-remote-module", remote_problems, "orders.remote.ts owns the feature's I/O"))

    problems = [
        directory for directory in _src_dirs(project)
        if directory in {"src/core/domain", "src/core/data"}
        or (directory.startswith("src/features/orders/") and LAYER_SEGMENTS & set(directory.split("/")))
    ]
    if not _under(sources, "src/features/orders/"):
        problems.append("no src/features/orders/ feature folder")
    checks.append(_check("stack-feature-ownership", problems, "feature owns its code without Clean layer copies"))
    return checks


NORMS: Dict[str, Callable[[Path], List[Dict[str, object]]]] = {
    "clean": _clean_norm,
    "local": _local_norm,
    "team": _team_norm,
}


def _run(command: List[str], project: Path) -> Dict[str, object]:
    # 새 세션으로 띄워 timeout이면 테스트가 띄운 자손까지 그룹째 끝낸다.
    try:
        process = subprocess.Popen(
            command, cwd=project, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
    except OSError as exc:
        return {"ok": False, "detail": f"{' '.join(command)}: {exc}"}
    try:
        stdout, stderr = process.communicate(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        return {"ok": False, "detail": f"{' '.join(command)}: {exc}"}
    output = (stdout + stderr).strip().splitlines()
    return {"ok": process.returncode == 0, "detail": f"exit {process.returncode}: " + " | ".join(output[-6:])}


def _hidden(project: Path, mode: str) -> Dict[str, object]:
    hidden_root = CASE_DIR / "author" / mode / "hidden"
    copied: List[Path] = []
    try:
        for source in sorted(path for path in hidden_root.rglob("*") if path.is_file()):
            target = project / source.relative_to(hidden_root)
            if target.exists():
                return {"ok": False, "detail": f"hidden file already exists in project: {target}"}
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            copied.append(target)
        tests = [path.relative_to(project).as_posix() for path in copied if path.name.endswith(".test.ts")]
        return _run(["node", "--test", *tests], project)
    finally:
        for path in copied:
            path.unlink(missing_ok=True)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("author", "norm"))
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=sorted(NORMS))
    args = parser.parse_args(argv)
    project = args.project.resolve()

    checks: List[Dict[str, object]] = []
    behavior = plan = None
    if args.command == "author":
        # 작성자가 고친 테스트로 채점하지 않도록 red phase의 visible 테스트를 원본으로 되돌린다.
        shutil.copytree(CASE_DIR / "author" / args.mode / "pre", project, dirs_exist_ok=True)
        visible = _run(list(CASE["test_command"]), project)
        behavior = bool(visible["ok"])
        checks.append({"id": "behavior-visible-tests", **visible})
        hidden = _hidden(project, args.mode)
        plan = bool(hidden["ok"])
        checks.append({"id": "plan-hidden-tests", **hidden})
    norm_checks = NORMS[args.mode](project)
    checks += norm_checks
    report = {
        "behavior": behavior,
        "plan": plan,
        "norm": all(check["ok"] for check in norm_checks),
        "checks": checks,
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
