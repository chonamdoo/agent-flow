"""관리 hook 등록 무결성 — 런 시작 시 shared dispatcher 등록과 runtime을 대조한다.

tripwire(`assert_leader_unchanged`)는 런 도중의 변경만 본다. 런 시작 전에 global
dispatcher, immutable runtime bundle, host 등록을 검증해야 오염된 상태를 기준선으로
굳히지 않는다. 프로젝트에는 실행 가능한 hook 코드가 없어야 하며, JSON host는
정확히 하나의 shared dispatcher command만 각 지원 이벤트에 등록한다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from agent_flow.core.worktree_isolation import leader_root_for

# JSON host가 shared dispatcher에 위임하는 canonical event와 matcher.
MANAGED_JSON_EVENT_PLACEMENT = {
    "PreToolUse": (
        "PreToolUse",
        "^(Bash|bash|shell|run_terminal_cmd|execute_command|local_shell|terminal|"
        "apply_patch|Write|Edit|MultiEdit|write|edit|multi_edit|multiedit|"
        "write_file|edit_file)$",
    ),
    "PostToolUse": (
        "PostToolUse",
        "^(Bash|bash|shell|run_terminal_cmd|execute_command|local_shell|terminal|"
        "apply_patch|Write|Edit|MultiEdit|write|edit|multi_edit|multiedit|"
        "write_file|edit_file|"
        "Read|read|read_file|view|cat|Skill|skill)$",
    ),
    "Stop": ("Stop", ""),
}
MANAGED_HOOK_EVENTS = tuple(f"@{event}" for event in MANAGED_JSON_EVENT_PLACEMENT)

HOOK_DIR_RELATIVE = Path(".agent-flow") / "scripts" / "hooks"
LEGACY_PROJECT_HOOK_FILES = (
    *(
        HOOK_DIR_RELATIVE / name
        for name in (
            "bind-host-worktree.py",
            "guard-protected-branch.sh",
            "guard-host-worktree.sh",
            "show-phase-status.sh",
            "comment-checker.py",
            "record-skill-read.py",
            "record-command-run.py",
            "worktree-tripwire.py",
            "guard-worktree.sh",
            "guard-worktree-write.py",
            "prepare-spec-user-prompt.py",
            "confirm-spec-user-prompt.py",
            "guard-spec-approval.sh",
        )
    ),
    Path(".agent-flow") / "scripts" / "hook-runtime" / "agent-flow-hook.py",
)
KIT_JSON_RELATIVE = Path(".agent-flow") / "kit.json"
SHARED_RUNTIME_CLI_ENTRYPOINT = "agent-flow-cli.py"

JSON_REGISTRATION_FILES = (
    Path(".claude") / "settings.json",
    Path(".Codex") / "hooks.json",
    Path(".codex") / "hooks.json",
)
_SAFE_HOOK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _global_omp_registration_file() -> Path:
    configured = os.environ.get("AGENT_FLOW_OMP_EXTENSION")
    if configured:
        return Path(configured).expanduser().resolve()
    return (
        Path.home() / ".omp" / "agent" / "extensions" / "agent-flow-hooks.ts"
    ).resolve()


def _shared_hook_home() -> Path:
    configured = os.environ.get("AGENT_FLOW_HOME") or os.environ.get(
        "AGENT_FLOW_SHARED_HOME"
    )
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".agent-flow").resolve()


class HookIntegrityError(RuntimeError):
    """관리 hook 등록이 `kit.json` 기록과 어긋난다."""


@dataclass(frozen=True)
class HookIntegrityReport:
    root: Path
    recorded: bool  # kit.json에 `hooks` 기록이 있는가
    expected_enabled: bool
    violations: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.violations


@dataclass(frozen=True)
class _Surface:
    name: str
    present: bool
    readable: bool
    entries: tuple[tuple[str, str, str], ...]  # (event, matcher, managed script name)
    # 관리 디렉터리를 가리키지만 **정확한 호출이 아닌** 명령. `... || true`처럼
    # 후행 셸 구문이 붙으면 hook의 실패를 셸이 덮어 차단이 사라진다.
    malformed: tuple[str, ...] = ()

    def scripts(self) -> set[str]:
        return {script for _, _, script in self.entries}

    def placements(self, script: str) -> set[tuple[str, str]]:
        return {
            (event, matcher) for event, matcher, name in self.entries if name == script
        }


def find_install_root(start) -> Path | None:
    """이 checkout이 딛고 있는 설치본. leader를 먼저 묻고, 그다음 조상 탐색이다.

    순서가 중요하다. 조상 탐색을 먼저 하면 (1) `$HOME/.agent-flow/kit.json`이 있는
    사용자에게는 홈이 모든 저장소의 설치본을 가려 버리고, (2) 워커가 자기 checkout에
    `.agent-flow/kit.json`을 쓰면 그 파일이 자기 무결성 기준선이 된다. leader를 먼저
    확정하면 두 경로 모두 닫힌다 — leader의 git dir은 워커가 쓸 수 없다.
    """
    if start is None:
        return None
    current = Path(start)
    leader = leader_root_for(current)
    if leader is not None and _is_file(leader / KIT_JSON_RELATIVE):
        return leader
    for candidate in [current, *current.parents]:
        if _is_file(candidate / KIT_JSON_RELATIVE):
            return candidate
    return None


def managed_hook_runtime_digest(root: Path) -> str:
    install_root = find_install_root(root)
    if install_root is None:
        raise HookIntegrityError("managed hook installation could not be found")
    runtime = _read_kit_json(install_root).get("hook_runtime")
    digest = runtime.get("digest") if isinstance(runtime, dict) else None
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise HookIntegrityError("kit.json has no valid shared hook runtime digest")
    return digest


def assert_managed_hooks_registered(*roots) -> tuple[HookIntegrityReport, ...]:
    """런 시작 게이트. 강제 hook을 증명하지 못하면 시작 전에 멈춘다."""
    reports = verify_managed_hooks(*roots)
    if not reports:
        raise HookIntegrityError(
            "managed hook installation could not be found. Nothing was changed. "
            "Run `node bin/agent-flow-kit.mjs install --hooks` from the leader "
            "checkout before starting a run."
        )
    failed = [
        report
        for report in reports
        if not report.recorded or not report.expected_enabled or not report.ok
    ]
    if not failed:
        return reports
    detail = "; ".join(
        f"{report.root}: "
        + (
            ", ".join(report.violations)
            if report.violations
            else "managed enforcement hooks are not enabled"
        )
        for report in failed
    )
    raise HookIntegrityError(
        "managed hook registration does not match the enabled enforcement "
        "contract in .agent-flow/kit.json: "
        + detail
        + ". Nothing was changed. The enforcement hooks this run depends on "
        "cannot be proven active, so the run refuses to start. Re-run "
        "`node bin/agent-flow-kit.mjs install --hooks` from the leader checkout "
        "only after confirming the registration was not tampered with."
    )


def verify_managed_hooks(*roots) -> tuple[HookIntegrityReport, ...]:
    """중복 없는 설치 루트마다 리포트를 만든다. 설치본이 없으면 빈 튜플이다."""
    seen: list[Path] = []
    for candidate in roots:
        root = find_install_root(candidate)
        if root is not None and root not in seen:
            seen.append(root)
    return tuple(_verify_root(root) for root in seen)


def _verify_root(root: Path) -> HookIntegrityReport:
    kit = _read_kit_json(root)
    if not isinstance(kit.get("hooks"), bool):
        return HookIntegrityReport(
            root, recorded=False, expected_enabled=False, violations=()
        )
    expected_enabled = bool(kit["hooks"])
    surfaces = tuple(_read_surfaces(root))
    violations: list[str] = []
    violations.extend(_legacy_project_hook_violations(root))
    violations.extend(
        f"cannot read the hook registration file: {surface.name}"
        for surface in surfaces
        if surface.present and not surface.readable
    )
    if expected_enabled:
        violations.extend(_missing_registrations(surfaces))
        violations.extend(_shared_hook_runtime_violations(root, kit))
        violations.extend(_misplaced_managed_hooks(surfaces))
        violations.extend(_unapproved_registrations(surfaces))
        violations.extend(_malformed_registrations(surfaces))
    return HookIntegrityReport(
        root,
        recorded=True,
        expected_enabled=expected_enabled,
        violations=tuple(violations),
    )


def _legacy_project_hook_violations(root: Path) -> Iterator[str]:
    for relative in LEGACY_PROJECT_HOOK_FILES:
        if _is_file(root / relative):
            yield f"project-local managed hook code must be removed: {relative}"


def _missing_registrations(surfaces: tuple[_Surface, ...]) -> Iterator[str]:
    for surface in surfaces:
        if not surface.present:
            yield f"{surface.name} is missing, so no shared dispatcher is registered there"
            continue
        if not surface.readable:
            continue
        registered = surface.scripts()
        for event in MANAGED_HOOK_EVENTS:
            if event not in registered:
                yield f"{surface.name} does not delegate {event[1:]} to the shared dispatcher"


def _trusted_stable_python_violations(
    interpreter: Path, *, label: str
) -> Iterator[str]:
    if not interpreter.is_absolute():
        yield f"{label} is not an absolute path: {interpreter}"
        return
    try:
        resolved = interpreter.resolve(strict=True)
        identity = resolved.stat()
    except OSError:
        yield f"{label} is missing: {interpreter}"
        return
    if resolved != interpreter:
        yield f"{label} is not a stable realpath: {interpreter}"
        return
    trusted_uids = {0, os.getuid()}
    if not stat.S_ISREG(identity.st_mode):
        yield f"{label} is not a regular file: {interpreter}"
    if identity.st_uid not in trusted_uids:
        yield f"{label} has a foreign owner: {interpreter}"
    if identity.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        yield f"{label} is group or world writable: {interpreter}"
    if not os.access(interpreter, os.X_OK):
        yield f"{label} is not executable: {interpreter}"
    for directory in interpreter.parents:
        try:
            directory_identity = directory.stat()
        except OSError:
            yield f"{label} ancestor is missing: {directory}"
            return
        if (
            not stat.S_ISDIR(directory_identity.st_mode)
            or directory_identity.st_uid not in trusted_uids
            or directory_identity.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            yield f"{label} ancestor is unsafe: {directory}"
            return


def _legacy_trusted_system_python() -> str | None:
    for candidate in (Path("/usr/bin/python3"), Path("/usr/local/bin/python3")):
        try:
            resolved = candidate.resolve(strict=True)
            if resolved.stat().st_uid != 0:
                continue
        except OSError:
            continue
        if not tuple(
            _trusted_stable_python_violations(
                resolved, label="legacy shared hook Python interpreter"
            )
        ):
            return str(resolved)
    return None


def _shared_hook_runtime_violations(root: Path, kit: dict) -> Iterator[str]:
    record = kit.get("hook_runtime")
    if not isinstance(record, dict):
        yield (
            "this installation does not select a shared hook runtime; re-run "
            "`node bin/agent-flow-kit.mjs install` from the leader checkout"
        )
        return
    if record.get("protocol_version") != 1:
        yield "kit.json selects an unsupported shared hook runtime protocol"
        return
    digest = record.get("digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        yield "kit.json has no valid shared hook runtime digest"
        return

    recorded_launcher = record.get("launcher_path")
    if not isinstance(recorded_launcher, str) or not os.path.isabs(recorded_launcher):
        yield "kit.json records an invalid shared hook launcher path"
        return
    launcher = Path(recorded_launcher).resolve(strict=False)
    if launcher.name != "agent-flow-hook" or launcher.parent.name != "bin":
        yield "kit.json records the shared hook launcher outside the global store"
        return
    home = launcher.parent.parent
    if home != _shared_hook_home():
        yield "kit.json records the shared hook launcher outside the configured global store"
        return
    runtime = home / "runtimes" / digest / "agent-flow-hook.py"
    recorded_runtime = record.get("path")
    if (
        not isinstance(recorded_runtime, str)
        or not os.path.isabs(recorded_runtime)
        or Path(recorded_runtime).resolve(strict=False) != runtime
    ):
        yield "kit.json selects a shared hook runtime outside the digest store"
    yield from _runtime_bundle_violations(home, digest)
    recorded_python = record.get("python")

    state_path = home / "hook-runtime.json"
    try:
        state = _read_owned_json(state_path)
    except (OSError, ValueError):
        yield f"shared hook runtime state is missing, unsafe, or unreadable: {state_path}"
        return
    launcher_digest = state.get("launcher_digest") if isinstance(state, dict) else None
    accepted_launcher_digests = (
        state.get("launcher_digests", [launcher_digest])
        if isinstance(state, dict)
        else None
    )
    state_python = (
        state.get("python")
        if isinstance(state, dict) and "python" in state
        else _legacy_trusted_system_python()
    )
    if (
        not isinstance(state, dict)
        or state.get("protocol_version") != 1
        or not isinstance(launcher_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", launcher_digest)
        or not isinstance(accepted_launcher_digests, list)
        or not accepted_launcher_digests
        or any(
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
            for value in accepted_launcher_digests
        )
        or not isinstance(state_python, str)
        or not os.path.isabs(state_python)
    ):
        yield f"shared hook runtime state is invalid: {state_path}"
        return
    yield from _trusted_stable_python_violations(
        Path(state_python), label="owned shared hook state Python interpreter"
    )
    if not isinstance(recorded_python, str):
        yield "kit.json records an invalid historical shared hook Python interpreter"
    else:
        yield from _trusted_stable_python_violations(
            Path(recorded_python), label="historical shared hook Python interpreter"
        )
    yield from _shared_executable_violations(
        launcher,
        expected_digest=tuple(accepted_launcher_digests),
        label="shared hook launcher",
    )
    yield from _managed_project_registry_violations(root, home)
    yield from _global_omp_adapter_violations(state)


def _runtime_bundle_entries(runtime_dir: Path) -> tuple[list[str], str | None]:
    entries: list[str] = []

    def visit(current: Path, relative: Path) -> str | None:
        try:
            children = list(current.iterdir())
        except OSError as exc:
            return f"runtime bundle directory is unreadable: {exc}"
        for child in children:
            child_relative = relative / child.name
            try:
                identity = child.lstat()
            except OSError as exc:
                return f"runtime bundle entry is unreadable: {exc}"
            if stat.S_ISLNK(identity.st_mode):
                return f"runtime bundle contains a symlink: {child_relative}"
            if stat.S_ISDIR(identity.st_mode):
                if (
                    identity.st_uid != os.getuid()
                    or stat.S_IMODE(identity.st_mode) != 0o555
                ):
                    return f"runtime bundle directory is unsafe: {child_relative}"
                failure = visit(child, child_relative)
                if failure:
                    return failure
            elif stat.S_ISREG(identity.st_mode):
                entries.append(child_relative.as_posix())
            else:
                return f"runtime bundle contains an unsupported entry: {child_relative}"
        return None

    failure = visit(runtime_dir, Path())
    return sorted(entries), failure


def _runtime_bundle_violations(home: Path, digest: str) -> Iterator[str]:
    runtime_dir = home / "runtimes" / digest
    try:
        identity = runtime_dir.lstat()
    except OSError:
        yield f"selected shared hook runtime directory is missing: {runtime_dir}"
        return
    if (
        not stat.S_ISDIR(identity.st_mode)
        or stat.S_ISLNK(identity.st_mode)
        or identity.st_uid != os.getuid()
        or stat.S_IMODE(identity.st_mode) != 0o555
    ):
        yield f"selected shared hook runtime directory is unsafe: {runtime_dir}"
        return
    manifest_path = runtime_dir / "runtime-manifest.json"
    try:
        manifest = _read_owned_json(manifest_path)
        if stat.S_IMODE(manifest_path.stat().st_mode) != 0o444:
            raise ValueError("runtime manifest mode is unsafe")
    except (OSError, ValueError):
        yield f"selected shared hook runtime manifest is unsafe or unreadable: {manifest_path}"
        return
    files = manifest.get("files")
    policy = manifest.get("policy_sequence")
    cli_entrypoint = manifest.get("cli_entrypoint")
    if (
        manifest.get("protocol_version") != 1
        or manifest.get("runtime_digest") != digest
        or manifest.get("entrypoint") != "agent-flow-hook.py"
        or cli_entrypoint != SHARED_RUNTIME_CLI_ENTRYPOINT
        or not isinstance(files, list)
        or not isinstance(policy, dict)
    ):
        yield f"selected shared hook runtime manifest is invalid: {manifest_path}"
        return
    canonical = {
        "protocol_version": manifest["protocol_version"],
        "entrypoint": manifest["entrypoint"],
        "cli_entrypoint": cli_entrypoint,
        "policy_sequence": policy,
        "files": files,
    }
    encoded = json.dumps(canonical, separators=(",", ":"), ensure_ascii=False).encode()
    if hashlib.sha256(encoded).hexdigest() != digest:
        yield f"selected shared hook runtime manifest digest does not match: {manifest_path}"
        return
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            yield f"selected shared hook runtime file record is invalid: {manifest_path}"
            continue
        relative = item.get("path")
        expected = item.get("sha256")
        mode = item.get("mode")
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or relative in seen
            or not isinstance(expected, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected) is None
            or mode not in (0o444, 0o555)
        ):
            yield f"selected shared hook runtime file record is invalid: {manifest_path}"
            continue
        seen.add(relative)
        file_path = runtime_dir / relative
        try:
            content = _read_owned_file(file_path, mode)
        except (OSError, ValueError):
            yield f"selected shared hook runtime file is unsafe or unreadable: {file_path}"
            continue
        if hashlib.sha256(content).hexdigest() != expected:
            yield f"selected shared hook runtime file digest does not match: {file_path}"
    if manifest["entrypoint"] not in seen or cli_entrypoint not in seen:
        yield f"selected shared hook runtime omits a required entrypoint: {manifest_path}"
    entries, entry_failure = _runtime_bundle_entries(runtime_dir)
    if entry_failure:
        yield f"selected shared hook runtime is unsafe: {entry_failure}"
    elif entries != sorted(["runtime-manifest.json", *seen]):
        yield f"selected shared hook runtime contains unrecorded files: {runtime_dir}"


def _managed_project_registry_violations(root: Path, home: Path) -> Iterator[str]:
    registry_path = home / "managed-projects.json"
    try:
        registry = _read_owned_json(registry_path)
        if stat.S_IMODE(registry_path.stat().st_mode) != 0o600:
            raise ValueError("managed project registry mode is unsafe")
    except (OSError, ValueError):
        yield f"managed project registry is missing, unsafe, or unreadable: {registry_path}"
        return
    canonical_root = str(root.resolve())
    projects = (
        registry.get("projects") if registry.get("protocol_version") == 1 else None
    )
    record = projects.get(canonical_root) if isinstance(projects, dict) else None
    accepted = (
        record.get("accepted_kit_digests", []) if isinstance(record, dict) else []
    )
    if (
        not isinstance(record, dict)
        or record.get("root") != canonical_root
        or not isinstance(record.get("kit_digest"), str)
        or re.fullmatch(r"[0-9a-f]{64}", record["kit_digest"]) is None
        or not isinstance(accepted, list)
        or any(
            not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in accepted
        )
    ):
        yield f"managed project registry does not contain a valid record for: {canonical_root}"
        return
    try:
        content = _read_owned_file(root / KIT_JSON_RELATIVE, 0o644)
    except (OSError, ValueError):
        yield f"registered project manifest is unsafe or unreadable: {root / KIT_JSON_RELATIVE}"
        return
    if hashlib.sha256(content).hexdigest() not in {record["kit_digest"], *accepted}:
        yield f"project manifest digest does not match the private registry: {canonical_root}"


def _global_omp_adapter_violations(state: dict) -> Iterator[str]:
    record = state.get("omp_adapter")
    if not isinstance(record, dict):
        yield "shared hook runtime state does not record the global OMP adapter"
        return
    expected_path = _global_omp_registration_file()
    digest = record.get("digest")
    accepted = record.get("accepted_digests", [digest])
    if record.get("path") != str(expected_path):
        yield "shared hook runtime state records the wrong global OMP adapter path"
    if (
        not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or not isinstance(accepted, list)
        or not accepted
        or any(
            not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in accepted
        )
    ):
        yield "shared hook runtime state has no valid global OMP adapter digest"
        return
    try:
        content = _read_owned_file(expected_path, 0o644)
    except (OSError, ValueError):
        yield f"global OMP adapter is missing, unsafe, or unreadable: {expected_path}"
        return
    actual = hashlib.sha256(content).hexdigest()
    if actual not in accepted:
        yield (
            "global OMP adapter content digest does not match its runtime record: "
            f"{expected_path}"
        )


def _read_owned_file(path: Path, mode: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        identity = os.fstat(descriptor)
        if (
            not stat.S_ISREG(identity.st_mode)
            or identity.st_uid != os.getuid()
            or identity.st_nlink != 1
            or stat.S_IMODE(identity.st_mode) != mode
        ):
            raise ValueError("unsafe owned file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def _read_owned_json(path: Path) -> dict:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        identity = os.fstat(descriptor)
        if (
            not stat.S_ISREG(identity.st_mode)
            or identity.st_uid != os.getuid()
            or identity.st_nlink != 1
            or identity.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise ValueError("unsafe owned JSON file")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as stream:
            payload = json.load(stream)
    finally:
        os.close(descriptor)
    if not isinstance(payload, dict):
        raise ValueError("owned JSON file is not an object")
    return payload


def _shared_executable_violations(
    path: Path,
    *,
    label: str,
    expected_digest: str | tuple[str, ...],
) -> Iterator[str]:
    try:
        identity = path.lstat()
    except OSError:
        yield f"{label} is missing: {path}"
        return
    if not stat.S_ISREG(identity.st_mode) or stat.S_ISLNK(identity.st_mode):
        yield f"{label} is not a regular owned file: {path}"
        return
    if identity.st_nlink != 1:
        yield f"{label} has unsafe link count: {path}"
    if identity.st_uid != os.getuid():
        yield f"{label} is not owned by the current user: {path}"
    if identity.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        yield f"{label} is group or world writable: {path}"
    if not os.access(path, os.X_OK):
        yield f"{label} is not executable: {path}"
    try:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        yield f"{label} cannot be read for digest verification: {path}"
        return
    accepted = (
        {expected_digest} if isinstance(expected_digest, str) else set(expected_digest)
    )
    if actual not in accepted:
        yield f"{label} content digest does not match its runtime record: {path}"




def _unapproved_registrations(surfaces: tuple[_Surface, ...]) -> Iterator[str]:
    for surface in surfaces:
        for event in sorted(surface.scripts() - set(MANAGED_HOOK_EVENTS)):
            yield f"{surface.name} delegates an unapproved dispatcher event: {event[1:]}"


def _misplaced_managed_hooks(surfaces: tuple[_Surface, ...]) -> Iterator[str]:
    for surface in surfaces:
        if not surface.present or not surface.readable:
            continue
        for event_name, (expected_event, expected_matcher) in sorted(
            MANAGED_JSON_EVENT_PLACEMENT.items()
        ):
            marker = f"@{event_name}"
            found = [
                (event, matcher)
                for event, matcher, name in surface.entries
                if name == marker
            ]
            if len(found) > 1:
                yield (
                    f"{surface.name} registers {event_name} dispatcher {len(found)} times; "
                    "exactly one registration is required"
                )
                continue
            if not found or found[0] == (expected_event, expected_matcher):
                continue
            actual_event, actual_matcher = found[0]
            yield (
                f"{surface.name} registers {event_name} dispatcher at "
                f"{actual_event or '(no event)'}/{actual_matcher or '(no matcher)'}, expected "
                f"{expected_event}/{expected_matcher or '(no matcher)'}"
            )


def _malformed_registrations(surfaces: tuple[_Surface, ...]) -> Iterator[str]:
    for surface in surfaces:
        for command in sorted(set(surface.malformed)):
            yield (
                f"{surface.name} registers a shared dispatcher path with extra shell syntax: "
                f"{command!r}; the shell would swallow the dispatcher's exit code"
            )


def _read_surfaces(root: Path) -> Iterator[_Surface]:
    """Global JSON host 등록 파일마다 shared dispatcher event 목록을 만든다."""
    home = Path.home()
    for relative in JSON_REGISTRATION_FILES:
        path = home / relative
        if not _is_file(path):
            yield _Surface(str(path), present=False, readable=False, entries=())
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            yield _Surface(str(path), present=True, readable=False, entries=())
            continue
        entries, malformed = _json_registrations(root, payload)
        yield _Surface(
            str(path),
            present=True,
            readable=True,
            entries=entries,
            malformed=malformed,
        )


def _json_registrations(
    root: Path, payload: object
) -> tuple[tuple[tuple[str, str, str], ...], tuple[str, ...]]:
    hooks = payload.get("hooks") if isinstance(payload, dict) else None
    if not isinstance(hooks, dict):
        return (), ()
    entries: list[tuple[str, str, str]] = []
    malformed: list[str] = []
    for event, blocks in hooks.items():
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            matcher = block.get("matcher", "") if isinstance(block, dict) else ""
            for command in _entry_commands(block):
                event_name = managed_path_hook_name(root, command)
                if event_name is not None:
                    entries.append((str(event), str(matcher or ""), event_name))
                elif mentions_managed_hook_dir(root, command):
                    malformed.append(command)
    return tuple(entries), tuple(malformed)


def _entry_commands(entry: object) -> Iterator[str]:
    if not isinstance(entry, dict):
        return
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return
    for hook in hooks:
        if isinstance(hook, dict) and isinstance(hook.get("command"), str):
            yield hook["command"]


def managed_path_hook_name(root: Path, command: str) -> str | None:
    """현재 dispatcher와 이전 project-local command의 literal argv를 판별한다."""
    runtime_record = _read_kit_json(root).get("hook_runtime")
    recorded_python = (
        runtime_record.get("python") if isinstance(runtime_record, dict) else None
    )
    recorded_launcher = (
        runtime_record.get("launcher_path")
        if isinstance(runtime_record, dict)
        else None
    )
    recorded_bootstrap_digest = (
        runtime_record.get("bootstrap_digest")
        if isinstance(runtime_record, dict)
        else None
    )
    try:
        state = _read_owned_json(_shared_hook_home() / "hook-runtime.json")
    except (OSError, ValueError):
        return None
    current_python = (
        state.get("python")
        if isinstance(state, dict) and "python" in state
        else _legacy_trusted_system_python()
    )
    if (
        not isinstance(state, dict)
        or state.get("protocol_version") != 1
        or not isinstance(current_python, str)
        or not os.path.isabs(current_python)
        or tuple(
            _trusted_stable_python_violations(
                Path(current_python),
                label="owned shared hook state Python interpreter",
            )
        )
        or not isinstance(recorded_launcher, str)
        or not os.path.isabs(recorded_launcher)
    ):
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if (
        len(tokens) == 6
        and tokens[:3] == [current_python, "-I", "-c"]
        and isinstance(recorded_bootstrap_digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", recorded_bootstrap_digest) is not None
        and hashlib.sha256(tokens[3].encode()).hexdigest()
        == recorded_bootstrap_digest
        and tokens[4] == "--event"
        and _SAFE_HOOK_NAME.fullmatch(tokens[5]) is not None
    ):
        expected = (
            f"{_quote_shell_word(current_python)} -I -c "
            f"{_quote_shell_word(tokens[3])} --event "
            f"{_quote_shell_word(tokens[5])}"
        )
        return f"@{tokens[5]}" if command == expected else None
    if (
        len(tokens) == 5
        and tokens[:4] == [current_python, "-I", recorded_launcher, "--event"]
        and _SAFE_HOOK_NAME.fullmatch(tokens[4]) is not None
    ):
        expected = (
            f"{_quote_shell_word(current_python)} -I "
            f"{_quote_shell_word(recorded_launcher)} --event "
            f"{_quote_shell_word(tokens[4])}"
        )
        return f"@{tokens[4]}" if command == expected else None
    resolved_root = str(root.resolve())
    if (
        len(tokens) == 7
        and tokens[:4] == [recorded_python, "-I", recorded_launcher, "--root"]
        and tokens[4:6] == [resolved_root, "--hook"]
        and tokens[6] in {path.name for path in LEGACY_PROJECT_HOOK_FILES}
    ):
        expected = (
            f"{_quote_shell_word(recorded_python)} -I "
            f"{_quote_shell_word(recorded_launcher)} --root "
            f"{_quote_shell_word(resolved_root)} --hook "
            f"{_quote_shell_word(tokens[6])}"
        )
        return tokens[6] if command == expected else None
    candidate: str | None = None
    if len(tokens) == 1:
        candidate = tokens[0]
    elif len(tokens) == 2 and tokens[0] == "/bin/bash" and tokens[1].endswith(".sh"):
        candidate = tokens[1]
    elif (
        len(tokens) == 3
        and tokens[:2] == [recorded_python, "-I"]
        and tokens[2].endswith(".py")
    ):
        candidate = tokens[2]
    if candidate is not None:
        normalized = candidate.replace("\\", "/")
        normalized_root = str(root.resolve()).replace("\\", "/")
        for relative in LEGACY_PROJECT_HOOK_FILES:
            script_name = relative.name
            if normalized in {
                f".agent-flow/scripts/hooks/{script_name}",
                f"scripts/hooks/{script_name}",
                f"{normalized_root}/.agent-flow/scripts/hooks/{script_name}",
                f"{normalized_root}/scripts/hooks/{script_name}",
            }:
                return script_name
    for relative in LEGACY_PROJECT_HOOK_FILES:
        script_name = relative.name
        expected = (
            f"cd {_quote_shell_word(resolved_root)} && "
            f"{_quote_shell_word(str(root.resolve() / relative))}"
        )
        if command == expected:
            return script_name
    return None


def _quote_shell_word(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def mentions_managed_hook_dir(root: Path, command: str) -> bool:
    """형식이 잘못된 command가 global shared launcher를 가리키는지 본다."""
    normalized = _unquote(command).replace("\\", "/")
    runtime_record = _read_kit_json(root).get("hook_runtime")
    launcher = (
        runtime_record.get("launcher_path")
        if isinstance(runtime_record, dict)
        else None
    )
    return isinstance(launcher, str) and launcher.replace("\\", "/") in normalized


def _unquote(value: str) -> str:
    text = value.strip()
    for quote in ("'", '"'):
        if len(text) >= 2 and text.startswith(quote) and text.endswith(quote):
            return text[1:-1]
    return text


def _read_kit_json(root: Path) -> dict:
    try:
        payload = json.loads((root / KIT_JSON_RELATIVE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _is_file(path: Path) -> bool:
    """`Path.is_file`은 EACCES를 삼키지 않고 다시 던진다. 여기서는 없는 것과 같다."""
    try:
        return path.is_file()
    except OSError:
        return False
