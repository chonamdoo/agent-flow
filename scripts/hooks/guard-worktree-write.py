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


sys.dont_write_bytecode = True


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
    "echo",
    "env",
    "false",
    "git",
    "grep",
    "head",
    "ls",
    "pwd",
    "printf",
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
SHELL_CWD_COMMANDS = {".", "cd", "chdir", "popd", "pushd", "source"}
NESTED_SHELL_COMMANDS = {"bash", "dash", "ksh", "sh", "zsh"}
SCRIPT_INTERPRETERS = {
    "node",
    "nodejs",
    "python",
    "python3",
    "python3.12",
    "python3.13",
    "python3.14",
}
SHELL_CONTROL_WORDS = {
    "!",
    "case",
    "do",
    "done",
    "elif",
    "else",
    "esac",
    "fi",
    "for",
    "function",
    "if",
    "in",
    "select",
    "then",
    "time",
    "until",
    "while",
}
MAX_SHELL_STATES = 64
MAX_SHELL_PATHS = 512
POSIX_SPECIAL_BUILTINS = {
    ".",
    ":",
    "break",
    "continue",
    "eval",
    "exec",
    "exit",
    "export",
    "readonly",
    "return",
    "set",
    "shift",
    "times",
    "trap",
    "unset",
}
GRADLE_PATH_OPTIONS = {
    "-I",
    "-b",
    "-c",
    "-g",
    "-p",
    "--build-file",
    "--gradle-user-home",
    "--include-build",
    "--init-script",
    "--project-cache-dir",
    "--project-dir",
    "--settings-file",
}
GRADLE_COMPACT_PATH_OPTIONS = ("-I", "-b", "-c", "-g", "-p")
MANAGED_CONTEXT_FILENAMES = {"agents.md", "claude.md", ".gitignore"}
XD_FS_MUTATORS = {"ast_edit"}
GRADLE_OPTION_ENVIRONMENT = (
    "GRADLE_OPTS",
    "JDK_JAVA_OPTIONS",
    "JAVA_TOOL_OPTIONS",
    "_JAVA_OPTIONS",
    "JAVA_OPTS",
)
MANAGED_MARKER_START = "<!-- agent-flow:start -->"
MANAGED_MARKER_END = "<!-- agent-flow:end -->"


