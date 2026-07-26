"""`layer_boundary` behavior and architecture oracle."""
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
PARAMETER_FLOW_PREFIX = "\0parameter:"
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
    __slots__ = (
        "kwarg",
        "local_name",
        "node",
        "owner",
        "parameters",
        "parent",
        "positional",
        "qualified",
        "receiver",
        "vararg",
    )

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
        if self.receiver:
            positional = positional[1:]
        self.qualified = qualified
        self.node = node
        self.owner = owner
        self.parent = parent
        self.local_name = local_name
        self.positional = positional
        self.vararg = node.args.vararg.arg if node.args.vararg else None
        self.kwarg = node.args.kwarg.arg if node.args.kwarg else None
        self.parameters = {
            *positional,
            *(argument.arg for argument in node.args.kwonlyargs),
            *(name for name in (self.vararg, self.kwarg) if name),
        }


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

    target_classes = _target_class_family(
        target_qualified,
        classes,
        modules,
    )
    target_owners = _class_ancestors(target_classes, classes, modules)
    concrete_usecases = _concrete_usecases(classes, modules)
    # 상속으로 끌어온 concrete sibling use case는 target 쪽 클래스가 아니라 위반이다.
    # 추상 base나 Protocol 조상은 concrete가 아니므로 여기 걸리지 않는다.
    inherited_siblings = (target_owners - target_classes) & concrete_usecases
    target_scope_owners = target_owners - inherited_siblings
    sibling_usecases = concrete_usecases - target_scope_owners
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


def _target_class_family(
    target_qualified: str,
    classes: dict[str, tuple[str, ast.ClassDef]],
    modules: dict[str, _ParsedModule],
) -> set[str]:
    family = {target_qualified}
    changed = True
    while changed:
        changed = False
        for qualified, (module_name, node) in classes.items():
            if qualified in family:
                continue
            module = modules[module_name]
            aliases = _scope_aliases(module, node, modules, qualified)
            if any(
                _resolve_expression(base, aliases, modules) in family
                for base in node.bases
            ):
                family.add(qualified)
                changed = True
    return family
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




def _concrete_usecases(
    classes: dict[str, tuple[str, ast.ClassDef]],
    modules: dict[str, _ParsedModule],
) -> set[str]:
    family = {
        qualified
        for qualified, (_, node) in classes.items()
        if node.name.endswith("UseCase")
    }
    changed = True
    while changed:
        changed = False
        for qualified, (module_name, node) in classes.items():
            if qualified in family:
                continue
            aliases = _scope_aliases(
                modules[module_name],
                node,
                modules,
                qualified,
            )
            if any(
                _resolve_expression(base, aliases, modules) in family
                for base in node.bases
            ):
                family.add(qualified)
                changed = True

    return {
        qualified
        for qualified in family
        if _effective_call_is_concrete(qualified, classes, modules)
    }


def _effective_call_is_concrete(
    qualified: str,
    classes: dict[str, tuple[str, ast.ClassDef]],
    modules: dict[str, _ParsedModule],
) -> bool:
    for owner in _class_mro(qualified, classes, modules):
        class_entry = classes.get(owner)
        if class_entry is None:
            continue
        member = next(
            (
                candidate
                for candidate in class_entry[1].body
                if isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef))
                and candidate.name == "__call__"
            ),
            None,
        )
        if member is None:
            continue
        decorators = {
            _expression_key(decorator)
            for decorator in member.decorator_list
        }
        return _call_has_implementation(member) and not any(
            name and name.endswith("abstractmethod")
            for name in decorators
        )
    return False


def _class_mro(
    qualified: str,
    classes: dict[str, tuple[str, ast.ClassDef]],
    modules: dict[str, _ParsedModule],
) -> tuple[str, ...]:
    cache: dict[str, tuple[str, ...]] = {}

    def resolve(current: str, active: frozenset[str]) -> tuple[str, ...]:
        if current in cache:
            return cache[current]
        if current in active or current not in classes:
            return (current,)
        module_name, node = classes[current]
        aliases = _scope_aliases(
            modules[module_name],
            node,
            modules,
            current,
        )
        bases = [
            resolved
            for base in node.bases
            if (
                resolved := _resolve_expression(base, aliases, modules)
            ) in classes
        ]
        sequences = [
            list(resolve(base, active | {current}))
            for base in bases
        ]
        sequences.append(list(bases))
        result = [current]
        while any(sequences):
            sequences = [sequence for sequence in sequences if sequence]
            candidate = next(
                (
                    sequence[0]
                    for sequence in sequences
                    if all(
                        sequence[0] not in other[1:]
                        for other in sequences
                    )
                ),
                None,
            )
            if candidate is None:
                for sequence in sequences:
                    for remaining in sequence:
                        if remaining not in result:
                            result.append(remaining)
                break
            result.append(candidate)
            for sequence in sequences:
                if sequence and sequence[0] == candidate:
                    sequence.pop(0)
        cache[current] = tuple(result)
        return cache[current]

    return resolve(qualified, frozenset())


