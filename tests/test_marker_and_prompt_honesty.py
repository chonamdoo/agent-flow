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
    phase_skills = PhaseSkills(required=("tdd",))
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
    # 이 phase는 test 실행 증거 게이트 대상이기도 하다. 여기서 검사하는 것은
    # "선언한 마커가 여전히 강제되는가" 하나이므로 목록 전체를 고정하지 않는다.
    assert "clean-architecture: applied" in runner._missing_required_markers(phase)

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

    presentation = PRESENTATION_SKILL.read_text(encoding="utf-8")
    assert "## Server-Driven Screen Exception" in presentation
    # 이름이 반대로 붙는다는 사실이 예외 절의 핵심이다. 방향으로 판정하게 만든다.
    assert "ScreenEvent" in presentation and "UiEffect" in presentation
    assert presentation.index("## Server-Driven Screen Exception") < presentation.index(
        "## Navigation Rule"
    )

    sdui_angle = (REVIEW_ANGLES / "sdui.md").read_text(encoding="utf-8")
    assert "sdui-udf-contract: pass|fail|n/a" in sdui_angle
    assert "sdui-udf-contract: pass|fail|n/a" in SDUI_SKILL.read_text(encoding="utf-8")

    checklist = (SDUI_SKILL.parent / "references" / "sdui-review-checklist.md").read_text(
        encoding="utf-8"
    )
    item_nine = checklist.split("## 9. `sdui-udf-contract`", 1)[1]
    for evidence_rule in ("Source:", "Rule:", "How to check:", "Verdict:"):
        assert evidence_rule in item_nine


def test_promoted_presentation_rules_stay_project_neutral():
    """반증: `AppResult<T>`를 올렸다가 존재하지 않는 타입을 요구한 적이 있다."""
    text = PRESENTATION_SKILL.read_text(encoding="utf-8")
    for promoted in (
        "Do not start initial screen-state loading from `init`",
        "`MutableStateFlow` is for ViewModel-owned input and transient transition state",
        "## Derived Display State",
        "## List Item Modeling",
        "## UiModel Stability",
        "Preview and Compose UI tests target the stateless screen/content composable",
    ):
        assert promoted in text
    # 프로젝트 로컬 메커니즘은 승격 대상이 아니다. 올리면 없는 타입을 요구하게 된다.
    for project_local in (
        "launchCatching",
        "CommonErrorNotifier",
        "ViewModelErrorLauncher",
        "AppError",
        "AppFailure",
        "AppResult",
        "TaraeDimensions",
        "TaraeColors",
        "TaraeTypography",
    ):
        assert project_local not in text


def test_viewmodel_dependency_boundary_replaces_the_usecase_only_marker():
    """반증: 조건절 없는 marker를 남겨 두면 use case 3개짜리 저장소가 전면 fail이 된다."""
    clean = (SKILLS / "android-clean-architecture" / "SKILL.md").read_text(encoding="utf-8")
    assert "viewmodel-injects-usecases-only" not in clean
    # 혼합 주입과 도메인 의존 없는 ViewModel도 합법 상태다. 값이 없으면 marker가
    # 조건절 없는 marker와 같은 방식으로 정상 코드를 fail로 만든다.
    assert (
        "viewmodel-dependency-boundary: "
        "usecase|single-context-repository|mixed|no-domain-dependency|fail|n/a" in clean
    )

    presentation = PRESENTATION_SKILL.read_text(encoding="utf-8")
    assert "single context's repository interface" in presentation
    # 조건 셋이 문장에 남아 있어야 "언제 use case인가"가 판정 가능하다.
    assert "two or more contexts" in presentation
    assert "order carries meaning" in presentation

    ssot_sources = (
        SDUI_SKILL,
        SKILLS / "android-sdui-architecture" / "references" / "offline-ssot-data-guide.md",
        SKILLS / "android-sdui-architecture" / "references" / "sdui-review-checklist.md",
        REVIEW_ANGLES / "sdui.md",
    )
    for path in ssot_sources:
        text = path.read_text(encoding="utf-8")
        assert "sdui-room-ssot:" not in text
        assert "Room is the single source of truth" not in text
        # frontmatter description도 같은 단정을 하면 phase 프롬프트 한 줄 요약이
        # 좁혀진 규칙보다 넓은 문장을 먼저 보여 준다.
        assert "Room-as-single-source-of-truth" not in text
    assert "sdui-room-ssot-scope: pass|fail|n/a" in SDUI_SKILL.read_text(encoding="utf-8")
    assert "sdui-room-ssot-scope: pass|fail|n/a" in (REVIEW_ANGLES / "sdui.md").read_text(
        encoding="utf-8"
    )


