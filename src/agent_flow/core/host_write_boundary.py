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
    LEADER_SWEEP_TRACKED_ONLY,
    LeaderDriftError,
    LeaderSnapshot,
    WorktreeIsolationError,
    assert_leader_unchanged,
    capture_leader_snapshot,
    leader_drift_message,
    leader_snapshot_payload,
    list_registered_worktrees,
    managed_worktree_root,
    recorded_snapshot_scope,
    recorded_snapshot_version,
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
_STATUS_RUN = re.compile(r"(?:^|\n)run:\s*([^\r\n]+)", re.MULTILINE)
_STATUS_NEXT_COMMAND = re.compile(r"(?:^|\n)next_command:\s*([^\r\n]+)", re.MULTILINE)
# binding **파일 스키마** 버전. 스냅샷 형식은 별개 축이고
# `LeaderSnapshot.version`이 들고 있다 — 스냅샷 형식이 바뀌었다고 이걸 올리면
# 낡은 스냅샷 하나 때문에 binding 파일 전체를 못 읽는 것으로 처리된다.
_BINDING_VERSION = 2
_BINDING_DIR = "host-sessions"
# 워커가 **자기 런을 진행시키는** 명령. 면제 범위를 넓히면 안 되는 이유는
# `_is_lifecycle_command` docstring에 있다.
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
class SessionScope:
    """이 세션이 자기 것으로 주장할 수 있는 자리.

    bound 세션은 binding이 지목한 active checkout이고, unbound 세션은 자기가 서
    있는 checkout이다. 판정 규칙은 하나다 — 자기 자리 안은 열고, **다른
    checkout의 작업**은 막는다.

    unbound 세션에도 자리를 주는 이유: 활성 run이 하나라도 있으면 unbound 세션의
    모든 write/command를 통째로 막던 시절엔, 그 run과 무관한 세션 —
    agent-flow가 만들지 않은 worktree에서 자기 브랜치를 커밋·푸시하는 세션 — 이
    함께 죽었다. 지켜야 하는 것은 저장소 전체가 아니라 남의 작업이다.
    """

    checkout: Path | None
    runtime_root: Path | None
    bound: bool

    def owns(self, path: Path) -> bool:
        return (self.checkout is not None and _is_within(path, self.checkout)) or (
            self.runtime_root is not None and _is_within(path, self.runtime_root)
        )

    def reject_target(self, resolved: Path, owner: Path) -> str:
        if self.bound:
            return (
                f"write target {resolved} is outside the bound worktree "
                f"{self.checkout}; stop and resume from the printed worktree path"
            )
        return self._unbound_reject(f"write target {resolved}", owner)

    def reject_reference(self, reference: object, owner: Path) -> str:
        if self.bound:
            return (
                f"shell command references checkout path {reference} outside the bound "
                f"worktree {self.checkout}"
            )
        return self._unbound_reject(f"shell command reference {reference}", owner)

    def reject_unprovable(self, subject: str) -> str:
        if self.bound:
            return f"the {subject} could not be proven to belong to the bound worktree"
        return (
            f"this host session is not bound to an active worktree and the {subject} "
            "could not be placed in a checkout; declare the host cwd, or run the "
            "printed agent-flow status/continue command first"
        )

    def reject_relative(self, target: str) -> str:
        if self.bound:
            return (
                f"relative write target {target!r} has no trusted host cwd; "
                "resume the host from the bound worktree"
            )
        return (
            f"relative write target {target!r} has no trusted host cwd; this host "
            "session is not bound to an active worktree, so agent-flow cannot tell "
            "whose work it would touch"
        )

    def reject_destructive(self, name: str, target: Path, owner: Path) -> str:
        return (
            f"`{name}` is refused: {target} reaches {owner}, which belongs to another "
            "checkout of this repository. Unlike ordinary writes this cannot be "
            "detected after the fact and undone, so it is blocked up front"
            + (f"; run it inside {self.checkout} instead" if self.checkout else "")
        )

    def _unbound_reject(self, subject: str, owner: Path) -> str:
        own = f" Work inside {self.checkout} instead." if self.checkout else ""
        return (
            f"this host session is not bound to an active worktree; {subject} belongs "
            f"to {owner}, which agent-flow protects while a run is active. Run the "
            "printed agent-flow status/continue command from that worktree first."
            + own
        )


@dataclass(frozen=True)
class _ActiveRunClaim:
    run_id: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class _CheckoutSurvey:
    """한 번의 git 조회로 얻는 checkout 지형. active만으로는 부족하다 —
    run이 붙지 않은 등록 worktree도 남의 작업이라 쓰기 대상이 되면 안 된다."""

    active: tuple[ActiveCheckout, ...]
    registered: tuple[Path, ...]




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
    # 이름으로 고르지 않는다. binding에 필요한 근거는 아래에서 전부 확인한다 —
    # 신뢰된 설치 산출물의 호출인가, exit 0인가, 출력에 실제 status payload가
    # 있는가. status를 내지 않는 명령은 그 셋에서 자동으로 탈락한다.
    if not command or _agent_flow_arguments(
        command, root=root, cwd=_session_cwd(payload, command)
    ) is None:
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
    # 열거는 한 번만 한다. 두 번 돌면 active run 수에 비례하는 검증 비용이 그대로
    # 두 배가 되고, hook 실행 시간 제한을 넘긴 세션은 binding을 못 받아 guard가 모든
    # write를 막는다. run 3개에서 그 교착이 실측됐다(10.7~11.6s, 제한 8s).
    active = _active_checkouts(root)
    context = _active_checkout(root, selector=selector, run_id=run_id, active=active)
    if context is None:
        raise HostWriteBoundaryError(
            "status output did not identify one verified active managed worktree"
        )
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
            "leader_snapshot": leader_snapshot_payload(leader_snapshot),
        },
    )
    return binding_path


