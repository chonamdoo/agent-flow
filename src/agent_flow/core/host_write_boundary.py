from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from agent_flow.core.worktree_isolation import (
    LeaderSnapshot,
    WorktreeIsolationError,
    assert_leader_unchanged,
    capture_leader_snapshot,
    list_registered_worktrees,
    managed_worktree_root,
    real_path,
    trusted_checkout_paths,
    verify_linked_worktree,
)

_COMMAND_TOOLS = frozenset(
    {
        "bash",
        "shell",
        "run_terminal_cmd",
        "execute_command",
        "local_shell",
        "terminal",
    }
)
_WRITE_TOOLS = frozenset(
    {
        "apply_patch",
        "write",
        "edit",
        "multiedit",
        "multi_edit",
    }
)
_COMMAND_KEYS = frozenset({"command", "cmd", "script", "shell_command"})
_PATH_KEYS = frozenset(
    {
        "path",
        "file",
        "file_path",
        "filepath",
        "target",
        "destination",
        "dest",
        "output_path",
        "directory",
        "cwd",
        "workdir",
        "working_directory",
    }
)
_CWD_KEYS = frozenset({"cwd", "workdir", "working_directory"})
_PATCH_KEYS = frozenset({"patch", "diff", "input"})
_VIRTUAL_PATH = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_SAFE_VIRTUAL_PATH = re.compile(r"^xd://", re.IGNORECASE)
_SHELL_PUNCTUATION = ";&|<>()`"
_PATCH_PATH = re.compile(
    r"^(?:\[(?P<hashline>.+?)#[0-9A-Fa-f]{4}\]|"
    r"\*\*\* (?:Add|Update|Delete) File: (?P<apply>.+)|"
    r"MV (?P<move>.+))$",
    re.MULTILINE,
)
_STATUS_JSON = re.compile(r"(?:^|\n)status_json:\s*", re.MULTILINE)
_BINDING_VERSION = 2
_BINDING_DIR = "host-sessions"
_LIFECYCLE_COMMANDS = frozenset({"continue", "run", "start", "status"})


class HostWriteBoundaryError(RuntimeError):
    pass


@dataclass(frozen=True)
class ActiveCheckout:
    name: str
    checkout: Path
    branch: str | None
    runtime_root: Path
    run_id: str


@dataclass(frozen=True)
class HostCheckoutBinding:
    session_key: str
    checkout: ActiveCheckout
    leader_snapshot: LeaderSnapshot


@dataclass(frozen=True)
class _ActiveRunClaim:
    run_id: str
    metadata: dict[str, Any]




def assert_adoption_allowed(*, root: Path) -> None:
    """채택은 런이 도는 동안에는 하지 않는다.

    채택은 인가다. 워커가 스스로 인가하면 관리 루트 밖 어디로든 쓰기 경계를 넓힌다.
    tool 경계의 명령 문자열 검사로는 막을 수 없고(`env agent-flow …`, 절대경로 호출,
    `python3 -m agent_flow.cli …`), 호출자의 cwd로도 막을 수 없다 — 그 값은 호출자가
    고른다(`cd /tmp && agent-flow worktree adopt …`). 그래서 호출자가 고를 수 없는
    것으로 판정한다: 이 저장소에 활성 run이 하나라도 있으면 거절한다.

    활성 run을 증명할 수 없으면 예외가 그대로 올라간다. 통과로 접으면 워커가 자기
    자리를 스스로 인가할 수 있다.
    """
    active = _active_checkouts(root)
    if active:
        names = ", ".join(context.name for context in active)
        raise HostWriteBoundaryError(
            f"refusing to adopt while runs are active in {root} ({names}); "
            "adoption is a leader-side action — finish or abort the run first"
        )


