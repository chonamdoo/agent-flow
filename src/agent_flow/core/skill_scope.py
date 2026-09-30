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

from typing import Any, NamedTuple, Sequence

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


def scope_document_ids(meta: dict[str, Any], phase_id: str) -> tuple[str, ...]:
    record = _record(meta, phase_id)
    if record is None:
        return ()
    values = record.get("document_ids", [])
    if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
        raise ValueError("invalid phase document delivery scope")
    return tuple(values)


def reviewer_document_ids(meta: dict[str, Any], phase_id: str, provider: str) -> tuple[str, ...]:
    record = _record(meta, phase_id)
    if record is None:
        return ()
    providers = record.get("reviewer_document_ids", {})
    if not isinstance(providers, dict):
        raise ValueError("invalid reviewer document delivery scope")
    values = providers.get(provider, [])
    if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
        raise ValueError("invalid reviewer document delivery identities")
    return tuple(values)


class ReviewerDelivery(NamedTuple):
    """multi-review controller의 skill 증거. 둘 다 provider 합집합이다.

    `delivered`: 성공한 reviewer 프롬프트에 runner가 실어 보낸 required skill 이름.
    `deliverable`: 이 phase에서 돌린 reviewer host가 해석할 수 있었던 required skill 이름.
    reviewer를 한 번도 돌리지 않았으면 None이다. 여기 없는 이름(controller host에만 있는
    skill)은 어떤 reviewer에게도 보낼 수 없으므로 재실행을 요구해도 풀리지 않는다.
    """

    delivered: frozenset[str]
    deliverable: frozenset[str] | None


def _provider_names(record: dict[str, Any], key: str) -> frozenset[str] | None:
    providers = record.get(key)
    if providers is None:
        return None
    if not isinstance(providers, dict):
        raise ValueError(f"invalid reviewer skill scope: {key}")
    names: set[str] = set()
    for values in providers.values():
        if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
            raise ValueError(f"invalid reviewer skill names: {key}")
        names.update(values)
    return frozenset(names)


def reviewer_delivery(meta: dict[str, Any], phase_id: str) -> ReviewerDelivery:
    """이 phase에서 runner가 독립 reviewer에게 보낸 skill 기록.

    multi-review controller는 규범을 직접 읽지 않으므로 "적용했다"는 자기신고 대신 이 기록이
    증거다. 이름으로 비교한다 — 문서 identity는 host별 route를 담아 controller 해석과 맞지 않는다.
    """
    record = _record(meta, phase_id)
    if record is None:
        return ReviewerDelivery(frozenset(), None)
    # 두 기록 모두 기록한 그 scope revision에만 유효하다. scope가 자라거나(새 이름) 같은 이름의
    # 문서 본문이 바뀌면(새 identity) revision이 오른다. 옛 전달 기록으로 통과시키면 새 규범을
    # 아무 reviewer도 보지 않은 채 적용한 것으로 남고, 옛 해석 목록에 새 이름이 없다는 것을
    # "어떤 reviewer host도 해석 못 한다"로 읽으면 재전달 요구가 사라진다.
    revision = scope_revision(meta, phase_id)
    delivered = (
        _provider_names(record, "reviewer_skill_names") or frozenset()
        if record.get("reviewer_skill_revision") == revision else frozenset()
    )
    return ReviewerDelivery(
        delivered,
        _provider_names(record, "reviewer_deliverable_names")
        if record.get("reviewer_deliverable_revision") == revision else None,
    )


def _reviewer_record(meta: dict[str, Any], phase_id: str) -> dict[str, Any]:
    if _record(meta, phase_id) is None:
        merge_scope(meta, phase_id, ())
    record = _record(meta, phase_id)
    assert record is not None
    return record


def record_reviewer_deliverable(
    meta: dict[str, Any], phase_id: str, provider: str, skill_names: Sequence[str],
) -> None:
    """reviewer job의 성패와 무관하게, 그 host가 해석한 required 이름을 현재 scope revision에 남긴다."""
    record = _reviewer_record(meta, phase_id)
    revision = scope_revision(meta, phase_id)
    if record.get("reviewer_deliverable_revision") != revision:
        record["reviewer_deliverable_names"] = {}
        record["reviewer_deliverable_revision"] = revision
    deliverable = record["reviewer_deliverable_names"]
    deliverable[provider] = sorted(set(deliverable.get(provider, ())) | set(skill_names))


def record_reviewer_documents(
    meta: dict[str, Any], phase_id: str, provider: str, identities: Sequence[str],
    *, skill_names: Sequence[str],
) -> None:
    previous = reviewer_document_ids(meta, phase_id, provider)
    record = _reviewer_record(meta, phase_id)
    providers = record.setdefault("reviewer_document_ids", {})
    providers[provider] = sorted(set(previous) | set(identities))
    revision = scope_revision(meta, phase_id)
    if record.get("reviewer_skill_revision") != revision:
        record["reviewer_skill_names"] = {}
        record["reviewer_skill_revision"] = revision
    delivered = record["reviewer_skill_names"]
    delivered[provider] = sorted(set(delivered.get(provider, ())) | set(skill_names))


def scope_revision(meta: dict[str, Any], phase_id: str) -> int:
    record = _record(meta, phase_id)
    if record is None:
        return 0
    revision = record.get("revision")
    return revision if isinstance(revision, int) and revision > 0 else 0


def merge_scope(
    meta: dict[str, Any], phase_id: str, names: Sequence[str],
    *, document_ids: Sequence[str] | None = None,
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
        if document_ids is not None:
            meta[SCOPE_KEY]["document_ids"] = sorted(set(document_ids))
        return ()
    known = set(scope_names(meta, phase_id))
    added = tuple(name for name in incoming if name not in known)
    previous_documents = set(scope_document_ids(meta, phase_id))
    incoming_documents = set(document_ids or ())
    new_documents = incoming_documents - previous_documents if "document_ids" in record else set()
    if document_ids is not None:
        record["document_ids"] = sorted(previous_documents | incoming_documents)
    if not added and not new_documents:
        return ()
    record["names"] = sorted(known | set(added))
    record["revision"] = scope_revision(meta, phase_id) + 1
    return (*added, *(f"document:{identity}" for identity in sorted(new_documents)))
