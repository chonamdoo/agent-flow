from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from agent_flow.core.gates import GateCommand, GateResult
from agent_flow.core.report import write_run_report
from agent_flow.core.workspace_boundary import (
    WorkspaceBoundaryError,
    authenticated_git_private_directory,
)


def init_project(root: Path) -> None:
    for relative in (
        ".agent-flow/runs",
        ".agent-flow/state",
        ".agent-flow/handoffs",
        ".agent-flow/team",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)


def write_prompt(*, root: Path, run_dir: Path, stage_id: str, content: str) -> Path:
    init_project(root)
    path = run_dir / "prompts" / f"{stage_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def write_gate_results(
    *,
    run_dir: Path,
    results: list[GateResult],
    commands: list[GateCommand] | None = None,
    fingerprint: dict[str, object] | None = None,
    verification_mode: str = "full",
) -> Path:
    _validated_directory(run_dir, create=True)
    if commands is not None and len(commands) != len(results):
        raise ValueError("gate result command/result length mismatch")
    passed = all(result.passed or not result.required for result in results)
    serialized_results = [
        _gate_result_payload(
            result,
            requested_command=commands[index] if commands is not None else None,
        )
        for index, result in enumerate(results)
    ]
    payload = {
        "passed": passed,
        "status": (
            "green"
            if passed and verification_mode == "full"
            else "targeted-green"
            if passed
            else "request-changes"
        ),
        "verification_mode": verification_mode,
        "fingerprint": fingerprint,
        "recorded_at": _now(),
        "results": serialized_results,
    }
    artifact_name = (
        "gate-results.json" if verification_mode == "full" else "gate-results-targeted.json"
    )
    path = run_dir / "artifacts" / artifact_name
    _write_json_atomic(path, payload)
    legacy_path = run_dir / "gate-results.json"
    _write_json_atomic(legacy_path, serialized_results)
    if not passed and verification_mode != "full":
        canonical_path = run_dir / "artifacts" / "gate-results.json"
        _write_json_atomic(canonical_path, payload)
    elif passed and verification_mode != "full":
        canonical_path = run_dir / "artifacts" / "gate-results.json"
        try:
            canonical = _read_json_regular(canonical_path)
            if not (
                isinstance(canonical, dict)
                and
                canonical.get("verification_mode") == "full"
                and canonical.get("status") == "green"
                and canonical.get("passed") is True
                and isinstance(canonical.get("fingerprint"), dict)
                and canonical["fingerprint"].get("tree_id") == (fingerprint or {}).get("tree_id")
            ):
                _unlink_regular(canonical_path)
        except FileNotFoundError:
            pass
        except json.JSONDecodeError:
            _unlink_regular(canonical_path)
    write_run_report(run_dir)
    return path


