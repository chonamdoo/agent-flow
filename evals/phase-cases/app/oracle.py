#!/usr/bin/env python3
"""Oracle for the app (Flutter, pure Dart) phase case.

`author`: behavior (visible tests via `test_command`), plan (hidden tests), norm (static mode rules).
`norm`:   static mode-rule checks only (used on review variants).
Norm checks read the files on disk; they never consult agent self-reports.
"""
import argparse
import json
import os
import posixpath
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

CASE_DIR = Path(__file__).resolve().parent
CASE = json.loads((CASE_DIR / "case.json").read_text(encoding="utf-8"))
MODES = tuple(CASE["modes"])

DIRECTIVE_RE = re.compile(r"^\s*(import|export|part\s+of|part)\s+['\"]([^'\"]+)['\"]", re.M)
CLASS_RE = re.compile(r"^\s*((?:(?:abstract|interface|base|final|sealed|mixin)\s+)*)class\s+(\w+)", re.M)
HANDLER_RE = re.compile(r"\}\s*(on\s+[A-Za-z_][\w<>?,. ]*?\s*)?(catch\s*\([^)]*\))?\s*\{")
LOCAL_FORBIDDEN_DIRS = {"core", "domain", "data", "presentation", "features", "api"}


def check(checks, check_id, ok, detail):
    checks.append({"id": check_id, "ok": bool(ok), "detail": detail})


def strip_comments(text):
    """Drop `//` and `/* */` comments while keeping string literals intact."""
    out = []
    i, n = 0, len(text)
    quote = None
    while i < n:
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            out.append(ch)
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def baseline_files(mode):
    """Project-relative lib files that exist before the change (seed + mode overlay)."""
    files = set()
    for root in (CASE_DIR / "seed", CASE_DIR / "modes" / mode / "overlay"):
        if root.is_dir():
            files.update(p.relative_to(root).as_posix() for p in root.rglob("*.dart"))
    return files


def changed_lib_files(project, mode):
    base = baseline_files(mode)
    lib = project / "lib"
    if not lib.is_dir():
        return []
    return sorted(
        rel for rel in (p.relative_to(project).as_posix() for p in lib.rglob("*.dart"))
        if rel not in base
    )


def source(project, rel):
    return strip_comments((project / rel).read_text(encoding="utf-8"))


def directives(project, rel):
    """(kind, resolved target) for each import/export/part directive of a file."""
    result = []
    for kind, uri in DIRECTIVE_RE.findall(source(project, rel)):
        kind = " ".join(kind.split())
        if uri.startswith(("dart:", "package:")):
            result.append((kind, uri))
        else:
            result.append((kind, posixpath.normpath(posixpath.join(posixpath.dirname(rel), uri))))
    return result


def block_after(text, open_index):
    """Text of the brace block starting at `text[open_index] == '{'`."""
    depth = 0
    for i in range(open_index, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_index:i + 1]
    return text[open_index:]


def argument_after(text, start):
    """Expression from `start` up to the first depth-0 `,` or closing bracket."""
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                return text[start:i]
            depth -= 1
        elif ch == "," and depth == 0:
            return text[start:i]
    return text[start:]


# ---------------------------------------------------------------- clean

