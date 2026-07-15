#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
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
EXPECTED_PROJECT_RUNTIME_CONTRACT_SHA256 = "__AGENT_FLOW_PROJECT_RUNTIME_CONTRACT_SHA256__"
EXPECTED_PYTHON_RUNTIME_INTEGRITY = "__AGENT_FLOW_PYTHON_RUNTIME_INTEGRITY__"
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
    "sed",
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


def _load_boundary_module(leader_root: Path) -> tuple[type[Exception], object, object, object]:
    runtime_root = leader_root / ".agent-flow" / "runtime" / "python"
    _verify_boundary_runtime(leader_root, runtime_root)
    if runtime_root.is_dir():
        sys.path.insert(0, str(runtime_root))
    try:
        from agent_flow.core.workspace_boundary import (
            WorkspaceBoundaryError,
            execution_identity_from_context,
            resolve_mutation_path,
            select_execution_workspace,
        )
    except ImportError as exc:
        raise RuntimeError("pinned workspace guard runtime is unavailable") from exc
    return (
        WorkspaceBoundaryError,
        execution_identity_from_context,
        select_execution_workspace,
        resolve_mutation_path,
    )


def _host_argument() -> str:
    try:
        index = sys.argv.index("--host")
    except ValueError:
        return ""
    return sys.argv[index + 1] if index + 1 < len(sys.argv) else ""


def _leader_root(cwd: Path) -> Path:
    resolved = cwd.resolve(strict=True)
    for candidate in (resolved, *resolved.parents):
        marker = candidate / ".git"
        if marker.is_dir():
            return candidate
        if marker.is_file():
            try:
                value = marker.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if value.startswith("gitdir:"):
                git_dir = Path(value.split(":", 1)[1].strip())
                if not git_dir.is_absolute():
                    git_dir = candidate / git_dir
                common = git_dir.resolve(strict=False)
                while common.name != ".git" and common != common.parent:
                    common = common.parent
                if common.name == ".git":
                    return common.parent
    return resolved


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


def _context_value(payload: dict[str, object], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _required_hook_execution_id(payload: dict[str, object]) -> str:
    execution_id = (
        os.environ.get("AGENT_FLOW_EXECUTION_ID", "").strip()
        or _context_value(payload, "execution_id", "thread_id", "session_id")
    )
    if not execution_id:
        raise ValueError(
            "execution_identity_missing: host hook did not declare a stable session identity"
        )
    return execution_id


def _forward_claude_execution_identity(
    payload: dict[str, object],
    tool_input: dict[str, object],
    command: str,
) -> None:
    session_id = _required_hook_execution_id(payload)
    agent_id = _context_value(payload, "agent_id")
    assignments = " ".join(
        (
            f"AGENT_FLOW_ACTIVE_HOST={shlex.quote('claude')}",
            f"AGENT_FLOW_EXECUTION_ID={shlex.quote(session_id)}",
            f"AGENT_FLOW_AGENT_ID={shlex.quote(agent_id)}",
        )
    )
    updated_input = dict(tool_input)
    updated_input["command"] = f"export {assignments}; {command}"
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "updatedInput": updated_input,
                }
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


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
                for match in re.finditer(
                    r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+)$",
                    patch,
                    re.MULTILINE,
                )
            )
        edits = value.get("edits")
        if isinstance(edits, list):
            visit(edits)

    visit(tool_input)
    return list(dict.fromkeys(paths))


def _split_shell_segments(command: str) -> list[str]:
    segments: list[str] = []
    start = 0
    quote = ""
    escaped = False
    index = 0
    while index < len(command):
        character = command[index]
        if escaped:
            escaped = False
        elif quote == "'":
            if character == "'":
                quote = ""
        elif quote == '"':
            if character == "\\":
                escaped = True
            elif character == '"':
                quote = ""
        elif character == "\\":
            escaped = True
        elif character in {"'", '"'}:
            quote = character
        else:
            operator_length = 0
            if character in {";", "\r", "\n", "|"}:
                operator_length = 2 if command[index : index + 2] in {"||"} else 1
            elif character == "&":
                if command[index : index + 2] == "&&":
                    operator_length = 2
                elif command[index : index + 2] != "&>" and (
                    index == 0 or command[index - 1] != ">"
                ):
                    operator_length = 1
            if operator_length:
                segment = command[start:index].strip()
                if segment:
                    segments.append(segment)
                index += operator_length
                start = index
                continue
        index += 1
    segment = command[start:].strip()
    if segment:
        segments.append(segment)
    return segments


