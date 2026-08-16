"""artifact 텍스트 → route key. phase 전진 **판정**은 runner가 갖는다.

runner가 갖는 것은 "어느 route로 갈지"이고, 여기 있는 것은 그 앞단인 "이 문서가
무슨 결과를 말하는가"의 파싱이다. 둘을 한 파일에 두면 gate JSON 한 필드를 고치는
변경이 phase 루프와 같은 자리에서 일어난다.

읽을 수 없는 gate 결과는 실패가 아니라 **판정 없음**(`GATE_MALFORMED`)이다.
`default`로 접으면 fix-loop가 근거 없이 코드를 고치기 시작한다.
"""
from __future__ import annotations

import json
import re

from agent_flow.core.markers import unfenced_markdown_text
from agent_flow.core.phase_workflow import overall_review_route_key
from agent_flow.core.profiles import GATE_PHASE_ALL


def route_key(text: str) -> str:
    lowered = text.lower()
    # gates 결과 JSON은 nested result가 아니라 top-level passed만 route source로 본다.
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("passed"), bool):
        results = payload.get("results")
        if payload["passed"] is True:
            if _gate_results_prove_pass(results):
                return "green"
            return "default"
        return "request-changes"
    checks = (
        "blocked",
        "request-changes",
        "ci-failed",
        "ci_failed",
        "comments",
        "has_comments",
        "skipped",
        "pending",
        "green",
        "approve",
        "merged",
        "closed",
        "error",
    )
    for line in lowered.splitlines():
        match = re.match(r"^(?:verdict|status):\s*([a-z_-]+)\s*$", line)
        if not match:
            continue
        key = match.group(1)
        if key in checks:
            return key
    return "default"


# route key가 아니라 "판정 불가" 표식이다. workflow가 이 key로 갈 target을
# 선언할 수 없게 이름을 route 어휘 밖에 둔다.
GATE_MALFORMED = "malformed-results"


