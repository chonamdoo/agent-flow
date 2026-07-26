"""`layer_boundary` behavior and architecture oracle.

아키텍처 판정은 두 규칙만 본다.
- R1: target use case에 sibling use case가 참조/주입되는가 (`usecase-reference:`, `usecase-injection:`).
- R2: domain 모듈이 data/HTTP/framework를 직접 또는 프로젝트 로컬 모듈을 거쳐 import 하는가 (`domain-import:`).

의도적으로 탐지하지 않는 우회 경로:
- `functools.partial`로 target 생성자를 감싸 인자를 나눠 넘기는 주입.
- target을 대신 만들어 주는 팩토리 함수(중첩 팩토리, classmethod 팩토리 포함)를 거친 주입.
- 주입 지점에서 헬퍼 함수나 호출 가능 객체가 sibling use case를 만들어 반환하는 간접 생성.
- 컨테이너(튜플/리스트/딕셔너리)에서 원소를 골라 꺼내 넘기는 주입.
- target 인스턴스를 만든 뒤 속성 대입이나 setter 메서드로 sibling을 심는 주입.
- MRO 순서를 풀어야만 드러나는 구체 use case 판별. base 상속으로 끌어온 concrete
  sibling은 잡지만, 믹스인 순서에 따라 구현이 갈리는 경우는 보지 않는다.
- `importlib.import_module` / `__import__` 같은 동적 import로 부르는 금지 모듈.
- 분기 도달 가능성 구분: 죽은 분기의 대입도 그대로 값으로 본다.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import signal
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

CASE_ROOT = Path(__file__).resolve().parent
CANONICAL_BEHAVIOR_TEST = CASE_ROOT / "seed" / "checkout_behavior.py"
DOMAIN_PACKAGE = "shop.core.domain"
TARGET_MODULE = "shop.core.domain.get_checkout_total"
TARGET_USECASE = "GetCheckoutTotalUseCase"
TARGET_QUALIFIED = f"{TARGET_MODULE}.{TARGET_USECASE}"
FORBIDDEN_PROJECT_IMPORT_PREFIXES = (
    "shop.adapters",
    "shop.app",
    "shop.core.data",
    "shop.di",
    "shop.framework",
    "shop.core.network",
    "shop.infra",
    "shop.infrastructure",
    "shop.presentation",
    "shop.ui",
    "shop.web",
)
FORBIDDEN_DOMAIN_IMPORT_ROOTS = {
    "aiohttp",
    "boto3",
    "dependency_injector",
    "django",
    "fastapi",
    "flask",
    "http",
    "ftplib",
    "imaplib",
    "poplib",
    "smtplib",
    "socket",
    "ssl",
    "telnetlib",
    "wsgiref",
    "xmlrpc",
    "httpx",
    "injector",
    "lagom",
    "marshmallow",
    "pydantic",
    "punq",
    "requests",
    "sqlalchemy",
    "sqlite3",
    "socketserver",
    "starlette",
    "tkinter",
    "urllib",
    "urllib3",
}
EXPECTED_BEHAVIOR_CASES = 5
PYTEST_RUNNER = f"""import sys
import pytest


class Results:
    collected = 0
    passed = 0

    def pytest_collection_finish(self, session):
        self.collected = len(session.items)

    def pytest_runtest_logreport(self, report):
        if report.when == "call" and report.passed:
            self.passed += 1