def _shell_redirection_paths(segment: str) -> list[str]:
    lexer = shlex.shlex(segment, posix=True, punctuation_chars=";&|<>\r\n")
    lexer.whitespace = " \t"
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens = list(lexer)
    paths: list[str] = []
    punctuation = set(";&|<>")
    for index, token in enumerate(tokens[:-1]):
        if not token or not set(token) <= punctuation or ">" not in token:
            continue
        target = tokens[index + 1]
        if token == ">&" and target.isdigit():
            continue
        paths.append(target)
    return paths


def _sed_option_enables_in_place(argument: str) -> bool:
    if not argument.startswith("-") or argument.startswith("--"):
        return False
    for character in argument[1:]:
        if character == "i":
            return True
        if character in {"e", "f"} or not character.isalpha():
            return False
    return False


def _sed_in_place_paths(arguments: list[str]) -> tuple[bool, list[str]]:
    in_place = False
    explicit_script = False
    positional: list[str] = []
    index = 0
    options_finished = False
    while index < len(arguments):
        argument = arguments[index]
        if not options_finished and argument == "--":
            options_finished = True
        elif not options_finished and argument in {"-e", "--expression", "-f", "--file"}:
            explicit_script = True
            index += 1
        elif not options_finished and (
            argument.startswith(("--expression=", "--file="))
            or (argument.startswith(("-e", "-f")) and len(argument) > 2)
        ):
            explicit_script = True
        elif not options_finished and argument in {"-i", "--in-place"}:
            in_place = True
            if argument == "-i" and index + 1 < len(arguments) and arguments[index + 1] == "":
                index += 1
        elif not options_finished and argument.startswith(("-i", "--in-place=")):
            in_place = True
        elif not options_finished and argument.startswith("-"):
            in_place = in_place or _sed_option_enables_in_place(argument)
        else:
            positional.append(argument)
        index += 1
    if not in_place:
        return False, []
    if not explicit_script and positional:
        positional = positional[1:]
    return True, positional


def _perl_option_enables_in_place(argument: str) -> bool:
    if not argument.startswith("-") or argument.startswith("--"):
        return False
    for character in argument[1:]:
        if character == "i":
            return True
        if character in {"e", "E", "F", "I", "M", "m", "x"} or not character.isalpha():
            return False
    return False


def _perl_in_place_paths(arguments: list[str]) -> tuple[bool, list[str]]:
    in_place = False
    explicit_program = False
    positional: list[str] = []
    index = 0
    options_finished = False
    while index < len(arguments):
        argument = arguments[index]
        if not options_finished and argument == "--":
            options_finished = True
        elif not options_finished and argument in {"-e", "-E"}:
            explicit_program = True
            index += 1
        elif not options_finished and (
            (argument.startswith("-e") or argument.startswith("-E"))
            and len(argument) > 2
        ):
            explicit_program = True
        elif not options_finished and argument.startswith("-"):
            in_place = in_place or _perl_option_enables_in_place(argument)
            if argument in {"-F", "-I", "-M", "-m", "-x"}:
                index += 1
        else:
            positional.append(argument)
        index += 1
    if not in_place:
        return False, []
    if not explicit_program and positional:
        positional = positional[1:]
    return True, positional


