"""phase가 이미 보여 준 required 목록의 기록.

프롬프트는 phase 시작에, 게이트는 phase 끝에 계산한다. 그 사이 agent의 편집으로
required가 자라면 게이트가 **보여 준 적 없는 이름**을 요구했다. 여기 테스트는 그
요구가 나가지 않는 것과, 자람을 알린 되풀이가 끝나는 것을 고정한다.
"""
from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = KIT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent_flow import runner as runner_module
from agent_flow.artifact import read_meta, write_meta
from agent_flow.core.phase_workflow import load_phase_workflow_definition
from agent_flow.core.skill_scope import (
    SCOPE_KEY,
    merge_scope,
    scope_names,
    scope_revision,
)
from agent_flow.runner import ResumeMode, Runner, _phases_from_definition

ARCHITECTURE_SKILL = "probe-architecture"
LAYER_PATH = "src/core/domain/Order.kt"


def _profile() -> dict:
    return {
        "id": "probe",
        "skills": {
            "required_review": [
                {
                    "group": "architecture",
                    "skills": [ARCHITECTURE_SKILL],
                    "when": "layer paths change",
                    "missing": "missing local profile: <skill>",
                    "path_globs": ["**/core/domain/**"],
                }
            ]
        },
    }


def _implement_phase():
    definition = load_phase_workflow_definition(KIT_ROOT, "development")
    for phase in _phases_from_definition(definition):
        if phase.id == "implement":
            return phase
    raise AssertionError("development workflow lost its implement phase")


def _runner(tmp_path: Path, monkeypatch, changed: list[str]) -> Runner:
    # 설치된 skill 카탈로그는 이 머신의 HOME을 훑는다. 빈 HOME으로 고정하지 않으면
    # 결과가 실행하는 사람마다 달라진다.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = tmp_path / "proj"
    run_dir = project / ".agent-flow" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    write_meta(run_dir, {"task": "레이어 경계 정리"})
    monkeypatch.setattr(runner_module, "changed_files", lambda root: tuple(changed))

    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.config_root = project
    runner.project_root = project
    runner.profile = _profile()
    return runner


def test_the_first_capture_is_not_growth():
    """반증: 시작 목록을 자람으로 보고하면 모든 phase가 한 번씩 헛되게 막힌다."""
    meta: dict = {}

    added = merge_scope(meta, "implement", ["a", "b"])

    assert added == ()
    assert scope_names(meta, "implement") == ("a", "b")
    assert scope_revision(meta, "implement") == 1


def test_a_new_name_is_reported_once_and_then_belongs_to_the_scope():
    """반증: 알린 이름을 기록하지 않으면 같은 자람을 매 라운드 다시 보고한다 —
    재진입이 끝나지 않고 phase가 영원히 막힌다."""
    meta: dict = {}
    merge_scope(meta, "implement", ["a"])

    first = merge_scope(meta, "implement", ["a", "b"])
    second = merge_scope(meta, "implement", ["a", "b"])

    assert first == ("b",)
    assert second == ()
    assert scope_revision(meta, "implement") == 2


def test_a_shrinking_scope_keeps_the_names_the_prompt_already_showed():
    """이 phase 안에서는 grow-only다. 줄어든 목록으로 기록을 덮으면 다시 자랄 때
    이미 알린 이름을 또 알린다."""
    meta: dict = {}
    merge_scope(meta, "implement", ["a", "b"])

    added = merge_scope(meta, "implement", ["a"])

    assert added == ()
    assert scope_names(meta, "implement") == ("a", "b")


def test_the_record_is_phase_local():
    """반증: run 전체로 grow-only하면 scope가 정당하게 줄어든 다음 phase에서
    이전 phase의 이름을 요구한다 — 이 모듈이 막으려는 바로 그 상태다."""
    meta: dict = {}
    merge_scope(meta, "implement", ["a"])

    added = merge_scope(meta, "review", ["b"])

    assert added == ()
    assert scope_names(meta, "review") == ("b",)
    assert scope_names(meta, "implement") == ()