def _load_boundary_module(leader_root: Path) -> tuple[type[Exception], object, object, object, object, object]:
    runtime_root = leader_root / ".agent-flow" / "runtime" / "python"
    _verify_boundary_runtime(leader_root, runtime_root)
    if runtime_root.is_dir():
        sys.path.insert(0, str(runtime_root))
    try:
        from agent_flow.core.workspace_boundary import (
            WorkspaceBoundaryError,
            execution_binding_exists,
            execution_identity_from_context,
            resolve_mutation_path,
            resolve_execution_finalizer_workspace,
            select_execution_workspace,
        )
    except ImportError as exc:
        raise RuntimeError("pinned workspace guard runtime is unavailable") from exc
    return (
        WorkspaceBoundaryError,
        execution_identity_from_context,
        select_execution_workspace,
        resolve_mutation_path,
        resolve_execution_finalizer_workspace,
        execution_binding_exists,
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
        native_patch = value.get("input")
        if isinstance(native_patch, str):
            paths.extend(_native_patch_paths(native_patch))
        edits = value.get("edits")
        if isinstance(edits, list):
            visit(edits)

    visit(tool_input)
    return list(dict.fromkeys(paths))


def _managed_marker_block(content: str) -> str | None:
    lines = content.splitlines(keepends=True)
    starts = [
        index
        for index, line in enumerate(lines)
        if line.rstrip("\r\n") == MANAGED_MARKER_START
    ]
    ends = [
        index
        for index, line in enumerate(lines)
        if line.rstrip("\r\n") == MANAGED_MARKER_END
    ]
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise ValueError("write boundary rejected: managed marker block is malformed")
    return "".join(lines[starts[0] : ends[0] + 1])


def _string_value(mapping: dict[str, object], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str):
            return value
    return None


def _shell_tool_cwd(tool_input: dict[str, object], base: Path) -> Path | None:
    for key in ("cwd", "workdir", "working_directory", "workingDirectory"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = base / candidate
            return candidate
    return None


def _native_patch_paths(text: str) -> list[str]:
    paths: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        header = re.fullmatch(r"\[([^#\]\r\n]+)#[0-9A-Fa-f]+\]", stripped)
        if header:
            paths.append(header.group(1).strip())
            continue
        move = re.fullmatch(r"MV\s+(.+)", stripped)
        if move:
            destination = move.group(1).strip()
            try:
                tokens = shlex.split(destination)
            except ValueError:
                tokens = [destination]
            if tokens:
                paths.append(tokens[0])
    return paths


def _xd_tool_targets(xd_tool: str, tool_input: dict[str, object]) -> list[str]:
    if xd_tool != "ast_edit":
        return []
    content = _string_value(tool_input, "content", "input")
    if content is None:
        return []
    try:
        arguments = json.loads(content)
    except (ValueError, TypeError):
        return []
    targets = arguments.get("paths") if isinstance(arguments, dict) else None
    if not isinstance(targets, list):
        return []
    paths = [item for item in targets if isinstance(item, str) and item]
    if any(re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", path) for path in paths):
        raise ValueError(
            "write boundary rejected: reason_code=target_uri_not_supported "
            "xd:// filesystem mutator targets must be local filesystem paths"
        )
    return paths


def _apply_declared_edit(content: str, edit: dict[str, object]) -> str:
    old = _string_value(edit, "old_string", "oldStr")
    new = _string_value(edit, "new_string", "newStr")
    if old is None or new is None or not old:
        raise ValueError(
            "write boundary rejected: managed marker block edit cannot be verified"
        )
    occurrences = content.count(old)
    replace_all = edit.get("replace_all") is True or edit.get("replaceAll") is True
    if occurrences == 0 or (not replace_all and occurrences != 1):
        raise ValueError(
            "write boundary rejected: managed marker block edit is ambiguous"
        )
    return content.replace(old, new) if replace_all else content.replace(old, new, 1)


def _resolve_declared_path(value: str, cwd: Path) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    return candidate.resolve(strict=False)


def _apply_patch_hunks(content: str, body: list[str]) -> str:
    index = 0
    applied = False
    while index < len(body):
        line = body[index]
        if line.startswith("*** Move to:"):
            raise ValueError(
                "write boundary rejected: managed marker block file cannot be moved"
            )
        if not line.startswith("@@"):
            if line.strip() and line.strip() != "*** End of File":
                raise ValueError(
                    "write boundary rejected: managed marker block patch cannot be verified"
                )
            index += 1
            continue
        index += 1
        old_lines: list[str] = []
        new_lines: list[str] = []
        while index < len(body) and not body[index].startswith("@@"):
            patch_line = body[index]
            if patch_line.strip() == "*** End of File":
                index += 1
                break
            if patch_line.startswith("*** "):
                raise ValueError(
                    "write boundary rejected: managed marker block patch cannot be verified"
                )
            if patch_line in {"\n", "\r\n"}:
                old_lines.append(patch_line)
                new_lines.append(patch_line)
                index += 1
                continue
            if not patch_line or patch_line[0] not in {" ", "+", "-"}:
                raise ValueError(
                    "write boundary rejected: managed marker block patch cannot be verified"
                )
            value = patch_line[1:]
            if patch_line[0] in {" ", "-"}:
                old_lines.append(value)
            if patch_line[0] in {" ", "+"}:
                new_lines.append(value)
            index += 1
        old = "".join(old_lines)
        new = "".join(new_lines)
        if not old or content.count(old) != 1:
            raise ValueError(
                "write boundary rejected: managed marker block patch is ambiguous"
            )
        content = content.replace(old, new, 1)
        applied = True
    if not applied:
        raise ValueError(
            "write boundary rejected: managed marker block patch cannot be verified"
        )
    return content


def _content_after_patch(
    content: str,
    patch: str,
    target: Path,
    cwd: Path,
) -> str:
    lines = patch.splitlines(keepends=True)
    header = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+?)\r?\n?$")
    index = 0
    matched = False
    while index < len(lines):
        match = header.match(lines[index])
        if match is None:
            index += 1
            continue
        operation, declared = match.groups()
        index += 1
        body_start = index
        while index < len(lines) and header.match(lines[index]) is None and not lines[index].startswith("*** End Patch"):
            index += 1
        body = lines[body_start:index]
        declared_target = _resolve_declared_path(declared.strip(), cwd)
        move_targets = [
            _resolve_declared_path(line.split(":", 1)[1].strip(), cwd)
            for line in body
            if line.startswith("*** Move to:")
        ]
        if declared_target != target and target not in move_targets:
            continue
        matched = True
        if operation != "Update" or move_targets:
            raise ValueError(
                "write boundary rejected: managed marker block file cannot be replaced, deleted, or moved"
            )
        content = _apply_patch_hunks(content, body)
    if not matched:
        raise ValueError(
            "write boundary rejected: managed marker block patch target cannot be verified"
        )
    return content


def _verify_managed_marker_integrity(
    tool_name: str,
    tool_input: dict[str, object],
    requested_path: str,
    target: Path,
    cwd: Path,
) -> None:
    if (
        Path(requested_path).name.casefold() not in MANAGED_CONTEXT_FILENAMES
        and target.name.casefold() not in MANAGED_CONTEXT_FILENAMES
    ):
        return
    try:
        current = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except (OSError, UnicodeError) as exc:
        raise ValueError(
            "write boundary rejected: managed marker block file cannot be read"
        ) from exc
    original_block = _managed_marker_block(current)
    if original_block is None:
        return
    if tool_name == "apply_patch":
        patch = _string_value(tool_input, "patch", "command")
        if patch is None:
            raise ValueError(
                "write boundary rejected: managed marker block patch is missing"
            )
        proposed = _content_after_patch(current, patch, target, cwd)
    elif tool_name == "write":
        proposed = _string_value(tool_input, "content")
        if proposed is None:
            raise ValueError(
                "write boundary rejected: managed marker block write cannot be verified"
            )
    elif tool_name in {"edit", "multiedit", "multi_edit"}:
        edits = tool_input.get("edits")
        if isinstance(edits, list):
            proposed = current
            outer_path = _string_value(
                tool_input,
                "file_path",
                "filePath",
                "filename",
                "path",
            )
            outer_target = (
                _resolve_declared_path(outer_path, cwd)
                if outer_path is not None
                else None
            )
            applied = False
            for edit in edits:
                if not isinstance(edit, dict):
                    raise ValueError(
                        "write boundary rejected: managed marker block edit cannot be verified"
                    )
                edit_path = _string_value(
                    edit,
                    "file_path",
                    "filePath",
                    "filename",
                    "path",
                )
                if edit_path is not None:
                    if _resolve_declared_path(edit_path, cwd) != target:
                        continue
                elif outer_target != target:
                    raise ValueError(
                        "write boundary rejected: managed marker block edit target cannot be verified"
                    )
                proposed = _apply_declared_edit(proposed, edit)
                applied = True
            if not applied:
                raise ValueError(
                    "write boundary rejected: managed marker block edit target cannot be verified"
                )
        else:
            proposed = _apply_declared_edit(current, tool_input)
    else:
        raise ValueError(
            "write boundary rejected: managed marker block mutation cannot be verified"
        )
    if _managed_marker_block(proposed) != original_block:
        raise ValueError(
            "write boundary rejected: managed marker block is immutable"
        )


def _split_shell_commands(command: str) -> list[tuple[str, str]]:
    commands: list[tuple[str, str]] = []
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
            operator = ""
            if command[index : index + 2] in {"||", "|&", "\r\n"}:
                operator = command[index : index + 2]
            elif character == "|" and index > 0 and command[index - 1] == ">":
                operator = ""
            elif character in {";", "\r", "\n", "|"}:
                operator = character
            elif character == "&":
                if command[index : index + 2] == "&&":
                    operator = "&&"
                elif command[index : index + 2] != "&>" and (
                    index == 0 or command[index - 1] != ">"
                ):
                    operator = "&"
            if operator:
                segment = command[start:index].strip()
                if segment:
                    commands.append((segment, operator))
                index += len(operator)
                start = index
                continue
        index += 1
    segment = command[start:].strip()
    if segment:
        commands.append((segment, ""))
    return commands


def _split_shell_segments(command: str) -> list[str]:
    return [segment for segment, _operator in _split_shell_commands(command)]


def _has_active_shell_substitution(command: str) -> bool:
    command = re.sub(r"\\\r?\n", "", command)
    if _has_executable_zsh_glob(command) or _has_active_brace_expansion(command):
        return True
    quote = ""
    escaped = False
    index = 0
    while index < len(command):
        character = command[index]
        if quote == "'":
            if character == "'":
                quote = ""
            index += 1
            continue
        if escaped:
            escaped = False
            index += 1
            continue
        if character == "\\":
            escaped = True
            index += 1
            continue
        if character == "'" and not quote:
            quote = "'"
        elif character == '"':
            quote = "" if quote == '"' else '"'
        elif character == "`":
            return True
        elif character == "$" and command[index : index + 2] == "$(":
            return True
        elif (
            not quote
            and character in {"<", ">", "="}
            and command[index : index + 2].endswith("(")
        ):
            return True
        index += 1
    return False


def _has_executable_zsh_glob(command: str) -> bool:
    masked: list[str] = []
    quote = ""
    escaped = False
    for character in command:
        if escaped:
            masked.append(" ")
            escaped = False
            continue
        if character == "\\" and quote != "'":
            masked.append(" ")
            escaped = True
            continue
        if quote:
            masked.append(" ")
            if character == quote:
                quote = ""
            continue
        if character in {"'", '"'}:
            masked.append(" ")
            quote = character
            continue
        masked.append(character)
    return re.search(
        r"(?:[*?+@!]|\[[^\]\n]*\])\([^\n)]*?e(?:[:{])",
        "".join(masked),
    ) is not None


def _has_active_brace_expansion(command: str) -> bool:
    masked: list[str] = []
    quote = ""
    escaped = False
    for character in command:
        if escaped:
            masked.append(" ")
            escaped = False
            continue
        if character == "\\" and quote != "'":
            masked.append(" ")
            escaped = True
            continue
        if quote:
            masked.append(" ")
            if character == quote:
                quote = ""
            continue
        if character in {"'", '"'}:
            masked.append(" ")
            quote = character
            continue
        masked.append(character)
    return re.search(
        r"(?<!\$)\{[^{}\n]*(?:,|\.\.)[^{}\n]*\}",
        "".join(masked),
    ) is not None


def _has_shell_parameter_expansion(command: str) -> bool:
    quote = ""
    escaped = False
    for index, character in enumerate(command):
        if quote == "'":
            if character == "'":
                quote = ""
            continue
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == "'" and not quote:
            quote = "'"
            continue
        if character == '"':
            quote = "" if quote == '"' else '"'
            continue
        if character != "$":
            continue
        following = command[index + 1 : index + 2]
        if following == "{" or re.match(r"[A-Za-z_0-9*@#?$!_-]", following):
            return True
    return False


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


def _without_shell_redirections(words: list[str]) -> list[str]:
    command_words: list[str] = []
    skip_target = False
    for word in words:
        if skip_target:
            skip_target = False
            continue
        match = re.fullmatch(r"\d*(?:&>|<>|>&|<&|>\||>>?|<)(.*)", word)
        if match is None:
            command_words.append(word)
            continue
        if command_words and command_words[-1].isdigit():
            command_words.pop()
        if not match.group(1):
            skip_target = True
    return command_words


def _shell_segment_words(segment: str) -> list[str]:
    lexer = shlex.shlex(segment, posix=True, punctuation_chars="(){}<>&")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _strip_shell_control_prefix(words: list[str]) -> list[str]:
    if words and words[0] == "case":
        return words[words.index(")") + 1 :] if ")" in words else []
    if len(words) > 1 and words[1] == "()" and "{" in words:
        return words[words.index("{") + 1 :]
    if words and words[0] == "function" and "{" in words:
        return words[words.index("{") + 1 :]
    while words and (
        words[0] in {"(", ")", "{", "}"} or words[0] in SHELL_CONTROL_WORDS
    ):
        words = words[1:]
    return words


def _shell_assignments(
    words: list[str],
    unwrapped: list[str],
) -> dict[str, str]:
    assignments: dict[str, str] = {}
    prefix_length = len(words) - len(unwrapped)
    for word in words[:prefix_length]:
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)(\+?=)(.*)", word)
        if match is not None:
            name, operator, value = match.groups()
            assignments[name] = (
                f"{os.environ.get(name, '')}{value}" if operator == "+=" else value
            )
    return assignments


def _assignment_operations(words: list[str]) -> list[tuple[str, str, str]]:
    operations: list[tuple[str, str, str]] = []
    for word in words:
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)(\+?=)(.*)", word)
        if match is not None:
            operations.append(match.groups())
    return operations