def record_host_checkout_binding(payload: object, project_root: Path) -> Path | None:
    root = _validated_project_root(project_root)
    session_id = _first_string(payload, ("session_id", "sessionId"))
    if not session_id:
        return None
    command = _first_string(payload, tuple(_COMMAND_KEYS))
    if not command or not _is_lifecycle_command(command):
        return None
    exit_code = _first_number(payload, ("exit_code", "exitCode", "returncode", "return_code"))
    if exit_code not in (None, 0):
        return None
    status = _last_status_payload(payload)
    if status is None:
        return None
    next_command = status.get("next_command")
    run_value = status.get("run")
    if not isinstance(next_command, str) or not isinstance(run_value, str):
        return None
    selector = _worktree_selector(next_command)
    run_id = run_value.rsplit("/", 1)[-1].strip()
    if not selector or not run_id:
        return None
    context = _active_checkout(root, selector=selector, run_id=run_id)
    if context is None:
        raise HostWriteBoundaryError(
            "status output did not identify one verified active managed worktree"
        )
    active = _active_checkouts(root)
    existing = _load_binding(root, session_id, active)
    if existing is not None:
        if (
            existing.checkout.checkout != context.checkout
            or existing.checkout.run_id != context.run_id
        ):
            raise HostWriteBoundaryError(
                f"host session is already bound to {existing.checkout.checkout}; "
                f"refusing to bind {context.checkout}"
            )
        assert_leader_unchanged(
            root,
            existing.leader_snapshot,
            worker_root=existing.checkout.checkout,
            include_ignored=False,
        )
        return _binding_path(root, session_id)
    leader_snapshot = capture_leader_snapshot(root, include_ignored=False)
    binding_path = _binding_path(root, session_id)
    _write_json_atomic(
        binding_path,
        {
            "version": _BINDING_VERSION,
            "project_root": str(root),
            "checkout": str(context.checkout),
            "worktree": context.name,
            "runtime_root": str(context.runtime_root),
            "run_id": context.run_id,
            "recorded_at": time.time(),
            "leader_snapshot": {
                "head": leader_snapshot.head,
                "branch": leader_snapshot.branch,
                "status": leader_snapshot.status,
                "armed": leader_snapshot.armed,
            },
        },
    )
    return binding_path


def host_write_boundary_violation(payload: object, project_root: Path) -> str | None:
    root = _validated_project_root(project_root)
    tool_name = _first_string(payload, ("tool_name", "toolName", "tool")).lower()
    if tool_name not in _COMMAND_TOOLS | _WRITE_TOOLS:
        return None
    active = _active_checkouts(root)
    if not active:
        return None
    command = _first_string(payload, tuple(_COMMAND_KEYS))
    session_id = _first_string(payload, ("session_id", "sessionId"))
    binding = _load_binding(root, session_id, active) if session_id else None
    if binding is None:
        if tool_name in _COMMAND_TOOLS and command and _is_lifecycle_command(command):
            return None
        return (
            "this host session is not bound to an active worktree; run the printed "
            "agent-flow status/continue command first, then retry from that worktree"
        )
    try:
        assert_leader_unchanged(
            root,
            binding.leader_snapshot,
            worker_root=binding.checkout.checkout,
            include_ignored=False,
        )
    except WorktreeIsolationError as exc:
        return str(exc)

    current = binding.checkout
    if tool_name in _WRITE_TOOLS:
        write_base = _write_base(payload)
        targets = tuple(_write_targets(payload))
        if not targets:
            return "the write target could not be proven to belong to the bound worktree"
        for target in targets:
            violation = _target_violation(target, current, base=write_base)
            if violation is not None:
                return violation
        return None

    if not command:
        return "the shell command could not be inspected at the worktree boundary"
    if _is_lifecycle_command(command):
        operation = _lifecycle_operation(command)
        if operation in {"run", "start"}:
            return (
                f"this host session is bound to {current.checkout}; refusing to start "
                "a new run until the current worktree session is complete"
            )
        command_root = _lifecycle_root(command, current.checkout)
        if command_root not in {root, current.checkout}:
            return (
                f"this host session is bound to {root}; refusing a lifecycle command "
                f"for another project root ({command_root})"
            )
        selector = _worktree_selector(command)
        if selector is not None:
            selected = _active_checkout(root, selector=selector)
            if selected is None or selected.checkout != current.checkout:
                return (
                    f"this host session is bound to {current.checkout}; refusing a lifecycle "
                    f"command for another worktree ({selector})"
                )
        lifecycle_cwd = _declared_command_cwd(payload, command, current.checkout)
        if lifecycle_cwd is None or not _is_within(
            lifecycle_cwd, current.checkout
        ):
            return (
                f"lifecycle cwd is not bound to {current.checkout}; resume the host "
                "from that exact worktree"
            )
        return None

    declared_cwd = _declared_command_cwd(payload, command, current.checkout)
    literal_violation = _command_literal_violation(
        command,
        current=current,
        root=root,
        active=active,
    )
    if literal_violation is not None:
        return literal_violation
    if declared_cwd is None or not _is_within(declared_cwd, current.checkout):
        return (
            f"shell cwd is not bound to {current.checkout}; pass that exact worktree as "
            "the tool cwd or start the command with an explicit cd into it"
        )
    for candidate in _command_path_candidates(command, declared_cwd):
        resolved = _resolve_path(candidate, declared_cwd)
        if resolved is None:
            continue
        # worktree 안에서 만든 심링크는 그 worktree의 것으로 본다. 실경로만 보면
        # leader의 `node_modules`를 공유한 순간 그 안의 실행 파일이 전부 막힌다.
        logical = _logical_path(candidate, declared_cwd)
        if logical is not None and (
            _is_within(logical, current.checkout)
            or _is_within(logical, current.runtime_root)
        ):
            continue
        if _is_within(resolved, current.checkout) or _is_within(
            resolved, current.runtime_root
        ):
            continue
        for other in active:
            if _is_within(resolved, other.checkout) or _is_within(resolved, root):
                return (
                    f"shell command references checkout path {resolved} outside the bound "
                    f"worktree {current.checkout}"
                )
    # 위 후보 루프는 **다른 checkout**을 지킨다. 그것만으로는 `Write` 도구가 받는
    # 제약과 어긋난다 — `_target_violation`은 worktree 밖이면 무조건 거부하는데,
    # 셸은 leader/타 checkout만 보므로 `Write(/tmp/x)`는 막히고
    # `touch /tmp/x`는 통과했다. 같은 규칙을 셸의 **쓰기 대상**에만 적용한다.
    # 후보 전부에 적용하면 `/usr/bin/python3` 같은 읽기 경로까지 막힌다.
    for target in _command_write_targets(command):
        violation = _target_violation(target, current, base=declared_cwd)
        if violation is not None:
            return violation
    return None