def host_write_boundary_violation(payload: object, project_root: Path) -> str | None:
    root = _validated_project_root(project_root)
    tool_name = _first_string(payload, ("tool_name", "toolName", "tool")).lower()
    if tool_name not in _COMMAND_TOOLS | _WRITE_TOOLS:
        return None
    survey = _survey_checkouts(root)
    active = survey.active
    if not active:
        return None
    command = _first_string(payload, tuple(_COMMAND_KEYS))
    session_id = _first_string(payload, ("session_id", "sessionId"))
    binding = _load_binding(root, session_id, active) if session_id else None
    # 이 cwd는 두 곳에서 같은 값이어야 한다: 설치된 CLI를 상대 경로로 부른 경우의
    # 기준점과, unbound 세션의 자리 판정. 따로 구하면 한쪽만 맞는 상태가 생긴다.
    # 명령 유무로 가르지 않는다 — write 도구는 command가 비어 있고, 그때 cwd를
    # 버리면 자기 checkout에서 일하는 세션이 자기 파일을 못 쓴다.
    session_cwd = _session_cwd(payload, command)
    if (
        tool_name in _COMMAND_TOOLS
        and command
        and _is_lifecycle_command(command, root=root, cwd=session_cwd)
    ):
        # agent-flow 자신의 호출은 leader drift 검증 앞에 둔다. 그 검증이 앞에
        # 서면 leader가 움직인 순간 status/continue까지 함께 막혀, 경계가 자기가
        # 만든 정지 상태의 유일한 해제 수단을 스스로 막는다.
        if binding is None:
            return None
        return _lifecycle_violation(
            payload,
            command,
            root=root,
            current=binding.checkout,
            active=active,
            cwd=session_cwd,
        )
    if binding is None:
        return _unbound_violation(
            payload,
            root=root,
            survey=survey,
            command=command,
            tool_name=tool_name,
            cwd=session_cwd,
        )
    current = binding.checkout
    try:
        assert_leader_unchanged(
            root,
            binding.leader_snapshot,
            worker_root=current.checkout,
            include_ignored=False,
        )
    except LeaderDriftError as exc:
        return leader_drift_message(exc.drift, worker_root=current.checkout)
    except WorktreeIsolationError as exc:
        return str(exc)
    return _scope_violation(
        payload,
        root=root,
        scope=SessionScope(
            checkout=current.checkout,
            runtime_root=current.runtime_root,
            bound=True,
        ),
        protected=_protected_write_roots(root, survey, current),
        active=active,
        command=command,
        tool_name=tool_name,
        command_cwd=(
            _declared_command_cwd(payload, command, current.checkout)
            if command
            else None
        ),
        require_cwd_inside_scope=True,
    )


def _lifecycle_violation(
    payload: object,
    command: str,
    *,
    root: Path,
    current: ActiveCheckout,
    active: tuple[ActiveCheckout, ...],
    cwd: Path | None,
) -> str | None:
    """bound 세션의 lifecycle 명령. 자기 프로젝트·자기 worktree·자기 자리만 허용한다.

    leader tripwire보다 **먼저** 돈다. drift가 그 검증 앞에 서면 leader가 움직인
    순간 워커가 자기 런을 확인하거나 이어 갈 수단까지 함께 막힌다. 대신 여기서
    대상이 이 세션의 worktree인지는 그대로 검증하므로, 다른 run으로 새어 나가지는
    못한다.
    """
    operation = _lifecycle_operation(command, root=root, cwd=cwd)
    if operation in {"run", "start"}:
        return (
            f"this host session is bound to {current.checkout}; refusing to start "
            "a new run until the current worktree session is complete"
        )
    command_root = _lifecycle_root(command, current.checkout, root=root, cwd=cwd)
    if command_root not in {root, current.checkout}:
        return (
            f"this host session is bound to {root}; refusing a lifecycle command "
            f"for another project root ({command_root})"
        )
    selector = _worktree_selector(command, root=root, cwd=cwd)
    if selector is not None:
        selected = _active_checkout(root, selector=selector, active=active)
        if selected is None or selected.checkout != current.checkout:
            return (
                f"this host session is bound to {current.checkout}; refusing a lifecycle "
                f"command for another worktree ({selector})"
            )
    lifecycle_cwd = _declared_command_cwd(payload, command, current.checkout)
    if lifecycle_cwd is None or not _is_within(lifecycle_cwd, current.checkout):
        return (
            f"lifecycle cwd is not bound to {current.checkout}; resume the host "
            "from that exact worktree"
        )
    return None