def norm_clean(project):
    checks = []
    files = changed_lib_files(project, "clean")
    domain = [f for f in files if f.startswith("lib/core/domain/")]
    data = [f for f in files if f.startswith("lib/core/data/")]
    features = [f for f in files if f.startswith("lib/features/")]

    allowed = re.compile(r"^lib/(core/domain/[^/]+/|core/data/[^/]+/|features/[^/]+/(presentation|api)/|app/)")
    stray = [f for f in files if not allowed.match(f)]
    check(checks, "clean-role-paths", not stray,
          "all new lib files sit in Clean role paths" if not stray else f"outside role paths: {stray}")

    offenders = []
    for f in domain:
        for _, target in directives(project, f):
            if not (target.startswith("dart:") or target.startswith("lib/core/domain/")):
                offenders.append(f"{f} -> {target}")
    check(checks, "domain-imports-only-domain", domain and not offenders,
          "no domain files under lib/core/domain" if not domain
          else (f"domain depends outward: {offenders}" if offenders else f"{len(domain)} domain files import only domain/dart:"))

    serialization = re.compile(r"\bfromJson\b|\btoJson\b|\w*Dto\b|\bjsonDecode\b|\bfromDto\b")
    leaking = [f for f in domain if serialization.search(source(project, f))]
    check(checks, "domain-no-serialization", not leaking,
          "domain has no DTO/JSON code" if not leaking else f"DTO/JSON in domain: {leaking}")

    port = re.compile(r"(abstract\s+(interface\s+|base\s+)?|interface\s+)class\s+OrderRepository\b")
    ports = [f for f in domain if port.search(source(project, f))]
    check(checks, "repository-port-in-domain", ports,
          f"abstract OrderRepository in {ports}" if ports else "no abstract/interface OrderRepository under lib/core/domain")

    impl = re.compile(r"class\s+\w+[^{;]*\b(implements|extends)\b[^{;]*\bOrderRepository\b")
    impls = [f for f in files if impl.search(source(project, f))]
    misplaced = [f for f in impls if not f.startswith("lib/core/data/")]
    check(checks, "repository-impl-in-data", impls and not misplaced,
          "no OrderRepository implementation" if not impls
          else (f"implementation outside lib/core/data: {misplaced}" if misplaced else f"implemented in {impls}"))

    reaching = []
    for f in features:
        for _, target in directives(project, f):
            if target.startswith("lib/core/data/"):
                reaching.append(f"{f} -> {target}")
    check(checks, "presentation-no-data-import", not reaching,
          "presentation does not import data" if not reaching else f"presentation imports data: {reaching}")
    return checks


# ---------------------------------------------------------------- local

def norm_local(project):
    checks = []
    files = changed_lib_files(project, "local")

    bad_layout = [f for f in files
                  if len(f.split("/")) != 3 or LOCAL_FORBIDDEN_DIRS.intersection(f.split("/")[1:-1])]
    check(checks, "flat-feature-folder", files and not bad_layout,
          "no new lib files" if not files
          else (f"not lib/<feature>/<file>.dart: {bad_layout}" if bad_layout else "all new files are flat lib/<feature>/*.dart"))

    abstract = []
    for f in files:
        for modifiers, name in CLASS_RE.findall(source(project, f)):
            if {"abstract", "interface"}.intersection(modifiers.split()):
                abstract.append(f"{f}: {' '.join(modifiers.split())} class {name}")
    check(checks, "no-abstract-or-interface-classes", not abstract,
          "no abstract/interface classes" if not abstract else f"forbidden declarations: {abstract}")

    problems = []
    folders = sorted({posixpath.dirname(f) for f in files if len(f.split("/")) == 3})
    for folder in folders:
        feature = posixpath.basename(folder)
        entry = f"{folder}/{feature}.dart"
        members = sorted(p.relative_to(project).as_posix() for p in (project / folder).glob("*.dart"))
        if entry not in members:
            problems.append(f"missing library file {entry}")
            continue
        entry_directives = directives(project, entry)
        parts = {target for kind, target in entry_directives if kind == "part"}
        exported = [target for kind, target in entry_directives if kind == "export" and target.startswith(folder + "/")]
        if exported:
            problems.append(f"{entry} exports siblings {exported}")
        for member in members:
            if member == entry:
                continue
            member_directives = directives(project, member)
            if not member_directives or member_directives[0] != ("part of", entry):
                problems.append(f"{member} does not start with part of '{feature}.dart'")
            if any(kind in ("import", "export") for kind, _ in member_directives):
                problems.append(f"{member} has import/export directives")
            if member not in parts:
                problems.append(f"{entry} lacks part '{posixpath.basename(member)}'")
    check(checks, "one-library-per-feature", folders and not problems,
          "no feature folder" if not folders
          else (f"library rule broken: {problems}" if problems else f"single part-based library in {folders}"))
    return checks


# ---------------------------------------------------------------- team

