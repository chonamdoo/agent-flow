"""주석에 새어 들어오는 외부 참조·값 재서술·과정 메모를 막는다.

기존 규칙은 영어 서술형 동사와 구분선만 봤다. 그래서 이 저장소가 실제로 쓰는 한국어
주석과, 스펙을 붙여넣을 때 딸려 들어오는 문서 번호는 전부 빠져나갔다.

아래 사례는 실제 Android 프로젝트에서 수집한 것이다. 지어낸 예로 규칙을 만들면 잡히는
것도 지어낸 것만 잡힌다.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "hooks" / "comment-checker.py"


def _check(comment: str, name: str = "Sample.kt") -> subprocess.CompletedProcess[str]:
    payload = {
        "tool_name": "Edit",
        "hook_event_name": "PostToolUse",
        "cwd": str(ROOT),
        "tool_input": {
            "file_path": name,
            "old_string": "val a = 1\n",
            "new_string": f"{comment}\nval a = 1\n",
        },
    }
    return subprocess.run(
        (sys.executable, str(CHECKER)),
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    "comment",
    [
        "// Figma 스펙: Variant=Outlined · Color=White · Size=Small",
        '// Figma "Tab Bar - Default": top-only border drawn over the container',
        "// 라벨↔연필 여백은 Figma 스펙(4dp)에 맞춰 직접 제어한다.",
        "// 자세한 내용은 https://www.figma.com/file/abc 참고",
        "// https://www.notion.so/team/spec-123",
        "// 설계 문서: https://example.com/spec",
    ],
)
def test_external_design_reference_is_blocked(comment: str) -> None:
    """반증: 링크가 죽으면 주석만 남고 의미는 사라진다."""
    result = _check(comment)
    assert result.returncode == 2, f"통과했다: {comment}"


@pytest.mark.parametrize(
    "comment",
    [
        "// story-engagement POLICY-001: PUBLISHED 스토리에만 좋아요 허용.",
        "// 국가/주 선택 바텀시트 최대 높이 = 상단 앱바까지(SCR-AUTH-09).",
        "// 시트를 유지한 채 안내 토스트만 노출한다(PRF-11 설계).",
        "// 결과 없음에도 드롭다운을 닫지 않는다(SCH-001).",
        "// 인덱스를 200ms ease로 보간한다(motion.md SCR-002 #2 translateX).",
    ],
)
def test_internal_document_identifier_is_blocked(comment: str) -> None:
    """반증: 사내 문서 번호는 그 문서를 못 보는 사람에게 아무 의미가 없다.

    규칙 자체는 남겨도 된다 — 번호만 빼면 된다.
    """
    result = _check(comment)
    assert result.returncode == 2, f"통과했다: {comment}"


@pytest.mark.parametrize(
    "comment",
    [
        "// chat-bubble/ai-bg #192128",
        "// line/normal/alternative rgba(112,115,124,0.08)",
        "// Thumbnail placeholder bg #262629.",
        "// 색상 #7D8890",
        "// 24 -> 16",
    ],
)
def test_value_restatement_is_blocked(comment: str) -> None:
    """반증: 코드가 이미 말하는 값을 되풀이하면, 값이 바뀔 때 주석만 거짓말로 남는다."""
    result = _check(comment)
    assert result.returncode == 2, f"통과했다: {comment}"


@pytest.mark.parametrize(
    "comment",
    [
        "// 뷰모델 초기화",
        "// 패딩 설정",
        "// 결과 반환",
        "// 어댑터 생성",
        "// 클릭 처리",
    ],
)
def test_korean_restating_comment_is_blocked(comment: str) -> None:
    """반증: 규칙이 영어 전용이라 이 저장소가 실제로 쓰는 주석이 전부 빠져나갔다."""
    result = _check(comment)
    assert result.returncode == 2, f"통과했다: {comment}"


@pytest.mark.parametrize(
    "comment",
    [
        "// [TEMP-TEST] dev API는 tailnet 호스트로 교체.",
        "// 오픈 스펙 제외(8월 배포): 재노출 시 복원.",
        "// 리뷰 반영: 순서 교정",
        "// 되돌리지 말 것",
    ],
)
def test_process_note_is_blocked(comment: str) -> None:
    """반증: 과정 메모는 머지되는 순간 소음이 된다. PR에 적을 내용이다."""
    result = _check(comment)
    assert result.returncode == 2, f"통과했다: {comment}"


@pytest.mark.parametrize(
    "comment",
    [
        "// 응답 생성 중에는 프로필을 변경할 수 없다. 시트는 유지한다.",
        "// PUBLISHED 스토리에만 좋아요를 허용한다.",
        "// 차단은 로그인 상태에서만 가능하다.",
        "// 결과가 없어도 드롭다운을 닫지 않는다. 닫으면 재검색이 불가능하다.",
        "// flock은 fd 단위라 같은 프로세스에서 두 번 잡으면 데드락이 된다.",
        "// 스크롤 중 딤이 사라지던 회귀가 있어 항상 적용한다.",
    ],
)
def test_policy_comment_is_allowed(comment: str) -> None:
    """불변: 정책을 적은 주석까지 막으면 규칙이 소음이 되고, 소음은 무시된다.

    위 사례들은 전부 실제 프로젝트에서 온 **좋은** 주석이다. 하나라도 막히면
    개발자는 checker를 끄지 규칙을 지키지 않는다.
    """
    result = _check(comment)
    assert result.returncode == 0, f"정당한 주석을 막았다: {comment}\n{result.stderr}"


def test_hex_inside_a_policy_sentence_is_allowed() -> None:
    """불변: 값이 등장한다고 다 재서술은 아니다.

    `#RRGGBB`가 규칙의 근거로 쓰이면 남겨야 한다. 값만 덩그러니 있는 것과 다르다.
    """
    result = _check("// 디자인 토큰이 없어 #192128을 직접 쓴다. 토큰 생기면 교체한다.")
    assert result.returncode == 0, result.stderr


def test_url_in_a_string_literal_is_not_a_comment() -> None:
    """불변: 문자열 안의 `//`를 주석으로 읽으면 정상 코드가 막힌다."""
    payload = {
        "tool_name": "Edit",
        "hook_event_name": "PostToolUse",
        "cwd": str(ROOT),
        "tool_input": {
            "file_path": "Sample.kt",
            "old_string": "val a = 1\n",
            "new_string": 'val url = "https://www.figma.com/file/abc"\nval a = 1\n',
        },
    }
    result = subprocess.run(
        (sys.executable, str(CHECKER)),
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "comment",
    [
        # 여러 줄 주석의 중간 줄이 우연히 그 낱말로 끝난다.
        "// 띄우고 실패 시 예외를 던지므로, 방금 끝난 게이트 결과를 그 조회",
        # `재생성`은 복합어다. 뒷부분만 보면 재서술로 오인한다.
        "// 사용자 제스처만 추적을 끈다: 직접 스크롤만 잡고, 재생성",
    ],
)
def test_long_sentence_ending_in_a_trigger_word_is_allowed(comment: str) -> None:
    """반증: 낱말만 보면 정당한 문장의 줄 끝과 복합어가 함께 걸린다."""
    result = _check(comment)
    assert result.returncode == 0, f"정당한 주석을 막았다: {comment}\n{result.stderr}"


@pytest.mark.parametrize(
    "comment",
    [
        "// PKCS-12 형식. 내보내기 시 비밀번호 없으면 거부한다.",
        "// API-29 이하에서는 ContentResolver.query() 반환값이 null일 수 있다.",
        "// CVE-2021-44228 대응으로 이 경로를 막는다.",
        "// IEEE-754 반올림 때문에 정수로 비교한다.",
    ],
)
def test_public_standard_identifier_is_allowed(comment: str) -> None:
    """불변: 공개 표준은 문서 번호가 아니라 이름의 일부다."""
    result = _check(comment)
    assert result.returncode == 0, f"공개 표준을 막았다: {comment}\n{result.stderr}"


def test_bare_noun_comment_is_still_blocked_after_the_fix() -> None:
    """불변: 오탐을 고치면서 원래 잡던 것까지 놓치면 안 된다."""
    assert _check("// 뷰모델 초기화").returncode == 2
    assert _check("// 결과 반환").returncode == 2