def test_an_unknown_record_version_is_recaptured_instead_of_reported():
    """형식이 바뀐 기록을 자람으로 보고하면 진행 중인 run이 근거 없이 막힌다."""
    meta = {
        SCOPE_KEY: {
            "version": 99,
            "phase_id": "implement",
            "revision": 7,
            "names": ["a"],
        }
    }

    added = merge_scope(meta, "implement", ["a", "b"])

    assert added == ()
    assert scope_revision(meta, "implement") == 1
    assert scope_names(meta, "implement") == ("a", "b")


def test_a_file_the_agent_created_mid_phase_re_enters_instead_of_being_demanded(
    tmp_path: Path, monkeypatch
):
    """이 기록이 있는 이유. agent가 phase 안에서 계층 경로에 파일을 만들면
    required가 자라는데, 그 이름을 게이트가 바로 요구하면 프롬프트가 보여 준 적
    없는 skill을 적으라는 요구가 된다."""
    changed: list[str] = []
    runner = _runner(tmp_path, monkeypatch, changed)
    phase = _implement_phase()

    entry = runner._grown_skill_names(phase)
    changed.append(LAYER_PATH)
    grown = runner._grown_skill_names(phase)
    settled = runner._grown_skill_names(phase)

    assert entry == ()
    assert grown == (ARCHITECTURE_SKILL,)
    # 되풀이는 끝난다. 알린 이름은 기록에 들어가고 다음 라운드의 요구는 정당해진다.
    assert settled == ()
    assert ARCHITECTURE_SKILL in scope_names(read_meta(runner.run_dir), phase.id)


def test_a_phase_that_does_not_enforce_markers_never_re_enters(
    tmp_path: Path, monkeypatch
):
    """반증: 강제하지 않는 phase에서 자람으로 막으면 아무도 요구하지 않은 것
    때문에 막히는 자리가 생긴다."""
    changed: list[str] = []
    runner = _runner(tmp_path, monkeypatch, changed)
    phase = _implement_phase()
    commit = dataclasses.replace(phase, id="commit")

    runner._grown_skill_names(commit)
    changed.append(LAYER_PATH)

    assert runner._grown_skill_names(commit) == ()
    assert read_meta(runner.run_dir).get(SCOPE_KEY) is None


def _git_project(tmp_path: Path) -> Path:
    """실제 저장소. `changed_files`가 git status를 읽으므로 여기서 가짜를 쓰면
    이 테스트가 검증하려는 배선의 입력이 사라진다."""
    project = tmp_path / "repo"
    project.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    return project