def handler_bodies(text):
    """Bodies of try/catch handlers, `.catchError(...)` arguments and `onError:` arguments."""
    bodies = []
    for match in HANDLER_RE.finditer(text):
        if match.group(1) or match.group(2):
            bodies.append(("catch", block_after(text, match.end() - 1)))
    for match in re.finditer(r"\.catchError\s*\(", text):
        bodies.append(("catchError", argument_after(text, match.end())))
    for match in re.finditer(r"\bonError\s*:", text):
        bodies.append(("onError", argument_after(text, match.end())))
    return bodies


def norm_team(project):
    checks = []
    feature_files = [
        p.relative_to(project).as_posix() for p in sorted((project / "lib" / "features").rglob("*.dart"))
    ] if (project / "lib" / "features").is_dir() else []

    total, silent = 0, []
    for f in feature_files:
        feature = f.split("/")[2] if len(f.split("/")) > 3 else ""
        report = re.compile(r"\breportTeamError\s*\(\s*['\"]" + re.escape(feature) + r"['\"]")
        for kind, body in handler_bodies(source(project, f)):
            total += 1
            if not report.search(body):
                silent.append(f"{f}: {kind} without reportTeamError('{feature}', …)")
    check(checks, "handlers-report-team-error", total and not silent,
          "no error handler in lib/features" if not total
          else (f"silent handlers: {silent}" if silent else f"{total} handlers call reportTeamError"))

    overlay = CASE_DIR / "modes" / "team" / "overlay"
    touched = []
    for original in sorted((overlay / "lib" / "team").rglob("*.dart")):
        rel = original.relative_to(overlay).as_posix()
        current = project / rel
        if not current.is_file() or current.read_bytes() != original.read_bytes():
            touched.append(rel)
    check(checks, "team-infra-untouched", not touched,
          "lib/team unchanged" if not touched else f"lib/team modified: {touched}")
    return checks


NORMS = {"clean": norm_clean, "local": norm_local, "team": norm_team}


# ---------------------------------------------------------------- behavior / plan

def run(command, project):
    # 새 세션으로 띄워 timeout이면 테스트가 띄운 자손까지 그룹째 끝낸다.
    try:
        process = subprocess.Popen(command, cwd=str(project), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   universal_newlines=True, start_new_session=True)
    except OSError as exc:
        return False, str(exc)
    try:
        stdout, _ = process.communicate(timeout=300)
    except subprocess.TimeoutExpired as exc:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        return False, str(exc)
    return process.returncode == 0, f"exit {process.returncode}: {stdout[-600:].strip()}"


def behavior(project, mode, checks):
    # 작성자가 고친 테스트로 채점하지 않도록 red phase의 visible 테스트를 원본으로 되돌린다.
    shutil.copytree(str(CASE_DIR / "author" / mode / "pre"), str(project), dirs_exist_ok=True)
    ok, detail = run(CASE["test_command"], project)
    check(checks, "visible-tests", ok, detail)
    return ok


def plan(project, mode, checks):
    hidden = CASE_DIR / "author" / mode / "hidden"
    tests = sorted(p.relative_to(hidden).as_posix() for p in hidden.rglob("*.dart"))
    copied = []
    ok_all = bool(tests)
    try:
        for rel in tests:
            target = project / rel
            if not target.exists():
                copied.append(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(hidden / rel), str(target))
        for rel in tests:
            ok, detail = run(["dart", rel], project)
            check(checks, f"hidden:{rel}", ok, detail)
            ok_all = ok_all and ok
    finally:
        for target in copied:
            target.unlink()
    return ok_all


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("author", "norm"))
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=MODES)
    args = parser.parse_args(argv)
    project = args.project.resolve()

    checks = []
    result = {"behavior": None, "plan": None, "norm": None, "checks": checks}
    if args.command == "author":
        result["behavior"] = behavior(project, args.mode, checks)
        result["plan"] = plan(project, args.mode, checks)
    norm_checks = NORMS[args.mode](project)
    checks.extend(norm_checks)
    result["norm"] = all(item["ok"] for item in norm_checks)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
