from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from agent_flow.core.markers import completion_gate_marker_values
from agent_flow.core.skill_resolver import (
    CODE_PHASES,
    PhaseSkills,
    ResolvedSkill,
    SkillResolution,
    resolve_phase_skills,
    skill_prompt_block,
)

APPLIED_MARKER = "project-local-skill-docs: applied"
AVAILABILITY_MARKER = "skill-availability: pass|degraded"
READ_EVIDENCE_MARKER = "skill-read-evidence: verified|unavailable"

# Read hook이 SKILL.md 읽기를 append-only로 기록하는 파일. O_APPEND라 read-modify-write race가 없다.
SKILLS_READ_LOG = Path(".agent-flow") / "skills-read.jsonl"


@dataclass(frozen=True)
class SkillReadEvidence:
    """L2 관측 결과. hook이 없으면 available=False이고 강제하지 않는다.

    한계를 분명히 해 둔다: 이 기록은 agent가 쓰기 가능한 워크스페이스 안의 평문
    로그다. 서명도 소유권 검증도 없으므로 **위조 불가능한 증명이 아니다.**
    자기신고보다 강한 이유는 하나뿐이다 — 통과하려면 실제 tool 호출을 흉내 내는
    별도 행위가 필요하고, 그 행위가 로그에 남는다. 신뢰 수준을 그 이상으로
    표현하지 마라.
    """

    available: bool
    read_paths: frozenset[str]

    def covers(self, skill: ResolvedSkill) -> bool:
        return skill.path is not None and str(skill.path.resolve()) in self.read_paths


def phase_skill_resolution(
    project_root: Path,
    phase_id: str,
    *,
    phase_skills: PhaseSkills | None = None,
    profile: dict | None = None,
    changed_files: Sequence[str] = (),
    task_text: str = "",
) -> SkillResolution:
    return resolve_phase_skills(
        project_root=project_root,
        phase_id=phase_id,
        phase_skills=phase_skills,
        profile=profile,
        changed_files=changed_files,
        task_text=task_text,
    )


def local_skill_prompt_block(
    project_root: Path,
    phase_id: str,
    *,
    phase_skills: PhaseSkills | None = None,
    profile: dict | None = None,
    changed_files: Sequence[str] = (),
    task_text: str = "",
) -> str:
    resolution = phase_skill_resolution(
        project_root,
        phase_id,
        phase_skills=phase_skills,
        profile=profile,
        changed_files=changed_files,
        task_text=task_text,
    )
    block = skill_prompt_block(project_root, resolution)
    if not block:
        return ""
    return block + _marker_instruction(resolution)


def missing_local_skill_markers(
    text: str,
    project_root: Path,
    phase_id: str,
    *,
    phase_skills: PhaseSkills | None = None,
    profile: dict | None = None,
    changed_files: Sequence[str] = (),
    task_text: str = "",
    since: float | None = None,
) -> list[str]:
    resolution = phase_skill_resolution(
        project_root,
        phase_id,
        phase_skills=phase_skills,
        profile=profile,
        changed_files=changed_files,
        task_text=task_text,
    )
    # skill 목록 주입은 모든 phase에 하지만, marker 강제는 코드 생성/리뷰 phase에만 건다.
    # commit·merge·pr-watch까지 막으면 얻는 것 없이 막히는 경로만 늘어난다.
    if phase_id not in CODE_PHASES or not resolution.required:
        return []
    values = completion_gate_marker_values(text)
    missing: list[str] = []

    # L1: 없는 skill은 위반이 아니다. 설치는 install/skills sync가 1회 처리하고,
    #     런타임은 degraded로 기록만 한다. 런 도중 사용자에게 설치를 묻지 않는다.
    if values.get("skill-availability") not in {"pass", "degraded", "n/a"}:
        missing.append(AVAILABILITY_MARKER)

    # L2: 진짜 강제 지점. 디스크에 있는데 안 읽은 skill만 막는다.
    #     hook이 없는 host는 관측이 불가능하므로 unavailable로 기록만 하고 통과시킨다.
    evidence = read_skill_evidence(project_root, since=since)
    if evidence.available:
        unread = [skill for skill in resolution.available_required if not evidence.covers(skill)]
        if unread:
            missing.append(
                f"skill-read-evidence: verified ({len(unread)} required skill(s) were "
                "never opened during this phase)"
            )
    elif values.get("skill-read-evidence") not in {"verified", "unavailable"}:
        missing.append(READ_EVIDENCE_MARKER)

    # L3: 자기신고는 표시용이다. resolver가 required로 판정하고 실제로 있는 것만 요구한다.
    if values.get("project-local-skills") != "checked":
        missing.append("project-local-skills: checked")
    if resolution.available_required:
        used = values.get("project-local-skills-used", "").strip()
        if used in {"", "n/a", "none", "optional"} or not _mentions_required(used, resolution):
            expected = ", ".join(skill.name for skill in resolution.available_required)
            missing.append(f"project-local-skills-used: {expected}")
        if values.get("project-local-skill-docs") != "applied":
            missing.append(APPLIED_MARKER)
    return missing


def read_skill_evidence(project_root: Path, *, since: float | None = None) -> SkillReadEvidence:
    """Read hook이 남긴 기록을 읽는다. 파일이 없으면 hook 미등록/미지원 host로 본다."""
    log_path = project_root / SKILLS_READ_LOG
    if not log_path.is_file():
        return SkillReadEvidence(available=False, read_paths=frozenset())
    paths: set[str] = set()
    try:
        raw = log_path.read_text(encoding="utf-8")
    except OSError:
        return SkillReadEvidence(available=False, read_paths=frozenset())
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        if since is not None and _entry_timestamp(entry) < since:
            continue
        path = entry.get("path")
        if isinstance(path, str) and path:
            paths.add(path)
    return SkillReadEvidence(available=True, read_paths=frozenset(paths))


def record_skill_read(project_root: Path, skill_path: Path) -> None:
    """hook 쪽에서 호출한다. append-only 한 줄이라 동시 기록이 서로를 덮지 않는다."""
    log_path = project_root / SKILLS_READ_LOG
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = json.dumps(
        {"path": str(skill_path.resolve()), "at": time.time()},
        ensure_ascii=False,
        sort_keys=True,
    )
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(entry + "\n")


def _marker_instruction(resolution: SkillResolution) -> str:
    expected = ", ".join(skill.name for skill in resolution.available_required) or "n/a"
    availability = "degraded" if resolution.missing else "pass"
    return "\n".join(
        [
            "",
            "The `## Completion Gate` must include:",
            "",
            "```text",
            f"skill-availability: {availability}",
            "skill-read-evidence: verified|unavailable",
            "project-local-skills: checked",
            f"project-local-skills-used: {expected}",
            APPLIED_MARKER,
            "```",
            "",
        ]
    )


def _mentions_required(value: str, resolution: SkillResolution) -> bool:
    names = {
        token.strip().lower()
        for token in value.replace(";", ",").split(",")
        if token.strip()
    }
    return all(skill.name.lower() in names for skill in resolution.available_required)


def _entry_timestamp(entry: dict) -> float:
    raw = entry.get("at")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0
