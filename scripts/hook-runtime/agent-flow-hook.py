#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn

PROTOCOL_VERSION = 1
SAFE_DIGEST = re.compile(r"[0-9a-f]{64}")
RUNTIME_MANIFEST = "runtime-manifest.json"
ENTRYPOINT = "agent-flow-hook.py"
CLI_ENTRYPOINT = "agent-flow-cli.py"
COMMAND_TOOLS = frozenset(
    {
        "bash",
        "shell",
        "run_terminal_cmd",
        "execute_command",
        "local_shell",
        "terminal",
    }
)
WRITE_TOOLS = frozenset(
    {
        "apply_patch",
        "write",
        "edit",
        "multiedit",
        "multi_edit",
        "write_file",
        "edit_file",
    }
)
READ_TOOLS = frozenset({"read", "read_file", "view", "cat", "skill"})
EVENTS = frozenset({"PreToolUse", "PostToolUse", "Stop"})
VERIFIED_IMPORT_BOOTSTRAP = r"""
import importlib.abc
import importlib.util
import json
import os
import sys

class _AgentFlowVerifiedLoader(importlib.abc.Loader):
    def __init__(self, record):
        self.record = record

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        descriptor = int(self.record["fd"])
        os.lseek(descriptor, 0, os.SEEK_SET)
        source = os.fdopen(os.dup(descriptor), "rb").read()
        module.__file__ = self.record["path"]
        exec(compile(source, self.record["path"], "exec"), module.__dict__)

class _AgentFlowVerifiedFinder(importlib.abc.MetaPathFinder):
    def __init__(self, records):
        self.records = records

    def find_spec(self, fullname, path=None, target=None):
        record = self.records.get(fullname)
        if record is None:
            return None
        loader = _AgentFlowVerifiedLoader(record)
        return importlib.util.spec_from_loader(
            fullname,
            loader,
            origin=record["path"],
            is_package=record["package"],
        )

sys.meta_path.insert(
    0,
    _AgentFlowVerifiedFinder(json.loads(os.environ["AGENT_FLOW_VERIFIED_MODULES"])),
)
"""


def _fail(message: str) -> NoReturn:
    print(f"agent-flow hook runtime error: {message}", file=sys.stderr)
    raise SystemExit(70)


