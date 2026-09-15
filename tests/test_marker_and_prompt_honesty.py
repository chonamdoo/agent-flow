"""마커 값 검사와 프롬프트 사실성 (#100 P6·P7·P8·P9).

이 네 항목의 공통점은 **약속과 강제가 어긋나 있었다**는 것이다. 프롬프트는
게이트보다 넓게 약속했고, 마커는 빈 값과 자리표시자를 받았고, 유일한 수치 검사
앵글은 dispatch되지 않았고, 환경변수 하나가 마커 검사 전체를 껐다.
"""
from __future__ import annotations

from collections.abc import Iterator
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SRC = str(REPO / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.adapters.generic import STUB_SENTINEL
from agent_flow.core.markers import missing_markers
from agent_flow.core.skill_resolver import (
    PhaseSkills,
    SkillResolution,
    SkillRoot,
    resolve_skill,
    skill_prompt_block,
)


def _gate(body: str) -> str:
    return f"# artifact\n\n## Completion Gate\n\n{body}\n"


@pytest.mark.parametrize("conditional,key", [
    (False, "clean-architecture"), (True, "architecture-contract"),
])
@pytest.mark.parametrize("contract_required", [False, True])
def test_architecture_assessment_preserves_applicability_guards(
    conditional: bool, key: str, contract_required: bool
) -> None:
    from agent_flow.core.markers import missing_architecture_assessment_markers

    text = _gate(f"{key}: n/a\nmust-avoid-check: n/a")
    assert missing_architecture_assessment_markers(
        text, contract_required=contract_required, conditional=conditional,
    ) == ([f"{key}: applied", "must-avoid-check: pass|fail"] if contract_required else [])
    assert missing_architecture_assessment_markers(
        _gate(f"{key}: applied\nmust-avoid-check: pass"),
        contract_required=contract_required, conditional=conditional,
    ) == []


def test_legacy_assessment_guard_does_not_alias_new_marker_names() -> None:
    from agent_flow.core.markers import missing_architecture_assessment_markers

    assert missing_architecture_assessment_markers(
        _gate("architecture-contract: n/a"), contract_required=True,
    ) == []
    assert missing_architecture_assessment_markers(
        _gate("clean-architecture: n/a"), contract_required=True,
    ) == ["clean-architecture: applied"]



@pytest.mark.parametrize("mode", ["clean", "local", "pending"])
@pytest.mark.parametrize("role", ["author", "reviewer"])
def test_envelope_uses_selected_conditional_marker_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, role: str
) -> None:
    from agent_flow.adapters.generic import GenericAdapter
    from agent_flow.core.phase_workflow import parse_phase_workflow_definition
    from tests.test_architecture_selection import (
        _clean_contract, _declare, _git_project, _local_contract,
    )

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = _git_project(tmp_path)
    if mode == "clean":
        _clean_contract(project)
    elif mode == "local":
        _local_contract(project)
    else:
        _declare(project, "schema_version: 1\narchitecture:\n  mode: pending\n")
    definition = parse_phase_workflow_definition(
        b"id: custom\nphases:\n  - id: implement\n"
        b"    required_markers: ['architecture-contract: applied|n/a']\n"
        b"    required_markers_by_architecture:\n"
        b"      clean: ['repository-boundary: pass|fail']\n",
        source=Path("custom.yaml"), name="custom",
    )
    run_dir = project / "run"
    run_dir.mkdir()

    envelope = GenericAdapter().render_envelope(
        definition.phases[0], run_dir, project, role=role,
    )

    assert "- `architecture-contract: applied|n/a`" in envelope
    assert ("- `repository-boundary: pass|fail`" in envelope) is (mode == "clean")
    assert "- `clean-architecture: applied|n/a`" not in envelope



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


def test_subheading_does_not_end_the_completion_gate():
    """하위 heading은 섹션 경계가 아니다.

    `### Notes` 한 줄에서 수집을 끊으면 그 아래 마커가 artifact에 있는데도
    missing으로 잡힌다. 사용자에게는 다 써 넣은 문서가 이유 없이 막히는 것으로만
    보이고, 원인이 본문이 아니라 heading 깊이라는 단서는 어디에도 없다.
    """
    text = "## Completion Gate\ncache-required: yes\n### Notes\ndesign-values: latency-first\n"
    assert missing_markers(text, ("cache-required: yes|no", "design-values:")) == []


def test_same_level_heading_still_ends_the_completion_gate():
    text = "## Completion Gate\ncache-required: yes\n## Next Section\ndesign-values: latency-first\n"
    assert missing_markers(text, ("design-values:",)) == ["design-values:"]


