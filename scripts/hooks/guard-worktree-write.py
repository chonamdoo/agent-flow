#!/bin/sh
""":"
TRUSTED_PYTHON=/usr/bin/python3
if [ ! -f "$TRUSTED_PYTHON" ] || [ ! -x "$TRUSTED_PYTHON" ]; then
    echo "agent-flow: blocked because trusted Python interpreter is unavailable or unsafe" >&2
    exit 2
fi
exec "$TRUSTED_PYTHON" "$0" "$@"
exit 2
":"""
from __future__ import annotations

"""Fail closed when an active run attempts to write through the leader checkout."""

import ast
from fnmatch import fnmatchcase
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path


_READ_ONLY_COMMANDS = {
    "[",
    "cd",
    "date",
    "echo",
    "file",
    "head",
    "ls",
    "pwd",
    "readlink",
    "realpath",
    "rg",
    "stat",
    "tail",
    "test",
    "true",
    "type",
    "uname",
    "wc",
    "which",
}
_READ_ONLY_GIT_COMMANDS = {
    "branch",
    "diff",
    "grep",
    "log",
    "ls-files",
    "merge-base",
    "rev-parse",
    "show",
    "show-ref",
    "status",
    "tag",
    "worktree",
}
_SHELL_COMMANDS = {"bash", "sh", "zsh"}
_DYNAMIC_EXECUTOR_COMMANDS = {"doas", "parallel", "sudo", "xargs"}
_SHELL_CONTROL_COMMANDS = {
    "!", "{", "}", "case", "do", "done", "elif", "else", "esac", "fi",
    "for", "function", "if", "select", "then", "until", "while",
}
_STATIC_PROCESS_WRAPPERS = {"ionice", "nice", "nohup", "setsid", "stdbuf", "time", "timeout"}
_ALL_TARGET_COMMANDS = {
    "chmod",
    "chown",
    "mkdir",
    "rm",
    "rmdir",
    "touch",
    "truncate",
}
_DESTINATION_COMMANDS = {"cp", "install", "rsync"}
_OUTPUT_REDIRECT_OPERATORS = {">", ">>", ">|", ">>|", ">&", ">&|", "&>", "&>>", "<>"}
_INPUT_REDIRECT_OPERATORS = {"<", "<<", "<<-", "<<<"}
_REDIRECT_OPERATORS = _OUTPUT_REDIRECT_OPERATORS | _INPUT_REDIRECT_OPERATORS
_SHELL_SEPARATORS = {"&&", "||", ";", "|", "&"}
_GIT_PATH_ENV = {
    "GIT_COMMON_DIR",
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_WORK_TREE",
}
_SHARED_REF_MUTATING_GIT_COMMANDS = {
    "fast-import",
    "filter-branch",
    "filter-repo",
    "receive-pack",
    "symbolic-ref",
    "update-ref",
}
_STANDALONE_SHARED_REF_MUTATORS = {
    "git-fast-import",
    "git-branch",
    "git-checkout",
    "git-filter-branch",
    "git-receive-pack",
    "git-send-pack",
    "git-symbolic-ref",
    "git-switch",
    "git-update-ref",
}
_INLINE_INTERPRETER_FLAGS = {
    "bun": {"-e", "--eval", "-p", "--print"},
    "deno": {"eval"},
    "julia": {"-e", "--eval"},
    "lua": {"-e"},
    "node": {"-e", "--eval", "-p", "--print"},
    "osascript": {"-e"},
    "perl": {"-e", "-E"},
    "php": {"-r", "--run"},
    "ruby": {"-e"},
    "swift": {"-e"},
}
_MAX_SCRIPT_BYTES = 256 * 1024
_MAX_SCRIPT_DEPTH = 4
_UNSAFE_LAUNCHER_ENVIRONMENT_MARKER = "agent-flow:unsafe-launcher-environment"
_PROJECT_RUNTIME_CONTRACT_VERSION = 2
_PROJECT_RUNTIME_COMMITMENT_VERSION = 2
_PROJECT_LAUNCHER_RELATIVE = ".agent-flow/bin/agent-flow"
_NODE_RUNTIME_ROOT_RELATIVE = ".agent-flow/runtime/node"
_NODE_RUNTIME_ENTRYPOINT_RELATIVE = (
    ".agent-flow/runtime/node/bin/agent-flow-kit.mjs"
)
_PYTHON_RUNTIME_ROOT_RELATIVE = ".agent-flow/runtime/python"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LAUNCHER_UNSAFE_ENVIRONMENT = {
    "BASH_ENV",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "ENV",
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "NODE_OPTIONS",
    "NODE_PATH",
    "PATH",
}
_SCRIPT_INTERPRETER_RE = re.compile(
    r"^(?:bun|deno|python|pypy|node|ruby|perl|php|lua|julia|swift)(?:\d+(?:\.\d+)*)?$"
)
_NODE_WRITE_CALL_RE = re.compile(
    r"\b(?:appendFile|appendFileSync|createWriteStream|mkdir|mkdirSync|remove|rm|rmSync|"
    r"unlink|unlinkSync|writeFile|writeFileSync|writeTextFile|writeTextFileSync)\s*\(\s*"
    r"(?P<quote>['\"`])(?P<path>(?:\\.|(?!\1).)*)\1",
    re.DOTALL,
)
_NODE_WRITE_EXPRESSION_RE = re.compile(
    r"\b(?:appendFile|appendFileSync|createWriteStream|mkdir|mkdirSync|remove|rm|rmSync|"
    r"unlink|unlinkSync|writeFile|writeFileSync|writeTextFile|writeTextFileSync)\s*\(\s*"
    r"(?P<target>[A-Za-z_$][A-Za-z0-9_$]*)\s*(?:,|\))",
    re.DOTALL,
)
_NODE_STRING_BINDING_RE = re.compile(
    r"\b(?:const|let|var)\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
    r"(?P<quote>['\"`])(?P<value>(?:\\.|(?!\2).)*)\2",
    re.DOTALL,
)
_NODE_COMMAND_CALL_RE = re.compile(
    r"\b(?:exec|execSync)\s*\(\s*(?P<quote>['\"`])"
    r"(?P<command>(?:\\.|(?!\1).)*)\1",
    re.DOTALL,
)
_NODE_LINK_CALL_RE = re.compile(
    r"\b(?:link|linkSync|symlink|symlinkSync)\s*\(\s*"
    r"(?P<source>(?P<source_quote>['\"`])(?:\\.|(?!(?P=source_quote)).)*(?P=source_quote)|[A-Za-z_$][A-Za-z0-9_$]*)"
    r"\s*,\s*"
    r"(?P<target>(?P<target_quote>['\"`])(?:\\.|(?!(?P=target_quote)).)*(?P=target_quote)|[A-Za-z_$][A-Za-z0-9_$]*)",
    re.DOTALL,
)
_NODE_LINK_CALL_NAME_RE = re.compile(
    r"\b(?:link|linkSync|symlink|symlinkSync)\s*\("
)
_TRUSTED_GIT = "/usr/bin/git"