def _call_has_implementation(
    member: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    statements = member.body
    if statements and isinstance(statements[0], ast.Expr):
        value = statements[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            statements = statements[1:]
    return any(
        not _is_placeholder_statement(statement)
        for statement in statements
    )




def _is_placeholder_statement(node: ast.stmt) -> bool:
    if isinstance(node, ast.Pass):
        return True
    if isinstance(node, ast.Expr):
        return isinstance(node.value, ast.Constant) and node.value.value is Ellipsis
    if isinstance(node, ast.Raise):
        exception = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        name = _expression_key(exception)
        return bool(name and name.endswith("NotImplementedError"))
    return False


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
            for imported in _imported_module_names(modules[module_name], modules):
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


def _imported_module_names(
    module: _ParsedModule,
    modules: dict[str, _ParsedModule],
) -> set[str]:
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

    dynamic, module_aliases = _dynamic_imports_in_scope(
        module.tree,
        module,
        {"__import__": {"__import__"}},
    )
    imported.update(dynamic)
    for scope, _, _ in _scope_entries(module):
        if isinstance(scope, ast.Module):
            continue
        lexical_aliases = module_aliases
        for lexical_scope in _scope_chain(module.tree, scope)[1:-1]:
            lexical_dynamic, lexical_aliases = _dynamic_imports_in_scope(
                lexical_scope,
                module,
                lexical_aliases,
            )
            imported.update(lexical_dynamic)
        dynamic, _ = _dynamic_imports_in_scope(
            scope,
            module,
            lexical_aliases,
        )
        imported.update(dynamic)
    return imported


def _dynamic_imports_in_scope(
    scope: ast.AST,
    module: _ParsedModule,
    initial: dict[str, set[str]],
) -> tuple[set[str], dict[str, set[str]]]:
    imported: set[str] = set()
    aliases = {
        key: set(values)
        for key, values in initial.items()
    }
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        arguments = scope.args
        for argument in [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ]:
            aliases.pop(argument.arg, None)
        if arguments.vararg:
            aliases.pop(arguments.vararg.arg, None)
        if arguments.kwarg:
            aliases.pop(arguments.kwarg.arg, None)

    def resolve_values(
        node: ast.AST,
        state: dict[str, set[str]],
    ) -> set[str]:
        if isinstance(node, ast.Name):
            return set(state.get(node.id, {node.id}))
        if isinstance(node, ast.Attribute):
            return {
                f"{owner}.{node.attr}"
                for owner in resolve_values(node.value, state)
            }
        if isinstance(node, ast.Subscript):
            return resolve_values(node.value, state)
        return set()
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        positional = [*scope.args.posonlyargs, *scope.args.args]
        default_bindings = [
            *zip(
                positional[-len(scope.args.defaults) :],
                scope.args.defaults,
            ),
            *(
                (argument, default)
                for argument, default in zip(
                    scope.args.kwonlyargs,
                    scope.args.kw_defaults,
                )
                if default is not None
            ),
        ]
        for argument, default in default_bindings:
            resolved = resolve_values(default, initial)
            if resolved:
                aliases[argument.arg] = resolved

    def scan_calls(node: ast.AST, state: dict[str, set[str]]) -> None:
        pending = [node]
        while pending:
            current = pending.pop()
            if current is not node and isinstance(current, _SCOPE_NODES):
                continue
            if isinstance(current, ast.Call) and resolve_values(
                current.func,
                state,
            ) & {"__import__", "importlib.import_module"}:
                argument = _call_argument(current, "name", 0)
                if isinstance(argument, ast.Constant) and isinstance(
                    argument.value,
                    str,
                ):
                    imported_name = argument.value
                    if imported_name.startswith("."):
                        package_argument = _call_argument(
                            current,
                            "package",
                            1,
                        )
                        package = (
                            package_argument.value
                            if isinstance(package_argument, ast.Constant)
                            and isinstance(package_argument.value, str)
                            else module.package
                        )
                        level = len(imported_name) - len(
                            imported_name.lstrip(".")
                        )
                        imported_name = _resolve_from(
                            package,
                            level,
                            imported_name[level:] or None,
                        )
                    if imported_name:
                        imported.add(imported_name)
            pending.extend(ast.iter_child_nodes(current))

    def merge(
        paths: list[dict[str, set[str]]],
    ) -> dict[str, set[str]]:
        merged: dict[str, set[str]] = {}
        for path in paths:
            for key, values in path.items():
                merged.setdefault(key, set()).update(values)
        return merged

    def execute(
        statements: list[ast.stmt],
        state: dict[str, set[str]],
    ) -> tuple[dict[str, set[str]], bool]:
        current = {
            key: set(values)
            for key, values in state.items()
        }
        for statement in statements:
            if isinstance(statement, ast.If):
                scan_calls(statement.test, current)
                condition = (
                    bool(statement.test.value)
                    if isinstance(statement.test, ast.Constant)
                    else None
                )
                if condition is True:
                    current, falls_through = execute(statement.body, current)
                elif condition is False:
                    current, falls_through = execute(
                        statement.orelse,
                        current,
                    )
                else:
                    body, body_falls = execute(statement.body, current)
                    otherwise, otherwise_falls = execute(
                        statement.orelse,
                        current,
                    )
                    paths = [
                        path
                        for path, falls in (
                            (body, body_falls),
                            (otherwise, otherwise_falls),
                        )
                        if falls
                    ]
                    if not paths:
                        return current, False
                    current = merge(paths)
                    falls_through = True
                if not falls_through:
                    return current, False
                continue
            if isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
                expression = (
                    statement.iter
                    if isinstance(statement, (ast.For, ast.AsyncFor))
                    else statement.test
                )
                scan_calls(expression, current)
                body, _ = execute(statement.body, current)
                current = merge([current, body])
                current, _ = execute(statement.orelse, current)
                continue
            if isinstance(statement, (ast.With, ast.AsyncWith)):
                for item in statement.items:
                    scan_calls(item.context_expr, current)
                current, falls_through = execute(statement.body, current)
                if not falls_through:
                    return current, False
                continue
            if isinstance(statement, ast.Try):
                body, body_falls = execute(statement.body, current)
                if body_falls:
                    body, body_falls = execute(statement.orelse, body)
                paths = [body] if body_falls else []
                for handler in statement.handlers:
                    handled, handler_falls = execute(handler.body, current)
                    if handler_falls:
                        paths.append(handled)
                if not paths:
                    return current, False
                current = merge(paths)
                current, falls_through = execute(
                    statement.finalbody,
                    current,
                )
                if not falls_through:
                    return current, False
                continue
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                current.pop(statement.name, None)
                continue
            if isinstance(statement, ast.Import):
                for imported_alias in statement.names:
                    local = (
                        imported_alias.asname
                        or imported_alias.name.partition(".")[0]
                    )
                    if imported_alias.name == "importlib":
                        current[local] = {"importlib"}
                    else:
                        current.pop(local, None)
                continue
            if isinstance(statement, ast.ImportFrom):
                base = _resolve_from(
                    module.package,
                    statement.level,
                    statement.module,
                )
                for imported_alias in statement.names:
                    local = imported_alias.asname or imported_alias.name
                    if (
                        base == "importlib"
                        and imported_alias.name == "import_module"
                    ):
                        current[local] = {"importlib.import_module"}
                    else:
                        current.pop(local, None)
                continue

            scan_calls(statement, current)
            for target, value in _assignment_bindings(statement):
                if not isinstance(target, ast.Name):
                    continue
                resolved = resolve_values(value, current) & {
                    "__import__",
                    "importlib",
                    "importlib.import_module",
                }
                if resolved:
                    current[target.id] = resolved
                else:
                    current.pop(target.id, None)
            if isinstance(statement, (ast.Return, ast.Raise)):
                return current, False
        return current, True

    if isinstance(scope, ast.Lambda):
        scan_calls(scope.body, aliases)
        return imported, aliases
    body = getattr(scope, "body", [])
    final, _ = execute(
        body if isinstance(body, list) else [],
        aliases,
    )
    return imported, final


def _call_argument(
    node: ast.Call,
    keyword_name: str,
    position: int,
) -> ast.AST | None:
    if position < len(node.args):
        return node.args[position]
    return next(
        (
            keyword.value
            for keyword in node.keywords
            if keyword.arg == keyword_name
        ),
        None,
    )


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
        if any(
            isinstance(target, ast.Name) and target.id == symbol
            for target, _ in _assignment_bindings(node)
        ):
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
        for target, value in _assignment_bindings(node):
            if not isinstance(target, ast.Name):
                continue
            resolved = _static_expression(value, aliases)
            if resolved:
                aliases[target.id] = resolved
            else:
                aliases.pop(target.id, None)
            if target.id == symbol:
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
        key = _expression_key(node)
        if key and key in aliases:
            qualified = aliases[key]
        else:
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
            targets, _ = _assignment(node)
            for target in targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    public[target.id] = f"{module.name}.{target.id}"
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
    ordered = sorted(
        nodes,
        key=lambda node: (
            getattr(node, "lineno", -1),
            getattr(node, "col_offset", -1),
        ),
    )
    for _ in range(len(ordered) + 1):
        before = aliases.copy()
        for node in ordered:
            for target, value in _assignment_bindings(node):
                if isinstance(value, (ast.Name, ast.Attribute, ast.Subscript)):
                    resolved = _resolve_expression(value, aliases, modules)
                elif isinstance(value, ast.Call):
                    candidate = _resolve_expression(value.func, aliases, modules)
                    import_name = _call_argument(value, "name", 0)
                    if (
                        candidate in {"__import__", "importlib.import_module"}
                        and isinstance(import_name, ast.Constant)
                        and isinstance(import_name.value, str)
                        and not import_name.value.startswith(".")
                    ):
                        resolved = import_name.value
                    elif candidate in callable_classes:
                        resolved = f"{candidate}.__call__"
                    else:
                        resolved = candidate if candidate in class_symbols else None
                else:
                    resolved = None
                if resolved and isinstance(target, ast.Name):
                    aliases[target.id] = resolved
                if isinstance(target, ast.Name):
                    for suffix, child in _container_members(
                        value,
                        aliases=aliases,
                        modules=modules,
                    ):
                        member = _resolve_expression(child, aliases, modules)
                        if member:
                            aliases[f"{target.id}{suffix}"] = member
        if aliases == before:
            return


_SCOPE_NODES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


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
    return sorted(
        children,
        key=lambda item: (
            getattr(item[0], "lineno", -1),
            getattr(item[0], "col_offset", -1),
        ),
    )


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


def _assignment(node: ast.AST) -> tuple[list[ast.AST], ast.AST | None]:
    if isinstance(node, ast.Assign):
        return node.targets, node.value
    if isinstance(node, ast.AnnAssign):
        return [node.target], node.value
    return [], None


def _bind_target(target: ast.AST, value: ast.AST) -> list[tuple[ast.AST, ast.AST]]:
    if isinstance(target, (ast.Tuple, ast.List)):
        starred = next(
            (
                index
                for index, child in enumerate(target.elts)
                if isinstance(child, ast.Starred)
            ),
            None,
        )
        if (
            starred is None
            and isinstance(value, (ast.Tuple, ast.List))
            and len(target.elts) == len(value.elts)
        ):
            values = list(value.elts)
        else:
            trailing = 0 if starred is None else len(target.elts) - starred - 1
            values = []
            for index, child in enumerate(target.elts):
                if isinstance(child, ast.Starred):
                    selected: ast.AST = ast.Subscript(
                        value=value,
                        slice=ast.Slice(
                            lower=ast.Constant(index),
                            upper=ast.Constant(-trailing) if trailing else None,
                        ),
                        ctx=ast.Load(),
                    )
                else:
                    selected_index = (
                        index
                        if starred is None or index < starred
                        else index - len(target.elts)
                    )
                    selected = ast.Subscript(
                        value=value,
                        slice=ast.Constant(selected_index),
                        ctx=ast.Load(),
                    )
                values.append(selected)
        return [
            binding
            for child, child_value in zip(target.elts, values)
            for binding in _bind_target(child, child_value)
        ]
    if isinstance(target, ast.Starred):
        return _bind_target(target.value, value)
    return [(target, value)]


def _assignment_bindings(node: ast.AST) -> list[tuple[ast.AST, ast.AST]]:
    targets, value = _assignment(node)
    if value is None:
        return []
    return [
        binding
        for target in targets
        for binding in _bind_target(target, value)
    ]


def _selector(node: ast.AST) -> str:
    if isinstance(node, ast.Constant):
        return repr(node.value)
    return ast.unparse(node)


def _container_members(
    node: ast.AST,
    prefix: str = "",
    *,
    aliases: dict[str, str] | None = None,
    modules: dict[str, _ParsedModule] | None = None,
) -> list[tuple[str, ast.AST]]:
    members: list[tuple[str, ast.AST]] = []
    if isinstance(node, (ast.List, ast.Tuple)):
        children = [
            (f"[{index}]", child)
            for index, child in enumerate(node.elts)
        ]
    elif isinstance(node, ast.Dict):
        children = [
            (f"[{_selector(key)}]", value)
            for key, value in zip(node.keys, node.values)
            if key is not None
        ]
    elif isinstance(node, ast.Call):
        children = [
            (f".{keyword.arg}", keyword.value)
            for keyword in node.keywords
            if keyword.arg is not None
        ]
        if aliases is not None and modules is not None:
            called = _resolve_expression(node.func, aliases, modules)
            class_entry = _classes(modules).get(called or "")
            if class_entry:
                _, class_node = class_entry
                fields = [
                    member.target.id
                    for member in class_node.body
                    if isinstance(member, ast.AnnAssign)
                    and isinstance(member.target, ast.Name)
                ]
                children.extend(
                    (f".{field}", argument)
                    for field, argument in zip(fields, node.args)
                )
    else:
        return members
    for suffix, child in children:
        path = f"{prefix}{suffix}"
        members.append((path, child))
        members.extend(
            _container_members(
                child,
                path,
                aliases=aliases,
                modules=modules,
            )
        )
    return members


def _constructed_member_argument(
    node: ast.Attribute,
    aliases: dict[str, str],
    modules: dict[str, _ParsedModule],
) -> ast.AST | None:
    if not isinstance(node.value, ast.Call):
        return None
    called = _resolve_expression(node.value.func, aliases, modules)
    classes = _classes(modules)
    if called not in classes:
        return None

    member_name = node.attr
    for owner in _class_mro(called, classes, modules):
        class_entry = classes.get(owner)
        if class_entry is None:
            continue
        property_member = next(
            (
                member
                for member in class_entry[1].body
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                and member.name == node.attr
                and any(
                    (name := _expression_key(decorator))
                    and name.endswith("property")
                    for decorator in member.decorator_list
                )
            ),
            None,
        )
        if property_member is None:
            continue
        returned_members = {
            returned.value.attr
            for returned in _nodes_in_scope(property_member)
            if isinstance(returned, ast.Return)
            and isinstance(returned.value, ast.Attribute)
            and isinstance(returned.value.value, ast.Name)
            and returned.value.value.id
            == property_member.args.args[0].arg
        }
        if len(returned_members) == 1:
            member_name = returned_members.pop()
        break

    for owner in _class_mro(called, classes, modules):
        class_entry = classes.get(owner)
        if class_entry is None:
            continue
        initializer = next(
            (
                member
                for member in class_entry[1].body
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                and member.name == "__init__"
            ),
            None,
        )
        if initializer is None:
            continue
        positional = [
            *initializer.args.posonlyargs,
            *initializer.args.args,
        ]
        if not positional:
            continue
        receiver = positional[0].arg
        parameters = positional[1:]
        for statement in _nodes_in_scope(initializer):
            for target, value in _assignment_bindings(statement):
                if not (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == receiver
                    and target.attr == member_name
                ):
                    continue
                value_key = _expression_key(value)
                parameter = next(
                    (
                        candidate
                        for candidate in parameters
                        if value_key == candidate.arg
                        or (value_key or "").startswith(f"{candidate.arg}.")
                        or (value_key or "").startswith(f"{candidate.arg}[")
                    ),
                    None,
                )
                if parameter is None:
                    continue
                argument = _call_argument(
                    node.value,
                    parameter.arg,
                    parameters.index(parameter),
                )
                if argument is not None:
                    return _select_argument(
                        argument,
                        (value_key or "")[len(parameter.arg) :],
                    )
        break
    return None


def _literal_member(
    node: ast.AST,
    aliases: dict[str, str],
    modules: dict[str, _ParsedModule],
) -> ast.AST | None:
    if isinstance(node, ast.Subscript):
        if isinstance(node.value, (ast.List, ast.Tuple)) and isinstance(
            node.slice,
            ast.Constant,
        ):
            index = node.slice.value
            if isinstance(index, int):
                try:
                    return node.value.elts[index]
                except IndexError:
                    return None
        if isinstance(node.value, ast.Dict) and isinstance(node.slice, ast.Constant):
            for key, value in zip(node.value.keys, node.value.values):
                if isinstance(key, ast.Constant) and key.value == node.slice.value:
                    return value
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Call):
        for keyword in node.value.keywords:
            if keyword.arg == node.attr:
                return keyword.value
        constructed = _constructed_member_argument(node, aliases, modules)
        if constructed is not None:
            return constructed
        called = _resolve_expression(node.value.func, aliases, modules)
        class_entry = _classes(modules).get(called or "")
        if class_entry:
            _, class_node = class_entry
            fields = [
                member.target.id
                for member in class_node.body
                if isinstance(member, ast.AnnAssign)
                and isinstance(member.target, ast.Name)
            ]
            for field, argument in zip(fields, node.value.args):
                if field == node.attr:
                    return argument
    return None


def _property_producer(
    node: ast.AST,
    aliases: dict[str, str],
    modules: dict[str, _ParsedModule],
) -> str | None:
    if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Call):
        return None
    called = _resolve_expression(node.value.func, aliases, modules)
    classes = _classes(modules)
    if called not in classes:
        return None
    for owner in _class_mro(called, classes, modules):
        class_entry = classes.get(owner)
        if class_entry is None:
            continue
        for member in class_entry[1].body:
            if (
                isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                and member.name == node.attr
                and any(
                    (name := _expression_key(decorator))
                    and name.endswith("property")
                    for decorator in member.decorator_list
                )
            ):
                return f"{owner}.{member.name}"
    return None


