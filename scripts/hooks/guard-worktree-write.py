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
from typing import Any, Mapping


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
    "false",
    "git",
    "grep",
    "head",
    "ls",
    "pwd",
    "readlink",
    "rg",
    "stat",
    "tail",
    "test",
    "true",
    "type",
    "uniq",
    "wc",
    "which",
}
SHELL_BUILTIN_READ_ONLY_COMMANDS = {"echo", "false", "printf", "pwd", "test", "true", "type"}
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
SHELL_BUILTIN_COMMANDS = (
    SHELL_CWD_COMMANDS
    | {"declare", "eval", "export", "readonly", "typeset", "unset"}
    | SHELL_BUILTIN_READ_ONLY_COMMANDS
)
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
GRADLE_OPTION_ENVIRONMENT = (
    "GRADLE_OPTS",
    "JDK_JAVA_OPTIONS",
    "JAVA_TOOL_OPTIONS",
    "_JAVA_OPTIONS",
    "JAVA_OPTS",
)
MANAGED_MARKER_START = "<!-- agent-flow:start -->"
MANAGED_MARKER_END = "<!-- agent-flow:end -->"


def _authenticate_runtime(leader_root: Path) -> dict[str, Any]:
    return _verify_boundary_runtime(
        leader_root,
        leader_root / ".agent-flow" / "runtime" / "python",
    )


def _checkout_at(candidate: Path) -> tuple[Path, Path, Path | None] | None:
    marker = candidate / ".git"
    try:
        fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        # O_NOFOLLOW makes a swapped-in symlink marker fail closed instead of
        # letting a racing process redirect us at another repo's gitdir.
        raise ValueError(
            "write boundary rejected: git checkout marker is a symlink or unreadable"
        ) from exc
    try:
        marker_stat = os.fstat(fd)
        if stat.S_ISDIR(marker_stat.st_mode):
            root = candidate.resolve(strict=True)
            return root, root, None
        if not stat.S_ISREG(marker_stat.st_mode):
            raise ValueError("write boundary rejected: git checkout marker is invalid")
        value = os.read(fd, 65536).decode("utf-8", "strict").strip()
    finally:
        os.close(fd)
    if not value.startswith("gitdir:"):
        raise ValueError("write boundary rejected: linked worktree marker is invalid")
    git_dir = Path(value.split(":", 1)[1].strip())
    if not git_dir.is_absolute():
        git_dir = candidate / git_dir
    git_dir = git_dir.resolve(strict=True)
    if not git_dir.is_dir():
        raise ValueError("write boundary rejected: linked worktree git directory is invalid")
    common = git_dir
    while common.name != ".git" and common != common.parent:
        common = common.parent
    if common.name != ".git" or not common.is_dir():
        raise ValueError("write boundary rejected: git common directory is invalid")
    worktrees_root = (common / "worktrees").resolve(strict=True)
    if git_dir == worktrees_root or worktrees_root not in git_dir.parents:
        raise ValueError("write boundary rejected: linked worktree git directory is untrusted")
    leader = common.parent.resolve(strict=True)
    if (leader / ".git").resolve(strict=True) != common:
        raise ValueError("write boundary rejected: git leader checkout is invalid")
    return candidate.resolve(strict=True), leader, git_dir