def _unbound_violation(
    payload: object,
    *,
    root: Path,
    survey: _CheckoutSurvey,
    command: str,
    tool_name: str,
    cwd: Path | None,
) -> str | None:
    """binding이 없는 세션. 자리는 **선언된 cwd**로만 증명된다.

    leader와 활성 worktree는 unbound 세션에게 계속 닫혀 있다 — 그 둘이야말로
    bind 전에 새는 워커가 노리는 자리다. 열리는 건 활성 run이 없는 등록 checkout
    하나, 즉 이 세션이 실제로 서서 일하는 자기 작업 자리뿐이다. 그래야 옆 worktree
    의 run 때문에 무관한 checkout의 commit/push가 죽지 않는다.
    """
    standing = _standing_checkout(root, survey, cwd)
    ownable = (
        standing is not None
        and standing != root
        and _active_checkout_at(survey, standing) is None
    )
    scope = SessionScope(
        checkout=standing if ownable else None,
        runtime_root=None,
        bound=False,
    )
    return _scope_violation(
        payload,
        root=root,
        scope=scope,
        protected=_unbound_protected_roots(root, survey, scope.checkout),
        active=survey.active,
        command=command,
        tool_name=tool_name,
        command_cwd=cwd,
        require_cwd_inside_scope=False,
    )


def _scope_violation(
    payload: object,
    *,
    root: Path,
    scope: SessionScope,
    protected: tuple[Path, ...],
    active: tuple[ActiveCheckout, ...],
    command: str,
    tool_name: str,
    command_cwd: Path | None,
    require_cwd_inside_scope: bool,
) -> str | None:
    """자리가 정해진 뒤의 공통 판정. bound/unbound가 같은 규칙을 쓴다."""
    if tool_name in _WRITE_TOOLS:
        targets = tuple(_write_targets(payload))
        if not targets:
            return scope.reject_unprovable("write target")
        write_base = _write_base(payload)
        for target in targets:
            violation = _target_violation(
                target, scope, base=write_base, protected=protected
            )
            if violation is not None:
                return violation
        return None

    if not command:
        return "the shell command could not be inspected at the worktree boundary"
    # 셸 판정은 두 개뿐이다. 명령이 **무엇을 쓰려는지** 맞히지 않는다 — 셸 문법은
    # 무한하고 우리 목록은 유한해서, 그 방향은 예외를 끝없이 붙이면서도 실제로
    # 무엇이 막히는지 말할 수 없게 만든다.
    #
    #   R1 보호 경로 리터럴: 명령 텍스트에 leader·형제 checkout·런타임 상태 경로가
    #      나타나면 거부. 문자열 스캔이라 파서가 필요 없다.
    #   R2 파괴 명령 금지 목록: `rm`/`mv`/`git checkout|restore|reset|clean`처럼
    #      되돌릴 수 없는 몇 개만, 보호 경로에 닿을 수 있으면 거부.
    #
    # 동적 경로(`$(...)`, 변수, 셸 확장)는 정적으로 볼 수 없다. 그 구멍은 파서로
    # 메우지 않고 phase 경계의 tripwire(`assert_leader_unchanged`)가 내용까지
    # 비교해서 잡는다. R2가 있는 이유는 그 tripwire가 **탐지 전용**이라
    # `rm -rf <leader>` 한 줄은 잡아도 이미 늦기 때문이다.
    literal_violation = _command_literal_violation(
        command,
        scope=scope,
        protected=protected,
    )
    if literal_violation is not None:
        return literal_violation
    if require_cwd_inside_scope and (
        scope.checkout is None
        or command_cwd is None
        or not _is_within(command_cwd, scope.checkout)
    ):
        return (
            f"shell cwd is not bound to {scope.checkout}; pass that exact worktree as "
            "the tool cwd or start the command with an explicit cd into it"
        )
    probe = command_cwd or scope.checkout or root
    for candidate in _command_path_candidates(command, probe):
        if command_cwd is None and not _is_absolute_text(candidate):
            # cwd를 증명하지 못한 세션에서 상대 경로를 leader 기준으로 풀면 없는
            # 위반을 만들어 낸다. 그런 세션은 절대 경로만 판정한다.
            continue
        resolved = _resolve_path(candidate, probe)
        if resolved is None:
            continue
        # worktree 안에서 만든 심링크는 그 worktree의 것으로 본다. 실경로만 보면
        # leader의 `node_modules`를 공유한 순간 그 안의 실행 파일이 전부 막힌다.
        logical = _logical_path(candidate, probe)
        if logical is not None and scope.owns(logical):
            continue
        if scope.owns(resolved):
            continue
        for other_root in protected:
            if _is_within(resolved, other_root):
                return scope.reject_reference(resolved, other_root)
    return _destructive_violation(
        command,
        scope=scope,
        protected=protected,
        cwd=command_cwd,
    )