def _updated_environment_values(
    current: dict[str, set[str]],
    operations: list[tuple[str, str, str]],
    *,
    retain_previous: bool,
) -> dict[str, set[str]]:
    updated = {name: set(values) for name, values in current.items()}
    for name, operator, value in operations:
        if name not in updated:
            continue
        previous = updated[name] or {""}
        values = (
            {f"{prefix}{value}" for prefix in previous}
            if operator == "+="
            else {value}
        )
        updated[name] = previous | values if retain_previous else values
    return updated


def _env_chdir_target(words: list[str], unwrapped: list[str]) -> tuple[bool, str | None]:
    prefix = words[: len(words) - len(unwrapped)]
    if not any(Path(word).name.lower() == "env" for word in prefix):
        return False, None
    for index, word in enumerate(prefix):
        if word in {"-C", "--chdir"}:
            return True, prefix[index + 1] if index + 1 < len(prefix) else None
        if word.startswith("--chdir="):
            return True, word.split("=", 1)[1]
        if word.startswith("-C") and word != "-C":
            return True, word[2:]
    return False, None


def _env_split_string(words: list[str], unwrapped: list[str]) -> str | None:
    prefix = words[: len(words) - len(unwrapped)]
    if not any(Path(word).name.lower() == "env" for word in prefix):
        return None
    for index, word in enumerate(prefix):
        if word in {"-S", "--split-string"}:
            return prefix[index + 1] if index + 1 < len(prefix) else ""
        if word.startswith("--split-string="):
            return word.split("=", 1)[1]
    return None


def _uses_external_command_wrapper(words: list[str], unwrapped: list[str]) -> bool:
    prefix = words[: len(words) - len(unwrapped)]
    return any(word == "exec" or Path(word).name.lower() == "env" for word in prefix)


def _shell_c_command(arguments: list[str]) -> str | None:
    options_with_values = {"+O", "+o", "-O", "-o", "--init-file", "--rcfile"}
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            return None
        if argument in options_with_values:
            index += 2
            continue
        if argument.startswith(("+O", "+o", "-O", "-o")) and len(argument) > 2:
            index += 1
            continue
        if (
            argument.startswith("-")
            and not argument.startswith("--")
            and "o" in argument[1:]
        ):
            index += 2
            continue
        if argument == "-c" or (
            argument.startswith("-")
            and not argument.startswith("--")
            and "c" in argument[1:]
        ):
            return arguments[index + 1] if index + 1 < len(arguments) else None
        if not argument.startswith("-"):
            return None
        index += 1
    return None


def _full_shell_group(command: str) -> str | None:
    stripped = command.strip()
    if len(stripped) < 2:
        return None
    opening = stripped[0]
    closing = ")" if opening == "(" else "}" if opening == "{" else ""
    if not closing or stripped[-1] != closing:
        return None
    if opening == "{" and len(stripped) > 1 and not stripped[1].isspace():
        return None
    quote = ""
    escaped = False
    depth = 0
    for index, character in enumerate(stripped):
        if escaped:
            escaped = False
            continue
        if quote == "'":
            if character == "'":
                quote = ""
            continue
        if quote == '"':
            if character == "\\":
                escaped = True
            elif character == '"':
                quote = ""
            continue
        if character == "\\":
            escaped = True
        elif character in {"'", '"'}:
            quote = character
        elif character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0 and index != len(stripped) - 1:
                return None
    return stripped[1:-1].strip() if depth == 0 and not quote else None


def _shell_command_name(token: str) -> str:
    return token.lower() if token in {".", ":"} else Path(token).name.lower()


def _compound_shell_command_words(command: str) -> list[list[str]]:
    commands: list[list[str]] = []
    for segment, _operator in _split_shell_commands(command):
        try:
            lexer = shlex.shlex(segment, posix=True, punctuation_chars="(){}<>&")
            lexer.whitespace_split = True
            lexer.commenters = ""
            words = _without_shell_redirections(list(lexer))
        except ValueError:
            continue
        words = _strip_shell_control_prefix(words)
        unwrapped = _unwrap_shell_command(words)
        if unwrapped:
            commands.append(unwrapped)
    return commands


def _unsupported_compound_shell_mutation(command: str) -> bool:
    unquoted = []
    quote = ""
    escaped = False
    for character in command:
        if escaped:
            unquoted.append(" ")
            escaped = False
        elif quote == "'":
            unquoted.append(" ")
            if character == "'":
                quote = ""
        elif quote == '"':
            unquoted.append(" ")
            if character == "\\":
                escaped = True
            elif character == '"':
                quote = ""
        elif character == "\\":
            unquoted.append(" ")
            escaped = True
        elif character in {"'", '"'}:
            unquoted.append(" ")
            quote = character
        else:
            unquoted.append(character)
    shell_text = "".join(unquoted)
    has_control = bool(
        re.search(
            r"[()]|(?:^|[;&|\r\n]\s*)\{(?=\s)|"
            r"(?:^|[;&|\r\n]\s*)(?:!(?=\s)|(?:if|then|elif|else|fi|for|while|"
            r"until|case|esac|do|done|function|select|time)\b)",
            shell_text,
        )
    )
    if not has_control:
        return False
    command_words = _compound_shell_command_words(command)
    names = [_shell_command_name(words[0]) for words in command_words]
    cwd_word = r"(?:cd|chdir|popd|pushd|source)"
    changes_cwd = any(name in SHELL_CWD_COMMANDS for name in names) or bool(
        re.search(
            rf"(?:^|[\s;&|<>(){{}}])(?:{cwd_word}|\.)(?=$|[\s;&|<>(){{}}])",
            shell_text,
        )
    )
    env_changes_cwd = bool(
        re.search(
            r"(?:^|[\s;&|(){}])(?:[^\s;&|(){}]*/)?env\b[^;&|\r\n]*"
            r"(?:\s-C(?:\s|\S)|\s--chdir(?:=|\s))",
            shell_text,
        )
    )
    changes_cwd = changes_cwd or env_changes_cwd
    nested_word = "|".join((*sorted(NESTED_SHELL_COMMANDS), "eval"))
    nests_commands = any(
        name in NESTED_SHELL_COMMANDS or name == "eval" for name in names
    ) or bool(
        re.search(
            rf"(?:^|[\s;&|<>(){{}}])(?:{nested_word})(?=$|[\s;&|<>(){{}}])",
            shell_text,
        )
    )
    env_splits_command = bool(
        re.search(
            r"(?:^|[\s;&|(){}])(?:[^\s;&|(){}]*/)?env\b[^;&|\r\n]*"
            r"(?:\s-S(?:\s|\S)|\s--split-string(?:=|\s))",
            shell_text,
        )
    )
    nests_commands = nests_commands or env_splits_command
    has_negation = bool(
        re.search(r"(?:^|[;&|\r\n]\s*)!(?=\s)", shell_text)
    )
    if not changes_cwd and not nests_commands and not has_negation:
        return False
    for words, name in zip(command_words, names):
        if name in SHELL_CWD_COMMANDS or name in {":", "export", "readonly", "unset"}:
            continue
        if name == "git":
            has_output = any(
                word == "--output" or word.startswith("--output=")
                for word in words[1:]
            )
            if _git_command_is_read_only(_git_command(words)) and not has_output:
                continue
        elif name == "sed":
            sed_mutating, _paths = _sed_in_place_paths(words[1:])
            if not sed_mutating:
                continue
        elif name in READ_ONLY_SHELL_COMMANDS:
            continue
        return True
    return bool(_shell_redirection_paths(command) or _has_inline_mutation(command))


