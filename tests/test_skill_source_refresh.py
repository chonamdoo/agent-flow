"""fetch 캐시 갱신 경로.

`main` 같은 움직이는 ref를 SHA로 핀하는 것은 이 버그를 고치지 않는다 — `main`은
이미 사실상 핀이고, 다만 머신마다 다른 **보이지 않는** 핀일 뿐이다. 진짜 결함은
갱신 경로가 없다는 것이라, 여기서 반증하는 것도 그것이다.
"""
from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SRC = str(REPO / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core.skill_sync import (
    SkillSource,
    cache_root,
    cached_source_checkout,
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
    checkout = cached_source_checkout(source, env=env)
    assert checkout is not None
    return checkout


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


def test_cli_refresh_publishes_moved_ref_without_mutating_reader(tmp_path, source, upstream, monkeypatch):
    from agent_flow import cli

    env = _env(tmp_path)
    monkeypatch.setenv("AGENT_FLOW_SKILL_CACHE", env["AGENT_FLOW_SKILL_CACHE"])
    project = tmp_path / "project"
    profiles = project / ".agent-flow/profiles"
    profiles.mkdir(parents=True)
    (profiles / "probe.yaml").write_text(
        yaml.safe_dump({"id": "probe", "skill_sources": [asdict(source)]}), encoding="utf-8",
    )
    (project / ".agent-flow/kit.json").write_text('{"profiles":["probe"]}', encoding="utf-8")
    assert cli.main(["skills", "sync", "--root", str(project)]) == 0
    previous = _checkout(source, env)
    (upstream / "VERSION").write_text("VERSION-2\n", encoding="utf-8")
    _git("commit", "-am", "v2", cwd=upstream)
    assert cli.main(["skills", "sync", "--root", str(project), "--refresh"]) == 0
    assert (_checkout(source, env) / "VERSION").read_text() == "VERSION-2\n"
    assert (previous / "VERSION").read_text() == "VERSION-1\n"


def test_skills_sync_loads_an_installed_custom_profile(tmp_path, capsys):
    from agent_flow import cli

    profiles = tmp_path / ".agent-flow" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "my-stack.yaml").write_text(
        "id: my-stack\n"
        "gates: []\n"
        "skill_sources:\n"
        "  - id: probe\n"
        "    kind: host-managed\n"
        "    install_hint: managed externally\n",
        encoding="utf-8",
    )
    (tmp_path / ".agent-flow" / "kit.json").write_text(
        '{"profiles":["my-stack"]}\n',
        encoding="utf-8",
    )

    assert cli.main(["skills", "sync", "--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == "my-stack: probe skipped managed externally"


def test_failed_refresh_preserves_last_published_checkout(tmp_path, source, upstream):
    env = _env(tmp_path)
    assert sync_skill_sources([source], env=env)[0].status == "fetched"
    previous = _checkout(source, env)
    hidden = upstream.with_name("temporarily-unavailable")
    upstream.rename(hidden)
    try:
        failed = sync_skill_sources([source], env=env, refresh=True)[0]
    finally:
        hidden.rename(upstream)
    assert failed.status == "failed"
    assert _checkout(source, env) == previous
    assert (previous / "VERSION").read_text() == "VERSION-1\n"


def test_same_source_id_and_ref_do_not_reuse_a_different_url(tmp_path, source, upstream):
    env = _env(tmp_path)
    assert sync_skill_sources([source], env=env)[0].status == "fetched"
    previous = _checkout(source, env)
    other = tmp_path / "other-upstream"
    _git("clone", str(upstream), str(other), cwd=tmp_path)
    changed = replace(source, url=str(other))
    assert cached_source_checkout(changed, env=env) is None
    assert sync_skill_sources([changed], env=env)[0].status == "fetched"
    assert _checkout(changed, env) != previous
    assert _checkout(source, env) == previous


def test_concurrent_refresh_never_exposes_staging_or_mutates_active_reader(tmp_path, source, upstream, monkeypatch):
    from agent_flow.core import skill_sync

    env = _env(tmp_path)
    assert sync_skill_sources([source], env=env)[0].status == "fetched"
    previous = _checkout(source, env)
    (upstream / "VERSION").write_text("VERSION-2\n", encoding="utf-8")
    _git("commit", "-am", "v2", cwd=upstream)
    entered = Event()
    release = Event()
    original_git = skill_sync.git_safe

    def pause_clone(*args, **kwargs):
        result = original_git(*args, **kwargs)
        if args[0] == "clone":
            entered.set()
            assert release.wait(timeout=20)
        return result

    monkeypatch.setattr(skill_sync, "git_safe", pause_clone)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(sync_skill_sources, [source], env=env, refresh=True)
        try:
            assert entered.wait(timeout=20)
            second = pool.submit(sync_skill_sources, [source], env=env, refresh=True)
            assert _checkout(source, env) == previous
            assert (previous / "VERSION").read_text() == "VERSION-1\n"
        finally:
            release.set()
        assert first.result(timeout=30)[0].status == "fetched"
        assert second.result(timeout=30)[0].status == "fetched"
    assert (_checkout(source, env) / "VERSION").read_text() == "VERSION-2\n"
    assert (previous / "VERSION").read_text() == "VERSION-1\n"


def test_resolver_ignores_unpublished_legacy_source_directory(tmp_path, source):
    from agent_flow.core.skill_resolver import skill_roots

    env = _env(tmp_path)
    legacy = cache_root(env) / source.id / source.ref / "demo"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
    configured = replace(source, layout="{skill}/SKILL.md")
    roots = skill_roots(tmp_path, profile={"skill_sources": [asdict(configured)]}, env=env)
    assert not any(root.source == "fetched" for root in roots)
    assert sync_skill_sources([configured], env=env)[0].status == "fetched"
    roots = skill_roots(tmp_path, profile={"skill_sources": [asdict(configured)]}, env=env)
    fetched = [root for root in roots if root.source == "fetched"]
    assert len(fetched) == 1
    assert fetched[0].template == str(_checkout(configured, env) / configured.layout)