def test_list_continuation_line_is_not_mistaken_for_a_code_block():
    """리스트 항목에 딸린 들여쓴 줄은 indented code block이 아니다.

    통째로 버리면 마커를 리스트 밑에 적은 artifact가 다 써 넣고도 missing으로
    막힌다. 원인이 본문이 아니라 들여쓰기 폭이라는 단서는 어디에도 없다.
    """
    text = "## Completion Gate\n- group\n    cache-required: yes\n"
    assert missing_markers(text, ("cache-required: yes|no",)) == []


def test_indented_code_block_is_still_not_a_marker():
    """빈 줄 뒤의 4칸 들여쓰기는 코드다. 예시가 게이트를 통과시키면 안 된다."""
    text = "## Completion Gate\nexample follows:\n\n    cache-required: yes\n"
    assert missing_markers(text, ("cache-required: yes|no",)) == ["cache-required: yes|no"]


def test_paragraph_continuation_line_is_not_a_code_block():
    """문단에 이어지는 들여쓴 줄은 코드가 아니다 — lazy continuation이다."""
    text = "## Completion Gate\nintro\n    cache-required: yes\n"
    assert missing_markers(text, ("cache-required: yes|no",)) == []


def test_indented_line_right_after_the_gate_heading_is_a_code_block():
    """반증: 빈 줄만 조건으로 삼으면 heading 바로 뒤의 예시가 게이트를 통과했다.

    heading은 문단이 아니므로 그 다음 줄의 4칸 들여쓰기는 CommonMark에서 코드
    블록을 연다. 이 줄을 본문으로 읽으면 marker를 하나도 안 쓴 artifact가
    코드 예시만으로 완료 판정을 받는다.
    """
    text = "## Completion Gate\n    cache-required: yes\n"
    assert missing_markers(text, ("cache-required: yes|no",)) == ["cache-required: yes|no"]


def test_indented_line_right_after_a_thematic_break_is_a_code_block():
    """thematic break도 문단이 아니다. 같은 규칙이 적용된다."""
    text = "## Completion Gate\n\nintro\n\n---\n    cache-required: yes\n"
    assert missing_markers(text, ("cache-required: yes|no",)) == ["cache-required: yes|no"]


def test_indented_line_right_after_a_setext_underline_is_a_code_block():
    """setext underline은 문단을 닫는다. 그 다음 들여쓴 줄은 코드다."""
    text = "## Completion Gate\n\nintro\n===\n    cache-required: yes\n"
    assert missing_markers(text, ("cache-required: yes|no",)) == ["cache-required: yes|no"]


def test_underline_without_an_open_paragraph_is_just_a_paragraph():
    """반증: 상태를 안 보고 `===`를 문단 종료로 읽으면 정상 marker가 코드가 된다.

    setext underline은 바로 위에 열린 문단이 있을 때만 성립한다. 빈 줄 뒤의
    `===`는 그냥 문단이므로 다음 줄은 lazy continuation이다.
    """
    text = "## Completion Gate\n\n===\n    cache-required: yes\n"
    assert missing_markers(text, ("cache-required: yes|no",)) == []


def test_a_fence_line_closes_an_open_indented_code_block():
    """fence opener는 3칸까지만 들여쓸 수 있어 열려 있던 indented code를 끝낸다.

    닫지 않으면 그 뒤의 들여쓴 marker가 코드로 버려져 정상 artifact가 막힌다.
    """
    text = "## Completion Gate\n\n    code\n```x```\n    cache-required: yes\n"
    assert missing_markers(text, ("cache-required: yes|no",)) == []


def test_indented_code_block_ends_at_the_first_unindented_line():
    text = "## Completion Gate\n\n    code line\ncache-required: yes\n"
    assert missing_markers(text, ("cache-required: yes|no",)) == []


def test_indented_gate_heading_does_not_open_the_gate():
    """반증: 코드/본문만 가르면 문단에 들여쓴 `## Completion Gate`가 게이트를 연다.

    빈 줄이 없으면 코드 블록이 아니지만, 4칸 들여쓴 줄은 CommonMark에서 heading이
    될 수도 없다. 두 판정은 별개다 — 이걸 합치면 문단 안의 예시가 게이트를 통과시킨다.
    """
    text = "notes\n    ## Completion Gate\n    cache-required: yes\n"
    assert missing_markers(text, ("cache-required: yes|no",)) == ["cache-required: yes|no"]


# --- P6 -----------------------------------------------------------------