def gate_parse_error(text: str) -> str:
    """왜 못 읽었는지 한 줄로. 사유가 없으면 사용자는 무엇을 고칠지 모른다."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return f"invalid JSON at line {exc.lineno} column {exc.colno}"
    if not isinstance(payload, dict):
        return f"top-level value is {type(payload).__name__}, expected an object"
    return "`passed` is missing or not a boolean"


def gates_route_key(text: str, *, nonce: str = "") -> str:
    # 읽을 수 없는 gate 결과는 "게이트가 실패했다"가 아니라 "판정 자체가 없다"다.
    # 예전처럼 `default`로 접으면 fix-loop가 근거 없이 코드를 고치기 시작하고,
    # 세 라운드를 태운 뒤 상한에 걸려 run이 영구 정지한다.
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return GATE_MALFORMED
    if not isinstance(payload, dict) or not isinstance(payload.get("passed"), bool):
        return GATE_MALFORMED
    # timeout은 "실패"가 아니라 "판정 불가"다. optional 게이트가 상한을 다 쓰고
    # 죽어도 passed 집계는 required만 보므로 green이 된다. 그 구멍을 여기서 닫는다.
    if _gate_results_timed_out(payload.get("results")):
        return "error"
    # 통과 라우팅에만 출처를 요구한다. 실패/차단은 손으로 써도 앞으로 못 가므로
    # 막을 이유가 없고, 막으면 복구 경로만 좁아진다.
    proven = (
        _gate_results_prove_pass(payload.get("results"))
        and _gate_nonce_matches(payload, nonce)
        and _gate_phase_covers_verification(payload)
    )
    status = payload.get("status")
    if isinstance(status, str):
        normalized_status = status.strip().lower().replace("_", "-")
        if payload["passed"] is True and normalized_status in {"green", "approve"}:
            return normalized_status if proven else "default"
        if payload["passed"] is False and normalized_status in {"request-changes", "blocked", "error", "pending"}:
            return normalized_status
    if payload["passed"] is True:
        return "green" if proven else "default"
    return "request-changes"


def _gate_results_timed_out(results: object) -> bool:
    if not isinstance(results, list):
        return False
    return any(
        isinstance(result, dict) and result.get("timed_out") is True
        for result in results
    )


def _gate_nonce_matches(payload: dict[str, object], nonce: str) -> bool:
    """이 run의 `agent-flow gates`가 쓴 파일인가.

    run에 nonce가 없으면(구버전 run, CLI 직접 사용) 대조할 기준이 없으므로
    요구하지 않는다. "없으면 위반"이 아니라 "기록과 다르면 위반"이다.
    """
    if not nonce:
        return True
    produced_by = payload.get("produced_by")
    if not isinstance(produced_by, dict):
        return False
    return produced_by.get("nonce") == nonce


def _gate_phase_covers_verification(payload: dict[str, object]) -> bool:
    """QA gate가 build/test까지 돌렸는가.

    `agent-flow gates`의 기본 phase는 `pre-commit`이고 build/test는 `pre-push`다.
    workflow의 gates phase는 커밋 직전 마지막 검증이므로 둘 다 돌아야 한다.
    `--phase pre-commit`으로 돈 결과는 "전부 통과"처럼 보이지만 실제로는
    build/test가 목록에 오르지도 않은 실행이다.

    `all`만 받는다. `pre-push` 단독은 lint/type/architecture-lint를 빼먹고, 부분
    phase를 조합으로 인정하기 시작하면 "무엇이 돌았는가"를 결과 목록에서 다시
    역산해야 한다. 번들 프로필에 post-merge gate는 아직 없다 — 생기면 `all`이
    그것까지 커밋 전에 돌리므로 그때 이 규칙을 다시 봐야 한다.

    기록이 없으면(구버전 파일, CLI 직접 사용) 대조할 기준이 없으므로 요구하지
    않는다. nonce와 같은 규칙이다 — "없으면 위반"이 아니라 "기록과 다르면 위반".
    """
    produced_by = payload.get("produced_by")
    if not isinstance(produced_by, dict):
        return True
    recorded = produced_by.get("gate_phase")
    if not isinstance(recorded, str) or not recorded:
        return True
    return recorded == GATE_PHASE_ALL


def recorded_gate_phase(text: str) -> str:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    produced_by = payload.get("produced_by")
    if not isinstance(produced_by, dict):
        return ""
    recorded = produced_by.get("gate_phase")
    return recorded if isinstance(recorded, str) else ""


def multi_review_route_key(text: str, phase_id: str = "") -> str:
    verdicts = _independent_reviewer_verdicts(text)
    overall = overall_review_route_key(text)
    if not verdicts:
        return "missing-reviewer"
    if overall == "invalid-verdict":
        return "invalid-verdict"
    if "request-changes" in verdicts.values() or overall == "request-changes":
        return "request-changes"
    if len(verdicts) < 2:
        return "insufficient-reviewers"
    if overall == "default":
        return "default"
    has_subagent = _has_subagent_reviewer(text)
    if overall == "approve" and has_subagent and len(verdicts) >= 2:
        return "approve"
    return "invalid-verdict"


def _gate_results_prove_pass(results: object) -> bool:
    if not isinstance(results, list) or not results:
        return False
    required_seen = False
    for result in results:
        if not isinstance(result, dict):
            return False
        if result.get("required") is False:
            continue
        required_seen = True
        command = result.get("command")
        if not isinstance(command, str) or not command.strip():
            return False
        if not _gate_result_has_evidence(result):
            return False
        if not (result.get("passed") is True or result.get("status") in {"pass", "ok"}):
            return False
    return required_seen


def _gate_result_has_evidence(result: dict[str, object]) -> bool:
    for key in ("output", "stdout", "stderr", "artifact", "path"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return True
    for key in ("exit_code", "exitCode"):
        value = result.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value == 0:
            return True
    return False




def _independent_reviewer_verdict_count(text: str) -> int:
    return len(_independent_reviewer_verdicts(text))


def _independent_reviewer_verdicts(text: str) -> dict[str, str]:
    reviewers: dict[str, dict[str, object]] = {}
    current_reviewer: str | None = None
    for line in unfenced_markdown_text(text).splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if not stripped:
            continue
        if stripped.startswith("#"):
            if re.match(r"^##\s*(?:overall|final)(?:\s+verdict)?\s*$", lowered):
                current_reviewer = None
                continue
            heading = re.match(r"^##\s*reviewer\s+(.+)$", lowered)
            if heading:
                key = _normalized_reviewer_heading_id(heading.group(1))
                current_reviewer = key or None
            continue
        source_match = re.match(
            r"^(reviewer[-_ ]?[a-z0-9-]+)\s+reviewer[-_ ]?source:\s*(.+)$",
            lowered,
        )
        if source_match:
            key = _normalized_reviewer_id(source_match.group(1))
            if key:
                state = reviewers.setdefault(key, {"has_source": False, "subagent": False, "verdict": None})
                state["has_source"] = True
                if _is_subagent_source(source_match.group(2)):
                    state["subagent"] = True
            continue
        if current_reviewer is not None and _line_marks_subagent_source(lowered):
            state = reviewers.setdefault(current_reviewer, {"has_source": False, "subagent": False, "verdict": None})
            state["has_source"] = True
            state["subagent"] = True
            continue
        if current_reviewer is not None and _line_marks_non_subagent_source(lowered):
            reviewers.setdefault(current_reviewer, {"has_source": True, "subagent": False, "verdict": None})
            continue
        if "verdict:" not in lowered:
            continue
        verdict_match = re.match(r"^(.*?)verdict:\s*(approve|request-changes)\s*$", stripped)
        if not verdict_match:
            continue
        prefix = verdict_match.group(1).strip(" -").lower()
        verdict = verdict_match.group(2)
        if prefix in {"overall", "overall verdict", "final", "final verdict"}:
            continue
        if prefix:
            key = _normalized_reviewer_id(prefix)
            if key:
                reviewers.setdefault(key, {"has_source": False, "subagent": False, "verdict": None})["verdict"] = verdict
        elif current_reviewer is not None:
            reviewers.setdefault(current_reviewer, {"has_source": False, "subagent": False, "verdict": None})["verdict"] = verdict
    return {
        reviewer: str(state["verdict"])
        for reviewer, state in reviewers.items()
        if state["verdict"] and state["subagent"]
    }


def _line_marks_subagent_source(value: str) -> bool:
    source_match = re.search(r"reviewer[-_ ]?source\s*:\s*(.+)$", value)
    return bool(source_match and _is_subagent_source(source_match.group(1)))


def _line_marks_non_subagent_source(value: str) -> bool:
    source_match = re.search(r"reviewer[-_ ]?source\s*:\s*(.+)$", value)
    return bool(source_match and not _is_subagent_source(source_match.group(1)))


def _has_subagent_reviewer(text: str) -> bool:
    return any(
        _line_marks_subagent_source(line.strip().lower())
        for line in unfenced_markdown_text(text).splitlines()
    )


def _is_subagent_source(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return normalized in {
        "sub agent",
        "subagent",
        "host sub agent",
        "host subagent",
        "active host sub agent",
        "active host subagent",
    }


def _reviewer_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return normalized or value


def _normalized_reviewer_id(value: str) -> str:
    # 섹션 라벨과 종합 verdict는 독립 reviewer id로 세지 않는다.
    key = _reviewer_key(value)
    key = re.sub(r"^reviewer\b", "", key).strip()
    generic_labels = {
        "verdict",
        "verdicts",
        "overall",
        "final",
        "summary",
        "review",
        "reviews",
        "feedback",
        "report",
        "reports",
        "assessment",
        "assessments",
        "analysis",
        "analyses",
        "decision",
        "decisions",
        "conclusion",
        "conclusions",
        "status",
        "statuses",
        "approval",
        "approvals",
        "note",
        "notes",
        "finding",
        "findings",
        "comment",
        "comments",
        "output",
        "outputs",
        "result",
        "results",
        "scope",
        "check",
        "checks",
        "checklist",
        "details",
        "detail",
    }
    if not key or any(part in generic_labels for part in key.split()):
        return ""
    return key


def _normalized_reviewer_heading_id(value: str) -> str:
    # Reviewer heading은 1-2 단어 id(claude, agent 1 등)만 독립 id로 인정한다.
    # 긴 서술형 heading은 reviewer가 아니라 prose일 가능성이 높아 제외한다.
    key = _normalized_reviewer_id(value)
    if re.fullmatch(r"[a-z0-9]+(?: [a-z0-9]+)?", key):
        return key
    return ""