def _shell_segment_is_definitely_read_only(segment: str) -> bool:
    try:
        if _shell_redirection_paths(segment):
            return False
        words = _strip_shell_control_prefix(
            _without_shell_redirections(_shell_segment_words(segment))
        )
    except ValueError:
        return False
    unwrapped = _unwrap_shell_command(words)
    if not unwrapped:
        return True
    token = unwrapped[0]
    name = _shell_command_name(token)
    if "/" in token or _has_inline_mutation(segment):
        return False
    if re.search(r"(?<!\\)(?:\$\(|`|<\(|>\()", segment):
        return False
    if name == "git":
        has_output = any(
            word == "--output" or word.startswith("--output=")
            for word in unwrapped[1:]
        )
        return _git_command_is_read_only(_git_command(unwrapped)) and not has_output
    if name == "sed":
        sed_mutating, _paths = _sed_in_place_paths(unwrapped[1:])
        return not sed_mutating
    return name in READ_ONLY_SHELL_COMMANDS or name == ":"


def _unsupported_pipefail_mutation(
    command: str,
    *,
    pipefail_enabled: bool = False,
) -> bool:
    pipefail_enabled = pipefail_enabled or bool(
        re.search(
            r"(?:^|[;\s])(?:bash|set|sh|zsh)\b[^;&\r\n]*"
            r"(?:^|\s)-[A-Za-z]*o[A-Za-z]*\s+pipefail\b",
            command,
        )
    )
    pipeline_open = False
    guarded_or_branch = False
    for segment, outgoing_operator in _split_shell_commands(command):
        if (
            pipefail_enabled
            and guarded_or_branch
            and not _shell_segment_is_definitely_read_only(segment)
        ):
            return True
        try:
            words = _strip_shell_control_prefix(
                _without_shell_redirections(_shell_segment_words(segment))
            )
        except ValueError:
            words = []
        unwrapped = _unwrap_shell_command(words)
        if unwrapped:
            name = _shell_command_name(unwrapped[0])
            nested_command = (
                _shell_c_command(unwrapped[1:])
                if name in NESTED_SHELL_COMMANDS
                else " ".join(unwrapped[1:])
                if name == "eval"
                else None
            )
            if nested_command is not None and _unsupported_pipefail_mutation(
                nested_command,
                pipefail_enabled=pipefail_enabled,
            ):
                return True
        if outgoing_operator in {"|", "|&"}:
            pipeline_open = True
        elif outgoing_operator == "||":
            guarded_or_branch = guarded_or_branch or pipeline_open
            pipeline_open = False
        elif outgoing_operator not in {"&&"}:
            pipeline_open = False
            guarded_or_branch = False
    return False


def _materialize_shell_paths(
    candidates: list[str],
    current_directories: set[Path | None],
    *,
    include_current_directories: bool,
) -> tuple[list[str], bool]:
    paths: list[str] = []
    unresolved = False
    for candidate in candidates:
        normalized = candidate.strip("'\"")
        if not normalized:
            continue
        if "$" in normalized or "`" in normalized or normalized.startswith("~"):
            unresolved = True
            continue
        if Path(normalized).is_absolute():
            paths.append(normalized)
            continue
        for current in current_directories:
            if current is None:
                unresolved = True
            else:
                paths.append(str(_resolve_shell_path(normalized, current)))
    if include_current_directories:
        for current in current_directories:
            if current is None:
                unresolved = True
            else:
                paths.append(str(current))
    return paths, unresolved


def _bounded_shell_states(
    states: set[tuple[Path | None, bool, bool]],
) -> set[tuple[Path | None, bool, bool]]:
    if len(states) <= MAX_SHELL_STATES:
        return states
    cdpath_states = {state[2] for state in states}
    return {
        (None, status, cdpath_active)
        for status in (False, True)
        for cdpath_active in cdpath_states
    }


def _extend_shell_paths(paths: list[str], additions: list[str]) -> bool:
    remaining = MAX_SHELL_PATHS - len(paths)
    if remaining > 0:
        paths.extend(additions[:remaining])
    return len(additions) > max(remaining, 0)


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


def _shell_cd_target(
    arguments: list[str],
    *,
    cdpath_active: bool = False,
) -> str | None:
    positional: list[str] = []
    options_finished = False
    for argument in arguments:
        if not options_finished and argument == "--":
            options_finished = True
        elif not options_finished and argument.startswith("-") and argument != "-":
            if re.fullmatch(r"-[LPe@]+", argument) is None:
                return None
        else:
            positional.append(argument)
    if len(positional) != 1:
        return None
    target = positional[0]
    if (
        target == "-"
        or target.startswith("~")
        or any(character in target for character in ("$", "`", "*", "?", "["))
    ):
        return None
    if cdpath_active and not Path(target).is_absolute() and not target.startswith("."):
        return None
    return target


def _resolve_shell_path(candidate: str, base_dir: Path) -> Path:
    requested = Path(candidate)
    if not requested.is_absolute():
        requested = base_dir / requested
    return requested.resolve(strict=False)


def _gradle_command_paths(
    command_token: str,
    arguments: list[str],
    environment_values: dict[str, set[str]],
) -> tuple[list[str], bool]:
    paths = [command_token]
    unresolved = "/" not in command_token and "\\" not in command_token
    option_groups = [(arguments, False)]
    for variable in GRADLE_OPTION_ENVIRONMENT:
        for value in environment_values.get(variable, set()):
            if not value:
                continue
            if re.search(r"(?<!\\)[$`]", value):
                unresolved = True
                continue
            try:
                option_groups.append((shlex.split(value), variable != "GRADLE_OPTS"))
            except ValueError:
                unresolved = True
    for options, java_options in option_groups:
        index = 0
        while index < len(options):
            argument = options[index]
            if java_options:
                for prefix in ("-javaagent:", "-agentpath:"):
                    if argument.startswith(prefix):
                        agent_path = argument[len(prefix) :].split("=", 1)[0]
                        if agent_path:
                            paths.append(agent_path)
                        else:
                            unresolved = True
                        break
            if argument in GRADLE_PATH_OPTIONS:
                if index + 1 >= len(options) or not options[index + 1]:
                    unresolved = True
                else:
                    paths.append(options[index + 1])
                    index += 1
            else:
                matched = False
                for option in sorted(GRADLE_PATH_OPTIONS, key=len, reverse=True):
                    prefix = f"{option}="
                    if argument.startswith(prefix):
                        value = argument[len(prefix) :]
                        if value:
                            paths.append(value)
                        else:
                            unresolved = True
                        matched = True
                        break
                if not matched:
                    for option in GRADLE_COMPACT_PATH_OPTIONS:
                        if argument.startswith(option) and argument != option:
                            paths.append(argument[len(option) :])
                            matched = True
                            break
                if not matched and argument.startswith(("-P", "-D")) and "=" in argument:
                    value = argument.split("=", 1)[1]
                    if _looks_like_path(value):
                        paths.append(value)
            index += 1
    for gradle_home in environment_values.get("GRADLE_USER_HOME", set()):
        if not gradle_home:
            continue
        if re.search(r"(?<!\\)[$`]", gradle_home):
            unresolved = True
        else:
            paths.append(gradle_home)
    return paths, unresolved