def _producer_siblings(flow: set[str]) -> set[str]:
    return {
        dependency
        for dependency in flow
        if not dependency.startswith(PARAMETER_FLOW_PREFIX)
    }


def _producer_parameters(flow: set[str]) -> set[str]:
    return {
        dependency.removeprefix(PARAMETER_FLOW_PREFIX)
        for dependency in flow
        if dependency.startswith(PARAMETER_FLOW_PREFIX)
    }


def _producer_entry(
    called: str | None,
    modules: dict[str, _ParsedModule],
) -> _FunctionEntry | None:
    for module in modules.values():
        for entry in _function_entries(module):
            if entry.qualified == called or (
                entry.owner == called and entry.local_name == "__call__"
            ):
                return entry
    return None


def _select_argument(argument: ast.AST, suffix: str) -> ast.AST:
    if not suffix:
        return argument
    expression = ast.parse(f"_argument{suffix}", mode="eval").body

    class ReplaceArgument(ast.NodeTransformer):
        def visit_Name(self, node: ast.Name) -> ast.AST:
            return argument if node.id == "_argument" else node

    return ReplaceArgument().visit(expression)


def _producer_arguments(
    node: ast.Call,
    called: str | None,
    flow: set[str],
    modules: dict[str, _ParsedModule],
) -> list[ast.AST]:
    entry = _producer_entry(called, modules)
    if entry is None:
        return []
    selected: list[ast.AST] = []
    for source in _producer_parameters(flow):
        parameter = next(
            (
                name
                for name in sorted(entry.parameters, key=len, reverse=True)
                if source == name
                or source.startswith(f"{name}.")
                or source.startswith(f"{name}[")
            ),
            None,
        )
        if parameter is None:
            continue
        suffix = source[len(parameter) :]
        for argument in _call_arguments_for_flow(
            node,
            entry.positional,
            entry.vararg,
            entry.kwarg,
            {parameter},
        ):
            selected.append(_select_argument(argument, suffix))
    return selected