def _active_checkouts(root: Path) -> tuple[ActiveCheckout, ...]:
    git_dir = root / ".git"
    if not git_dir.exists() and not git_dir.is_symlink():
        return ()
    _require_directory(git_dir, label="trusted leader git directory")
    runtime_claims = _runtime_active_claims(root)
    if not runtime_claims:
        return ()
    registered_by_path: dict[Path, Any] = {}
    for registered in list_registered_worktrees(root):
        checkout = real_path(registered.path)
        if checkout == root:
            continue
        registered_by_path[checkout] = registered

    contexts: list[ActiveCheckout] = []
    for name, runs in sorted(runtime_claims.items()):
        entry = _registered_for_claim(
            root=root, name=name, registered_by_path=registered_by_path
        )
        if entry is None:
            raise HostWriteBoundaryError(
                "active run state has no provable registered worktree owner: "
                f"{name}"
            )
        if len(runs) != 1:
            raise HostWriteBoundaryError(
                f"multiple active runs claim one worktree: {name}"
            )
        checkout = real_path(entry.path)
        # 관리 자리 밖의 checkout은 채택 기록이 소유를 증명한다. 증명이 깨지면
        # (raw git으로 지우고 다시 만든 경우처럼) 경계 오류로 바꿔 던진다 —
        # 여기서 isolation 예외가 그대로 나가면 host hook이 깔끔한 차단이 아니라
        # 추적 불가한 예외로 죽는다.
        try:
            verified_root = managed_worktree_root(root=root, path=checkout)
            verify_linked_worktree(
                root=root,
                path=checkout,
                expected_branch=entry.branch,
                managed_root=verified_root,
            )
        except WorktreeIsolationError as exc:
            raise HostWriteBoundaryError(
                f"active run checkout is not a provable managed worktree: {exc}"
            ) from exc
        run = runs[0]
        if run.metadata.get("checkout_identity") != f"worktree:{name}":
            raise HostWriteBoundaryError(
                f"active run metadata does not own its worktree: {checkout}"
            )
        if (
            entry.registration_identity is None
            or run.metadata.get("checkout_registration_identity")
            != entry.registration_identity
        ):
            raise HostWriteBoundaryError(
                f"active run registration does not own its worktree: {checkout}"
            )
        contexts.append(
            ActiveCheckout(
                name=name,
                checkout=checkout,
                branch=entry.branch,
                runtime_root=_worktree_runtime_root(root, name),
                run_id=run.run_id,
            )
        )
    return tuple(contexts)


