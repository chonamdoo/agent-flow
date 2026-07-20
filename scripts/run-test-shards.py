#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence


ROOT = Path(__file__).resolve().parent.parent
FINAL_SHARDS = ("preflight", "fast", "integration", "worktree-lifecycle", "parity")
PUBLIC_SHARDS = ("fast", "targeted", "related", "integration", "parity", "worktree-lifecycle", "full-final")
FINGERPRINT_ENV = (
    "AGENT_FLOW_AUTO_EXTERNAL_SKILLS",
    "AGENT_FLOW_ACTIVE_HOST",
    "AGENT_FLOW_HOST",
    "AGENT_FLOW_MODULE",
    "AGENT_FLOW_PACKAGE",
    "AGENT_FLOW_PROFILE",
    "CLAUDE_HOME",
    "CI",
    "CODEX_HOME",
    "GRADLE_OPTS",
    "HOME",
    "JAVA_HOME",
    "NODE_OPTIONS",
    "OMP_HOME",
    "PYTHON",
    "PYTHON_EXECUTABLE",
    "PYTHONHASHSEED",
    "PYTHONPATH",
    "VIRTUAL_ENV",
)
DEPENDENCY_FILES = (
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "gradle.lockfile",
    "gradle/libs.versions.toml",
)
CONFIG_FILES = (
    "pyproject.toml",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "package.json",
    "settings.gradle",
    "settings.gradle.kts",
    "build.gradle",
    "build.gradle.kts",
    "gradle.properties",
)
SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

RELATED_TESTS_BY_PRODUCTION = {
    "scripts/run-test-shards.py": ("tests/test_test_shards.py",),
    "src/agent_flow/cli.py": (
        "tests/test_cli.py::CliTest::test_gate_order_ignores_changed_file_kind_tokens",
        "tests/test_runner_smoke.py::test_stale_worktree_remove_handles_reference_hook_rejection",
    ),
    "src/agent_flow/core/commands.py": (
        "tests/test_runner_smoke.py::test_run_safe_command_times_out_without_hanging",
        "tests/test_runner_smoke.py::test_compare_and_delete_preserves_branch_when_existing_reference_hook_rejects",
    ),
    "src/agent_flow/core/workspace_boundary.py": (
        "tests/test_pinned_workspace_boundary.py::test_workspace_start_claim_recovers_reused_pid_but_not_live_owner",
        "tests/test_pinned_workspace_boundary.py::test_worktree_lifecycle_claim_recovers_after_leader_head_changes",
    ),
    "src/agent_flow/core/worktrees.py": (
        "tests/test_runner_smoke.py::test_compare_and_delete_preserves_branch_that_moves_after_validation",
        "tests/test_runner_smoke.py::test_compare_and_delete_preserves_branch_that_moves_during_deletion",
        "tests/test_runner_smoke.py::test_compare_and_delete_preserves_branch_checked_out_in_another_worktree",
        "tests/test_runner_smoke.py::test_compare_and_delete_preserves_branch_checked_out_during_deletion",
        "tests/test_runner_smoke.py::test_compare_and_delete_preserves_branch_when_existing_reference_hook_rejects",
        "tests/test_runner_smoke.py::test_stale_worktree_remove_handles_reference_hook_rejection",
    ),
}


