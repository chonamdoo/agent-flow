"""릴리스 갱신 알림.

이 알림이 사는 이유는 brew로 설치한 kit이 조용히 낡는 것이다. 그래서 반증할 것은
"새 릴리스가 있으면 말한다" 하나가 아니라, 그 확인이 사용자의 명령을 느리게 만들지
않는다는 쪽이다 — 네트워크가 막힌 환경에서 명령마다 상한만큼 멈추면 알림이 아니라
방해가 된다.

기대 버전을 파일에 박지 않는다. 비교 자체를 반증하고, 실제 릴리스 값은 런타임에서 온다.
"""
from __future__ import annotations

import http.client
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = str(REPO / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.cli import main
from agent_flow.core import update_check as U


class _Counted:
    """조회 횟수를 세는 fetcher. 캐시가 도는지는 호출 수로만 증명된다."""

    def __init__(self, *results: str | None) -> None:
        self.results = list(results)
        self.calls = 0

    def __call__(self) -> str | None:
        self.calls += 1
        if not self.results:
            return None
        return self.results[min(self.calls - 1, len(self.results) - 1)]


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"XDG_STATE_HOME": str(tmp_path), **extra}


def test_the_result_is_cached_for_a_day(tmp_path: Path) -> None:
    """반증: 명령마다 다시 물으면 phase마다 네트워크 지연이 붙는다."""
    fetch = _Counted("v0.9.0")
    env = _env(tmp_path)
    first = U.latest_release(fetch=fetch, now=1_000.0, env=env)
    second = U.latest_release(fetch=fetch, now=1_000.0 + U.CHECK_TTL_S - 1, env=env)
    assert (first, second) == ("v0.9.0", "v0.9.0")
    assert fetch.calls == 1
    # TTL을 넘기면 다시 묻는다. 넘겨도 캐시를 쓰면 릴리스가 나와도 영원히 조용하다.
    U.latest_release(fetch=fetch, now=1_000.0 + U.CHECK_TTL_S + 1, env=env)
    assert fetch.calls == 2


def test_a_failed_check_is_cached_too(tmp_path: Path) -> None:
    """반증: 실패를 캐시하지 않으면 네트워크가 막힌 곳에서 매 명령이 상한만큼 멈춘다."""
    fetch = _Counted(None)
    env = _env(tmp_path)
    assert U.latest_release(fetch=fetch, now=10.0, env=env) is None
    assert U.latest_release(fetch=fetch, now=20.0, env=env) is None
    assert fetch.calls == 1
    assert json.loads((tmp_path / "update-check.json").read_text(encoding="utf-8")) == {
        "checked_at": 10.0,
        "latest": "",
    }


def test_a_failed_check_does_not_erase_a_known_release(tmp_path: Path) -> None:
    """반증: 실패가 알던 값을 덮으면 이미 나온 릴리스 알림이 하루 사라진다."""
    env = _env(tmp_path)
    U.latest_release(fetch=_Counted("v1.2.3"), now=0.0, env=env)
    offline = _Counted(None)
    kept = U.latest_release(fetch=offline, now=U.CHECK_TTL_S + 1, env=env)
    assert kept == "v1.2.3"
    assert offline.calls == 1
    # 시각은 새로 찍혀야 한다. 그래야 막힌 네트워크를 하루에 한 번만 두드린다.
    later = U.latest_release(fetch=offline, now=U.CHECK_TTL_S + 2, env=env)
    assert (later, offline.calls) == ("v1.2.3", 1)


def test_the_switch_stops_the_automatic_check_but_not_the_explicit_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """반증: 직접 물은 명령까지 스위치가 막으면 확인할 방법이 없다."""
    monkeypatch.setattr(U, "installed_version", lambda: "0.1.0")
    env = _env(tmp_path, **{U.DISABLE_ENV: "1"})
    silenced = _Counted("v9.9.9")
    U.warn_if_update_available(REPO, fetch=silenced, now=0.0, env=env)
    assert capsys.readouterr().err == ""
    assert silenced.calls == 0

    asked = _Counted("v9.9.9")
    assert U.print_update_report(REPO, fetch=asked, now=0.0, env=env) == 0
    assert asked.calls == 1
    assert "latest: v9.9.9" in capsys.readouterr().out


def test_versions_compare_by_number_not_by_string() -> None:
    """반증: 문자열로 비교하면 0.10.0이 0.9.0보다 낡은 것이 된다."""
    assert U.update_available("0.9.0", "0.10.0") is True
    assert U.update_available("0.1.0", "v0.1.1") is True
    assert U.update_available("1.0", "1.0.0") is False
    assert U.update_available("1.0.1", "v1.0.0") is False
    # 읽을 수 없는 값으로는 알리지 않는다. 알림의 근거가 추측이 되면 안 된다.
    assert U.update_available("0.1.0", "nightly") is False
    assert U.update_available(None, "v1.0.0") is False


def test_the_upgrade_command_follows_how_the_kit_was_installed(tmp_path: Path) -> None:
    """반증: brew 설치본에 `git pull`을 시키면 그 명령은 아무것도 고치지 못한다."""
    cellar = tmp_path / "Cellar" / "agent-flow" / "0.1.0" / "libexec"
    cellar.mkdir(parents=True)
    assert U.is_homebrew_install(cellar) is True
    assert U.upgrade_command(cellar) == f"brew upgrade {U.TAP_FORMULA}"

    # HEAD 설치본은 `--fetch-HEAD` 없이는 upstream commit을 아예 보지 않는다.
    head = tmp_path / "Cellar" / "agent-flow" / "HEAD-a1b2c3d" / "libexec"
    head.mkdir(parents=True)
    assert U.upgrade_command(head) == f"brew upgrade --fetch-HEAD {U.TAP_FORMULA}"

    checkout = tmp_path / "src" / "agent-flow"
    checkout.mkdir(parents=True)
    assert U.is_homebrew_install(checkout) is False
    assert U.upgrade_command(checkout) == f"git -C {checkout} pull"