def _values(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {
                "file_path",
                "filePath",
                "path",
                "filename",
                "target_file",
                "notebook_path",
                "notebookPath",
            } and isinstance(child, str):
                yield child
            if key in {"patch", "input"} and isinstance(child, str):
                for match in re.finditer(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", child, re.MULTILINE):
                    yield match.group(1).strip()
            yield from _values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _values(child)
    elif isinstance(value, str):
        for match in re.finditer(
            r"^\*\*\* (?:(?:Add|Update|Delete) File:|Move to:) (.+)$",
            value,
            re.MULTILINE,
        ):
            yield match.group(1).strip()


def _active_run_control_root(run: dict, root: Path) -> Path | None:
    validated = run.get("_control_root")
    if isinstance(validated, Path):
        return validated
    raw = run.get("run_dir")
    if not isinstance(raw, str) or not raw:
        return None
    candidate = Path(raw)
    candidate = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    runs_root = (root / ".agent-flow" / "runs").resolve()
    return candidate if candidate != runs_root and _path_is_within(candidate, runs_root) else None


def _is_control_target(candidate: Path, control_root: Path | None) -> bool:
    if control_root is None or not _path_is_within(candidate, control_root):
        return False
    relative = candidate.relative_to(control_root)
    if not relative.parts or relative.name in {"manifest.json", "meta.json"}:
        return False
    if len(relative.parts) == 1:
        return relative.suffix.lower() in {".json", ".md", ".txt", ".yaml", ".yml"}
    return relative.parts[0] in {"artifacts", "logs"}


def _bash_command(payload) -> str | None:
    tool_name = str(payload.get("tool_name") or payload.get("tool") or "").lower()
    if tool_name != "bash":
        return None
    for key in ("tool_input", "input", "parameters"):
        value = payload.get(key)
        if isinstance(value, dict) and isinstance(value.get("command"), str):
            return value["command"]
    return ""


def _is_embedded_execution_tool(payload) -> bool:
    tool_name = str(payload.get("tool_name") or payload.get("tool") or "").lower()
    return tool_name in {"eval", "python"}


def _leader_bash_is_read_only(
    command: str,
    *,
    allow_start: bool,
    root: Path | None = None,
    cwd: Path | None = None,
) -> bool:
    if not command.strip() or "`" in command or "$(" in command or "\n" in command or "\r" in command:
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return False
    if any(token in _REDIRECT_OPERATORS for token in tokens):
        return False
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in {"&&", "||", ";", "|", "&"}:
            if not segments[-1]:
                return False
            segments.append([])
        else:
            segments[-1].append(token)
    if not segments[-1]:
        return False
    return all(
        _leader_segment_is_read_only(
            segment,
            allow_start=allow_start,
            root=root,
            cwd=cwd,
        )
        for segment in segments
    )


def _leader_segment_is_read_only(
    tokens: list[str],
    *,
    allow_start: bool,
    root: Path | None,
    cwd: Path | None,
) -> bool:
    environment = _leading_environment(tokens)
    tokens = _strip_command_prefixes(tokens)
    if not tokens:
        return False
    command = Path(tokens[0]).name
    args = tokens[1:]
    bare_agent_flow = (
        tokens[0] == command
        and command in {"agent-flow", "agent-flow-kit"}
        and (root is None or not _project_install_metadata_present(root))
    )
    pinned_agent_flow = (
        root is not None
        and cwd is not None
        and _is_authenticated_project_launcher(tokens[0], root=root, cwd=cwd)
    )
    if bare_agent_flow or pinned_agent_flow:
        return (
            not _launcher_environment_is_unsafe(environment)
            and _agent_flow_invocation_is_allowed(
                args,
                allow_start=allow_start,
                in_worktree=False,
            )
        )
    if (
        tokens[0] == command
        and command == "agent-flow-python"
        and (root is None or not _project_install_metadata_present(root))
    ):
        if not args:
            return False
        if args[0] == "status":
            return True
        if args[0] in {"continue", "abort"}:
            return not allow_start
        if args[0] in {"run", "start"}:
            return allow_start
        if args[0] == "worktree":
            return allow_start and len(args) > 1 and args[1] == "create"
        return False
    if command == "git":
        if _unsafe_git_environment(environment):
            return False
        config_values: list[str] = []
        config_env_values: list[str] = []
        index = 0
        while index < len(args) and args[index].startswith("-") and args[index] != "--":
            option = args[index]
            if option in {"--no-pager", "--paginate"}:
                index += 1
                continue
            if option == "-C":
                if index + 1 >= len(args):
                    return False
                index += 2
                continue
            if option == "-c":
                if index + 1 >= len(args):
                    return False
                config_values.append(args[index + 1])
                index += 2
                continue
            if option.startswith("-c") and option != "-c":
                config_values.append(option[2:])
                index += 1
                continue
            if option == "--config-env":
                if index + 1 >= len(args):
                    return False
                config_env_values.append(args[index + 1])
                index += 2
                continue
            if option.startswith("--config-env="):
                config_env_values.append(option.split("=", 1)[1])
                index += 1
                continue
            return False
        if index < len(args) and args[index] == "--":
            index += 1
        args = args[index:]
        cwd = Path.cwd().resolve()
        if _unsafe_git_config(
            config_values,
            config_env_values,
            environment,
            cwd,
            cwd,
        ):
            return False
        if not args or args[0] not in _READ_ONLY_GIT_COMMANDS:
            return False
        if any(value == "--output" or value.startswith("--output=") for value in args[1:]):
            return False
        if args[0] == "branch" and not any(value in {"--show-current", "--list", "-l", "-r", "-a"} for value in args[1:]):
            return len(args) == 1
        if args[0] == "branch" and any(
            value in {"-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy", "--edit-description", "--unset-upstream"}
            or value.startswith("--set-upstream-to")
            for value in args[1:]
        ):
            return False
        if args[0] == "tag" and any(not value.startswith("-") for value in args[1:]):
            return False
        if args[0] == "worktree" and (len(args) < 2 or args[1] != "list"):
            return False
        return True
    if command == "sed":
        return _sed_invocation_is_read_only(args)
    if command == "rg" and any(value == "--pre" or value.startswith("--pre=") for value in args):
        return False
    return command in _READ_ONLY_COMMANDS


def _sed_invocation_is_read_only(args: list[str]) -> bool:
    quiet = False
    expressions: list[str] = []
    positional: list[str] = []
    index = 0
    options = True
    while index < len(args):
        value = args[index]
        if options and value == "--":
            options = False
            index += 1
            continue
        if options and value in {"-n", "--quiet", "--silent"}:
            quiet = True
            index += 1
            continue
        if options and value in {"-e", "--expression"}:
            if index + 1 >= len(args):
                return False
            expressions.append(args[index + 1])
            index += 2
            continue
        if options and value.startswith("--expression="):
            expressions.append(value.split("=", 1)[1])
            index += 1
            continue
        if options and value.startswith("-e") and value != "-e":
            expressions.append(value[2:])
            index += 1
            continue
        if options and value.startswith("-"):
            return False
        positional.append(value)
        index += 1
    if not quiet:
        return False
    if not expressions:
        if not positional:
            return False
        expressions.append(positional.pop(0))
    return all(
        expression and not _sed_expression_has_unsafe_command(expression)
        for expression in expressions
    )


_SED_ADDRESS = r"(?:[0-9]+|\$|/(?:\\.|[^/\n])*/)"
_SED_COMMAND_START = re.compile(
    rf"(?:^|[;\n{{}}])\s*(?:{_SED_ADDRESS}(?:\s*,\s*{_SED_ADDRESS})?\s*)?!?\s*"
)


def _sed_expression_has_unsafe_command(expression: str) -> bool:
    for match in _SED_COMMAND_START.finditer(expression):
        command_index = match.end()
        if command_index >= len(expression):
            continue
        command = expression[command_index]
        if command in "eEwW":
            return True
        if command == "s" and _sed_substitution_has_unsafe_flag(
            expression, command_index
        ):
            return True
    return False


def _sed_substitution_has_unsafe_flag(expression: str, command_index: int) -> bool:
    delimiter_index = command_index + 1
    if delimiter_index >= len(expression):
        return True
    delimiter = expression[delimiter_index]
    if delimiter in {"\\", "\n"} or delimiter.isspace():
        return True
    cursor = delimiter_index + 1
    for _ in range(2):
        escaped = False
        while cursor < len(expression):
            char = expression[cursor]
            cursor += 1
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == delimiter:
                break
        else:
            return True
    while cursor < len(expression) and expression[cursor] not in ";\n":
        char = expression[cursor]
        if char in "eEwW":
            return True
        if not char.isspace() and not char.isdigit() and char not in "gIpM":
            break
        cursor += 1
    return False


def _launcher_environment_is_unsafe(environment: dict[str, str]) -> bool:
    return any(name in _LAUNCHER_UNSAFE_ENVIRONMENT for name in environment)


def _project_install_metadata_present(root: Path) -> bool:
    kit = root / ".agent-flow" / "kit.json"
    try:
        return os.path.lexists(kit)
    except OSError:
        return True


def _managed_project_launcher(root: Path) -> Path | None:
    try:
        return _verify_project_runtime_contract(root)
    except (OSError, RuntimeError):
        return None


def _verify_project_runtime_contract(root: Path) -> Path:
    kit_path = _require_runtime_descendant(
        root,
        ".agent-flow/kit.json",
        kind="file",
        label="installed kit metadata",
    )
    try:
        kit = json.loads(kit_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("installed kit metadata is unreadable") from exc
    if not isinstance(kit, dict):
        raise RuntimeError("installed kit metadata is invalid")
    contract = _normalize_project_runtime_contract(
        kit.get("project_runtime_contract")
    )
    commitment = kit.get("project_runtime_contract_commitment")
    if (
        kit.get("project_runtime_contract_commitment_version")
        != _PROJECT_RUNTIME_COMMITMENT_VERSION
        or not isinstance(commitment, str)
        or _SHA256_RE.fullmatch(commitment) is None
    ):
        raise RuntimeError("installed project runtime commitment is invalid")
    commitment_payload = {
        "version": _PROJECT_RUNTIME_COMMITMENT_VERSION,
        "contract": contract,
    }
    computed_commitment = hashlib.sha256(
        json.dumps(
            commitment_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if computed_commitment != commitment:
        raise RuntimeError(
            "installed project runtime commitment does not match provenance"
        )
    compatibility = kit.get("node_runtime")
    if (
        not isinstance(compatibility, dict)
        or set(compatibility) != {"path", "tree_hash"}
        or compatibility.get("path") != _NODE_RUNTIME_ENTRYPOINT_RELATIVE
        or compatibility.get("tree_hash")
        != contract["node_runtime"]["tree_hash"]
    ):
        raise RuntimeError("installed Node runtime compatibility metadata is invalid")
    python_compatibility = kit.get("python_runtime")
    if (
        not isinstance(python_compatibility, dict)
        or set(python_compatibility) != {"path", "tree_hash"}
        or python_compatibility.get("path") != _PYTHON_RUNTIME_ROOT_RELATIVE
        or python_compatibility.get("tree_hash")
        != contract["python_runtime"]["tree_hash"]
    ):
        raise RuntimeError("installed Python runtime compatibility metadata is invalid")

    launcher = _require_runtime_descendant(
        root,
        contract["launcher"]["path"],
        kind="file",
        label="pinned project launcher",
    )
    if not os.access(launcher, os.X_OK):
        raise RuntimeError("pinned project launcher is not executable")
    if _sha256_file(launcher) != contract["launcher"]["sha256"]:
        raise RuntimeError("pinned project launcher changed after install")
    runtime_root = _require_runtime_descendant(
        root,
        contract["node_runtime"]["root"],
        kind="directory",
        label="pinned Node runtime root",
    )
    _require_runtime_descendant(
        root,
        contract["node_runtime"]["entrypoint"],
        kind="file",
        label="pinned Node runtime entrypoint",
    )
    if _hash_runtime_tree(runtime_root, "pinned Node runtime") != contract["node_runtime"]["tree_hash"]:
        raise RuntimeError("pinned Node runtime changed after install")
    python_runtime_root = _require_runtime_descendant(
        root,
        contract["python_runtime"]["root"],
        kind="directory",
        label="pinned Python runtime root",
    )
    if (
        _hash_runtime_tree(python_runtime_root, "pinned Python runtime")
        != contract["python_runtime"]["tree_hash"]
    ):
        raise RuntimeError("pinned Python runtime changed after install")
    return launcher


def _normalize_project_runtime_contract(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "version",
        "launcher",
        "node_runtime",
        "python_runtime",
    }:
        raise RuntimeError("installed project runtime contract is invalid")
    launcher = value.get("launcher")
    node_runtime = value.get("node_runtime")
    python_runtime = value.get("python_runtime")
    if (
        value.get("version") != _PROJECT_RUNTIME_CONTRACT_VERSION
        or not isinstance(launcher, dict)
        or set(launcher) != {"path", "sha256"}
        or launcher.get("path") != _PROJECT_LAUNCHER_RELATIVE
        or not isinstance(launcher.get("sha256"), str)
        or _SHA256_RE.fullmatch(launcher["sha256"]) is None
        or not isinstance(node_runtime, dict)
        or set(node_runtime) != {"root", "entrypoint", "tree_hash"}
        or node_runtime.get("root") != _NODE_RUNTIME_ROOT_RELATIVE
        or node_runtime.get("entrypoint") != _NODE_RUNTIME_ENTRYPOINT_RELATIVE
        or not isinstance(node_runtime.get("tree_hash"), str)
        or _SHA256_RE.fullmatch(node_runtime["tree_hash"]) is None
        or not isinstance(python_runtime, dict)
        or set(python_runtime) != {"root", "tree_hash"}
        or python_runtime.get("root") != _PYTHON_RUNTIME_ROOT_RELATIVE
        or not isinstance(python_runtime.get("tree_hash"), str)
        or _SHA256_RE.fullmatch(python_runtime["tree_hash"]) is None
    ):
        raise RuntimeError("installed project runtime contract is invalid")
    return {
        "version": _PROJECT_RUNTIME_CONTRACT_VERSION,
        "launcher": {
            "path": _PROJECT_LAUNCHER_RELATIVE,
            "sha256": launcher["sha256"],
        },
        "node_runtime": {
            "root": _NODE_RUNTIME_ROOT_RELATIVE,
            "entrypoint": _NODE_RUNTIME_ENTRYPOINT_RELATIVE,
            "tree_hash": node_runtime["tree_hash"],
        },
        "python_runtime": {
            "root": _PYTHON_RUNTIME_ROOT_RELATIVE,
            "tree_hash": python_runtime["tree_hash"],
        },
    }


def _require_runtime_descendant(
    root: Path,
    relative: str,
    *,
    kind: str,
    label: str,
) -> Path:
    project_root = root.resolve(strict=True)
    cursor = project_root
    parts = relative.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise RuntimeError(f"{label} path is invalid")
    for index, part in enumerate(parts):
        cursor /= part
        try:
            mode = cursor.lstat().st_mode
        except OSError as exc:
            raise RuntimeError(f"{label} is unavailable") from exc
        if stat.S_ISLNK(mode):
            raise RuntimeError(f"{label} path may not use symlinks")
        final = index == len(parts) - 1
        valid = stat.S_ISREG(mode) if final and kind == "file" else stat.S_ISDIR(mode)
        if not valid:
            raise RuntimeError(f"{label} path has an invalid component")
        if final and kind == "file" and cursor.lstat().st_nlink != 1:
            raise RuntimeError(f"{label} may not be hard-linked")
    try:
        cursor.relative_to(project_root)
    except ValueError as exc:
        raise RuntimeError(f"{label} escapes the project") from exc
    return cursor


def _sha256_file(file: Path) -> str:
    digest = hashlib.sha256()
    with file.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _hash_runtime_tree(root: Path, label: str) -> str:
    files: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(directory.iterdir())
        except OSError as exc:
            raise RuntimeError(f"{label} is unreadable") from exc
        for entry in entries:
            try:
                mode = entry.lstat().st_mode
            except OSError as exc:
                raise RuntimeError(f"{label} is unreadable") from exc
            if stat.S_ISLNK(mode):
                raise RuntimeError(f"{label} may not contain symlinks")
            if stat.S_ISDIR(mode):
                pending.append(entry)
            elif stat.S_ISREG(mode):
                if entry.lstat().st_nlink != 1:
                    raise RuntimeError(f"{label} may not contain hard-linked files")
                files.append(entry)
            else:
                raise RuntimeError(
                    f"{label} may contain only regular files"
                )
    digest = hashlib.sha256()
    for file in sorted(files, key=lambda candidate: candidate.as_posix()):
        digest.update(file.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(file.read_bytes())
        except OSError as exc:
            raise RuntimeError(f"{label} is unreadable") from exc
        digest.update(b"\0")
    return digest.hexdigest()


def _is_authenticated_project_launcher(raw_command: str, *, root: Path, cwd: Path) -> bool:
    if not raw_command or "/" not in raw_command or "$" in raw_command or "`" in raw_command:
        return False
    managed = _managed_project_launcher(root)
    if managed is None:
        return False
    try:
        candidate = _resolve_shell_path(raw_command, cwd).resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return candidate == managed


def _agent_flow_invocation_is_allowed(
    args: list[str],
    *,
    allow_start: bool,
    in_worktree: bool,
) -> bool:
    if not args:
        return False
    command = args[0]
    if command == "status":
        return True
    if command == "install":
        return allow_start and not in_worktree
    if command == "run":
        if len(args) < 2:
            return False
        subcommand = args[1]
        if subcommand in {"status", "next", "advance"}:
            return True
        if subcommand in {"push-watch", "push-watch-tick"}:
            return in_worktree
        if subcommand == "install":
            return allow_start and not in_worktree
        return allow_start
    return in_worktree and command in {"architecture-lint", "experiment", "gates"}


def _worktree_bash_is_safe(
    command: str,
    *,
    root: Path,
    workspace: Path,
    cwd: Path,
    control_root: Path | None,
    depth: int = 0,
    inspected: frozenset[str] = frozenset(),
) -> bool:
    if not command.strip() or depth > _MAX_SCRIPT_DEPTH or "`" in command or "$(" in command:
        return False
    try:
        lexer = shlex.shlex(
            command.replace("\r", "\n").replace("\n", ";"),
            posix=True,
            punctuation_chars=";&|<>",
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return False
    current_cwd = cwd
    segment: list[str] = []
    for token in tokens + [";"]:
        if token in _SHELL_SEPARATORS or (token and all(char in ";&|" for char in token)):
            if segment:
                safe, current_cwd = _worktree_segment_is_safe(
                    segment,
                    root=root,
                    workspace=workspace,
                    cwd=current_cwd,
                    control_root=control_root,
                    depth=depth,
                    inspected=inspected,
                )
                if not safe:
                    return False
                segment = []
            continue
        segment.append(token)
    return not segment


def _worktree_segment_is_safe(
    tokens: list[str],
    *,
    root: Path,
    workspace: Path,
    cwd: Path,
    control_root: Path | None,
    depth: int,
    inspected: frozenset[str],
) -> tuple[bool, Path]:
    command_tokens: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _OUTPUT_REDIRECT_OPERATORS:
            if command_tokens and command_tokens[-1].isdigit():
                command_tokens.pop()
            if index + 1 >= len(tokens):
                return False, cwd
            target = tokens[index + 1]
            duplicates_fd = token in {">&", ">&|"} and (target.isdigit() or target == "-")
            if not duplicates_fd and _write_target_is_outside(
                target,
                cwd,
                workspace,
                control_root,
            ):
                return False, cwd
            index += 2
            continue
        if token in _INPUT_REDIRECT_OPERATORS:
            if command_tokens and command_tokens[-1].isdigit():
                command_tokens.pop()
            if index + 1 >= len(tokens):
                return False, cwd
            index += 2
            continue
        command_tokens.append(token)
        index += 1
    if not command_tokens:
        return True, cwd
    if not _git_path_assignments_are_safe(command_tokens, cwd, workspace):
        return False, cwd
    environment = _leading_environment(command_tokens)
    command_tokens = _strip_command_prefixes(command_tokens)
    if not command_tokens:
        return True, cwd
    launcher_environment_unsafe = (
        _launcher_environment_is_unsafe(environment)
        or _UNSAFE_LAUNCHER_ENVIRONMENT_MARKER in inspected
    )
    launcher_inspected = (
        inspected | {_UNSAFE_LAUNCHER_ENVIRONMENT_MARKER}
        if launcher_environment_unsafe
        else inspected
    )
    command_name = Path(command_tokens[0]).name
    args = command_tokens[1:]
    bare_agent_flow = (
        command_tokens[0] == command_name
        and command_name in {"agent-flow", "agent-flow-kit"}
        and not _project_install_metadata_present(root)
    )
    pinned_agent_flow = _is_authenticated_project_launcher(
        command_tokens[0],
        root=root,
        cwd=cwd,
    )
    if bare_agent_flow or pinned_agent_flow:
        return (
            not launcher_environment_unsafe
            and _agent_flow_invocation_is_allowed(
                args,
                allow_start=control_root is None,
                in_worktree=True,
            ),
            cwd,
        )
    if command_tokens[0] == command_name and command_name in {"agent-flow", "agent-flow-kit"}:
        return False, cwd
    if "/" in command_tokens[0] and command_name in {"agent-flow", "agent-flow-kit"}:
        return False, cwd
    if command_name == "agent-flow-python":
        if _project_install_metadata_present(root) or command_tokens[0] != command_name:
            return False, cwd
        if not args:
            return False, cwd
        if args[0] == "status":
            return True, cwd
        if args[0] in {"continue", "abort"}:
            return control_root is not None, cwd
        if args[0] in {"run", "start"}:
            return control_root is None, cwd
        if args[0] == "worktree":
            return control_root is None and len(args) > 1 and args[1] == "create", cwd
        return False, cwd
    if command_name in _SHELL_CONTROL_COMMANDS:
        return False, cwd
    wrapped = _static_process_wrapper_command(command_name, args)
    if wrapped is not None:
        if not wrapped or depth >= _MAX_SCRIPT_DEPTH:
            return False, cwd
        return _worktree_segment_is_safe(
            wrapped,
            root=root,
            workspace=workspace,
            cwd=cwd,
            control_root=control_root,
            depth=depth + 1,
            inspected=launcher_inspected,
        )
    if command_name in _DYNAMIC_EXECUTOR_COMMANDS:
        return False, cwd
    if command_name in _STANDALONE_SHARED_REF_MUTATORS:
        return False, cwd
    if command_name == "find" and any(
        value in {"-delete", "-exec", "-execdir", "-ok", "-okdir"}
        for value in args
    ):
        return False, cwd
    if command_name in {"builtin", "command", "env", "eval", "exec"}:
        return False, cwd
    if _inline_interpreter_is_unsafe(command_name, args):
        return False, cwd
    if command_name in {"cd", "pushd"}:
        if not args or args[0].startswith("-") or "$" in args[0]:
            return False, cwd
        destination = _resolve_shell_path(args[0], cwd)
        return (_path_is_within(destination, workspace), destination)
    if command_name == "popd":
        return False, cwd
    if command_name in {"bun", "npm", "pnpm", "yarn"}:
        package_safe = _package_manager_command_is_safe(
            command_name,
            args,
            root=root,
            workspace=workspace,
            cwd=cwd,
            control_root=control_root,
            depth=depth,
            inspected=launcher_inspected,
        )
        if package_safe is not None:
            return package_safe, cwd
    interpreter_safe = _interpreter_command_is_safe(
        command_name,
        args,
        root=root,
        workspace=workspace,
        cwd=cwd,
        control_root=control_root,
        depth=depth,
        inspected=launcher_inspected,
    )
    if interpreter_safe is not None:
        return interpreter_safe, cwd
    if command_name in {"gmake", "make"}:
        return (
            _make_command_is_safe(
                args,
                root=root,
                workspace=workspace,
                cwd=cwd,
                control_root=control_root,
                depth=depth,
                inspected=launcher_inspected,
            ),
            cwd,
        )
    awk_safe = _awk_command_is_safe(
        command_name,
        args,
        workspace=workspace,
        cwd=cwd,
    )
    if awk_safe is not None:
        return awk_safe, cwd
    if command_tokens[0] == "." or command_name == "source":
        script = next((value for value in args if not value.startswith("-")), None)
        if script is None:
            return False, cwd
        return (
            _script_file_is_safe(
                script,
                language="shell",
                root=root,
                workspace=workspace,
                cwd=cwd,
                control_root=control_root,
                depth=depth + 1,
                inspected=launcher_inspected,
            ),
            cwd,
        )
    if command_name in _SHELL_COMMANDS:
        nested = _nested_shell_payload(args)
        if nested is not None:
            return (
                _worktree_bash_is_safe(
                    nested,
                    root=root,
                    workspace=workspace,
                    cwd=cwd,
                    control_root=control_root,
                    depth=depth + 1,
                    inspected=launcher_inspected,
                ),
                cwd,
            )
        script = next((value for value in args if not value.startswith("-")), None)
        if script is None:
            return False, cwd
        return (
            _script_file_is_safe(
                script,
                language="shell",
                root=root,
                workspace=workspace,
                cwd=cwd,
                control_root=control_root,
                depth=depth + 1,
                inspected=launcher_inspected,
            ),
            cwd,
        )
    direct_script = _direct_local_script(command_tokens[0], environment.get("PATH"), cwd)
    if direct_script is not None:
        script_path, language = direct_script
        return (
            _script_file_is_safe(
                str(script_path),
                language=language,
                root=root,
                workspace=workspace,
                cwd=cwd,
                control_root=control_root,
                depth=depth + 1,
                inspected=launcher_inspected,
            ),
            cwd,
        )
    if command_name == "git":
        if _unsafe_git_environment(environment):
            return False, cwd
        return _worktree_git_is_safe(
            args,
            cwd,
            workspace,
            control_root,
            environment,
        ), cwd
    if command_name == "ln":
        targets = _ln_write_paths(args)
        return (
            targets is not None
            and all(
                not _write_target_is_outside(target, cwd, workspace, control_root)
                for target in targets
            ),
            cwd,
        )
    if command_name in _ALL_TARGET_COMMANDS:
        targets = _non_option_operands(args)
        return (
            bool(targets)
            and all(not _write_target_is_outside(target, cwd, workspace, control_root) for target in targets),
            cwd,
        )
    if command_name in _DESTINATION_COMMANDS:
        destination = _destination_operand(args)
        return (
            destination is not None
            and not _write_target_is_outside(destination, cwd, workspace, control_root),
            cwd,
        )
    if command_name == "mv":
        targets = _non_option_operands(args)
        return (
            len(targets) >= 2
            and all(not _write_target_is_outside(target, cwd, workspace, control_root) for target in targets),
            cwd,
        )
    if command_name == "tee":
        targets = _non_option_operands(args)
        return all(not _write_target_is_outside(target, cwd, workspace, control_root) for target in targets), cwd
    if command_name == "dd":
        outputs = [value.removeprefix("of=") for value in args if value.startswith("of=")]
        return all(not _write_target_is_outside(target, cwd, workspace, control_root) for target in outputs), cwd
    if command_name == "sed" and any(value == "-i" or value.startswith("-i") for value in args):
        targets = _non_option_operands(args)
        return all(not _write_target_is_outside(target, cwd, workspace, control_root) for target in targets), cwd
    if command_name in _READ_ONLY_COMMANDS:
        return True, cwd
    return not any(
        _token_references_outside_path(token, cwd, workspace, control_root)
        for token in command_tokens
    ), cwd


def _static_process_wrapper_command(
    command_name: str,
    args: list[str],
) -> list[str] | None:
    if command_name not in _STATIC_PROCESS_WRAPPERS:
        return None
    index = 0
    options_with_values: set[str] = set()
    if command_name == "nice":
        options_with_values = {"--adjustment", "-n"}
    elif command_name == "timeout":
        options_with_values = {"--kill-after", "--signal", "-k", "-s"}
    elif command_name == "time":
        options_with_values = {"--format", "--output", "-f", "-o"}
    elif command_name == "stdbuf":
        options_with_values = {"--error", "--input", "--output", "-e", "-i", "-o"}
    elif command_name == "ionice":
        options_with_values = {
            "--class", "--classdata", "--pgid", "--pid", "--uid",
            "-c", "-n", "-P", "-p", "-u",
        }
    while index < len(args):
        value = args[index]
        if command_name == "time" and (
            value in {"--output", "-o"}
            or value.startswith("--output=")
            or (value.startswith("-o") and value != "-o")
        ):
            return []
        if value == "--":
            index += 1
            break
        if value in options_with_values:
            if index + 1 >= len(args):
                return []
            index += 2
            continue
        if value.startswith("--") and "=" in value:
            index += 1
            continue
        if value.startswith("-") and value != "-":
            index += 1
            continue
        break
    if command_name == "timeout":
        if index >= len(args):
            return []
        index += 1
    return args[index:]


def _inline_interpreter_is_unsafe(command_name: str, args: list[str]) -> bool:
    normalized = command_name.lower()
    if re.fullmatch(r"(?:python|pypy)(?:\d+(?:\.\d+)*)?", normalized):
        return any(
            value == "-c"
            or value.startswith("-c")
            or bool(re.match(r"^-[A-Za-z]+c", value))
            for value in args
        )
    if re.fullmatch(r"(?:node|ruby|perl|php|lua|julia|swift)(?:\d+(?:\.\d+)*)?", normalized):
        normalized = re.sub(r"\d+(?:\.\d+)*$", "", normalized)
    flags = _INLINE_INTERPRETER_FLAGS.get(normalized)
    if not flags:
        return False
    for value in args:
        if (
            normalized in {"perl", "ruby"}
            and value.startswith("-")
            and not value.startswith("--")
            and any(flag in value[1:] for flag in ("e", "E"))
        ):
            return True
        if value in flags:
            return True
        if any(value.startswith(f"{flag}=") for flag in flags if flag.startswith("--")):
            return True
        if any(
            value.startswith(flag) and value != flag
            for flag in flags
            if flag.startswith("-") and not flag.startswith("--")
        ):
            return True
    return False


def _awk_command_is_safe(
    command_name: str,
    args: list[str],
    *,
    workspace: Path,
    cwd: Path,
) -> bool | None:
    if command_name not in {"awk", "gawk", "mawk", "nawk"}:
        return None
    sources: list[str] = []
    inline_program: str | None = None
    index = 0
    while index < len(args):
        value = args[index]
        if value == "--":
            if index + 1 >= len(args):
                return False
            inline_program = args[index + 1]
            break
        if value in {"-E", "--exec", "-f", "--file"}:
            if index + 1 >= len(args):
                return False
            source = _read_script_text(_resolve_shell_path(args[index + 1], cwd), workspace)
            if source is None:
                return False
            sources.append(source)
            index += 2
            continue
        if value.startswith(("--exec=", "--file=")):
            source = _read_script_text(
                _resolve_shell_path(value.split("=", 1)[1], cwd),
                workspace,
            )
            if source is None:
                return False
            sources.append(source)
            index += 1
            continue
        if value in {"-e", "--source"}:
            if index + 1 >= len(args):
                return False
            sources.append(args[index + 1])
            index += 2
            continue
        if value.startswith("--source="):
            sources.append(value.split("=", 1)[1])
            index += 1
            continue
        if value == "-W":
            if index + 1 >= len(args):
                return False
            option = args[index + 1]
            if option == "exec":
                if index + 2 >= len(args):
                    return False
                raw_source = args[index + 2]
                index += 3
            elif option.startswith("exec="):
                raw_source = option.split("=", 1)[1]
                index += 2
            else:
                index += 2
                continue
            source = _read_script_text(_resolve_shell_path(raw_source, cwd), workspace)
            if source is None:
                return False
            sources.append(source)
            continue
        if value.startswith("-Wexec="):
            source = _read_script_text(
                _resolve_shell_path(value.split("=", 1)[1], cwd),
                workspace,
            )
            if source is None:
                return False
            sources.append(source)
            index += 1
            continue
        if value in {"-F", "-v", "--assign", "--field-separator"}:
            if index + 1 >= len(args):
                return False
            index += 2
            continue
        if value.startswith(("-F", "-v", "--assign=", "--field-separator=")):
            index += 1
            continue
        if value.startswith("-"):
            index += 1
            continue
        if not sources:
            inline_program = value
        break
    if inline_program is not None:
        sources.append(inline_program)
    if not sources:
        return False
    return all(not _awk_source_can_write(source) for source in sources)


def _awk_source_can_write(source: str) -> bool:
    if re.search(r"(?:^|\W)@[A-Za-z_]", source):
        return True
    if re.search(r"\b(?:close|system)\s*\(", source):
        return True
    if re.search(r"(?<!\|)\|(?!\|)", source):
        return True
    return bool(
        re.search(
            r"\b(?:print|printf)\b[^;}\n]*(?:>>?|\|&?)",
            source,
            re.DOTALL,
        )
    )


def _leading_environment(tokens: list[str]) -> dict[str, str]:
    _, environment = _command_prefix_info(tokens)
    return environment


def _interpreter_command_is_safe(
    command_name: str,
    args: list[str],
    *,
    root: Path,
    workspace: Path,
    cwd: Path,
    control_root: Path | None,
    depth: int,
    inspected: frozenset[str],
) -> bool | None:
    normalized = command_name.lower()
    if not _SCRIPT_INTERPRETER_RE.fullmatch(normalized):
        return None
    interpreter = re.sub(r"\d+(?:\.\d+)*$", "", normalized)
    if args and all(value in {"-h", "--help", "-v", "-V", "--version"} for value in args):
        return True
    if interpreter in {"python", "pypy"}:
        for index, value in enumerate(args):
            if value == "-m":
                if index + 1 >= len(args) or args[index + 1] not in {"pytest", "unittest"}:
                    return False
                return not any(
                    _token_references_outside_path(item, cwd, workspace, control_root)
                    for item in args[index + 2 :]
                )
            if value.startswith("-m") and value != "-m":
                return False
    if interpreter == "node" and any(
        value in {"-r", "--import", "--loader", "--require"}
        or value.startswith(("--import=", "--loader=", "--require="))
        for value in args
    ):
        return False
    if interpreter == "node" and any(value in {"-c", "--check"} for value in args):
        return True
    script = _interpreter_script_operand(interpreter, args)
    if script is None:
        return False
    language = (
        "python"
        if interpreter in {"python", "pypy"}
        else "node"
        if interpreter in {"bun", "deno", "node"}
        else "generic"
    )
    return _script_file_is_safe(
        script,
        language=language,
        root=root,
        workspace=workspace,
        cwd=cwd,
        control_root=control_root,
        depth=depth + 1,
        inspected=inspected,
    )


def _interpreter_script_operand(interpreter: str, args: list[str]) -> str | None:
    options_with_values = {
        "python": {"-W", "-X", "--check-hash-based-pycs"},
        "pypy": {"-W", "-X", "--check-hash-based-pycs"},
        "node": {
            "-r",
            "--conditions",
            "--diagnostic-dir",
            "--import",
            "--loader",
            "--openssl-config",
            "--redirect-warnings",
            "--require",
            "--title",
        },
        "ruby": {"-C", "-E", "-I", "-K", "-r"},
        "perl": {"-F", "-I", "-M", "-m", "-x"},
        "php": {"-c", "-d", "-f"},
        "lua": {"-l"},
        "julia": {"-J", "--project", "--sysimage"},
        "swift": {"-I", "-L", "-l", "-module-cache-path", "-sdk"},
        "deno": {"--config", "--import-map", "--location", "--lock", "--node-modules-dir"},
        "bun": {"--cwd", "--preload"},
    }
    needs_value = options_with_values.get(interpreter, set())
    if interpreter == "deno":
        if not args or args[0] != "run":
            return None
        args = args[1:]
    elif interpreter == "bun" and args and args[0] == "run":
        args = args[1:]
    index = 0
    while index < len(args):
        value = args[index]
        if value == "--":
            return args[index + 1] if index + 1 < len(args) else None
        if interpreter == "php" and value == "-f":
            return args[index + 1] if index + 1 < len(args) else None
        if value in needs_value:
            index += 2
            continue
        if any(value.startswith(f"{option}=") for option in needs_value if option.startswith("--")):
            index += 1
            continue
        if value.startswith("-"):
            index += 1
            continue
        return value
    return None


def _package_manager_command_is_safe(
    manager: str,
    args: list[str],
    *,
    root: Path,
    workspace: Path,
    cwd: Path,
    control_root: Path | None,
    depth: int,
    inspected: frozenset[str],
) -> bool | None:
    package_cwd = cwd
    remaining: list[str] = []
    cwd_options = {"--cwd", "-C"}
    if manager in {"npm", "pnpm"}:
        cwd_options.add("--prefix")
    if manager == "pnpm":
        cwd_options.add("--dir")
    index = 0
    while index < len(args):
        value = args[index]
        if value in cwd_options:
            if index + 1 >= len(args):
                return False
            package_cwd = _resolve_shell_path(args[index + 1], cwd)
            index += 2
            continue
        if "-C" in cwd_options and value.startswith("-C") and value != "-C":
            compact_cwd = value[2:].removeprefix("=")
            if not compact_cwd:
                return False
            package_cwd = _resolve_shell_path(compact_cwd, cwd)
            index += 1
            continue
        if any(value.startswith(f"{option}=") for option in cwd_options if option.startswith("--")):
            package_cwd = _resolve_shell_path(value.split("=", 1)[1], cwd)
            index += 1
            continue
        remaining.append(value)
        index += 1
    if not _path_is_within(package_cwd, workspace):
        return False
    if any(
        _token_references_outside_path(value, package_cwd, workspace, control_root)
        for value in remaining
    ):
        return False
    command_index = next(
        (position for position, value in enumerate(remaining) if not value.startswith("-")),
        None,
    )
    if command_index is None:
        return None
    command = remaining[command_index]
    trailing = remaining[command_index + 1 :]
    if command in {"run", "run-script"}:
        script_name = next((value for value in trailing if value != "--" and not value.startswith("-")), None)
    elif manager == "npm" and command in {"start", "stop", "test"}:
        script_name = command
    else:
        script_name = command if manager in {"pnpm", "yarn"} else None
    if script_name is None:
        return None
    package_json = _nearest_package_json(package_cwd, workspace)
    if package_json is None:
        return True if command in {"run", "run-script", "start", "stop", "test"} else None
    payload = _read_json_file(package_json, workspace)
    if payload is None:
        return False
    scripts = payload.get("scripts", {})
    if not isinstance(scripts, dict):
        return False
    lifecycle = (f"pre{script_name}", script_name, f"post{script_name}")
    if script_name not in scripts and all(name not in scripts for name in lifecycle):
        return True if command in {"run", "run-script", "start", "stop", "test"} else None
    for lifecycle_name in lifecycle:
        script = scripts.get(lifecycle_name)
        if script is None:
            continue
        if not isinstance(script, str) or not script.strip():
            return False
        key = f"package:{package_json.resolve()}#{lifecycle_name}"
        if key in inspected:
            return False
        if not _worktree_bash_is_safe(
            script,
            root=root,
            workspace=workspace,
            cwd=package_json.parent,
            control_root=control_root,
            depth=depth + 1,
            inspected=inspected | {key},
        ):
            return False
    return True


def _make_command_is_safe(
    args: list[str],
    *,
    root: Path,
    workspace: Path,
    cwd: Path,
    control_root: Path | None,
    depth: int,
    inspected: frozenset[str],
) -> bool:
    make_cwd = cwd
    makefile: Path | None = None
    targets: list[str] = []
    index = 0
    while index < len(args):
        value = args[index]
        if value in {"-C", "--directory"}:
            if index + 1 >= len(args):
                return False
            make_cwd = _resolve_shell_path(args[index + 1], make_cwd)
            index += 2
            continue
        if value.startswith("-C") and value != "-C":
            compact_cwd = value[2:].removeprefix("=")
            if not compact_cwd:
                return False
            make_cwd = _resolve_shell_path(compact_cwd, make_cwd)
            index += 1
            continue
        if value.startswith("--directory="):
            make_cwd = _resolve_shell_path(value.split("=", 1)[1], make_cwd)
            index += 1
            continue
        if value in {"-f", "--file", "--makefile"}:
            if index + 1 >= len(args):
                return False
            makefile = _resolve_shell_path(args[index + 1], make_cwd)
            index += 2
            continue
        if value.startswith("-f") and value != "-f":
            compact_file = value[2:].removeprefix("=")
            if not compact_file:
                return False
            makefile = _resolve_shell_path(compact_file, make_cwd)
            index += 1
            continue
        if value.startswith(("--file=", "--makefile=")):
            makefile = _resolve_shell_path(value.split("=", 1)[1], make_cwd)
            index += 1
            continue
        if value in {"--eval", "-E"} or value.startswith("--eval="):
            return False
        if value in {"-j", "--jobs", "-l", "--load-average"}:
            if index + 1 < len(args) and re.fullmatch(r"\d+(?:\.\d+)?", args[index + 1]):
                index += 2
            else:
                index += 1
            continue
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", value):
            if _token_references_outside_path(value, make_cwd, workspace, control_root):
                return False
            index += 1
            continue
        if value == "--":
            targets.extend(args[index + 1 :])
            break
        if value.startswith("-"):
            index += 1
            continue
        targets.append(value)
        index += 1
    if not _path_is_within(make_cwd, workspace):
        return False
    if makefile is None:
        makefile = next(
            (
                make_cwd / name
                for name in ("GNUmakefile", "makefile", "Makefile")
                if (make_cwd / name).exists() or (make_cwd / name).is_symlink()
            ),
            None,
        )
    if makefile is None:
        return False
    loaded = _load_make_rules(makefile, workspace, frozenset())
    if loaded is None:
        return False
    rules, order, resolved_makefile = loaded
    selected = targets or next(
        (
            [name]
            for name in order
            if not name.startswith(".") and "%" not in name
        ),
        [],
    )
    if not selected:
        return False
    return all(
        _make_target_is_safe(
            target,
            rules=rules,
            makefile=resolved_makefile,
            root=root,
            workspace=workspace,
            cwd=make_cwd,
            control_root=control_root,
            depth=depth + 1,
            inspected=inspected,
        )
        for target in selected
    )


def _load_make_rules(
    makefile: Path,
    workspace: Path,
    seen: frozenset[str],
) -> tuple[dict[str, dict[str, list[str]]], list[str], Path] | None:
    try:
        resolved = makefile.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not _path_is_within(resolved, workspace):
        return None
    key = str(resolved)
    if key in seen:
        return None
    source = _read_script_text(resolved, workspace)
    if source is None or "$(shell" in source or re.search(r"^[^#\n]*!=", source, re.MULTILINE):
        return None
    rules: dict[str, dict[str, list[str]]] = {}
    order: list[str] = []
    current_targets: list[str] = []
    for raw_line in source.splitlines():
        if raw_line.startswith("\t"):
            if not current_targets:
                return None
            recipe = raw_line[1:].lstrip("@-+").strip()
            if recipe:
                for target in current_targets:
                    rules[target]["recipes"].append(recipe)
            continue
        current_targets = []
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        include = re.fullmatch(r"-?include\s+(.+)", line)
        if include:
            for raw_include in shlex.split(include.group(1)):
                if "$" in raw_include or "`" in raw_include:
                    return None
                nested = _load_make_rules(
                    _resolve_shell_path(raw_include, resolved.parent),
                    workspace,
                    seen | {key},
                )
                if nested is None:
                    return None
                nested_rules, nested_order, _ = nested
                for target, details in nested_rules.items():
                    if target not in rules:
                        rules[target] = {"deps": [], "recipes": []}
                        order.append(target)
                    rules[target]["deps"].extend(details["deps"])
                    rules[target]["recipes"].extend(details["recipes"])
                order.extend(name for name in nested_order if name not in order)
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_.-]*\s*(?::=|\?=|\+=|=)", line):
            continue
        target_text, separator, remainder = line.partition(":")
        if not separator or not target_text.strip():
            return None
        dependency_text, inline_separator, inline_recipe = remainder.partition(";")
        current_targets = target_text.split()
        dependencies = [value for value in dependency_text.split() if value != "|"]
        for target in current_targets:
            if "$" in target or "`" in target:
                return None
            if target not in rules:
                rules[target] = {"deps": [], "recipes": []}
                order.append(target)
            rules[target]["deps"].extend(dependencies)
            if inline_separator and inline_recipe.strip():
                rules[target]["recipes"].append(inline_recipe.strip())
    return rules, order, resolved


def _make_target_is_safe(
    target: str,
    *,
    rules: dict[str, dict[str, list[str]]],
    makefile: Path,
    root: Path,
    workspace: Path,
    cwd: Path,
    control_root: Path | None,
    depth: int,
    inspected: frozenset[str],
) -> bool:
    if depth > _MAX_SCRIPT_DEPTH or target not in rules:
        return False
    key = f"make:{makefile}#{target}"
    if key in inspected:
        return False
    next_inspected = inspected | {key}
    details = rules[target]
    for dependency in details["deps"]:
        if "$" in dependency or "`" in dependency:
            return False
        if dependency in rules and not _make_target_is_safe(
            dependency,
            rules=rules,
            makefile=makefile,
            root=root,
            workspace=workspace,
            cwd=cwd,
            control_root=control_root,
            depth=depth + 1,
            inspected=next_inspected,
        ):
            return False
    return all(
        _worktree_bash_is_safe(
            recipe,
            root=root,
            workspace=workspace,
            cwd=cwd,
            control_root=control_root,
            depth=depth + 1,
            inspected=next_inspected,
        )
        for recipe in details["recipes"]
    )


def _nearest_package_json(cwd: Path, workspace: Path) -> Path | None:
    current = cwd
    while _path_is_within(current, workspace):
        candidate = current / "package.json"
        if candidate.exists() or candidate.is_symlink():
            return candidate
        if current == workspace:
            break
        current = current.parent
    return None


def _read_json_file(path: Path, workspace: Path) -> dict | None:
    text = _read_script_text(path, workspace)
    if text is None:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _direct_local_script(
    raw_command: str,
    path_override: str | None,
    cwd: Path,
) -> tuple[Path, str] | None:
    candidates: list[Path] = []
    if "/" in raw_command:
        candidates.append(_resolve_shell_path(raw_command, cwd))
    elif path_override is not None:
        for entry in path_override.split(os.pathsep):
            if entry in {"$PATH", "${PATH}"}:
                continue
            if entry in {"$PWD", "${PWD}"}:
                directory = cwd
            elif entry.startswith("$PWD/"):
                directory = cwd / entry.removeprefix("$PWD/")
            elif entry.startswith("${PWD}/"):
                directory = cwd / entry.removeprefix("${PWD}/")
            elif "$" in entry or "`" in entry:
                continue
            else:
                directory = cwd if not entry else _resolve_shell_path(entry, cwd)
            candidates.append(directory / raw_command)
    for candidate in candidates:
        if candidate.exists() or candidate.is_symlink() or "/" in raw_command:
            return candidate, _script_language(candidate)
    return None


def _script_language(path: Path) -> str:
    if path.suffix == ".py":
        return "python"
    if path.suffix in {".js", ".cjs", ".mjs"}:
        return "node"
    return "shell"


def _script_file_is_safe(
    raw_script: str,
    *,
    language: str,
    root: Path,
    workspace: Path,
    cwd: Path,
    control_root: Path | None,
    depth: int,
    inspected: frozenset[str],
) -> bool:
    if depth > _MAX_SCRIPT_DEPTH or not raw_script or "$" in raw_script or "`" in raw_script:
        return False
    candidate = _resolve_shell_path(raw_script, cwd)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    if not _path_is_within(resolved, workspace):
        return False
    key = f"file:{resolved}"
    if key in inspected:
        return False
    source = _read_script_text(resolved, workspace)
    if source is None:
        return False
    next_inspected = inspected | {key}
    if language == "shell":
        if resolved.name in {"gradlew", "mvnw"}:
            return not _shell_wrapper_has_literal_outside_write(
                source,
                cwd,
                workspace,
                control_root,
            )
        shell_source = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        return bool(shell_source.strip()) and _worktree_bash_is_safe(
            shell_source,
            root=root,
            workspace=workspace,
            cwd=cwd,
            control_root=control_root,
            depth=depth,
            inspected=next_inspected,
        )
    if language == "python":
        return _python_script_is_safe(
            source,
            root=root,
            workspace=workspace,
            cwd=cwd,
            control_root=control_root,
            depth=depth,
            inspected=next_inspected,
        )
    if language == "node":
        return _node_script_is_safe(
            source,
            root=root,
            workspace=workspace,
            cwd=cwd,
            control_root=control_root,
            depth=depth,
            inspected=next_inspected,
        )
    return _generic_script_is_safe(source, cwd, workspace, control_root)


def _shell_wrapper_has_literal_outside_write(
    source: str,
    cwd: Path,
    workspace: Path,
    control_root: Path | None,
) -> bool:
    mutating = re.compile(
        r"(?:^|[;&|])\s*(?:chmod|chown|cp|dd|install|ln|mkdir|mv|rm|rmdir|rsync|"
        r"sed\s+-[^\s]*i|tee|touch|truncate)\b"
    )
    literal_path = re.compile(r"(?<![A-Za-z0-9_$])((?:/|(?:\.\./)+)[^\s'\";|&)]+)")
    for line in source.splitlines():
        if not mutating.search(line) and not re.search(r"(?:^|[^<])>>?\s*[^&]", line):
            continue
        for match in literal_path.finditer(line):
            if _write_target_is_outside(match.group(1), cwd, workspace, control_root):
                return True
    return False


def _read_script_text(path: Path, workspace: Path) -> str | None:
    try:
        resolved = path.resolve(strict=True)
        if not _path_is_within(resolved, workspace) or not resolved.is_file():
            return None
        if resolved.stat().st_size > _MAX_SCRIPT_BYTES:
            return None
        return resolved.read_text(encoding="utf-8")
    except (OSError, RuntimeError, UnicodeError):
        return None


def _python_script_is_safe(
    source: str,
    *,
    root: Path,
    workspace: Path,
    cwd: Path,
    control_root: Path | None,
    depth: int,
    inspected: frozenset[str],
) -> bool:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value_node = node.value
            value = _python_path_literal(value_node, bindings, cwd)
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if value is not None:
                for target in targets:
                    if isinstance(target, ast.Name):
                        bindings[target.id] = value
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _python_call_name(node.func).rsplit(".", 1)[-1] in {"chdir", "fchdir"}:
            return False
        is_link, link_targets = _python_link_targets(node, bindings, cwd)
        if is_link and (
            link_targets is None
            or any(
                _write_target_is_outside(target, cwd, workspace, control_root)
                for target in link_targets
            )
        ):
            return False
        is_write, target = _python_write_target(node, bindings, cwd)
        if is_write and (
            target is None
            or _write_target_is_outside(target, cwd, workspace, control_root)
        ):
            return False
        is_command, nested = _python_nested_command(node, bindings, cwd)
        if is_command and (
            nested is None
            or not _worktree_bash_is_safe(
                nested,
                root=root,
                workspace=workspace,
                cwd=cwd,
                control_root=control_root,
                depth=depth + 1,
                inspected=inspected,
            )
        ):
            return False
    return True


def _python_link_targets(
    node: ast.Call,
    bindings: dict[str, str],
    cwd: Path,
) -> tuple[bool, tuple[str, str] | None]:
    name = _python_call_name(node.func)
    method = name.rsplit(".", 1)[-1]
    if method in {"hardlink_to", "link_to", "symlink_to"} and isinstance(node.func, ast.Attribute):
        source = _python_path_literal(node.func.value, bindings, cwd)
        target_node = node.args[0] if node.args else next(
            (item.value for item in node.keywords if item.arg == "target"),
            None,
        )
        target = _python_path_literal(target_node, bindings, cwd)
        return True, (source, target) if source is not None and target is not None else None
    if method not in {"link", "symlink"}:
        return False, None
    if any(item.arg in {"dir_fd", "dst_dir_fd", "src_dir_fd"} for item in node.keywords):
        return True, None
    source_node = node.args[0] if node.args else next(
        (item.value for item in node.keywords if item.arg in {"src", "source"}),
        None,
    )
    target_node = node.args[1] if len(node.args) > 1 else next(
        (item.value for item in node.keywords if item.arg in {"dst", "target"}),
        None,
    )
    source = _python_path_literal(source_node, bindings, cwd)
    target = _python_path_literal(target_node, bindings, cwd)
    return True, (source, target) if source is not None and target is not None else None


def _python_write_target(
    node: ast.Call,
    bindings: dict[str, str],
    cwd: Path,
) -> tuple[bool, str | None]:
    name = _python_call_name(node.func)
    method = name.rsplit(".", 1)[-1]
    if name.endswith("os.open"):
        flags = node.args[1] if len(node.args) > 1 else None
        writes = _python_open_flags_write(flags) if flags is not None else None
        if writes is False:
            return False, None
        target = _python_path_literal(node.args[0], bindings, cwd) if node.args else None
        return True, target
    if method == "open":
        mode_index = 0 if isinstance(node.func, ast.Attribute) else 1
        mode_node = node.args[mode_index] if len(node.args) > mode_index else None
        mode_node = next((item.value for item in node.keywords if item.arg == "mode"), mode_node)
        mode = _python_path_literal(mode_node, bindings, cwd) if mode_node is not None else "r"
        if mode is None:
            return True, None
        if not any(flag in mode for flag in "wax+"):
            return False, None
        target_node = node.func.value if isinstance(node.func, ast.Attribute) else node.args[0] if node.args else None
        return True, _python_path_literal(target_node, bindings, cwd)
    if isinstance(node.func, ast.Attribute) and method in {
        "mkdir",
        "rmdir",
        "touch",
        "unlink",
        "write_bytes",
        "write_text",
    }:
        return True, _python_path_literal(node.func.value, bindings, cwd)
    if isinstance(node.func, ast.Attribute) and method in {"rename", "replace", "symlink_to"}:
        target_node = node.args[0] if node.args else None
        return True, _python_path_literal(target_node, bindings, cwd)
    if method in {"makedirs", "mkdir", "remove", "rmdir", "unlink"}:
        target_node = node.args[0] if node.args else None
        return True, _python_path_literal(target_node, bindings, cwd)
    if method in {"copy", "copy2", "copyfile", "copytree", "move", "rename", "replace"}:
        target_node = node.args[1] if len(node.args) > 1 else None
        return True, _python_path_literal(target_node, bindings, cwd)
    if method in {"dump", "save", "to_csv", "to_excel", "to_json", "to_parquet"}:
        target_node = node.args[0] if node.args else None
        return True, _python_path_literal(target_node, bindings, cwd)
    return False, None


def _python_open_flags_write(node: ast.expr) -> bool | None:
    if isinstance(node, ast.Attribute):
        if node.attr == "O_RDONLY":
            return False
        if node.attr in {"O_APPEND", "O_CREAT", "O_EXCL", "O_RDWR", "O_TRUNC", "O_WRONLY"}:
            return True
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _python_open_flags_write(node.left)
        right = _python_open_flags_write(node.right)
        if left is True or right is True:
            return True
        if left is False and right is False:
            return False
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value != os.O_RDONLY
    return None


def _python_nested_command(
    node: ast.Call,
    bindings: dict[str, str],
    cwd: Path,
) -> tuple[bool, str | None]:
    name = _python_call_name(node.func)
    method = name.rsplit(".", 1)[-1]
    if method in {"eval", "exec"}:
        return True, None
    if method not in {
        "Popen",
        "call",
        "check_call",
        "check_output",
        "run",
        "system",
    }:
        return False, None
    if not node.args:
        return True, None
    command_node = node.args[0]
    if isinstance(command_node, (ast.List, ast.Tuple)):
        values = [_python_path_literal(value, bindings, cwd) for value in command_node.elts]
        return True, shlex.join(values) if all(value is not None for value in values) else None
    return True, _python_path_literal(command_node, bindings, cwd)


def _python_call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _python_call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _python_path_literal(
    node: ast.expr | None,
    bindings: dict[str, str],
    cwd: Path,
) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int)):
        return str(node.value)
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                rendered = _python_path_literal(value.value, bindings, cwd)
                if rendered is None:
                    return None
                parts.append(rendered)
            else:
                return None
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Div)):
        left = _python_path_literal(node.left, bindings, cwd)
        right = _python_path_literal(node.right, bindings, cwd)
        if left is None or right is None:
            return None
        return str(Path(left) / right) if isinstance(node.op, ast.Div) else left + right
    if isinstance(node, ast.Call):
        name = _python_call_name(node.func)
        if name.rsplit(".", 1)[-1] in {"Path", "PurePath", "str"} and node.args:
            return _python_path_literal(node.args[0], bindings, cwd)
        if name.endswith("Path.cwd") and not node.args:
            return str(cwd)
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"absolute", "resolve"}:
            return _python_path_literal(node.func.value, bindings, cwd)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "joinpath":
            base = _python_path_literal(node.func.value, bindings, cwd)
            parts = [_python_path_literal(value, bindings, cwd) for value in node.args]
            if base is not None and all(part is not None for part in parts):
                return str(Path(base).joinpath(*parts))
        if name.endswith("os.path.join"):
            parts = [_python_path_literal(value, bindings, cwd) for value in node.args]
            if parts and all(part is not None for part in parts):
                return os.path.join(*parts)
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        base = _python_path_literal(node.value, bindings, cwd)
        return str(Path(base).parent) if base is not None else None
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "parents"
    ):
        base = _python_path_literal(node.value.value, bindings, cwd)
        index = _python_path_literal(node.slice, bindings, cwd)
        if base is not None and index is not None and index.isdigit():
            try:
                return str(Path(base).parents[int(index)])
            except IndexError:
                return None
    return None


