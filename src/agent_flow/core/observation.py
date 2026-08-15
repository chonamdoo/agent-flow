"""Run 하나의 phase 궤적(trace).

`meta.json`은 "지금 어디인가"만 말하고 `transitions.jsonl`은 "어디로 갔는가"만
말한다. 둘 다 "무엇을 넣었고 무엇이 어긋났는가"는 남기지 않아서, 실패한 run을
나중에 재현하거나 eval fixture로 옮길 수가 없다. 이 모듈이 그 한 줄기를 맡는다.

sink는 새로 만들지 않고 `core.context_contract`의 run-local `events.jsonl`을
쓴다. 관측 기록이 두 벌이 되면 어느 쪽이 전부인지 아무도 말할 수 없다.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from agent_flow.core.context_contract import (
    append_context_event,
    offload_tool_output,
    run_relative_path,
)

PHASE_ENTERED = "phase_entered"
PROMPT_RENDERED = "prompt_rendered"
VALIDATION_FAILED = "validation_failed"
PHASE_EXITED = "phase_exited"

OBSERVATION_KINDS = frozenset(
    {PHASE_ENTERED, PROMPT_RENDERED, VALIDATION_FAILED, PHASE_EXITED}
)

# 중첩 details를 강제 변환할 최대 깊이. 순환 참조는 `json.dumps`에서 ValueError로
# 나오지만, 강제 변환이 먼저 돌기 때문에 여기서 멈추지 않으면 RecursionError가
# 경계를 빠져나간다.
_MAX_DETAIL_DEPTH = 6


@dataclass(frozen=True)
class PhaseObservation:
    """한 사건. payload는 값이 아니라 파일로 간다."""

    kind: str
    phase_id: str
    details: Mapping[str, object] = field(default_factory=dict)
    payload: str | None = None
    payload_name: str | None = None


def record_observation(
    *,
    run_dir: Path,
    observation: PhaseObservation,
) -> None:
    """관측 하나를 run-local trace에 남긴다.

    관측 실패가 run을 멈춰서는 안 된다. 기록은 부수적 증거지 전이 조건이 아니다.
    그래서 경계는 디스크 오류(`OSError`)만이 아니라 **직렬화 오류까지** 닫는다.
    호출자는 `details`에 무엇이든 넣을 수 있고 — `Path`, `set`, dataclass —
    그것들은 `json.dumps`에서 `TypeError`를 낸다. surrogate가 섞인 문자열은
    utf-8 인코딩에서 `UnicodeEncodeError`(=`ValueError`)를 낸다. 어느 쪽이든
    원장을 이미 적은 뒤의 phase 루프를 죽였다.
    """
    if observation.kind not in OBSERVATION_KINDS:
        raise ValueError(f"unknown observation kind: {observation.kind!r}")
    try:
        details: dict[str, object] = {
            "phase": observation.phase_id,
            **{
                _safe_text(str(key)): _json_safe(value)
                for key, value in observation.details.items()
            },
        }
        if observation.payload is not None:
            details.update(
                _payload_reference(
                    run_dir=run_dir,
                    name=observation.payload_name or observation.kind,
                    content=observation.payload,
                )
            )
        append_context_event(
            run_dir=run_dir,
            event=observation.kind,
            details=details,
        )
    except (OSError, TypeError, ValueError):
        return


def _payload_reference(*, run_dir: Path, name: str, content: str) -> dict[str, object]:
    """payload를 content-addressed 파일로 내리고 참조만 돌려준다.

    prompt 전문을 event에 넣으면 trace 파일이 곧 프롬프트 사본이 되어, 이 층이
    없애려는 바로 그 컨텍스트 비용을 다시 만든다.
    """
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError:
        # 인코딩할 수 없는 프롬프트도 증거로는 남아야 한다. 여기서 포기하면
        # 그 phase의 렌더링 결과가 trace에서 통째로 사라진다.
        content = content.encode("utf-8", "replace").decode("utf-8")
        encoded = content.encode("utf-8")
    # 같은 offload를 generic event로 한 번 더 적지 않는다. 아래 참조가 정본이다.
    path = offload_tool_output(
        run_dir=run_dir, name=name, content=content, record_event=False
    )
    return {
        "payload_path": run_relative_path(path, run_dir),
        "payload_sha256": hashlib.sha256(encoded).hexdigest(),
        "payload_bytes": len(encoded),
    }


def _json_safe(value: object, depth: int = 0) -> object:
    """JSON으로 적을 수 있는 값으로 강제한다. 모르는 타입은 문자열이 된다."""
    if isinstance(value, str):
        return _safe_text(value)
    # bool은 int의 하위형이라 먼저 걸러야 숫자 분기로 새지 않는다.
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        # NaN/Infinity는 `json.dumps`가 예외 없이 적지만 JSON이 아니다. 그 줄을
        # 읽는 쪽(kit의 JS)이 파싱에서 죽는다.
        return value if math.isfinite(value) else repr(value)
    if depth >= _MAX_DETAIL_DEPTH:
        return _safe_text(str(value))
    if isinstance(value, Mapping):
        return {
            _safe_text(str(key)): _json_safe(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (set, frozenset)):
        # 집합은 순서가 없다. 정렬하지 않으면 같은 사건이 실행마다 다른 줄이 된다.
        return sorted(_safe_text(str(item)) for item in value)
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth + 1) for item in value]
    return _safe_text(str(value))


def _safe_text(value: str) -> str:
    """utf-8로 인코딩할 수 없는 문자열(짝 없는 surrogate 등)을 대체 문자로 바꾼다."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return value.encode("utf-8", "replace").decode("utf-8")
    return value