def _shell_mutation_paths(
    command: str,
    *,
    base_dir: Path | None = None,
) -> tuple[bool, list[str], bool]:
    grouped_command = _full_shell_group(command)
    if grouped_command is not None:
        return _shell_mutation_paths(grouped_command, base_dir=base_dir)
    if _unsupported_compound_shell_mutation(command):
        return True, [], True
    if _unsupported_pipefail_mutation(command):
        return True, [], True

    paths: list[str] = []
    dynamic_target = False
    mutating_segment = False
    initial_cwd = base_dir.resolve(strict=False) if base_dir is not None else None
    states: set[tuple[Path | None, bool, bool]] = {
        (initial_cwd, True, bool(os.environ.get("CDPATH")))
    }
    gradle_environment = {
        name: {os.environ.get(name, "")}
        for name in (*GRADLE_OPTION_ENVIRONMENT, "GRADLE_USER_HOME")
    }
    incoming_operator = ""
    for segment, outgoing_operator in _split_shell_commands(command):
        if incoming_operator == "&&":
            executing_states = {state for state in states if state[1]}
        elif incoming_operator == "||":
            executing_states = {state for state in states if not state[1]}
        else:
            executing_states = set(states)
        skipped_states = states - executing_states
        if not executing_states:
            states = skipped_states
            incoming_operator = outgoing_operator
            continue
        try:
            segment_words = _shell_segment_words(segment)
            segment_redirections = _shell_redirection_paths(segment)
        except ValueError:
            return True, paths, True
        command_words = _strip_shell_control_prefix(
            _without_shell_redirections(segment_words)
        )
        segment_unsafe = _has_active_shell_substitution(segment)
        segment_parameter_expansion = _has_shell_parameter_expansion(segment)
        if segment_unsafe:
            mutating_segment = True
            dynamic_target = True
        wrapper_unwrapped = _unwrap_shell_command(command_words)
        assignments = _shell_assignments(command_words, wrapper_unwrapped)
        has_env_chdir, env_chdir_target = _env_chdir_target(
            command_words,
            wrapper_unwrapped,
        )
        env_split_string = _env_split_string(command_words, wrapper_unwrapped)
        try:
            if env_split_string is None:
                unwrapped = wrapper_unwrapped
            else:
                split_words = [
                    "env",
                    *shlex.split(env_split_string),
                    *wrapper_unwrapped,
                ]
                unwrapped = _unwrap_shell_command(split_words)
                assignments.update(_shell_assignments(split_words, unwrapped))
                split_has_chdir, split_chdir_target = _env_chdir_target(
                    split_words,
                    unwrapped,
                )
                if split_has_chdir:
                    has_env_chdir = True
                    env_chdir_target = split_chdir_target
        except ValueError:
            return True, paths, True
        current_directories = {state[0] for state in executing_states}
        if segment_redirections:
            redirection_paths, unresolved = _materialize_shell_paths(
                segment_redirections,
                current_directories,
                include_current_directories=True,
            )
            dynamic_target = (
                dynamic_target
                or unresolved
                or _extend_shell_paths(paths, redirection_paths)
            )
            mutating_segment = True
        command_directories = current_directories
        if has_env_chdir:
            if (
                env_chdir_target is None
                or env_chdir_target.startswith("~")
                or any(character in env_chdir_target for character in ("$", "`", "*", "?", "["))
            ):
                command_directories = {None}
            else:
                command_directories = {
                    _resolve_shell_path(env_chdir_target, current)
                    if current is not None
                    else None
                    for current in current_directories
                }
            mutating_segment = True
        if not unwrapped:
            gradle_environment = _updated_environment_values(
                gradle_environment,
                _assignment_operations(command_words),
                retain_previous=True,
            )
            cdpath_value = assignments.get("CDPATH")
            assignment_outcomes = {
                (
                    current,
                    True,
                    bool(cdpath_value) if cdpath_value is not None else cdpath_active,
                )
                for current, _status, cdpath_active in executing_states
            }
            assignment_outcomes.update(
                (current, False, cdpath_active)
                for current, _status, cdpath_active in executing_states
            )
            states = _bounded_shell_states(skipped_states | assignment_outcomes)
            incoming_operator = outgoing_operator
            continue
        command_name = _shell_command_name(unwrapped[0])
        arguments = unwrapped[1:]
        shell_builtin_token = (
            "/" not in unwrapped[0]
            and unwrapped[0] == command_name
            and not _uses_external_command_wrapper(command_words, wrapper_unwrapped)
        )
        persistent_cdpath = (
            assignments.get("CDPATH")
            if shell_builtin_token and command_name in POSIX_SPECIAL_BUILTINS
            else None
        )

        nested_command: str | None = None
        if command_name in NESTED_SHELL_COMMANDS:
            nested_command = _shell_c_command(arguments)
        elif command_name == "eval" and shell_builtin_token:
            eval_arguments = arguments[1:] if arguments[:1] == ["--"] else arguments
            nested_command = " ".join(eval_arguments)
        if nested_command is not None:
            nested_mutating = False
            for current in command_directories:
                mutating, nested_paths, unresolved = _shell_mutation_paths(
                    nested_command,
                    base_dir=current,
                )
                if mutating:
                    nested_mutating = True
                    dynamic_target = (
                        dynamic_target
                        or unresolved
                        or _extend_shell_paths(paths, nested_paths)
                    )
            if nested_mutating:
                mutating_segment = True
                if re.search(r"(?<!\\)[$`]", nested_command):
                    dynamic_target = True
            nested_changes_cwd = any(
                _shell_command_name(words[0]) in SHELL_CWD_COMMANDS
                for words in _compound_shell_command_words(nested_command)
            )
            local_cdpath = assignments.get("CDPATH")
            if nested_changes_cwd and (
                bool(local_cdpath)
                or any(state[2] for state in executing_states)
            ):
                dynamic_target = dynamic_target or nested_mutating
            persists = outgoing_operator not in {"|", "|&", "&"}
            outcomes: set[tuple[Path | None, bool, bool]] = set()
            for current, _status, cdpath_active in executing_states:
                success_cwd = (
                    None
                    if command_name == "eval" and nested_changes_cwd and persists
                    else current
                )
                outcomes.add((success_cwd, True, cdpath_active))
                outcomes.add((current, False, cdpath_active))
            if persistent_cdpath is not None:
                outcomes = {
                    (
                        current,
                        status,
                        bool(persistent_cdpath) if status else cdpath_active,
                    )
                    for current, status, cdpath_active in outcomes
                }
            states = _bounded_shell_states(skipped_states | outcomes)
            incoming_operator = outgoing_operator
            continue

        state_only_command = shell_builtin_token and (
            command_name in SHELL_CWD_COMMANDS
            or command_name
            in {"declare", "export", "readonly", "typeset", "unset"}
        )
        sed_mutating, sed_paths = _sed_in_place_paths(arguments) if command_name == "sed" else (False, [])
        perl_mutating, perl_paths = _perl_in_place_paths(arguments) if command_name == "perl" else (False, [])
        segment_inline = _has_inline_mutation(segment)
        segment_mutating_options = (
            sed_mutating
            or perl_mutating
            or command_name == "xargs"
            or (
                command_name == "find"
                and bool(re.search(r"(?:^|\s)-(?:delete|exec|execdir)\b", segment))
            )
            or command_name in {"dd", "rsync"}
        )
        git_command: tuple[str, str, tuple[str, ...]] | None = None
        declared_paths: list[str] = []
        if command_name == "git":
            git_command = _git_command(unwrapped)
            for index, word in enumerate(unwrapped[1:], start=1):
                if word.startswith("--output="):
                    declared_paths.append(word.split("=", 1)[1])
                    segment_mutating_options = True
                elif word == "--output":
                    segment_mutating_options = True
                    if index + 1 < len(unwrapped):
                        declared_paths.append(unwrapped[index + 1])
        segment_read_only = (
            state_only_command
            or (
                command_name in READ_ONLY_SHELL_COMMANDS
                and (git_command is None or _git_command_is_read_only(git_command))
                and not segment_mutating_options
                and not segment_unsafe
                and not segment_inline
            )
        )
        if not segment_read_only:
            mutating_segment = True
            dynamic_target = dynamic_target or segment_unsafe or command_name == "xargs"
            segment_paths: list[str] = list(declared_paths)
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
            elif command_name in {"gradle", "gradlew"}:
                prefix_operations = _assignment_operations(
                    command_words[: len(command_words) - len(wrapper_unwrapped)]
                )
                effective_gradle_environment = _updated_environment_values(
                    gradle_environment,
                    prefix_operations,
                    retain_previous=False,
                )
                gradle_paths, gradle_unresolved = _gradle_command_paths(
                    unwrapped[0],
                    arguments,
                    effective_gradle_environment,
                )
                segment_paths.extend(gradle_paths)
                dynamic_target = dynamic_target or gradle_unresolved
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
            materialized, unresolved = _materialize_shell_paths(
                segment_paths,
                command_directories,
                include_current_directories=True,
            )
            dynamic_target = (
                dynamic_target
                or segment_parameter_expansion
                or unresolved
                or _extend_shell_paths(paths, materialized)
            )

        persists = outgoing_operator not in {"|", "|&", "&"}
        if persists and (
            not unwrapped
            or (
                shell_builtin_token
                and command_name in {
                    "declare",
                    "export",
                    "readonly",
                    "typeset",
                }
            )
        ):
            operation_words = (
                command_words[: len(command_words) - len(wrapper_unwrapped)]
                if not unwrapped
                else arguments
            )
            gradle_environment = _updated_environment_values(
                gradle_environment,
                _assignment_operations(operation_words),
                retain_previous=True,
            )
        if persists and shell_builtin_token and command_name == "unset":
            for name in arguments:
                if name in gradle_environment:
                    gradle_environment[name].add("")
        if persists and shell_builtin_token and command_name == "printf":
            for index, argument in enumerate(arguments):
                if argument == "-v" and index + 1 < len(arguments):
                    name = arguments[index + 1]
                    if name in gradle_environment:
                        gradle_environment[name].add("$AGENT_FLOW_UNRESOLVED")
        if persists and shell_builtin_token and command_name == "read":
            for argument in arguments:
                name = argument.rsplit("?", 1)[0]
                if name in gradle_environment:
                    gradle_environment[name].add("$AGENT_FLOW_UNRESOLVED")
        outcomes = set()
        if shell_builtin_token and command_name in {"cd", "chdir"}:
            local_cdpath = assignments.get("CDPATH")
            for current, _status, cdpath_active in executing_states:
                target = _shell_cd_target(
                    arguments,
                    cdpath_active=(
                        bool(local_cdpath)
                        if local_cdpath is not None
                        else cdpath_active
                    ),
                )
                success_cwd = (
                    _resolve_shell_path(target, current)
                    if persists and target is not None and current is not None
                    else None if persists else current
                )
                outcomes.add((success_cwd, True, cdpath_active))
                outcomes.add((current, False, cdpath_active))
        elif shell_builtin_token and command_name == "pushd":
            local_cdpath = assignments.get("CDPATH")
            for current, _status, cdpath_active in executing_states:
                target = _shell_cd_target(
                    arguments,
                    cdpath_active=(
                        bool(local_cdpath)
                        if local_cdpath is not None
                        else cdpath_active
                    ),
                )
                success_cwd = (
                    _resolve_shell_path(target, current)
                    if persists and target is not None and current is not None
                    else None if persists else current
                )
                outcomes.add((success_cwd, True, cdpath_active))
                outcomes.add((current, False, cdpath_active))
        elif shell_builtin_token and command_name == "popd":
            for current, _status, cdpath_active in executing_states:
                outcomes.add((None if persists else current, True, cdpath_active))
                outcomes.add((current, False, cdpath_active))
        elif shell_builtin_token and command_name in {".", "source"}:
            for current, _status, cdpath_active in executing_states:
                outcomes.add((None if persists else current, True, cdpath_active))
                outcomes.add((current, False, cdpath_active))
        elif shell_builtin_token and command_name in {
            "declare",
            "export",
            "readonly",
            "typeset",
        }:
            exported_cdpath = next(
                (
                    argument.split("=", 1)[1]
                    for argument in arguments
                    if argument.startswith("CDPATH=")
                ),
                None,
            )
            for current, _status, cdpath_active in executing_states:
                outcomes.add(
                    (
                        current,
                        True,
                        bool(exported_cdpath)
                        if exported_cdpath is not None
                        else cdpath_active,
                    )
                )
                outcomes.add((current, False, cdpath_active))
        elif shell_builtin_token and command_name == "unset":
            clears_cdpath = "CDPATH" in arguments and not any(
                option == "-f" or "f" in option[1:]
                for option in arguments
                if option.startswith("-") and option != "--"
            )
            for current, _status, cdpath_active in executing_states:
                outcomes.add((current, True, False if clears_cdpath else cdpath_active))
                outcomes.add((current, False, cdpath_active))
        elif shell_builtin_token and command_name in {":", "true"}:
            outcomes = {
                (
                    current,
                    True,
                    bool(assignments["CDPATH"])
                    if command_name == ":" and "CDPATH" in assignments
                    else cdpath_active,
                )
                for current, _status, cdpath_active in executing_states
            }
        elif shell_builtin_token and command_name == "false":
            outcomes = {
                (current, False, cdpath_active)
                for current, _status, cdpath_active in executing_states
            }
        else:
            for current, _status, cdpath_active in executing_states:
                outcomes.add((current, True, cdpath_active))
                outcomes.add((current, False, cdpath_active))
        if persistent_cdpath is not None:
            outcomes = {
                (
                    current,
                    status,
                    bool(persistent_cdpath) if status else cdpath_active,
                )
                for current, status, cdpath_active in outcomes
            }
        if segment_redirections:
            outcomes.update(
                (current, False, cdpath_active)
                for current, _status, cdpath_active in executing_states
            )
        states = _bounded_shell_states(skipped_states | outcomes)
        incoming_operator = outgoing_operator
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
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\+?=.*", word):
            index += 1
            continue
        if word == "command":
            index += 1
            while index < len(words) and words[index].startswith("-"):
                option = words[index]
                index += 1
                if option == "--":
                    break
            continue
        if word == "builtin":
            index += 1
            if index < len(words) and words[index] == "--":
                index += 1
            continue
        if word == "exec":
            index += 1
            while index < len(words) and words[index].startswith("-"):
                option = words[index]
                index += 1
                if option == "--":
                    break
                if option == "-a" and index < len(words):
                    index += 1
            continue
        if Path(word).name.lower() != "env":
            break
        index += 1
        while index < len(words):
            option = words[index]
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\+?=.*", option):
                index += 1
            elif option in {"-i", "--ignore-environment"}:
                index += 1
            elif option in {
                "-C",
                "-P",
                "-S",
                "-u",
                "--argv0",
                "--chdir",
                "--split-string",
                "--unset",
            } and index + 1 < len(words):
                index += 2
            elif option.startswith(("-C", "--chdir=", "--split-string=")):
                index += 1
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