def _registered_for_claim(
    *,
    root: Path,
    name: str,
    registered_by_path: dict[Path, Any],
) -> Any | None:
    """이 런타임 claim이 소유한 등록 행 하나.

    디렉터리 이름으로 묶으면 관리 루트 안의 ``feat-x``와 밖의 ``feat-x``가 같은 키가
    되고, 그 순간 무관한 worktree까지 저장소 전역으로 경계 판정이 죽는다. 그래서
    이름이 아니라 경로로 고른다.

    후보는 워커가 쓸 수 없는 두 근거에서만 나온다(생성 규약의 자리, 채택 기록).
    manifest는 후보 근거가 아니다 — 워커의 쓰기 허용 구역 안이라 자기 claim을 다른
    checkout으로 돌릴 수 있다. 증명이 안 되면 ``None``이고, 호출자는 fail-closed다.
    """
    for candidate in trusted_checkout_paths(root=root, name=name):
        entry = registered_by_path.get(candidate)
        if entry is not None:
            return entry
    return None


def _active_checkout(
    root: Path,
    *,
    selector: str,
    run_id: str | None = None,
) -> ActiveCheckout | None:
    matches: list[ActiveCheckout] = []
    path_candidates = [_resolve_path(selector, root)]
    try:
        path_candidates.append(_resolve_path(selector, Path.cwd()))
    except OSError:
        pass
    for context in _active_checkouts(root):
        selected = selector in {context.name, context.branch}
        selected = selected or any(
            candidate is not None and candidate == context.checkout
            for candidate in path_candidates
        )
        if not selected:
            continue
        if run_id is not None and context.run_id != run_id:
            continue
        matches.append(context)
    if len(matches) > 1:
        raise HostWriteBoundaryError(
            f"worktree selector is ambiguous at the host boundary: {selector}"
        )
    return matches[0] if matches else None


def _worktree_runtime_root(root: Path, name: str) -> Path:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise HostWriteBoundaryError(f"invalid worktree runtime key: {name!r}")
    git_dir = _require_directory(
        root / ".git",
        label="trusted leader git directory",
    )
    current = git_dir
    for component in ("agent-flow", "worktrees", name):
        current = current / component
        if current.exists() or current.is_symlink():
            _require_directory(current, label="worktree runtime directory")
    return current


def _runtime_active_claims(
    root: Path,
) -> dict[str, tuple[_ActiveRunClaim, ...]]:
    current = _require_directory(
        root / ".git",
        label="trusted leader git directory",
    )
    for component in ("agent-flow", "worktrees"):
        current = current / component
        if not current.exists() and not current.is_symlink():
            return {}
        _require_directory(current, label="worktree runtime directory")
    try:
        entries = tuple(current.iterdir())
    except OSError as exc:
        raise HostWriteBoundaryError(
            f"cannot inspect worktree runtime state under {current}"
        ) from exc
    claims: dict[str, tuple[_ActiveRunClaim, ...]] = {}
    for entry in entries:
        _require_directory(entry, label="worktree runtime directory")
        active = _active_run_claims(entry)
        if active:
            claims[entry.name] = active
    return claims


def _active_run_claims(runtime_root: Path) -> tuple[_ActiveRunClaim, ...]:
    current = runtime_root
    for component in (".agent-flow", "runs"):
        current = current / component
        if not current.exists() and not current.is_symlink():
            return ()
        _require_directory(current, label="active run directory")
    claims: list[_ActiveRunClaim] = []
    try:
        entries = tuple(current.iterdir())
    except OSError as exc:
        raise HostWriteBoundaryError(
            f"cannot inspect active runs under {current}"
        ) from exc
    for run_dir in entries:
        try:
            run_info = run_dir.lstat()
        except OSError as exc:
            raise HostWriteBoundaryError(
                f"cannot inspect run state {run_dir}"
            ) from exc
        if not stat.S_ISDIR(run_info.st_mode) or run_dir.is_symlink():
            continue
        active = run_dir / "active"
        if not active.exists() and not active.is_symlink():
            continue
        _require_regular_file(active, label="active run marker")
        metadata = _read_trusted_json(run_dir / "meta.json")
        claims.append(_ActiveRunClaim(run_id=run_dir.name, metadata=metadata))
    return tuple(sorted(claims, key=lambda claim: claim.run_id))