def _session_cwd(payload: object, command: str) -> Path | None:
    """세션이 서 있다고 **선언한** 자리. 없으면 ``None``.

    bound 세션의 판정과 같은 증거를 쓴다: payload cwd, tool cwd, 그리고 명령
    앞머리의 `cd`. 여기서 대상 경로를 근거로 삼으면 안 된다 — 쓰기 대상이 자기
    권한을 스스로 만들어 내는 순환이 된다.
    """
    base = _write_base(payload)
    if not command:
        return base
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return base
    if len(tokens) < 2 or tokens[0] not in {"cd", "pushd"}:
        return base
    moved = _resolve_path(tokens[1], base or Path(os.sep))
    if moved is None:
        return base
    if base is None and not _is_absolute_text(tokens[1]):
        return None
    return moved


def _standing_checkout(
    root: Path,
    survey: _CheckoutSurvey,
    cwd: Path | None,
) -> Path | None:
    """``cwd``를 담는 이 저장소의 checkout. 가장 안쪽 것이 이긴다.

    leader 폴더 **안**에 등록된 worktree(옛 자리 규약)가 있을 수 있어서, 바깥부터
    맞히면 worktree 안의 세션을 leader 소속으로 잘못 읽는다.
    """
    if cwd is None:
        return None
    best: Path | None = None
    for candidate in (root, *survey.registered):
        if not _is_within(cwd, candidate):
            continue
        if best is None or len(str(candidate)) > len(str(best)):
            best = candidate
    return best


def _active_checkout_at(
    survey: _CheckoutSurvey, path: Path
) -> ActiveCheckout | None:
    for context in survey.active:
        if context.checkout == path:
            return context
    return None


def _unbound_protected_roots(
    root: Path,
    survey: _CheckoutSurvey,
    standing: Path | None,
) -> tuple[Path, ...]:
    """unbound 세션이 건드리면 안 되는 자리.

    leader, 등록된 checkout 전부, 활성 run의 런타임 상태, 그리고 신뢰 상태
    디렉터리다. 빠지는 건 이 세션이 서 있는 checkout 하나뿐이고, 그것도 활성
    run이 없을 때만이다(``standing``은 그 조건을 이미 통과한 값이다).
    """
    roots = [root, *survey.registered]
    roots.extend(context.runtime_root for context in survey.active)
    kept = [path for path in roots if path != standing]
    # standing이 leader일 때도 런타임·binding 상태는 남의 것이다. leader를 빼는
    # 경로가 생기더라도 이 자리는 항상 닫아 둔다.
    kept.append(root / ".git" / "agent-flow")
    return tuple(dict.fromkeys(kept))


def _is_absolute_text(value: str) -> bool:
    text = value.strip().strip("\"'")
    if not text:
        return False
    try:
        return Path(text).expanduser().is_absolute()
    except (OSError, RuntimeError, ValueError):
        return False


def _active_checkouts(root: Path) -> tuple[ActiveCheckout, ...]:
    return _survey_checkouts(root).active


def _survey_checkouts(root: Path) -> _CheckoutSurvey:
    git_dir = root / ".git"
    if not git_dir.exists() and not git_dir.is_symlink():
        return _CheckoutSurvey(active=(), registered=())
    _require_directory(git_dir, label="trusted leader git directory")
    runtime_claims = _runtime_active_claims(root)
    if not runtime_claims:
        return _CheckoutSurvey(active=(), registered=())
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
            # checkout이 아예 사라졌으면 그 run은 이어질 수 없고 지킬 대상도 없다.
            # 그걸 조작으로 보고 raise하면 hook이 이 프로젝트의 **모든** write와
            # bash에서 죽는다 — 워크트리 폴더를 지운 것만으로 복구 불가가 된다.
            # 되살릴 경로는 `agent-flow abort --worktree <name>`이다.
            if _claim_checkout_is_absent(
                root=root, name=name, registered=tuple(registered_by_path)
            ):
                continue
            # 자리는 있는데 등록만 없는 경우는 다르다. 살아 있는 checkout의 등록이
            # 사라진 것이므로 fail-closed를 유지한다.
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
    return _CheckoutSurvey(
        active=tuple(contexts),
        registered=tuple(sorted(registered_by_path)),
    )


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


def _claim_checkout_is_absent(
    *,
    root: Path,
    name: str,
    registered: tuple[Path, ...],
) -> bool:
    """이 claim이 가리킬 수 있는 자리가 **하나도** 디스크에 없는가.

    자리가 살아 있는데 소유 증명만 없는 경우(관리 자리 밖의 등록 checkout이 자기
    이름으로 런타임 상태를 만든 경우)는 조작 신호다 — 여기서 "없다"고 답하면
    그 fail-closed가 사라진다. 그래서 신뢰 후보뿐 아니라 **같은 이름으로 등록된
    살아 있는 checkout**도 존재로 센다.
    """
    if any(path.name == name and path.exists() for path in registered):
        return False
    try:
        candidates = trusted_checkout_paths(root=root, name=name)
    except (OSError, RuntimeError, ValueError):
        # 후보를 못 세우면 없다고 단정하지 않는다. 판정은 호출자의 fail-closed로.
        return False
    return not any(candidate.exists() for candidate in candidates)