def _resolution() -> SkillResolution:
    # 손으로 만든 ResolvedSkill은 resolve_skill이 채우는 필드를 못 담는다. 실제
    # 해석 경로로 만들어야 fixture가 계약과 갈라지지 않는다.
    root = SkillRoot(source="bundled", template=str(REPO / "skills" / "{skill}" / "SKILL.md"))
    return SkillResolution(required=(resolve_skill("tdd", (root,)),), optional=())


def test_enforced_phase_prompt_names_the_marker_the_gate_requires():
    block = skill_prompt_block(REPO, _resolution(), enforced=True)
    assert "skill-use-evidence" in block


def test_ungated_phase_prompt_does_not_promise_enforcement():
    """게이트 없는 phase의 "Read every one of these"는 거짓 약속이다."""
    block = skill_prompt_block(REPO, _resolution(), enforced=False)
    assert "no skill read gate" in block
    assert "skill-use-evidence" not in block


def test_enforced_phase_prompt_does_not_claim_observed_reads_are_enforced():
    """반증: 관측 강제를 자기신고로 낮췄는데 프롬프트가 그 약속을 그대로 남겼다.

    "listed skill exists on disk and was never opened" 류의 문장은 이제 거짓이다.
    아래 `test_the_prompt_claim_survives_only_while_the_gate_backs_it`이 행동과
    엮어 지키고, 이 테스트는 되돌아온 옛 문구를 이름으로 잡는다.
    """
    block = skill_prompt_block(REPO, _resolution(), enforced=True)
    assert "never opened" not in block
    assert "was never" not in block


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
    """불변: 프롬프트의 약속과 실제 강제 조건이 같은 값을 쓴다.

    약속의 내용은 "`skill-use-evidence`를 적어야 통과한다"다. 그러니 프롬프트가
    그 이름을 대는지와 게이트가 그 이름으로 막는지를 같은 값으로 묶는다.
    """
    from agent_flow.core.local_skills import local_skill_prompt_block, missing_local_skill_markers

    profile = {"skills": {"required_review": ["tdd"]}}
    phase_skills = PhaseSkills(required=("tdd",))
    block = local_skill_prompt_block(REPO, phase_id, phase_skills=phase_skills, profile=profile)
    missing = missing_local_skill_markers(
        "", REPO, phase_id, phase_skills=phase_skills, profile=profile
    )
    assert bool(missing) is enforced
    assert any(item.startswith("skill-use-evidence") for item in missing) is enforced
    if not block:
        pytest.skip("no skills resolved for this phase")
    assert ("skill-use-evidence" in block) is enforced


def _project_with_one_installed_skill(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    skill = root / "skills" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: alpha\ndescription: Alpha rules.\n---\n# alpha\n", encoding="utf-8")
    return root


def test_the_prompt_claim_survives_only_while_the_gate_backs_it(tmp_path):
    """불변: 프롬프트 문장과 게이트의 실제 행동을 한 단언 안에 묶는다.

    반증: 문자열만 검사하면 강제를 관측 기반에서 자기신고로 낮춘 뒤에도
    "a listed skill exists on disk and was never opened during it" 문장이 남아
    통과했다. 여기서는 **디스크에 있는 required skill을 한 번도 열지 않은** 상태를
    실제로 만들고 자기신고만 적는다. 게이트가 그것을 통과시키면 프롬프트는 관측
    강제를 말할 수 없고, 막으면 말해야 한다.
    """
    from agent_flow.core.local_skills import (
        SKILLS_READ_LOG,
        local_skill_prompt_block,
        missing_local_skill_markers,
    )

    root = _project_with_one_installed_skill(tmp_path)
    # 로그는 읽히지만 이 skill 기록은 없다 — 관측으로 막던 시절의 차단 조건이다.
    log = root / SKILLS_READ_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"path": "/elsewhere/skills/other/SKILL.md", "at": 1.0}\n', encoding="utf-8")
    phase_skills = PhaseSkills(required=("alpha",))
    gate = (
        "## Completion Gate\n"
        "skill-availability: pass\n"
        "skill-use-evidence: verified\n"
        "project-local-skills: checked\n"
        "project-local-skills-used: alpha\n"
        "project-local-skill-docs: applied\n"
        "missing-required-profile-skills: none\n"
    )
    missing = missing_local_skill_markers(
        gate, root, "implement", phase_skills=phase_skills, profile={}
    )
    block = local_skill_prompt_block(root, "implement", phase_skills=phase_skills, profile={})

    assert "- `alpha`" in block, block
    blocks_unopened_skills = any(
        item.startswith("skill-use-evidence") for item in missing
    )
    claims_it_checks_reads = (
        "never opened" in block
        or "does check which files you actually opened" in block
        or "verifies that you opened" in block
    )
    assert claims_it_checks_reads is blocks_unopened_skills, (missing, block)
    # 양성 축: 낮춘 계약을 프롬프트가 실제로 말하고 있는지.
    assert not blocks_unopened_skills
    assert "takes your word for it" in block
    assert "does not check which files you actually opened" in block


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
    from agent_flow.artifact import create_run
    from agent_flow.runner import Phase, Runner

    project = tmp_path / "proj"
    (project / ".agent-flow").mkdir(parents=True)
    (project / ".agent-flow" / "kit.json").write_text('{"profile": "generic"}', encoding="utf-8")
    run_dir = create_run(project, "default", "Check authored markers")

    monkeypatch.setenv("AGENT_FLOW_GENERIC_MODE", "stub-success")
    runner = Runner(project_root=project, run_dir=run_dir)
    runner._adapter_name = "generic"
    phase = Phase(id="implement", description="d", required_markers=("clean-architecture: applied",))

    (run_dir / "implement.md").write_text("# implement\n\nno gate here\n", encoding="utf-8")
    # 이 phase는 test 실행 증거 게이트 대상이기도 하다. 여기서 검사하는 것은
    # "선언한 마커가 여전히 강제되는가" 하나이므로 목록 전체를 고정하지 않는다.
    assert "clean-architecture: applied" in runner._missing_required_markers(phase)

    (run_dir / "implement.md").write_text(
        f"# implement\n\n<!-- {STUB_SENTINEL} -->\n", encoding="utf-8"
    )
    assert runner._missing_required_markers(phase) == []