def test_the_resume_gate_reports_the_growth_instead_of_demanding_it(
    tmp_path: Path, monkeypatch, capsys
):
    """배선 확인: 게이트 자리에 들어가 있어야 blocked 사유가 자람으로 나온다.

    phase 안에서 계층 경로 파일이 생기면, 다음 게이트는 marker를 요구하는 대신
    자란 이름을 알리고 같은 phase를 다시 연다.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLOW_ADAPTER", "generic")
    monkeypatch.delenv("AGENT_FLOW_GENERIC_MODE", raising=False)
    monkeypatch.setattr(
        runner_module, "assert_managed_hooks_registered", lambda *a, **k: None
    )
    project = _git_project(tmp_path)
    phase = _implement_phase()

    def runner(run_dir: Path | None = None) -> Runner:
        instance = Runner(project, run_dir=run_dir, workflow="development")
        instance.phases = [phase]
        instance.profile = _profile()
        return instance

    started = runner()
    started.run(ResumeMode.START, task="레이어 경계 정리")
    run_dir = started.run_dir
    assert run_dir is not None
    artifact = run_dir / (phase.artifact or f"{phase.id}.md")
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("# implement\n\nedited\n", encoding="utf-8")
    layer = project / "src" / "core" / "domain"
    layer.mkdir(parents=True)
    (layer / "Order.kt").write_text("class Order\n", encoding="utf-8")
    capsys.readouterr()

    runner(run_dir).run(ResumeMode.RESUME)
    reported = capsys.readouterr().out

    assert "reason: skill_scope_grew" in reported
    assert ARCHITECTURE_SKILL in reported
    # 요구는 이 라운드에 나가지 않는다. 프롬프트가 아직 보여 준 적 없는 이름이다.
    assert "missing_completion_markers" not in reported


def test_every_lifecycle_command_accepts_a_declared_concern():
    """반증: 전달부가 `getattr`로 감싸여 있으면 옵션 누락이 조용히 빈 값이 된다.

    실측: `start`가 이 선언을 빼먹어 `agent-flow start … --concern <id>`가 argparse
    unknown-option으로 exit 2였고, 그 lifecycle에서는 concern이 언제나 비어 있었다.
    parser를 직접 보므로 run을 만들지 않고도 세 곳의 어긋남을 잡는다.
    """
    import argparse

    from agent_flow.cli import _add_concern_option

    parsed = {}
    for command in ("run", "start", "continue"):
        parser = argparse.ArgumentParser(prog=command)
        _add_concern_option(parser)
        parsed[command] = parser.parse_args(
            ["--concern", "architecture", "--concern", "security"]
        ).concerns

    assert parsed == {
        command: ["architecture", "security"]
        for command in ("run", "start", "continue")
    }


def test_the_shipped_cli_registers_the_concern_option_on_every_lifecycle_command(
    capsys,
):
    """위 테스트는 helper만 본다. 실제 CLI가 그 helper를 세 subparser에 붙였는지는
    출하되는 parser를 봐야 드러난다 — 누락됐던 곳이 바로 `start`다."""
    import pytest

    from agent_flow.cli import main

    for command in ("run", "start", "continue"):
        with pytest.raises(SystemExit) as caught:
            main([command, "--help"])
        assert caught.value.code == 0, command
        assert "--concern" in capsys.readouterr().out, command


HOST_SCOPED_SKILL = "probe-host-skill"


def _host_scoped_profile() -> dict:
    return {
        "id": "probe",
        "skills": {
            "required_review": [
                {
                    "group": "profile",
                    "skills": [HOST_SCOPED_SKILL],
                    "when": "kotlin files change",
                    "missing": "missing local profile: <skill>",
                    "path_globs": ["**/*.kt"],
                }
            ]
        },
    }


def test_each_reviewer_prompt_resolves_skills_against_its_own_host(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_flow import multi_review
    from agent_flow.adapters import hosted
    from agent_flow.cli_detect import CliInfo

    home = tmp_path / "home"
    installed = home / ".claude" / "skills" / HOST_SCOPED_SKILL / "SKILL.md"
    installed.parent.mkdir(parents=True)
    installed.write_text(
        f"---\nname: {HOST_SCOPED_SKILL}\ndescription: probe skill.\n---\n\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "proj"
    (project / ".agent-flow").mkdir(parents=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("AGENT_FLOW_HOST", "omp")
    adapter = hosted.HostedAdapter("omp")
    adapter._profile_snapshot = _host_scoped_profile()
    adapter._changed_files = ("app/src/main/Main.kt",)
    phase = SimpleNamespace(
        id="review",
        description="d",
        prompt="p",
        artifact=None,
        multi_review=True,
        required_markers=(),
        skills=None,
    )

    monkeypatch.setattr(
        multi_review,
        "detect_available_clis",
        lambda: [
            CliInfo("claude", ("claude",), ("-p",)),
            CliInfo("codex", ("codex",), ("exec",)),
        ],
    )
    monkeypatch.delenv("AGENT_FLOW_REVIEWERS", raising=False)
    jobs = hosted._reviewer_jobs(phase, run_dir, project, adapter)

    assert jobs
    claude_prompt = jobs[0].prompt_for("claude")
    codex_prompt = jobs[0].prompt_for("codex")
    assert str(installed) in claude_prompt
    assert "Not installed for this host" not in claude_prompt
    assert str(installed) not in codex_prompt
    assert f"Not installed for this host: {HOST_SCOPED_SKILL}" in codex_prompt
    assert "never make it a verdict" in codex_prompt
    controller_prompt = adapter.render_envelope(
        phase, run_dir, project, prompt_variant="probe-controller"
    )
    assert str(installed) not in controller_prompt
    monkeypatch.setattr(
        multi_review,
        "detect_available_clis",
        lambda: [CliInfo("codex", ("codex",), ("exec",))],
    )
    monkeypatch.delenv("AGENT_FLOW_REVIEWERS", raising=False)
    distribution = multi_review.distribute(jobs, host="codex", phase_id="review")
    bound = distribution.by_cli["codex"][0]
    assert bound.prompt == codex_prompt
    assert not bound.prompt_by_provider
    monkeypatch.setattr(
        multi_review,
        "detect_available_clis",
        lambda: [
            CliInfo("claude", ("claude",), ("-p",)),
            CliInfo("codex", ("codex",), ("exec",)),
        ],
    )
    fanout = multi_review.distribute(jobs, host="claude", phase_id="review")
    assert fanout.by_cli["claude"][0].prompt == claude_prompt
    codex_extra = fanout.by_cli["codex"][0]
    assert codex_extra.prompt == codex_prompt
    assert codex_extra.angle_id.endswith("-codex-extra")
    assert not codex_extra.prompt_by_provider


def test_phase_declared_skill_is_also_scoped_to_the_reviewer_host(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_flow.adapters import hosted
    from agent_flow.core.skill_resolver import PhaseSkills

    home = tmp_path / "home"
    installed = home / ".claude" / "skills" / HOST_SCOPED_SKILL / "SKILL.md"
    installed.parent.mkdir(parents=True)
    installed.write_text(
        f"---\nname: {HOST_SCOPED_SKILL}\ndescription: phase skill.\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "proj"
    project.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    adapter = hosted.HostedAdapter("omp")
    phase = SimpleNamespace(
        id="review",
        description="d",
        prompt="p",
        artifact=None,
        multi_review=True,
        required_markers=(),
        skills=PhaseSkills(required=(HOST_SCOPED_SKILL,)),
    )

    job = hosted._reviewer_jobs(phase, run_dir, project, adapter)[0]

    assert str(installed) in job.prompt_for("claude")
    assert str(installed) not in job.prompt_for("codex")
    assert f"Not installed for this host: {HOST_SCOPED_SKILL}" in job.prompt_for("codex")


def test_frontmatter_catalog_is_scoped_before_external_matching(tmp_path, monkeypatch):
    from agent_flow.core.local_skills import phase_skill_resolution

    home = tmp_path / "home"
    installed = home / ".claude" / "skills" / "probe-external" / "SKILL.md"
    installed.parent.mkdir(parents=True)
    installed.write_text(
        "---\nname: probe-external\ndescription: external probe.\n"
        "workflowPhases: [review]\ntaskTerms: [provider-probe]\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "proj"
    project.mkdir()

    claude = phase_skill_resolution(
        project, "review", task_text="provider-probe", host="claude"
    )
    codex = phase_skill_resolution(
        project, "review", task_text="provider-probe", host="codex"
    )

    assert any(skill.path == installed for skill in (*claude.required, *claude.optional))
    assert all(skill.name != "probe-external" for skill in (*codex.required, *codex.optional))



def test_gated_angles_use_only_eligible_reviewer_providers(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_flow import multi_review
    from agent_flow.adapters import hosted
    from agent_flow.cli_detect import CliInfo

    home = tmp_path / "home"
    installed = home / ".codex" / "skills" / "probe-clean-architecture" / "SKILL.md"
    installed.parent.mkdir(parents=True)
    installed.write_text(
        "---\nname: probe-clean-architecture\ndescription: architecture probe.\n"
        "workflowPhases: [review]\npathGlobs: [\"**/*.kt\"]\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    clis = {
        "claude": CliInfo("claude", ("claude",), ("-p",)),
        "codex": CliInfo("codex", ("codex",), ("exec",)),
    }
    monkeypatch.setattr(
        multi_review,
        "detect_available_clis",
        lambda: list(clis.values()),
    )
    monkeypatch.setattr(multi_review, "cli_by_name", clis.get)
    project = tmp_path / "proj"
    project.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    adapter = hosted.HostedAdapter("omp")
    adapter._changed_files = ("app/src/main/Main.kt",)
    phase = SimpleNamespace(
        id="review",
        description="d",
        prompt="p",
        artifact=None,
        multi_review=True,
        required_markers=(),
        skills=None,
    )

    claude_only = hosted._reviewer_jobs(
        phase, run_dir, project, adapter, providers=("claude",)
    )
    codex_only = hosted._reviewer_jobs(
        phase, run_dir, project, adapter, providers=("codex",)
    )

    assert "clean-architecture" not in {job.angle_id for job in claude_only}
    assert "clean-architecture" in {job.angle_id for job in codex_only}