def _read_trusted_json(path: Path) -> dict[str, Any]:
    _require_regular_file(path, label="active run metadata")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HostWriteBoundaryError(f"cannot read trusted JSON state {path}") from exc
    if not isinstance(payload, dict):
        raise HostWriteBoundaryError(f"trusted JSON state is not an object: {path}")
    return payload


def _require_directory(path: Path, *, label: str) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise HostWriteBoundaryError(f"{label} is unavailable: {path}") from exc
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise HostWriteBoundaryError(f"{label} is not a real directory: {path}")
    return path


def _require_regular_file(path: Path, *, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise HostWriteBoundaryError(f"{label} is unavailable: {path}") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise HostWriteBoundaryError(f"{label} is not a trusted file: {path}")


def _load_binding(
    root: Path,
    session_id: str,
    active: tuple[ActiveCheckout, ...],
) -> HostCheckoutBinding | None:
    path = _binding_path(root, session_id)
    try:
        info = path.lstat()
        if not path.is_file() or path.is_symlink() or info.st_nlink != 1:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != _BINDING_VERSION:
        return None
    if payload.get("project_root") != str(root):
        return None
    snapshot_payload = payload.get("leader_snapshot")
    if not isinstance(snapshot_payload, dict):
        return None
    head = snapshot_payload.get("head")
    branch = snapshot_payload.get("branch")
    status = snapshot_payload.get("status")
    armed = snapshot_payload.get("armed")
    if (
        not isinstance(head, str)
        or not isinstance(branch, str)
        or not isinstance(status, str)
        or armed is not True
    ):
        return None
    leader_snapshot = LeaderSnapshot(
        head=head,
        branch=branch,
        status=status,
        armed=True,
    )
    for context in active:
        if (
            payload.get("checkout") == str(context.checkout)
            and payload.get("worktree") == context.name
            and payload.get("runtime_root") == str(context.runtime_root)
            and payload.get("run_id") == context.run_id
        ):
            return HostCheckoutBinding(
                session_key=_session_key(session_id),
                checkout=context,
                leader_snapshot=leader_snapshot,
            )
    return None


def _target_violation(
    target: str,
    current: ActiveCheckout,
    *,
    base: Path | None,
) -> str | None:
    text = target.strip()
    if _VIRTUAL_PATH.match(text):
        if _SAFE_VIRTUAL_PATH.match(text):
            return None
        return f"virtual write target is not trusted by the worktree boundary: {target!r}"
    try:
        relative = not Path(text).expanduser().is_absolute()
    except (OSError, RuntimeError, ValueError):
        relative = True
    if relative and base is None:
        return (
            f"relative write target {target!r} has no trusted host cwd; "
            "resume the host from the bound worktree"
        )
    resolved = _resolve_path(target, base or current.checkout)
    if resolved is None:
        return f"write target is not a valid filesystem path: {target!r}"
    if _is_within(resolved, current.checkout) or _is_within(
        resolved, current.runtime_root
    ):
        return None
    return (
        f"write target {resolved} is outside the bound worktree {current.checkout}; "
        "stop and resume from the printed worktree path"
    )


def _write_targets(payload: object) -> Iterable[str]:
    tool_input = _tool_input(payload)
    tool_name = _first_string(payload, ("tool_name", "toolName", "tool")).lower()
    patch_command = tool_name in {"apply_patch", "edit", "multiedit", "multi_edit"}
    seen: set[str] = set()

    def collect(value: object, key: str = "") -> None:
        lowered = key.lower()
        if isinstance(value, dict):
            for child_key, child in value.items():
                collect(child, str(child_key))
            return
        if isinstance(value, list):
            for child in value:
                collect(child, key)
            return
        if not isinstance(value, str):
            return
        if lowered in _PATH_KEYS:
            seen.add(value)
        if (
            lowered in _PATCH_KEYS
            or (patch_command and lowered in _COMMAND_KEYS)
            or (not key and isinstance(tool_input, str))
        ):
            for match in _PATCH_PATH.finditer(value):
                path = match.group("hashline") or match.group("apply") or match.group("move")
                if path:
                    seen.add(path.strip())

    collect(tool_input)
    return tuple(sorted(seen))


def _write_base(payload: object) -> Path | None:
    payload_cwd = ""
    if isinstance(payload, dict):
        raw_cwd = payload.get("cwd")
        if isinstance(raw_cwd, str):
            payload_cwd = raw_cwd.strip()
    base: Path | None = None
    if payload_cwd:
        try:
            expanded = Path(payload_cwd).expanduser()
            if expanded.is_absolute():
                base = expanded.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            base = None
    explicit = _first_string_for_keys(_tool_input(payload), _CWD_KEYS)
    if not explicit:
        return base
    try:
        expanded = Path(explicit).expanduser()
    except (OSError, RuntimeError, ValueError):
        return None
    if expanded.is_absolute():
        return _resolve_path(explicit, expanded.parent)
    if base is None:
        return None
    return _resolve_path(explicit, base)


def _declared_command_cwd(
    payload: object,
    command: str,
    checkout: Path,
) -> Path | None:
    tool_input = _tool_input(payload)
    explicit = _first_string_for_keys(tool_input, _CWD_KEYS)
    payload_cwd = _first_string(payload, ("cwd",))
    base = _resolve_path(payload_cwd, checkout) if payload_cwd else checkout
    if explicit:
        return _resolve_path(explicit, base or checkout)
    if base is not None and _is_within(base, checkout):
        return base
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if len(tokens) >= 2 and tokens[0] in {"cd", "pushd"}:
        return _resolve_path(tokens[1], base or checkout)
    return None


def _command_path_candidates(command: str, cwd: Path) -> Iterable[str]:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError as exc:
        raise HostWriteBoundaryError("cannot parse shell command safely") from exc
    candidates: list[str] = []
    for token in tokens:
        stripped = token.strip(";&|()<>")
        if not stripped:
            continue
        if "=" in stripped and not stripped.startswith(("/", "./", "../", "~")):
            _, stripped = stripped.split("=", 1)
        if not stripped or _VIRTUAL_PATH.match(stripped):
            continue
        local = cwd / stripped
        if (
            stripped.startswith(("/", "./", "../", "~"))
            or "/" in stripped
            or local.is_symlink()
        ):
            candidates.append(stripped)
    return tuple(candidates)


# 셸이 **쓰는** 자리만 고른다. `_command_path_candidates`는 읽기 경로까지 모으므로
# 그 전부를 worktree 안으로 강제하면 `python3 /usr/bin/thing.py` 같은 정상 호출이
# 막힌다. 리터럴로 특정된 쓰기 대상만 본다.
_SHELL_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "|&"})
_WRITE_REDIRECTS = frozenset({">", ">>"})
# `2>&1`은 fd 복제라 파일 대상이 아니다. 뒤 토큰을 목적지로 읽으면 `1`을 경로로 본다.
_NON_FILE_REDIRECTS = frozenset({">&", "<&", "<", "<<", "<<<"})
_WRITE_OPERAND_COMMANDS = frozenset(
    {
        "touch", "mkdir", "rmdir", "rm", "unlink", "shred",
        "truncate", "tee", "chmod", "chown", "chgrp",
    }
)
# 마지막 피연산자만 목적지인 명령.
_WRITE_DEST_LAST_COMMANDS = frozenset({"cp", "mv", "ln", "install", "rsync"})


