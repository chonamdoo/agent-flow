"""fetch 캐시 갱신 경로.

`main` 같은 움직이는 ref를 SHA로 핀하는 것은 이 버그를 고치지 않는다 — `main`은
이미 사실상 핀이고, 다만 머신마다 다른 **보이지 않는** 핀일 뿐이다. 진짜 결함은
갱신 경로가 없다는 것이라, 여기서 반증하는 것도 그것이다.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = str(REPO / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core.skill_sync import (
    SkillSource,
    cache_root,
    cached_source_sha,
    sync_skill_sources,
)


def _git(*args, cwd):
    return subprocess.run(("git", *args), cwd=str(cwd), capture_output=True, text=True, check=True)


@pytest.fixture()
def upstream(tmp_path):
    root = tmp_path / "upstream"
    root.mkdir()
    _git("init", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "VERSION").write_text("VERSION-1\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-m", "v1", cwd=root)
    return root


@pytest.fixture()
def source(upstream):
    return SkillSource(
        id="probe",
        kind="fetch",
        url=str(upstream),
        ref="main",
        layout="",
        install_hint="",
    )


def _env(tmp_path) -> dict:
    # `cache_root`는 XDG_STATE_HOME / Path.home()를 본다. HOME만 바꾸면 `Path.home()`이
    # 실제 os.environ을 읽어 사용자 캐시를 오염시킨다. 명시 override만 쓴다.
    return {"AGENT_FLOW_SKILL_CACHE": str(tmp_path / "skill-cache")}


def _checkout(source, env) -> Path:
    return cache_root(env) / source.id / source.ref


def test_first_sync_fetches_and_records_the_sha(tmp_path, source, upstream):
    env = _env(tmp_path)
    result = sync_skill_sources([source], env=env)[0]
    assert result.status == "fetched"
    head = _git("rev-parse", "HEAD", cwd=upstream).stdout.strip()
    assert cached_source_sha(source, env=env) == head
    assert (_checkout(source, env) / "VERSION").read_text() == "VERSION-1\n"


def test_second_sync_without_refresh_stays_cached(tmp_path, source, upstream):
    env = _env(tmp_path)
    sync_skill_sources([source], env=env)
    (upstream / "VERSION").write_text("VERSION-2\n", encoding="utf-8")
    _git("commit", "-am", "v2", cwd=upstream)
    result = sync_skill_sources([source], env=env)[0]
    assert result.status == "cached"
    assert (_checkout(source, env) / "VERSION").read_text() == "VERSION-1\n"


def test_refresh_picks_up_the_moved_ref(tmp_path, source, upstream):
    """반증: 이게 없으면 캐시가 최초 1회 받은 커밋에 영구히 굳는다."""
    env = _env(tmp_path)
    sync_skill_sources([source], env=env)
    first = cached_source_sha(source, env=env)
    (upstream / "VERSION").write_text("VERSION-2\n", encoding="utf-8")
    _git("commit", "-am", "v2", cwd=upstream)

    result = sync_skill_sources([source], env=env, refresh=True)[0]
    assert result.status == "fetched"
    assert (_checkout(source, env) / "VERSION").read_text() == "VERSION-2\n"
    assert cached_source_sha(source, env=env) != first
    assert cached_source_sha(source, env=env) == _git("rev-parse", "HEAD", cwd=upstream).stdout.strip()


def test_refresh_does_not_touch_non_fetch_sources(tmp_path):
    source = SkillSource(id="host", kind="host-managed", url="", ref="", layout="", install_hint="hint")
    assert sync_skill_sources([source], env=_env(tmp_path), refresh=True)[0].status == "skipped"


def test_cli_exposes_refresh(tmp_path, monkeypatch):
    """`--refresh`가 실제로 sync까지 전달되는가. 플래그만 있고 안 쓰면 무의미하다."""
    from agent_flow import cli

    seen: list[bool] = []
    monkeypatch.setattr(
        cli, "parse_skill_sources", lambda payload: (SkillSource("probe", "fetch", "u", "main", "", ""),)
    )
    monkeypatch.setattr(cli, "active_profile_ids", lambda root, requested: ["generic"])
    monkeypatch.setattr(cli, "load_profile_payload", lambda profile_id: {})
    monkeypatch.setattr(
        cli,
        "sync_skill_sources",
        lambda sources, refresh=False: seen.append(refresh) or [],
    )
    assert cli.main(["skills", "sync", "--root", str(tmp_path)]) == 0
    assert cli.main(["skills", "sync", "--root", str(tmp_path), "--refresh"]) == 0
    assert seen == [False, True]