def _read_fd(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _open_owned(path: Path, *, mode: int, label: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        _fail(f"{label} is unavailable: {exc}")
    identity = os.fstat(fd)
    if not stat.S_ISREG(identity.st_mode):
        os.close(fd)
        _fail(f"{label} is not a regular file")
    if identity.st_nlink != 1:
        os.close(fd)
        _fail(f"{label} has unsafe link count")
    if hasattr(os, "getuid") and identity.st_uid != os.getuid():
        os.close(fd)
        _fail(f"{label} is not owned by the current user")
    if stat.S_IMODE(identity.st_mode) != mode:
        os.close(fd)
        _fail(f"{label} has unsafe mode")
    return fd


def _read_json(path: Path, *, mode: int, label: str) -> dict[str, Any]:
    fd = _open_owned(path, mode=mode, label=label)
    try:
        value = json.loads(_read_fd(fd))
    except (OSError, ValueError, TypeError) as exc:
        os.close(fd)
        _fail(f"{label} is invalid: {exc}")
    os.close(fd)
    if not isinstance(value, dict):
        _fail(f"{label} is invalid")
    return value


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and SAFE_DIGEST.fullmatch(value) is not None



def _runtime_bundle_entries(runtime_dir: Path) -> list[str]:
    entries: list[str] = []

    def visit(current: Path, relative: Path) -> None:
        try:
            children = list(current.iterdir())
        except OSError as exc:
            _fail(f"runtime bundle directory is unreadable: {exc}")
        for child in children:
            child_relative = relative / child.name
            try:
                identity = child.lstat()
            except OSError as exc:
                _fail(f"runtime bundle entry is unreadable: {exc}")
            if stat.S_ISLNK(identity.st_mode):
                _fail(f"runtime bundle contains a symlink: {child_relative}")
            if stat.S_ISDIR(identity.st_mode):
                if hasattr(os, "getuid") and identity.st_uid != os.getuid():
                    _fail(
                        f"runtime bundle directory has another owner: {child_relative}"
                    )
                if stat.S_IMODE(identity.st_mode) != 0o555:
                    _fail(f"runtime bundle directory has unsafe mode: {child_relative}")
                visit(child, child_relative)
            elif stat.S_ISREG(identity.st_mode):
                entries.append(child_relative.as_posix())
            else:
                _fail(f"runtime bundle contains an unsupported entry: {child_relative}")

    visit(runtime_dir, Path())
    return sorted(entries)


def _verify_runtime_bundle(
    runtime_dir: Path,
    runtime_digest: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    try:
        directory_identity = runtime_dir.lstat()
    except OSError as exc:
        _fail(f"runtime directory is unavailable: {exc}")
    if not stat.S_ISDIR(directory_identity.st_mode) or stat.S_ISLNK(
        directory_identity.st_mode
    ):
        _fail("runtime directory is unsafe")
    if hasattr(os, "getuid") and directory_identity.st_uid != os.getuid():
        _fail("runtime directory is not owned by the current user")
    if stat.S_IMODE(directory_identity.st_mode) != 0o555:
        _fail("runtime directory has unsafe mode")
    manifest = _read_json(
        runtime_dir / RUNTIME_MANIFEST, mode=0o444, label="runtime manifest"
    )
    if (
        manifest.get("protocol_version") != PROTOCOL_VERSION
        or manifest.get("runtime_digest") != runtime_digest
        or manifest.get("entrypoint") != ENTRYPOINT
        or manifest.get("cli_entrypoint") != CLI_ENTRYPOINT
        or not isinstance(manifest.get("policy_sequence"), dict)
        or not isinstance(manifest.get("files"), list)
    ):
        _fail("runtime manifest identity mismatch")
    identity = {
        "protocol_version": manifest.get("protocol_version"),
        "entrypoint": manifest.get("entrypoint"),
        "cli_entrypoint": manifest.get("cli_entrypoint"),
        "policy_sequence": manifest.get("policy_sequence"),
        "files": manifest.get("files"),
    }
    canonical = json.dumps(identity, separators=(",", ":"), ensure_ascii=False).encode()
    if hashlib.sha256(canonical).hexdigest() != runtime_digest:
        _fail("runtime manifest digest mismatch")
    files: dict[str, dict[str, Any]] = {}
    for item in manifest["files"]:
        if not isinstance(item, dict):
            _fail("runtime manifest file record is invalid")
        relative = item.get("path")
        digest = item.get("sha256")
        mode = item.get("mode")
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or relative in files
            or not _valid_digest(digest)
            or mode not in (0o444, 0o555)
        ):
            _fail("runtime manifest file record is invalid")
        fd = _open_owned(runtime_dir / relative, mode=mode, label="runtime bundle file")
        try:
            actual = hashlib.sha256(_read_fd(fd)).hexdigest()
        finally:
            os.close(fd)
        if actual != digest:
            _fail(f"runtime bundle file digest mismatch: {relative}")
        files[relative] = item
    expected_entries = sorted([RUNTIME_MANIFEST, *files])
    if _runtime_bundle_entries(runtime_dir) != expected_entries:
        _fail("runtime bundle contains unrecorded files")
    if ENTRYPOINT not in files:
        _fail("runtime entrypoint is not recorded")
    policy = manifest["policy_sequence"]
    for event in EVENTS:
        event_policy = policy.get(event)
        if not isinstance(event_policy, dict) or not isinstance(
            event_policy.get("matcher"), str
        ):
            _fail("runtime policy sequence is invalid")
        for tool_class, scripts in event_policy.items():
            if tool_class == "matcher":
                continue
            if not isinstance(scripts, list) or not all(
                isinstance(script, str) and f"hooks/{script}" in files
                for script in scripts
            ):
                _fail("runtime policy sequence is invalid")
    return files, policy


def _tool_name(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("tool_name", "toolName", "tool"):
        value = payload.get(key)
        if isinstance(value, str):
            return value.strip().lower()
        if isinstance(value, dict):
            nested = value.get("name")
            if isinstance(nested, str):
                return nested.strip().lower()
    return ""


def _tool_class(event: str, payload: object) -> str | None:
    if event == "Stop":
        return "stop"
    name = _tool_name(payload)
    if name in COMMAND_TOOLS:
        return "command"
    if name in WRITE_TOOLS:
        return "write"
    if name in READ_TOOLS:
        return "read"
    return None


def _open_verified_modules(
    runtime_dir: Path,
    files: dict[str, dict[str, Any]],
) -> tuple[list[int], dict[str, dict[str, Any]]]:
    descriptors: list[int] = []
    modules: dict[str, dict[str, Any]] = {}
    try:
        for relative, record in sorted(files.items()):
            prefix = "runtime/python/"
            if not relative.startswith(prefix) or not relative.endswith(".py"):
                continue
            module_parts = relative[len(prefix) :].split("/")
            is_package = module_parts[-1] == "__init__.py"
            if is_package:
                module_parts.pop()
            else:
                module_parts[-1] = module_parts[-1][:-3]
            module_name = ".".join(module_parts)
            if not module_name:
                _fail(f"runtime module path is invalid: {relative}")
            module_path = runtime_dir.joinpath(*relative.split("/"))
            descriptor = _open_owned(
                module_path,
                mode=record["mode"],
                label="runtime policy module",
            )
            if hashlib.sha256(_read_fd(descriptor)).hexdigest() != record["sha256"]:
                _fail(f"runtime policy module changed after validation: {relative}")
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.set_inheritable(descriptor, True)
            descriptors.append(descriptor)
            modules[module_name] = {
                "fd": descriptor,
                "package": is_package,
                "path": str(module_path),
            }
        return descriptors, modules
    except BaseException:
        for descriptor in descriptors:
            os.close(descriptor)
        raise


HOOK_TIMEOUT_SECONDS = 15


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ("taskkill", "/PID", str(process.pid), "/T", "/F"),
                check=False,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        if process.poll() is None:
            process.kill()


def _run_managed_process(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    payload: bytes,
    pass_fds: tuple[int, ...],
    timeout: float = HOOK_TIMEOUT_SECONDS,
) -> tuple[subprocess.CompletedProcess[bytes], bool]:
    options: dict[str, Any] = {
        "cwd": cwd,
        "env": env,
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "close_fds": True,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
        options["pass_fds"] = pass_fds
    process = subprocess.Popen(command, **options)
    try:
        stdout, stderr = process.communicate(input=payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process)
        stdout, stderr = process.communicate()
        return (
            subprocess.CompletedProcess(
                command,
                124,
                stdout=stdout or b"",
                stderr=stderr or b"",
            ),
            True,
        )
    return (
        subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout=stdout or b"",
            stderr=stderr or b"",
        ),
        False,
    )


def _run_hook(
    runtime_dir: Path,
    files: dict[str, dict[str, Any]],
    script_name: str,
    payload: bytes,
    project_root: Path,
) -> int:
    relative = f"hooks/{script_name}"
    record = files.get(relative)
    if not isinstance(record, dict):
        _fail(f"managed hook is missing from runtime manifest: {script_name}")
    script_path = runtime_dir / relative
    module_fds: list[int] = []
    fd = _open_owned(script_path, mode=record["mode"], label="managed hook")
    try:
        if hashlib.sha256(_read_fd(fd)).hexdigest() != record["sha256"]:
            _fail(f"managed hook changed after validation: {script_name}")
        os.lseek(fd, 0, os.SEEK_SET)
        os.set_inheritable(fd, True)
        module_fds, modules = _open_verified_modules(runtime_dir, files)
        env = {
            name: os.environ[name]
            for name in (
                "HOME",
                "LANG",
                "LC_ALL",
                "LC_CTYPE",
                "LOGNAME",
                "TMPDIR",
                "USER",
            )
            if name in os.environ
        }
        env["PATH"] = "/usr/bin:/bin"
        env["AGENT_FLOW_MANAGED_HOOK_FD"] = str(fd)
        env["AGENT_FLOW_MANAGED_HOOK_PATH"] = str(script_path)
        env["AGENT_FLOW_VERIFIED_IMPORT_BOOTSTRAP"] = VERIFIED_IMPORT_BOOTSTRAP
        env["AGENT_FLOW_VERIFIED_MODULES"] = json.dumps(
            modules,
            separators=(",", ":"),
            sort_keys=True,
        )
        if script_name.endswith(".py"):
            bootstrap = (
                "import os;"
                "exec(os.environ['AGENT_FLOW_VERIFIED_IMPORT_BOOTSTRAP'],globals());"
                "fd=int(os.environ['AGENT_FLOW_MANAGED_HOOK_FD']);"
                "os.lseek(fd,0,0);"
                "source=os.fdopen(os.dup(fd),'rb').read();"
                "path=os.environ['AGENT_FLOW_MANAGED_HOOK_PATH'];"
                "exec(compile(source,path,'exec'),"
                "{'__name__':'__main__','__file__':path})"
            )
            command = [sys.executable, "-I", "-c", bootstrap]
        else:
            command = [
                "/bin/bash",
                "-c",
                'source "/dev/fd/${AGENT_FLOW_MANAGED_HOOK_FD}"',
            ]
        env["AGENT_FLOW_POLICY_ROOT"] = str(runtime_dir)
        env["AGENT_FLOW_RUNTIME_DIR"] = str(runtime_dir)
        env["AGENT_FLOW_MANAGED_PYTHON"] = sys.executable
        env["AGENT_FLOW_PROJECT_ROOT"] = str(project_root)
        try:
            result, timed_out = _run_managed_process(
                command,
                cwd=runtime_dir,
                env=env,
                payload=payload,
                pass_fds=(fd, *module_fds),
            )
        except OSError as exc:
            _fail(f"managed hook {script_name} failed to run: {exc}")
    finally:
        for module_fd in module_fds:
            os.close(module_fd)
        os.close(fd)
    if result.stdout:
        sys.stdout.buffer.write(result.stdout)
        sys.stdout.buffer.flush()
    if result.stderr:
        sys.stderr.buffer.write(result.stderr)
        sys.stderr.buffer.flush()
    if timed_out:
        _fail(
            f"managed hook {script_name} timed out after "
            f"{HOOK_TIMEOUT_SECONDS} seconds"
        )
    return result.returncode




def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root", required=True)
    parser.add_argument("--event", required=True, choices=sorted(EVENTS))
    args = parser.parse_args()
    root = Path(args.root).resolve()
    payload_bytes = sys.stdin.buffer.read()
    if not payload_bytes.strip():
        _fail("hook payload is empty")
    try:
        payload = json.loads(payload_bytes)
    except (ValueError, TypeError) as exc:
        _fail(f"hook payload is invalid JSON: {exc}")
    if not isinstance(payload, dict):
        _fail("hook payload must be a JSON object")
    runtime_digest = os.environ.get("AGENT_FLOW_RUNTIME_DIGEST", "")
    if not _valid_digest(runtime_digest):
        _fail("runtime digest was not provided by the stable launcher")
    runtime_dir = Path(__file__).resolve().parent
    selected_root = os.environ.get("AGENT_FLOW_PROJECT_ROOT", "")
    selected_runtime_dir = os.environ.get("AGENT_FLOW_RUNTIME_DIR", "")
    selected_entrypoint = os.environ.get("AGENT_FLOW_RUNTIME_ENTRYPOINT", "")
    if not selected_root or Path(selected_root).resolve() != root:
        _fail("selected project root does not match hook arguments")
    if (
        not selected_runtime_dir
        or Path(selected_runtime_dir).resolve() != runtime_dir
        or not selected_entrypoint
        or Path(selected_entrypoint).resolve() != runtime_dir / ENTRYPOINT
    ):
        _fail("selected runtime path does not match executed runtime")
    if runtime_dir.name != runtime_digest:
        _fail("runtime path does not match its digest")
    files, policy = _verify_runtime_bundle(runtime_dir, runtime_digest)
    executed_fd = os.environ.get("AGENT_FLOW_EXECUTED_FD")
    if not isinstance(executed_fd, str) or not executed_fd.isdecimal():
        _fail("runtime was not entered through the verified launcher descriptor")
    expected_entrypoint = files[ENTRYPOINT]["sha256"]
    if hashlib.sha256(_read_fd(int(executed_fd))).hexdigest() != expected_entrypoint:
        _fail("executed runtime descriptor digest mismatch")
    tool_class = _tool_class(args.event, payload)
    if tool_class is None:
        if args.event == "PreToolUse":
            _fail("PreToolUse payload has no supported tool identity")
        return 0
    for script_name in policy[args.event].get(tool_class, []):
        code = _run_hook(runtime_dir, files, script_name, payload_bytes, root)
        if code != 0:
            return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