def _shell_mutation_paths(command: str) -> tuple[bool, list[str], bool]:
    paths: list[str] = []
    dynamic_target = False
    mutating_segment = False
    for segment in _split_shell_segments(command):
        try:
            segment_words = shlex.split(segment)
            segment_redirections = _shell_redirection_paths(segment)
        except ValueError:
            return True, paths, True
        unwrapped = _unwrap_shell_command(segment_words)
        if not unwrapped:
            continue
        command_name = Path(unwrapped[0]).name.lower()
        arguments = unwrapped[1:]
        sed_mutating, sed_paths = _sed_in_place_paths(arguments) if command_name == "sed" else (False, [])
        perl_mutating, perl_paths = _perl_in_place_paths(arguments) if command_name == "perl" else (False, [])
        segment_unsafe = re.search(r"(?<!\\)(?:\$\(|`|<\(|>\()", segment) is not None
        segment_inline = _has_inline_mutation(segment)
        segment_mutating_options = (
            sed_mutating
            or perl_mutating
            or (
                command_name == "find"
                and bool(re.search(r"(?:^|\s)-(?:delete|exec|execdir)\b", segment))
            )
            or command_name in {"dd", "rsync"}
            or bool(segment_redirections)
        )
        git_command: tuple[str, str, tuple[str, ...]] | None = None
        if command_name == "git":
            git_command = _git_command(unwrapped)
            for index, word in enumerate(unwrapped[1:], start=1):
                if word.startswith("--output="):
                    paths.append(word.split("=", 1)[1])
                    segment_mutating_options = True
                elif word == "--output":
                    segment_mutating_options = True
                    if index + 1 < len(unwrapped):
                        paths.append(unwrapped[index + 1])
        segment_read_only = (
            command_name in READ_ONLY_SHELL_COMMANDS
            and (git_command is None or _git_command_is_read_only(git_command))
            and not segment_mutating_options
            and not segment_unsafe
            and not segment_inline
        )
        if segment_read_only:
            continue
        mutating_segment = True
        dynamic_target = dynamic_target or segment_unsafe
        segment_paths: list[str] = list(segment_redirections)
        positional = [argument for argument in arguments if not argument.startswith("-")]
        if git_command is not None:
            segment_paths.extend(git_command[2])
        elif sed_mutating:
            segment_paths.extend(sed_paths)
        elif perl_mutating:
            segment_paths.extend(perl_paths)
        elif command_name in {"cp", "install", "rsync"}:
            if positional:
                segment_paths.append(positional[-1])
        elif command_name in {"ln", "mv"}:
            segment_paths.extend(positional)
        elif command_name in SHELL_MUTATORS or segment_mutating_options:
            if command_name == "dd":
                segment_paths.extend(
                    argument.split("=", 1)[1]
                    for argument in arguments
                    if argument.startswith("of=") and len(argument.split("=", 1)) == 2
                )
            else:
                segment_paths.extend(positional)
        else:
            segment_paths.extend(
                argument
                for argument in arguments
                if _looks_like_path(argument.strip("'\""))
            )
        if segment_inline:
            segment_paths.extend(
                match.group(1)
                for match in re.finditer(r"['\"]((?:/|\.\.?/)[^'\"]+)['\"]", segment)
            )
            if re.search(
                r"\b(?:process\.env|os\.environ|getenv|ENV\[)|\$[{A-Za-z_(]",
                segment,
            ):
                dynamic_target = True
            if not segment_paths:
                dynamic_target = True
        for candidate in segment_paths:
            normalized = candidate.strip("'\"")
            if "$" in normalized or "`" in normalized or normalized.startswith("~"):
                dynamic_target = True
            paths.append(normalized)
    if not paths and not mutating_segment:
        return False, [], False
    if dynamic_target:
        return True, [], True
    return True, list(dict.fromkeys(paths)), False


def _has_inline_mutation(command: str) -> bool:
    return re.search(
        r"\b(write_text|write_bytes|unlink|mkdir|makedirs|remove|rename|replace)\b|"
        r"\b(writeFileSync|appendFileSync|createWriteStream|rmSync|unlinkSync|mkdirSync|renameSync)\b|"
        r"\b(File\.(?:write|delete|rename)|open)\s*\([^)]*,?\s*['\"][wax+]",
        command,
        re.IGNORECASE,
    ) is not None


def _looks_like_path(candidate: str) -> bool:
    return (
        candidate.startswith(("/", "./", "../"))
        or "/../" in candidate
        or candidate.endswith("/..")
    )