def _node_script_is_safe(
    source: str,
    *,
    root: Path,
    workspace: Path,
    cwd: Path,
    control_root: Path | None,
    depth: int,
    inspected: frozenset[str],
) -> bool:
    bindings = {
        match.group("name"): _decode_script_literal(match.group("quote"), match.group("value"))
        for match in _NODE_STRING_BINDING_RE.finditer(source)
    }
    for match in _NODE_WRITE_CALL_RE.finditer(source):
        target = _decode_script_literal(match.group("quote"), match.group("path"))
        if target is None or _write_target_is_outside(target, cwd, workspace, control_root):
            return False
    for match in _NODE_WRITE_EXPRESSION_RE.finditer(source):
        target = bindings.get(match.group("target"))
        if target is None or _write_target_is_outside(target, cwd, workspace, control_root):
            return False
    link_matches = list(_NODE_LINK_CALL_RE.finditer(source))
    without_verified_links = _NODE_LINK_CALL_RE.sub("", source)
    if _NODE_LINK_CALL_NAME_RE.search(without_verified_links):
        return False
    for match in link_matches:
        link_source = _node_path_expression(match.group("source"), bindings)
        link_target = _node_path_expression(match.group("target"), bindings)
        if (
            link_source is None
            or link_target is None
            or _write_target_is_outside(link_source, cwd, workspace, control_root)
            or _write_target_is_outside(link_target, cwd, workspace, control_root)
        ):
            return False
    for match in _NODE_COMMAND_CALL_RE.finditer(source):
        command = _decode_script_literal(match.group("quote"), match.group("command"))
        if command is None or not _worktree_bash_is_safe(
            command,
            root=root,
            workspace=workspace,
            cwd=cwd,
            control_root=control_root,
            depth=depth + 1,
            inspected=inspected,
        ):
            return False
    return True