def test_shared_presentation_contract_marker_is_required_by_architecture_reviews():
    marker = "shared-presentation-contract-placement: pass|fail|n/a"
    for workflow_name, phase_id in (
        ("default.yaml", "final-review"),
        ("full-feature.yaml", "architecture-review"),
    ):
        workflow = yaml.safe_load(
            (REPO / "src" / "agent_flow" / "workflows" / workflow_name).read_text(
                encoding="utf-8"
            )
        )
        phase = next(item for item in workflow["phases"] if item["id"] == phase_id)
        assert marker in phase["required_markers"]
        assert marker in phase["prompt"]

    architecture_skill = (
        SKILLS / "architecture-reviewer" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert marker not in architecture_skill
    assert "Use only the markers supplied by the active phase" in architecture_skill
    assert "do not launch reviewer CLIs yourself" in architecture_skill


def test_sdui_completion_marker_values_match_the_evidence_contract():
    template = (REVIEW_ANGLES / "sdui.md").read_text(encoding="utf-8")
    skill = SDUI_SKILL.read_text(encoding="utf-8")

    for text in (template, skill):
        assert "sdui-architecture: applied|n/a" in text
        assert "sdui-design-token-only: pass|fail|n/a|unverified" in text
    assert template.count("sdui-review-checklist.md") == 1
    assert "mark the completion gate `n/a`" not in template


def test_sdui_ssot_maps_data_failures_before_presentation():
    text = (
        SKILLS / "android-sdui-architecture" / "references" / "offline-ssot-data-guide.md"
    ).read_text(encoding="utf-8")

    assert "ObserveScreenResult.Failure(it.toDomainError())" in text
    assert "runCatching<ObserveScreenResult?>" in text
    assert ".catch { emit(ObserveScreenResult.Failure" not in text
    assert "screenErrorMapper.toUiModel(result.error)" in text
    assert "screenUiMapper.toUiModel(result.screen)" in text
    assert "ScreenUiState.Error(it.message)" not in text


def test_use_case_error_semantics_do_not_cross_the_ui_mapping_boundary():
    """반증: use case가 screen result를 만들면 domain이 UI 표현에 의존한다."""
    sources = (
        SKILLS / "clean-architecture-core" / "SKILL.md",
        SKILLS / "android-clean-architecture" / "SKILL.md",
        PRESENTATION_SKILL,
    )

    for path in sources:
        text = path.read_text(encoding="utf-8")
        assert "domain/business failure semantics" in text
        assert "screen result type" not in text
        assert "screen's result type" not in text
    presentation = PRESENTATION_SKILL.read_text(encoding="utf-8")
    assert "presentation mapper" in presentation


def test_the_three_contradicted_rules_are_reconciled():
    """반증: 두 문서가 같은 어휘로 정반대를 요구하면 리뷰어가 어느 쪽이든 fail을 낼 수 있다."""
    stability = (REVIEW_ANGLES / "compose-stability.md").read_text(encoding="utf-8")
    # 값 읽기는 아래로, flow 수집은 진입 composable에. 둘을 구분하지 않으면 UDF와 충돌한다.
    assert "Narrow the read, never the collection point." in stability
    assert "skills/android-clean-presentation-architecture/SKILL.md" in stability

    presentation = PRESENTATION_SKILL.read_text(encoding="utf-8")
    assert "where acquisition happens, not the call shape" in presentation
    assert "stateful overload" in presentation
    assert "screen/content composables" not in presentation.lower()
    assert "stateless rendering composables" in presentation
    clean = (SKILLS / "android-clean-architecture" / "SKILL.md").read_text(encoding="utf-8")
    assert "screen/content composables" not in clean.lower()
    assert "stateless rendering composables" in clean.lower()
    udf = " ".join((REVIEW_ANGLES / "udf.md").read_text(encoding="utf-8").split())
    assert "screen and content composables do not" not in udf
    assert "content composables below the screen entry do not" in udf

    # 존재하지 않는 타입 요구가 android profile의 다른 앵글로 옮겨가지 않게 함께 본다.
    generalized = (
        SKILLS / "android-guides" / "references" / "architecture-rules-guide.md",
        REVIEW_ANGLES / "android-skills.md",
    )
    for path in generalized:
        text = path.read_text(encoding="utf-8")
        assert "AppResult" not in text
        assert "AppError" not in text
    guide = generalized[0].read_text(encoding="utf-8")
    assert "the project's result type" in guide
    assert "existing `Result`/exception contract" in guide
    angle = generalized[1].read_text(encoding="utf-8")
    assert "transport-failure to domain-error" in angle


def test_presentation_review_marker_has_one_runtime_contract():
    expected = "presentation-state-review: pass|fail|n/a"
    seen = []

    for workflow_path in sorted((REPO / "src" / "agent_flow" / "workflows").glob("*.yaml")):
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        for phase in workflow["phases"]:
            prompt = phase.get("prompt") or ""
            for canned_default in (
                "presentation-skill: n/a",
                "presentation-state-based-development: n/a",
                "presentation-state-review: n/a",
                "ui-state-modeling: n/a",
                "presentation-mapping-boundary: n/a",
                "di-boundary: n/a",
            ):
                assert canned_default not in prompt
            for marker in phase.get("required_markers") or []:
                if marker.startswith("presentation-state-review:"):
                    seen.append((workflow_path.name, phase["id"], marker))
                    assert marker == expected

    assert seen
    for name in (
        "android-clean-presentation-architecture",
        "ios-clean-presentation-architecture",
        "react-clean-presentation-architecture",
        "react-native-clean-presentation-architecture",
    ):
        path = SKILLS / name / "SKILL.md"
        assert "required_markers" not in _frontmatter(path)
        assert f"`{expected}`" in path.read_text(encoding="utf-8")
