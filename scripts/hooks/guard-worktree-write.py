#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shlex
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
READ_ONLY_SHELL_COMMANDS = {
    "basename",
    "cat",
    "command",
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
    proven_read_only = (
        bool(command_names)
        and all(name in READ_ONLY_SHELL_COMMANDS for name in command_names)
        and git_read_only
        and not mutating_options
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
    dynamic_target = False
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
    if not paths:
        paths.append(".")
    return True, list(dict.fromkeys(paths))


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
