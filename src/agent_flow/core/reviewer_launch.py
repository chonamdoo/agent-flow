"""reviewer launch 선언의 파서와 매칭 술어.

이 모듈이 따로 있는 이유는 순환이다. 파서가 `multi_review`에 있던 동안
`core.profiles`의 override 검증은 함수 안 지연 import로만 그것을 부를 수 있었고
(`multi_review`는 `core.profiles`를 최상위에서 import한다), 지연 import는 순환을
가릴 뿐 없애지 않는다. 선언 해석은 profile 소비자와 리뷰 실행이 **같은 한 벌**을
써야 하므로, 둘 다 최상위에서 import할 수 있는 잎 모듈로 내린다.

여기 있는 것은 선언 → 값 번역뿐이다. provider CLI argv 번역과 PATH/버전 관측은
`multi_review`에 남는다 — 그쪽은 프로세스를 띄우는 자리의 관심사다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# `profiles/_schema.yaml`의 execution.reviewers[].candidates[].provider가 여는 값.
# 어휘는 선언이 정하므로 여기 산다. `cli_detect`는 PATH를 뒤지는 환경 탐지라
# 그쪽에 두면 profile 로딩이 subprocess/PATH probe를 통째로 끌고 온다.
REVIEW_CLI_NAMES: tuple[str, ...] = ("claude", "codex")
# `profiles/_schema.yaml`의 execution.reviewers[].candidates[].effort가 여는 값.
REVIEWER_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh")
# `execution` 바로 아래에서 소비되는 키. 그 밖의 키는 거부한다 — `reviewer:`처럼
# 한 글자 틀린 선언을 통과시키면 선언 전체가 조용히 버려진다.
_EXECUTION_KEYS = frozenset({"reviewers"})
_RULE_KEYS = frozenset({"match", "candidates"})
_MATCH_KEYS = frozenset({"phase", "angle"})
_CANDIDATE_KEYS = frozenset({"provider", "model", "effort"})


class ReviewerLaunchError(ValueError):
    """launch 선언을 해석할 수 없다.

    선언이 없는 것과 구분한다. 선언이 없으면 provider 기본값으로 돌지만, 선언이
    있는데 해석할 수 없으면 실행을 막는다 — 조용히 기본 model로 접으면 artifact가
    "선언한 model로 돌았다"를 증명할 수 없고, 그 거짓이 리뷰 결과에 붙는다.
    """


@dataclass(frozen=True)
class LaunchCandidate:
    provider: str
    model: str | None
    effort: str | None


def matching_reviewer_rule(
    profile: dict[str, Any] | None,
    *,
    phase_id: str | None,
    angle_id: str,
) -> dict[str, Any] | None:
    """먼저 일치하는 rule 하나. 선언 순서가 우선순위다."""
    for rule in reviewer_launch_rules(profile):
        if rule_matches(rule, phase_id=phase_id, angle_id=angle_id):
            return rule
    return None


def reviewer_launch_rules(
    profile: dict[str, Any] | None,
) -> tuple[dict[str, Any], ...]:
    execution = profile.get("execution") if isinstance(profile, dict) else None
    if execution is None:
        return ()
    if not isinstance(execution, dict):
        raise ReviewerLaunchError("profile execution must be a mapping")
    unknown = _unknown_keys(execution, _EXECUTION_KEYS, where="profile execution")
    if unknown:
        raise ReviewerLaunchError(
            "profile execution supports only reviewers: "
            f"remove {', '.join(unknown)}"
        )
    reviewers = execution.get("reviewers")
    if reviewers is None:
        return ()
    if not isinstance(reviewers, list) or not all(
        isinstance(rule, dict) for rule in reviewers
    ):
        raise ReviewerLaunchError(
            "profile execution.reviewers must be a list of mappings"
        )
    return tuple(reviewers)


def rule_matches(
    rule: dict[str, Any], *, phase_id: str | None, angle_id: str
) -> bool:
    match = validated_match(rule)
    if match is None:
        return True
    for key, observed in (("phase", phase_id), ("angle", angle_id)):
        declared = match.get(key)
        if declared is not None and declared != observed:
            return False
    return True


def validated_match(rule: dict[str, Any]) -> dict[str, Any] | None:
    """rule의 `match` 블록 전체를 검사하고 돌려준다. 선언 없으면 None.

    비교보다 **먼저** 전부 검사한다. 검사와 비교를 섞으면 앞 키에서 어긋난 rule의
    뒤 키가 한 번도 읽히지 않는다 — `{phase: review, angle: 5}`는 phase가 다른
    phase에서는 조용히 통과하고 `review` phase에 들어선 순간에만 터진다. 그러면
    선언 검증(`validate_reviewer_launch_declaration`)도 그 오타를 보지 못하고,
    리뷰가 시작되는 자리에서 fail-closed로 처음 드러난다.
    """
    unknown = _unknown_keys(
        rule, _RULE_KEYS, where="profile execution.reviewers entry"
    )
    if unknown:
        raise ReviewerLaunchError(
            "profile execution.reviewers entry supports only match, candidates: "
            f"remove {', '.join(unknown)}"
        )
    match = rule.get("match")
    if match is None:
        return None
    if not isinstance(match, dict):
        raise ReviewerLaunchError(
            "profile execution.reviewers[].match must be a mapping"
        )
    unknown = _unknown_keys(
        match, _MATCH_KEYS, where="profile execution.reviewers[].match"
    )
    if unknown:
        raise ReviewerLaunchError(
            "profile execution.reviewers[].match supports only phase, angle: "
            f"remove {', '.join(unknown)}"
        )
    # frozenset이 아니라 고정 순서로 돈다. 두 키가 동시에 틀렸을 때 어느 오류가
    # 먼저 보고되는지가 실행마다 달라지면 안 된다.
    for key in ("phase", "angle"):
        declared = match.get(key)
        if declared is None:
            continue
        if not isinstance(declared, str) or not declared.strip():
            raise ReviewerLaunchError(
                f"profile execution.reviewers[].match.{key} must be a non-empty string"
            )
    return match


def select_launch_candidate(
    rule: dict[str, Any], *, provider: str
) -> LaunchCandidate | None:
    """배정된 `provider`를 장식하는 candidate. 없으면 None(= 기본 argv).

    provider를 고르는 권한은 `multi_review.distribute()`에만 있다. 여기서 선언
    순서로 provider를 바꾸면 claude만 선언한 rule이 두 슬롯을 모두 claude로 띄우고,
    artifact 이름은 여전히 `<angle>-codex.md`로 남아 "독립된 두 provider가 봤다"가
    존재하지 않는 다양성으로 만족된 것처럼 읽힌다.

    선언 순서는 같은 provider가 여러 번 선언됐을 때의 우선순위다.
    """
    # 고르기 전에 전부 검증한다. 다른 provider candidate의 오타가 이 슬롯에서
    # 숨어 있다가 그 provider가 배정되는 날에만 터지지 않게 한다.
    parsed = launch_candidates(rule)
    for candidate in parsed:
        if candidate.provider == provider:
            return candidate
    return None


def launch_candidates(rule: dict[str, Any]) -> tuple[LaunchCandidate, ...]:
    candidates = rule.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ReviewerLaunchError(
            "profile execution.reviewers[].candidates must be a non-empty list"
        )
    return tuple(parse_launch_candidate(item) for item in candidates)


def validate_reviewer_launch_declaration(execution: object) -> None:
    """선언이 해석 가능한지 선언한 자리에서 검사한다.

    `core.profiles`의 override loader와 배포 profile을 훑는 테스트가 부른다.
    실행 경로가 쓰는 파서 그대로라서 "여기서는 통과하는데 리뷰에서 막히는" 상태가
    생기지 않는다. 매칭은 흉내내지 않는다 — 검사는 어느 phase/angle에서 도는지와
    무관해야 한다.
    """
    for rule in reviewer_launch_rules({"execution": execution}):
        validated_match(rule)
        launch_candidates(rule)


def parse_launch_candidate(item: object) -> LaunchCandidate:
    if not isinstance(item, dict):
        raise ReviewerLaunchError(
            "profile execution.reviewers[].candidates[] must be a mapping"
        )
    unknown = _unknown_keys(item, _CANDIDATE_KEYS, where="reviewer candidate")
    if unknown:
        raise ReviewerLaunchError(
            "reviewer candidate supports only provider, model, effort: "
            f"remove {', '.join(unknown)}"
        )
    provider = item.get("provider")
    if provider not in REVIEW_CLI_NAMES:
        raise ReviewerLaunchError(
            "reviewer candidate provider must be one of "
            f"{', '.join(REVIEW_CLI_NAMES)}: got {provider!r}"
        )
    model = item.get("model")
    if model is not None and (
        not isinstance(model, str) or not model.strip() or model.startswith("-")
    ):
        # 선두 `-`는 model id가 아니라 플래그로 읽힌다. 선언 하나가 argv 뒤쪽을
        # 통째로 다른 뜻으로 만들 수 있으므로 여기서 막는다.
        raise ReviewerLaunchError(
            f"reviewer candidate model must be a model id, not a flag: got {model!r}"
        )
    effort = item.get("effort")
    if effort is not None and effort not in REVIEWER_EFFORTS:
        raise ReviewerLaunchError(
            "reviewer candidate effort must be one of "
            f"{', '.join(REVIEWER_EFFORTS)}: got {effort!r}"
        )
    return LaunchCandidate(provider=provider, model=model, effort=effort)


def _unknown_keys(
    mapping: dict[Any, Any], allowed: frozenset[str], *, where: str
) -> list[str]:
    """`allowed` 밖의 키. 키 타입 검증이 먼저다.

    YAML mapping의 키는 `Any`다 — `1:`이나 `on:`(bool)로 적으면 키가 문자열이
    아니다. 그것을 그대로 `sorted`/`join`에 넘기면 `TypeError`가 이 모듈의
    예외 계약을 뚫고 나가 호출자의 `except ReviewerLaunchError`도, CLI의
    핸들러도 잡지 못하고 traceback으로 사용자에게 닿는다.
    """
    non_string = sorted(repr(key) for key in mapping if not isinstance(key, str))
    if non_string:
        raise ReviewerLaunchError(
            f"{where} keys must be strings: got {', '.join(non_string)}"
        )
    return sorted(set(mapping) - allowed)