def _active_checkout(
    root: Path,
    *,
    selector: str,
    run_id: str | None = None,
    active: tuple[ActiveCheckout, ...] | None = None,
) -> ActiveCheckout | None:
    """``active``를 주면 그 목록에서 고른다. 열거는 active run 수에 비례해 비싸다.

    열거 한 번이 run마다 `verify_linked_worktree`(git 왕복)를 돈다. 호출부가 이미
    목록을 갖고 있는데 여기서 다시 열거하면 같은 검증을 두 번 치르고, 그 두 배가
    host hook의 실행 시간 제한을 넘기면 세션이 binding되지 못한 채 guard에 막힌다 —
    run이 늘수록 확실히 막히는 쪽으로 기운다.
    """
    matches: list[ActiveCheckout] = []
    path_candidates = [_resolve_path(selector, root)]
    try:
        path_candidates.append(_resolve_path(selector, Path.cwd()))
    except OSError:
        pass
    for context in (_active_checkouts(root) if active is None else active):
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
    return _load_binding_file(
        _binding_path(root, session_id),
        _session_key(session_id),
        root,
        active,
    )


def _load_binding_file(
    path: Path,
    session_key: str,
    root: Path,
    active: tuple[ActiveCheckout, ...],
) -> HostCheckoutBinding | None:
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
        version=recorded_snapshot_version(snapshot_payload.get("version")),
        scope=recorded_snapshot_scope(snapshot_payload.get("scope")),
    )
    # 이 경로는 언제나 부분 sweep으로 찍고 부분 sweep으로 대조한다. 범위가 다른
    # 기록은 형식이 다른 기록과 똑같이 대조하면 항상 차이가 난다.
    if not leader_snapshot.comparable_within(LEADER_SWEEP_TRACKED_ONLY):
        # 낡은 형식은 대조하면 항상 차이가 난다. 그것을 leader 오염으로 보고하면
        # 업그레이드 한 번에 모든 bound 세션의 write가 근거 없이 막힌다. binding이
        # 없는 것으로 읽어서 다음 lifecycle 명령이 새 형식으로 다시 맺게 한다.
        #
        # 대가: 그 사이 세션은 unbound이므로 "한 세션은 한 worktree" 불변식이 한 번
        # 열린다. 다른 checkout으로 갈아타려면 그 checkout의 status 출력을 위조해야
        # 하고(`record_host_checkout_binding`이 payload로 대상을 고른다), leader와
        # 활성 worktree는 unbound 세션에게 계속 닫혀 있다. 형식 전환 1회의 창이다.
        return None
    for context in active:
        if (
            payload.get("checkout") == str(context.checkout)
            and payload.get("worktree") == context.name
            and payload.get("runtime_root") == str(context.runtime_root)
            and payload.get("run_id") == context.run_id
        ):
            return HostCheckoutBinding(
                session_key=session_key,
                checkout=context,
                leader_snapshot=leader_snapshot,
            )
    return None


def _protected_write_roots(
    root: Path,
    survey: _CheckoutSurvey,
    current: ActiveCheckout,
) -> tuple[Path, ...]:
    """bound 세션이 건드리면 안 되는 자리. leader와 **등록된** 형제 checkout 전부다.

    active만 담으면 run이 끝나고 아직 정리되지 않은 형제 worktree — 커밋 안 된
    작업이 그대로 있는 자리 — 가 열린다.
    """
    roots = [root]
    roots.extend(path for path in survey.registered if path != current.checkout)
    roots.extend(
        context.runtime_root
        for context in survey.active
        if context.checkout != current.checkout
    )
    return tuple(roots)


def _target_violation(
    target: str,
    scope: SessionScope,
    *,
    base: Path | None,
    protected: tuple[Path, ...],
) -> str | None:
    """이 경계는 **다른 checkout의 작업**을 지킨다. 세션을 파일시스템에서 격리하는
    장치가 아니다.

    이전에는 bound worktree 밖이면 무조건 거부했다. 그러면 leader가
    `~/Downloads` 같은 폴더 안에 있을 때 프로젝트와 무관한 파일 한 줄
    (`~/Downloads/note.md`, `/tmp/x`)까지 권한 오류처럼 막혀서, 실제로 지켜야 할
    leader·형제 worktree 거부와 구분되지 않았다. 그래서 `protected`에 든 자리만
    막는다.
    """
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
        return scope.reject_relative(target)
    resolved = _resolve_path(target, base or scope.checkout or Path(os.sep))
    if resolved is None:
        return f"write target is not a valid filesystem path: {target!r}"
    if scope.owns(resolved):
        return None
    # glob은 셸이 펼친다. `<leader의 부모>/*`를 리터럴 경로로 보면 보호 root를 품는
    # 판정에서 빠져나간다 — metachar 앞까지의 실제 디렉터리로 판정한다.
    container = _glob_free_prefix(resolved)
    for other_root in protected:
        # 안쪽만 보면 `rm -rf ~/Downloads`처럼 보호 root를 **품는** 경로가 통과한다.
        # 그 한 줄이 leader와 형제 checkout을 통째로 지운다.
        if _is_within(resolved, other_root) or _is_within(other_root, container):
            return scope.reject_target(resolved, other_root)
    return None


_GLOB_METACHARS = frozenset("*?[")


def _glob_free_prefix(path: Path) -> Path:
    parts = path.parts
    for index, part in enumerate(parts):
        if any(character in _GLOB_METACHARS for character in part):
            return Path(*parts[:index]) if index else path
    return path
    return None


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