def _node_path_expression(expression: str, bindings: dict[str, str | None]) -> str | None:
    value = expression.strip()
    if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", value):
        return bindings.get(value)
    if len(value) < 2 or value[0] not in {'\"', "'", "`"} or value[-1] != value[0]:
        return None
    return _decode_script_literal(value[0], value[1:-1])


def _decode_script_literal(quote: str, value: str) -> str | None:
    if quote == "`":
        return None if "${" in value else value
    try:
        decoded = ast.literal_eval(f"{quote}{value}{quote}")
    except (SyntaxError, ValueError):
        return None
    return decoded if isinstance(decoded, str) else None


def _generic_script_is_safe(
    source: str,
    cwd: Path,
    workspace: Path,
    control_root: Path | None,
) -> bool:
    pattern = re.compile(
        r"\b(?:append|file_put_contents|mkdir|open|remove|rename|replace|touch|unlink|write)\w*"
        r"\s*\(\s*(['\"])(.*?)\1",
        re.DOTALL,
    )
    return all(
        not _write_target_is_outside(match.group(2), cwd, workspace, control_root)
        for match in pattern.finditer(source)
    )


def _strip_command_prefixes(tokens: list[str]) -> list[str]:
    remaining, _ = _command_prefix_info(tokens)
    return remaining