def _sibling_dependencies(
    node: ast.AST,
    aliases: dict[str, str],
    modules: dict[str, _ParsedModule],
    sibling_usecases: set[str],
    values: dict[str, set[str]],
    producers: dict[str, set[str]],
) -> set[str]:
    property_producer = _property_producer(node, aliases, modules)
    if property_producer:
        dependencies = _producer_siblings(
            producers.get(property_producer, set())
        )
        if dependencies:
            return dependencies
    selected = _literal_member(node, aliases, modules)
    if selected is not None:
        return _sibling_dependencies(
            selected,
            aliases,
            modules,
            sibling_usecases,
            values,
            producers,
        )
    key = _expression_key(node)
    if key and key in values:
        return set(values[key])
    if isinstance(node, (ast.Attribute, ast.Subscript)):
        owner = _expression_key(node.value)
        if owner and any(
            value_key.startswith(f"{owner}.")
            or value_key.startswith(f"{owner}[")
            for value_key in values
        ):
            return set()
    if isinstance(node, (ast.Name, ast.Attribute)):
        resolved = _resolve_expression(node, aliases, modules)
        dependencies = _producer_siblings(producers.get(resolved or "", set()))
        if resolved in sibling_usecases:
            dependencies.add(resolved)
        if dependencies:
            return dependencies
    if isinstance(node, ast.Call):
        called = _resolve_expression(node.func, aliases, modules)
        flow = producers.get(called or "", set())
        dependencies = _producer_siblings(flow)
        if called in sibling_usecases:
            dependencies.add(called)
        if called in producers:
            arguments = _producer_arguments(node, called, flow, modules)
        else:
            arguments = [
                *node.args,
                *(keyword.value for keyword in node.keywords),
            ]
        for argument in arguments:
            dependencies.update(
                _sibling_dependencies(
                    argument,
                    aliases,
                    modules,
                    sibling_usecases,
                    values,
                    producers,
                )
            )
        return dependencies
    if isinstance(node, ast.Dict):
        children = [*node.keys, *node.values]
    elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        children = list(node.elts)
    elif isinstance(node, (ast.Attribute, ast.Starred, ast.Subscript)):
        children = [node.value]
    elif isinstance(node, ast.IfExp):
        children = [node.body, node.orelse]
    else:
        return set()
    dependencies: set[str] = set()
    for child in children:
        if child is not None:
            dependencies.update(
                _sibling_dependencies(
                    child,
                    aliases,
                    modules,
                    sibling_usecases,
                    values,
                    producers,
                )
            )
    return dependencies


def _created_values(
    scope: ast.AST,
    aliases: dict[str, str],
    modules: dict[str, _ParsedModule],
    sibling_usecases: set[str],
    producers: dict[str, set[str]],
    initial: dict[str, set[str]] | None = None,
) -> dict[str, set[str]]:
    def copy_state(
        state: dict[str, set[str]],
    ) -> dict[str, set[str]]:
        return {
            key: set(dependencies)
            for key, dependencies in state.items()
        }

    def assign(
        statement: ast.AST,
        state: dict[str, set[str]],
    ) -> dict[str, set[str]]:
        updated = copy_state(state)
        for target, value in _assignment_bindings(statement):
            key = _expression_key(target)
            if not key:
                continue
            candidates = [
                (key, value),
                *(
                    (f"{key}{suffix}", child)
                    for suffix, child in _container_members(
                        value,
                        aliases=aliases,
                        modules=modules,
                    )
                ),
            ]
            resolved = [
                (
                    candidate_key,
                    _sibling_dependencies(
                        candidate_value,
                        aliases,
                        modules,
                        sibling_usecases,
                        state,
                        producers,
                    ),
                )
                for candidate_key, candidate_value in candidates
            ]
            for stale in [
                value_key
                for value_key in updated
                if value_key == key
                or value_key.startswith(f"{key}.")
                or value_key.startswith(f"{key}[")
            ]:
                updated.pop(stale)
            for candidate_key, dependencies in resolved:
                if dependencies:
                    updated[candidate_key] = dependencies
        return updated

    def merge(
        paths: list[dict[str, set[str]]],
    ) -> dict[str, set[str]]:
        merged: dict[str, set[str]] = {}
        for path in paths:
            for key, dependencies in path.items():
                merged.setdefault(key, set()).update(dependencies)
        return merged

    def execute(
        statements: list[ast.stmt],
        state: dict[str, set[str]],
    ) -> tuple[dict[str, set[str]], bool]:
        current = copy_state(state)
        for statement in statements:
            if isinstance(statement, ast.If):
                condition = (
                    bool(statement.test.value)
                    if isinstance(statement.test, ast.Constant)
                    else None
                )
                if condition is True:
                    current, falls_through = execute(statement.body, current)
                elif condition is False:
                    current, falls_through = execute(
                        statement.orelse,
                        current,
                    )
                else:
                    body, body_falls = execute(statement.body, current)
                    otherwise, otherwise_falls = execute(
                        statement.orelse,
                        current,
                    )
                    paths = [
                        path
                        for path, falls in (
                            (body, body_falls),
                            (otherwise, otherwise_falls),
                        )
                        if falls
                    ]
                    if not paths:
                        return current, False
                    current = merge(paths)
                    falls_through = True
                if not falls_through:
                    return current, False
                continue
            if isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
                body, _ = execute(statement.body, current)
                current = merge([current, body])
                current, _ = execute(statement.orelse, current)
                continue
            if isinstance(statement, (ast.With, ast.AsyncWith)):
                current, falls_through = execute(statement.body, current)
                if not falls_through:
                    return current, False
                continue
            if isinstance(statement, ast.Try):
                body, body_falls = execute(statement.body, current)
                if body_falls:
                    body, body_falls = execute(statement.orelse, body)
                paths = [body] if body_falls else []
                for handler in statement.handlers:
                    handled, handler_falls = execute(handler.body, current)
                    if handler_falls:
                        paths.append(handled)
                if not paths:
                    return current, False
                current = merge(paths)
                current, falls_through = execute(statement.finalbody, current)
                if not falls_through:
                    return current, False
                continue
            current = assign(statement, current)
            if isinstance(statement, (ast.Return, ast.Raise)):
                return current, False
        return current, True

    if isinstance(scope, ast.Lambda):
        return copy_state(initial or {})
    body = getattr(scope, "body", [])
    final, _ = execute(
        body if isinstance(body, list) else [],
        initial or {},
    )
    return final


