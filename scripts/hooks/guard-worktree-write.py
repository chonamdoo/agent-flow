#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


WRITE_TOOLS = {
    "apply_patch",
    "edit",
    "eval",
    "multiedit",
    "multi_edit",
    "notebook",
    "notebookedit",
    "notebook_edit",
    "python",
    "write",
}
SHELL_TOOLS = {"bash", "shell", "terminal", "exec", "exec_command"}
SHELL_MUTATORS = {
    "chmod",
    "chown",
    "cp",
    "install",
    "ln",
    "mkdir",
    "mv",
    "rm",
    "rmdir",
    "sed",
    "tee",
    "touch",
    "truncate",
}
RUNTIME_SOURCE_DIRECTORIES = (
    "bin",
    "lib",
    "workflows",
    "profiles",
    "skills",
    "templates",
    "scripts",
    "bootstrap",
    "src/agent_flow",
    ".Codex/agents",
    ".Codex/rules",
    ".Codex/context",
    ".claude/agents",
)
READ_ONLY_SHELL_COMMANDS = {
    "basename",
    "cat",
    "cut",
    "dirname",
    "du",
    "env",
    "false",
    "git",
    "grep",
    "head",
    "ls",
    "pwd",
    "readlink",
    "rg",
    "sort",
    "stat",
    "tail",
    "test",
    "true",
    "type",
    "uniq",
    "wc",
    "which",
}
READ_ONLY_GIT_SUBCOMMANDS = {
    "branch",
    "diff",
    "log",
    "merge-base",
    "rev-parse",
    "show",
    "status",
}


def _load_boundary_module() -> tuple[type[Exception], object, object]:
    cwd = Path.cwd()
    runtime_root = _leader_root(cwd) / ".agent-flow" / "runtime" / "python"
    if runtime_root.is_dir():
        sys.path.insert(0, str(runtime_root))
    try:
        from agent_flow.core.workspace_boundary import (
            WorkspaceBoundaryError,
            find_active_pinned_workspace,
            resolve_mutation_path,
        )
    except ImportError as exc:
        raise RuntimeError("pinned workspace guard runtime is unavailable") from exc
    return WorkspaceBoundaryError, find_active_pinned_workspace, resolve_mutation_path


