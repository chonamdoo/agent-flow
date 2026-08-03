"""task에서 뽑은 slug가 그 task를 대표하는지 판정한다.

비ASCII task는 두 갈래로 갈라지는데 나쁜 쪽이 해시가 아니다. 해시는 이름이 없다는
사실이 드러나지만, 한 낱말만 살아남은 이름은 성공한 것처럼 보인 채 브랜치 목록·PR
제목·머지 커밋까지 간다.

여기서 고정하는 것은 slug 문자열이 아니라 그 slug를 믿어도 되는지다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core.worktrees import describe_slug


def test_all_ascii_task_is_trusted():
    """불변: 영문 task는 그대로 쓴다. 여기에 경고를 붙이면 경고가 소음이 된다."""
    quality = describe_slug("fix search result padding")
    assert quality.kind == "ascii"
    assert quality.slug == "fix-search-result-padding"
    assert quality.dropped == ()


def test_partial_survivor_is_flagged_not_silently_accepted():
    """반증: 한 단어만 살아남은 이름을 성공으로 내놓으면 아무도 모른다."""
    quality = describe_slug("로그인 화면 Figma 구현")
    assert quality.slug == "figma"
    assert quality.kind == "partial", "그럴듯한 이름일수록 표시가 필요하다"
    assert quality.dropped == ("로그인", "화면", "구현")


def test_mixed_task_that_keeps_most_words_is_still_partial():
    """불변: 많이 살아남아도 버려진 단어가 있으면 사실대로 말한다.

    비율로 판정하면 임계값이 곧 정책이 되고, 그 정책은 코드가 아니라 취향이다.
    버려진 것이 있으면 있다고만 한다 — 무엇을 할지는 부르는 쪽이 정한다.
    """
    quality = describe_slug("parity 죽이기 체인: root/src YAML 중복 제거")
    assert quality.slug == "parity-root-src-yaml"
    assert quality.kind == "partial"
    assert "죽이기" in quality.dropped and "중복" in quality.dropped


def test_no_survivor_falls_back_to_a_stable_digest():
    """불변: 이름을 못 만들어도 실행은 막지 않는다. 다만 digest임을 밝힌다."""
    quality = describe_slug("홈 검색 결과 화면 디자인 수정")
    assert quality.kind == "digest"
    assert quality.slug.startswith("task-")
    assert quality.dropped == ("홈", "검색", "결과", "화면", "디자인", "수정")


def test_digest_is_stable_for_the_same_task():
    """불변: 같은 task가 매번 다른 이름을 내면 재개가 다른 worktree를 만든다."""
    assert describe_slug("홈 검색 결과").slug == describe_slug("홈 검색 결과").slug


def test_slug_matches_what_the_worktree_name_uses():
    """불변: 판정과 실제 이름이 다르면 경고가 엉뚱한 이름을 가리킨다."""
    from agent_flow.core.worktrees import _feature_worktree_name

    for task in ("fix padding", "로그인 화면 Figma 구현", "홈 검색 결과"):
        assert _feature_worktree_name(task) == f"feat-{describe_slug(task).slug}"


def test_task_with_no_usable_character_still_raises():
    """불변: 이름을 만들 재료가 아예 없으면 조용히 넘어가지 않는다."""
    with pytest.raises(ValueError):
        describe_slug("...")


def test_partial_slug_warns_and_names_what_was_dropped(capsys):
    """반증: 경고가 없으면 `feat-figma`가 성공한 이름처럼 흘러간다."""
    from agent_flow.cli import _warn_if_slug_does_not_represent_the_task

    _warn_if_slug_does_not_represent_the_task("로그인 화면 Figma 구현")

    err = capsys.readouterr().err
    assert "feat-figma" in err, "어떤 이름이 문제인지 말해야 한다"
    assert "로그인" in err, "무엇이 버려졌는지 말해야 고칠 수 있다"
    assert "--worktree" in err, "어떻게 고치는지 알려주지 않으면 경고가 막다른 길이다"


def test_digest_slug_also_warns(capsys):
    """불변: 해시로 떨어진 것도 사용자가 알아야 한다."""
    from agent_flow.cli import _warn_if_slug_does_not_represent_the_task

    _warn_if_slug_does_not_represent_the_task("홈 검색 결과 화면 디자인 수정")
    assert "task-" in capsys.readouterr().err


def test_ascii_task_is_silent(capsys):
    """불변: 멀쩡한 이름에 경고를 붙이면 경고가 소음이 되고, 소음은 읽히지 않는다."""
    from agent_flow.cli import _warn_if_slug_does_not_represent_the_task

    _warn_if_slug_does_not_represent_the_task("fix search result padding")
    assert capsys.readouterr().err == ""


def test_unusable_task_does_not_crash_the_warning(capsys):
    """불변: 경고 경로가 예외를 던지면 run 시작이 경고 때문에 죽는다."""
    from agent_flow.cli import _warn_if_slug_does_not_represent_the_task

    _warn_if_slug_does_not_represent_the_task("...")
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "task",
    ["로그인화면Figma구현", "로그인(Figma)", "검색결과UI수정"],
)
def test_non_ascii_glued_to_ascii_is_still_partial(task: str):
    """반증: 토큰 단위로 생사를 보면 붙여 쓴 task가 통째로 살아남은 것이 된다.

    공백이 없으면 토큰이 하나뿐이고, 그 안에 ASCII가 남아 있으니 보존으로 판정된다.
    결과는 `feat-figma`인데 `ascii`로 나와 경고도 위임도 건너뛴다 — 이 작업이 막으려던
    바로 그 실패다.
    """
    quality = describe_slug(task)
    assert quality.kind == "partial", f"{task} -> {quality.kind}"
    assert quality.dropped, "무엇이 사라졌는지 말해야 한다"


def test_dropped_words_do_not_pile_up_separators():
    """반증: `1-1 홈 UI - CTA 6dp 적용`이 `feat/1-1-ui---cta-6dp`를 만들었다.

    버려진 글자마다 구분자를 하나씩 남기면 원문의 `-` 양옆이 같이 치환된다.
    브랜치 이름에 그대로 드러나므로 조용한 결함이 아니라 눈에 띄는 흉터다.
    """
    quality = describe_slug("1-1 홈 UI - CTA 6dp 적용")
    assert quality.slug == "1-1-ui-cta-6dp"
    assert "--" not in quality.slug


def test_slug_without_letters_falls_back_to_a_digest():
    """반증: `1-1`은 브랜치 이름이 아니다.

    그 이름으로는 어떤 작업인지 누구도 알 수 없고, 다음 번호 작업과 충돌한다.
    비ASCII task와 같은 취급으로 digest를 쓴다.
    """
    quality = describe_slug("1-1")
    assert quality.kind == "digest"
    assert quality.slug.startswith("task-")


def test_a_single_dot_inside_a_word_survives():
    """불변: 구분자 접기가 `v1.2` 같은 진짜 값을 망가뜨리면 안 된다."""
    assert describe_slug("v1.2 release").slug == "v1.2-release"