def _shell_tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError as exc:
        raise HostWriteBoundaryError("cannot parse shell command safely") from exc


def _command_write_targets(command: str) -> tuple[str, ...]:
    targets: list[str] = []
    argv: list[str] = []

    def drain() -> None:
        if not argv:
            return
        name = os.path.basename(argv[0]).lower()
        operands = [token for token in argv[1:] if not token.startswith("-")]
        if name in _WRITE_OPERAND_COMMANDS:
            targets.extend(operands)
        elif name in _WRITE_DEST_LAST_COMMANDS and len(operands) >= 2:
            targets.append(operands[-1])
        elif name == "dd":
            targets.extend(
                token.partition("=")[2]
                for token in argv[1:]
                if token.startswith("of=")
            )
        elif name == "sed" and any(
            token.startswith("-i") for token in argv[1:] if token.startswith("-")
        ):
            # 첫 피연산자는 스크립트고, 제자리 편집 대상은 그 뒤다.
            targets.extend(operands[1:])
        argv.clear()

    tokens = _shell_tokens(command)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _SHELL_SEPARATORS:
            drain()
            index += 1
            continue
        if token in _WRITE_REDIRECTS:
            if index + 1 < len(tokens):
                targets.append(tokens[index + 1])
            index += 2
            continue
        if token in _NON_FILE_REDIRECTS:
            index += 2
            continue
        argv.append(token)
        index += 1
    drain()
    return tuple(target for target in targets if target.strip())