def _leader_root(cwd: Path) -> Path:
    result = subprocess.run(
        ("git", "-C", str(cwd), "rev-parse", "--path-format=absolute", "--git-common-dir"),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        return cwd.resolve()
    common = Path(result.stdout.strip()).resolve(strict=False)
    return common.parent if common.name == ".git" else cwd.resolve()


def _tool_name(payload: dict[str, object]) -> str:
    for key in ("tool_name", "toolName", "name"):
        value = payload.get(key)
        if isinstance(value, str):
            return value.lower()
    return ""


def _tool_input(payload: dict[str, object]) -> dict[str, object]:
    value = payload.get("tool_input")
    if not isinstance(value, dict):
        value = payload.get("input")
    return value if isinstance(value, dict) else {}


def _requested_paths(tool_input: dict[str, object]) -> list[str]:
    paths: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        for key in ("file_path", "filePath", "filename", "path"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                paths.append(candidate)
        patch = value.get("patch")
        if isinstance(patch, str):
            paths.extend(
                match.group(1).strip()
                for match in re.finditer(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", patch, re.MULTILINE)
            )
        edits = value.get("edits")
        if isinstance(edits, list):
            visit(edits)

    visit(tool_input)
    return list(dict.fromkeys(paths))


def _shell_mutation_paths(command: str) -> tuple[bool, list[str]]:
    unsafe_substitution = re.search(r"(?<!\\)(?:\$\(|`|<\(|>\()", command) is not None
    paths = [
        match.group(2)
        for match in re.finditer(
            r"(?:^|[\s;&|])(?:\d*>>?|&>)\s*(['\"]?)([^\s;'\"|&]+)\1",
            command,
        )
    ]
    try:
        words = shlex.split(command.replace(";", " ").replace("&&", " ").replace("||", " "))
    except ValueError:
        return True, paths
    command_names: list[str] = []
    for segment in re.split(r"(?:&&|\|\||[;|])", command):
        try:
            segment_words = shlex.split(segment)
        except ValueError:
            return True, paths
        skip_env = False
        for word in segment_words:
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", word):
                continue
            if Path(word).name.lower() == "env":
                skip_env = True
                continue
            if skip_env and word.startswith("-"):
                continue
            command_names.append(Path(word).name.lower())
            break
    commands = set(command_names)
    git_mutation = "git" in commands and any(
        word in {
            "am",
            "apply",
            "checkout",
            "cherry-pick",
            "clean",
            "merge",
            "rebase",
            "reset",
            "restore",
            "switch",
        }
        for word in words
    )
    inline_mutation = re.search(
        r"\b(write_text|write_bytes|unlink|mkdir|makedirs|remove|rename|replace)\b|"
        r"\b(writeFileSync|appendFileSync|createWriteStream|rmSync|unlinkSync|mkdirSync|renameSync)\b|"
        r"\b(File\.(?:write|delete|rename)|open)\s*\([^)]*,?\s*['\"][wax+]",
        command,
        re.IGNORECASE,
    ) is not None
    mutating_options = (
        ("sed" in commands and bool(re.search(r"(?:^|\s)-[^\s]*i", command)))
        or ("perl" in commands and bool(re.search(r"(?:^|\s)-[^\s]*[pi]", command)))
        or ("find" in commands and bool(re.search(r"(?:^|\s)-(?:delete|exec|execdir)\b", command)))
        or "dd" in commands
        or "rsync" in commands
    )
    git_read_only = "git" not in commands
    if "git" in commands:
        git_index = next(
            (index for index, word in enumerate(words) if Path(word).name.lower() == "git"),
            -1,
        )
        git_arguments = [word for word in words[git_index + 1 :] if not word.startswith("-")]
        git_subcommand = git_arguments[0] if git_arguments else ""
        git_read_only = git_subcommand in READ_ONLY_GIT_SUBCOMMANDS or (
            git_subcommand == "worktree"
            and len(git_arguments) > 1
            and git_arguments[1] == "list"
        )
        for index, word in enumerate(words):
            if word.startswith("--output="):
                paths.append(word.split("=", 1)[1])
                mutating_options = True
            elif word == "--output":
                mutating_options = True
                if index + 1 < len(words):
                    paths.append(words[index + 1])
    proven_read_only = (
        bool(command_names)
        and all(name in READ_ONLY_SHELL_COMMANDS for name in command_names)
        and git_read_only
        and not mutating_options
        and not unsafe_substitution
    )
    mutating = (
        bool(paths)
        or bool(commands & SHELL_MUTATORS)
        or git_mutation
        or inline_mutation
        or mutating_options
        or not proven_read_only
    )
    if not mutating:
        return False, []
    dynamic_target = unsafe_substitution
    for match in re.finditer(r"(?:^|\s)(?:[A-Za-z_][A-Za-z0-9_]*|of|dest|destination)=([^\s;&|]+)", command):
        candidate = match.group(1).strip("'\"")
        if candidate.startswith(("/", "./", "../")):
            paths.append(candidate)
        if "$" in candidate or "`" in candidate or candidate.startswith("~"):
            dynamic_target = True
    for quoted in re.finditer(r"['\"]((?:/|\.\.?/)[^'\"]+)['\"]", command):
        paths.append(quoted.group(1))
    if inline_mutation and re.search(r"\b(?:process\.env|os\.environ|getenv|ENV\[)|\$[{A-Za-z_(]", command):
        dynamic_target = True
    for word in words:
        candidate = word.strip("'\"")
        if "$" in candidate or "`" in candidate or candidate.startswith("~"):
            dynamic_target = True
        if (
            candidate.startswith(("/", "./", "../"))
            or "/../" in candidate
            or candidate.endswith("/..")
        ):
            paths.append(candidate)
    for index, word in enumerate(words):
        command_name = Path(word).name.lower()
        if command_name not in SHELL_MUTATORS:
            continue
        arguments: list[str] = []
        for candidate in words[index + 1 :]:
            if candidate in {"|", "&&", "||"}:
                break
            if not candidate.startswith("-"):
                arguments.append(candidate)
        if command_name in {"cp", "install", "ln", "mv"}:
            if arguments:
                paths.append(arguments[-1])
        else:
            paths.extend(arguments)
    if "rsync" in commands:
        arguments = [word for word in words[1:] if not word.startswith("-")]
        if arguments:
            paths.append(arguments[-1])
    if dynamic_target:
        return True, []
    return True, list(dict.fromkeys(paths))


def _is_agent_flow_launcher(command: str, cwd: Path, leader_root: Path, pinned_root: Path) -> bool:
    if re.search(r"(?<!\\)(?:\$\(|`|<\(|>\(|[;&|]|\d*>>?|&>)", command):
        return False
    if any(
        os.environ.get(name)
        for name in (
            "NODE_OPTIONS",
            "NODE_PATH",
            "BASH_ENV",
            "ENV",
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "DYLD_INSERT_LIBRARIES",
            "DYLD_LIBRARY_PATH",
        )
    ):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if (
        not words
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0])
        or Path(words[0]).name.lower() == "env"
    ):
        return False
    command_token = words[0]
    command_name = Path(command_token).name.lower()
    arguments = words[1:]
    if command_name not in {"agent-flow", "agent-flow-kit"} or not arguments:
        return False
    if arguments[0] not in {"status", "continue", "run", "gate", "gates", "architecture-lint"}:
        return False
    if arguments[:2] == ["run", "install"]:
        return False
    if "/" in command_token or "\\" in command_token:
        candidate = (cwd / command_token).resolve(strict=True) if not Path(command_token).is_absolute() else Path(command_token).resolve(strict=True)
    else:
        found = shutil.which(command_token)
        if not found:
            return False
        candidate = Path(found).resolve(strict=True)
    local_launcher = leader_root / ".agent-flow" / "bin" / "agent-flow"
    if local_launcher.exists() and candidate == local_launcher.resolve(strict=True):
        kit_path = leader_root / ".agent-flow" / "kit.json"
        try:
            runtime = leader_root / ".agent-flow" / "runtime" / "node"
            for managed_path in (local_launcher, kit_path, runtime):
                cursor = leader_root
                for part in managed_path.relative_to(leader_root).parts:
                    cursor /= part
                    if cursor.is_symlink():
                        return False
            kit = json.loads(kit_path.read_text(encoding="utf-8"))
            contract = kit["project_runtime_contract"]
            launcher_contract = contract["launcher"]
            runtime_contract = contract["runtime"]
            node_contract = contract["node"]
            python_contract = contract["python"]
            if (
                contract["version"] != 1
                or launcher_contract["path"] != ".agent-flow/bin/agent-flow"
                or runtime_contract["path"] != ".agent-flow/runtime/node"
                or Path(node_contract["path"]).resolve(strict=True) != Path(node_contract["path"])
                or not Path(python_contract["path"]).is_absolute()
                or Path(python_contract["path"]).resolve(strict=True) != Path(python_contract["resolved_path"])
            ):
                return False
            if local_launcher.is_symlink() or local_launcher.stat().st_nlink != 1:
                return False
            return (
                hashlib.sha256(local_launcher.read_bytes()).hexdigest() == launcher_contract["sha256"]
                and _runtime_tree_integrity(runtime) == runtime_contract["integrity"]
            )
        except (KeyError, OSError, TypeError, ValueError):
            return False
    if any(
        os.environ.get(name)
        for name in ("PYTHON", "PYTHON_EXECUTABLE", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")
    ):
        return False
    if any(candidate == root or root in candidate.parents for root in (leader_root, pinned_root)):
        return False
    runtime_root = leader_root / ".agent-flow" / "runtime" / "node"
    runtime = runtime_root / "bin" / "agent-flow-kit.mjs"
    kit_path = leader_root / ".agent-flow" / "kit.json"
    cursor = leader_root
    for part in runtime.relative_to(leader_root).parts:
        cursor /= part
        stat = cursor.lstat() if cursor.exists() else None
        if stat is None or cursor.is_symlink():
            return False
    if not runtime.is_file() or runtime.stat().st_nlink != 1 or not candidate.is_file() or candidate.is_symlink():
        return False
    try:
        contract = json.loads(kit_path.read_text(encoding="utf-8"))["project_runtime_contract"]
        node_path = Path(contract["node"]["path"])
        selected_node = shutil.which("node")
        return (
            contract["version"] == 1
            and contract["runtime"]["path"] == ".agent-flow/runtime/node"
            and _runtime_tree_integrity(runtime_root) == contract["runtime"]["integrity"]
            and selected_node is not None
            and Path(selected_node).resolve(strict=True) == node_path.resolve(strict=True)
            and hashlib.sha256(runtime.read_bytes()).digest() == hashlib.sha256(candidate.read_bytes()).digest()
            and _global_runtime_matches(candidate, runtime_root)
        )
    except (KeyError, OSError, TypeError, ValueError):
        return False


def _runtime_tree_integrity(root: Path) -> str:
    entries: list[dict[str, object]] = []

    def visit(current: Path, relative: str) -> None:
        stat = current.lstat()
        if current.is_symlink() or not current.is_dir():
            raise ValueError(f"unsafe runtime path: {current}")
        entries.append({"path": relative, "type": "directory", "mode": stat.st_mode & 0o777})
        for child in sorted(current.iterdir(), key=lambda path: path.name):
            child_relative = f"{relative}/{child.name}" if relative else child.name
            child_stat = child.lstat()
            if child.is_symlink():
                raise ValueError(f"unsafe runtime path: {child}")
            if child.is_dir():
                visit(child, child_relative)
            elif child.is_file():
                entries.append(
                    {
                        "path": child_relative,
                        "type": "file",
                        "mode": child_stat.st_mode & 0o777,
                        "sha256": hashlib.sha256(child.read_bytes()).hexdigest(),
                    }
                )
            else:
                raise ValueError(f"unsafe runtime path: {child}")

    visit(root, "")
    entries.sort(key=lambda entry: str(entry["path"]))
    payload = json.dumps(
        {"version": 1, "entries": entries},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _global_runtime_matches(candidate: Path, installed_runtime: Path) -> bool:
    package_root = candidate.parent.parent
    return all(
        _runtime_tree_integrity(package_root / relative)
        == _runtime_tree_integrity(installed_runtime / relative)
        for relative in RUNTIME_SOURCE_DIRECTORIES
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be an object")
        tool_name = _tool_name(payload)
        if tool_name not in WRITE_TOOLS and tool_name not in SHELL_TOOLS:
            return 0
        boundary_error, find_active, resolve_path = _load_boundary_module()
        cwd_value = payload.get("cwd")
        cwd = Path(cwd_value) if isinstance(cwd_value, str) and cwd_value else Path.cwd()
        active = find_active(_leader_root(cwd))
        if active is None:
            return 0
        tool_input = _tool_input(payload)
        if tool_name in SHELL_TOOLS:
            command = tool_input.get("command")
            if not isinstance(command, str) or not command.strip():
                raise boundary_error("write boundary rejected: shell tool did not declare a command")
            mutating, paths = _shell_mutation_paths(command)
            if not mutating:
                return 0
            pinned_root = Path(active.identity.workspace_root).resolve(strict=True)
            leader_root = _leader_root(cwd).resolve(strict=True)
            if _is_agent_flow_launcher(command, cwd, leader_root, pinned_root):
                return 0
            current_root = cwd.resolve(strict=True)
            if current_root != pinned_root:
                raise boundary_error(
                    "write boundary rejected: "
                    f"requested_path={command} resolved_path={current_root} "
                    f"pinned_workspace_root={pinned_root} "
                    f"host={payload.get('host') or 'unknown'} "
                    f"phase={payload.get('phase') or 'unknown'} "
                    "reason=mutating shell command must run from pinned workspace"
                )
        else:
            paths = _requested_paths(tool_input)
        if not paths:
            raise boundary_error("write boundary rejected: write tool did not declare a target path")
        host = str(payload.get("host") or os.environ.get("AGENT_FLOW_ACTIVE_HOST") or "unknown")
        phase = str(payload.get("phase") or "unknown")
        for requested in paths:
            resolve_path(active.identity, requested, host=host, phase=phase)
        return 0
    except Exception as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
