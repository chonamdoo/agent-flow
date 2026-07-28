"""마커 값 검사와 프롬프트 사실성 (#100 P6·P7·P8·P9).

이 네 항목의 공통점은 **약속과 강제가 어긋나 있었다**는 것이다. 프롬프트는
게이트보다 넓게 약속했고, 마커는 빈 값과 자리표시자를 받았고, 유일한 수치 검사
앵글은 dispatch되지 않았고, 환경변수 하나가 마커 검사 전체를 껐다.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SRC = str(REPO / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.adapters.generic import STUB_SENTINEL
from agent_flow.core.markers import missing_markers
from agent_flow.core.skill_resolver import (
    SkillResolution,
    SkillRoot,
    resolve_skill,
    skill_prompt_block,
)


def _gate(body: str) -> str:
    return f"# artifact\n\n## Completion Gate\n\n{body}\n"


# --- P7 -----------------------------------------------------------------


def test_empty_marker_value_is_rejected():
    """반증: 빈 값 검사를 지워도 488개 테스트가 전부 통과했다. 그 구멍을 여기서 막는다."""
    assert missing_markers(_gate("active-profiles:"), ("active-profiles:",)) == ["active-profiles:"]
    assert missing_markers(_gate("active-profiles:   "), ("active-profiles:",)) == ["active-profiles:"]


def test_concrete_marker_value_passes():
    assert missing_markers(_gate("active-profiles: android, python"), ("active-profiles:",)) == []


def test_angle_bracket_placeholder_is_rejected():
    """프롬프트의 틀을 그대로 복사해 붙이면 게이트는 통과하고 값은 없다."""
    text = _gate("cache-invalidation-policy: <policy or n/a>")
    assert missing_markers(text, ("cache-invalidation-policy:",)) == ["cache-invalidation-policy:"]


def test_value_that_merely_contains_angle_brackets_passes():
    """`Map<String, Int>` 같은 진짜 값을 자리표시자로 오인하면 안 된다."""
    text = _gate("solid-isp-consumer-ports: Map<String, Int> port split")
    assert missing_markers(text, ("solid-isp-consumer-ports:",)) == []


def test_enum_marker_still_rejects_illegal_values():
    text = _gate("usecase-interface: checked")
    assert missing_markers(text, ("usecase-interface: required|optional|n/a",)) != []


# --- P6 -----------------------------------------------------------------


def _resolution() -> SkillResolution:
    # 손으로 만든 ResolvedSkill은 resolve_skill이 채우는 필드를 못 담는다. 실제
    # 해석 경로로 만들어야 fixture가 계약과 갈라지지 않는다.
    root = SkillRoot(source="bundled", template=str(REPO / "skills" / "{skill}" / "SKILL.md"))
    return SkillResolution(required=(resolve_skill("tdd", (root,)),), optional=())


def test_enforced_phase_prompt_states_the_gate():
    block = skill_prompt_block(REPO, _resolution(), enforced=True)
    assert "completion gate blocks this phase" in block


def test_ungated_phase_prompt_does_not_promise_enforcement():
    """게이트 없는 phase의 "Read every one of these"는 거짓 약속이다."""
    block = skill_prompt_block(REPO, _resolution(), enforced=False)
    assert "no skill read gate" in block
    assert "completion gate blocks this phase" not in block


def test_prompt_puts_exploration_before_reading():
    """Vercel eval: "먼저 skill을 호출하라"는 문서 패턴에 앵커링돼 프로젝트 컨텍스트를 놓쳤다.

    반증: 순서 힌트가 사라지면 우리는 측정된 열등 문구로 돌아간다.
    """
    for enforced in (True, False):
        block = skill_prompt_block(REPO, _resolution(), enforced=enforced)
        skim = block.find("skim")
        read = block.find("read every one of these" if enforced else "read the ones that actually apply")
        assert 0 <= skim < read, block


def test_prompt_prefers_the_files_over_recalled_knowledge():
    """retrieval-led over pre-training-led. 이 문장이 Vercel eval의 100%를 만들었다."""
    block = skill_prompt_block(REPO, _resolution(), enforced=True)
    assert "Prefer what these files say over what you already know" in block


def test_prompt_lines_carry_a_one_line_summary():
    """이름만 주면 optional의 "scope가 걸리면 읽어라"는 판단 재료가 없는 판단 지점이다."""
    from agent_flow.core.skill_resolver import skill_summary

    summary = skill_summary(REPO / "skills" / "tdd" / "SKILL.md")
    assert summary
    assert summary in skill_prompt_block(REPO, _resolution(), enforced=True)


def test_summary_is_the_first_sentence_not_the_whole_description(tmp_path):
    """반증: 전문을 넣으면 목록이 곧 문서가 된다 — 압축이 목적이다.

    길이 상한과 독립으로 확인한다. 상한이 잘라 준 결과를 압축으로 착각하면
    문장 분리를 no-op으로 바꿔도 테스트가 안 죽는다.
    """
    from agent_flow.core.skill_resolver import skill_summary

    path = tmp_path / "SKILL.md"
    path.write_text(
        "---\nname: probe\ndescription: First sentence. Second sentence that must not appear.\n---\n",
        encoding="utf-8",
    )
    assert skill_summary(path) == "First sentence."


def test_a_sentenceless_description_is_capped(tmp_path):
    """반증: 문장 경계가 없으면 상한만 남는다. 상한이 없으면 한 줄이 문단이 된다."""
    from agent_flow.core.skill_resolver import _SUMMARY_MAX_CHARS, skill_summary

    path = tmp_path / "SKILL.md"
    path.write_text(
        f"---\nname: probe\ndescription: {'word ' * 200}\n---\n",
        encoding="utf-8",
    )
    summary = skill_summary(path)
    assert len(summary) == _SUMMARY_MAX_CHARS
    assert summary.endswith("…")


def test_a_multiline_description_stays_on_one_line(tmp_path):
    """반증: 줄바꿈이 남으면 markdown 목록 항목이 그 자리에서 끊긴다."""
    from agent_flow.core.skill_resolver import skill_summary

    path = tmp_path / "SKILL.md"
    path.write_text(
        "---\nname: probe\ndescription: |\n  First line\n  second line.\n  Tail.\n---\n",
        encoding="utf-8",
    )
    assert skill_summary(path) == "First line second line."


@pytest.mark.parametrize("phase_id,enforced", [("green", True), ("commit", False)])
def test_prompt_enforcement_claim_matches_the_gate(tmp_path, phase_id, enforced):
    """불변: 프롬프트의 약속과 실제 강제 조건이 같은 값을 쓴다."""
    from agent_flow.core.local_skills import local_skill_prompt_block, missing_local_skill_markers

    profile = {"skills": {"required_review": ["tdd"]}}
    phase_skills = type("PhaseSkills", (), {"required": ("tdd",), "optional": ()})()
    block = local_skill_prompt_block(REPO, phase_id, phase_skills=phase_skills, profile=profile)
    gated = bool(
        missing_local_skill_markers("", REPO, phase_id, phase_skills=phase_skills, profile=profile)
    )
    assert gated is enforced
    if not block:
        pytest.skip("no skills resolved for this phase")
    assert ("completion gate blocks this phase" in block) is enforced


# --- P8 -----------------------------------------------------------------


@pytest.mark.parametrize("copy", ["src/agent_flow/profiles"])
def test_sdui_review_angle_is_dispatched(copy):
    """저장소에서 유일한 기계적 수치 검사가 한 번도 실행된 적이 없었다."""
    profile = yaml.safe_load((REPO / copy / "android.yaml").read_text(encoding="utf-8"))
    angles = {angle["id"]: angle["prompt"] for angle in profile["review_angles"]}
    assert angles.get("sdui") == "templates/_shared/review/sdui.md"
    assert (REPO / "templates" / "_shared" / "review" / "sdui.md").is_file()


# --- P9 -----------------------------------------------------------------


def test_stub_mode_does_not_bypass_markers_for_authored_artifacts(tmp_path, monkeypatch):
    """반증: 환경변수 하나가 마커 검사 **전체**를 끄면 그건 전면 킬스위치다."""
    from agent_flow.runner import Phase, Runner

    project = tmp_path / "proj"
    (project / ".agent-flow").mkdir(parents=True)
    (project / ".agent-flow" / "kit.json").write_text('{"profile": "generic"}', encoding="utf-8")
    run_dir = project / ".agent-flow" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text('{"run_id": "r1", "task": "t"}', encoding="utf-8")

    monkeypatch.setenv("AGENT_FLOW_GENERIC_MODE", "stub-success")
    runner = Runner(project_root=project, run_dir=run_dir)
    runner._adapter_name = "generic"
    phase = Phase(id="implement", description="d", required_markers=("clean-architecture: applied",))

    (run_dir / "implement.md").write_text("# implement\n\nno gate here\n", encoding="utf-8")
    assert runner._missing_required_markers(phase) == ["clean-architecture: applied"]

    (run_dir / "implement.md").write_text(
        f"# implement\n\n<!-- {STUB_SENTINEL} -->\n", encoding="utf-8"
    )
    assert runner._missing_required_markers(phase) == []


def test_stub_sentinel_is_absent_outside_stub_mode(tmp_path, monkeypatch):
    from agent_flow.runner import Phase, Runner

    project = tmp_path / "proj"
    (project / ".agent-flow").mkdir(parents=True)
    (project / ".agent-flow" / "kit.json").write_text('{"profile": "generic"}', encoding="utf-8")
    run_dir = project / ".agent-flow" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text('{"run_id": "r1", "task": "t"}', encoding="utf-8")
    monkeypatch.delenv("AGENT_FLOW_GENERIC_MODE", raising=False)

    runner = Runner(project_root=project, run_dir=run_dir)
    runner._adapter_name = "generic"
    phase = Phase(id="implement", description="d", required_markers=("clean-architecture: applied",))
    (run_dir / "implement.md").write_text(
        f"# implement\n\n<!-- {STUB_SENTINEL} -->\n", encoding="utf-8"
    )
    assert runner._missing_required_markers(phase) == ["clean-architecture: applied"]