def _requests_agent_flow_launcher(
    command: str,
    cwd: Path,
    *,
    _depth: int = 0,
    _search_path: str | None = None,
) -> bool:
    if _depth > 8:
        return True
    normalized = re.sub(r"\\\r?\n", "", command)
    for segment in _split_shell_segments(normalized):
        try:
            words = shlex.split(segment)
        except ValueError:
            return True
        if _launcher_words_request_agent_flow(
            words,
            cwd=cwd,
            _depth=_depth,
            _search_path=_search_path,
        ):
            return True
    return False


def _launcher_words_request_agent_flow(
    words: list[str],
    *,
    cwd: Path,
    _depth: int,
    _search_path: str | None = None,
) -> bool:
    search_path = os.environ.get("PATH", os.defpath) if _search_path is None else _search_path
    index = 0
    while index < len(words) and (
        words[index] in {"command", "builtin", "exec"}
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\+?=.*", words[index])
    ):
        wrapper = words[index]
        index += 1
        assignment = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)(\+?)=(.*)", wrapper)
        if assignment and assignment.group(1) == "PATH":
            if re.search(r"(?<!\\)[$`]", assignment.group(3)):
                return True
            search_path = (
                f"{search_path}{assignment.group(3)}"
                if assignment.group(2)
                else assignment.group(3)
            )
            continue
        if wrapper in {"command", "builtin", "exec"}:
            while index < len(words) and words[index].startswith("-"):
                option = words[index]
                index += 1
                if option == "--":
                    break
                if wrapper == "exec" and option == "-a" and index < len(words):
                    index += 1
    words = words[index:]
    if not words:
        return False
    command_name = Path(words[0]).name.lower()
    resolved_interpreter = _resolved_script_interpreter_name(words[0], cwd, search_path)
    if command_name in {"agent-flow", "agent-flow-kit"} or resolved_interpreter in {
        "agent-flow",
        "agent-flow-kit",
        "agent-flow-kit.mjs",
    }:
        return True
    if command_name == "env":
        index = 1
        command_cwd = cwd
        while index < len(words):
            word = words[index]
            assignment = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)(\+?)=(.*)", word)
            if assignment:
                if assignment.group(1) == "PATH":
                    if re.search(r"(?<!\\)[$`]", assignment.group(3)):
                        return True
                    search_path = (
                        f"{search_path}{assignment.group(3)}"
                        if assignment.group(2)
                        else assignment.group(3)
                    )
                index += 1
                continue
            if word in {"-S", "--split-string"}:
                if index + 1 >= len(words):
                    return True
                try:
                    split_words = shlex.split(words[index + 1])
                except ValueError:
                    return True
                return _launcher_words_request_agent_flow(
                    [*split_words, *words[index + 2 :]],
                    cwd=command_cwd,
                    _depth=_depth + 1,
                    _search_path=search_path,
                )
            if word.startswith("--split-string="):
                try:
                    split_words = shlex.split(word.split("=", 1)[1])
                except ValueError:
                    return True
                return _launcher_words_request_agent_flow(
                    [*split_words, *words[index + 1 :]],
                    cwd=command_cwd,
                    _depth=_depth + 1,
                    _search_path=search_path,
                )
            if word in {
                "-C",
                "-P",
                "-u",
                "--argv0",
                "--chdir",
                "--unset",
            }:
                if index + 1 >= len(words):
                    return True
                value = words[index + 1]
                if word == "-P":
                    if re.search(r"(?<!\\)[$`]", value):
                        return True
                    search_path = value
                if word in {"-C", "--chdir"}:
                    if value.startswith("~") or re.search(r"(?<!\\)[$`]", value):
                        return True
                    command_cwd = _resolve_shell_path(value, command_cwd)
                if word in {"-u", "--unset"} and value == "PATH":
                    search_path = os.defpath
                index += 2
                continue
            if word in {"-i", "--ignore-environment"}:
                search_path = os.defpath
                index += 1
                continue
            if word.startswith("--unset="):
                if word.split("=", 1)[1] == "PATH":
                    search_path = os.defpath
                index += 1
                continue
            if word.startswith("--chdir="):
                value = word.split("=", 1)[1]
                if not value or value.startswith("~") or re.search(r"(?<!\\)[$`]", value):
                    return True
                command_cwd = _resolve_shell_path(value, command_cwd)
                index += 1
                continue
            if word.startswith("-C") and word != "-C":
                value = word[2:]
                if not value or value.startswith("~") or re.search(r"(?<!\\)[$`]", value):
                    return True
                command_cwd = _resolve_shell_path(value, command_cwd)
                index += 1
                continue
            if word == "--":
                index += 1
                break
            if word.startswith("-"):
                index += 1
                continue
            break
        return _launcher_words_request_agent_flow(
            words[index:],
            cwd=command_cwd,
            _depth=_depth + 1,
            _search_path=search_path,
        )
    effective_interpreter = resolved_interpreter or command_name
    if effective_interpreter in NESTED_SHELL_COMMANDS:
        nested = _shell_c_command(words[1:])
        if nested is not None:
            return _requests_agent_flow_launcher(
                nested,
                cwd,
                _depth=_depth + 1,
                _search_path=search_path,
            )
        return any(_word_requests_agent_flow_runtime(word) for word in words[1:])
    if effective_interpreter in SCRIPT_INTERPRETERS or re.fullmatch(
        r"python(?:3(?:\.\d+)?)?",
        effective_interpreter,
    ):
        return any(_word_requests_agent_flow_runtime(word) for word in words[1:])
    if command_name == "eval":
        arguments = words[1:]
        if arguments[:1] == ["--"]:
            arguments = arguments[1:]
        return bool(arguments) and _requests_agent_flow_launcher(
            " ".join(arguments),
            cwd,
            _depth=_depth + 1,
            _search_path=search_path,
        )
    return False