def _unwrap_shell_command(words: list[str]) -> list[str]:
    index = 0
    while index < len(words):
        word = words[index]
        if word == "command" or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", word):
            index += 1
            continue
        if Path(word).name.lower() != "env":
            break
        index += 1
        while index < len(words):
            option = words[index]
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", option):
                index += 1
            elif option in {"-i", "--ignore-environment"}:
                index += 1
            elif option in {"-u", "--unset"} and index + 1 < len(words):
                index += 2
            elif option.startswith("-"):
                index += 1
            else:
                break
    return words[index:]


def _git_command(words: list[str]) -> tuple[str, str, tuple[str, ...]]:
    index = 1
    targets: list[str] = []
    options_with_value = {
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--config-env",
        "--exec-path",
    }
    while index < len(words) and words[index].startswith("-") and words[index] != "--":
        option = words[index]
        index += 1
        if option in options_with_value and index < len(words):
            if option in {"-C", "--git-dir", "--work-tree"}:
                targets.append(words[index])
            index += 1
        else:
            for prefix in ("--git-dir=", "--work-tree=", "-C"):
                if option.startswith(prefix) and option != prefix:
                    targets.append(option[len(prefix) :])
                    break
    if index < len(words) and words[index] == "--":
        index += 1
    subcommand = words[index].lower() if index < len(words) else ""
    next_argument = words[index + 1].lower() if index + 1 < len(words) else ""
    return subcommand, next_argument, tuple(targets)


def _git_command_is_read_only(command: tuple[str, str, tuple[str, ...]]) -> bool:
    subcommand, next_argument, _targets = command
    return subcommand in READ_ONLY_GIT_SUBCOMMANDS or (
        subcommand == "worktree" and next_argument == "list"
    )


def _requests_agent_flow_launcher(command: str) -> bool:
    for segment in _split_shell_segments(command):
        try:
            words = shlex.split(segment)
        except ValueError:
            return True
        while words and (
            words[0] == "command"
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0])
        ):
            words = words[1:]
        if words and Path(words[0]).name.lower() == "env":
            words = words[1:]
            while words and (
                words[0].startswith("-")
                or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0])
            ):
                words = words[1:]
        if words and Path(words[0]).name.lower() in {"agent-flow", "agent-flow-kit"}:
            return True
    return False