def _lexical_values(
    module: _ParsedModule,
    scope: ast.AST,
    modules: dict[str, _ParsedModule],
    sibling_usecases: set[str],
    producers: dict[str, set[str]],
    global_values: dict[str, set[str]],
) -> dict[str, set[str]]:
    values = global_values
    for lexical_scope in _scope_chain(module.tree, scope)[1:-1]:
        aliases = _scope_aliases(module, lexical_scope, modules)
        values = _created_values(
            lexical_scope,
            aliases,
            modules,
            sibling_usecases,
            producers,
            values,
        )
    return values


def _producer_flows(
    modules: dict[str, _ParsedModule],
    sibling_usecases: set[str],
) -> dict[str, set[str]]:
    producers: dict[str, set[str]] = {}
    changed = True
    while changed:
        changed = False
        for module in modules.values():
            module_aliases = _scope_aliases(module, module.tree, modules)
            global_values = _created_values(
                module.tree,
                module_aliases,
                modules,
                sibling_usecases,
                producers,
            )
            for entry in _function_entries(module):
                nodes = _nodes_in_scope(entry.node)
                aliases = _scope_aliases(
                    module,
                    entry.node,
                    modules,
                    entry.owner,
                    entry.receiver,
                )
                initial = _lexical_values(
                    module,
                    entry.node,
                    modules,
                    sibling_usecases,
                    producers,
                    global_values,
                )
                values = _created_values(
                    entry.node,
                    aliases,
                    modules,
                    sibling_usecases,
                    producers,
                    initial,
                )
                _, parameter_states = _parameter_source_states(
                    entry.node,
                    entry.parameters,
                )
                if isinstance(entry.node, ast.Lambda):
                    returned = [entry.node.body]
                else:
                    returned = [
                        node.value
                        for node in nodes
                        if isinstance(node, ast.Return)
                        and node.value is not None
                        and node in parameter_states
                    ]
                outputs: set[str] = set()
                for returned_value in returned:
                    outputs.update(
                        _sibling_dependencies(
                            returned_value,
                            aliases,
                            modules,
                            sibling_usecases,
                            values,
                            producers,
                        )
                    )
                    outputs.update(
                        f"{PARAMETER_FLOW_PREFIX}{source}"
                        for source in _parameter_lineage(
                            returned_value,
                            parameter_states.get(returned_value, {}),
                        )
                    )
                keys = [entry.qualified]
                if entry.owner and entry.local_name == "__call__":
                    keys.append(entry.owner)
                for key in keys:
                    previous = producers.get(key)
                    merged = (previous or set()) | outputs
                    if previous != merged:
                        producers[key] = merged
                        changed = True
    return producers


def _parameter_source_states(
    scope: ast.AST,
    parameters: set[str],
) -> tuple[
    dict[str, set[str]],
    dict[ast.AST, dict[str, set[str]]],
]:
    initial = {name: {name} for name in parameters}
    states: dict[ast.AST, dict[str, set[str]]] = {}

    def copy_state(
        state: dict[str, set[str]],
    ) -> dict[str, set[str]]:
        return {
            key: set(lineage)
            for key, lineage in state.items()
        }

    def record(node: ast.AST, state: dict[str, set[str]]) -> None:
        for child in [node, *_nodes_in_scope(node)]:
            states[child] = copy_state(state)

    def assign(
        node: ast.AST,
        state: dict[str, set[str]],
    ) -> dict[str, set[str]]:
        updated = copy_state(state)
        for target, value in _assignment_bindings(node):
            key = _expression_key(target)
            if not key:
                continue
            candidates = [
                (key, _parameter_lineage(value, state)),
                *(
                    (
                        f"{key}{suffix}",
                        _parameter_lineage(child, state),
                    )
                    for suffix, child in _container_members(value)
                ),
            ]
            for stale in [
                source_key
                for source_key in updated
                if source_key == key
                or source_key.startswith(f"{key}.")
                or source_key.startswith(f"{key}[")
            ]:
                updated.pop(stale)
            for candidate_key, lineage in candidates:
                if lineage:
                    updated[candidate_key] = lineage
        return updated

    def merge(
        paths: list[dict[str, set[str]]],
    ) -> dict[str, set[str]]:
        merged: dict[str, set[str]] = {}
        for path in paths:
            for key, lineage in path.items():
                merged.setdefault(key, set()).update(lineage)
        return merged

    def execute(
        statements: list[ast.stmt],
        state: dict[str, set[str]],
    ) -> tuple[dict[str, set[str]], bool]:
        current = copy_state(state)
        for statement in statements:
            record(statement, current)
            if isinstance(statement, ast.If):
                condition = (
                    bool(statement.test.value)
                    if isinstance(statement.test, ast.Constant)
                    else None
                )
                if condition is True:
                    current, falls_through = execute(statement.body, current)
                elif condition is False:
                    current, falls_through = execute(
                        statement.orelse,
                        current,
                    )
                else:
                    body, body_falls = execute(statement.body, current)
                    otherwise, otherwise_falls = execute(
                        statement.orelse,
                        current,
                    )
                    paths = [
                        path
                        for path, falls in (
                            (body, body_falls),
                            (otherwise, otherwise_falls),
                        )
                        if falls
                    ]
                    if not paths:
                        return current, False
                    current = merge(paths)
                    falls_through = True
                if not falls_through:
                    return current, False
                continue
            if isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
                body, _ = execute(statement.body, current)
                current = merge([current, body])
                current, _ = execute(statement.orelse, current)
                continue
            if isinstance(statement, (ast.With, ast.AsyncWith)):
                current, falls_through = execute(statement.body, current)
                if not falls_through:
                    return current, False
                continue
            if isinstance(statement, ast.Try):
                body, body_falls = execute(statement.body, current)
                if body_falls:
                    body, body_falls = execute(statement.orelse, body)
                paths = [body] if body_falls else []
                for handler in statement.handlers:
                    handled, handler_falls = execute(handler.body, current)
                    if handler_falls:
                        paths.append(handled)
                if not paths:
                    return current, False
                current = merge(paths)
                current, falls_through = execute(statement.finalbody, current)
                if not falls_through:
                    return current, False
                continue
            if isinstance(statement, getattr(ast, "Match", ())):
                paths: list[dict[str, set[str]]] = []
                for case in statement.cases:
                    matched, case_falls = execute(case.body, current)
                    if case_falls:
                        paths.append(matched)
                paths.append(current)
                current = merge(paths)
                continue
            current = assign(statement, current)
            if isinstance(statement, (ast.Return, ast.Raise)):
                return current, False
        return current, True

    if isinstance(scope, ast.Lambda):
        record(scope.body, initial)
        return initial, states
    body = getattr(scope, "body", [])
    final, _ = execute(
        body if isinstance(body, list) else [],
        initial,
    )
    return final, states


def _parameter_lineage(node: ast.AST, sources: dict[str, set[str]]) -> set[str]:
    key = _expression_key(node)
    if key and key in sources:
        return set(sources[key])
    if key:
        for source_key, lineage in sources.items():
            if (
                lineage == {source_key}
                and (
                    key.startswith(f"{source_key}.")
                    or key.startswith(f"{source_key}[")
                )
            ):
                return {key}
    if isinstance(node, ast.Call):
        children = [
            node.func,
            *node.args,
            *(keyword.value for keyword in node.keywords),
        ]
    elif isinstance(node, ast.Dict):
        children = [*node.keys, *node.values]
    elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        children = list(node.elts)
    elif isinstance(node, (ast.Attribute, ast.Starred, ast.Subscript)):
        children = [node.value]
    elif isinstance(node, ast.IfExp):
        children = [node.body, node.orelse]
    else:
        return set()
    lineage: set[str] = set()
    for child in children:
        if child is not None:
            lineage.update(_parameter_lineage(child, sources))
    return lineage