def _enclosing_checkout(cwd: Path) -> tuple[Path, Path, Path | None] | None:
    absolute = Path(os.path.abspath(cwd))
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise ValueError("write boundary rejected: mutation cwd is unavailable") from exc
    if not resolved.is_dir():
        raise ValueError("write boundary rejected: mutation cwd is not a directory")

    lexical_candidates: list[Path] = []
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor /= part
        try:
            metadata = cursor.lstat()
        except OSError as exc:
            raise ValueError("write boundary rejected: mutation cwd is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            break
        lexical_candidates.append(cursor)

    seen: set[Path] = set()
    for candidate in (
        *reversed(lexical_candidates),
        resolved,
        *resolved.parents,
    ):
        if candidate in seen:
            continue
        seen.add(candidate)
        checkout = _checkout_at(candidate)
        if checkout is not None:
            return checkout
    return None


def _host_argument() -> str:
    try:
        index = sys.argv.index("--host")
    except ValueError:
        return ""
    return sys.argv[index + 1] if index + 1 < len(sys.argv) else ""


def _boundary_error(
    *,
    requested: str,
    resolved: Path,
    root: Path,
    host: str,
    reason_code: str,
    reason: str,
) -> ValueError:
    return ValueError(
        "write boundary rejected: "
        f"requested_path={requested} resolved_path={resolved} "
        f"pinned_workspace_root={root} host={host} "
        f"reason_code={reason_code} reason={reason}"
    )


def _run_area(pinned_root: Path, leader_root: Path, resolved: Path) -> Path | None:
    # Return the `.agent-flow/runs` root that contains `resolved` when it is a
    # run directory this checkout may write phase artifacts into. The leader
    # orchestrates every run: its own checkout-local runs plus any worktree's
    # git-private runs (omp keeps cwd on the leader even for worktree runs). A
    # linked worktree may write only its OWN runs — checkout-local or the
    # git-private root keyed to its name — never a sibling's or the leader's.
    # Lifecycle files stay gated afterward by _is_allowed_run_artifact.
    if pinned_root == leader_root:
        checkout_runs = leader_root / ".agent-flow" / "runs"
        if resolved == checkout_runs or checkout_runs in resolved.parents:
            return checkout_runs
        private_worktrees = leader_root / ".git" / "agent-flow" / "worktrees"
        try:
            parts = resolved.relative_to(private_worktrees).parts
        except ValueError:
            return None
        if len(parts) >= 4 and parts[1] == ".agent-flow" and parts[2] == "runs":
            return private_worktrees / parts[0] / ".agent-flow" / "runs"
        return None
    worktree_runs = pinned_root / ".agent-flow" / "runs"
    if resolved == worktree_runs or worktree_runs in resolved.parents:
        return worktree_runs
    own_private_runs = (
        leader_root / ".git" / "agent-flow" / "worktrees"
        / pinned_root.name / ".agent-flow" / "runs"
    )
    if resolved == own_private_runs or own_private_runs in resolved.parents:
        return own_private_runs
    return None


def _is_allowed_run_artifact(run_root: Path, resolved: Path) -> bool:
    try:
        parts = resolved.relative_to(run_root).parts
    except ValueError:
        return False
    if not parts or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.(?:md|log)", parts[-1]) is None:
        return False
    if len(parts) in {3, 4} and parts[-2] == "artifacts":
        return True
    return len(parts) in {2, 3} and resolved.suffix == ".md"


def _ensure_cwd_within_pinned(pinned_root: Path, cwd: Path, host: str) -> None:
    root = pinned_root.resolve(strict=True)
    current = cwd.resolve(strict=True)
    if current != root and root not in current.parents:
        raise _boundary_error(
            requested=str(cwd),
            resolved=current,
            root=root,
            host=host,
            reason_code="mutation_cwd_not_pinned",
            reason="mutating command must run from pinned workspace",
        )


def _resolve_within_pinned(
    pinned_root: Path,
    requested: str,
    cwd: Path,
    *,
    host: str,
    leader_root: Path,
) -> Path:
    root = pinned_root.resolve(strict=True)
    base = cwd.resolve(strict=True)
    requested_path = Path(requested)
    candidate = requested_path if requested_path.is_absolute() else base / requested_path
    resolved = candidate.resolve(strict=False)
    containing: Path | None = None
    runs_root = _run_area(root, leader_root, resolved)
    if runs_root is not None:
        if not _is_allowed_run_artifact(runs_root, resolved):
            raise _boundary_error(
                requested=requested,
                resolved=resolved,
                root=root,
                host=host,
                reason_code="protected_run_state_path",
                reason="run metadata is not writable; only phase artifacts are allowed",
            )
        containing = runs_root
    elif resolved == root or root in resolved.parents:
        git_metadata = root / ".git"
        if resolved == git_metadata or git_metadata in resolved.parents:
            raise _boundary_error(
                requested=requested,
                resolved=resolved,
                root=root,
                host=host,
                reason_code="git_metadata_write",
                reason="git metadata is not writable",
            )
        containing = root
    if containing is None:
        raise _boundary_error(
            requested=requested,
            resolved=resolved,
            root=root,
            host=host,
            reason_code="target_outside_pinned_workspace",
            reason="resolved path escapes pinned workspace",
        )
    existing_parent = resolved
    while not existing_parent.exists() and existing_parent != containing:
        existing_parent = existing_parent.parent
    try:
        parent_resolved = existing_parent.resolve(strict=True)
    except FileNotFoundError as exc:
        raise _boundary_error(
            requested=requested,
            resolved=resolved,
            root=root,
            host=host,
            reason_code="target_parent_missing",
            reason="target has no existing parent inside pinned workspace",
        ) from exc
    if parent_resolved != containing and containing not in parent_resolved.parents:
        raise _boundary_error(
            requested=requested,
            resolved=resolved,
            root=root,
            host=host,
            reason_code="target_parent_outside_pinned_workspace",
            reason="existing parent escapes pinned workspace",
        )
    return resolved


def _detected_execution_host(environment: Mapping[str, str]) -> str:
    if environment.get("CODEX_THREAD_ID") or environment.get("CODEX_CLI"):
        return "codex"
    if environment.get("CLAUDE_SESSION_ID") or environment.get("CLAUDECODE") or environment.get("CLAUDE_CLI"):
        return "claude"
    if environment.get("OMP_SESSION_ID") or environment.get("OMP_PROFILE"):
        return "omp"
    return ""


def _host_session_id(host: str, environment: Mapping[str, str]) -> str:
    names = {
        "codex": ("CODEX_THREAD_ID", "CODEX_SESSION_ID"),
        "claude": ("CLAUDE_SESSION_ID",),
        "omp": ("OMP_SESSION_ID",),
    }.get(host, ())
    return next((environment[name] for name in names if environment.get(name)), "")


def _host_agent_id(host: str, environment: Mapping[str, str]) -> str:
    names = {
        "codex": ("CODEX_AGENT_ID",),
        "claude": ("CLAUDE_AGENT_ID",),
        "omp": ("OMP_AGENT_ID",),
    }.get(host, ())
    return next((environment[name] for name in names if environment.get(name)), "")


def _leader_private_execution_identity(
    payload: dict[str, object],
    host: str,
) -> dict[str, str]:
    host = host.strip().lower()
    session_id = (
        os.environ.get("AGENT_FLOW_EXECUTION_ID", "").strip()
        or os.environ.get("AGENT_FLOW_SESSION_ID", "").strip()
        or _host_session_id(host, os.environ)
        or _context_value(payload, "execution_id", "thread_id", "session_id")
    )
    agent_id = (
        os.environ.get("AGENT_FLOW_AGENT_ID", "").strip()
        or _host_agent_id(host, os.environ)
        or _context_value(payload, "agent_id")
    )
    if not host or not session_id:
        raise ValueError(
            "execution_identity_missing: leader worktree-private artifact write "
            "did not declare a stable host session identity"
        )
    if (
        len(host) > 32
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in host)
        or len(session_id) > 512
        or len(agent_id) > 512
    ):
        raise ValueError("execution_identity_invalid: leader artifact execution identity")
    return {"host": host, "session_id": session_id, "agent_id": agent_id}