def _gate_input_fingerprint(
    root: Path,
    command: GateCommand,
    *,
    verification_mode: str,
    changed: list[str],
    all_files: list[str],
    common: dict[str, object],
) -> dict[str, object]:
    scope_files, scope_kind = _gate_scope_files(
        root,
        command,
        verification_mode=verification_mode,
        changed=changed,
        all_files=all_files,
    )
    production = [item for item in scope_files if not _is_test_path(item)]
    tests = [item for item in scope_files if _is_test_path(item)]
    is_test = _is_test_gate(command)
    includes_test_sources = is_test or _is_architecture_gate(command)
    relevant_scope = sorted(set(production + (tests if includes_test_sources else [])))
    stable: dict[str, object] = {
        "gate_id": command.gate_id,
        "argv": list(command.command),
        "required": command.required,
        "scope": scope_kind,
        "scope_files": relevant_scope,
        "production_sha256": _path_set_hash(root, production),
        "test_sha256": _path_set_hash(root, tests) if includes_test_sources else None,
        **common,
    }
    encoded = json.dumps(stable, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return {
        **stable,
        "fingerprint_id": hashlib.sha256(encoded).hexdigest(),
    }


def _gate_scope_files(
    root: Path,
    command: GateCommand,
    *,
    verification_mode: str,
    changed: list[str],
    all_files: list[str],
) -> tuple[list[str], str]:
    if verification_mode == "full":
        return sorted(set(all_files)), "repository"
    if "--files" in command.command:
        index = command.command.index("--files") + 1
        return sorted(set(command.command[index:])), "explicit-files"
    module_prefixes = _android_command_module_prefixes(command.command)
    if module_prefixes:
        dependency_prefixes = _gradle_dependency_prefixes(root, module_prefixes)
        if dependency_prefixes is None:
            return sorted(set(all_files)), "android-repository-fallback"
        scoped = [
            item
            for item in all_files
            if any(
                item == prefix or item.startswith(f"{prefix}/")
                for prefix in dependency_prefixes
            )
        ]
        scoped.extend(
            item
            for item in changed
            if any(
                item == prefix or item.startswith(f"{prefix}/")
                for prefix in dependency_prefixes
            )
        )
        return sorted(set(scoped)), "android-module-dependency-closure"
    return sorted(set(changed)), "changed-files"


def _android_command_module_prefixes(command: tuple[str, ...]) -> tuple[str, ...]:
    prefixes: set[str] = set()
    for value in command[1:]:
        if not value.startswith(":") or value.startswith("--"):
            continue
        parts = [part for part in value.split(":") if part]
        if len(parts) > 1:
            prefixes.add("/".join(parts[:-1]))
    return tuple(sorted(prefixes))


def _gradle_dependency_prefixes(
    root: Path,
    initial: tuple[str, ...],
) -> tuple[str, ...] | None:
    dependency_pattern = re.compile(
        r"\bproject\s*\(\s*(?:(?:path\s*[:=]\s*)?['\"](?P<path>:[^'\"]+)['\"])\s*\)"
    )
    resolved = set(initial)
    pending = list(initial)
    for settings in (root / "settings.gradle.kts", root / "settings.gradle"):
        if not settings.is_file():
            continue
        try:
            settings_content = settings.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        if re.search(
            r"\b(?:plugins|includeBuild|includeFlat|projectDir|setProjectDir|apply|alias)\b",
            settings_content,
        ):
            return None
    for root_build in (root / "build.gradle.kts", root / "build.gradle"):
        if not root_build.is_file():
            continue
        try:
            root_content = root_build.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        if re.search(
            r"\b(?:allprojects|subprojects|configure|project|dependencies|"
            r"afterEvaluate|plugins\.withId)\b",
            root_content,
        ) or re.search(r"\b(?:alias|apply)\s*\(", root_content):
            return None
        root_plugins = re.findall(
            r"\bid\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
            root_content,
        )
        if any(
            plugin not in {"java", "java-library", "org.jetbrains.kotlin"}
            and not plugin.startswith(("com.android.", "org.jetbrains.kotlin."))
            for plugin in root_plugins
        ):
            return None
    while pending:
        module = pending.pop()
        module_root = root.joinpath(*module.split("/"))
        build_file = next(
            (
                candidate
                for candidate in (
                    module_root / "build.gradle.kts",
                    module_root / "build.gradle",
                )
                if candidate.is_file()
            ),
            None,
        )
        if build_file is None:
            return None
        try:
            content = build_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        declared_plugins = re.findall(
            r"\bid\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
            content,
        )
        if any(
            plugin not in {"java", "java-library", "org.jetbrains.kotlin"}
            and not plugin.startswith(("com.android.", "org.jetbrains.kotlin."))
            for plugin in declared_plugins
        ) or re.search(r"\b(?:alias|apply)\s*\(", content):
            return None
        residual = dependency_pattern.sub("", content)
        if re.search(r"\bproject\s*\(", residual) or re.search(r"\bprojects\.", residual):
            return None
        for match in dependency_pattern.finditer(content):
            dependency = match.group("path").strip(":").replace(":", "/")
            if dependency and dependency not in resolved:
                resolved.add(dependency)
                pending.append(dependency)
    return tuple(sorted(resolved))


def _is_test_gate(command: GateCommand) -> bool:
    return "test" in command.gate_id.lower() or any(
        "test" in value.lower() for value in command.command[1:] if not value.startswith("--")
    )


def _is_architecture_gate(command: GateCommand) -> bool:
    return "architecture-lint" in command.gate_id or "architecture-lint" in command.command


def _is_device_gate(command: GateCommand) -> bool:
    return any(
        "connected" in value.lower() and "androidtest" in value.lower()
        for value in command.command
    )


def _gate_cache_path(root: Path, run_dir: Path, *, create: bool) -> Path | None:
    common_value = _git_output(root.resolve(strict=True), "rev-parse", "--git-common-dir")
    if not common_value:
        return None
    common = Path(common_value)
    if not common.is_absolute():
        common = root / common
    try:
        authority = authenticated_git_private_directory(
            common.resolve(strict=True),
            "agent-flow",
            "gate-cache",
            create=create,
        )
        resolved_run = _validated_directory(run_dir, create=create)
    except (FileNotFoundError, OSError, WorkspaceBoundaryError):
        return None
    metadata = resolved_run.lstat()
    digest = hashlib.sha256(
        f"{resolved_run}\0{metadata.st_dev}\0{metadata.st_ino}".encode("utf-8")
    ).hexdigest()
    return authority / f"{digest}.json"


def _validated_directory(path: Path, *, create: bool) -> Path:
    absolute = path.absolute()
    if create:
        absolute.mkdir(parents=True, exist_ok=True)
    metadata = absolute.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise WorkspaceBoundaryError(f"artifact directory is not regular: {absolute}")
    return absolute.resolve(strict=True)


def _write_json_atomic(path: Path, payload: object) -> None:
    parent = _validated_directory(path.parent, create=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    directory = os.open(parent, directory_flags)
    temporary_name = f".{path.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    descriptor = -1
    try:
        try:
            existing = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise WorkspaceBoundaryError(f"artifact file is not regular: {path}")
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory)
        encoded = f"{json.dumps(payload, indent=2, sort_keys=True)}\n".encode("utf-8")
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("artifact write failed")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        os.fsync(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def _read_json_regular(path: Path) -> object:
    parent = _validated_directory(path.parent, create=False)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    directory = os.open(parent, directory_flags)
    try:
        descriptor = os.open(path.name, flags, dir_fd=directory)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise WorkspaceBoundaryError(f"artifact file is not regular: {path}")
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = -1
                return json.load(stream)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    finally:
        os.close(directory)


def _unlink_regular(path: Path) -> None:
    parent = _validated_directory(path.parent, create=False)
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    directory = os.open(parent, directory_flags)
    try:
        metadata = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise WorkspaceBoundaryError(f"artifact file is not regular: {path}")
        os.unlink(path.name, dir_fd=directory)
    finally:
        os.close(directory)


def gate_execution_fingerprint(
    *,
    root: Path,
    profile_ids: list[str],
    verification_mode: str,
    changed_files: list[str] | None,
    commands: list[GateCommand],
) -> dict[str, object]:
    resolved = root.resolve(strict=True)
    inside_worktree = _git_output(resolved, "rev-parse", "--is-inside-work-tree") == "true"
    git_tree = _git_output(resolved, "rev-parse", "HEAD^{tree}")
    diff = _git_bytes_optional(resolved, "diff", "--binary", "HEAD")
    untracked = _git_lines_optional(resolved, "ls-files", "--others", "--exclude-standard")
    git_files = _git_lines_optional(
        resolved,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
    )
    filesystem_files = _filesystem_files(resolved)
    all_files = git_files if git_files is not None else filesystem_files
    symlinks_replayable = _symlink_targets_replayable(resolved, all_files)
    reusable = (
        inside_worktree
        and git_tree is not None
        and diff is not None
        and untracked is not None
        and git_files is not None
        and changed_files is not None
        and symlinks_replayable
    )
    changed = sorted(set(changed_files or []) | set(untracked or []))
    production = [item for item in changed if not _is_test_path(item)]
    tests = [item for item in changed if _is_test_path(item)]
    dependency_files = _git_config_files(
        resolved,
        {
            "package-lock.json",
            "npm-shrinkwrap.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "gradle.lockfile",
            "libs.versions.toml",
        },
    )
    config_files = _git_config_files(
        resolved,
        {
            "package.json",
            "pyproject.toml",
            "settings.gradle",
            "settings.gradle.kts",
            "build.gradle",
            "build.gradle.kts",
            "gradle.properties",
            "gradle-wrapper.properties",
        },
    )
    config_files = [item for item in config_files if _is_global_gate_config(item)]
    common: dict[str, object] = {
        "dependency_lock_sha256": _path_set_hash(resolved, dependency_files),
        "build_test_config_sha256": _path_set_hash(resolved, config_files),
        "profile_gate_definition_sha256": _profile_gate_definition_hash(
            resolved,
            profile_ids,
        ),
        "gate_planner_sha256": _gate_planner_hash(),
        "toolchain": _gate_toolchain(resolved),
        "profile": profile_ids,
        "host": os.environ.get("AGENT_FLOW_ACTIVE_HOST", "codex"),
        "environment": _gate_environment_fingerprint(),
    }
    gate_inputs = [
        _gate_input_fingerprint(
            resolved,
            command,
            verification_mode=verification_mode,
            changed=changed,
            all_files=all_files,
            common=common,
        )
        for command in commands
    ]
    stable: dict[str, object] = {
        "git_tree": git_tree,
        "worktree_diff_sha256": hashlib.sha256(diff or b"").hexdigest(),
        "untracked_sha256": _path_set_hash(resolved, untracked or []),
        "filesystem_state_sha256": _path_set_hash(resolved, all_files),
        "changed_files": changed,
        "production_sha256": _path_set_hash(resolved, production),
        "test_sha256": _path_set_hash(resolved, tests),
        **common,
        "verification_mode": verification_mode,
        "gate_inputs": gate_inputs,
        "reusable": reusable,
        "route_replayable": symlinks_replayable,
    }
    encoded = json.dumps(stable, separators=(",", ":"), sort_keys=True).encode("utf-8")
    input_stable = {key: value for key, value in stable.items() if key != "verification_mode"}
    input_encoded = json.dumps(
        input_stable,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    tree_stable = {
        key: value
        for key, value in stable.items()
        if key
        not in {
            "verification_mode",
            "changed_files",
            "production_sha256",
            "test_sha256",
            "gate_inputs",
            "reusable",
        }
    }
    tree_encoded = json.dumps(
        tree_stable,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return {
        **stable,
        "input_id": hashlib.sha256(input_encoded).hexdigest(),
        "tree_id": hashlib.sha256(tree_encoded).hexdigest(),
        "fingerprint_id": hashlib.sha256(encoded).hexdigest(),
        "recorded_at": _now(),
    }


def gate_fingerprint_matches_current(root: Path, payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    fingerprint = payload.get("fingerprint")
    if (
        payload.get("verification_mode") != "full"
        or not isinstance(fingerprint, dict)
        or fingerprint.get("route_replayable") is not True
        or not isinstance(fingerprint.get("fingerprint_id"), str)
        or not isinstance(fingerprint.get("profile"), list)
        or not all(isinstance(item, str) for item in fingerprint["profile"])
    ):
        return False
    gate_inputs = fingerprint.get("gate_inputs")
    results = payload.get("results")
    if (
        not isinstance(gate_inputs, list)
        or not gate_inputs
        or not isinstance(results, list)
        or len(gate_inputs) != len(results)
    ):
        return False
    commands: list[GateCommand] = []
    for gate_input, result in zip(gate_inputs, results):
        if not isinstance(gate_input, dict) or not isinstance(result, dict):
            return False
        gate_id = gate_input.get("gate_id")
        argv = gate_input.get("argv")
        required = gate_input.get("required")
        if (
            not isinstance(gate_id, str)
            or not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) for item in argv)
            or not isinstance(required, bool)
            or result.get("gate_id") != gate_id
            or result.get("requested_argv") != argv
            or result.get("required") is not required
            or (
                required
                and (
                    result.get("passed") is not True
                    or result.get("exit_code") != 0
                )
            )
        ):
            return False
        commands.append(GateCommand(gate_id, tuple(argv), required=required))
    try:
        changed_files = _current_changed_files(root)
        current = gate_execution_fingerprint(
            root=root,
            profile_ids=list(fingerprint["profile"]),
            verification_mode="full",
            changed_files=changed_files,
            commands=commands,
        )
    except (OSError, ValueError):
        return False
    return (
        current.get("route_replayable") is True
        and current.get("fingerprint_id") == fingerprint.get("fingerprint_id")
    )


def reusable_gate_results(
    *,
    run_dir: Path,
    root: Path,
    commands: list[GateCommand],
    fingerprint: dict[str, object],
) -> dict[int, GateResult]:
    if fingerprint.get("reusable") is not True:
        return {}
    cache_path = _gate_cache_path(root, run_dir, create=False)
    if cache_path is None:
        return {}
    try:
        payload = _read_json_regular(cache_path)
    except (FileNotFoundError, json.JSONDecodeError, OSError, WorkspaceBoundaryError):
        return {}
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        return {}
    try:
        expected_run_dir = str(run_dir.resolve(strict=True))
        run_metadata = run_dir.lstat()
    except OSError:
        return {}
    if (
        payload.get("run_dir") != expected_run_dir
        or payload.get("run_dir_device") != run_metadata.st_dev
        or payload.get("run_dir_inode") != run_metadata.st_ino
    ):
        return {}
    gate_inputs = fingerprint.get("gate_inputs")
    if not isinstance(gate_inputs, list) or len(gate_inputs) != len(commands):
        return {}
    reusable: dict[int, GateResult] = {}
    for index, command in enumerate(commands):
        if _is_device_gate(command):
            continue
        gate_input = gate_inputs[index]
        if not isinstance(gate_input, dict):
            continue
        expected_fingerprint = gate_input.get("fingerprint_id")
        for entry in payload["entries"]:
            if (
                not isinstance(entry, dict)
                or entry.get("gate_fingerprint_id") != expected_fingerprint
                or entry.get("gate_id") != command.gate_id
                or entry.get("argv") != list(command.command)
                or entry.get("required") is not command.required
            ):
                continue
            result = entry.get("result")
            if (
                not isinstance(result, dict)
                or result.get("gate_id") != command.gate_id
                or result.get("requested_argv") != list(command.command)
                or result.get("required") is not command.required
                or result.get("passed") is not True
                or result.get("exit_code") != 0
                or not isinstance(result.get("argv"), list)
                or not result.get("argv")
            ):
                continue
            try:
                reusable[index] = GateResult(
                    gate_id=str(result["gate_id"]),
                    command=tuple(str(item) for item in result["argv"]),
                    passed=True,
                    exit_code=int(result["exit_code"]),
                    stdout=str(result.get("stdout", "")),
                    stderr=str(result.get("stderr", "")),
                    required=bool(result.get("required", True)),
                    executed_at=str(entry.get("executed_at") or "") or None,
                    reused=True,
                    reused_at=_now(),
                )
            except (KeyError, TypeError, ValueError):
                pass
            break
    return reusable


def write_gate_cache(
    *,
    run_dir: Path,
    root: Path,
    commands: list[GateCommand],
    results: list[GateResult],
    fingerprint: dict[str, object],
    reused_indices: set[int] | None = None,
) -> Path:
    path = _gate_cache_path(root, run_dir, create=True)
    if path is None or fingerprint.get("reusable") is not True:
        return run_dir / "artifacts" / "gate-cache.disabled"
    try:
        current = _read_json_regular(path)
        entries = current.get("entries", []) if isinstance(current, dict) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError, WorkspaceBoundaryError):
        entries = []
    gate_inputs = fingerprint.get("gate_inputs")
    if not isinstance(gate_inputs, list) or len(gate_inputs) != len(commands):
        raise ValueError("gate cache fingerprint/command length mismatch")
    reused = reused_indices or set()
    replacements = {
        (gate_inputs[index].get("fingerprint_id"), command.gate_id, tuple(command.command), command.required)
        for index, command in enumerate(commands)
        if index not in reused and isinstance(gate_inputs[index], dict)
    }
    kept = [
        entry
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("argv"), list)
        and (
            entry.get("gate_fingerprint_id"),
            entry.get("gate_id"),
            tuple(entry.get("argv", [])),
            entry.get("required"),
        )
        not in replacements
    ]
    if len(commands) != len(results):
        raise ValueError("gate cache command/result length mismatch")
    for index, (command, result) in enumerate(zip(commands, results)):
        if index in reused or _is_device_gate(command):
            continue
        gate_input = gate_inputs[index]
        if not isinstance(gate_input, dict):
            continue
        executed_at = result.executed_at or _now()
        result_payload = _gate_result_payload(result)
        result_payload["requested_argv"] = list(command.command)
        result_payload["executed_at"] = executed_at
        kept.append(
            {
                "gate_fingerprint_id": gate_input.get("fingerprint_id"),
                "gate_id": command.gate_id,
                "argv": list(command.command),
                "required": command.required,
                "executed_at": executed_at,
                "result": result_payload,
            }
        )
    run_metadata = run_dir.lstat()
    _write_json_atomic(
        path,
        {
            "run_dir": str(run_dir.resolve(strict=True)),
            "run_dir_device": run_metadata.st_dev,
            "run_dir_inode": run_metadata.st_ino,
            "entries": kept,
        },
    )
    return path


def remove_gate_cache(*, root: Path, run_dir: Path) -> bool:
    path = _gate_cache_path(root, run_dir, create=False)
    if path is None:
        return False
    try:
        _unlink_regular(path)
    except FileNotFoundError:
        return False
    return True


def _git_output(root: Path, *args: str) -> str | None:
    result = subprocess.run(
        ("git", "-C", str(root), *args),
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _git_bytes_optional(root: Path, *args: str) -> bytes | None:
    result = subprocess.run(
        ("git", "-C", str(root), *args),
        capture_output=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _git_lines_optional(root: Path, *args: str) -> list[str] | None:
    output = _git_output(root, *args)
    if output is None:
        return None
    return [line for line in output.splitlines() if line]


def _git_lines(root: Path, *args: str) -> list[str]:
    output = _git_output(root, *args)
    return [line for line in (output or "").splitlines() if line]


def _current_changed_files(root: Path) -> list[str]:
    resolved = root.resolve(strict=True)
    if _git_output(resolved, "rev-parse", "--is-inside-work-tree") != "true":
        return []
    commands: list[tuple[str, ...]] = [
        ("diff", "--no-renames", "--name-only", "--diff-filter=ACMRD"),
        ("diff", "--cached", "--no-renames", "--name-only", "--diff-filter=ACMRD"),
        ("ls-files", "--others", "--exclude-standard"),
    ]
    for base in ("origin/main", "main"):
        merge_base = _git_output(resolved, "merge-base", "HEAD", base)
        if merge_base:
            commands.append(
                (
                    "diff",
                    "--no-renames",
                    "--name-only",
                    "--diff-filter=ACMRD",
                    f"{merge_base}..HEAD",
                )
            )
            break
    changed: set[str] = set()
    for command in commands:
        values = _git_lines_optional(resolved, *command)
        if values is None:
            raise ValueError("cannot determine current gate change set")
        changed.update(values)
    return sorted(changed)


def _git_config_files(root: Path, names: set[str]) -> list[str]:
    tracked = sorted(
        item
        for item in _git_lines(root, "ls-files")
        if Path(item).name in names
    )
    if tracked or _git_output(root, "rev-parse", "--is-inside-work-tree") == "true":
        return tracked
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and path.name in names
        and not any(part in {".agent-flow", ".git", "build", "node_modules"} for part in path.parts)
    )


def _filesystem_files(root: Path) -> list[str]:
    excluded = {
        ".agent-flow",
        ".git",
        ".gradle",
        ".idea",
        "build",
        "dist",
        "node_modules",
        "__pycache__",
    }
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if (path.is_file() or path.is_symlink())
        and not any(part in excluded for part in path.relative_to(root).parts)
    )
def _is_global_gate_config(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return "/" not in normalized or normalized.startswith(
        ("gradle/", "buildSrc/", "build-logic/")
    )


def _is_test_path(value: str) -> bool:
    normalized = f"/{value.replace(os.sep, '/').lower()}/"
    return any(token in normalized for token in ("/test/", "/tests/", "/androidtest/"))


def _path_set_hash(root: Path, values: list[str]) -> str:
    digest = hashlib.sha256()
    for value in sorted(set(values)):
        digest.update(value.encode("utf-8"))
        path = root / value
        try:
            metadata = path.lstat()
        except OSError:
            digest.update(b"<missing>")
            continue
        digest.update(str(stat.S_IMODE(metadata.st_mode)).encode("ascii"))
        if path.is_symlink():
            digest.update(b"<symlink>")
            digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
            try:
                target = path.resolve(strict=True)
                target.relative_to(root)
            except (OSError, ValueError):
                digest.update(b"<external-or-missing>")
            else:
                digest.update(_path_content_hash(target))
        elif path.is_file():
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        else:
            digest.update(b"<unsupported>")
    return digest.hexdigest()


def _profile_gate_definition_hash(root: Path, profile_ids: list[str]) -> str:
    from agent_flow.core.profiles import load_project_profile_payload

    definitions: list[object] = []
    for profile_id in profile_ids:
        try:
            payload = load_project_profile_payload(root, profile_id)
        except (OSError, UnicodeError, ValueError):
            definitions.append({"id": profile_id, "error": "unavailable"})
            continue
        definitions.append({"id": payload.get("id"), "gates": payload.get("gates")})
    encoded = json.dumps(
        definitions,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _gate_planner_hash() -> str:
    from agent_flow import cli
    from agent_flow.core import profiles

    try:
        source = b"".join(
            source_path.resolve(strict=True).read_bytes()
            for source_path in (
                Path(__file__),
                Path(cli.__file__),
                Path(profiles.__file__),
            )
        )
    except (OSError, TypeError):
        return "unavailable"
    return hashlib.sha256(source).hexdigest()


def _symlink_targets_replayable(root: Path, values: list[str]) -> bool:
    for value in values:
        path = root / value
        if not path.is_symlink():
            continue
        try:
            path.resolve(strict=True).relative_to(root)
        except (OSError, ValueError):
            return False
    return True


def _path_content_hash(path: Path) -> bytes:
    digest = hashlib.sha256()
    visited: set[tuple[int, int]] = set()

    def update(current: Path) -> None:
        metadata = current.lstat()
        digest.update(str(stat.S_IMODE(metadata.st_mode)).encode("ascii"))
        identity = (metadata.st_dev, metadata.st_ino)
        if identity in visited:
            digest.update(b"<cycle>")
            return
        visited.add(identity)
        if stat.S_ISLNK(metadata.st_mode):
            digest.update(b"<symlink>")
            digest.update(os.readlink(current).encode("utf-8", errors="surrogateescape"))
            update(current.resolve(strict=True))
        elif stat.S_ISDIR(metadata.st_mode):
            digest.update(b"<directory>")
            for child in sorted(current.iterdir(), key=lambda item: item.name):
                digest.update(child.name.encode("utf-8", errors="surrogateescape"))
                update(child)
        elif stat.S_ISREG(metadata.st_mode):
            digest.update(b"<file>")
            digest.update(hashlib.sha256(current.read_bytes()).digest())
        else:
            digest.update(b"<unsupported>")

    update(path)
    return digest.digest()


def _gate_toolchain(root: Path) -> dict[str, str]:
    wrapper = root / "gradle" / "wrapper" / "gradle-wrapper.properties"
    gradle = "unavailable"
    if wrapper.is_file():
        match = re.search(r"gradle-([0-9][^-]*)-", wrapper.read_text(encoding="utf-8"))
        if match:
            gradle = match.group(1)
    return {
        "python": sys.version.split()[0],
        "node": _version_output("node", "--version"),
        "java": _version_output("java", "-version"),
        "gradle": gradle,
    }


def _version_output(*command: str) -> str:
    try:
        result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    return (result.stdout or result.stderr).splitlines()[0].strip() if result.returncode == 0 else "unavailable"


def _gate_environment_fingerprint() -> dict[str, str]:
    internal_python = {
        "PYTHONPATH",
        "PYTHONNOUSERSITE",
        "PYTHONSAFEPATH",
        "PYTHONDONTWRITEBYTECODE",
    }
    return {
        name: hashlib.sha256(value.encode("utf-8")).hexdigest()
        for name, value in os.environ.items()
        if name not in internal_python
        and (
            name in {"CI", "LANG", "LC_ALL", "TZ", "PYTHONHASHSEED"}
            or name.startswith(("ANDROID_", "GRADLE_", "JAVA_", "NODE_"))
        )
    }


def _gate_result_payload(
    result: GateResult,
    *,
    requested_command: GateCommand | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "gate_id": result.gate_id,
        "command": " ".join(result.command),
        "argv": list(result.command),
        "passed": result.passed,
        "required": result.required,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "executed_at": result.executed_at,
        "reused": result.reused,
        "reused_at": result.reused_at,
    }
    if requested_command is not None:
        payload["requested_argv"] = list(requested_command.command)
    return payload


def write_stage_result(
    *,
    run_dir: Path,
    stage_id: str,
    content: str,
    status: str = "completed",
    evidence_type: str = "observed",
    confidence: str = "unknown",
) -> Path:
    path = run_dir / "artifacts" / f"{stage_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                f"# Stage Result: {stage_id}",
                "",
                f"- Status: {status}",
                f"- Evidence Type: {evidence_type}",
                f"- Confidence: {confidence}",
                f"- Recorded At: {_now()}",
                "",
                content.rstrip(),
                "",
            ]
        ),
        encoding="utf-8",
    )
    write_run_report(run_dir)
    return path