@dataclass(frozen=True)
class CommandSpec:
    label: str
    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    pytest_report: bool = False


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    requested = "targeted" if args.command == "related" else args.command
    run_id = _validate_run_id(args.run_id or _default_run_id(requested))
    run_dir = _run_directory(run_id)
    shards = FINAL_SHARDS if requested == "full-final" else (requested,)
    changed_files = _validate_changed_files(args.changed_file or _changed_files(ROOT))
    test_nodeids = _validate_test_nodeids(args.test_nodeid or ())
    if test_nodeids and requested != "targeted":
        raise SystemExit("--test-nodeid is only valid for targeted/related runs")

    if args.plan:
        print(json.dumps(_plan(shards, run_dir, changed_files, test_nodeids), indent=2))
        return 0

    fingerprint = _fingerprint(ROOT, changed_files)
    state = _load_or_create_state(
        run_dir,
        run_id,
        requested,
        fingerprint,
        args.resume,
        changed_files,
        test_nodeids,
    )

    try:
        for shard in shards:
            if _fingerprint(ROOT, changed_files)["digest"] != fingerprint["digest"]:
                state["status"] = "invalidated"
                state["invalidated_at"] = _now()
                _write_state(run_dir, state)
                print("agent-flow-tests: workspace fingerprint changed before shard", file=sys.stderr)
                return 2
            previous = state["shards"].get(shard, {})
            if args.resume and previous.get("status") == "passed":
                print(f"agent-flow-tests: reuse {shard}")
                continue
            result = _run_shard(shard, run_dir, changed_files, state, test_nodeids)
            if _fingerprint(ROOT, changed_files)["digest"] != fingerprint["digest"]:
                result["status"] = "invalidated"
                result["exit_code"] = 2
                result["invalidated_at"] = _now()
            state["shards"][shard] = result
            state["status"] = result["status"] if result["status"] in {"failed", "invalidated"} else "running"
            _write_state(run_dir, state)
            if result["status"] != "passed":
                return int(result["exit_code"] or 1)
    except KeyboardInterrupt:
        state["status"] = "interrupted"
        state["interrupted_at"] = _now()
        _write_state(run_dir, state)
        return 130

    state["status"] = "passed"
    state["completed_at"] = _now()
    _write_state(run_dir, state)
    print(f"agent-flow-tests: passed artifact={run_dir / 'state.json'}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run resumable agent-flow test shards.")
    parser.add_argument("command", choices=PUBLIC_SHARDS)
    parser.add_argument("--run-id")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--changed-file", action="append")
    parser.add_argument("--test-nodeid", action="append")
    parser.add_argument("--plan", action="store_true")
    return parser


def _plan(
    shards: Sequence[str],
    run_dir: Path,
    changed_files: Sequence[str],
    test_nodeids: Sequence[str] = (),
) -> dict[str, object]:
    return {
        "shards": {
            shard: [list(command.argv) for command in _commands_for_shard(shard, run_dir, changed_files, test_nodeids)]
            for shard in shards
        },
        "changed_files": list(changed_files),
        "test_nodeids": list(test_nodeids),
    }


def _commands_for_shard(
    shard: str,
    run_dir: Path,
    changed_files: Sequence[str],
    test_nodeids: Sequence[str] = (),
) -> tuple[CommandSpec, ...]:
    python = sys.executable
    if shard == "preflight":
        dist = run_dir / "dist"
        pycache = run_dir / "pycache"
        commands = [
            CommandSpec("build", ("uv", "build", "--out-dir", str(dist))),
            CommandSpec(
                "typecheck",
                (python, "-m", "compileall", "-q", "src", "tests"),
                {"PYTHONPYCACHEPREFIX": str(pycache)},
            ),
            CommandSpec(
                "architecture-lint",
                (python, "-m", "agent_flow.cli", "architecture-lint", "--root", ".", "--profile", "python"),
                {"PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1"},
            ),
            CommandSpec("diff-check", ("git", "diff", "--check")),
        ]
        if (ROOT / "gradlew").is_file():
            commands.append(CommandSpec("android-final", ("./gradlew", "test", "lint", "assemble")))
        return tuple(commands)
    if shard in {"fast", "integration", "worktree-lifecycle"}:
        report = run_dir / f"{shard}.pytest.json"
        commands = [
            CommandSpec(
                shard,
                (
                    python,
                    "-m",
                    "pytest",
                    "-q",
                    "--maxfail=1",
                    f"--agent-flow-shard={shard}",
                    f"--agent-flow-report={report}",
                ),
                {"PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1"},
                pytest_report=True,
            ),
        ]
        if shard == "fast":
            commands.append(
                CommandSpec(
                    "provider-registry-node",
                    ("node", "--test", "tests/test_skill_provider_registry.mjs"),
                )
            )
        return tuple(commands)
    if shard == "parity":
        report = run_dir / "parity.pytest.json"
        return (
            CommandSpec(
                "parity-pytest",
                (
                    python,
                    "-m",
                    "pytest",
                    "-q",
                    "--maxfail=1",
                    "--agent-flow-shard=parity",
                    f"--agent-flow-report={report}",
                ),
                {"PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1"},
                pytest_report=True,
            ),
            CommandSpec("source-runtime-node", ("node", "--test", "tests/test_skill_source_runtime.mjs")),
            CommandSpec("host-runtime-parity", ("node", "scripts/check-agent-flow-parity.mjs")),
        )
    if shard == "targeted":
        return _targeted_commands(run_dir, changed_files, test_nodeids)
    raise ValueError(f"unknown test shard: {shard}")