def test_the_warning_names_the_release_and_the_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """반증: 버전만 알리고 명령을 빼면 사용자는 설치 방식을 스스로 알아내야 한다."""
    monkeypatch.setattr(U, "installed_version", lambda: "0.1.0")
    cellar = tmp_path / "Cellar" / "agent-flow" / "0.1.0" / "libexec"
    cellar.mkdir(parents=True)
    U.warn_if_update_available(
        cellar, fetch=_Counted("v0.2.0"), now=0.0, env=_env(tmp_path)
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "agent-flow v0.2.0 is available (installed 0.1.0)" in captured.err
    assert f"brew upgrade {U.TAP_FORMULA}" in captured.err


def test_nothing_is_said_when_the_installed_version_is_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """반증: 비교 대상을 모르는 채 알리면 그 알림은 추측이다."""
    monkeypatch.setattr(U, "installed_version", lambda: None)
    fetch = _Counted("v0.2.0")
    U.warn_if_update_available(REPO, fetch=fetch, now=0.0, env=_env(tmp_path))
    assert capsys.readouterr().err == ""
    assert fetch.calls == 0


def test_the_state_file_lives_under_the_declared_state_home(tmp_path: Path) -> None:
    """반증: 캐시가 프로젝트 안에 생기면 leader tripwire가 그것을 오염으로 본다."""
    assert U.cache_path(_env(tmp_path)) == tmp_path / "update-check.json"
    # 상대 경로면 자리를 알 수 없다. 조용히 cwd에 쓰지 않고 거절한다.
    with pytest.raises(ValueError):
        U.cache_path({"XDG_STATE_HOME": "relative/state"})


def test_an_unreadable_tag_is_neither_compared_nor_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """반증: 응답 본문을 그대로 태그로 받으면 자릿수 없는 숫자가 `int()`를 넘어뜨리고,
    제어 문자가 신뢰받는 CLI 메시지 안으로 들어온다."""
    monkeypatch.setattr(U, "installed_version", lambda: "0.1.0")
    for hostile in ("9" * 5_000, "v1.0.0\x1b[31m FAKE", "v" + "1." * 200 + "0"):
        U.warn_if_update_available(
            REPO, fetch=_Counted(hostile), now=0.0, env=_env(tmp_path / hostile[:8])
        )
        captured = capsys.readouterr()
        # 빈 stderr만으로는 부족하다 — 그 경로는 예외를 삼키므로, 통째로 죽어도 같은
        # 모습이다. 그래서 gate와 비교를 직접 부른다.
        assert U._accepted_tag(hostile) is None, hostile[:32]
        assert captured.err == "", hostile[:32]
        assert U.update_available("0.1.0", hostile) is False
    # 끝의 개행은 비교에는 무해하다(비교는 strip한다). 막아야 하는 자리는 저장과 출력
    # 이고, 그 gate는 `$`가 아니라 `\Z`로 끝나야 개행을 인정하지 않는다.
    assert U._accepted_tag("v1.0.0\n") is None


def test_a_prerelease_version_never_advertises_an_older_release(tmp_path: Path) -> None:
    """반증: 접미사가 붙은 버전에서 patch 자리가 잘리면(`0.1.9rc1` → `0.1`) 더 낡은
    릴리스가 업그레이드로 보인다. 그 값은 pre-release 빌드의 설치 버전으로 실제로 온다."""
    assert U._version_tuple("0.1.9rc1") is None
    assert U._version_tuple("1.2.3b1") is None
    assert U.update_available("0.1.9rc1", "v0.1.5") is False
    # 읽을 수 있는 형태는 그대로 읽는다.
    assert U._version_tuple("v1.2.3") == (1, 2, 3)
    assert U._version_tuple("1.2.3-1") == (1, 2, 3)


def test_a_poisoned_cache_cannot_freeze_the_check(tmp_path: Path) -> None:
    """반증: `Infinity` 하나로 TTL 비교가 영원히 참이 되면 캐시는 다시 갱신되지 않는다."""
    (tmp_path / "update-check.json").write_text(
        '{"checked_at": Infinity, "latest": "v0.0.1"}', encoding="utf-8"
    )
    fetch = _Counted("v0.2.0")
    assert U.latest_release(fetch=fetch, now=1.0, env=_env(tmp_path)) == "v0.2.0"
    assert fetch.calls == 1


def test_a_failing_check_never_breaks_the_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """반증: 조언 경로의 예외가 새면 사용자가 치려던 명령이 알림 때문에 죽는다."""
    monkeypatch.setattr(U, "installed_version", lambda: "0.1.0")

    def _explode() -> str:
        raise http.client.IncompleteRead(b"")

    U.warn_if_update_available(REPO, fetch=_explode, now=0.0, env=_env(tmp_path))
    assert capsys.readouterr().err == ""


def test_a_lifecycle_command_is_wired_to_the_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """반증: 모듈만 검증하면 `cli.py`에서 호출을 지워도 스위트가 초록으로 남는다.

    conftest의 스위치를 이 테스트 안에서만 걷어내고, 실제 진입점으로 들어간다.
    """
    monkeypatch.delenv(U.DISABLE_ENV, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(U, "installed_version", lambda: "0.0.1")
    monkeypatch.setattr(U, "fetch_latest_release", lambda: "v9.9.9")
    monkeypatch.chdir(tmp_path)
    main(["status"])
    assert "agent-flow v9.9.9 is available (installed 0.0.1)" in capsys.readouterr().err
