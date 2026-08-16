"""이 phase가 이미 보여 준 required skill 목록. drift를 재진입으로 바꾸는 자리다.

프롬프트는 phase 시작에 한 번 렌더되고, 게이트는 phase 끝에 다시 계산한다. 그 사이에
agent가 파일을 만들면 `changed_files`가 자라고 required 집합도 자란다. 그러면 게이트는
**프롬프트가 보여 준 적 없는 skill 이름**을 artifact에 적으라고 요구한다 — agent가
자기 작업으로 자기 기준을 바꾼 뒤 그 기준으로 심사받는 상태다.

그래서 phase마다 "지금까지 보여 준 이름"을 기록한다. 게이트에서 그보다 큰 집합이
나오면 요구하지 않고, 자란 이름을 알리고 같은 phase를 다시 열어 준다(revision+1).
다음 라운드에는 기록이 이미 그 이름을 담고 있으므로 자람이 없고, 요구는 그때 나간다.
자람은 단조롭고 카탈로그가 상한이므로 이 되풀이는 끝난다.

**phase-local이다.** run 전체로 grow-only하게 두면 scope가 정당하게 줄어든 다음 phase
에서도 이전 phase의 이름을 요구하게 되고, 그건 이 모듈이 막으려는 바로 그 상태다.
"""

from __future__ import annotations

from typing import Any, Sequence

SCOPE_KEY = "skill_scope"
# 형식이 바뀐 기록은 비교하지 않고 새로 잡는다. 형식 차이를 자람으로 보고하면
# 진행 중인 run이 근거 없이 한 번 더 막힌다.
SCOPE_RECORD_VERSION = 1


def _record(meta: dict[str, Any], phase_id: str) -> dict[str, Any] | None:
    raw = meta.get(SCOPE_KEY)
    if not isinstance(raw, dict):
        return None
    if raw.get("version") != SCOPE_RECORD_VERSION:
        return None
    if raw.get("phase_id") != phase_id:
        return None
    return raw


def scope_names(meta: dict[str, Any], phase_id: str) -> tuple[str, ...]:
    """이 phase가 보여 준 required 이름. 기록이 없으면 빈 tuple."""
    record = _record(meta, phase_id)
    if record is None:
        return ()
    names = record.get("names")
    if not isinstance(names, list):
        return ()
    return tuple(str(name) for name in names)


def scope_revision(meta: dict[str, Any], phase_id: str) -> int:
    record = _record(meta, phase_id)
    if record is None:
        return 0
    revision = record.get("revision")
    return revision if isinstance(revision, int) and revision > 0 else 0


def merge_scope(
    meta: dict[str, Any], phase_id: str, names: Sequence[str]
) -> tuple[str, ...]:
    """meta를 갱신하고 **새로 늘어난 이름**을 돌려준다.

    첫 기록은 자람이 아니다. 그것까지 자람으로 보고하면 모든 phase가 아무 이유 없이
    한 번씩 막힌다 — 시작할 때의 목록은 프롬프트가 바로 그 목록을 보여 주기 때문이다.
    """
    incoming = tuple(sorted({str(name) for name in names}))
    record = _record(meta, phase_id)
    if record is None:
        meta[SCOPE_KEY] = {
            "version": SCOPE_RECORD_VERSION,
            "phase_id": phase_id,
            "revision": 1,
            "names": list(incoming),
        }
        return ()
    known = set(scope_names(meta, phase_id))
    added = tuple(name for name in incoming if name not in known)
    if not added:
        return ()
    record["names"] = sorted(known | set(added))
    record["revision"] = scope_revision(meta, phase_id) + 1
    return added