def _is_agent_flow_launcher(command: str, cwd: Path, leader_root: Path, pinned_root: Path) -> bool:
    if re.search(r"(?<!\\)(?:\$\(|`|<\(|>\(|[\r\n;&|]|\d*>>?|&>)", command):
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
                contract["version"] != 3
                or kit["project_runtime_contract_commitment_version"] != 1
                or kit["project_runtime_contract_commitment"] != _project_runtime_contract_commitment(contract)
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
                and _verify_executable_contract(node_contract)
                and _verify_executable_contract(contract["git"])
                and _verify_executable_contract(python_contract, "resolved_path")
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
        kit = json.loads(kit_path.read_text(encoding="utf-8"))
        contract = kit["project_runtime_contract"]
        node_path = Path(contract["node"]["path"])
        selected_node = shutil.which("node")
        return (
            contract["version"] == 3
            and kit["project_runtime_contract_commitment_version"] == 1
            and kit["project_runtime_contract_commitment"] == _project_runtime_contract_commitment(contract)
            and contract["runtime"]["path"] == ".agent-flow/runtime/node"
            and _runtime_tree_integrity(runtime_root) == contract["runtime"]["integrity"]
            and node_path.is_absolute()
            and node_path.resolve(strict=True) == node_path
            and selected_node is not None
            and Path(selected_node).resolve(strict=True) == node_path
            and _verify_executable_contract(contract["node"])
            and _verify_executable_contract(contract["git"])
            and _verify_executable_contract(contract["python"], "resolved_path")
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


def _project_runtime_contract_commitment(contract: dict[str, object]) -> str:
    payload = [
        contract["version"],
        contract["launcher"]["path"],
        contract["launcher"]["sha256"],
        contract["node"]["path"],
        contract["node"]["sha256"],
        contract["node"]["device"],
        contract["node"]["inode"],
        contract["node"]["links"],
        contract["node"]["mode"],
        contract["git"]["path"],
        contract["git"]["sha256"],
        contract["git"]["device"],
        contract["git"]["inode"],
        contract["git"]["links"],
        contract["git"]["mode"],
        contract["python"]["path"],
        contract["python"]["resolved_path"],
        contract["python"]["sha256"],
        contract["python"]["device"],
        contract["python"]["inode"],
        contract["python"]["links"],
        contract["python"]["mode"],
        contract["runtime"]["path"],
        contract["runtime"]["integrity"],
        contract["python_runtime"]["path"],
        contract["python_runtime"]["integrity"],
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _verify_executable_contract(contract: dict[str, object], path_key: str = "path") -> bool:
    candidate = Path(str(contract[path_key]))
    resolved = candidate.resolve(strict=True)
    metadata = resolved.lstat()
    return (
        candidate.is_absolute()
        and not resolved.is_symlink()
        and resolved.is_file()
        and metadata.st_mode & 0o022 == 0
        and hashlib.sha256(resolved.read_bytes()).hexdigest() == contract["sha256"]
        and str(metadata.st_dev) == contract["device"]
        and str(metadata.st_nlink) == contract["links"]
        and (str(metadata.st_nlink) != "1" or str(metadata.st_ino) == contract["inode"])
        and stat.S_IMODE(metadata.st_mode) == contract["mode"]
    )


def _legacy_runtime_tree_hash(root: Path) -> str:
    digest = hashlib.sha256()

    def visit(directory: Path) -> None:
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            metadata = child.lstat()
            if child.is_symlink():
                raise ValueError("legacy runtime contains a symlink")
            if child.is_dir():
                visit(child)
            elif child.is_file() and metadata.st_nlink == 1:
                digest.update(child.relative_to(root).as_posix().encode())
                digest.update(b"\0")
                digest.update(child.read_bytes())
                digest.update(b"\0")
            else:
                raise ValueError("legacy runtime contains an unsafe entry")

    if root.is_symlink() or not root.is_dir():
        raise ValueError("legacy runtime root is unsafe")
    visit(root)
    return digest.hexdigest()


def _verify_legacy_boundary_runtime(leader_root: Path, kit: dict[str, object], contract: dict[str, object]) -> None:
    normalized = {
        "version": contract["version"],
        "launcher": contract["launcher"],
        "node_runtime": contract["node_runtime"],
        "python_runtime": contract["python_runtime"],
    }
    commitment = hashlib.sha256(
        json.dumps(
            {"version": 2, "contract": normalized},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    launcher = leader_root / str(contract["launcher"]["path"])
    node_runtime = leader_root / str(contract["node_runtime"]["root"])
    python_runtime = leader_root / str(contract["python_runtime"]["root"])
    if (
        contract["version"] != 2
        or kit["project_runtime_contract_commitment_version"] != 2
        or kit["project_runtime_contract_commitment"] != commitment
        or hashlib.sha256(launcher.read_bytes()).hexdigest() != contract["launcher"]["sha256"]
        or _legacy_runtime_tree_hash(node_runtime) != contract["node_runtime"]["tree_hash"]
        or _legacy_runtime_tree_hash(python_runtime) != contract["python_runtime"]["tree_hash"]
    ):
        raise ValueError("legacy project runtime contract is invalid")
    if Path("/usr/bin/git").is_file():
        os.environ["AGENT_FLOW_GIT_EXECUTABLE"] = "/usr/bin/git"


def _verify_boundary_runtime(leader_root: Path, runtime_root: Path) -> None:
    kit_path = leader_root / ".agent-flow" / "kit.json"
    try:
        kit = json.loads(kit_path.read_text(encoding="utf-8"))
        contract = kit["project_runtime_contract"]
        embedded = EXPECTED_PROJECT_RUNTIME_CONTRACT_SHA256
        embedded_python = EXPECTED_PYTHON_RUNTIME_INTEGRITY
        embedded_authority = not embedded.startswith("__AGENT_FLOW_")
        if not embedded_authority and "node_runtime" in contract:
            _verify_legacy_boundary_runtime(leader_root, kit, contract)
            return
        commitment = _project_runtime_contract_commitment(contract)
        if (
            contract["version"] != 3
            or kit["project_runtime_contract_commitment_version"] != 1
            or kit["project_runtime_contract_commitment"] != commitment
            or contract["python_runtime"]["path"] != ".agent-flow/runtime/python"
            or _runtime_tree_integrity(runtime_root) != contract["python_runtime"]["integrity"]
            or not _verify_executable_contract(contract["node"])
            or not _verify_executable_contract(contract["git"])
            or not _verify_executable_contract(contract["python"], "resolved_path")
        ):
            raise ValueError("project runtime contract is invalid")
        if embedded_authority and embedded != commitment:
            raise ValueError("project runtime contract differs from verifier authority")
        if not embedded_python.startswith("__AGENT_FLOW_") and embedded_python != contract["python_runtime"]["integrity"]:
            raise ValueError("Python runtime differs from verifier authority")
        git_path = Path(contract["git"]["path"])
        if not git_path.is_absolute() or git_path.resolve(strict=True) != git_path:
            raise ValueError("project git runtime is invalid")
        os.environ["AGENT_FLOW_GIT_EXECUTABLE"] = str(git_path)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("pinned workspace guard runtime authentication failed") from exc


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be an object")
        tool_name = _tool_name(payload)
        if tool_name not in WRITE_TOOLS and tool_name not in SHELL_TOOLS:
            return 0
        cwd_value = payload.get("cwd")
        cwd = Path(cwd_value) if isinstance(cwd_value, str) and cwd_value else Path.cwd()
        leader_root = _leader_root(cwd)
        if not (leader_root / ".git").exists():
            return 0
        host = str(
            payload.get("host")
            or _host_argument()
            or os.environ.get("AGENT_FLOW_ACTIVE_HOST")
            or "unknown"
        ).strip().lower()
        tool_input = _tool_input(payload)
        command = ""
        paths: list[str] = []
        unresolved_shell_target = False
        if tool_name in SHELL_TOOLS:
            command_value = tool_input.get("command")
            if not isinstance(command_value, str) or not command_value.strip():
                raise ValueError("write boundary rejected: shell tool did not declare a command")
            command = command_value
            mutating, paths, unresolved_shell_target = _shell_mutation_paths(command)
            if not mutating:
                return 0
            if _is_agent_flow_launcher(command, cwd, leader_root, leader_root):
                if host == "claude":
                    _forward_claude_execution_identity(payload, tool_input, command)
                elif host == "omp":
                    _required_hook_execution_id(payload)
                return 0
            if _requests_agent_flow_launcher(command):
                raise ValueError("write boundary rejected: agent-flow launcher is not trusted")
        boundary_error, resolve_execution, select_workspace, resolve_path = _load_boundary_module(leader_root)
        execution = resolve_execution(payload, os.environ, host_hint=host)
        active = select_workspace(leader_root, execution)
        if active is None:
            return 0
        if tool_name in SHELL_TOOLS:
            pinned_root = Path(active.identity.workspace_root).resolve(strict=True)
            leader_root = _leader_root(cwd).resolve(strict=True)
            if _is_agent_flow_launcher(command, cwd, leader_root, pinned_root):
                if host == "claude":
                    _forward_claude_execution_identity(payload, tool_input, command)
                elif host == "omp":
                    _required_hook_execution_id(payload)
                return 0
            current_root = cwd.resolve(strict=True)
            if current_root != pinned_root and pinned_root not in current_root.parents:
                raise boundary_error(
                    "write boundary rejected: "
                    f"requested_path={command} resolved_path={current_root} "
                    f"pinned_workspace_root={pinned_root} "
                    f"host={host} "
                    f"phase={payload.get('phase') or 'unknown'} "
                    "reason_code=mutation_cwd_not_pinned "
                    "reason=mutating shell command must run from pinned workspace"
                )
        else:
            paths = _requested_paths(tool_input)
        if unresolved_shell_target:
            raise boundary_error(
                "write boundary rejected: shell command has an unresolved mutation target"
            )
        if tool_name in SHELL_TOOLS and not paths:
            return 0
        if not paths:
            raise boundary_error("write boundary rejected: write tool did not declare a target path")
        phase = str(payload.get("phase") or "unknown")
        for requested in paths:
            resolve_path(active.identity, requested, base_dir=cwd, host=host, phase=phase)
        return 0
    except Exception as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