def _targeted_commands(
    run_dir: Path,
    changed_files: Sequence[str],
    test_nodeids: Sequence[str] = (),
) -> tuple[CommandSpec, ...]:
    android_modules = sorted(
        module
        for module in {_android_module(path) for path in changed_files}
        if module is not None
    )
    commands: list[CommandSpec] = []
    if android_modules and (ROOT / "gradlew").is_file():
        for module in android_modules:
            prefix = f":{module.replace('/', ':')}" if module != "." else ""
            commands.append(
                CommandSpec(
                    f"android-{'root' if module == '.' else module}",
                    (
                        "./gradlew",
                        f"{prefix}:test" if prefix else "test",
                        f"{prefix}:lint" if prefix else "lint",
                        f"{prefix}:assemble" if prefix else "assemble",
                    ),
                )
            )
    commands.extend(_javascript_targeted_commands(changed_files))
    if any(
        path in changed_files
        for path in (
            "lib/skill-provider-registry.mjs",
            "lib/skill-provider-registry-loader.mjs",
            "lib/portable-skill-name.mjs",
            "lib/skill-selection.mjs",
            "skills/provider-registry.json",
            "tests/test_skill_provider_registry.mjs",
        )
    ):
        commands.append(
            CommandSpec(
                "provider-registry-node",
                ("node", "--test", "tests/test_skill_provider_registry.mjs"),
            )
        )
    if any(
        path in changed_files
        for path in (
            "lib/runtime-parity.mjs",
            "lib/skill-selection.mjs",
            "tests/test_skill_source_runtime.mjs",
        )
    ):
        commands.append(
            CommandSpec(
                "source-runtime-node",
                ("node", "--test", "tests/test_skill_source_runtime.mjs"),
            )
        )
    if "scripts/check-agent-flow-parity.mjs" in changed_files:
        commands.append(CommandSpec("parity-syntax", ("node", "--check", "scripts/check-agent-flow-parity.mjs")))
    python_tests = tuple(test_nodeids) or _related_python_tests(changed_files)
    if python_tests:
        report = run_dir / "targeted.pytest.json"
        commands.append(
            CommandSpec(
                "targeted-pytest",
                (
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "--maxfail=1",
                    "--agent-flow-shard=targeted",
                    f"--agent-flow-report={report}",
                    *python_tests,
                ),
                {"PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1"},
                pytest_report=True,
            )
        )
    if not commands:
        report = run_dir / "targeted.pytest.json"
        commands.append(
            CommandSpec(
                "targeted-fast-fallback",
                (
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "--maxfail=1",
                    "--agent-flow-shard=fast",
                    f"--agent-flow-report={report}",
                ),
                {"PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1"},
                pytest_report=True,
            )
        )
    return tuple(commands)