def _command_literal_violation(
    command: str,
    *,
    current: ActiveCheckout,
    root: Path,
    active: tuple[ActiveCheckout, ...],
) -> str | None:
    allowed = tuple(
        sorted(
            {str(current.checkout), str(current.runtime_root)},
            key=len,
            reverse=True,
        )
    )
    protected = {str(root)}
    protected.update(str(context.checkout) for context in active)
    for prefix in sorted(protected, key=len, reverse=True):
        start = 0
        while True:
            index = command.find(prefix, start)
            if index < 0:
                break
            suffix = command[index + len(prefix) : index + len(prefix) + 1]
            boundary = not suffix or suffix in {"/", "\\", "'", '"', " ", "\t", ":", ")", "]", "}"}
            if boundary and not any(command.startswith(path, index) for path in allowed):
                return (
                    f"shell command references checkout path {prefix} outside the bound "
                    f"worktree {current.checkout}"
                )
            start = index + len(prefix)
    return None


def _tool_input(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    for key in ("tool_input", "input", "parameters"):
        if key in payload:
            return payload[key]
    return payload


def _first_string(value: object, keys: tuple[str, ...]) -> str:
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for child in value.values():
            found = _first_string(child, keys)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_string(child, keys)
            if found:
                return found
    return ""


def _first_string_for_keys(value: object, keys: frozenset[str]) -> str:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in keys and isinstance(child, str) and child.strip():
                return child.strip()
            found = _first_string_for_keys(child, keys)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_string_for_keys(child, keys)
            if found:
                return found
    return ""


def _first_number(value: object, keys: tuple[str, ...]) -> int | None:
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                return candidate
        for child in value.values():
            found = _first_number(child, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_number(child, keys)
            if found is not None:
                return found
    return None


def _last_status_payload(payload: object) -> dict[str, Any] | None:
    found: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    for text in _output_strings(payload):
        for match in _STATUS_JSON.finditer(text):
            try:
                value, _ = decoder.raw_decode(text[match.end() :])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                found.append(value)
    return found[-1] if found else None


def _output_strings(payload: object) -> Iterable[str]:
    if not isinstance(payload, dict):
        return ()
    values: list[str] = []
    for key in ("output", "stdout", "tool_response", "toolResponse", "result", "response"):
        if key in payload:
            values.extend(_strings(payload[key]))
    return tuple(values)


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, dict):
        values: list[str] = []
        for key, child in value.items():
            if key in {"text", "content", "output", "stdout", "message"}:
                values.extend(_strings(child))
        return tuple(values)
    if isinstance(value, list):
        values: list[str] = []
        for child in value:
            values.extend(_strings(child))
        return tuple(values)
    return ()


def _direct_shell_argv(command: str) -> tuple[str, ...] | None:
    if any(marker in command for marker in ("\r", "\n", "`", "$(")):
        return None
    try:
        lexer = shlex.shlex(
            command,
            posix=True,
            punctuation_chars=_SHELL_PUNCTUATION,
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        argv = tuple(lexer)
    except ValueError:
        return None
    if not argv or any(
        token and all(character in _SHELL_PUNCTUATION for character in token)
        for token in argv
    ):
        return None
    return argv


def _agent_flow_arguments(command: str) -> tuple[str, ...] | None:
    argv = _direct_shell_argv(command)
    if not argv or argv[0] not in {"agent-flow", "agent-flow-kit"}:
        return None
    return argv[1:]


def _is_lifecycle_command(command: str) -> bool:
    arguments = _agent_flow_arguments(command)
    if not arguments:
        return False
    if arguments[0] == "run" and len(arguments) > 1 and arguments[1] in {
        "start",
        "status",
    }:
        return True
    return arguments[0] in _LIFECYCLE_COMMANDS


def _lifecycle_operation(command: str) -> str | None:
    arguments = _agent_flow_arguments(command)
    if not arguments:
        return None
    if arguments[0] == "run" and len(arguments) > 1 and arguments[1] in {
        "start",
        "status",
    }:
        return arguments[1]
    return arguments[0]


def _lifecycle_root(command: str, default: Path) -> Path:
    arguments = _agent_flow_arguments(command) or ()
    for index, token in enumerate(arguments):
        if token == "--root" and index + 1 < len(arguments):
            return real_path(Path(arguments[index + 1]).expanduser())
        if token.startswith("--root="):
            return real_path(Path(token.split("=", 1)[1]).expanduser())
    return real_path(default)


def _worktree_selector(command: str) -> str | None:
    arguments = _agent_flow_arguments(command)
    if arguments is None:
        return None
    for index, token in enumerate(arguments):
        if token == "--worktree" and index + 1 < len(arguments):
            return arguments[index + 1]
        if token.startswith("--worktree="):
            return token.split("=", 1)[1]
    return None


def _binding_path(root: Path, session_id: str) -> Path:
    current = _require_directory(
        root / ".git",
        label="trusted leader git directory",
    )
    for component in ("agent-flow", _BINDING_DIR):
        current = current / component
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise HostWriteBoundaryError(
                f"cannot create trusted host binding directory {current}"
            ) from exc
        _require_directory(current, label="trusted host binding directory")
    try:
        current.chmod(0o700)
    except OSError as exc:
        raise HostWriteBoundaryError(
            f"cannot secure host binding directory {current}"
        ) from exc
    return current / f"{_session_key(session_id)}.json"


def _session_key(session_id: str) -> str:
    if not session_id or len(session_id) > 4096:
        raise HostWriteBoundaryError("invalid host session identity")
    return hashlib.sha256(session_id.encode("utf-8", "strict")).hexdigest()


def _validated_project_root(project_root: Path) -> Path:
    root = real_path(project_root)
    if not root.is_dir():
        raise HostWriteBoundaryError(f"project root does not exist: {root}")
    return root


def _resolve_path(value: str, base: Path) -> Path | None:
    text = value.strip().strip("\"'")
    if not text or _VIRTUAL_PATH.match(text):
        return None
    try:
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = base / path
        return path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _logical_path(value: str, base: Path) -> Path | None:
    """심링크를 따르지 않고 표준화만 한 논리 경로."""
    text = value.strip().strip("\"'")
    if not text or _VIRTUAL_PATH.match(text):
        return None
    try:
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = base / path
        return Path(os.path.normpath(path))
    except (OSError, RuntimeError, ValueError):
        return None


def bound_worktree_for_session(
    session_id: str, project_root: Path
) -> "HostCheckoutBinding | None":
    """세션에 연결된 worktree binding. worktree-tripwire hook이 쓴다."""
    root = _validated_project_root(project_root)
    active = _active_checkouts(root)
    if not active:
        return None
    return _load_binding(root, session_id, active)


def _is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False


def _write_json_atomic(path: Path, payload: object) -> None:
    _require_directory(path.parent, label="trusted host binding directory")
    directory_flags = os.O_RDONLY
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(path.parent, directory_flags)
    except OSError as exc:
        raise HostWriteBoundaryError(
            f"cannot open trusted host binding directory {path.parent}"
        ) from exc
    temporary = f".{path.name}.{secrets.token_hex(16)}"
    fd = -1
    try:
        file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        file_flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temporary, file_flags, 0o600, dir_fd=directory_fd)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temporary,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    except OSError as exc:
        raise HostWriteBoundaryError(
            f"cannot persist trusted host binding {path}"
        ) from exc
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise HostWriteBoundaryError(
                f"cannot remove temporary host binding {temporary}"
            ) from exc
        finally:
            os.close(directory_fd)