def _call_arguments_for_flow(
    node: ast.Call,
    positional: list[str],
    vararg: str | None,
    kwarg: str | None,
    target_parameters: set[str],
) -> list[ast.AST]:
    def selected(argument: ast.AST, parameter: str) -> list[ast.AST]:
        sources = {
            source
            for source in target_parameters
            if source == parameter
            or source.startswith(f"{parameter}.")
            or source.startswith(f"{parameter}[")
        }
        return [
            _select_argument(argument, source[len(parameter) :])
            for source in sources
        ]

    expanded_args: list[ast.AST] = []
    for argument in node.args:
        if isinstance(argument, ast.Starred) and isinstance(
            argument.value,
            (ast.List, ast.Tuple),
        ):
            expanded_args.extend(argument.value.elts)
        else:
            expanded_args.append(argument)

    arguments: list[ast.AST] = []
    fixed = expanded_args[: len(positional)]
    for index, argument in enumerate(fixed):
        parameter = positional[index]
        if isinstance(argument, ast.Starred):
            if any(
                source == parameter
                or source.startswith(f"{parameter}.")
                or source.startswith(f"{parameter}[")
                for source in target_parameters
            ):
                arguments.append(argument.value)
        else:
            arguments.extend(selected(argument, parameter))

    extra = expanded_args[len(positional) :]
    if vararg and extra:
        if any(isinstance(argument, ast.Starred) for argument in extra):
            for argument in extra:
                candidate = (
                    argument.value
                    if isinstance(argument, ast.Starred)
                    else argument
                )
                arguments.extend(selected(candidate, vararg))
        else:
            arguments.extend(
                selected(
                    ast.Tuple(elts=list(extra), ctx=ast.Load()),
                    vararg,
                )
            )

    expanded_keywords: list[ast.keyword] = []
    dynamic_keywords: list[ast.AST] = []
    for keyword in node.keywords:
        if keyword.arg is not None:
            expanded_keywords.append(keyword)
            continue
        if isinstance(keyword.value, ast.Dict) and all(
            isinstance(key, ast.Constant) and isinstance(key.value, str)
            for key in keyword.value.keys
            if key is not None
        ):
            expanded_keywords.extend(
                ast.keyword(arg=key.value, value=value)
                for key, value in zip(
                    keyword.value.keys,
                    keyword.value.values,
                )
                if isinstance(key, ast.Constant)
                and isinstance(key.value, str)
            )
        else:
            dynamic_keywords.append(keyword.value)

    for keyword in expanded_keywords:
        arguments.extend(selected(keyword.value, keyword.arg or ""))
    if kwarg and expanded_keywords:
        arguments.extend(
            selected(
                ast.Dict(
                    keys=[
                        ast.Constant(keyword.arg)
                        for keyword in expanded_keywords
                    ],
                    values=[
                        keyword.value
                        for keyword in expanded_keywords
                    ],
                ),
                kwarg,
            )
        )
    for mapping in dynamic_keywords:
        if kwarg:
            arguments.extend(selected(mapping, kwarg))
        for source in target_parameters:
            root = source.split(".", 1)[0].split("[", 1)[0]
            if root in {vararg, kwarg}:
                continue
            arguments.append(
                _select_argument(
                    mapping,
                    f"[{root!r}]{source[len(root):]}",
                )
            )
    return arguments


def _parameter_defaults(entry: _FunctionEntry) -> dict[str, ast.AST]:
    if isinstance(entry.node, ast.Lambda):
        arguments = entry.node.args
    else:
        arguments = entry.node.args
    positional = [*arguments.posonlyargs, *arguments.args]
    defaults = {
        argument.arg: value
        for argument, value in zip(
            positional[-len(arguments.defaults):],
            arguments.defaults,
        )
    } if arguments.defaults else {}
    defaults.update(
        {
            argument.arg: value
            for argument, value in zip(arguments.kwonlyargs, arguments.kw_defaults)
            if value is not None
        }
    )
    return defaults


def _factory_parameter_lineage(
    node: ast.AST,
    sources: dict[str, set[str]],
    aliases: dict[str, str],
    modules: dict[str, _ParsedModule],
    producers: dict[str, set[str]],
) -> set[str]:
    if isinstance(node, ast.Call):
        called = _resolve_expression(node.func, aliases, modules)
        if called in producers:
            lineage: set[str] = set()
            for argument in _producer_arguments(
                node,
                called,
                producers[called],
                modules,
            ):
                lineage.update(
                    _factory_parameter_lineage(
                        argument,
                        sources,
                        aliases,
                        modules,
                        producers,
                    )
                )
            return lineage
    return _parameter_lineage(node, sources)


def _returned_target_paths(
    node: ast.AST,
    aliases: dict[str, str],
    modules: dict[str, _ParsedModule],
    target_classes: set[str],
    factory_paths: dict[str, set[str]],
) -> set[str]:
    selected = _literal_member(node, aliases, modules)
    if selected is not None:
        return _returned_target_paths(
            selected,
            aliases,
            modules,
            target_classes,
            factory_paths,
        )
    if isinstance(node, ast.Call):
        called = _resolve_expression(node.func, aliases, modules)
        if called in target_classes:
            return {""}
        return set(factory_paths.get(called or "", set()))
    if isinstance(node, (ast.List, ast.Tuple)):
        return {
            f"[{index}]{path}"
            for index, child in enumerate(node.elts)
            for path in _returned_target_paths(
                child,
                aliases,
                modules,
                target_classes,
                factory_paths,
            )
        }
    if isinstance(node, ast.Dict):
        return {
            f"[{_selector(key)}]{path}"
            for key, child in zip(node.keys, node.values)
            if key is not None
            for path in _returned_target_paths(
                child,
                aliases,
                modules,
                target_classes,
                factory_paths,
            )
        }
    if isinstance(node, ast.IfExp):
        return _returned_target_paths(
            node.body,
            aliases,
            modules,
            target_classes,
            factory_paths,
        ) | _returned_target_paths(
            node.orelse,
            aliases,
            modules,
            target_classes,
            factory_paths,
        )
    return set()


def _target_factory_flows(
    modules: dict[str, _ParsedModule],
    target_classes: set[str],
    producers: dict[str, set[str]],
) -> tuple[
    dict[str, tuple[list[str], str | None, str | None, set[str]]],
    dict[str, set[str]],
]:
    flows: dict[
        str,
        tuple[list[str], str | None, str | None, set[str]],
    ] = {}
    result_paths: dict[str, set[str]] = {}
    changed = True
    while changed:
        changed = False
        for module in modules.values():
            for entry in _function_entries(module):
                nodes = _nodes_in_scope(entry.node)
                aliases = _scope_aliases(
                    module,
                    entry.node,
                    modules,
                    entry.owner,
                    entry.receiver,
                )
                _, source_states = _parameter_source_states(
                    entry.node,
                    entry.parameters,
                )
                returned_values = (
                    [entry.node.body]
                    if isinstance(entry.node, ast.Lambda)
                    else [
                        candidate.value
                        for candidate in nodes
                        if isinstance(candidate, ast.Return)
                        and candidate.value is not None
                        and candidate in source_states
                    ]
                )
                entry_paths = {
                    path
                    for returned_value in returned_values
                    for path in _returned_target_paths(
                        returned_value,
                        aliases,
                        modules,
                        target_classes,
                        result_paths,
                    )
                }
                target_parameters: set[str] = set()
                for node in nodes:
                    if not isinstance(node, ast.Call):
                        continue
                    called = _resolve_expression(node.func, aliases, modules)
                    if called in target_classes:
                        arguments = [
                            *node.args,
                            *(keyword.value for keyword in node.keywords),
                        ]
                    elif called in flows:
                        positional, vararg, kwarg, target = flows[called]
                        arguments = _call_arguments_for_flow(
                            node,
                            positional,
                            vararg,
                            kwarg,
                            target,
                        )
                    else:
                        continue
                    for argument in arguments:
                        target_parameters.update(
                            _factory_parameter_lineage(
                                argument,
                                source_states.get(node, {}),
                                aliases,
                                modules,
                                producers,
                            )
                        )
                keys = [entry.qualified]
                if entry.owner and entry.local_name == "__call__":
                    keys.append(entry.owner)
                for key in keys:
                    previous_paths = result_paths.get(key, set())
                    merged_paths = previous_paths | entry_paths
                    if previous_paths != merged_paths:
                        result_paths[key] = merged_paths
                        changed = True
                    previous = flows.get(key)
                    merged = set(previous[3]) if previous else set()
                    merged.update(target_parameters)
                    if entry_paths and (
                        previous is None or previous[3] != merged
                    ):
                        flows[key] = (
                            entry.positional,
                            entry.vararg,
                            entry.kwarg,
                            merged,
                        )
                        changed = True
    return flows, result_paths