def _resolved_script_interpreter_name(
    command_token: str,
    cwd: Path,
    search_path: str,
) -> str | None:
    try:
        if "/" in command_token or "\\" in command_token:
            candidate = Path(command_token)
            if not candidate.is_absolute():
                candidate = cwd / candidate
            resolved = candidate.resolve(strict=True)
        else:
            found = shutil.which(command_token, path=search_path)
            if not found:
                return None
            resolved = Path(found).resolve(strict=True)
    except OSError:
        return None
    name = resolved.name.lower()
    if name in {
        "agent-flow",
        "agent-flow-kit",
        "agent-flow-kit.mjs",
    }:
        return name
    if name in NESTED_SHELL_COMMANDS or name in SCRIPT_INTERPRETERS:
        return name
    if re.fullmatch(r"python(?:3(?:\.\d+)?)?", name):
        return name
    return None


def _word_requests_agent_flow_runtime(word: str) -> bool:
    normalized = word.replace("\\", "/").rstrip("/")
    basename = Path(normalized).name.lower()
    return basename in {"agent-flow", "agent-flow-kit", "agent-flow-kit.mjs"} or (
        "/.agent-flow/runtime/" in f"/{normalized.lstrip('/')}"
        and basename in {"agent-flow", "agent-flow-kit", "agent-flow-kit.mjs"}
    )


def _normalized_worktree_name(value: str) -> str | None:
    lowered = value.strip().lower()
    safe = re.sub(r"[^a-z0-9._-]+", "-", lowered).strip("-")
    if not safe or safe.startswith(".") or ".." in safe:
        if not any(character.isalnum() for character in lowered):
            return None
        safe = f"task-{hashlib.sha1(lowered.encode('utf-8')).hexdigest()[:8]}"
    return safe if safe.startswith("feat-") else f"feat-{safe}"


def _requested_agent_flow_subcommand(command: str) -> str | None:
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if len(words) < 2 or Path(words[0]).name.lower() not in {"agent-flow", "agent-flow-kit"}:
        return None
    return words[1]


def _requested_worktree_removal(command: str) -> str | None:
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if len(words) != 5 or Path(words[0]).name.lower() not in {"agent-flow", "agent-flow-kit"}:
        return None
    if words[1:4] != ["worktree", "remove", "--name"]:
        return None
    return _normalized_worktree_name(words[4])


def _record_stage_arguments_are_bounded(
    arguments: list[str],
    *,
    cwd: Path,
    leader_root: Path,
    run_dir: Path | None,
) -> bool:
    if run_dir is None or len(arguments) < 9 or len(arguments) % 2 == 0:
        return False
    allowed = {
        "--root",
        "--run-dir",
        "--stage",
        "--status",
        "--evidence-type",
        "--confidence",
        "--content",
    }
    options: dict[str, str] = {}
    for index in range(1, len(arguments), 2):
        option = arguments[index]
        if option not in allowed or option in options:
            return False
        options[option] = arguments[index + 1]
    if not {"--root", "--run-dir", "--stage", "--content"} <= options.keys():
        return False
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", options["--stage"]) is None:
        return False
    try:
        requested_root = Path(options["--root"])
        if not requested_root.is_absolute():
            requested_root = cwd / requested_root
        requested_run_dir = Path(options["--run-dir"])
        if not requested_run_dir.is_absolute():
            requested_run_dir = cwd / requested_run_dir
        return (
            requested_root.resolve(strict=True) == leader_root.resolve(strict=True)
            and requested_run_dir.resolve(strict=True) == run_dir.resolve(strict=True)
        )
    except OSError:
        return False