def _command_prefix_info(tokens: list[str]) -> tuple[list[str], dict[str, str]]:
    remaining = list(tokens)
    environment: dict[str, str] = {}
    while remaining:
        while remaining:
            match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", remaining[0])
            if not match:
                break
            environment[match.group(1)] = match.group(2)
            remaining = remaining[1:]
        if not remaining:
            return remaining, environment
        wrapper = Path(remaining[0]).name
        if wrapper in {"command", "builtin"}:
            index = 1
            while index < len(remaining):
                token = remaining[index]
                if token in {"-v", "-V"}:
                    return [], environment
                if token == "-p":
                    index += 1
                    continue
                if token == "--":
                    index += 1
                    break
                if token.startswith("-"):
                    return remaining, environment
                break
            remaining = remaining[index:]
            continue
        if wrapper == "env":
            index = 1
            while index < len(remaining):
                token = remaining[index]
                match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", token)
                if match:
                    environment[match.group(1)] = match.group(2)
                    index += 1
                    continue
                if token in {"-i", "--ignore-environment"}:
                    environment.clear()
                    index += 1
                    continue
                if token in {"-u", "--unset"}:
                    if index + 1 >= len(remaining):
                        return remaining, environment
                    environment.pop(remaining[index + 1], None)
                    index += 2
                    continue
                if token.startswith("--unset="):
                    environment.pop(token.split("=", 1)[1], None)
                    index += 1
                    continue
                if token == "--":
                    index += 1
                    break
                if token.startswith("-"):
                    return remaining, environment
                break
            remaining = remaining[index:]
            continue
        if wrapper == "exec":
            index = 1
            while index < len(remaining):
                token = remaining[index]
                if token == "--":
                    index += 1
                    break
                if token in {"-c", "-l"} or (
                    token.startswith("-")
                    and not token.startswith("--")
                    and token != "-"
                    and set(token[1:]) <= {"c", "l"}
                ):
                    if "c" in token:
                        environment.clear()
                    index += 1
                    continue
                if token == "-a":
                    if index + 1 >= len(remaining):
                        return remaining, environment
                    index += 2
                    continue
                if token.startswith("-"):
                    return remaining, environment
                break
            remaining = remaining[index:]
            continue
        return remaining, environment
    return remaining, environment


def _git_path_assignments_are_safe(tokens: list[str], cwd: Path, workspace: Path) -> bool:
    for token in tokens:
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", token)
        if match and match.group(1) in _GIT_PATH_ENV:
            if _write_target_is_outside(match.group(2), cwd, workspace, None):
                return False
    return True


def _nested_shell_payload(args: list[str]) -> str | None:
    for index, token in enumerate(args):
        if token == "-c" or (token.startswith("-") and not token.startswith("--") and "c" in token[1:]):
            return args[index + 1] if index + 1 < len(args) else None
    return None