def _target_injection_flows(
    modules: dict[str, _ParsedModule],
    target_classes: set[str],
    producers: dict[str, set[str]],
) -> dict[str, tuple[list[str], str | None, str | None, set[str]]]:
    flows: dict[
        str,
        tuple[list[str], str | None, str | None, set[str]],
    ] = {}
    changed = True
    while changed:
        changed = False
        for module in modules.values():
            for entry in _function_entries(module):
                aliases = _scope_aliases(
                    module,
                    entry.node,
                    modules,
                    entry.owner,
                    entry.receiver,
                )
                nodes = _nodes_in_scope(entry.node)
                _, source_states = _parameter_source_states(
                    entry.node,
                    entry.parameters,
                )
                target_parameters: set[str] = set()
                for node in nodes:
                    if not isinstance(node, ast.Call):
                        continue
                    called = _resolve_expression(node.func, aliases, modules)
                    if called in target_classes:
                        arguments = [
                            *node.args,
                            *(keyword.value for keyword in node.keywords),
                        ]
                    elif called in flows:
                        positional, vararg, kwarg, target = flows[called]
                        arguments = _call_arguments_for_flow(
                            node,
                            positional,
                            vararg,
                            kwarg,
                            target,
                        )
                    else:
                        continue
                    for argument in arguments:
                        target_parameters.update(
                            _factory_parameter_lineage(
                                argument,
                                source_states.get(node, {}),
                                aliases,
                                modules,
                                producers,
                            )
                        )
                if not target_parameters:
                    continue
                keys = [entry.qualified]
                if entry.owner and entry.local_name == "__call__":
                    keys.append(entry.owner)
                for key in keys:
                    previous = flows.get(key)
                    merged = set(previous[3]) if previous else set()
                    merged.update(target_parameters)
                    if previous is not None and previous[3] == merged:
                        continue
                    flows[key] = (
                        entry.positional,
                        entry.vararg,
                        entry.kwarg,
                        merged,
                    )
                    changed = True
    return flows


def _partial_target_factory(
    value: ast.AST,
    aliases: dict[str, str],
    modules: dict[str, _ParsedModule],
    target_classes: set[str],
    factory_flows: dict[str, tuple[list[str], str | None, str | None, set[str]]],
    partials: dict[str, tuple[str, list[ast.AST], list[ast.keyword]]],
) -> tuple[str, list[ast.AST], list[ast.keyword]] | None:
    if (
        isinstance(value, ast.Call)
        and _resolve_expression(value.func, aliases, modules) == "functools.partial"
        and value.args
    ):
        wrapped = _resolve_expression(value.args[0], aliases, modules)
        if wrapped in target_classes or wrapped in factory_flows:
            return wrapped, list(value.args[1:]), list(value.keywords)
        nested = partials.get(_expression_key(value.args[0]) or "")
        if nested:
            nested_wrapped, nested_args, nested_keywords = nested
            return (
                nested_wrapped,
                [*nested_args, *value.args[1:]],
                [*nested_keywords, *value.keywords],
            )
    elif isinstance(value, (ast.Name, ast.Attribute)):
        return partials.get(_expression_key(value) or "")
    return None


def _partial_target_factories(
    nodes: list[ast.AST],
    aliases: dict[str, str],
    modules: dict[str, _ParsedModule],
    target_classes: set[str],
    factory_flows: dict[str, tuple[list[str], str | None, str | None, set[str]]],
    initial: dict[str, tuple[str, list[ast.AST], list[ast.keyword]]] | None = None,
) -> dict[str, tuple[str, list[ast.AST], list[ast.keyword]]]:
    partials = dict(initial or {})
    changed = True
    while changed:
        changed = False
        for node in nodes:
            for target, value in _assignment_bindings(node):
                key = _expression_key(target)
                if not key:
                    continue
                flow = _partial_target_factory(
                    value,
                    aliases,
                    modules,
                    target_classes,
                    factory_flows,
                    partials,
                )
                if flow is not None and partials.get(key) != flow:
                    partials[key] = flow
                    changed = True
    return partials


def _target_instance_paths(
    node: ast.AST,
    aliases: dict[str, str],
    modules: dict[str, _ParsedModule],
    target_classes: set[str],
    factory_flows: dict[str, tuple[list[str], str | None, str | None, set[str]]],
    factory_paths: dict[str, set[str]],
    partials: dict[str, tuple[str, list[ast.AST], list[ast.keyword]]],
    instances: set[str],
) -> set[str]:
    selected = _literal_member(node, aliases, modules)
    if selected is not None:
        return _target_instance_paths(
            selected,
            aliases,
            modules,
            target_classes,
            factory_flows,
            factory_paths,
            partials,
            instances,
        )
    key = _expression_key(node)
    paths: set[str] = set()
    if key:
        if key in instances:
            paths.add("")
        paths.update(
            instance[len(key) :]
            for instance in instances
            if instance.startswith(f"{key}.")
            or instance.startswith(f"{key}[")
        )
    if isinstance(node, ast.Call):
        called = _resolve_expression(node.func, aliases, modules)
        if called in target_classes:
            paths.add("")
        paths.update(factory_paths.get(called or "", set()))
        partial = partials.get(_expression_key(node.func) or "")
        if partial:
            wrapped = partial[0]
            if wrapped in target_classes:
                paths.add("")
            paths.update(factory_paths.get(wrapped, set()))
        return paths
    if isinstance(node, (ast.List, ast.Tuple)):
        paths.update(
            f"[{index}]{path}"
            for index, child in enumerate(node.elts)
            for path in _target_instance_paths(
                child,
                aliases,
                modules,
                target_classes,
                factory_flows,
                factory_paths,
                partials,
                instances,
            )
        )
    elif isinstance(node, ast.Dict):
        paths.update(
            f"[{_selector(key_node)}]{path}"
            for key_node, child in zip(node.keys, node.values)
            if key_node is not None
            for path in _target_instance_paths(
                child,
                aliases,
                modules,
                target_classes,
                factory_flows,
                factory_paths,
                partials,
                instances,
            )
        )
    elif isinstance(node, ast.IfExp):
        for child in (node.body, node.orelse):
            paths.update(
                _target_instance_paths(
                    child,
                    aliases,
                    modules,
                    target_classes,
                    factory_flows,
                    factory_paths,
                    partials,
                    instances,
                )
            )
    return paths