sys.path.insert(0, sys.argv[1])
results = Results()
exit_code = pytest.main([
    sys.argv[2],
    "-c", sys.argv[3],
    "-q",
    "-p", "no:cacheprovider",
    "-o", "addopts=",
], plugins=[results])
complete = (
    exit_code == 0
    and results.collected == {EXPECTED_BEHAVIOR_CASES}
    and results.passed == {EXPECTED_BEHAVIOR_CASES}
)
raise SystemExit(0 if complete else 1)
"""


class _ParsedModule:
    __slots__ = ("name", "package", "tree")

    def __init__(self, name: str, package: str, tree: ast.Module) -> None:
        self.name = name
        self.package = package
        self.tree = tree


class _FunctionEntry:
    __slots__ = ("local_name", "node", "owner", "parent", "qualified", "receiver")

    def __init__(
        self,
        qualified: str,
        node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
        owner: str | None,
        parent: ast.AST,
        local_name: str | None,
    ) -> None:
        positional = [
            argument.arg
            for argument in (*node.args.posonlyargs, *node.args.args)
        ]
        decorators = {
            _expression_key(decorator)
            for decorator in getattr(node, "decorator_list", ())
        }
        is_static = any(name and name.endswith("staticmethod") for name in decorators)
        self.receiver = positional[0] if owner and positional and not is_static else None
        self.qualified = qualified
        self.node = node
        self.owner = owner
        self.parent = parent
        self.local_name = local_name


def score(project: Path) -> dict[str, object]:
    violations = _architecture_violations(project)
    return {
        "behavior": _tests_pass(project),
        "norm": _architecture_ok(violations),
        "norm_reasons": violations,
    }


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        if process.poll() is None:
            process.kill()


def _tests_pass(project: Path) -> bool:
    with tempfile.TemporaryDirectory(dir=project.parent) as raw:
        harness = Path(raw)
        test_path = harness / CANONICAL_BEHAVIOR_TEST.name
        config_path = harness / "pytest.ini"
        runner_path = harness / "run_pytest.py"
        test_path.write_text(CANONICAL_BEHAVIOR_TEST.read_text(encoding="utf-8"), encoding="utf-8")
        config_path.write_text("[pytest]\n", encoding="utf-8")
        runner_path.write_text(PYTEST_RUNNER, encoding="utf-8")
        env = os.environ.copy()
        for name in ("PYTHONPATH", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
            env.pop(name, None)
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            process = subprocess.Popen(
                (
                    sys.executable,
                    str(runner_path),
                    str((project / "src").resolve()),
                    str(test_path),
                    str(config_path),
                ),
                cwd=harness,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        timed_out = False
        try:
            return_code = process.wait(timeout=120)
        except subprocess.TimeoutExpired:
            timed_out = True
            return_code = None
        finally:
            _kill_process_group(process)
            if process.poll() is None:
                process.wait()
    return not timed_out and return_code == 0


def _architecture_ok(violations: list[str]) -> bool:
    return not violations


def _architecture_violations(project: Path) -> list[str]:
    modules = _parse_modules(project)
    violations = _domain_import_violations(modules)
    classes = _classes(modules)
    target_qualified = _canonical_symbol(TARGET_QUALIFIED, modules)
    if target_qualified not in classes:
        return sorted(violations)

    target_classes = _target_class_family(target_qualified, classes, modules)
    target_owners = _class_ancestors(target_classes, classes, modules)
    usecases = _usecase_classes(classes, modules)
    # 상속으로 끌어온 concrete sibling use case는 target 쪽 클래스가 아니라 위반이다.
    # 추상 base나 Protocol 조상은 concrete가 아니므로 여기 걸리지 않는다.
    inherited_siblings = {
        owner
        for owner in (target_owners - target_classes) & usecases
        if _is_concrete_usecase(owner, classes, modules)
    }
    target_scope_owners = target_owners - inherited_siblings
    sibling_usecases = usecases - target_scope_owners
    for sibling in inherited_siblings:
        violations.add(f"usecase-reference:{sibling}")
    for module_name in {
        classes[target_owner][0]
        for target_owner in target_scope_owners
    }:
        for sibling in _sibling_references(
            modules[module_name],
            modules,
            sibling_usecases,
            target_scope_owners,
        ):
            violations.add(f"usecase-reference:{sibling}")
    for sibling in _transitive_target_references(
        modules,
        sibling_usecases,
        target_scope_owners,
    ):
        violations.add(f"usecase-reference:{sibling}")
    for sibling in _target_injections(
        modules,
        sibling_usecases,
        target_classes,
    ):
        violations.add(f"usecase-injection:{sibling}")
    return sorted(violations)


def _parse_modules(project: Path) -> dict[str, _ParsedModule]:
    source_root = project / "src"
    if not source_root.is_dir():
        return {}

    modules: dict[str, _ParsedModule] = {}
    try:
        paths = sorted(source_root.rglob("*.py"))
    except OSError:
        return modules
    for path in paths:
        try:
            relative = path.relative_to(source_root)
            parts = list(relative.with_suffix("").parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
                package = ".".join(parts)
                name = package
            else:
                name = ".".join(parts)
                package = ".".join(parts[:-1])
            if not name:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError):
            continue
        modules[name] = _ParsedModule(name=name, package=package, tree=tree)
    return modules


def _domain_import_violations(modules: dict[str, _ParsedModule]) -> set[str]:
    violations: set[str] = set()
    domain_modules = (
        module
        for module in modules.values()
        if module.name == DOMAIN_PACKAGE or module.name.startswith(f"{DOMAIN_PACKAGE}.")
    )
    for origin in domain_modules:
        pending = [origin.name]
        visited: set[str] = set()
        while pending:
            module_name = pending.pop()
            if module_name in visited:
                continue
            visited.add(module_name)
            for imported in _imported_module_names(modules[module_name]):
                if any(
                    imported == prefix or imported.startswith(f"{prefix}.")
                    for prefix in FORBIDDEN_PROJECT_IMPORT_PREFIXES
                ):
                    violations.add(f"domain-import:{origin.name}->{imported}")
                    continue
                root = imported.partition(".")[0]
                if root in FORBIDDEN_DOMAIN_IMPORT_ROOTS:
                    violations.add(f"domain-import:{origin.name}->{imported}")
                    continue
                local_targets = _project_import_targets(imported, modules)
                if local_targets:
                    pending.extend(local_targets)
                    continue
                if (
                    imported != DOMAIN_PACKAGE
                    and not imported.startswith(f"{DOMAIN_PACKAGE}.")
                    and not _is_stdlib_import(root)
                ):
                    violations.add(f"domain-import:{origin.name}->{imported}")
    return violations


def _imported_module_names(module: _ParsedModule) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from(module.package, node.level, node.module)
            if base:
                imported.add(base)
                imported.update(
                    f"{base}.{alias.name}"
                    for alias in node.names
                    if alias.name != "*"
                )
    return imported


def _project_import_targets(
    imported: str,
    modules: dict[str, _ParsedModule],
) -> set[str]:
    if imported in modules:
        targets = {imported}
    else:
        parents = [
            name
            for name in modules
            if imported.startswith(f"{name}.")
        ]
        if parents:
            targets = {max(parents, key=len)}
        else:
            targets = {
                name
                for name in modules
                if name.startswith(f"{imported}.")
            }

    expanded = set(targets)
    for target in targets:
        parts = target.split(".")
        expanded.update(
            parent
            for index in range(1, len(parts))
            if (parent := ".".join(parts[:index])) in modules
        )
    return expanded


def _is_stdlib_import(root: str) -> bool:
    if root == "__future__" or root in sys.builtin_module_names:
        return True
    try:
        spec = importlib.util.find_spec(root)
    except (AttributeError, ImportError, ModuleNotFoundError, ValueError):
        return False
    if spec is None or spec.origin is None:
        return False
    if spec.origin in {"built-in", "frozen"}:
        return True

    origin = Path(spec.origin).resolve()
    stdlib = Path(sysconfig.get_path("stdlib")).resolve()
    try:
        origin.relative_to(stdlib)
    except ValueError:
        return False
    for key in ("purelib", "platlib"):
        installed = sysconfig.get_path(key)
        if not installed:
            continue
        try:
            origin.relative_to(Path(installed).resolve())
        except ValueError:
            continue
        return False
    return True


def _class_entries(
    module: _ParsedModule,
) -> list[tuple[str, ast.ClassDef, ast.AST]]:
    entries: list[tuple[str, ast.ClassDef, ast.AST]] = []
    functions = {
        entry.node: entry.qualified
        for entry in _function_entries(module)
    }

    def visit(scope: ast.AST, prefix: str) -> None:
        for node, _ in _direct_child_scopes(scope):
            if isinstance(node, ast.ClassDef):
                qualified = f"{prefix}.{node.name}"
                entries.append((qualified, node, scope))
                visit(node, qualified)
            elif isinstance(
                node,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda),
            ):
                qualified = functions.get(node)
                if qualified:
                    visit(node, qualified)

    visit(module.tree, module.name)
    return entries


def _classes(
    modules: dict[str, _ParsedModule],
) -> dict[str, tuple[str, ast.ClassDef]]:
    return {
        qualified: (module.name, node)
        for module in modules.values()
        for qualified, node, _ in _class_entries(module)
    }


def _subclass_closure(
    family: set[str],
    classes: dict[str, tuple[str, ast.ClassDef]],
    modules: dict[str, _ParsedModule],
) -> set[str]:
    closure = set(family)
    changed = True
    while changed:
        changed = False
        for qualified, (module_name, node) in classes.items():
            if qualified in closure:
                continue
            aliases = _scope_aliases(modules[module_name], node, modules, qualified)
            if any(
                _resolve_expression(base, aliases, modules) in closure
                for base in node.bases
            ):
                closure.add(qualified)
                changed = True
    return closure


def _target_class_family(
    target_qualified: str,
    classes: dict[str, tuple[str, ast.ClassDef]],
    modules: dict[str, _ParsedModule],
) -> set[str]:
    return _subclass_closure({target_qualified}, classes, modules)


def _usecase_classes(
    classes: dict[str, tuple[str, ast.ClassDef]],
    modules: dict[str, _ParsedModule],
) -> set[str]:
    named = {
        qualified
        for qualified, (_, node) in classes.items()
        if node.name.endswith("UseCase")
    }
    return _subclass_closure(named, classes, modules)


def _is_concrete_usecase(
    qualified: str,
    classes: dict[str, tuple[str, ast.ClassDef]],
    modules: dict[str, _ParsedModule],
) -> bool:
    # 조상까지 보는 이유는 base가 구현을 주고 자식이 이름만 바꾸는 경우가 있어서다.
    return any(
        _defines_concrete_call(classes[owner][1])
        for owner in _class_ancestors({qualified}, classes, modules)
    )


def _defines_concrete_call(node: ast.ClassDef) -> bool:
    for member in node.body:
        if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if member.name != "__call__":
            continue
        if any(
            _expression_key(decorator) in {"abstractmethod", "abc.abstractmethod"}
            for decorator in member.decorator_list
        ):
            continue
        if all(_is_placeholder_statement(statement) for statement in member.body):
            continue
        return True
    return False


def _is_placeholder_statement(node: ast.stmt) -> bool:
    if isinstance(node, ast.Pass):
        return True
    if isinstance(node, ast.Expr):
        value = node.value
        if isinstance(value, ast.Constant) and (value.value is Ellipsis or isinstance(value.value, str)):
            return True
    if isinstance(node, ast.Raise):
        return True
    return False


def _class_ancestors(
    descendants: set[str],
    classes: dict[str, tuple[str, ast.ClassDef]],
    modules: dict[str, _ParsedModule],
) -> set[str]:
    ancestors = set(descendants)
    pending = list(descendants)
    while pending:
        qualified = pending.pop()
        module_name, node = classes[qualified]
        aliases = _scope_aliases(
            modules[module_name],
            node,
            modules,
            qualified,
        )
        for base in node.bases:
            resolved = _resolve_expression(base, aliases, modules)
            if resolved in classes and resolved not in ancestors:
                ancestors.add(resolved)
                pending.append(resolved)
    return ancestors


def _resolve_from(package: str, level: int, imported: str | None) -> str:
    if level == 0:
        return imported or ""
    parts = package.split(".") if package else []
    parents = level - 1
    if parents > len(parts):
        return ""
    base = parts[: len(parts) - parents]
    if imported:
        base.extend(imported.split("."))
    return ".".join(base)


def _static_expression(
    node: ast.AST,
    aliases: dict[str, str],
) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        owner = _static_expression(node.value, aliases)
        return f"{owner}.{node.attr}" if owner else None
    if isinstance(node, ast.Subscript):
        return _static_expression(node.value, aliases)
    if isinstance(node, ast.Call):
        called = _static_expression(node.func, aliases)
        return f"{called}.__call__" if called else None
    return None


def _module_binds_name(module: _ParsedModule, symbol: str) -> bool:
    for node in module.tree.body:
        if (
            isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == symbol
        ):
            return True
        if isinstance(node, ast.ImportFrom):
            if any(
                imported.name != "*"
                and (imported.asname or imported.name) == symbol
                for imported in node.names
            ):
                return True
        elif isinstance(node, ast.Import):
            if any(
                (imported.asname or imported.name.partition(".")[0]) == symbol
                for imported in node.names
            ):
                return True
        if any(name == symbol for name, _ in _name_bindings(node)):
            return True
    return False


def _canonical_symbol(
    qualified: str,
    modules: dict[str, _ParsedModule],
    seen: frozenset[str] = frozenset(),
) -> str:
    if qualified in seen:
        return qualified
    candidates = [name for name in modules if qualified.startswith(f"{name}.")]
    if not candidates:
        return qualified
    module_name = max(candidates, key=len)
    symbol = qualified[len(module_name) + 1 :]
    if "." in symbol:
        return qualified

    module = modules[module_name]
    aliases: dict[str, str] = {}
    binding: str | None = None
    for node in module.tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            resolved = f"{module.name}.{node.name}"
            aliases[node.name] = resolved
            if node.name == symbol:
                binding = resolved
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from(module.package, node.level, node.module)
            if not base:
                continue
            for imported in node.names:
                if imported.name == "*":
                    candidate = f"{base}.{symbol}"
                    resolved = _canonical_symbol(
                        candidate,
                        modules,
                        seen | {qualified},
                    )
                    target_module = modules.get(base)
                    if (
                        resolved != candidate
                        or target_module is not None
                        and _module_binds_name(target_module, symbol)
                    ):
                        aliases[symbol] = resolved
                        binding = resolved
                    continue
                local = imported.asname or imported.name
                imported_qualified = f"{base}.{imported.name}"
                aliases[local] = imported_qualified
                if local == symbol:
                    binding = imported_qualified
        elif isinstance(node, ast.Import):
            for imported in node.names:
                local = imported.asname or imported.name.partition(".")[0]
                resolved = imported.name if imported.asname else local
                aliases[local] = resolved
                if local == symbol:
                    binding = resolved
        for name, value in _name_bindings(node):
            resolved = _static_expression(value, aliases)
            if resolved:
                aliases[name] = resolved
            else:
                aliases.pop(name, None)
            if name == symbol:
                binding = resolved

    if binding is None or binding == qualified:
        return qualified
    return _canonical_symbol(binding, modules, seen | {qualified})


def _resolve_expression(
    node: ast.AST,
    aliases: dict[str, str],
    modules: dict[str, _ParsedModule],
) -> str | None:
    if isinstance(node, ast.Name):
        qualified = aliases.get(node.id, node.id)
    elif isinstance(node, ast.Attribute):
        key = _expression_key(node)
        if key and key in aliases:
            qualified = aliases[key]
        else:
            owner = _resolve_expression(node.value, aliases, modules)
            if not owner:
                return None
            qualified = f"{owner}.{node.attr}"
    elif isinstance(node, ast.Subscript):
        return _resolve_expression(node.value, aliases, modules)
    else:
        return None
    return _canonical_symbol(qualified, modules)


def _module_aliases(
    module: _ParsedModule,
    modules: dict[str, _ParsedModule],
) -> dict[str, str]:
    aliases = {
        node.name: f"{module.name}.{node.name}"
        for node in module.tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    nodes = _nodes_in_scope(module.tree)
    _add_import_aliases(aliases, nodes, module, modules)
    _add_callable_aliases(aliases, module.tree, _function_entries(module))
    _add_class_aliases(aliases, module.tree, _class_entries(module))
    _propagate_aliases(aliases, nodes, modules)
    return aliases


def _scope_aliases(
    module: _ParsedModule,
    scope: ast.AST,
    modules: dict[str, _ParsedModule],
    owner: str | None = None,
    receiver: str | None = None,
) -> dict[str, str]:
    aliases = _module_aliases(module, modules)
    entries = _function_entries(module)
    entries_by_node = {entry.node: entry for entry in entries}
    for lexical_scope in _scope_chain(module.tree, scope):
        if isinstance(lexical_scope, ast.Module):
            continue
        nodes = _nodes_in_scope(lexical_scope)
        _add_import_aliases(aliases, nodes, module, modules)
        _add_callable_aliases(aliases, lexical_scope, entries)
        _add_class_aliases(aliases, lexical_scope, _class_entries(module))
        entry = entries_by_node.get(lexical_scope)
        if entry and entry.owner and entry.receiver:
            aliases[entry.receiver] = entry.owner
        _propagate_aliases(aliases, nodes, modules)
    if owner and receiver:
        aliases[receiver] = owner
    return aliases


def _public_module_aliases(
    module_name: str,
    modules: dict[str, _ParsedModule],
    seen: frozenset[str] = frozenset(),
) -> dict[str, str]:
    if module_name in seen:
        return {}
    module = modules.get(module_name)
    if module is None:
        return {}
    public: dict[str, str] = {}
    for node in module.tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                public[node.name] = f"{module.name}.{node.name}"
        elif isinstance(node, ast.Import):
            for imported in node.names:
                local = imported.asname or imported.name.partition(".")[0]
                if not local.startswith("_"):
                    public[local] = imported.name if imported.asname else local
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from(module.package, node.level, node.module)
            if not base:
                continue
            for imported in node.names:
                if imported.name == "*":
                    public.update(
                        _public_module_aliases(
                            base,
                            modules,
                            seen | {module_name},
                        )
                    )
                    continue
                local = imported.asname or imported.name
                if not local.startswith("_"):
                    public[local] = f"{base}.{imported.name}"
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for name, _ in _name_bindings(node):
                if not name.startswith("_"):
                    public[name] = f"{module.name}.{name}"
    return {
        name: _canonical_symbol(qualified, modules)
        for name, qualified in public.items()
    }


def _add_import_aliases(
    aliases: dict[str, str],
    nodes: list[ast.AST],
    module: _ParsedModule,
    modules: dict[str, _ParsedModule],
) -> None:
    for node in nodes:
        if isinstance(node, ast.Import):
            for imported in node.names:
                local = imported.asname or imported.name.partition(".")[0]
                aliases[local] = imported.name if imported.asname else local
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from(module.package, node.level, node.module)
            if not base:
                continue
            for imported in node.names:
                if imported.name == "*":
                    aliases.update(_public_module_aliases(base, modules))
                    continue
                local = imported.asname or imported.name
                aliases[local] = f"{base}.{imported.name}"


def _propagate_aliases(
    aliases: dict[str, str],
    nodes: list[ast.AST],
    modules: dict[str, _ParsedModule],
) -> None:
    class_symbols = set(_classes(modules))
    callable_classes = {
        entry.owner
        for module in modules.values()
        for entry in _function_entries(module)
        if entry.owner and entry.local_name == "__call__"
    }
    ordered = sorted(nodes, key=_source_order)
    for _ in range(len(ordered) + 1):
        before = aliases.copy()
        for node in ordered:
            for name, value in _name_bindings(node):
                if isinstance(value, (ast.Name, ast.Attribute, ast.Subscript)):
                    resolved = _resolve_expression(value, aliases, modules)
                elif isinstance(value, ast.Call):
                    candidate = _resolve_expression(value.func, aliases, modules)
                    if candidate in callable_classes:
                        resolved = f"{candidate}.__call__"
                    else:
                        resolved = candidate if candidate in class_symbols else None
                else:
                    resolved = None
                if resolved:
                    aliases[name] = resolved
        if aliases == before:
            return


_SCOPE_NODES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_CALLABLE_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _source_order(node: ast.AST) -> tuple[int, int]:
    return getattr(node, "lineno", -1), getattr(node, "col_offset", -1)


def _nodes_in_scope(scope: ast.AST) -> list[ast.AST]:
    nodes: list[ast.AST] = []
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        if isinstance(node, _SCOPE_NODES):
            continue
        nodes.append(node)
        pending.extend(ast.iter_child_nodes(node))
    return nodes


def _direct_child_scopes(scope: ast.AST) -> list[tuple[ast.AST, ast.AST]]:
    children: list[tuple[ast.AST, ast.AST]] = []
    pending = [(child, scope) for child in ast.iter_child_nodes(scope)]
    while pending:
        node, parent = pending.pop()
        if isinstance(node, _SCOPE_NODES):
            children.append((node, parent))
            continue
        pending.extend((child, node) for child in ast.iter_child_nodes(node))
    return sorted(children, key=lambda item: _source_order(item[0]))


def _lambda_local_name(node: ast.Lambda, parent: ast.AST) -> str | None:
    targets: list[ast.AST] = []
    if isinstance(parent, ast.Assign) and parent.value is node:
        targets = parent.targets
    elif isinstance(parent, ast.AnnAssign) and parent.value is node:
        targets = [parent.target]
    elif isinstance(parent, ast.NamedExpr) and parent.value is node:
        targets = [parent.target]
    for target in targets:
        if isinstance(target, ast.Name):
            return target.id
    return None


def _function_entries(module: _ParsedModule) -> list[_FunctionEntry]:
    entries: list[_FunctionEntry] = []

    def visit(scope: ast.AST, prefix: str) -> None:
        for node, parent in _direct_child_scopes(scope):
            if isinstance(node, ast.ClassDef):
                visit(node, f"{prefix}.{node.name}")
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                local_name = node.name
                owner = prefix if isinstance(scope, ast.ClassDef) else None
                if isinstance(scope, (ast.Module, ast.ClassDef)):
                    qualified = f"{prefix}.{local_name}"
                else:
                    qualified = f"{prefix}.<locals>.{local_name}@{node.lineno}"
            elif isinstance(node, ast.Lambda):
                local_name = _lambda_local_name(node, parent)
                label = local_name or "lambda"
                qualified = f"{prefix}.<locals>.{label}@{node.lineno}:{node.col_offset}"
                owner = None
            else:
                continue
            entries.append(
                _FunctionEntry(
                    qualified,
                    node,
                    owner,
                    scope,
                    local_name,
                )
            )
            visit(node, qualified)

    visit(module.tree, module.name)
    return entries


def _add_callable_aliases(
    aliases: dict[str, str],
    scope: ast.AST,
    entries: list[_FunctionEntry],
) -> None:
    for entry in entries:
        if entry.parent is scope and entry.local_name:
            aliases[entry.local_name] = entry.qualified


def _add_class_aliases(
    aliases: dict[str, str],
    scope: ast.AST,
    entries: list[tuple[str, ast.ClassDef, ast.AST]],
) -> None:
    for qualified, node, parent in entries:
        if parent is scope:
            aliases[node.name] = qualified


def _scope_chain(root: ast.Module, scope: ast.AST) -> list[ast.AST]:
    parents = {
        child: parent
        for parent in ast.walk(root)
        for child in ast.iter_child_nodes(parent)
    }
    chain: list[ast.AST] = []
    current: ast.AST | None = scope
    while current is not None:
        if isinstance(current, _SCOPE_NODES):
            chain.append(current)
        if current is root:
            return list(reversed(chain))
        current = parents.get(current)
    return [root, scope] if scope is not root else [root]


def _scope_entries(
    module: _ParsedModule,
) -> list[tuple[ast.AST, str | None, str | None]]:
    scopes: list[tuple[ast.AST, str | None, str | None]] = [
        (module.tree, None, None),
        *(
            (node, qualified, None)
            for qualified, node, _ in _class_entries(module)
        ),
    ]
    scopes.extend(
        (entry.node, entry.owner, entry.receiver)
        for entry in _function_entries(module)
    )
    return scopes


def _assignment(node: ast.AST) -> tuple[list[ast.AST], ast.AST | None]:
    if isinstance(node, ast.Assign):
        return node.targets, node.value
    if isinstance(node, ast.AnnAssign):
        return [node.target], node.value
    return [], None


def _name_bindings(node: ast.AST) -> list[tuple[str, ast.AST]]:
    targets, value = _assignment(node)
    if value is None:
        return []
    return [
        (target.id, value)
        for target in targets
        if isinstance(target, ast.Name)
    ]


def _parameter_defaults(node: ast.AST) -> dict[str, ast.AST]:
    arguments = node.args
    positional = [*arguments.posonlyargs, *arguments.args]
    bound = dict(
        zip(
            (argument.arg for argument in positional[len(positional) - len(arguments.defaults) :]),
            arguments.defaults,
        )
    )
    bound.update(
        (argument.arg, default)
        for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults)
        if default is not None
    )
    return bound


def _sibling_references(
    module: _ParsedModule,
    modules: dict[str, _ParsedModule],
    sibling_usecases: set[str],
    target_owners: set[str],
) -> set[str]:
    references: set[str] = set()
    for scope, owner, receiver in _scope_entries(module):
        if owner not in target_owners:
            continue
        aliases = _scope_aliases(module, scope, modules, owner, receiver)
        for node in _nodes_in_scope(scope):
            if not isinstance(node, (ast.Name, ast.Attribute)):
                continue
            resolved = _resolve_expression(node, aliases, modules)
            if resolved in sibling_usecases:
                references.add(resolved)
    return references


def _transitive_target_references(
    modules: dict[str, _ParsedModule],
    sibling_usecases: set[str],
    target_classes: set[str],
) -> set[str]:
    callables: dict[str, tuple[_ParsedModule, _FunctionEntry]] = {}
    pending: list[str] = []
    for module in modules.values():
        for entry in _function_entries(module):
            callables[entry.qualified] = (module, entry)
            if entry.owner in target_classes:
                pending.append(entry.qualified)
            if entry.owner and entry.local_name == "__init__":
                callables[entry.owner] = (module, entry)

    references: set[str] = set()
    visited: set[str] = set()
    while pending:
        qualified = pending.pop()
        if qualified in visited:
            continue
        visited.add(qualified)
        callable_entry = callables.get(qualified)
        if callable_entry is None:
            continue
        module, entry = callable_entry
        aliases = _scope_aliases(
            module,
            entry.node,
            modules,
            entry.owner,
            entry.receiver,
        )
        for node in _nodes_in_scope(entry.node):
            if isinstance(node, (ast.Name, ast.Attribute)):
                resolved = _resolve_expression(node, aliases, modules)
                if resolved in sibling_usecases:
                    references.add(resolved)
            if isinstance(node, ast.Call):
                called = _resolve_expression(node.func, aliases, modules)
                if called in callables and called not in visited:
                    pending.append(called)
    return references


def _target_injections(
    modules: dict[str, _ParsedModule],
    sibling_usecases: set[str],
    target_classes: set[str],
) -> set[str]:
    injected: set[str] = set()
    for module in modules.values():
        for scope, owner, receiver in _scope_entries(module):
            aliases = _scope_aliases(module, scope, modules, owner, receiver)
            calls = [node for node in _nodes_in_scope(scope) if isinstance(node, ast.Call)]
            enclosing = _enclosing_values(module, scope)
            for node in calls:
                if _resolve_expression(node.func, aliases, modules) not in target_classes:
                    continue
                values = {
                    **enclosing,
                    **_local_values(scope, _source_order(node)),
                }
                for argument in (
                    *node.args,
                    *(keyword.value for keyword in node.keywords),
                ):
                    injected.update(
                        _sibling_dependencies(
                            argument,
                            aliases,
                            modules,
                            sibling_usecases,
                            values,
                        )
                    )
    return injected


def _enclosing_values(module: _ParsedModule, scope: ast.AST) -> dict[str, ast.AST]:
    values: dict[str, ast.AST] = {}
    for lexical_scope in _scope_chain(module.tree, scope):
        if isinstance(lexical_scope, _CALLABLE_SCOPES):
            values.update(_parameter_defaults(lexical_scope))
        if lexical_scope is scope:
            continue
        for node in sorted(_nodes_in_scope(lexical_scope), key=_source_order):
            values.update(_name_bindings(node))
    return values


def _local_values(scope: ast.AST, limit: tuple[int, int]) -> dict[str, ast.AST]:
    """호출 지점 앞에서 끝난 같은 scope의 바인딩만 돌려준다.

    scope 전체의 마지막 바인딩만 보면, 위반 호출 뒤에 같은 이름을 다른 값으로
    덮어쓰는 것만으로 R1을 빠져나갈 수 있다.
    """
    values: dict[str, ast.AST] = {}
    for node in sorted(_nodes_in_scope(scope), key=_source_order):
        if _source_order(node) >= limit:
            break
        values.update(_name_bindings(node))
    return values


def _sibling_dependencies(
    node: ast.AST,
    aliases: dict[str, str],
    modules: dict[str, _ParsedModule],
    sibling_usecases: set[str],
    values: dict[str, ast.AST],
    seen: frozenset[str] = frozenset(),
) -> set[str]:
    if isinstance(node, (ast.Name, ast.Attribute)):
        resolved = _resolve_expression(node, aliases, modules)
        if resolved in sibling_usecases:
            return {resolved}
        if not isinstance(node, ast.Name) or node.id in seen:
            return set()
        bound = values.get(node.id)
        if bound is None:
            return set()
        return _sibling_dependencies(
            bound,
            aliases,
            modules,
            sibling_usecases,
            values,
            seen | {node.id},
        )
    if isinstance(node, ast.Call):
        children = [
            node.func,
            *node.args,
            *(keyword.value for keyword in node.keywords),
        ]
    elif isinstance(node, ast.Dict):
        children = [child for child in (*node.keys, *node.values) if child is not None]
    elif isinstance(node, (ast.List, ast.Set, ast.Tuple)):
        children = list(node.elts)
    elif isinstance(node, ast.Starred):
        children = [node.value]
    elif isinstance(node, ast.IfExp):
        children = [node.body, node.orelse]
    else:
        return set()
    dependencies: set[str] = set()
    for child in children:
        dependencies.update(
            _sibling_dependencies(
                child,
                aliases,
                modules,
                sibling_usecases,
                values,
                seen,
            )
        )
    return dependencies


def _expression_key(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _expression_key(node.value)
        return f"{owner}.{node.attr}" if owner else None
    return None