def _worktree_git_is_safe(
    args: list[str],
    cwd: Path,
    workspace: Path,
    control_root: Path | None,
    environment: dict[str, str],
) -> bool:
    context = cwd
    explicit_contexts: list[str] = []
    config_values: list[str] = []
    config_env_values: list[str] = []
    index = 0
    while index < len(args) and args[index].startswith("-") and args[index] != "--":
        option = args[index]
        if option == "-C":
            if index + 1 >= len(args):
                return False
            explicit_contexts.append(args[index + 1])
            context = _resolve_shell_path(args[index + 1], context)
            index += 2
            continue
        if option.startswith("-C") and option != "-C":
            explicit_contexts.append(option[2:])
            context = _resolve_shell_path(option[2:], context)
            index += 1
            continue
        matched = False
        for name in ("--git-dir", "--work-tree"):
            if option == name:
                if index + 1 >= len(args):
                    return False
                explicit_contexts.append(args[index + 1])
                index += 2
                matched = True
                break
            if option.startswith(f"{name}="):
                explicit_contexts.append(option.split("=", 1)[1])
                index += 1
                matched = True
                break
        if matched:
            continue
        if option == "-c":
            if index + 1 >= len(args):
                return False
            config_values.append(args[index + 1])
            index += 2
            continue
        if option.startswith("-c") and option != "-c":
            config_values.append(option[2:])
            index += 1
            continue
        if option == "--config-env":
            if index + 1 >= len(args):
                return False
            config_env_values.append(args[index + 1])
            index += 2
            continue
        if option.startswith("--config-env="):
            config_env_values.append(option.split("=", 1)[1])
            index += 1
            continue
        if option == "--exec-path" or option.startswith("--exec-path="):
            return False
        if option == "--namespace":
            if index + 1 >= len(args):
                return False
            index += 2
            continue
        index += 1
    if index < len(args) and args[index] == "--":
        index += 1
    subcommand = args[index] if index < len(args) else ""
    remaining = args[index + 1:] if index < len(args) else []
    inline_config = _inline_git_config(config_values, config_env_values, environment)
    if inline_config is None or _unsafe_git_config(
        config_values,
        config_env_values,
        environment,
        cwd,
        workspace,
    ):
        return False
    if subcommand:
        aliases = _git_config_values(context, f"alias.{subcommand}")
        if aliases is None or aliases:
            return False
    if subcommand in {"commit", "pull", "push", "send-pack"} and _workspace_branch_is_protected(context):
        return False
    if subcommand == "push" and (
        _git_push_targets_protected(remaining)
        or _git_configured_push_targets_protected(remaining, context, inline_config)
    ):
        return False
    if subcommand == "send-pack" and _git_send_pack_targets_protected(remaining):
        return False
    if subcommand in {"fetch", "pull"} and _git_fetch_targets_protected(
        remaining,
        context,
    ):
        return False
    for target in _git_output_targets(remaining):
        if _write_target_is_outside(target, context, workspace, control_root):
            return False
    if any(_git_remaining_path_is_outside(value, context, workspace) for value in remaining):
        return False
    if subcommand == "config":
        return _git_config_args_are_read_only(remaining)
    if subcommand == "remote":
        return _git_remote_args_are_read_only(remaining)
    if subcommand == "submodule":
        return _git_submodule_args_are_read_only(remaining)
    if subcommand == "worktree":
        return bool(remaining) and remaining[0] == "list"
    if subcommand in _SHARED_REF_MUTATING_GIT_COMMANDS:
        return False
    if subcommand in {"checkout", "switch"}:
        return False
    if subcommand == "branch" and not _git_branch_args_are_read_only(remaining):
        return False
    if subcommand == "tag" and any(value != "--" and not value.startswith("-") for value in remaining):
        return False
    if subcommand in _READ_ONLY_GIT_COMMANDS:
        return True
    if not _path_is_within(context, workspace):
        return False
    return all(not _write_target_is_outside(value, cwd, workspace, None) for value in explicit_contexts)


def _workspace_branch_is_protected(cwd: Path) -> bool:
    branch = _workspace_branch(cwd)
    return branch is None or branch in {"main", "master", "develop"}