def test_stub_sentinel_is_absent_outside_stub_mode(tmp_path, monkeypatch):
    from agent_flow.artifact import create_run
    from agent_flow.runner import Phase, Runner

    project = tmp_path / "proj"
    (project / ".agent-flow").mkdir(parents=True)
    (project / ".agent-flow" / "kit.json").write_text('{"profile": "generic"}', encoding="utf-8")
    run_dir = create_run(project, "default", "Check stub isolation")
    monkeypatch.delenv("AGENT_FLOW_GENERIC_MODE", raising=False)

    runner = Runner(project_root=project, run_dir=run_dir)
    runner._adapter_name = "generic"
    phase = Phase(id="implement", description="d", required_markers=("clean-architecture: applied",))
    (run_dir / "implement.md").write_text(
        f"# implement\n\n<!-- {STUB_SENTINEL} -->\n", encoding="utf-8"
    )
    assert "clean-architecture: applied" in runner._missing_required_markers(phase)


_ANDROID_SELF_REPORT_MARKERS = (
    "android-local-skills",
    "android-local-skills-used",
    "chrisbanes-skills",
    "chrisbanes-skills-used",
)

_SCANNED_SUFFIXES = frozenset({".json", ".md", ".mjs", ".py", ".sh", ".template", ".yaml", ".yml"})
# `.omp`는 install 산출물만 있는 자리다(tracked 파일 0). 되돌린 설치가 남긴 사본이
# 여기 남아 있으면 kit **소스** 검사가 그 사본을 위반으로 보고한다 — `.agent-flow`를
# 건너뛰는 것과 같은 이유다. `.Codex`/`.claude`는 tracked 소스를 함께 담고 있어 뺀다.
_SKIPPED_DIRS = frozenset(
    {".agent-flow", ".git", ".omp", ".pytest_cache", ".venv", "__pycache__", "dist", "node_modules"}
)
# `chrisbanes-skills`는 vendor 설치 디렉터리명으로도 쓰인다. 그 단정은 마커 계약이
# 아니라 설치 경로 계약이라 이 검사 대상이 아니다. 이 파일 자신은 마커 이름을
# 리터럴로 들고 있으므로 자기 자신도 제외한다.
_SKIPPED_FILES = frozenset(
    {"tests/test_custom_skill_install.py", "tests/test_marker_and_prompt_honesty.py"}
)


def _kit_source_lines(root: Path = REPO) -> Iterator[tuple[str, int, str]]:
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if path.suffix not in _SCANNED_SUFFIXES or not path.is_file():
            continue
        if _SKIPPED_DIRS.intersection(rel.parts) or rel.as_posix() in _SKIPPED_FILES:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for number, line in enumerate(lines, 1):
            yield rel.as_posix(), number, line


def test_kit_source_lines_skips_non_utf8_files(tmp_path: Path):
    (tmp_path / "binary.md").write_bytes(b"\xff")

    assert list(_kit_source_lines(tmp_path)) == []


