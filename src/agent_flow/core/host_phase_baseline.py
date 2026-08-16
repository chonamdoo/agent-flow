"""host phase의 leader 기준선 **레코드**를 읽고 쓴다.

leader tripwire의 관측 primitive는 `core/worktree_isolation.py`가, sweep 범위
해석은 `core/leader_tripwire.py`가 가진다. 여기 있는 것은 그 사이의 세 번째 층이다:
run meta 안에 기준선을 어떤 필드로 남기고, 어떤 조건에서 그것을 신뢰하고, 언제
버리고 다시 찍는가.

runner에서 떼어낸 이유는 권한이다. runner는 phase를 전진시키는 판정을 갖고,
이 층은 "그 판정을 시작해도 되는가"를 갖는다. 한 파일에 두면 phase 라우팅을 고치는
변경이 격리 검증 코드를 함께 흔든다.

**응답 정책은 여기 없다.** drift를 발견했을 때 무엇을 출력하고 어떤 해제 명령을
광고하는지는 호출자(runner)가 정하고, 이 모듈은 `assert_unchanged` 콜러블로 그것을
받는다. 그래서 검사 순서(승인 → 대조 → 재캡처)는 여기 한 곳에만 있고, 그 순서가
바뀌면 함께 깨질 자리도 한 곳이다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agent_flow.artifact import read_meta, write_meta
from agent_flow.core.worktree_isolation import (
    HOST_PHASE_LEADER_BASELINE_KEY,
    LEADER_SNAPSHOT_VERSION,
    STATUS_DRIFT_KIND,
    LeaderDrift,
    LeaderSnapshot,
    WorktreeIsolationError,
    capture_leader_snapshot,
    leader_drift_paths,
    leader_snapshot_payload,
    leader_sweep_includes_ignored,
    real_path,
    recorded_snapshot_scope,
    recorded_snapshot_version,
)

# 키 이름은 baseline을 쓰는 쪽과 읽는 쪽이 공유해야 한다.
BASELINE_KEY = HOST_PHASE_LEADER_BASELINE_KEY
# baseline **레코드 자체**의 필드 구성 버전. 그 안에 담기는 스냅샷의 형식 버전은
# 별개 축이고 `LeaderSnapshot.version`이 들고 있다. 이 값은 `run_id`/`phase_id`/
# `leader_root`/`snapshot`의 의미가 바뀔 때만 올린다.
#
# v2는 v1의 `phase_index`를 뺐다. cursor의 index는 phase 이름에서 나오는 파생값이라
# 승인된 drift가 정의를 재배치하면 이름이 그대로여도 움직인다. 그 값을 기록에 남겨
# 등호로 대조하면 baseline이 두 번째 권위가 되어 정당한 재개를 죽인다.
BASELINE_RECORD_VERSION = 2
# 사용자가 확인한 leader 변경을 담는 자리. baseline과 **다른 키**여야 한다 —
# baseline 레코드는 필드 집합을 등호로 검사하므로 여기에 얹으면 malformed가 된다.
DRIFT_KEY = "host_phase_leader_drift"
DRIFT_ACKNOWLEDGEMENTS_KEY = "leader_drift_acknowledgements"
DRIFT_RECORD_VERSION = 1

_BASELINE_FIELDS = frozenset({"version", "run_id", "phase_id", "leader_root", "snapshot"})
_SNAPSHOT_REQUIRED_FIELDS = frozenset({"head", "branch", "status", "armed"})
_SNAPSHOT_KNOWN_FIELDS = _SNAPSHOT_REQUIRED_FIELDS | {"version", "scope"}

AssertUnchanged = Callable[..., None]


@dataclass(frozen=True)
class BaselineScope:
    """한 run의 기준선 판정에 필요한 값들. runner의 self에서 한 번 뽑아 온다."""

    run_dir: Path
    worker_root: Path
    sweep_scope: str
    include_ignored: bool
    accept_drift: bool

    @property
    def run_id(self) -> str:
        return self.run_dir.name


def verify_baseline(
    scope: BaselineScope,
    meta: dict[str, Any],
    *,
    phase_id: str,
    leader_root: Path | None,
    assert_unchanged: AssertUnchanged,
) -> LeaderSnapshot | None:
    """기록된 기준선이 지금도 유효한가. 없거나 버렸으면 ``None``."""
    raw = meta.get(BASELINE_KEY)
    if raw is None:
        return None
    if leader_root is None:
        raise WorktreeIsolationError(
            "durable host-phase leader baseline exists without a linked worktree"
        )
    if not isinstance(raw, dict):
        raise WorktreeIsolationError("durable host-phase leader baseline is malformed")
    expected_root = str(real_path(leader_root))
    # 버전을 필드 집합보다 **먼저** 본다. 순서가 반대면 예전 형식의 기록은
    # 필드가 다르다는 이유로 malformed 하드 raise에 걸려, 아래 재캡처 경로에
    # 도달하지 못한 채 업그레이드를 걸친 run이 재개 불가가 된다.
    record_version = recorded_snapshot_version(raw.get("version"))
    if record_version != BASELINE_RECORD_VERSION:
        # 레코드 필드 구성이 다르면 그 안의 값들을 현재 의미로 읽을 수 없다.
        # 스냅샷 형식과 같은 처리를 한다 — 하드 raise로 막으면 업그레이드를
        # 걸친 run이 재개 불가가 되고, 그건 이 경로가 없애려던 교착이다.
        migrate_stale_baseline(
            scope,
            meta,
            recorded=f"record format v{record_version}",
            expected=f"v{BASELINE_RECORD_VERSION}",
        )
        return None
    if set(raw) != set(_BASELINE_FIELDS):
        raise WorktreeIsolationError("durable host-phase leader baseline is malformed")
    # 동일성은 run·phase 이름·leader 체크아웃이 진다. index는 여기 없다 —
    # 승인된 drift가 정의를 재배치하면 같은 phase가 다른 자리에 앉고, index를
    # 대조하면 그 정당한 재개가 오염 보고로 죽는다.
    if (
        raw.get("run_id") != scope.run_id
        or raw.get("phase_id") != phase_id
        or raw.get("leader_root") != expected_root
    ):
        raise WorktreeIsolationError(
            "durable host-phase leader baseline does not match the current "
            "run, phase, or leader checkout"
        )
    snapshot = _snapshot_from_record(raw.get("snapshot"))
    if not snapshot.comparable:
        migrate_stale_baseline(
            scope,
            meta,
            recorded=f"snapshot format v{snapshot.version}",
            expected=f"v{LEADER_SNAPSHOT_VERSION}",
        )
        return None
    if not snapshot.comparable_within(scope.sweep_scope):
        # profile이 sweep 범위를 바꿨다. 범위가 다른 두 기록은 leader를 아무도
        # 건드리지 않아도 항상 다르므로 새 범위로 다시 찍어야 한다.
        #
        # 다만 형식 전환과 달리 여기서는 **기록된 범위로 먼저 대조한다**. 형식은
        # kit 업그레이드가 바꾸지만 범위는 파일 한 줄이 바꾸고, 그 파일은 워커가
        # 닿을 수 있는 자리에 있다. 대조 없이 버리면 범위를 좁히는 그 phase 하나가
        # 검사 없는 통과가 되고, 그 창이 정확히 워커가 노릴 자리다.
        #
        # 승인 경로를 대조보다 **앞에** 둔다. 뒤에 두면 도달하지 못한다 — 대조가
        # raise하면 그 아래가 실행되지 않고 baseline도 그대로 남아, 다음 재개가
        # 같은 지점에서 다시 막힌다. 그러면 이 knob이 존재하는 이유인 "leader가
        # 계속 움직이는 상황"에서 광고된 해제 명령이 무효가 된다.
        recorded_include_ignored = leader_sweep_includes_ignored(snapshot.scope)
        accepted = accept_recorded_drift(
            scope,
            meta,
            raw=raw,
            leader_root=leader_root,
            include_ignored=recorded_include_ignored,
        )
        if accepted is None:
            assert_unchanged(
                leader_root, snapshot, include_ignored=recorded_include_ignored
            )
        migrate_stale_baseline(
            scope,
            meta,
            recorded=f"sweep scope {snapshot.scope}",
            expected=scope.sweep_scope,
        )
        return None
    accepted = accept_recorded_drift(scope, meta, raw=raw, leader_root=leader_root)
    if accepted is not None:
        return accepted
    assert_unchanged(leader_root, snapshot)
    if meta.pop(DRIFT_KEY, None) is not None:
        # leader가 스스로 돌아왔다. 남은 보고 기록은 나중에 같은 상태가 우연히
        # 재현될 때 승인을 대신 서 줄 수 있으므로 여기서 버린다.
        write_meta(scope.run_dir, meta)
    return snapshot


def accept_recorded_drift(
    scope: BaselineScope,
    meta: dict[str, Any],
    *,
    raw: dict[str, Any],
    leader_root: Path,
    include_ignored: bool | None = None,
) -> LeaderSnapshot | None:
    """사용자가 확인한 leader 변경만 새 기준선으로 굳힌다. 아니면 ``None``.

    경로를 비교에서 빼지 않는 이유: ignored 경로에는 빌드 산출물만 있는 게
    아니라 host가 **실행하는** 것도 있다(`.agent-flow/scripts/hooks/`,
    `.claude/hooks/`, `.venv/bin`). 예외 목록을 만들면 그 자리의 변조가 영원히
    조용해진다. 그래서 탐지 범위는 그대로 두고 응답만 바꾼다 — 무엇이 바뀌었는지
    전부 보여주고, 사람이 받아들인 그 상태에서 다시 시작한다.

    승인은 **보여준 그 상태**에만 붙는다. 보고 이후 leader가 또 움직였으면
    기록을 버리고 통과시키지 않는다. 그러지 않으면 승인 한 번이 이후의 모든
    변경까지 함께 덮어, 사람이 본 적 없는 오염이 기준선이 된다.
    """
    if not scope.accept_drift:
        return None
    recorded = meta.get(DRIFT_KEY)
    if (
        not isinstance(recorded, dict)
        or recorded.get("version") != DRIFT_RECORD_VERSION
        or recorded.get("run_id") != scope.run_id
        or recorded.get("kind") != STATUS_DRIFT_KIND
        or not isinstance(recorded.get("observed"), dict)
    ):
        return None
    # 보고했던 그 범위로 다시 찍는다. 범위 전환 중이라면 기록은 옛 범위로
    # 만들어졌으므로, 새 범위로 찍어 비교하면 leader가 그대로여도 절대 일치하지
    # 않아 승인이 영원히 성립하지 않는다.
    observed = capture_leader_snapshot(
        leader_root,
        include_ignored=(
            scope.include_ignored if include_ignored is None else include_ignored
        ),
    )
    if leader_snapshot_payload(observed) != recorded["observed"]:
        # 보고한 상태가 아니다. 기록을 버려 다음 검사가 현재 차이를 새로 보고한다.
        meta.pop(DRIFT_KEY, None)
        write_meta(scope.run_dir, meta)
        return None
    paths = [str(path) for path in recorded.get("paths") or ()]
    meta[BASELINE_KEY] = {**raw, "snapshot": leader_snapshot_payload(observed)}
    meta.setdefault(DRIFT_ACKNOWLEDGEMENTS_KEY, []).append(
        {
            "at": datetime.now(timezone.utc).isoformat(),
            "phase_id": raw.get("phase_id"),
            "paths": paths,
        }
    )
    meta.pop(DRIFT_KEY, None)
    write_meta(scope.run_dir, meta)
    print(
        f"  [accepted] leader drift acknowledged ({len(paths)} paths); "
        "re-baselined to the state reported above"
    )
    return observed


def migrate_stale_baseline(
    scope: BaselineScope, meta: dict[str, Any], *, recorded: str, expected: str
) -> None:
    """비교할 수 없는 기록을 버린다. 이 phase가 새 형식으로 다시 찍는다.

    형식이 다른 기록을 그대로 대조하면 **항상** 차이가 나온다 — leader를 아무도
    건드리지 않아도 그렇다. 그 오탐은 진행 중인 run 전부를 막고 근거가 없으므로
    사용자가 풀 방법도 없다. 하드 raise도 같은 결과다(run 재개 불가).
    """
    print(
        f"  [migrate] host-phase leader baseline was recorded in "
        f"{recorded}; re-capturing as {expected}"
    )
    meta.pop(BASELINE_KEY, None)
    write_meta(scope.run_dir, meta)


def record_drift(scope: BaselineScope, drift: LeaderDrift) -> tuple[str, ...]:
    """보고한 상태를 남기고 공개할 경로를 돌려준다.

    승인은 이 기록과 대조해서만 통한다. `reasons`가 아니라 `paths`를 남기는
    이유는 `reasons`가 8개에서 잘리기 때문이다.
    """
    paths = leader_drift_paths(drift)
    meta = read_meta(scope.run_dir)
    meta[DRIFT_KEY] = {
        "version": DRIFT_RECORD_VERSION,
        "run_id": scope.run_id,
        "kind": drift.kind,
        "paths": list(paths),
        "observed": leader_snapshot_payload(drift.after),
    }
    write_meta(scope.run_dir, meta)
    return paths


def persist_baseline(
    scope: BaselineScope,
    *,
    phase_id: str,
    leader_root: Path,
    snapshot: LeaderSnapshot,
) -> None:
    if not snapshot.armed:
        raise WorktreeIsolationError("cannot persist an unarmed host-phase leader baseline")
    meta = read_meta(scope.run_dir)
    if meta.get(BASELINE_KEY) is not None:
        raise WorktreeIsolationError(
            "host-phase leader baseline changed without phase advancement"
        )
    meta[BASELINE_KEY] = {
        "version": BASELINE_RECORD_VERSION,
        "run_id": scope.run_id,
        "phase_id": phase_id,
        "leader_root": str(real_path(leader_root)),
        "snapshot": leader_snapshot_payload(snapshot),
    }
    write_meta(scope.run_dir, meta)


def _snapshot_from_record(raw: object) -> LeaderSnapshot:
    if not isinstance(raw, dict) or not (
        _SNAPSHOT_REQUIRED_FIELDS <= set(raw) <= _SNAPSHOT_KNOWN_FIELDS
    ):
        raise WorktreeIsolationError("durable host-phase leader snapshot is malformed")
    head = raw.get("head")
    branch = raw.get("branch")
    status = raw.get("status")
    armed = raw.get("armed")
    if (
        not isinstance(head, str)
        or not head
        or not isinstance(branch, str)
        or not branch
        or not isinstance(status, str)
        or armed is not True
    ):
        raise WorktreeIsolationError("durable host-phase leader snapshot is incomplete")
    return LeaderSnapshot(
        head=head,
        branch=branch,
        status=status,
        armed=True,
        version=recorded_snapshot_version(raw.get("version")),
        scope=recorded_snapshot_scope(raw.get("scope")),
    )
