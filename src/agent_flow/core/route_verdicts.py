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
from typing import Literal

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
        and _gate_execution_is_local(payload)
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
    """Return whether the local gate wave covered every declared gate phase."""
    produced_by = payload.get("produced_by")
    if not isinstance(produced_by, dict):
        return True
    recorded = produced_by.get("gate_phase")
    if not isinstance(recorded, str) or not recorded:
        return True
    return recorded == GATE_PHASE_ALL


def _gate_execution_is_local(payload: dict[str, object]) -> bool:
    """A CI-only reproduction cannot substitute for the local gate wave."""
    produced_by = payload.get("produced_by")
    if not isinstance(produced_by, dict):
        return True
    recorded = produced_by.get("gate_execution")
    if not isinstance(recorded, str) or not recorded:
        return True
    return recorded == "local"


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


def recorded_gate_execution(text: str) -> str:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    produced_by = payload.get("produced_by")
    if not isinstance(produced_by, dict):
        return ""
    recorded = produced_by.get("gate_execution")
    return recorded if isinstance(recorded, str) else ""


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