def test_android_self_report_markers_are_gone():
    """죽은 자기신고다. 어떤 workflow `required_markers`도 이 4종을 요구하지 않는다.

    이름 표가 어휘 조인으로 바뀐 뒤 "어느 그룹에서 왔는지"를 산출물에 적게 하는
    계약은 검사되지 않는 신고로만 남았다.
    """
    offenders = [
        f"{rel}:{number}"
        for rel, number, line in _kit_source_lines()
        if any(marker in line for marker in _ANDROID_SELF_REPORT_MARKERS)
    ]
    assert offenders == []
ANDROID_PROFILE = REPO / "src" / "agent_flow" / "profiles" / "android.yaml"
REVIEW_ANGLES = REPO / "templates" / "_shared" / "review"
SKILLS = REPO / "skills"
PRESENTATION_SKILL = SKILLS / "android-clean-presentation-architecture" / "SKILL.md"
SDUI_SKILL = SKILLS / "android-sdui-architecture" / "SKILL.md"


def _android_review_angles() -> dict[str, str]:
    profile = yaml.safe_load(ANDROID_PROFILE.read_text(encoding="utf-8"))
    return {angle["id"]: angle["prompt"] for angle in profile["review_angles"]}


def test_udf_review_angle_is_dispatched():
    """반증: sdui 앵글만 등재돼 비-SDUI 화면의 UDF는 아무도 판정하지 않았다."""
    assert _android_review_angles().get("udf") == "templates/_shared/review/udf.md"
    template = REVIEW_ANGLES / "udf.md"
    assert template.is_file()
    text = template.read_text(encoding="utf-8")
    # 규칙 본문은 skill이 소유한다. 템플릿이 복제하면 원본이 둘이 된다.
    assert "skills/android-clean-presentation-architecture/SKILL.md" in text
    for marker in (
        "udf-architecture: applied",
        "udf-immutable-state-exposure: pass|fail|n/a",
        "udf-explicit-state-modeling: pass|fail|n/a",
        "udf-event-direction: pass|fail|n/a",
        "viewmodel-statein-initial-load: pass|fail|n/a",
        "udf-stateless-content-composable: pass|fail|n/a",
        "udf-route-owns-collection: pass|fail|n/a",
        "udf-uimodel-boundary: pass|fail|n/a",
        "udf-state-holder-purity: pass|fail|n/a",
        "derived-state-precomputed: pass|fail|n/a",
    ):
        assert marker in text


def _frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    body = text.split("---\n", 2)[1]
    parsed = yaml.safe_load(body)
    assert isinstance(parsed, dict)
    return parsed


def test_frontmatter_helper_accepts_crlf(tmp_path: Path):
    skill = tmp_path / "SKILL.md"
    skill.write_bytes(b"---\r\nname: probe\r\n---\r\n")

    assert _frontmatter(skill)["name"] == "probe"


def test_sdui_depends_on_the_presentation_contract():
    """반증: `requires`에 presentation이 없으면 SDUI 세션이 UDF 원문을 읽지 않는다."""
    requirements = _frontmatter(SDUI_SKILL).get("requires") or []
    assert "android-clean-architecture" in requirements
    assert "android-clean-presentation-architecture" in requirements


    sdui_angle = (REVIEW_ANGLES / "sdui.md").read_text(encoding="utf-8")
    assert "sdui-udf-contract: pass|fail|n/a" in sdui_angle
    assert "sdui-udf-contract: pass|fail|n/a" in SDUI_SKILL.read_text(encoding="utf-8")










def test_sdui_completion_marker_values_match_the_evidence_contract():
    template = (REVIEW_ANGLES / "sdui.md").read_text(encoding="utf-8")
    skill = SDUI_SKILL.read_text(encoding="utf-8")

    markers = (
        "sdui-design-token-only", "sdui-room-ssot-scope",
        "sdui-action-finite-vocabulary", "sdui-parse-depth-limit",
        "sdui-unknown-node-fallback", "sdui-list-key-contenttype",
        "sdui-accessibility-field", "sdui-semantic-promotion", "sdui-udf-contract",
    )
    for text in (template, skill):
        assert "sdui-architecture: applied|n/a" in text
        for marker in markers:
            contract = f"{marker}: pass|fail|n/a"
            permits_unverified = marker == "sdui-design-token-only"
            if permits_unverified:
                contract += "|unverified"
            declaration = next(line for line in text.splitlines() if line.startswith(f"{marker}:"))
            assert declaration == contract
            unresolved = missing_markers(_gate(f"{marker}: unverified"), (declaration,))
            assert bool(unresolved) is not permits_unverified