def _workspace_branch(cwd: Path) -> str | None:
    try:
        branch = subprocess.check_output(
            [_TRUSTED_GIT, "branch", "--show-current"],
            cwd=cwd,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return branch or None


def _unsafe_git_environment(environment: dict[str, str]) -> bool:
    if any(
        name in environment
        for name in {"GIT_EXEC_PATH", "HOME", "PATH", "XDG_CONFIG_HOME"}
    ):
        return True
    effective = {
        name: value
        for name, value in os.environ.items()
        if name
        in {
            "EDITOR",
            "GIT_DIFF_OPTS",
            "GIT_EDITOR",
            "GIT_EXTERNAL_DIFF",
            "GIT_PAGER",
            "GIT_SEQUENCE_EDITOR",
            "LESS",
            "PAGER",
            "TERM",
            "VISUAL",
        }
        or name == "GIT_CONFIG_COUNT"
        or name
        in {
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_NOSYSTEM",
            "GIT_CONFIG_PARAMETERS",
            "GIT_CONFIG_SYSTEM",
        }
        or re.fullmatch(r"GIT_CONFIG_(?:KEY|VALUE)_\d+", name)
    }
    effective.update(environment)
    if any(
        name
        in {
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_NOSYSTEM",
            "GIT_CONFIG_PARAMETERS",
            "GIT_CONFIG_SYSTEM",
        }
        or re.fullmatch(r"GIT_CONFIG_(?:KEY|VALUE)_\d+", name)
        for name in effective
    ):
        return True
    for name in ("PAGER", "GIT_PAGER"):
        value = effective.get(name)
        if value is not None and not _safe_pager_command(value):
            return True
    for name in ("EDITOR", "GIT_EDITOR", "GIT_SEQUENCE_EDITOR", "VISUAL"):
        value = effective.get(name)
        if value and not _safe_editor_command(value):
            return True
    external_diff = effective.get("GIT_EXTERNAL_DIFF")
    if external_diff:
        return True
    diff_options = effective.get("GIT_DIFF_OPTS")
    if diff_options is not None and not _safe_git_diff_options(diff_options):
        return True
    less = effective.get("LESS")
    if less is not None and not _safe_less_options(less):
        return True
    term = effective.get("TERM")
    return term is not None and not bool(re.fullmatch(r"[A-Za-z0-9_.+-]+", term))


def _safe_pager_command(value: str) -> bool:
    if not value:
        return True
    if re.search(r"[;&|<>`$()]", value):
        return False
    try:
        tokens = shlex.split(value)
    except ValueError:
        return False
    if not tokens or Path(tokens[0]).name not in {"cat", "less", "more"}:
        return False
    for option in tokens[1:]:
        if not option.startswith("-"):
            return False
        if option in {"-o", "-O", "--log-file"} or option.startswith(
            ("-o", "-O", "--log-file=")
        ):
            return False
    return True


def _safe_editor_command(value: str) -> bool:
    if re.search(r"[;&|<>`$()]", value):
        return False
    try:
        tokens = shlex.split(value)
    except ValueError:
        return False
    if not tokens or Path(tokens[0]).name not in {
        "cat",
        "code",
        "emacs",
        "false",
        "nano",
        "nvim",
        "true",
        "vi",
        "vim",
    }:
        return False
    return all(option.startswith("-") for option in tokens[1:])


def _safe_git_diff_options(value: str) -> bool:
    if not value:
        return True
    try:
        tokens = shlex.split(value)
    except ValueError:
        return False
    return bool(tokens) and all(
        bool(re.fullmatch(r"(?:-u\d*|--unified(?:=\d+)?)", token))
        for token in tokens
    )


def _safe_less_options(value: str) -> bool:
    if not value:
        return True
    if re.search(r"[;&|<>`$()]", value):
        return False
    try:
        tokens = shlex.split(value)
    except ValueError:
        return False
    return all(
        token.startswith("-")
        and token not in {"-o", "-O", "--log-file"}
        and not token.startswith(("-o", "-O", "--log-file="))
        for token in tokens
    )


def _unsafe_git_config(
    values: list[str],
    env_values: list[str],
    environment: dict[str, str],
    cwd: Path,
    workspace: Path,
) -> bool:
    for value in values:
        key, separator, configured = value.partition("=")
        if _unsafe_git_config_entry(
            key,
            configured,
            bool(separator),
            cwd,
            workspace,
        ):
            return True
    for value in env_values:
        key, separator, env_name = value.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
            return True
        configured = environment.get(env_name, os.environ.get(env_name))
        if configured is None or _unsafe_git_config_entry(
            key,
            configured,
            True,
            cwd,
            workspace,
        ):
            return True
    return False


def _inline_git_config(
    values: list[str],
    env_values: list[str],
    environment: dict[str, str],
) -> dict[str, str] | None:
    configured: dict[str, str] = {}
    for value in values:
        key, separator, setting = value.partition("=")
        if not key:
            return None
        configured[key.lower()] = setting if separator else "true"
    for value in env_values:
        key, separator, env_name = value.partition("=")
        if not key or not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
            return None
        setting = environment.get(env_name, os.environ.get(env_name))
        if setting is None:
            return None
        configured[key.lower()] = setting
    return configured


def _unsafe_git_config_entry(
    key: str,
    configured: str,
    has_value: bool,
    cwd: Path,
    workspace: Path,
) -> bool:
    normalized = key.lower()
    if normalized.startswith("alias."):
        return True
    if normalized in {"core.pager"} or normalized.startswith("pager."):
        return not has_value or not _safe_pager_command(configured)
    if normalized in {"core.editor", "sequence.editor"}:
        return not has_value or not _safe_editor_command(configured)
    if normalized == "diff.external":
        return not has_value or bool(configured)
    if re.fullmatch(r"diff\..+\.(?:command|textconv)", normalized):
        return True
    if re.fullmatch(r"filter\..+\.(?:clean|smudge|process)", normalized):
        return True
    if re.fullmatch(r"remote\..+\.push", normalized):
        return not has_value or _git_refspec_targets_protected(configured)
    if re.fullmatch(r"remote\..+\.fetch", normalized):
        return not has_value or _git_fetch_refspec_targets_protected(configured)
    if normalized == "push.default" and configured.lower() == "matching":
        return True
    if re.fullmatch(r"remote\..+\.mirror", normalized) and configured.lower() == "true":
        return True
    if normalized == "core.hookspath":
        return has_value and bool(configured)
    if normalized == "core.worktree":
        return not has_value or _write_target_is_outside(configured, cwd, workspace, None)
    return False


def _git_refspec_targets_protected(refspec: str) -> bool:
    normalized = refspec.lstrip("+")
    if normalized == ":":
        return True
    source, separator, target = normalized.partition(":")
    target = target if separator else source
    target = target.removeprefix("refs/heads/").rstrip("/")
    source = source.removeprefix("refs/heads/").rstrip("/")
    protected = {"main", "master", "develop"}
    return any(fnmatchcase(branch, target) for branch in protected) or (
        separator
        and not target
        and any(fnmatchcase(branch, source) for branch in protected)
    )


def _git_fetch_refspec_targets_protected(refspec: str) -> bool:
    normalized = refspec.lstrip("+")
    if normalized.startswith("^"):
        return False
    _, separator, target = normalized.partition(":")
    if not separator or not target:
        return False
    target = target.removeprefix("refs/heads/").rstrip("/")
    return any(
        fnmatchcase(branch, target)
        for branch in {"main", "master", "develop"}
    )


def _git_push_targets_protected(args: list[str]) -> bool:
    if any(value in {"--all", "--mirror"} for value in args):
        return True
    _, refspecs, valid = _git_push_operands(args)
    return not valid or any(_git_refspec_targets_protected(refspec) for refspec in refspecs)


def _git_send_pack_targets_protected(args: list[str]) -> bool:
    if any(value in {"--all", "--mirror", "--stdin"} for value in args):
        return True
    _, refspecs, valid = _git_push_operands(args)
    return (
        not valid
        or not refspecs
        or any(_git_refspec_targets_protected(refspec) for refspec in refspecs)
    )


def _git_push_operands(args: list[str]) -> tuple[str | None, list[str], bool]:
    operands: list[str] = []
    repository: str | None = None
    options_with_value = {"--exec", "--push-option", "--receive-pack", "--repo", "-o"}
    index = 0
    while index < len(args):
        value = args[index]
        if value == "--":
            operands.extend(args[index + 1 :])
            break
        if value in options_with_value:
            if index + 1 >= len(args):
                return None, [], False
            if value == "--repo":
                repository = args[index + 1]
            index += 2
            continue
        if value.startswith("--repo="):
            repository = value.split("=", 1)[1]
            index += 1
            continue
        if value.startswith("-"):
            index += 1
            continue
        operands.append(value)
        index += 1
    if repository is not None:
        return repository, operands, bool(repository)
    if not operands:
        return None, [], True
    return operands[0], operands[1:], True


def _git_configured_push_targets_protected(
    args: list[str],
    cwd: Path,
    inline_config: dict[str, str],
) -> bool:
    repository, refspecs, valid = _git_push_operands(args)
    if not valid:
        return True
    if refspecs:
        return False
    branch = _workspace_branch(cwd)
    if branch is None:
        return True
    if repository is None:
        repository = _configured_push_remote(cwd, branch, inline_config)
        if repository is None:
            return True
    if re.fullmatch(r"[A-Za-z0-9._/-]+", repository):
        mirror = _git_effective_config_values(cwd, f"remote.{repository}.mirror", inline_config)
        configured_refspecs = _git_effective_config_values(cwd, f"remote.{repository}.push", inline_config)
        if mirror is None or configured_refspecs is None:
            return True
        if any(value.lower() == "true" for value in mirror):
            return True
        if configured_refspecs:
            return any(
                _git_refspec_targets_protected(refspec)
                for refspec in configured_refspecs
            )
    push_default_values = _git_effective_config_values(cwd, "push.default", inline_config)
    if push_default_values is None:
        return True
    push_default = push_default_values[-1].lower() if push_default_values else "simple"
    if push_default == "matching":
        return True
    if push_default in {"tracking", "upstream"}:
        upstream = _git_effective_config_values(cwd, f"branch.{branch}.merge", inline_config)
        if upstream is None:
            return True
        return any(_git_refspec_targets_protected(value) for value in upstream)
    return False


def _git_fetch_targets_protected(args: list[str], cwd: Path) -> bool:
    if any(value in {"--all", "--multiple", "--stdin"} for value in args):
        return True
    repository, refspecs, refmaps, valid = _git_fetch_operands(args)
    if not valid:
        return True
    if any(
        _git_fetch_refspec_targets_protected(refspec)
        for refspec in (*refspecs, *refmaps)
    ):
        return True
    if refspecs:
        return False
    if repository is None:
        repository = _configured_fetch_remote(cwd)
        if repository is None:
            return True
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", repository):
        return False
    configured_refspecs = _git_config_values(cwd, f"remote.{repository}.fetch")
    return configured_refspecs is None or any(
        _git_fetch_refspec_targets_protected(refspec)
        for refspec in configured_refspecs
    )


def _git_fetch_operands(
    args: list[str],
) -> tuple[str | None, list[str], list[str], bool]:
    options_with_value = {
        "--cleanup",
        "--deepen",
        "--depth",
        "--filter",
        "--jobs",
        "--negotiation-tip",
        "--refmap",
        "--server-option",
        "--shallow-exclude",
        "--shallow-since",
        "--strategy",
        "--strategy-option",
        "--upload-pack",
        "-j",
        "-o",
    }
    operands: list[str] = []
    refmaps: list[str] = []
    index = 0
    while index < len(args):
        value = args[index]
        if value == "--":
            operands.extend(args[index + 1 :])
            break
        if value in options_with_value:
            if index + 1 >= len(args):
                return None, [], [], False
            if value == "--refmap":
                refmaps.append(args[index + 1])
            index += 2
            continue
        if value.startswith("--refmap="):
            refmaps.append(value.split("=", 1)[1])
            index += 1
            continue
        if value.startswith("-"):
            index += 1
            continue
        operands.append(value)
        index += 1
    if not operands:
        return None, [], refmaps, True
    return operands[0], operands[1:], refmaps, True


def _configured_fetch_remote(cwd: Path) -> str | None:
    branch = _workspace_branch(cwd)
    if branch is not None:
        values = _git_config_values(cwd, f"branch.{branch}.remote")
        if values is None:
            return None
        if values and values[-1]:
            return values[-1]
    return "origin"


def _configured_push_remote(
    cwd: Path,
    branch: str,
    inline_config: dict[str, str] | None = None,
) -> str | None:
    keys = (
        f"branch.{branch}.pushRemote",
        "remote.pushDefault",
        f"branch.{branch}.remote",
    )
    for key in keys:
        values = _git_effective_config_values(cwd, key, inline_config or {})
        if values is None:
            return None
        if values and values[-1]:
            return values[-1]
    return "origin"


def _git_config_values(cwd: Path, key: str) -> list[str] | None:
    try:
        result = subprocess.run(
            ("git", "config", "--get-all", key),
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 1:
        return []
    if result.returncode != 0:
        return None
    return result.stdout.splitlines()


def _git_effective_config_values(
    cwd: Path,
    key: str,
    inline_config: dict[str, str],
) -> list[str] | None:
    inline = inline_config.get(key.lower())
    return [inline] if inline is not None else _git_config_values(cwd, key)


def _git_config_args_are_read_only(args: list[str]) -> bool:
    mutating = {
        "--add",
        "--edit",
        "--remove-section",
        "--rename-section",
        "--replace-all",
        "--unset",
        "--unset-all",
        "-e",
    }
    if any(value in mutating for value in args):
        return False
    queries = {
        "--get",
        "--get-all",
        "--get-regexp",
        "--get-urlmatch",
        "--list",
        "-l",
    }
    if any(value in queries for value in args):
        return True
    operands = [value for value in args if value != "--" and not value.startswith("-")]
    if operands and operands[0] in {"get", "get-all", "get-regexp", "get-urlmatch", "list"}:
        return True
    return len(operands) == 1


def _git_remote_args_are_read_only(args: list[str]) -> bool:
    operands = [value for value in args if value != "--" and not value.startswith("-")]
    return not operands or operands[0] in {"get-url", "show"}


def _git_submodule_args_are_read_only(args: list[str]) -> bool:
    operands = [value for value in args if value != "--" and not value.startswith("-")]
    return not operands or operands[0] in {"status", "summary"}


def _git_output_targets(args: list[str]) -> list[str]:
    targets: list[str] = []
    index = 0
    while index < len(args):
        value = args[index]
        if value in {"-o", "--output", "--output-directory"}:
            if index + 1 < len(args):
                targets.append(args[index + 1])
            else:
                targets.append("")
            index += 2
            continue
        if value.startswith(("--output=", "--output-directory=")):
            targets.append(value.split("=", 1)[1])
        elif value.startswith("-o") and value != "-o":
            targets.append(value[2:])
        index += 1
    return targets


def _git_remaining_path_is_outside(token: str, cwd: Path, workspace: Path) -> bool:
    if not token or token == "--":
        return False
    value = token.split("=", 1)[1] if token.startswith("-") and "=" in token else token
    if value.startswith("file://"):
        value = value.removeprefix("file://")
    looks_like_path = (
        value in {".", ".."}
        or value.startswith(("/", "./", "../", "~", "$"))
        or "/" in value
    )
    return looks_like_path and _write_target_is_outside(value, cwd, workspace, None)


def _git_branch_args_are_read_only(args: list[str]) -> bool:
    if not args:
        return True
    mutating = {
        "-c", "-C", "-d", "-D", "-m", "-M",
        "--copy", "--delete", "--edit-description", "--move", "--unset-upstream",
    }
    if any(value in mutating or value.startswith("--set-upstream-to") for value in args):
        return False
    return all(value == "--" or value.startswith("-") for value in args)


def _non_option_operands(args: list[str]) -> list[str]:
    return [value for value in args if value != "--" and not value.startswith("-")]


def _destination_operand(args: list[str]) -> str | None:
    for index, value in enumerate(args):
        if value in {"-t", "--target-directory"} and index + 1 < len(args):
            return args[index + 1]
        if value.startswith("--target-directory="):
            return value.split("=", 1)[1]
    operands = _non_option_operands(args)
    return operands[-1] if len(operands) >= 2 else None


def _ln_write_paths(args: list[str]) -> list[str] | None:
    operands: list[str] = []
    target_directory: str | None = None
    no_target_directory = False
    options = True
    index = 0
    while index < len(args):
        value = args[index]
        if options and value == "--":
            options = False
            index += 1
            continue
        if not options or value == "-" or not value.startswith("-"):
            operands.append(value)
            index += 1
            continue
        if value in {"-t", "--target-directory"}:
            if target_directory is not None or index + 1 >= len(args):
                return None
            target_directory = args[index + 1]
            if not target_directory:
                return None
            index += 2
            continue
        if value.startswith("--target-directory="):
            if target_directory is not None:
                return None
            target_directory = value.split("=", 1)[1]
            if not target_directory:
                return None
            index += 1
            continue
        if value in {"-T", "--no-target-directory"}:
            no_target_directory = True
            index += 1
            continue
        if value in {"-S", "--suffix"}:
            if index + 1 >= len(args):
                return None
            index += 2
            continue
        if value.startswith("--suffix="):
            index += 1
            continue
        if value.startswith("--"):
            if value.split("=", 1)[0] not in {
                "--backup",
                "--dereference",
                "--directory",
                "--force",
                "--interactive",
                "--logical",
                "--no-dereference",
                "--physical",
                "--relative",
                "--symbolic",
                "--verbose",
            }:
                return None
            index += 1
            continue
        cluster = value[1:]
        cluster_index = 0
        while cluster_index < len(cluster):
            option = cluster[cluster_index]
            if option in {"S", "t"}:
                argument = cluster[cluster_index + 1 :]
                if not argument:
                    if index + 1 >= len(args):
                        return None
                    argument = args[index + 1]
                    index += 1
                if option == "t":
                    if target_directory is not None or not argument:
                        return None
                    target_directory = argument
                cluster_index = len(cluster)
                continue
            if option not in {"b", "d", "f", "i", "L", "n", "P", "r", "s", "T", "v"}:
                return None
            if option == "T":
                no_target_directory = True
            cluster_index += 1
        index += 1
    if target_directory is not None:
        if no_target_directory or not operands:
            return None
        return [target_directory, *operands]
    if not operands:
        return None
    return operands


def _write_target_is_outside(
    raw: str,
    cwd: Path,
    workspace: Path,
    control_root: Path | None,
) -> bool:
    if not raw or "$" in raw or "`" in raw:
        return True
    candidate = _resolve_shell_path(raw, cwd)
    if _path_is_within(candidate, workspace):
        return False
    if _is_control_target(candidate, control_root):
        return False
    return candidate.as_posix() not in {"/dev/null", "/dev/stdout", "/dev/stderr"}


def _token_references_outside_path(
    token: str,
    cwd: Path,
    workspace: Path,
    control_root: Path | None,
) -> bool:
    if "$" in token or "`" in token:
        return True
    candidates = [token]
    candidates.extend(re.findall(r"(?:\.\./)+[^\s'\"),;]+", token))
    candidates.extend(re.findall(r"/[^\s'\"),;]+", token))
    return any(
        _write_target_is_outside(candidate, cwd, workspace, control_root)
        for candidate in candidates
        if candidate and (candidate in {".", ".."} or candidate.startswith(("/", "./", "../", "~")) or "/" in candidate)
    )


def _resolve_shell_path(raw: str, cwd: Path) -> Path:
    value = raw.split("=", 1)[1] if raw.startswith(("of=", "--target-directory=")) else raw
    candidate = Path(value).expanduser()
    return candidate.resolve() if candidate.is_absolute() else (cwd / candidate).resolve()


def _path_is_within(candidate: Path, root: Path) -> bool:
    return candidate == root or root in candidate.parents


def _registered_task_worktree(root: Path, candidate: Path) -> bool:
    try:
        worktrees = subprocess.check_output(
            [_TRUSTED_GIT, "worktree", "list", "--porcelain"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    for block in worktrees.split("\n\n"):
        if not block.startswith(f"worktree {candidate}\n"):
            continue
        branch_line = next(
            (line for line in block.splitlines() if line.startswith("branch refs/heads/")),
            "",
        )
        branch = branch_line.removeprefix("branch refs/heads/")
        return bool(branch) and branch not in {"main", "master", "develop"}
    return False


def _registered_detached_worktree(root: Path, candidate: Path) -> bool:
    try:
        worktrees = subprocess.check_output(
            [_TRUSTED_GIT, "worktree", "list", "--porcelain"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    for block in worktrees.split("\n\n"):
        if block.startswith(f"worktree {candidate}\n"):
            return "detached" in block.splitlines()
    return False


def _detached_initial_start_command(command: str) -> bool:
    if not command.strip() or "`" in command or "$(" in command or "\n" in command or "\r" in command:
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return False
    if any(token in _SHELL_SEPARATORS or token in _REDIRECT_OPERATORS for token in tokens):
        return False
    tokens = _strip_command_prefixes(tokens)
    if not tokens:
        return False
    command_name = Path(tokens[0]).name
    args = tokens[1:]
    if command_name in {"agent-flow", "agent-flow-kit"}:
        return (
            len(args) > 1
            and args[0] == "run"
            and args[1]
            not in {
                "advance",
                "install",
                "next",
                "push-watch",
                "push-watch-tick",
                "status",
            }
        )
    return command_name == "agent-flow-python" and bool(args) and args[0] in {"run", "start"}


def _registered_worktree_branch(root: Path, candidate: Path) -> str | None:
    try:
        worktrees = subprocess.check_output(
            [_TRUSTED_GIT, "worktree", "list", "--porcelain"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    for block in worktrees.split("\n\n"):
        if not block.startswith(f"worktree {candidate}\n"):
            continue
        branch_line = next(
            (line for line in block.splitlines() if line.startswith("branch refs/heads/")),
            "",
        )
        return branch_line.removeprefix("branch refs/heads/") or None
    return None


def _python_active_run(root: Path, git_common: Path) -> dict | None:
    runtime_root = (git_common / "agent-flow" / "worktrees").resolve()
    if not runtime_root.exists():
        return None
    if runtime_root.is_symlink() or not runtime_root.is_dir():
        raise RuntimeError("Python runtime worktree root is unsafe")
    active: list[dict] = []
    for worktree_runtime in sorted(runtime_root.iterdir(), key=lambda entry: entry.name):
        if worktree_runtime.is_symlink() or not worktree_runtime.is_dir():
            continue
        manifest, workspace, branch = _runtime_worktree_identity(root, worktree_runtime)
        runs_root = worktree_runtime / ".agent-flow" / "runs"
        if not runs_root.exists():
            continue
        if runs_root.is_symlink() or not runs_root.is_dir():
            raise RuntimeError("Python runs root is unsafe")
        for active_file in sorted(runs_root.glob("*/active")):
            run_dir = active_file.parent.resolve()
            try:
                if active_file.is_symlink() or not active_file.is_file():
                    raise RuntimeError("Python active marker is unsafe")
                meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("Python active run metadata is unreadable") from exc
            if (
                not isinstance(meta, dict)
                or meta.get("run_id") != run_dir.name
                or not isinstance(meta.get("workflow"), str)
                or not meta["workflow"]
            ):
                raise RuntimeError("Python active run identity is invalid")
            active.append(_python_hook_run(meta, run_dir, workspace, worktree_runtime.name))
        for manifest_path in sorted(runs_root.glob("*/*/manifest.json")):
            run_dir = manifest_path.parent.resolve()
            try:
                if manifest_path.is_symlink() or not manifest_path.is_file():
                    raise RuntimeError("Python run manifest is unsafe")
                state = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("Python run manifest is unreadable") from exc
            if (
                isinstance(state, dict)
                and isinstance(state.get("workflow"), str)
                and "workflow_id" not in state
            ):
                continue
            if not isinstance(state, dict) or "workflow_id" not in state:
                raise RuntimeError("run manifest runtime is ambiguous")
            raw_run_dir = state.get("run_dir")
            resolved_run_dir = (
                Path(raw_run_dir).resolve()
                if isinstance(raw_run_dir, str) and Path(raw_run_dir).is_absolute()
                else (worktree_runtime / str(raw_run_dir or "")).resolve()
            )
            if (
                state.get("workflow_id") != manifest_path.parents[1].name
                or state.get("run_id") != run_dir.name
                or resolved_run_dir != run_dir
                or not isinstance(state.get("status"), str)
            ):
                raise RuntimeError("Python run manifest identity is invalid")
            if state["status"] in {"complete", "aborted"}:
                continue
            active.append(_python_hook_run(state, run_dir, workspace, worktree_runtime.name))
    if len(active) > 1:
        raise RuntimeError("multiple Python runs are active")
    return active[0] if active else None


def _runtime_worktree_identity(root: Path, worktree_runtime: Path) -> tuple[dict, Path, str]:
    try:
        manifest_path = worktree_runtime / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise RuntimeError("Python worktree manifest is unsafe")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Python worktree manifest is unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("name") != worktree_runtime.name:
        raise RuntimeError("Python worktree manifest name is invalid")
    raw_workspace = manifest.get("path")
    branch = manifest.get("branch")
    if not isinstance(raw_workspace, str) or not raw_workspace or not isinstance(branch, str):
        raise RuntimeError("Python worktree manifest is invalid")
    workspace_path = Path(raw_workspace)
    workspace = workspace_path.resolve() if workspace_path.is_absolute() else (root / workspace_path).resolve()
    if _registered_worktree_branch(root, workspace) != branch or branch in {"main", "master", "develop"}:
        raise RuntimeError("Python active run worktree is not registered")
    return manifest, workspace, branch


def _python_hook_run(state: dict, run_dir: Path, workspace: Path, worktree_name: str) -> dict:
    workflow = state.get("workflow_id") or state.get("workflow")
    return {
        "_runtime_kind": "python",
        "_control_root": run_dir,
        "run_id": state["run_id"],
        "run_dir": str(run_dir),
        "status": state.get("status", "running"),
        "task": state.get("task") if isinstance(state.get("task"), str) else "",
        "workflow": workflow,
        "workspace_root": str(workspace),
        "worktree_mode": state.get("worktree_mode"),
        "worktree_name": worktree_name,
    }


def _python_leader_active_run(root: Path) -> dict | None:
    runs_root = root / ".agent-flow" / "runs"
    if not runs_root.exists():
        return None
    if runs_root.is_symlink() or not runs_root.is_dir():
        raise RuntimeError("leader Python runs root is unsafe")
    active: list[dict] = []
    active_files = sorted(
        candidate
        for candidate in runs_root.glob("*/active")
        if candidate.is_file() and not candidate.is_symlink()
    )
    if len(active_files) > 1:
        raise RuntimeError("multiple leader Python runs are active")
    if active_files:
        active_file = active_files[0]
        run_dir = active_file.parent.resolve()
        try:
            meta_path = run_dir / "meta.json"
            if meta_path.is_symlink() or not meta_path.is_file():
                raise RuntimeError("leader Python active run metadata is unsafe")
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("leader Python active run metadata is unreadable") from exc
        if (
            not isinstance(meta, dict)
            or meta.get("run_id") != run_dir.name
            or not isinstance(meta.get("workflow"), str)
            or not meta["workflow"]
        ):
            raise RuntimeError("leader Python active run identity is invalid")
        active.append(_python_hook_run(meta, run_dir, root, ""))
    for manifest_path in sorted(runs_root.glob("*/*/manifest.json")):
        run_dir = manifest_path.parent.resolve()
        try:
            if manifest_path.is_symlink() or not manifest_path.is_file():
                raise RuntimeError("leader Python run manifest is unsafe")
            state = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("leader Python run manifest is unreadable") from exc
        if (
            isinstance(state, dict)
            and isinstance(state.get("workflow"), str)
            and "workflow_id" not in state
        ):
            continue
        if not isinstance(state, dict) or "workflow_id" not in state:
            raise RuntimeError("run manifest runtime is ambiguous")
        raw_run_dir = state.get("run_dir")
        resolved_run_dir = (
            Path(raw_run_dir).resolve()
            if isinstance(raw_run_dir, str) and Path(raw_run_dir).is_absolute()
            else (root / str(raw_run_dir or "")).resolve()
        )
        if (
            state.get("workflow_id") != manifest_path.parents[1].name
            or state.get("run_id") != run_dir.name
            or resolved_run_dir != run_dir
            or not isinstance(state.get("status"), str)
        ):
            raise RuntimeError("leader Python run manifest identity is invalid")
        if state["status"] in {"complete", "aborted"}:
            continue
        active.append(_python_hook_run(state, run_dir, root, ""))
    if len(active) > 1:
        raise RuntimeError("multiple leader Python runs are active")
    return active[0] if active else None


def _node_run_is_active(state: dict) -> bool:
    return state.get("status") not in {"complete", "aborted"} and state.get("phase") != "complete"


def _read_node_run_state(path: Path, label: str) -> dict:
    try:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"Node {label} is unsafe")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Node {label} is unreadable") from exc
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("run_id"), str)
        or not payload["run_id"]
        or not isinstance(payload.get("workflow"), str)
        or not payload["workflow"]
        or not isinstance(payload.get("run_dir"), str)
        or not payload["run_dir"]
        or not isinstance(payload.get("status"), str)
    ):
        raise RuntimeError(f"Node {label} is invalid")
    return payload


def _node_state_roots(root: Path, git_common: Path | None) -> list[Path]:
    roots = [root]
    if git_common is None:
        return roots
    runtime_root = git_common / "agent-flow" / "worktrees"
    if not runtime_root.exists():
        return roots
    if runtime_root.is_symlink() or not runtime_root.is_dir():
        raise RuntimeError("Node runtime worktree root is unsafe")
    roots.extend(
        entry for entry in sorted(runtime_root.iterdir(), key=lambda item: item.name)
        if entry.is_dir() and not entry.is_symlink()
    )
    return roots


def _node_run_manifests(root: Path, git_common: Path | None) -> list[dict]:
    manifests: list[dict] = []
    for state_root in _node_state_roots(root, git_common):
        runs_root = state_root / ".agent-flow" / "runs"
        if not runs_root.exists():
            continue
        if runs_root.is_symlink() or not runs_root.is_dir():
            raise RuntimeError("Node runs root is unsafe")
        for workflow_dir in sorted(runs_root.iterdir(), key=lambda entry: entry.name):
            if workflow_dir.is_symlink() or not workflow_dir.is_dir():
                continue
            for run_dir in sorted(workflow_dir.iterdir(), key=lambda entry: entry.name):
                if run_dir.is_symlink() or not run_dir.is_dir():
                    continue
                manifest_path = run_dir / "manifest.json"
                if not manifest_path.exists() and not manifest_path.is_symlink():
                    continue
                try:
                    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise RuntimeError("Node run manifest is unreadable") from exc
                if isinstance(raw, dict) and "workflow_id" in raw and "workflow" not in raw:
                    continue
                manifest = _read_node_run_state(manifest_path, "run manifest")
                raw_run_dir = Path(manifest["run_dir"])
                resolved_run_dir = raw_run_dir.resolve() if raw_run_dir.is_absolute() else (state_root / raw_run_dir).resolve()
                if (
                    manifest["workflow"] != workflow_dir.name
                    or manifest["run_id"] != run_dir.name
                    or resolved_run_dir != run_dir.resolve()
                ):
                    raise RuntimeError("Node run manifest identity mismatch")
                manifests.append(
                    {
                        **manifest,
                        "run_dir": str(run_dir.resolve()),
                        "_control_root": run_dir.resolve(),
                    }
                )
    return manifests


def _same_node_run(root: Path, left: dict, right: dict) -> bool:
    left_run_dir = Path(left["run_dir"])
    right_run_dir = Path(right["run_dir"])
    return (
        left.get("run_id") == right.get("run_id")
        and left.get("workflow") == right.get("workflow")
        and (
            left_run_dir.resolve()
            if left_run_dir.is_absolute()
            else (root / left_run_dir).resolve()
        )
        == (
            right_run_dir.resolve()
            if right_run_dir.is_absolute()
            else (root / right_run_dir).resolve()
        )
    )


def _active_run_state(root: Path, git_common: Path | None) -> dict:
    pointers: list[dict] = []
    for state_root in _node_state_roots(root, git_common):
        state = state_root / ".agent-flow" / "state" / "current-run.json"
        if state.exists() or state.is_symlink():
            pointer = _read_node_run_state(state, "active run state")
            pointers.append(
                {
                    **pointer,
                    "run_dir": str((state_root / pointer["run_dir"]).resolve()),
                }
            )
    try:
        manifests = _node_run_manifests(root, git_common)
    except OSError as exc:
        raise RuntimeError("Node run manifests are unreadable") from exc
    active_manifests = [manifest for manifest in manifests if _node_run_is_active(manifest)]
    if len(active_manifests) > 1:
        raise RuntimeError("multiple Node run manifests are active")
    node_run = active_manifests[0] if active_manifests else None
    active_pointers = [pointer for pointer in pointers if _node_run_is_active(pointer)]
    if len(active_pointers) > 1:
        raise RuntimeError("multiple Node active run pointers found")
    if active_pointers:
        if node_run is None or not _same_node_run(root, active_pointers[0], node_run):
            raise RuntimeError("Node active run pointer does not match an active run manifest")
    if node_run is not None and (
        not isinstance(node_run.get("workspace_root"), str)
        or not node_run["workspace_root"]
    ):
        raise RuntimeError("Node active run workspace is invalid")
    python_candidates = [_python_leader_active_run(root)]
    if git_common is not None:
        python_candidates.insert(0, _python_active_run(root, git_common))
    python_runs = [run for run in python_candidates if run is not None]
    if len(python_runs) > 1:
        raise RuntimeError("multiple Python runs are active")
    python_run = python_runs[0] if python_runs else None
    if node_run and python_run:
        raise RuntimeError("Node and Python runs are active at the same time")
    return node_run or python_run or {}


def _non_git_project_root(cwd: Path) -> Path | None:
    candidates: list[Path] = []
    script_name = globals().get("__file__")
    if (
        not isinstance(script_name, str)
        or script_name.startswith("/dev/fd/")
    ):
        script_name = os.environ.get("AGENT_FLOW_MANAGED_HOOK_SOURCE_PATH", "")
    script = Path(script_name).resolve() if script_name else Path()
    if len(script.parents) > 3 and script.parents[2].name == ".agent-flow":
        candidates.append(script.parents[3])
    candidates.extend((cwd, *cwd.parents))
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        agent_flow = resolved / ".agent-flow"
        try:
            if agent_flow.is_symlink() or not agent_flow.is_dir():
                continue
        except OSError:
            continue
        markers = (
            agent_flow / "kit.json",
            agent_flow / "state" / "current-run.json",
            agent_flow / "runs",
        )
        if any(marker.exists() and not marker.is_symlink() for marker in markers):
            return resolved
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        print("agent-flow: blocked because hook input could not be validated", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("agent-flow: blocked because hook input could not be validated", file=sys.stderr)
        return 2
    cwd = Path(str(payload.get("cwd") or os.getcwd())).resolve()
    if _is_embedded_execution_tool(payload):
        print(
            "agent-flow: blocked embedded execution tool because its writes cannot be proven path-safe",
            file=sys.stderr,
        )
        return 2
    git_common: Path | None
    try:
        top = Path(subprocess.check_output([_TRUSTED_GIT, "rev-parse", "--show-toplevel"], cwd=cwd, text=True, stderr=subprocess.DEVNULL).strip()).resolve()
        common = Path(subprocess.check_output([_TRUSTED_GIT, "rev-parse", "--git-common-dir"], cwd=cwd, text=True, stderr=subprocess.DEVNULL).strip())
        git_common = (top / common).resolve() if not common.is_absolute() else common.resolve()
        root = git_common.parent
    except (OSError, subprocess.CalledProcessError):
        root = _non_git_project_root(cwd)
        if root is None:
            bash_command = _bash_command(payload)
            if bash_command is not None and _leader_bash_is_read_only(
                bash_command,
                allow_start=False,
            ):
                return 0
            print(
                "agent-flow: blocked because non-git project boundaries could not be validated",
                file=sys.stderr,
            )
            return 2
        top = root
        git_common = None
    try:
        run = _active_run_state(root, git_common)
    except RuntimeError as exc:
        print(f"agent-flow: blocked because {exc}", file=sys.stderr)
        return 2
    active = bool(run) and run.get("status") not in {"complete", "aborted"}
    control_root = _active_run_control_root(run, root) if active else None
    if active and control_root is None:
        print("agent-flow: blocked because active run control root is invalid", file=sys.stderr)
        return 2
    pinned = run.get("workspace_root") if active else None
    workspace = None
    if isinstance(pinned, str) and pinned:
        pinned_path = Path(pinned)
        workspace = (
            pinned_path.resolve()
            if pinned_path.is_absolute()
            else (root / pinned_path).resolve()
        )
    leader_disabled = (
        active
        and workspace == root
        and run.get("worktree_mode") == "disabled"
    )
    bash_command = _bash_command(payload)
    if bash_command is not None:
        if top == root:
            if leader_disabled:
                if _worktree_bash_is_safe(
                    bash_command,
                    root=root,
                    workspace=root,
                    cwd=cwd,
                    control_root=control_root,
                ):
                    return 0
                print(
                    "agent-flow: blocked command targeting outside the disabled-mode leader workspace",
                    file=sys.stderr,
                )
                return 2
            if _leader_bash_is_read_only(
                bash_command,
                allow_start=not active,
                root=root,
                cwd=cwd,
            ):
                return 0
            print("agent-flow: blocked leader checkout command; continue from the active worktree", file=sys.stderr)
            return 2
        allowed_workspace = workspace if active else top
        detached_start = (
            not active
            and _registered_detached_worktree(root, top)
            and _detached_initial_start_command(bash_command)
        )
        if (
            (not _registered_task_worktree(root, top) and not detached_start)
            or (active and top != workspace)
        ):
            print(f"agent-flow: blocked command from non-pinned worktree; use {allowed_workspace}", file=sys.stderr)
            return 2
        if not _worktree_bash_is_safe(
            bash_command,
            root=root,
            workspace=top,
            cwd=cwd,
            control_root=control_root,
        ):
            print("agent-flow: blocked command targeting outside the active worktree", file=sys.stderr)
            return 2
        return 0
    targets = list(_values(payload))
    if not targets:
        print("agent-flow: blocked because write target was not provided", file=sys.stderr)
        return 2
    resolved_targets = []
    for raw in targets:
        candidate = Path(raw)
        resolved_targets.append(
            (cwd / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        )
    control_only = active and all(_is_control_target(candidate, control_root) for candidate in resolved_targets)
    if (
        top == root
        and (not active or workspace in {None, root})
        and not control_only
        and not leader_disabled
    ):
        print("agent-flow: blocked leader checkout write; start or continue in a feature worktree", file=sys.stderr)
        return 2
    target_workspace = workspace if active else top
    if (
        target_workspace is None
        or (target_workspace == root and not leader_disabled)
        or (
            target_workspace != root
            and not _registered_task_worktree(root, target_workspace)
        )
    ):
        if not control_only:
            print(f"agent-flow: blocked unregistered pinned worktree; use {target_workspace}", file=sys.stderr)
            return 2
    if top not in {root, target_workspace}:
        print(f"agent-flow: blocked write from non-pinned worktree; use {target_workspace}", file=sys.stderr)
        return 2
    for candidate in resolved_targets:
        if _is_control_target(candidate, control_root):
            continue
        if candidate != target_workspace and target_workspace not in candidate.parents:
            print("agent-flow: blocked path outside the active worktree", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