def _target_instance_keys(
    scope: ast.AST,
    aliases: dict[str, str],
    modules: dict[str, _ParsedModule],
    target_classes: set[str],
    factory_flows: dict[str, tuple[list[str], str | None, str | None, set[str]]],
    factory_paths: dict[str, set[str]],
    partials: dict[str, tuple[str, list[ast.AST], list[ast.keyword]]],
    initial: set[str] | None = None,
) -> set[str]:
    def assign(statement: ast.AST, current: set[str]) -> set[str]:
        updated = set(current)
        for target, value in _assignment_bindings(statement):
            key = _expression_key(target)
            if not key:
                continue
            paths = _target_instance_paths(
                value,
                aliases,
                modules,
                target_classes,
                factory_flows,
                factory_paths,
                partials,
                current,
            )
            updated = {
                instance
                for instance in updated
                if instance != key
                and not instance.startswith(f"{key}.")
                and not instance.startswith(f"{key}[")
            }
            updated.update(f"{key}{path}" for path in paths)
        return updated

    def execute(statements: list[ast.stmt], current: set[str]) -> tuple[set[str], bool]:
        state = set(current)
        for statement in statements:
            if isinstance(statement, ast.If):
                condition = (
                    bool(statement.test.value)
                    if isinstance(statement.test, ast.Constant)
                    else None
                )
                if condition is True:
                    state, falls_through = execute(statement.body, state)
                elif condition is False:
                    state, falls_through = execute(statement.orelse, state)
                else:
                    body, body_falls = execute(statement.body, state)
                    otherwise, otherwise_falls = execute(
                        statement.orelse,
                        state,
                    )
                    paths = [
                        path
                        for path, falls in (
                            (body, body_falls),
                            (otherwise, otherwise_falls),
                        )
                        if falls
                    ]
                    if not paths:
                        return state, False
                    state = set().union(*paths)
                    falls_through = True
                if not falls_through:
                    return state, False
                continue
            if isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
                body, _ = execute(statement.body, state)
                state.update(body)
                state, _ = execute(statement.orelse, state)
                continue
            if isinstance(statement, (ast.With, ast.AsyncWith)):
                state, falls_through = execute(statement.body, state)
                if not falls_through:
                    return state, False
                continue
            if isinstance(statement, ast.Try):
                body, body_falls = execute(statement.body, state)
                if body_falls:
                    body, body_falls = execute(statement.orelse, body)
                paths = [body] if body_falls else []
                for handler in statement.handlers:
                    handled, handler_falls = execute(handler.body, state)
                    if handler_falls:
                        paths.append(handled)
                if not paths:
                    return state, False
                state = set().union(*paths)
                state, falls_through = execute(statement.finalbody, state)
                if not falls_through:
                    return state, False
                continue
            state = assign(statement, state)
            if isinstance(statement, (ast.Return, ast.Raise)):
                return state, False
        return state, True

    if isinstance(scope, ast.Lambda):
        return set(initial or ())
    body = getattr(scope, "body", [])
    result, _ = execute(
        body if isinstance(body, list) else [],
        set(initial or ()),
    )
    return result


def _target_member_injections(
    nodes: list[ast.AST],
    aliases: dict[str, str],
    modules: dict[str, _ParsedModule],
    instances: set[str],
) -> list[ast.AST]:
    arguments: list[ast.AST] = []
    for node in nodes:
        for target, value in _assignment_bindings(node):
            if isinstance(target, (ast.Attribute, ast.Subscript)):
                owner = _expression_key(target.value)
                if owner in instances:
                    arguments.append(value)
        if not isinstance(node, ast.Call):
            continue
        called = _resolve_expression(node.func, aliases, modules)
        if (
            called in {"setattr", "builtins.setattr"}
            and len(node.args) >= 3
            and _expression_key(node.args[0]) in instances
        ):
            arguments.append(node.args[2])
            continue
        if (
            isinstance(node.func, ast.Attribute)
            and _expression_key(node.func.value) in instances
        ):
            arguments.extend(node.args)
            arguments.extend(keyword.value for keyword in node.keywords)
    return arguments


def _target_parameter_defaults(
    entry: _FunctionEntry,
    target_parameters: set[str],
) -> list[ast.AST]:
    defaults = _parameter_defaults(entry)
    selected: list[ast.AST] = []
    for source in target_parameters:
        parameter = next(
            (
                name
                for name in sorted(defaults, key=len, reverse=True)
                if source == name
                or source.startswith(f"{name}.")
                or source.startswith(f"{name}[")
            ),
            None,
        )
        if parameter is not None:
            selected.append(
                _select_argument(
                    defaults[parameter],
                    source[len(parameter) :],
                )
            )
    return selected


def _target_injections(
    modules: dict[str, _ParsedModule],
    sibling_usecases: set[str],
    target_classes: set[str],
) -> set[str]:
    producers = _producer_flows(modules, sibling_usecases)
    injection_flows = _target_injection_flows(
        modules,
        target_classes,
        producers,
    )
    factory_flows, factory_paths = _target_factory_flows(
        modules,
        target_classes,
        producers,
    )
    injected: set[str] = set()
    entries = {
        entry.qualified: (module, entry)
        for module in modules.values()
        for entry in _function_entries(module)
    }
    for qualified, (_, _, _, target_parameters) in factory_flows.items():
        callable_entry = entries.get(qualified)
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
        for default in _target_parameter_defaults(entry, target_parameters):
            injected.update(
                _sibling_dependencies(
                    default,
                    aliases,
                    modules,
                    sibling_usecases,
                    {},
                    producers,
                )
            )
    for module in modules.values():
        module_aliases = _scope_aliases(module, module.tree, modules)
        global_nodes = _nodes_in_scope(module.tree)
        global_partials = _partial_target_factories(
            global_nodes,
            module_aliases,
            modules,
            target_classes,
            factory_flows,
        )
        global_values = _created_values(
            module.tree,
            module_aliases,
            modules,
            sibling_usecases,
            producers,
        )
        global_instances = _target_instance_keys(
            module.tree,
            module_aliases,
            modules,
            target_classes,
            factory_flows,
            factory_paths,
            global_partials,
        )
        for scope, owner, receiver in _scope_entries(module):
            nodes = _nodes_in_scope(scope)
            aliases = _scope_aliases(module, scope, modules, owner, receiver)
            initial = (
                None
                if isinstance(scope, ast.Module)
                else _lexical_values(
                    module,
                    scope,
                    modules,
                    sibling_usecases,
                    producers,
                    global_values,
                )
            )
            values = _created_values(
                scope,
                aliases,
                modules,
                sibling_usecases,
                producers,
                initial,
            )
            partials = _partial_target_factories(
                nodes,
                aliases,
                modules,
                target_classes,
                factory_flows,
                global_partials,
            )
            instances = _target_instance_keys(
                scope,
                aliases,
                modules,
                target_classes,
                factory_flows,
                factory_paths,
                partials,
                None if isinstance(scope, ast.Module) else global_instances,
            )
            for argument in _target_member_injections(
                nodes,
                aliases,
                modules,
                instances,
            ):
                injected.update(
                    _sibling_dependencies(
                        argument,
                        aliases,
                        modules,
                        sibling_usecases,
                        values,
                        producers,
                    )
                )
            for node in nodes:
                if not isinstance(node, ast.Call):
                    continue
                called = _resolve_expression(node.func, aliases, modules)
                if called in target_classes:
                    arguments = [
                        *node.args,
                        *(keyword.value for keyword in node.keywords),
                    ]
                elif called in factory_flows:
                    positional, vararg, kwarg, target_parameters = factory_flows[called]
                    arguments = _call_arguments_for_flow(
                        node,
                        positional,
                        vararg,
                        kwarg,
                        target_parameters,
                    )
                elif partial := (
                    partials.get(_expression_key(node.func) or "")
                    or _partial_target_factory(
                        node.func,
                        aliases,
                        modules,
                        target_classes,
                        factory_flows,
                        partials,
                    )
                ):
                    wrapped, bound_args, bound_keywords = partial
                    combined = ast.Call(
                        func=node.func,
                        args=[*bound_args, *node.args],
                        keywords=[*bound_keywords, *node.keywords],
                    )
                    if wrapped in target_classes:
                        arguments = [
                            *combined.args,
                            *(keyword.value for keyword in combined.keywords),
                        ]
                    else:
                        positional, vararg, kwarg, target_parameters = factory_flows[
                            wrapped
                        ]
                        arguments = _call_arguments_for_flow(
                            combined,
                            positional,
                            vararg,
                            kwarg,
                            target_parameters,
                        )
                elif called in injection_flows:
                    positional, vararg, kwarg, target_parameters = (
                        injection_flows[called]
                    )
                    arguments = _call_arguments_for_flow(
                        node,
                        positional,
                        vararg,
                        kwarg,
                        target_parameters,
                    )
                else:
                    continue
                for argument in arguments:
                    injected.update(
                        _sibling_dependencies(
                            argument,
                            aliases,
                            modules,
                            sibling_usecases,
                            values,
                            producers,
                        )
                    )
    return injected


def _expression_key(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _expression_key(node.value)
        return f"{owner}.{node.attr}" if owner else None
    if isinstance(node, ast.Subscript):
        owner = _expression_key(node.value)
        return f"{owner}[{_selector(node.slice)}]" if owner else None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
    ):
        owner = _expression_key(node.func.value)
        return f"{owner}[{_selector(node.args[0])}]" if owner else None
    return None
