"""비ASCII task의 이름 짓기를 활성 host에 위임하는 경로.

profile이 선언한 임의 명령을 돌리므로 경계를 좁게 잡는다 — 셸을 거치지 않고, 출력은
slug 규칙으로 다시 검증하며, 어떤 실패도 worktree 생성을 막지 않는다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core.worktrees import delegated_slug


def _echo(text: str) -> list[str]:
    return [sys.executable, "-c", f"print({text!r})"]


def test_host_command_output_becomes_the_slug():
    """불변: 위임이 성공하면 그 이름을 쓴다."""
    assert delegated_slug(
        task="로그인 화면 Figma 구현",
        command=_echo("login-screen-figma"),
        timeout_s=10,
    ) == "login-screen-figma"


def test_output_is_normalised_not_trusted_verbatim():
    """불변: host 출력은 제안이지 최종 이름이 아니다.

    대문자·공백·따옴표가 섞여 나오는 것은 흔하다. 그대로 브랜치 이름에 쓰면
    git이 거부하거나 디렉터리 이름이 깨진다.
    """
    assert delegated_slug(
        task="t", command=_echo('  "Login Screen Figma"  '), timeout_s=10
    ) == "login-screen-figma"


@pytest.mark.parametrize(
    "output",
    [
        "../../etc/passwd",       # 경로 탈출
        "..",                     # 상위 참조
        ".hidden",                # 점으로 시작
        "",                       # 빈 출력
        "   ",                    # 공백뿐
        "한글만-나온-경우",        # 여전히 비ASCII
    ],
)
def test_unusable_output_is_refused(output: str):
    """반증: 출력을 그대로 믿으면 profile 한 줄로 이름이 경로가 된다."""
    assert delegated_slug(task="t", command=_echo(output), timeout_s=10) is None


def test_absurdly_long_output_is_truncated_on_a_boundary():
    """불변: 길이 제한이 단어를 자르면 읽을 수 없는 이름이 남는다."""
    long = "-".join(["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"])
    slug = delegated_slug(task="t", command=_echo(long), timeout_s=10, max_length=20)
    assert slug is not None
    assert len(slug) <= 20
    assert not slug.endswith("-")
    assert all(part in long.split("-") for part in slug.split("-"))


def test_failing_command_is_not_an_error():
    """불변: 이름을 못 지었다고 worktree 생성이 막히면 안 된다."""
    assert delegated_slug(
        task="t", command=[sys.executable, "-c", "raise SystemExit(3)"], timeout_s=10
    ) is None


def test_timeout_is_bounded():
    """불변: host가 멈추면 worktree 생성이 함께 멈춘다."""
    slow = [sys.executable, "-c", "import time; time.sleep(5)"]
    assert delegated_slug(task="t", command=slow, timeout_s=1) is None


def test_missing_executable_is_not_an_error():
    """불변: 선언한 host CLI가 안 깔린 환경에서도 run은 시작돼야 한다."""
    assert delegated_slug(
        task="t", command=["definitely-not-a-real-binary-xyz"], timeout_s=10
    ) is None


def test_task_is_passed_as_an_argument_not_a_shell_string():
    """불변: 셸을 거치면 task 텍스트가 명령이 된다.

    task는 사용자가 자유롭게 쓰는 문자열이다. `; rm -rf ~`가 들어와도 그건 인자일
    뿐이어야 한다.
    """
    show_argv = [sys.executable, "-c", "import sys; print(sys.argv[1])", "{task}"]
    # 이어진 구분자는 하나로 접는다 — `; rm` 자리의 `--`가 `-`가 된다. 셸을 거쳤다면
    # 애초에 이 출력이 나오지 않는다.
    assert delegated_slug(
        task="drop; rm -rf ~", command=show_argv, timeout_s=10
    ) == "drop-rm-rf"


def test_empty_command_declaration_is_skipped():
    """불변: 선언이 없으면 위임 자체를 하지 않는다."""
    assert delegated_slug(task="t", command=[], timeout_s=10) is None


def test_ascii_task_never_reaches_delegation(tmp_path, monkeypatch):
    """불변: 멀쩡한 영문 task에 LLM 왕복을 붙이면 매 run이 그만큼 느려진다."""
    from agent_flow import cli as CLI

    called: list[str] = []
    monkeypatch.setattr(CLI, "_slug_naming_for_active_host", lambda root: (called.append("x") or [], 60))

    assert CLI._derive_worktree_selector(root=tmp_path, task="fix padding") == "fix padding"
    assert called == [], "영문 task는 위임 경로에 들어가면 안 된다"


def test_delegated_name_is_used_when_the_host_succeeds(tmp_path, monkeypatch, capsys):
    """불변: 위임이 성공하면 그 이름을 쓰고, 썼다는 사실을 알린다."""
    from agent_flow import cli as CLI

    monkeypatch.setattr(CLI, "_slug_naming_for_active_host", lambda root: (["fake"], 60))
    monkeypatch.setattr(CLI, "delegated_slug", lambda **kw: "login-screen-figma")

    assert CLI._derive_worktree_selector(
        root=tmp_path, task="로그인 화면 Figma 구현"
    ) == "login-screen-figma"
    assert "login-screen-figma" in capsys.readouterr().out


def test_failed_delegation_falls_back_to_the_honest_warning(tmp_path, monkeypatch, capsys):
    """불변: 위임이 실패해도 run은 시작되고, 나쁜 이름은 나쁘다고 말한다.

    Slice 1이 만들어 둔 정직한 실패가 여기서 안전망이 된다 — 해시로 떨어져도
    사용자가 그 사실을 안다.
    """
    from agent_flow import cli as CLI

    monkeypatch.setattr(CLI, "_slug_naming_for_active_host", lambda root: (["fake"], 60))
    monkeypatch.setattr(CLI, "delegated_slug", lambda **kw: None)

    task = "로그인 화면 Figma 구현"
    assert CLI._derive_worktree_selector(root=tmp_path, task=task) == task
    assert "feat-figma" in capsys.readouterr().err


def test_no_declaration_means_no_delegation(tmp_path, monkeypatch, capsys):
    """불변: 선언이 없는 프로젝트는 지금과 똑같이 동작한다."""
    from agent_flow import cli as CLI

    monkeypatch.setattr(CLI, "_slug_naming_for_active_host", lambda root: ([], 60))
    monkeypatch.setattr(
        CLI, "delegated_slug", lambda **kw: pytest.fail("선언이 없는데 위임했다")
    )

    task = "홈 검색 결과 화면 디자인 수정"
    assert CLI._derive_worktree_selector(root=tmp_path, task=task) == task
    assert "task-" in capsys.readouterr().err


def test_profile_length_limit_reaches_the_delegated_name(tmp_path, monkeypatch):
    """불변: profile이 선언한 제한을 안 읽으면 host가 낸 긴 이름이 그대로 쓰인다."""
    from agent_flow import cli as CLI

    seen: dict[str, object] = {}
    monkeypatch.setattr(CLI, "_slug_naming_for_active_host", lambda root: (["fake"], 20))
    monkeypatch.setattr(
        CLI, "delegated_slug", lambda **kw: seen.update(kw) or "short-name"
    )

    CLI._derive_worktree_selector(root=tmp_path, task="로그인 화면 Figma 구현")
    assert seen.get("max_length") == 20


def test_missing_length_declaration_falls_back_to_the_default(tmp_path, monkeypatch):
    """불변: 선언이 없거나 값이 이상하면 기본값으로 떨어진다."""
    from agent_flow import cli as CLI
    from agent_flow.core.worktrees import DEFAULT_SLUG_MAX_LENGTH

    seen: dict[str, object] = {}
    monkeypatch.setattr(
        CLI, "_slug_naming_for_active_host", lambda root: (["fake"], DEFAULT_SLUG_MAX_LENGTH)
    )
    monkeypatch.setattr(CLI, "delegated_slug", lambda **kw: seen.update(kw) or "n")

    CLI._derive_worktree_selector(root=tmp_path, task="로그인 화면 Figma 구현")
    assert seen.get("max_length") == DEFAULT_SLUG_MAX_LENGTH
