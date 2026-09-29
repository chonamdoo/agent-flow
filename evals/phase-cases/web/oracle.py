"""Oracle for the web (nextjs) orders-list phase case.

    python oracle.py author --project <dir> --mode <clean|local|team>
    python oracle.py norm   --project <dir> --mode <clean|local|team>

Prints {"behavior", "plan", "norm", "checks"} as JSON. `norm` runs only the static
mode-rule checks (import graph, file layout, naming); it never reads agent output.
Python 3.9+, stdlib only.
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

HERE = os.path.dirname(os.path.abspath(__file__))
MODES = ("clean", "local", "team")
SOURCE_ROOTS = ("src", "app")
SOURCE_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs")
PLAN_COMMAND = ["node", "--test", "tests/hidden/*.test.ts"]
TEST_TIMEOUT = 300

IMPORT_RE = re.compile(
    r"""(?:\bimport\s+(?:type\s+)?(?:[^'";]*?\sfrom\s+)?|\bexport\s+(?:type\s+)?[^'";]*?\sfrom\s+|\bimport\s*\(\s*)['"]([^'"]+)['"]"""
)
BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
LINE_COMMENT_RE = re.compile(r"(^|[\s;{}()])//[^\n]*")


def load_case():
    with open(os.path.join(HERE, "case.json"), encoding="utf-8") as handle:
        return json.load(handle)


def check(check_id, ok, detail):
    return {"id": check_id, "ok": bool(ok), "detail": detail}


def run(command, cwd):
    # 새 세션으로 띄워 timeout이면 테스트가 띄운 자손까지 그룹째 끝낸다.
    try:
        process = subprocess.Popen(
            command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, start_new_session=True,
        )
    except OSError as error:
        return False, str(error)
    try:
        stdout, _ = process.communicate(timeout=TEST_TIMEOUT)
    except subprocess.TimeoutExpired as error:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        return False, str(error)
    tail = "\n".join(stdout.strip().splitlines()[-8:])
    return process.returncode == 0, "exit {}: {}".format(process.returncode, tail)


# ---------------------------------------------------------------- source model


class Source:
    def __init__(self, project, rel):
        self.rel = rel
        with open(os.path.join(project, rel), encoding="utf-8", errors="replace") as handle:
            raw = handle.read()
        self.code = LINE_COMMENT_RE.sub(r"\1", BLOCK_COMMENT_RE.sub("", raw))
        self.imports = [resolve(rel, spec) for spec in IMPORT_RE.findall(self.code)]


def resolve(importer, spec):
    """Project-relative path without extension, or 'pkg:<name>' for bare packages."""
    if spec.startswith("@/"):
        target = "src/" + spec[2:]
    elif spec.startswith("."):
        target = posixpath.normpath(posixpath.join(posixpath.dirname(importer), spec))
    elif spec.startswith("src/") or spec.startswith("app/"):
        target = spec
    else:
        return "pkg:" + spec
    return re.sub(r"\.(tsx?|jsx?|mjs)$", "", target)


def collect_sources(project):
    sources = []
    for root in SOURCE_ROOTS:
        base = os.path.join(project, root)
        for directory, dirnames, filenames in os.walk(base):
            dirnames[:] = [name for name in dirnames if name not in ("node_modules", ".next")]
            for name in sorted(filenames):
                if not name.endswith(SOURCE_EXTENSIONS) or re.search(r"\.(test|spec)\.", name):
                    continue
                rel = os.path.relpath(os.path.join(directory, name), project).replace(os.sep, "/")
                sources.append(Source(project, rel))
    return sources


def source_dirs(project):
    found = []
    base = os.path.join(project, "src")
    for directory, dirnames, _ in os.walk(base):
        dirnames[:] = [name for name in dirnames if name != "node_modules"]
        for name in dirnames:
            found.append(os.path.relpath(os.path.join(directory, name), project).replace(os.sep, "/"))
    return found


def under(path, prefix):
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def summarize(problems, ok_detail):
    return ok_detail if not problems else "; ".join(problems[:6]) + (" …" if len(problems) > 6 else "")


# ---------------------------------------------------------------- clean mode

DTO_RE = re.compile(r"\b\w*Dto\b")
FETCH_RE = re.compile(r"\bfetch\s*\(")
UI_PACKAGES = ("pkg:react", "pkg:react-dom", "pkg:next")


def clean_checks(project, sources):
    checks = []
    domain = [s for s in sources if under(s.rel, "src/core/domain")]
    data = [s for s in sources if under(s.rel, "src/core/data")]
    presentation = [s for s in sources if under(s.rel, "src/features")]

    layout = []
    if not [s for s in domain if under(s.rel, "src/core/domain/orders")]:
        layout.append("no module under src/core/domain/orders")
    if not [s for s in data if under(s.rel, "src/core/data/orders")]:
        layout.append("no module under src/core/data/orders")
    if not [s for s in presentation if under(s.rel, "src/features/orders/presentation")]:
        layout.append("no module under src/features/orders/presentation")
    checks.append(check("clean-layout", not layout, summarize(layout, "orders spans core-domain, core-data, feature-presentation")))

    problems = []
    for source in domain:
        for target in source.imports:
            if target.startswith("pkg:") or not under(target, "src/core/domain"):
                problems.append("{} imports {}".format(source.rel, target))
        if FETCH_RE.search(source.code):
            problems.append("{} calls fetch".format(source.rel))
        if DTO_RE.search(source.code):
            problems.append("{} references a DTO".format(source.rel))
    checks.append(check("clean-domain-isolated", not problems, summarize(problems, "domain imports only domain")))

    ports = set()
    for source in domain:
        if under(source.rel, "src/core/domain/orders"):
            ports.update(re.findall(r"\bexport\s+(?:interface|type)\s+(\w*Repository)\b", source.code))
    implemented = [
        s.rel for s in data
        if any(under(t, "src/core/domain") for t in s.imports)
        and any(re.search(r"\b{}\b".format(port), s.code) for port in ports)
    ]
    detail = "port {} implemented in {}".format(sorted(ports), implemented) if ports and implemented else (
        "no *Repository port exported from src/core/domain/orders" if not ports
        else "no src/core/data module imports and implements {}".format(sorted(ports)))
    checks.append(check("clean-repository-port-and-adapter", bool(ports and implemented), detail))

    problems = []
    for source in presentation:
        for target in source.imports:
            if under(target, "src/core/data") or under(target, "src/core/network"):
                problems.append("{} imports {}".format(source.rel, target))
        if FETCH_RE.search(source.code):
            problems.append("{} calls fetch".format(source.rel))
        if DTO_RE.search(source.code):
            problems.append("{} references a DTO".format(source.rel))
    checks.append(check("clean-presentation-uses-domain-only", not problems, summarize(problems, "presentation depends on domain, not data")))

    problems = []
    for source in data:
        for target in source.imports:
            if under(target, "src/features") or under(target, "app") or under(target, "src/app") or target in UI_PACKAGES:
                problems.append("{} imports {}".format(source.rel, target))
    checks.append(check("clean-data-no-ui", not problems, summarize(problems, "data does not import UI")))
    return checks


# ---------------------------------------------------------------- local mode

FORBIDDEN_DIRS = ("core", "domain", "data", "presentation", "features")
TYPE_DECL_RE = re.compile(r"(?:^|[\s;{}])(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*(?:<[^=]*>)?\s*=")
EXPORTED_TYPE_RE = re.compile(r"\bexport\s+(?:declare\s+)?(?:type|interface)\s+([A-Za-z_$][\w$]*)")


def local_checks(project, sources):
    checks = []
    problems = []
    for directory in source_dirs(project):
        parts = directory.split("/")
        if any(part in FORBIDDEN_DIRS for part in parts[1:]):
            problems.append("layer directory {}".format(directory))
        elif directory != "src/orders" and under(directory, "src/orders"):
            problems.append("nested folder {}".format(directory))
    orders = [s for s in sources if under(s.rel, "src/orders")]
    if not orders:
        problems.append("no module under src/orders")
    checks.append(check("local-flat-feature-folder", not problems, summarize(problems, "orders lives flat in src/orders")))

    problems = [
        "{} is not <role>.orders.ts".format(s.rel) for s in orders
        if not re.match(r"^src/orders/[a-z]+\.orders\.tsx?$", s.rel)
    ]
    checks.append(check("local-role-file-names", not problems and bool(orders), summarize(problems, "every file matches <role>.orders.ts")))

    problems = []
    for source in sources:
        if not under(source.rel, "src"):
            continue
        if re.search(r"\bclass\s+[A-Za-z_$]", source.code):
            problems.append("{} declares a class".format(source.rel))
        if re.search(r"\binterface\s+[A-Za-z_$]", source.code):
            problems.append("{} declares an interface".format(source.rel))
        if re.search(r"\w*Repository\w*|\w*Port\b|\w*UseCase\w*", source.code):
            problems.append("{} introduces a repository/port/use-case abstraction".format(source.rel))
    checks.append(check("local-no-classes-or-ports", not problems, summarize(problems, "plain functions only")))

    problems = []
    exported_order_type = False
    for source in sources:
        if not under(source.rel, "src"):
            continue
        names = set(TYPE_DECL_RE.findall(source.code)) | set(EXPORTED_TYPE_RE.findall(source.code))
        for name in sorted(names):
            if not re.match(r"^T[A-Z][A-Za-z0-9]*$", name):
                problems.append("{}: type {} lacks the T prefix".format(source.rel, name))
        if re.search(r"\bexport\s+type\s+TOrder\b", source.code):
            exported_order_type = True
    if not exported_order_type:
        problems.append("no exported TOrder type")
    checks.append(check("local-t-prefixed-types", not problems, summarize(problems, "types are T-prefixed; TOrder exported")))
    return checks


# ---------------------------------------------------------------- team mode (FSD stack + team skill)

FSD_RANK = {"app": 4, "pages": 3, "widgets": 2, "features": 1, "entities": 0, "shared": -1}
SLICED = ("pages", "widgets", "features", "entities")
ENDPOINTS = "src/shared/api/endpoints"
API_LITERAL_RE = re.compile(r"""['"`]/api/""")


def fsd_location(path):
    parts = path.split("/")
    if parts[0] == "app":
        return "app", None
    if parts[0] != "src" or len(parts) < 2 or parts[1] not in FSD_RANK:
        return None, None
    layer = parts[1]
    return layer, (parts[2] if layer in SLICED and len(parts) > 2 else None)


def team_checks(project, sources):
    checks = []
    problems = []
    for directory in source_dirs(project):
        parts = directory.split("/")
        if len(parts) > 1 and parts[1] == "core":
            problems.append("Clean core directory {}".format(directory))
        elif any(part in ("domain", "data", "presentation") for part in parts[1:]):
            problems.append("pass-through Clean tier {}".format(directory))
    if not [s for s in sources if under(s.rel, "src/features/order-list")]:
        problems.append("no module under src/features/order-list")
    checks.append(check("fsd-no-clean-tiers", not problems, summarize(problems, "FSD layers only, no Clean tiers")))

    problems = []
    for source in sources:
        layer, slice_name = fsd_location(source.rel)
        if layer is None:
            continue
        for target in source.imports:
            if target.startswith("pkg:"):
                continue
            target_layer, target_slice = fsd_location(target)
            if target_layer is None:
                continue
            if FSD_RANK[target_layer] > FSD_RANK[layer]:
                problems.append("{} imports higher layer {}".format(source.rel, target))
            elif target_layer == layer and layer in SLICED and target_slice != slice_name:
                problems.append("{} imports sibling slice {}".format(source.rel, target))
    checks.append(check("fsd-import-direction", not problems, summarize(problems, "imports go down or stay in slice")))

    problems = []
    registry = next((s for s in sources if re.sub(r"\.(tsx?|jsx?|mjs)$", "", s.rel) == ENDPOINTS), None)
    if registry is None:
        problems.append("missing src/shared/api/endpoints.ts")
    else:
        constants = re.findall(r"""\bexport\s+const\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*['"`](/api/[^'"`]*)['"`]""", registry.code)
        if not any(value == "/api/orders" for _, value in constants):
            problems.append("endpoints.ts does not export the /api/orders path")
        for name, value in constants:
            if not re.match(r"^EP_[A-Z0-9]+(?:_[A-Z0-9]+)*$", name):
                problems.append("endpoint constant {} for {} lacks the EP_ UPPER_SNAKE name".format(name, value))
    for source in sources:
        if registry is not None and source.rel == registry.rel:
            continue
        if API_LITERAL_RE.search(source.code):
            problems.append("{} contains an /api/ literal".format(source.rel))
    checks.append(check("team-endpoint-registry", not problems, summarize(problems, "endpoint paths only in EP_ constants")))
    return checks


NORM = {"clean": clean_checks, "local": local_checks, "team": team_checks}


def norm_checks(project, mode):
    return NORM[mode](project, collect_sources(project))


# ---------------------------------------------------------------- author


def copy_hidden(project, mode):
    source = os.path.join(HERE, "author", mode, "hidden")
    copied = []
    for directory, _, filenames in os.walk(source):
        for name in filenames:
            rel = os.path.relpath(os.path.join(directory, name), source)
            target = os.path.join(project, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(os.path.join(directory, name), target)
            copied.append(target)
    return copied


def author(project, mode, case):
    checks = []
    # 작성자가 고친 테스트로 채점하지 않도록 red phase의 visible 테스트를 원본으로 되돌린다.
    shutil.copytree(os.path.join(HERE, "author", mode, "pre"), project, dirs_exist_ok=True)
    behavior_ok, detail = run(case["test_command"], project)
    checks.append(check("visible-tests", behavior_ok, detail))
    hidden_dir = os.path.join(project, "tests", "hidden")
    preexisting = os.path.exists(hidden_dir)
    copied = copy_hidden(project, mode)
    try:
        plan_ok, detail = run(PLAN_COMMAND, project)
    finally:
        for path in copied:
            os.remove(path)
        if not preexisting and os.path.isdir(hidden_dir) and not os.listdir(hidden_dir):
            os.rmdir(hidden_dir)
    checks.append(check("hidden-plan-tests", plan_ok, detail))
    norm = norm_checks(project, mode)
    checks.extend(norm)
    return {"behavior": behavior_ok, "plan": plan_ok, "norm": all(c["ok"] for c in norm), "checks": checks}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("author", "norm"))
    parser.add_argument("--project", required=True)
    parser.add_argument("--mode", required=True, choices=MODES)
    args = parser.parse_args(argv)
    project = os.path.abspath(args.project)
    if args.command == "author":
        result = author(project, args.mode, load_case())
    else:
        norm = norm_checks(project, args.mode)
        result = {"behavior": None, "plan": None, "norm": all(c["ok"] for c in norm), "checks": norm}
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