def _is_agent_flow_launcher(
    command: str,
    cwd: Path,
    leader_root: Path,
    pinned_root: Path,
    *,
    run_dir: Path | None = None,
) -> bool:
    if _has_active_shell_substitution(command) or re.search(r"[\r\n;&|]|\d*>>?|&>", command):
        return False
    if any(
        value
        for name, value in os.environ.items()
        if name in {"NODE_OPTIONS", "NODE_PATH", "BASH_ENV", "ENV"}
        or name.startswith(("LD_", "DYLD_"))
    ):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if (
        not words
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\+?=.*", words[0])
        or Path(words[0]).name.lower() == "env"
    ):
        return False
    command_token = words[0]
    command_name = Path(command_token).name.lower()
    arguments = words[1:]
    if command_name not in {"agent-flow", "agent-flow-kit"} or not arguments:
        return False
    trusted_subcommands = {
        "status",
        "continue",
        "run",
        "gate",
        "gates",
        "record-stage",
        "architecture-lint",
        "install",
        "worktree",
    }
    if arguments[0] not in trusted_subcommands:
        return False
    if arguments[:2] == ["run", "install"]:
        return False
    if arguments[0] == "record-stage" and not _record_stage_arguments_are_bounded(
        arguments,
        cwd=cwd,
        leader_root=leader_root,
        run_dir=run_dir,
    ):
        return False
    if arguments[0] == "install":
        try:
            if cwd.resolve(strict=True) != leader_root.resolve(strict=True):
                return False
        except OSError:
            return False
        if arguments[1:] not in ([], ["--force-managed"]):
            return False
    if arguments[0] == "worktree":
        requested_name = _requested_worktree_removal(command)
        try:
            leader = leader_root.resolve(strict=True)
            current = cwd.resolve(strict=True)
            pinned_name = pinned_root.name
        except OSError:
            return False
        if requested_name is None or current != leader or requested_name != pinned_name:
            return False
    if "/" in command_token or "\\" in command_token:
        candidate = (cwd / command_token).resolve(strict=True) if not Path(command_token).is_absolute() else Path(command_token).resolve(strict=True)
    else:
        found = shutil.which(command_token)
        if not found:
            return False
        candidate = Path(found).resolve(strict=True)
    local_launcher = leader_root / ".agent-flow" / "bin" / "agent-flow"
    if arguments[0] == "install":
        requested_launcher = Path(command_token)
        if not requested_launcher.is_absolute():
            requested_launcher = cwd / requested_launcher
        if Path(os.path.abspath(requested_launcher)) != Path(os.path.abspath(local_launcher)):
            return False
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
            commitment = _project_runtime_contract_commitment(contract)
            launcher_contract = contract["launcher"]
            runtime_contract = contract["runtime"]
            node_contract = contract["node"]
            python_contract = contract["python"]
            embedded = EXPECTED_PROJECT_RUNTIME_CONTRACT_SHA256
            embedded_python = EXPECTED_PYTHON_RUNTIME_INTEGRITY
            embedded_authority = not embedded.startswith("__AGENT_FLOW_")
            if (
                contract["version"] != 3
                or kit["project_runtime_contract_commitment_version"] != 1
                or kit["project_runtime_contract_commitment"] != commitment
                or launcher_contract["path"] != ".agent-flow/bin/agent-flow"
                or runtime_contract["path"] != ".agent-flow/runtime/node"
                or Path(node_contract["path"]).resolve(strict=True) != Path(node_contract["path"])
                or not Path(python_contract["path"]).is_absolute()
                or Path(python_contract["path"]).resolve(strict=True) != Path(python_contract["resolved_path"])
                or (embedded_authority and embedded != commitment)
                or (
                    not embedded_python.startswith("__AGENT_FLOW_")
                    and embedded_python != contract["python_runtime"]["integrity"]
                )
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
        json.dumps(contract["node"].get("dependencies", []), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        contract["git"]["path"],
        contract["git"]["sha256"],
        contract["git"]["device"],
        contract["git"]["inode"],
        contract["git"]["links"],
        contract["git"]["mode"],
        json.dumps(contract["git"].get("dependencies", []), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        contract["python"]["path"],
        contract["python"]["resolved_path"],
        contract["python"]["sha256"],
        contract["python"]["device"],
        contract["python"]["inode"],
        contract["python"]["links"],
        contract["python"]["mode"],
        json.dumps(contract["python"].get("dependencies", []), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
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
    executable_matches = (
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
    if not executable_matches:
        return False
    dependency_entries = contract.get("dependencies", [])
    if not isinstance(dependency_entries, list):
        return False
    expected_dependencies = {
        str(entry.get("name")): entry
        for entry in dependency_entries
        if isinstance(entry, dict)
    }
    if len(expected_dependencies) != len(dependency_entries):
        return False
    for name, expected in expected_dependencies.items():
        if Path(str(expected.get("path", ""))).name != name:
            return False
        load_paths = expected.get("load_paths", [expected.get("path")])
        if (
            not isinstance(load_paths, list)
            or not load_paths
            or any(
                not isinstance(load_path, str)
                or not Path(load_path).is_absolute()
                or str(Path(load_path).resolve(strict=True)) != expected.get("path")
                for load_path in load_paths
            )
        ):
            return False
        dependency = Path(str(expected["path"])).resolve(strict=True)
        dependency_metadata = dependency.lstat()
        if not (
            str(dependency) == expected["path"]
            and dependency.is_file()
            and not dependency.is_symlink()
            and dependency_metadata.st_mode & 0o022 == 0
            and hashlib.sha256(dependency.read_bytes()).hexdigest() == expected["sha256"]
            and str(dependency_metadata.st_dev) == expected["device"]
            and str(dependency_metadata.st_ino) == expected["inode"]
            and str(dependency_metadata.st_nlink) == expected["links"]
            and stat.S_IMODE(dependency_metadata.st_mode) == expected["mode"]
        ):
            return False
    return True


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
        context_cwd = Path(cwd_value) if isinstance(cwd_value, str) and cwd_value else Path.cwd()
        leader_root = _leader_root(context_cwd)
        if not (leader_root / ".git").exists():
            return 0
        cwd = context_cwd
        tool_input = _tool_input(payload)
        if tool_name in SHELL_TOOLS:
            declared_cwd = _shell_tool_cwd(tool_input, context_cwd)
            if declared_cwd is not None:
                cwd = declared_cwd
        host = str(
            payload.get("host")
            or _host_argument()
            or os.environ.get("AGENT_FLOW_ACTIVE_HOST")
            or "unknown"
        ).strip().lower()
        command = ""
        paths: list[str] = []
        unresolved_shell_target = False
        requested_launcher = False
        requested_worktree_removal: str | None = None
        requested_subcommand: str | None = None
        if tool_name in SHELL_TOOLS:
            command_value = tool_input.get("command")
            if not isinstance(command_value, str) or not command_value.strip():
                raise ValueError("write boundary rejected: shell tool did not declare a command")
            command = command_value
            mutating, paths, unresolved_shell_target = _shell_mutation_paths(
                command,
                base_dir=cwd,
            )
            if not mutating:
                return 0
            requested_launcher = _requests_agent_flow_launcher(command, cwd)
            requested_subcommand = _requested_agent_flow_subcommand(command)
            requested_worktree_removal = _requested_worktree_removal(command)
            if (
                requested_worktree_removal is None
                and _is_agent_flow_launcher(command, cwd, leader_root, leader_root)
            ):
                if host == "claude":
                    _forward_claude_execution_identity(payload, tool_input, command)
                elif host == "omp":
                    _required_hook_execution_id(payload)
                return 0
            if (
                requested_launcher
                and requested_worktree_removal is None
                and requested_subcommand != "record-stage"
            ):
                raise ValueError("write boundary rejected: agent-flow launcher is not trusted")
        (
            boundary_error,
            resolve_execution,
            select_workspace,
            resolve_path,
            resolve_finalizer_workspace,
            binding_exists,
        ) = _load_boundary_module(leader_root)
        execution = resolve_execution(payload, os.environ, host_hint=host)
        if requested_worktree_removal is not None:
            try:
                active = resolve_finalizer_workspace(
                    leader_root,
                    execution,
                    requested_worktree_removal,
                )
            except boundary_error as exc:
                raise ValueError(
                    "write boundary rejected: worktree cleanup requires authenticated completed run ownership"
                ) from exc
        else:
            active = select_workspace(leader_root, execution)
            if active is None:
                if binding_exists(leader_root, execution):
                    raise boundary_error(
                        "write boundary rejected: "
                        f"resolved_path={cwd} "
                        f"host={host} "
                        f"phase={payload.get('phase') or 'unknown'} "
                        "reason_code=bound_run_not_active "
                        "reason=execution is bound to a run without an active pinned worktree; "
                        "refusing mutation outside a worktree (recreate the worktree run or clear the stale binding)"
                    )
                return 0
        if tool_name in SHELL_TOOLS:
            pinned_root = Path(active.identity.workspace_root)
            if requested_worktree_removal is None:
                pinned_root = pinned_root.resolve(strict=True)
            leader_root = leader_root.resolve(strict=True)
            if _is_agent_flow_launcher(
                command,
                cwd,
                leader_root,
                pinned_root,
                run_dir=Path(active.run_dir),
            ):
                if host == "claude":
                    _forward_claude_execution_identity(payload, tool_input, command)
                elif host == "omp":
                    _required_hook_execution_id(payload)
                return 0
            if requested_launcher:
                raise ValueError("write boundary rejected: agent-flow launcher is not trusted")
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
            primary = _string_value(
                tool_input, "path", "file_path", "filePath", "filename"
            )
            if isinstance(primary, str) and primary.startswith("xd://"):
                xd_tool = primary[len("xd://"):].strip("/").split("/", 1)[0].lower()
                if xd_tool in XD_FS_MUTATORS:
                    paths = _xd_tool_targets(xd_tool, tool_input)
                    if not paths:
                        raise boundary_error(
                            "write boundary rejected: xd:// tool did not declare a target path"
                        )
                else:
                    return 0
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
            target = resolve_path(
                active.identity,
                requested,
                base_dir=cwd,
                host=host,
                phase=phase,
                run_dir=getattr(active, "run_dir", None),
            )
            _verify_managed_marker_integrity(
                tool_name,
                tool_input,
                requested,
                target,
                cwd,
            )
        return 0
    except Exception as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