def _command_path_candidates(command: str, cwd: Path) -> tuple[str, ...]:
    """명령에 리터럴로 등장한 경로 후보. 읽기 경로까지 포함한다.

    파싱 실패는 거부가 아니다. 판정 불가를 전부 차단으로 접으면 인용부호 하나가
    어긋난 명령 때문에 무관한 작업까지 죽는다. 보호 경로 리터럴은 파서 없이도
    ``_command_literal_violation``이 잡으므로, 여기서는 빈 결과로 물러난다.
    """
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return ()
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


_SHELL_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "|&"})
# 되돌릴 수 없는 명령. tripwire는 탐지 전용이라 이 계열은 사후에 잡아도 늦다.
# 목록은 짧게 유지한다 — 여기 이름을 늘리는 것은 "셸이 무엇을 쓰는지 맞히기"로
# 되돌아가는 길이다. 판정 기준은 "무엇을 쓰는가"가 아니라 "복구가 불가능한가"다.
_DESTRUCTIVE_COMMANDS = frozenset({"rm", "rmdir", "shred", "mv"})
_DESTRUCTIVE_GIT_SUBCOMMANDS = frozenset({"checkout", "restore", "clean", "reset"})
# `git worktree`는 두 번째 낱말로 갈린다. `list`/`add`까지 막으면 조회와 채택이
# 죽고, 남의 작업을 지우는 것은 `remove`(`--force`면 미커밋 변경까지)와 경로를
# 옮기는 `move`뿐이다. `prune`은 **이미 사라진** 등록만 정리하므로 되돌릴 작업물이
# 없다 — 그래서 넣지 않는다.
_DESTRUCTIVE_GIT_WORKTREE_VERBS = frozenset({"remove", "move"})
# `find` 자체는 조회다. 되돌릴 수 없게 만드는 것은 이 flag들이고, `-exec`는 뒤에
# 오는 명령이 파괴 계열일 때만이다. 조건 없이 `-exec`를 막으면
# `find . -name '*.kt' -exec grep -l x {} +` 같은 흔한 조회가 함께 죽는다.
_FIND_DESTRUCTIVE_FLAGS = frozenset({"-delete", "-ok"})
_FIND_EXEC_FLAGS = frozenset({"-exec", "-execdir"})
# `chmod`는 파일 하나면 되돌릴 수 있다. 재귀 형태만 원래 mode 조합을 잃는다.
# `-r`은 포함하지 않는다 — chmod에서 그것은 재귀가 아니라 read 권한 제거다.
_CHMOD_RECURSIVE_FLAGS = frozenset({"-R", "--recursive"})
# `git -C <path> checkout`처럼 값을 받는 flag를 건너뛰지 않으면 그 값이 서브명령으로
# 읽혀 판정이 빗나간다.
_GIT_VALUE_FLAGS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--exec-path", "--namespace"}
)
# 실제 명령 앞에 붙어 이름을 가리는 wrapper. 벗기지 않으면 `env rm -rf .`가
# 파괴 목록에 걸리지 않는다.
_COMMAND_NAME_WRAPPERS = frozenset({"env", "command", "nohup", "nice", "stdbuf", "time"})
_ENV_ASSIGNMENT_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _unwrap_command_argv(argv: list[str]) -> list[str]:
    unwrapped = list(argv)
    while unwrapped:
        head = unwrapped[0]
        if _ENV_ASSIGNMENT_TOKEN.match(head):
            unwrapped = unwrapped[1:]
            continue
        if os.path.basename(head).lower() in _COMMAND_NAME_WRAPPERS:
            unwrapped = [
                token
                for token in unwrapped[1:]
                if not token.startswith("-")
            ]
            continue
        return unwrapped
    return unwrapped


def _shell_tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError as exc:
        raise HostWriteBoundaryError("cannot parse shell command safely") from exc