def _read_git_private_json(leader_root: Path, path: Path) -> dict[str, object]:
    git_root = leader_root / ".git"
    try:
        relative = path.relative_to(git_root)
    except ValueError as exc:
        raise ValueError("git-private metadata path escapes the checkout") from exc
    if len(relative.parts) < 2:
        raise ValueError("git-private metadata path is invalid")

    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    try:
        root_before = git_root.lstat()
        directory_fd = os.open(git_root, directory_flags)
    except OSError as exc:
        raise ValueError("git-private metadata directory is unavailable") from exc
    try:
        root_opened = os.fstat(directory_fd)
        if (
            root_opened.st_dev != root_before.st_dev
            or root_opened.st_ino != root_before.st_ino
            or not stat.S_ISDIR(root_opened.st_mode)
            or root_opened.st_uid != os.getuid()
            or stat.S_IMODE(root_opened.st_mode) & 0o022
        ):
            raise ValueError("git-private metadata directory is unsafe")
        for part in relative.parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            try:
                metadata = os.fstat(next_fd)
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) & 0o022
                ):
                    raise ValueError("git-private metadata directory is unsafe")
            except BaseException:
                os.close(next_fd)
                raise
            os.close(directory_fd)
            directory_fd = next_fd

        file_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            file_flags |= os.O_NOFOLLOW
        descriptor = os.open(relative.name, file_flags, dir_fd=directory_fd)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o022
            ):
                raise ValueError("git-private metadata file is unsafe")
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > 1024 * 1024:
                    raise ValueError("git-private metadata file is too large")
                chunks.append(chunk)
            repeated = os.fstat(descriptor)
            if repeated.st_dev != metadata.st_dev or repeated.st_ino != metadata.st_ino:
                raise ValueError("git-private metadata changed while reading")
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("git-private metadata is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("git-private metadata is not an object")
    return payload


def _ensure_leader_private_run_binding(
    payload: dict[str, object],
    pinned_root: Path,
    leader_root: Path,
    resolved: Path,
    host: str,
) -> None:
    private_worktrees = leader_root / ".git" / "agent-flow" / "worktrees"
    try:
        parts = resolved.relative_to(private_worktrees).parts
    except ValueError:
        return
    if len(parts) < 4 or parts[1:3] != (".agent-flow", "runs"):
        return

    execution = _leader_private_execution_identity(payload, host)
    digest = hashlib.sha256(
        json.dumps(execution, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    try:
        binding = _read_git_private_json(
            leader_root,
            leader_root / ".git" / "agent-flow" / "executions" / f"{digest}.json",
        )
    except FileNotFoundError as exc:
        raise ValueError(
            "execution_binding_missing: active execution is not bound to the "
            "worktree-private run"
        ) from exc
    if binding.get("execution") != execution:
        raise ValueError("execution_binding_invalid: execution identity mismatch")

    workspace_name = parts[0]
    run_dir = private_worktrees.joinpath(*parts[:4])
    workspace = binding.get("workspace")
    if binding.get("workspace_name") != workspace_name:
        raise ValueError(
            "execution_binding_conflict: active execution is bound to a different workspace"
        )
    expected_workspace = (leader_root / ".agent-flow" / "worktrees" / workspace_name).resolve(strict=True)
    expected_common_dir = (leader_root / ".git").resolve(strict=True)
    if not isinstance(workspace, dict) or (
        workspace.get("workspace_root") != str(expected_workspace)
        or workspace.get("git_common_dir") != str(expected_common_dir)
    ):
        raise ValueError("execution_binding_conflict: recovery workspace path changed")
    try:
        workspace_metadata = expected_workspace.lstat()
        manifest = _read_git_private_json(
            leader_root,
            private_worktrees / workspace_name / "manifest.json",
        )
    except OSError as exc:
        raise ValueError("execution_binding_stale: bound workspace is unavailable") from exc
    if (
        manifest.get("identity") != workspace
        or workspace_metadata.st_dev != workspace.get("device")
        or workspace_metadata.st_ino != workspace.get("inode")
    ):
        raise ValueError("execution_binding_stale: bound workspace identity changed")
    run_id = parts[3]
    if binding.get("run_id") != run_id:
        raise ValueError("execution_binding_conflict: active execution is bound to a different run")
    try:
        run_meta = _read_git_private_json(leader_root, run_dir / "meta.json")
    except OSError as exc:
        raise ValueError("execution_binding_stale: bound run is not active") from exc
    if run_meta.get("run_id") != run_id or run_meta.get("workspace") != workspace:
        raise ValueError("execution_binding_stale: bound run metadata changed")
    run_dir_value = binding.get("run_dir")
    if not isinstance(run_dir_value, str) or not run_dir_value:
        raise ValueError("execution_binding_invalid: run directory")
    try:
        authenticated_run = run_dir.resolve(strict=True)
        declared_run = Path(run_dir_value).resolve(strict=True)
        active = authenticated_run / "active"
        active_metadata = active.lstat()
    except OSError as exc:
        raise ValueError("execution_binding_stale: bound run is not active") from exc
    if declared_run != authenticated_run:
        raise ValueError(
            "execution_binding_conflict: active execution is bound to a different run"
        )
    if (
        stat.S_ISLNK(active_metadata.st_mode)
        or not stat.S_ISREG(active_metadata.st_mode)
    ):
        raise ValueError("execution_binding_stale: bound run is not active")

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
        multiple = value.get("paths")
        if isinstance(multiple, list):
            for item in multiple:
                if isinstance(item, str) and item:
                    paths.append(item)
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
        edit_patch = value.get("input")
        if isinstance(edit_patch, str):
            paths.extend(
                match.group(1).strip()
                for match in re.finditer(
                    r"^\[(.+)#[0-9A-Fa-f]{4}\][ \t]*$",
                    edit_patch,
                    re.MULTILINE,
                )
            )
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


def _read_only_shell_command_is_trusted(
    executable: str,
    command_name: str,
    shell_builtin_token: bool,
) -> bool:
    if shell_builtin_token:
        return command_name in SHELL_BUILTIN_READ_ONLY_COMMANDS
    if command_name not in READ_ONLY_SHELL_COMMANDS:
        return False
    trusted_location = shutil.which(command_name, path=os.defpath)
    if not trusted_location:
        return False
    try:
        if "/" in executable:
            candidate = Path(executable).resolve(strict=True)
        else:
            located = shutil.which(executable)
            if not located:
                return False
            candidate = Path(located).resolve(strict=True)
        trusted = Path(trusted_location).resolve(strict=True)
    except OSError:
        return False
    return candidate == trusted


def _read_only_command_arguments_are_safe(command_name: str, arguments: list[str]) -> bool:
    if command_name == "printf":
        return not any(argument == "-v" or argument.startswith("-v") for argument in arguments)
    if command_name == "git":
        if any(value for name, value in os.environ.items() if name.startswith("GIT_") or name == "PAGER"):
            return False
        dangerous = {
            "-c",
            "-C",
            "--config",
            "--config-env",
            "--exec-path",
            "--ext-diff",
            "--paginate",
            "--textconv",
        }
        return not any(
            argument in dangerous
            or (argument.startswith("-c") and not argument.startswith("--"))
            or argument.startswith("--config=")
            or argument.startswith("--config-env=")
            or argument.startswith("--exec-path=")
            for argument in arguments
        )
    if command_name == "rg":
        if any(value for name, value in os.environ.items() if name.startswith("RIPGREP_")):
            return False
        return not any(
            argument == "--pre"
            or argument.startswith("--pre=")
            or argument.startswith("--pre-glob")
            for argument in arguments
        )
    return True


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


_ADB_GLOBAL_OPTIONS_WITH_VALUE = {"-s", "-t", "-H", "-P", "-L", "--one-device"}
# adb subcommands that run on the device or only read host files, so their
# path operands are device-side paths, never host write targets. Host-writing
# subcommands (pull, backup, bugreport) and unknown subcommands are excluded so
# they stay under conservative host-path collection.
_ADB_HOST_READ_ONLY_SUBCOMMANDS = {
    "shell",
    "exec-out",
    "emu",
    "push",
    "install",
    "install-multiple",
    "install-multi-package",
    "uninstall",
    "sync",
    "sideload",
    "devices",
    "get-state",
    "get-serialno",
    "get-devpath",
    "logcat",
    "forward",
    "reverse",
    "connect",
    "disconnect",
    "reconnect",
    "pair",
    "root",
    "unroot",
    "remount",
    "reboot",
    "tcpip",
    "usb",
    "jdwp",
    "start-server",
    "kill-server",
}


def _adb_subcommand(arguments: list[str]) -> str | None:
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token.startswith("-"):
            index += 2 if token in _ADB_GLOBAL_OPTIONS_WITH_VALUE else 1
            continue
        # wait-for-* is a transport prefix that chains a real subcommand after
        # it, so skip it and classify the following subcommand instead.
        if token.startswith("wait-for-"):
            index += 1
            continue
        return token
    return None


def _adb_subcommand_is_host_read_only(arguments: list[str]) -> bool:
    return _adb_subcommand(arguments) in _ADB_HOST_READ_ONLY_SUBCOMMANDS


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
            and command_name in SHELL_BUILTIN_COMMANDS
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
            command_name in (SHELL_CWD_COMMANDS - {".", "source"})
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
        interpreter_read_only = (
            not segment_inline
            and _script_interpreter_invocation_is_read_only(
                command_name,
                arguments,
            )
        )
        segment_read_only = (
            state_only_command
            or interpreter_read_only
            or (
                _read_only_shell_command_is_trusted(
                    unwrapped[0], command_name, shell_builtin_token
                )
                and _read_only_command_arguments_are_safe(command_name, arguments)
                and (git_command is None or _git_command_is_read_only(git_command))
                and not segment_mutating_options
                and not segment_unsafe
                and not segment_inline
            )
        )
        if not segment_read_only:
            mutating_segment = True
            dynamic_target = (
                dynamic_target
                or segment_unsafe
                or command_name in {"sed", "sort", "xargs"}
            )
            segment_paths: list[str] = list(declared_paths)
            positional = [argument for argument in arguments if not argument.startswith("-")]
            if git_command is not None:
                segment_paths.extend(git_command[2])
            elif sed_mutating:
                segment_paths.extend(sed_paths)
            elif perl_mutating:
                segment_paths.extend(perl_paths)
            elif command_name in {"cp", "install"}:
                target_directory = _target_directory_path(arguments)
                if target_directory is not None:
                    segment_paths.append(target_directory)
                elif positional:
                    segment_paths.append(positional[-1])
            elif command_name == "rsync":
                if _rsync_has_unresolved_output_option(arguments):
                    dynamic_target = True
                elif positional and _rsync_destination_is_remote(positional[-1]):
                    dynamic_target = True
                elif positional:
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
            elif command_name == "adb" and _adb_subcommand_is_host_read_only(
                arguments
            ):
                # device-side operands are not host paths; outer shell
                # redirections are still collected separately above.
                pass
            elif command_name == "adb" and _adb_subcommand(arguments) == "pull" and positional:
                segment_paths.append(positional[-1])
            elif command_name == "adb":
                dynamic_target = True
            elif (
                not segment_inline
                and (command_name in SCRIPT_INTERPRETERS or _has_script_program_argument(arguments))
            ):
                # An interpreter's program is input, not a declared write
                # target.  A renamed wrapper has no trustworthy output target
                # either, so treat script execution as unresolved.
                dynamic_target = True
            else:
                # An unmodelled executable can interpret any operand as an
                # output path; inline literals do not make the executable or
                # its other, potentially constructed targets trustworthy.
                dynamic_target = True
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
            if not segment_paths and not (
                command_name == "adb" and _adb_subcommand_is_host_read_only(arguments)
            ):
                # An unclassified mutator (including a renamed interpreter) has
                # no trustworthy write destination.  Do not let the current
                # working directory stand in for one: that would make a hidden
                # target fail open.
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
    # Normalize statically-known string concatenation before looking for mutation
    # APIs.  This catches common indirect calls such as
    # ``getattr(os, 'sym' + 'link')(...)`` without treating arbitrary getattr
    # reads as writes.
    normalized = command
    for _ in range(8):
        expanded = re.sub(
            r"(['\"])([^'\"\\]*)\1\s*\+\s*\1([^'\"\\]*)\1",
            lambda match: f"{match.group(1)}{match.group(2)}{match.group(3)}{match.group(1)}",
            normalized,
        )
        if expanded == normalized:
            break
        normalized = expanded
    mutation_apis = (
        "write_text|write_bytes|unlink|mkdir|makedirs|remove|rename|replace|"
        "copy|copy2|copyfile|symlink|writeFileSync|appendFileSync|"
        "createWriteStream|copyFileSync|symlinkSync|rmSync|unlinkSync|"
        "mkdirSync|renameSync"
    )
    return re.search(
        rf"\b(?:{mutation_apis})\s*\(|"
        rf"\bgetattr\s*\([^,]+,\s*['\"](?:{mutation_apis})['\"]\s*\)\s*\(|"
        r"\b(File\.(?:write|delete|rename)|open)\s*\([^)]*,?\s*['\"][wax+]",
        normalized,
        re.IGNORECASE,
    ) is not None


def _script_interpreter_invocation_is_read_only(
    command_name: str,
    arguments: list[str],
) -> bool:
    if command_name not in SCRIPT_INTERPRETERS:
        return False
    is_python = command_name == "py" or command_name.startswith("python")
    if is_python:
        options: list[str] = []
        index = 0
        while index < len(arguments) and arguments[index] in {"-I", "-S"}:
            options.append(arguments[index])
            index += 1
        if set(options) != {"-I", "-S"}:
            return False
        remaining = arguments[index:]
        if remaining in (["--version"], ["-V"]):
            return True
        if len(remaining) != 2 or remaining[0] != "-c":
            return False
        source = remaining[1].strip()
        literal = r"(?:['\"][^'\"\\]*['\"]|[-+]?\d+(?:\.\d+)?|True|False|None)"
        return re.fullmatch(rf"print\(\s*{literal}\s*\)", source) is not None
    if arguments in (["--version"], ["-V"]):
        return True
    if len(arguments) == 2 and arguments[0] == "-e":
        source = arguments[1].strip()
        literal = r"(?:['\"][^'\"\\]*['\"]|[-+]?\d+(?:\.\d+)?|True|False|None)"
        return re.fullmatch(rf"console\.log\(\s*{literal}\s*\)", source) is not None
    return False


def _has_script_program_argument(arguments: list[str]) -> bool:
    script_suffixes = {".py", ".pyw", ".js", ".cjs", ".mjs", ".rb", ".pl"}
    return any(
        not argument.startswith("-")
        and Path(argument.strip("'\"")).suffix.lower() in script_suffixes
        for argument in arguments
    )


def _has_nested_shell_evaluation(command: str) -> bool:
    stripped = command.lstrip()
    if stripped.startswith(("(", "{")) or re.search(r"(?:[;|&]\s*)[({]", command):
        return True
    for segment, _operator in _split_shell_commands(command):
        try:
            words = _shell_segment_words(segment)
        except ValueError:
            return True
        if words and _shell_command_name(words[0]) == "env" and any(
            argument in {"-S", "--split-string"}
            or argument.startswith("--split-string=")
            for argument in words[1:]
        ):
            return True
        unwrapped = _unwrap_shell_command(words)
        if not unwrapped:
            continue
        executable, *arguments = unwrapped
        command_name = _shell_command_name(executable)
        if command_name == "env" and any(
            argument in {"-S", "--split-string"}
            or argument.startswith("--split-string=")
            for argument in arguments
        ):
            return True
        if command_name == "eval":
            return True
        if command_name in {"bash", "sh", "dash", "zsh", "ksh"} and _shell_c_command(
            arguments
        ) is not None:
            return True
    return False


def _untrusted_shell_startup_assignment_name(name: str) -> bool:
    return (
        name
        in {
            "BASH_ENV",
            "ENV",
            "NODE_OPTIONS",
            "NODE_PATH",
            "NODE_V8_COVERAGE",
            "NODE_COMPILE_CACHE",
            "PATH",
        }
        or name.startswith("GIT_")
        or name.startswith("RIPGREP_")
        or name == "PAGER"
        or name.startswith("PYTHON")
        or name.startswith("LD_")
        or name.startswith("DYLD_")
        or name.startswith("BASH_FUNC_")
    )


def _has_untrusted_shell_startup_assignment(command: str) -> bool:
    return any(
        _untrusted_shell_startup_assignment_name(match.group(1))
        for match in re.finditer(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)=", command)
    )


def _has_untrusted_shell_startup_environment() -> bool:
    return any(
        value
        and (
            name in {"BASH_ENV", "ENV"}
            or name.startswith("LD_")
            or name.startswith("DYLD_")
            or name.startswith("BASH_FUNC_")
        )
        for name, value in os.environ.items()
    )


def _ensure_read_only_interpreter_is_trusted(
    command: str,
    cwd: Path,
    leader_root: Path,
) -> None:
    """Fail closed for syntactically read-only Python/Node invocations.

    The shell guard does not execute the command, so a read-only allowlist is
    safe only when the executable is the authenticated runtime and no startup
    environment can preload worktree code.
    """
    if _has_nested_shell_evaluation(command):
        raise ValueError("nested shell evaluation is not trusted")
    segments = _split_shell_commands(command)
    if len(segments) != 1 or segments[0][1]:
        for segment, _operator in segments:
            try:
                words = _shell_segment_words(segment)
            except ValueError:
                continue
            unwrapped = _unwrap_shell_command(words)
            if not unwrapped:
                continue
            executable, *arguments = unwrapped
            if _shell_command_name(executable) in SCRIPT_INTERPRETERS:
                raise ValueError("untrusted interpreter invocation")
        return
    segment = segments[0][0]
    try:
        words = _shell_segment_words(segment)
    except ValueError:
        return
    unwrapped = _unwrap_shell_command(words)
    if not unwrapped:
        return
    executable, *arguments = unwrapped
    command_name = _shell_command_name(executable)
    if command_name not in SCRIPT_INTERPRETERS:
        return
    if not _script_interpreter_invocation_is_read_only(command_name, arguments):
        raise ValueError("mutating interpreter invocation is not trusted")
    if (
        words != unwrapped
        or _shell_redirection_paths(segment)
        or _has_active_shell_substitution(segment)
        or _has_shell_parameter_expansion(segment)
    ):
        raise ValueError("untrusted read-only interpreter invocation")
    startup_environment = {
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "PYTHONWARNINGS",
        "PYTHONPYCACHEPREFIX",
        "NODE_OPTIONS",
        "NODE_PATH",
        "NODE_V8_COVERAGE",
        "NODE_COMPILE_CACHE",
        "BASH_ENV",
        "ENV",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
    }
    if any(os.environ.get(name) for name in startup_environment) or any(
        value
        and (
            name.startswith("LD_")
            or name.startswith("DYLD_")
            or name.startswith("BASH_FUNC_")
        )
        for name, value in os.environ.items()
    ):
        raise ValueError("untrusted read-only interpreter startup environment")

    contract = _authenticate_runtime(leader_root)
    if "/" in executable:
        candidate = Path(executable)
        if not candidate.is_absolute():
            candidate = cwd / candidate
    else:
        located = shutil.which(executable)
        if not located:
            raise ValueError("untrusted read-only interpreter executable")
        candidate = Path(located)
    try:
        resolved = candidate.resolve(strict=True)
        if "node_runtime" in contract:
            if command_name in {"node", "nodejs"}:
                # v2 contracts pin the managed Node runtime tree but predate an
                # executable contract.  Keep legacy read-only compatibility
                # only for a host system Node, never a workspace/PATH shadow.
                if not any(
                    resolved.is_relative_to(root)
                    for root in (Path("/usr/bin"), Path("/usr/local/bin"), Path("/usr/local/lib"))
                ):
                    raise ValueError("legacy Node executable is not a trusted system runtime")
                trusted = resolved
            else:
                # The guard process itself is the only authenticated Python
                # authority available to a v2 contract.
                trusted = Path(sys.executable).resolve(strict=True)
        elif command_name in {"node", "nodejs"}:
            trusted = Path(str(contract["node"]["path"])).resolve(strict=True)
        else:
            trusted = Path(str(contract["python"]["resolved_path"])).resolve(strict=True)
    except (KeyError, OSError, RuntimeError) as exc:
        raise ValueError("untrusted read-only interpreter executable") from exc
    if resolved != trusted:
        raise ValueError("untrusted read-only interpreter executable")


def _rsync_has_unresolved_output_option(arguments: list[str]) -> bool:
    safe_short_options = {"a", "c", "n", "q", "r", "t", "v"}
    safe_long_options = {
        "archive",
        "checksum",
        "dry-run",
        "quiet",
        "recursive",
        "times",
        "verbose",
    }
    for argument in arguments:
        if argument == "--":
            break
        if argument.startswith("--"):
            name = argument[2:].split("=", 1)[0]
            if name not in safe_long_options:
                return True
        elif argument.startswith("-") and argument != "-":
            if any(option not in safe_short_options for option in argument[1:]):
                return True
    return False


def _rsync_destination_is_remote(destination: str) -> bool:
    return destination.startswith("rsync://") or bool(
        re.match(r"^(?:[^/:\s]+@)?[^/:\s]+::?.*$", destination)
    )


def _target_directory_path(arguments: list[str]) -> str | None:
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            break
        if argument.startswith("--"):
            name, separator, value = argument[2:].partition("=")
            if name and "target-directory".startswith(name):
                if separator:
                    return value or None
                if index + 1 < len(arguments):
                    return arguments[index + 1]
                return None
        elif argument.startswith("-") and argument != "-":
            options = argument[1:]
            target_index = options.find("t")
            if target_index >= 0:
                attached_value = options[target_index + 1 :]
                if attached_value:
                    return attached_value
                if index + 1 < len(arguments):
                    return arguments[index + 1]
                return None
        index += 1
    return None


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

def _runtime_content_matches_contract(leader_root: Path) -> bool:
    try:
        kit = json.loads((leader_root / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
        contract = kit["project_runtime_contract"]
        commitment = _project_runtime_contract_commitment(contract)
        launcher = leader_root / str(contract["launcher"]["path"])
        node_runtime = leader_root / str(contract["runtime"]["path"])
        python_runtime = leader_root / str(contract["python_runtime"]["path"])
        embedded = EXPECTED_PROJECT_RUNTIME_CONTRACT_SHA256
        embedded_python = EXPECTED_PYTHON_RUNTIME_INTEGRITY
        return (
            contract["version"] == 3
            and kit["project_runtime_contract_commitment_version"] == 1
            and kit["project_runtime_contract_commitment"] == commitment
            and contract["launcher"]["path"] == ".agent-flow/bin/agent-flow"
            and contract["runtime"]["path"] == ".agent-flow/runtime/node"
            and contract["python_runtime"]["path"] == ".agent-flow/runtime/python"
            and launcher.is_file()
            and not launcher.is_symlink()
            and hashlib.sha256(launcher.read_bytes()).hexdigest() == contract["launcher"]["sha256"]
            and _runtime_tree_integrity(node_runtime) == contract["runtime"]["integrity"]
            and _runtime_tree_integrity(python_runtime) == contract["python_runtime"]["integrity"]
            and (embedded.startswith("__AGENT_FLOW_") or embedded == commitment)
            and (
                embedded_python.startswith("__AGENT_FLOW_")
                or embedded_python == contract["python_runtime"]["integrity"]
            )
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _contracted_executable_is_missing(
    contract: dict[str, object],
    path_key: str = "path",
) -> bool:
    try:
        Path(str(contract[path_key])).lstat()
    except (FileNotFoundError, NotADirectoryError):
        return True
    return False


def _python_executable_contract_state(
    contract: dict[str, object],
) -> tuple[bool, bool]:
    configured = Path(str(contract["path"]))
    resolved = Path(str(contract["resolved_path"]))
    if not configured.is_absolute() or not resolved.is_absolute():
        return False, False
    configured_missing = _contracted_executable_is_missing(contract, "path")
    resolved_missing = _contracted_executable_is_missing(contract, "resolved_path")
    if not configured_missing:
        try:
            current_resolved = configured.resolve(strict=True)
        except (FileNotFoundError, NotADirectoryError):
            if resolved_missing:
                return True, True
            return False, False
        if current_resolved != resolved:
            return False, False
    if not resolved_missing and not _verify_executable_contract(
        contract,
        "resolved_path",
    ):
        return False, False
    return True, configured_missing or resolved_missing


def _runtime_requires_portable_repin(leader_root: Path) -> bool:
    if not _runtime_content_matches_contract(leader_root):
        return False
    try:
        kit = json.loads((leader_root / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
        contract = kit["project_runtime_contract"]
        missing = False
        for executable in (contract["node"], contract["git"]):
            if _contracted_executable_is_missing(executable):
                missing = True
                continue
            if not _verify_executable_contract(executable):
                return False
        python_valid, python_missing = _python_executable_contract_state(
            contract["python"]
        )
        return python_valid and (missing or python_missing)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _recovery_arguments_target_leader(
    arguments: list[str],
    cwd: Path,
    leader_root: Path,
) -> bool:
    roots: list[str] = []
    for index, argument in enumerate(arguments):
        option = argument.partition("=")[0]
        if option != "--root" and "--root".startswith(option):
            return False
        if argument == "--root":
            if index + 1 >= len(arguments):
                return False
            roots.append(arguments[index + 1])
        elif argument.startswith("--root="):
            roots.append(argument.removeprefix("--root="))
    try:
        return all(
            (cwd / value).resolve(strict=True) == leader_root.resolve(strict=True)
            if not Path(value).is_absolute()
            else Path(value).resolve(strict=True) == leader_root.resolve(strict=True)
            for value in roots
        )
    except OSError:
        return False


def _is_leader_recovery_launcher(command: str, cwd: Path, leader_root: Path) -> bool:
    if _has_active_shell_substitution(command) or re.search(r"[\r\n;&|]|\d*>>?|&>", command):
        return False
    if cwd.resolve(strict=True) != leader_root.resolve(strict=True):
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
        launcher = Path(words[0])
        if not launcher.is_absolute():
            launcher = cwd / launcher
        expected = leader_root / ".agent-flow" / "bin" / "agent-flow"
        if (
            len(words) < 2
            or launcher.resolve(strict=True) != expected.resolve(strict=True)
            or launcher.is_symlink()
        ):
            return False
    except (IndexError, OSError, ValueError):
        return False
    arguments = words[1:]
    recovery = (
        arguments[0] in {"install", "sync", "status", "continue", "abort"}
        or arguments[:2] == ["run", "install"]
        or arguments[:2] == ["worktree", "repin"]
    )
    return (
        recovery
        and _recovery_arguments_target_leader(arguments, cwd, leader_root)
        and _runtime_requires_portable_repin(leader_root)
    )

def _is_agent_flow_launcher(command: str, cwd: Path, leader_root: Path, pinned_root: Path) -> bool:
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
        "abort",
        "status",
        "continue",
        "run",
        "gate",
        "gates",
        "architecture-lint",
        "install",
        "worktree",
        "export-apk",
        "publish-artifact",
    }
    if arguments[0] not in trusted_subcommands:
        return False
    if arguments[:2] == ["run", "install"]:
        return False
    if arguments[0] == "install":
        try:
            if (
                pinned_root.resolve(strict=True) != leader_root.resolve(strict=True)
                or cwd.resolve(strict=True) != leader_root.resolve(strict=True)
            ):
                return False
        except OSError:
            return False
        if arguments[1:] not in ([], ["--force-managed"]):
            return False
    if arguments[0] == "worktree":
        try:
            if (
                pinned_root.resolve(strict=True) != leader_root.resolve(strict=True)
                or cwd.resolve(strict=True) != leader_root.resolve(strict=True)
            ):
                return False
        except OSError:
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


def _verify_boundary_runtime(leader_root: Path, runtime_root: Path) -> dict[str, Any]:
    kit_path = leader_root / ".agent-flow" / "kit.json"
    try:
        kit = json.loads(kit_path.read_text(encoding="utf-8"))
        contract = kit["project_runtime_contract"]
        embedded = EXPECTED_PROJECT_RUNTIME_CONTRACT_SHA256
        embedded_python = EXPECTED_PYTHON_RUNTIME_INTEGRITY
        embedded_authority = not embedded.startswith("__AGENT_FLOW_")
        if not embedded_authority and "node_runtime" in contract:
            _verify_legacy_boundary_runtime(leader_root, kit, contract)
            return contract
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
        return contract
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
        checkout = _enclosing_checkout(cwd)
        if checkout is None:
            return 0
        pinned_root, leader_root, _worktree_git_dir = checkout
        if not (leader_root / ".git").exists():
            return 0
        host = str(
            os.environ.get("AGENT_FLOW_ACTIVE_HOST")
            or os.environ.get("AGENT_FLOW_HOST")
            or _detected_execution_host(os.environ)
            or payload.get("host")
            or _host_argument()
            or "unknown"
        ).strip().lower()
        tool_input = _tool_input(payload)
        _ensure_cwd_within_pinned(pinned_root, cwd, host)
        paths: list[str] = []
        unresolved_shell_target = False
        if tool_name in SHELL_TOOLS:
            command_value = tool_input.get("command")
            if not isinstance(command_value, str) or not command_value.strip():
                raise ValueError("write boundary rejected: shell tool did not declare a command")
            command = command_value
            if _has_untrusted_shell_startup_environment() or _has_untrusted_shell_startup_assignment(
                command
            ):
                raise ValueError("untrusted shell startup environment")
            if _is_agent_flow_launcher(command, cwd, leader_root, pinned_root):
                if host == "claude":
                    _forward_claude_execution_identity(payload, tool_input, command)
                elif host == "omp":
                    _required_hook_execution_id(payload)
                return 0
            if _requests_agent_flow_launcher(command, cwd):
                if _is_leader_recovery_launcher(command, cwd, leader_root):
                    return 0
                raise ValueError("write boundary rejected: agent-flow launcher is not trusted")
            # The parser is retained only to produce precise denial diagnostics
            # for known dangerous forms. It never authorizes execution: every
            # generic command reaches the mandatory sandboxed-gate denial below.
            _ensure_read_only_interpreter_is_trusted(command, cwd, leader_root)
            mutating, paths, unresolved_shell_target = _shell_mutation_paths(
                command,
                base_dir=cwd,
            )
            if not mutating:
                raise ValueError(
                    "write boundary rejected: run arbitrary commands through the trusted sandboxed agent-flow gate"
                )
        else:
            paths = _requested_paths(tool_input)
            if not paths:
                raise ValueError(
                    "write boundary rejected: write tool did not declare a target path"
                )
        _authenticate_runtime(leader_root)
        if unresolved_shell_target:
            raise ValueError(
                "write boundary rejected: shell command has an unresolved mutation target"
            )
        if not paths:
            raise ValueError(
                "write boundary rejected: shell command has an unresolved mutation target"
            )
        for requested in paths:
            target = _resolve_within_pinned(
                pinned_root,
                requested,
                cwd,
                host=host,
                leader_root=leader_root,
            )
            _ensure_leader_private_run_binding(
                payload,
                pinned_root,
                leader_root,
                target,
                host,
            )
            _verify_managed_marker_integrity(
                tool_name,
                tool_input,
                requested,
                target,
                cwd,
            )
        if tool_name in SHELL_TOOLS:
            raise ValueError(
                "write boundary rejected: run arbitrary commands through the trusted sandboxed agent-flow gate"
            )
        return 0
    except Exception as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