def _javascript_targeted_commands(
    changed_files: Sequence[str],
) -> tuple[CommandSpec, ...]:
    javascript = [
        Path(value)
        for value in changed_files
        if (
            Path(value).suffix in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}
            or Path(value).name in {"package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "tsconfig.json"}
        )
    ]
    packages: dict[Path, list[Path]] = {}
    for path in javascript:
        for parent in (path.parent, *path.parents):
            if (ROOT / parent / "package.json").is_file():
                packages.setdefault(parent, []).append(path)
                break
    commands: list[CommandSpec] = []
    for package_root, paths in sorted(packages.items(), key=lambda item: item[0].as_posix()):
        package_json = ROOT / package_root / "package.json"
        metadata = json.loads(package_json.read_text(encoding="utf-8"))
        scripts = metadata.get("scripts") if isinstance(metadata.get("scripts"), dict) else {}
        dependencies = {
            key
            for field in ("dependencies", "devDependencies")
            for key in (metadata.get(field) or {})
        }
        relative_paths = tuple(path.relative_to(package_root).as_posix() for path in paths)
        cwd = package_root.as_posix()
        label = "root" if cwd == "." else cwd.replace("/", "-")
        npm = ("npm",) if cwd == "." else ("npm", "--prefix", cwd)
        package_config_changed = any(path.name in {"package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "tsconfig.json"} for path in paths)
        if package_config_changed and "test" in scripts:
            commands.append(CommandSpec(f"javascript-{label}-tests", (*npm, "test")))
        elif "vitest" in dependencies:
            commands.append(
                CommandSpec(
                    f"javascript-{label}-related",
                    (*npm, "exec", "--", "vitest", "related", *relative_paths, "--run"),
                )
            )
        elif "jest" in dependencies:
            commands.append(
                CommandSpec(
                    f"javascript-{label}-related",
                    (*npm, "test", "--", "--findRelatedTests", *relative_paths, "--runInBand"),
                )
            )
        elif "node --test" in str(scripts.get("test", "")) and all("test" in path.name.lower() for path in paths):
            commands.append(CommandSpec(f"javascript-{label}-tests", ("node", "--test", *relative_paths)))
        for script in ("typecheck", "lint"):
            if script in scripts:
                commands.append(CommandSpec(f"javascript-{label}-{script}", (*npm, "run", script)))
    return tuple(commands)


def _run_shard(
    shard: str,
    run_dir: Path,
    changed_files: Sequence[str],
    state: dict[str, object],
    test_nodeids: Sequence[str] = (),
) -> dict[str, object]:
    started = _now()
    results: list[dict[str, object]] = []
    commands = _commands_for_shard(shard, run_dir, changed_files, test_nodeids)
    state["shards"][shard] = {
        "status": "running",
        "started_at": started,
        "commands": [list(command.argv) for command in commands],
    }
    _write_state(run_dir, state)
    for index, command in enumerate(commands):
        command_started = _now()
        print(f"agent-flow-tests: {shard}/{command.label}: {_display_command(command.argv)}", flush=True)
        env = {**os.environ, **command.env}
        if command.pytest_report:
            report_path = _report_path(command.argv)
            if report_path:
                report_path.unlink(missing_ok=True)
        try:
            completed = subprocess.run(command.argv, cwd=ROOT, env=env, check=False)
        except KeyboardInterrupt:
            interrupted_result: dict[str, object] = {
                "label": command.label,
                "command": list(command.argv),
                "exit_code": 130,
                "status": "interrupted",
                "started_at": command_started,
                "finished_at": _now(),
            }
            if command.pytest_report:
                report_path = _report_path(command.argv)
                if report_path and report_path.is_file():
                    interrupted_result["tests"] = json.loads(report_path.read_text(encoding="utf-8"))
            results.append(interrupted_result)
            state["shards"][shard] = {
                "status": "interrupted",
                "exit_code": 130,
                "started_at": started,
                "finished_at": _now(),
                "results": results,
                "unrun_commands": [item.label for item in commands[index + 1 :]],
            }
            _write_state(run_dir, state)
            raise
        command_result: dict[str, object] = {
            "label": command.label,
            "command": list(command.argv),
            "exit_code": completed.returncode,
            "started_at": command_started,
            "finished_at": _now(),
        }
        if command.pytest_report:
            report_path = _report_path(command.argv)
            if report_path and report_path.is_file():
                command_result["tests"] = json.loads(report_path.read_text(encoding="utf-8"))
        results.append(command_result)
        state["shards"][shard] = {
            "status": "running",
            "started_at": started,
            "results": results,
            "unrun_commands": [item.label for item in commands[index + 1 :]],
        }
        _write_state(run_dir, state)
        if completed.returncode != 0:
            return {
                "status": "failed",
                "exit_code": completed.returncode,
                "started_at": started,
                "finished_at": _now(),
                "results": results,
                "unrun_commands": [item.label for item in commands[len(results) :]],
            }
    return {
        "status": "passed",
        "exit_code": 0,
        "started_at": started,
        "finished_at": _now(),
        "results": results,
        "unrun_commands": [],
    }


def _load_or_create_state(
    run_dir: Path,
    run_id: str,
    command: str,
    fingerprint: dict[str, object],
    resume: bool,
    changed_files: Sequence[str] = (),
    test_nodeids: Sequence[str] = (),
) -> dict[str, object]:
    shards = FINAL_SHARDS if command == "full-final" else (command,)
    plan_digest = _plan_digest(shards, run_dir, changed_files, test_nodeids)
    state_path = run_dir / "state.json"
    if state_path.is_file():
        if not resume:
            raise SystemExit(f"run already exists; use --resume: {run_id}")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("fingerprint", {}).get("digest") != fingerprint["digest"]:
            raise SystemExit("cannot resume test shards with a different workspace fingerprint")
        if state.get("command") != command or state.get("shard_order") != list(shards):
            raise SystemExit("cannot resume test shards with a different command or shard order")
        if state.get("plan_digest") != plan_digest:
            raise SystemExit("cannot resume test shards with a different command plan")
        return state
    if resume:
        raise SystemExit(f"test shard run does not exist: {run_id}")
    run_dir.mkdir(parents=True, exist_ok=False)
    state: dict[str, object] = {
        "version": 1,
        "run_id": run_id,
        "command": command,
        "status": "pending",
        "created_at": _now(),
        "fingerprint": fingerprint,
        "plan_digest": plan_digest,
        "shard_order": list(shards),
        "shards": {
            shard: {"status": "pending"}
            for shard in shards
        },
    }
    _write_state(run_dir, state)
    return state


def _plan_digest(
    shards: Sequence[str],
    run_dir: Path,
    changed_files: Sequence[str],
    test_nodeids: Sequence[str] = (),
) -> str:
    commands = {
        shard: [
            {
                "argv": list(command.argv),
                "env": dict(sorted(command.env.items())),
            }
            for command in _commands_for_shard(shard, run_dir, changed_files, test_nodeids)
        ]
        for shard in shards
    }
    return hashlib.sha256(
        json.dumps(commands, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _fingerprint(
    root: Path,
    changed_scope: Sequence[str] | None = None,
) -> dict[str, object]:
    files = _workspace_files(root)
    production = [
        path
        for path in files
        if path.relative_to(root).parts
        and path.relative_to(root).parts[0] in {"bin", "lib", "scripts", "src", "workflows", "profiles", "skills"}
    ]
    tests = [
        path
        for path in files
        if path.relative_to(root).parts and path.relative_to(root).parts[0] == "tests"
    ]
    dependencies = [root / value for value in DEPENDENCY_FILES if (root / value).is_file()]
    configuration = [root / value for value in CONFIG_FILES if (root / value).is_file()]
    changed_files = list(changed_scope) if changed_scope is not None else _changed_files(root)
    catalog_files = [
        path
        for path in files
        if path.relative_to(root).parts
        and (
            path.relative_to(root).parts[0] in {"profiles", "skills"}
            or "skills" in path.relative_to(root).parts[:-1]
        )
    ]
    skill_lock_files = _resolved_skill_lock_files(root)
    active_modules, active_packages = _active_scopes(root, changed_files)
    payload = {
        "git_tree_hash": _git_tree_hash(root),
        "workspace_tree_hash": _hash_files(files, root),
        "changed_files": changed_files,
        "production_file_hash": _hash_files(production, root),
        "test_file_hash": _hash_files(tests, root),
        "dependency_lockfile_hash": _hash_files(dependencies, root),
        "build_test_configuration_hash": _hash_files(configuration, root),
        "catalog_fingerprint": _catalog_fingerprint(root, catalog_files),
        "resolved_skill_lock_hash": _hash_files(skill_lock_files, root),
        "toolchain": _toolchain(root),
        "profile": os.environ.get("AGENT_FLOW_PROFILE") or "python",
        "active_modules": active_modules,
        "active_packages": active_packages,
        "hosts": ["codex", "claude", "omp"],
        "active_host": os.environ.get("AGENT_FLOW_ACTIVE_HOST") or os.environ.get("AGENT_FLOW_HOST") or "codex",
        "environment": {name: os.environ.get(name) for name in FINGERPRINT_ENV},
    }
    digest_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"active_modules", "active_packages", "changed_files", "git_tree_hash"}
    }
    payload["digest"] = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload["recorded_at"] = _now()
    return payload


def _resolved_skill_lock_files(root: Path) -> list[Path]:
    return [
        path
        for path in (
            root / "skills" / "upstream-lock.json",
            root / ".agent-flow" / "skills" / "upstream-lock.json",
            root / ".agent-flow" / "skills" / "index.json",
            root / ".agent-flow" / "kit.json",
        )
        if path.is_file()
    ]


def _git_tree_hash(root: Path) -> str:
    result = subprocess.run(
        ("git", "rev-parse", "HEAD^{tree}"),
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _workspace_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ("git", "ls-files", "-co", "--exclude-standard", "-z"),
        cwd=root,
        check=True,
        capture_output=True,
    )
    return sorted(
        root / value.decode()
        for value in result.stdout.split(b"\0")
        if value and ((root / value.decode()).is_file() or (root / value.decode()).is_symlink())
    )


def _hash_files(paths: Sequence[Path], root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        file_type = "symlink" if path.is_symlink() else "file"
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(f"{file_type}:{metadata.st_mode & 0o777:o}".encode())
        digest.update(b"\0")
        content = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
        digest.update(hashlib.sha256(content).digest())
        digest.update(b"\0")
    return digest.hexdigest()


def _catalog_fingerprint(root: Path, workspace_catalog_files: Sequence[Path]) -> str:
    digest = hashlib.sha256(_hash_files(workspace_catalog_files, root).encode())
    home = Path(os.environ.get("HOME") or Path.home())
    catalogs = (
        ("project-local", root / ".agent-flow" / "local-skills"),
        ("codex", Path(os.environ.get("CODEX_HOME") or home / ".codex") / "skills"),
        ("claude", Path(os.environ.get("CLAUDE_HOME") or home / ".claude") / "skills"),
        ("omp", Path(os.environ.get("OMP_HOME") or home / ".omp") / "agent" / "skills"),
        ("shared", home / ".agents" / "skills"),
    )
    for label, catalog in catalogs:
        digest.update(label.encode())
        digest.update(b"\0")
        digest.update(_external_tree_hash(catalog).encode())
        digest.update(b"\0")
    return digest.hexdigest()


def _external_tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.exists() and not root.is_symlink():
        digest.update(b"missing")
        return digest.hexdigest()

    def visit(path: Path, relative: str) -> None:
        try:
            metadata = path.lstat()
            mode = metadata.st_mode & 0o777
            if path.is_symlink():
                kind = "symlink"
                content = os.readlink(path).encode()
            elif path.is_dir():
                kind = "directory"
                content = b""
            elif path.is_file():
                kind = "file"
                content = path.read_bytes()
            else:
                kind = "unsupported"
                content = b""
        except OSError as exc:
            digest.update(f"error:{relative}:{exc.errno}".encode())
            return
        digest.update(f"{relative}\0{kind}:{mode:o}\0".encode())
        digest.update(hashlib.sha256(content).digest())
        if kind == "directory":
            try:
                children = sorted(path.iterdir(), key=lambda child: child.name)
            except OSError as exc:
                digest.update(f"error:{relative}:{exc.errno}".encode())
                return
            for child in children:
                child_relative = f"{relative}/{child.name}" if relative else child.name
                visit(child, child_relative)

    visit(root, "")
    return digest.hexdigest()


def _changed_files(root: Path) -> list[str]:
    result = subprocess.run(
        ("git", "status", "--porcelain=v1", "-z"),
        cwd=root,
        check=True,
        capture_output=True,
    )
    entries = result.stdout.decode(errors="surrogateescape").split("\0")
    changed: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status = entry[:2]
        changed.append(entry[3:])
        if ("R" in status or "C" in status) and index < len(entries) and entries[index]:
            changed.append(entries[index])
            index += 1
    return sorted(changed)


def _active_scopes(root: Path, changed_files: Sequence[str]) -> tuple[list[str], list[str]]:
    modules = sorted({module for value in changed_files if (module := _android_module(value)) is not None})
    package_roots: set[str] = set()
    for value in changed_files:
        path = Path(value)
        candidates = [path.parent, *path.parents]
        for candidate in candidates:
            relative = candidate.as_posix() or "."
            if (root / candidate / "package.json").is_file() or (root / candidate / "pyproject.toml").is_file():
                package_roots.add(relative)
                break
    return modules or [os.environ.get("AGENT_FLOW_MODULE") or "."], sorted(package_roots) or [os.environ.get("AGENT_FLOW_PACKAGE") or "."]


def _toolchain(root: Path) -> dict[str, str | None]:
    return {
        "python": sys.version.split()[0],
        "node": _version(("node", "--version"), root),
        "git": _version(("git", "--version"), root),
        "uv": _version(("uv", "--version"), root),
        "java": _version(("java", "-version"), root),
        "gradle": _gradle_version(root) if (root / "gradlew").is_file() else None,
    }


def _version(argv: tuple[str, ...], root: Path) -> str | None:
    if shutil.which(argv[0]) is None and not (argv[0].startswith("./") and (root / argv[0]).is_file()):
        return None
    try:
        result = subprocess.run(argv, cwd=root, text=True, capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (result.stdout or result.stderr).strip().splitlines()
    return output[0] if output else None


def _gradle_version(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ("./gradlew", "--version"),
            cwd=root,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    match = re.search(r"(?m)^Gradle\s+([^\s]+)", output)
    if match:
        return f"Gradle {match.group(1)}"
    return hashlib.sha256(output.encode()).hexdigest() if output else None


def _related_python_tests(changed_files: Sequence[str]) -> tuple[str, ...]:
    selected: set[str] = set()
    for value in changed_files:
        path = Path(value)
        mapped = RELATED_TESTS_BY_PRODUCTION.get(path.as_posix())
        if mapped:
            selected.update(mapped)
            continue
        if path.parts and path.parts[0] == "tests" and path.suffix == ".py":
            selected.add(path.as_posix())
        elif path.parts[:2] == ("src", "agent_flow") and path.suffix == ".py":
            candidate = ROOT / "tests" / f"test_{path.stem}.py"
            if candidate.is_file():
                selected.add(candidate.relative_to(ROOT).as_posix())
        elif path.as_posix() in {"bin/agent-flow-kit.mjs", "bin/agent-flow-install.mjs"}:
            selected.update(
                (
                    "tests/test_custom_skill_install.py::test_install_materializes_authenticated_project_launcher",
                    "tests/test_cli.py::CliTest::test_node_status_escapes_task_newlines_and_emits_json",
                )
            )
        elif path.as_posix().startswith("scripts/hooks/"):
            selected.add("tests/test_pinned_workspace_boundary.py")
    return tuple(sorted(selected))


def _android_module(value: str) -> str | None:
    path = Path(value)
    parts = path.parts
    if not parts:
        return None
    normalized = path.as_posix()
    if normalized == "gradle/libs.versions.toml" or normalized.startswith("gradle/wrapper/"):
        return "."
    if path.name in {"settings.gradle", "settings.gradle.kts", "gradle.properties"}:
        return "."
    if path.name in {"build.gradle", "build.gradle.kts"}:
        return path.parent.as_posix() if path.parent != Path(".") else "."
    if path.suffix == ".pro":
        parent = path.parent.parent if path.parent.name in {"config", "proguard", "r8"} else path.parent
        return parent.as_posix() if parent != Path(".") else "."
    if "src" in parts:
        index = parts.index("src")
        source_content = parts[index + 2 :]
        if (
            source_content
            and (
                source_content[0] in {"aidl", "assets", "java", "jni", "jniLibs", "kotlin", "res", "resources"}
                or path.name == "AndroidManifest.xml"
            )
        ):
            return "/".join(parts[:index]) or "."
        if path.suffix in {".kt", ".kts", ".java", ".xml", ".aidl"}:
            return "/".join(parts[:index]) or "."
    return None


def _run_directory(run_id: str) -> Path:
    _validate_run_id(run_id)
    result = subprocess.run(
        ("git", "rev-parse", "--path-format=absolute", "--git-common-dir"),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return Path(result.stdout.strip()) / "agent-flow" / "test-runs" / run_id


def _validate_changed_files(values: Sequence[str]) -> tuple[str, ...]:
    root = ROOT.resolve()
    validated: list[str] = []
    for value in values:
        path = Path(value)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise SystemExit("changed file must be a safe workspace-relative path")
        try:
            (root / path).resolve(strict=False).relative_to(root)
        except ValueError as exc:
            raise SystemExit("changed file must be a safe workspace-relative path") from exc
        validated.append(path.as_posix())
    return tuple(sorted(set(validated)))


def _validate_test_nodeids(values: Sequence[str]) -> tuple[str, ...]:
    validated: list[str] = []
    for value in values:
        test_path = value.split("::", 1)[0]
        paths = _validate_changed_files((test_path,))
        path = Path(paths[0])
        if not path.parts or path.parts[0] != "tests" or path.suffix != ".py":
            raise SystemExit("test nodeid must reference a workspace tests/*.py path")
        validated.append(value)
    return tuple(sorted(set(validated)))


def _validate_run_id(run_id: str) -> str:
    if not SAFE_RUN_ID.fullmatch(run_id) or run_id in {".", ".."}:
        raise SystemExit("run id must be one safe path segment")
    return run_id


def _write_state(run_dir: Path, state: Mapping[str, object]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    destination = run_dir / "state.json"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=run_dir, delete=False) as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(destination)


def _report_path(argv: Sequence[str]) -> Path | None:
    prefix = "--agent-flow-report="
    return next((Path(value[len(prefix) :]) for value in argv if value.startswith(prefix)), None)


def _display_command(argv: Sequence[str]) -> str:
    return " ".join(shlex.quote(value) for value in argv)


def _default_run_id(shard: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{shard}-{os.getpid()}-{uuid.uuid4().hex}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