def _shell_segments(command: str) -> tuple[tuple[str, ...], ...]:
    """구분자로 나눈 argv 목록. 파싱 실패는 빈 결과다.

    파싱 실패를 거부로 접지 않는 이유는 ``_command_path_candidates``와 같다 —
    보호 경로 리터럴은 파서 없이 잡히고, 판정 불가를 차단으로 바꾸면 무관한
    명령까지 죽는다.
    """
    try:
        tokens = _shell_tokens(command)
    except HostWriteBoundaryError:
        return ()
    segments: list[tuple[str, ...]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            if current:
                segments.append(tuple(current))
            current = []
            continue
        current.append(token)
    if current:
        segments.append(tuple(current))
    return tuple(segments)


def _destructive_segments(command: str) -> tuple[tuple[str, ...], ...]:
    return tuple(
        segment for segment in _shell_segments(command) if _is_destructive(segment)
    )


def _is_destructive(segment: tuple[str, ...]) -> bool:
    """되돌릴 수 없는 형태인가.

    이름만으로 갈리는 것은 `_DESTRUCTIVE_COMMANDS`뿐이다. 나머지 셋은 같은 이름이
    조회로도 쓰이므로 flag를 봐야 한다 — 이름만 보고 막으면 `git worktree list`,
    `find -name`, `chmod +x` 같은 정상 사용이 함께 죽는다. 이 세 개가 조건부인
    이유는 정확히 그것이고, 목록을 늘릴 때도 같은 질문을 통과해야 한다:
    **이 형태는 되돌릴 수 없는가.**
    """
    argv = _unwrap_command_argv(list(segment))
    if not argv:
        return False
    name = os.path.basename(argv[0]).lower()
    if name in _DESTRUCTIVE_COMMANDS:
        return True
    if name == "chmod":
        return any(token in _CHMOD_RECURSIVE_FLAGS for token in argv[1:])
    if name == "find":
        return _find_is_destructive(argv[1:])
    if name == "git":
        return _git_is_destructive(argv[1:])
    return False


def _find_is_destructive(arguments: tuple[str, ...] | list[str]) -> bool:
    for index, token in enumerate(arguments):
        if token in _FIND_DESTRUCTIVE_FLAGS:
            return True
        if token in _FIND_EXEC_FLAGS and index + 1 < len(arguments):
            if os.path.basename(arguments[index + 1]).lower() in _DESTRUCTIVE_COMMANDS:
                return True
    return False


def _git_is_destructive(arguments: tuple[str, ...] | list[str]) -> bool:
    verbs: list[str] = []
    index = 0
    while index < len(arguments) and len(verbs) < 2:
        token = arguments[index]
        if token in _GIT_VALUE_FLAGS:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        verbs.append(token.lower())
        index += 1
    if not verbs:
        return False
    if verbs[0] == "worktree":
        return len(verbs) > 1 and verbs[1] in _DESTRUCTIVE_GIT_WORKTREE_VERBS
    return verbs[0] in _DESTRUCTIVE_GIT_SUBCOMMANDS


def _destructive_violation(
    command: str,
    *,
    scope: SessionScope,
    protected: tuple[Path, ...],
    cwd: Path | None,
) -> str | None:
    """파괴 명령이 보호 경로에 닿을 수 있는가.

    대상은 리터럴 피연산자, 피연산자가 없으면 **cwd 자신**이다. `git clean -fd`와
    `git reset --hard`는 경로를 적지 않고도 현재 디렉터리를 되돌린다.

    보호 경로를 **품는** 경로도 거부한다(`rm -rf <leader의 부모>`). 이 포함 검사를
    읽기 명령에까지 적용하면 `ls ~/Downloads`가 막히므로, 파괴 목록 안에서만 한다.

    cwd를 증명하지 못하면 이 계열만 거부한다. 상대 경로를 어디에 붙일지 모르는
    상태에서 되돌릴 수 없는 명령을 통과시키면, 그 한 줄의 대가가 복구 불가다.
    """
    segments = _destructive_segments(command)
    if not segments:
        return None
    if cwd is None:
        return scope.reject_unprovable("destructive command")
    for segment in segments:
        for target in _destructive_targets(segment, cwd):
            for other_root in protected:
                if _is_within(target, other_root) or _is_within(other_root, target):
                    return scope.reject_destructive(segment[0], target, other_root)
    return None


def _destructive_targets(segment: tuple[str, ...], cwd: Path) -> tuple[Path, ...]:
    targets: list[Path] = []
    for token in _unwrap_command_argv(list(segment))[1:]:
        if token.startswith("-") or not token.strip(_SHELL_PUNCTUATION):
            continue
        resolved = _resolve_path(token, cwd)
        if resolved is not None:
            # glob은 셸이 펼친다. metachar 앞까지의 실제 디렉터리로 판정한다.
            targets.append(_glob_free_prefix(resolved))
    return tuple(targets) if targets else (cwd,)



def _command_literal_violation(
    command: str,
    *,
    scope: SessionScope,
    protected: tuple[Path, ...],
) -> str | None:
    """보호 경로가 명령 텍스트에 리터럴로 나타나는가.

    파서를 쓰지 않는다. 인용, 리다이렉션, wrapper, 어떤 문법으로 감싸도 경로
    문자열 자체는 명령에 남기 때문이다. 그래서 이것이 셸 쪽의 주 판정이고, 토큰
    기반 검사는 상대 경로를 풀기 위한 보조다.
    """
    allowed = tuple(
        sorted(
            {
                str(path)
                for path in (scope.checkout, scope.runtime_root)
                if path is not None
            },
            key=len,
            reverse=True,
        )
    )
    for prefix in sorted({str(path) for path in protected}, key=len, reverse=True):
        start = 0
        while True:
            index = command.find(prefix, start)
            if index < 0:
                break
            suffix = command[index + len(prefix) : index + len(prefix) + 1]
            boundary = not suffix or suffix in {"/", "\\", "'", '"', " ", "\t", ":", ")", "]", "}"}
            if boundary and not any(command.startswith(path, index) for path in allowed):
                return scope.reject_reference(prefix, Path(prefix))
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
    structured_runs: list[str] = []
    structured_next_commands: list[str] = []
    decoder = json.JSONDecoder()
    for text in _output_strings(payload):
        for match in _STATUS_JSON.finditer(text):
            try:
                value, _ = decoder.raw_decode(text[match.end() :])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                found.append(value)
        structured_runs.extend(
            match.group(1).strip() for match in _STATUS_RUN.finditer(text)
        )
        structured_next_commands.extend(
            match.group(1).strip() for match in _STATUS_NEXT_COMMAND.finditer(text)
        )
    if found:
        return found[-1]
    if not structured_runs or not structured_next_commands:
        return None
    return {
        "run": structured_runs[-1],
        "next_command": structured_next_commands[-1],
    }


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


def _agent_flow_arguments(
    command: str,
    *,
    root: Path | None = None,
    cwd: Path | None = None,
) -> tuple[str, ...] | None:
    argv = _direct_shell_argv(command)
    if not argv:
        return None
    if argv[0] in {"agent-flow", "agent-flow-kit"}:
        return argv[1:]
    if root is not None and _is_installed_cli(argv[0], root=root, cwd=cwd):
        return argv[1:]
    return None


def _is_installed_cli(token: str, *, root: Path, cwd: Path | None) -> bool:
    """이 argv[0]이 **이 프로젝트에 설치된** CLI 실체인가.

    이름만 인정하면 PATH에 없는 환경에서 복구 경로가 사라진다. 실제로 그 상태에
    갇힌 세션이 있었다 — 활성 run 때문에 모든 write가 막혔는데, 유일한 해제 명령이
    PATH에 없어서 경로로 부르면 거부됐다. 경계의 복구 명령이 그 경계 뒤에 있으면
    그건 경계가 아니라 교착이다.

    그래서 이름 대신 **설치 산출물과의 경로 동일성**으로 판정한다. 이름만 흉내 낸
    실행 파일(`node /tmp/agent-flow-kit.mjs`, `/tmp/agent-flow`)은 통과하지 못하고,
    이 shim 자체는 leader 안이라 경계가 다른 세션의 수정을 막는다.
    """
    if "/" not in token:
        return False
    installed = root / ".agent-flow" / "bin" / "agent-flow"
    try:
        info = installed.lstat()
    except OSError:
        return False
    if installed.is_symlink() or not stat.S_ISREG(info.st_mode):
        return False
    resolved = _resolve_path(token, cwd or root)
    return resolved is not None and resolved == real_path(installed)


def _is_lifecycle_command(
    command: str,
    *,
    root: Path | None = None,
    cwd: Path | None = None,
) -> bool:
    """이 명령이 워커가 **자기 런을 진행시키는** 호출인가.

    면제 대상을 이 네 개로 좁혀 둔다. 서브커맨드 이름을 안 보고 agent-flow 호출
    전체를 면제하면, 면제된 경로가 R1 리터럴 검사·R2 파괴 목록·leader tripwire를
    한꺼번에 건너뛴다. 그 상태에서 `eval --judge-command`는 임의 argv를 실행하고,
    `worktree remove --name`은 형제 checkout을 지우고, `record-stage --run-dir`은
    남의 런 산출물을 덮어쓴다 — 셋 다 실행으로 확인됐다. ``_lifecycle_violation``이
    검증하는 인자는 `--root`/`--worktree`뿐이라 그 축들을 대신 막지 못한다.

    이 목록이 CLI보다 뒤처져 새 명령을 가둘 위험은 남는다. 그것을 목록 확장으로
    풀면 위 우회가 되살아나므로, 확장이 필요해지면 먼저
    ``_lifecycle_violation``에 경로형 인자 검증을 세워야 한다.
    """
    arguments = _agent_flow_arguments(command, root=root, cwd=cwd)
    if not arguments:
        return False
    if arguments[0] == "run" and len(arguments) > 1 and arguments[1] in {
        "start",
        "status",
    }:
        return True
    return arguments[0] in _LIFECYCLE_COMMANDS


def _lifecycle_operation(
    command: str,
    *,
    root: Path | None = None,
    cwd: Path | None = None,
) -> str | None:
    arguments = _agent_flow_arguments(command, root=root, cwd=cwd)
    if not arguments:
        return None
    if arguments[0] == "run" and len(arguments) > 1 and arguments[1] in {
        "start",
        "status",
    }:
        return arguments[1]
    return arguments[0]


def _lifecycle_root(
    command: str,
    default: Path,
    *,
    root: Path | None = None,
    cwd: Path | None = None,
) -> Path:
    arguments = _agent_flow_arguments(command, root=root, cwd=cwd) or ()
    for index, token in enumerate(arguments):
        if token == "--root" and index + 1 < len(arguments):
            return real_path(Path(arguments[index + 1]).expanduser())
        if token.startswith("--root="):
            return real_path(Path(token.split("=", 1)[1]).expanduser())
    return real_path(default)


def _worktree_selector(
    command: str,
    *,
    root: Path | None = None,
    cwd: Path | None = None,
) -> str | None:
    arguments = _agent_flow_arguments(command, root=root, cwd=cwd)
    if arguments is None:
        return None
    for index, token in enumerate(arguments):
        if token == "--worktree" and index + 1 < len(arguments):
            return arguments[index + 1]
        if token.startswith("--worktree="):
            return token.split("=", 1)[1]
    return None


def _binding_dir(root: Path) -> Path:
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
    return current


def _binding_path(root: Path, session_id: str) -> Path:
    return _binding_dir(root) / f"{_session_key(session_id)}.json"


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