def write_handoff(
    *,
    root: Path,
    run_dir: Path,
    from_stage: str,
    to_stage: str,
    decided: str,
    rejected: str,
    risks: str,
    files: str,
    remaining: str,
) -> Path:
    init_project(root)
    filename = f"{from_stage}-to-{to_stage}.md"
    content = "\n".join(
        [
            f"# Handoff: {from_stage} -> {to_stage}",
            "",
            f"- Decided: {decided or 'None'}",
            f"- Rejected: {rejected or 'None'}",
            f"- Risks: {risks or 'None'}",
            f"- Files: {files or 'None'}",
            f"- Remaining: {remaining or 'None'}",
            "",
        ]
    )
    run_path = run_dir / "handoffs" / filename
    run_path.parent.mkdir(parents=True, exist_ok=True)
    run_path.write_text(content, encoding="utf-8")
    write_run_report(run_dir)

    project_path = root / ".agent-flow" / "handoffs" / filename
    project_path.parent.mkdir(parents=True, exist_ok=True)
    project_path.write_text(content, encoding="utf-8")
    return run_path


def write_recovery(
    *,
    run_dir: Path,
    title: str,
    cause: str,
    artifacts: list[str],
    rerun_command: str,
    manual_action: str,
) -> Path:
    path = run_dir / "recovery.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Recovery: {title}",
        "",
        f"- Cause: {cause or 'Unknown'}",
        f"- Rerun Command: {rerun_command or 'None'}",
        f"- Manual Action: {manual_action or 'None'}",
        "",
        "## Artifacts",
        "",
    ]
    lines.extend(f"- {artifact}" for artifact in artifacts) if artifacts else lines.append("- None")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    write_run_report(run_dir)
    return path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
