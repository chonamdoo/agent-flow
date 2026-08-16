from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from agent_flow.core.markers import completion_gate_marker_values
from agent_flow.core.profiles import active_profile_ids, load_profile_payload
from agent_flow.core.profile_routing import routed_profile_skills
from agent_flow.core.worktree_isolation import git_repo_state, git_safe
from agent_flow.core.skill_resolver import (
    CODE_PHASES,
    PhaseSkills,
    SkillResolution,
    resolve_phase_skills,
    skill_prompt_block,
)

APPLIED_MARKER = "project-local-skill-docs: applied"
AVAILABILITY_MARKER = "skill-availability: pass|degraded"
USE_EVIDENCE_MARKER = "skill-use-evidence: verified|unavailable"

# Read hook이 SKILL.md 읽기를 append-only로 기록하는 파일. O_APPEND라 read-modify-write race가 없다.
SKILLS_READ_LOG = Path(".agent-flow") / "skills-read.jsonl"


def changed_files(project_root: Path) -> tuple[str, ...]:
    """working tree + staged 변경 경로. git이 없거나 실패하면 빈 튜플로 degrade한다."""
    # -uall이 없으면 git이 untracked 디렉터리를 `app/sdui/` 한 줄로 접어서
    # pathGlobs가 파일 경로를 못 본다.
    result = git_safe(
        "status", "--porcelain=v1", "-z", "-uall",
        cwd=project_root,
        timeout_s=10,
        optional_locks=False,
    )
    if not result.ok:
        # 저장소인데 실패한 것과 애초에 저장소가 아닌 것은 다르다. 전자는
        # pathGlobs skill 강제가 통째로 꺼지는 사고이므로 조용히 넘기지 않는다.
        if git_repo_state(project_root) == "repo":
            print(
                "agent-flow: git status 실패로 변경 파일을 못 읽었다. "
                "pathGlobs로 걸리는 skill이 활성화되지 않는다: "
                f"{result.stderr.strip() or result.error or 'git did not answer'}",
                file=sys.stderr,
            )
        return ()
    paths: list[str] = []
    for record in result.stdout.split("\0"):
        if len(record) > 3:
            paths.append(record[3:].replace("\\", "/"))
    return tuple(dict.fromkeys(paths))


def merged_profile_payload(payloads: Sequence[dict]) -> dict:
    """다중 profile 합본. resolver가 보는 키만 이어 붙인다.

    `update`만 하면 뒤 profile이 앞 profile의 `skills.required_review`와 `skills.external`을
    통째로 덮는다. react-native + android처럼 둘 다 활성인 조합에서 한쪽 routing이
    조용히 사라지는 경로가 그것이다. 순서는 `active_profile_ids()`가 준 순서를
    유지하고, domain 이름은 **먼저 온 profile이 이긴다** — detect 우선순위가 곧 소유권이다.
    `required_review` group은 그 규칙을 쓸 수 없다: 6개 profile 전부가 `group: profile`을
    쓰므로 group 이름만으로 dedupe하면 두 번째 profile의 표가 통째로 사라진다. 그래서
    소유 profile id와 group 이름의 쌍으로 dedupe한다.
    """
    merged: dict = {}
    sources: list = []
    groups: list[dict] = []
    seen_groups: set[str] = set()
    domains: list[dict] = []
    seen_domains: set[str] = set()
    external: dict = {}
    for payload in payloads:
        merged.update(payload)
        declared = payload.get("skill_sources")
        if isinstance(declared, list):
            sources.extend(declared)
        skills = payload.get("skills")
        if not isinstance(skills, dict):
            continue
        if isinstance(skills.get("required_review"), list):
            for group in skills["required_review"]:
                if not isinstance(group, dict):
                    continue
                key = f"{payload.get('id', '')}:{group.get('group', '')}"
                if key in seen_groups:
                    continue
                seen_groups.add(key)
                groups.append(group)
        block = skills.get("external")
        if isinstance(block, dict):
            # `enabled`는 합집합이다. RN 프로젝트의 `android/` 변경이 Android 어휘를
            # 함께 받는 경로가 여기다 — 옛 escalation 그룹이 하던 일을 이것이 대신한다.
            external = {**block, **external, "enabled": external.get("enabled") or block.get("enabled")}
            for domain in block.get("domains") or []:
                if not isinstance(domain, dict):
                    continue
                domain_id = str(domain.get("id", ""))
                if domain_id in seen_domains:
                    continue
                seen_domains.add(domain_id)
                domains.append(domain)
    if sources:
        merged["skill_sources"] = sources
    if groups or domains:
        skills = dict(merged.get("skills") or {})
        if groups:
            skills["required_review"] = groups
        if domains:
            skills["external"] = {**external, "domains": domains}
        merged["skills"] = skills
    return merged


def resolved_profile(project_root: Path, requested: str | None = None) -> dict | None:
    """활성 profile을 합친 payload. 호출자가 profile을 안 넘기면 여기가 유일한 출처다.

    실패는 조용히 넘기지 않는다. profile을 못 읽으면 skill_sources가 사라져
    required 집합이 조용히 줄고, 그게 status와 runner를 다시 갈라놓는다.
    """
    try:
        payloads = [
            load_profile_payload(profile_id, project_root)
            for profile_id in active_profile_ids(project_root, requested or "auto")
        ]
    except Exception as exc:  # yaml 오류까지 포함해야 status가 traceback으로 죽지 않는다
        print(f"agent-flow: profile을 읽지 못해 skill 판정이 축소됐다: {exc}", file=sys.stderr)
        return None
    return merged_profile_payload(payloads) if payloads else None


@dataclass(frozen=True)
class SkillReadEvidence:
    """L2 관측 결과. 게이트를 막는 데는 쓰지 않고 진단에만 쓴다.

    한계를 분명히 해 둔다: 이 기록은 agent가 쓰기 가능한 워크스페이스 안의 평문
    로그다. 서명도 소유권 검증도 없으므로 **위조 불가능한 증명이 아니다.** 게다가
    hook이 로드되지 않은 세션에서는 실제 읽기도 기록되지 않는다 — 기록이 없다는
    것은 "안 읽었다"가 아니라 "관측되지 않았다"는 뜻이다. 그래서 이 값으로
    차단하지 않고, 자기신고가 없어 막는 순간의 원인 안내에만 쓴다.

    진단만 쓰므로 개별 skill과 기록을 대조하지 않는다. 그 대조(체크아웃 기준
    상대경로 매칭)는 관측으로 차단하던 L2 가지가 유일한 소비자였고 함께 지웠다.
    """

    # 로그를 읽을 수 있었는가. 강제 여부가 아니라 진단 가능 여부다.
    available: bool
    read_paths: frozenset[str]
    # Skill tool과 `skill://`은 경로를 주지 않는다. 이름만 관측되는 사용 경로다.
    used_names: frozenset[str] = frozenset()


def declared_concern_ids(profile: dict | None) -> set[str]:
    """선언된 concern id 전체. 오타를 조용히 무시하지 않으려면 목록이 하나여야 한다."""
    from agent_flow.core.profile_routing import declared_group_concerns
    from agent_flow.core.skill_matching import declared_domain_ids

    return declared_domain_ids(profile) | declared_group_concerns(profile)


def phase_skill_resolution(
    project_root: Path,
    phase_id: str,
    *,
    phase_skills: PhaseSkills | None = None,
    profile: dict | None = None,
    changed_files: Sequence[str] = (),
    task_text: str = "",
    concerns: Sequence[str] = (),
) -> SkillResolution:
    return resolve_phase_skills(
        project_root=project_root,
        phase_id=phase_id,
        phase_skills=phase_skills,
        profile=profile,
        changed_files=changed_files,
        task_text=task_text,
        concerns=concerns,
    )


def local_skill_prompt_block(
    project_root: Path,
    phase_id: str,
    *,
    phase_skills: PhaseSkills | None = None,
    profile: dict | None = None,
    changed_files: Sequence[str] = (),
    task_text: str = "",
    concerns: Sequence[str] = (),
) -> str:
    resolution = phase_skill_resolution(
        project_root,
        phase_id,
        phase_skills=phase_skills,
        profile=profile,
        changed_files=changed_files,
        task_text=task_text,
        concerns=concerns,
    )
    # 강제 지점과 같은 조건을 쓴다. 둘이 갈라지면 프롬프트가 다시 거짓말한다.
    enforced = phase_id in CODE_PHASES
    block = skill_prompt_block(project_root, resolution, enforced=enforced)
    if not block:
        return ""
    routed_missing = _missing_routed_names(
        phase_id,
        profile=profile,
        changed_files=changed_files,
        task_text=task_text,
        concerns=concerns,
        resolution=resolution,
    )
    return block + _marker_instruction(
        resolution, enforced=enforced, routed_missing=routed_missing
    )


def missing_local_skill_markers(
    text: str,
    project_root: Path,
    phase_id: str,
    *,
    phase_skills: PhaseSkills | None = None,
    profile: dict | None = None,
    changed_files: Sequence[str] = (),
    task_text: str = "",
    concerns: Sequence[str] = (),
    since: float | None = None,
) -> list[str]:
    resolution = phase_skill_resolution(
        project_root,
        phase_id,
        phase_skills=phase_skills,
        profile=profile,
        changed_files=changed_files,
        task_text=task_text,
        concerns=concerns,
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

    # L2: 강제는 자기신고 하나로 한다. 관측은 진단에만 쓴다.
    #
    #     관측으로 강제하면 read hook이 로드되지 않은 host 세션이 영원히 막힌다.
    #     그 세션은 skill을 실제로 읽어도 기록을 남기지 못하는데, 다른 세션이 로그
    #     파일을 한 번이라도 만들어 뒀으면 available=True가 되어 "안 읽었다"로 차단된다.
    #     자기가 만들 수 없는 marker를 요구받는 상태이고, `unavailable`로 자기신고해도
    #     그 가지에는 닿지 못했다. 그래서 관측을 강제 조건에서 뺀다. 로그에서 안 읽은
    #     skill이 보이는 것은 이제 blocker가 아니다.
    #
    #     이 교환의 대가: 읽지 않고 `verified`라고 적으면 이제 통지 없이 통과한다.
    #     그것을 잡아내던 자리는 여기 하나뿐이었다. 남은 것은 "뭐라도 신고하게 하는"
    #     강제와, 막는 순간 붙는 진단뿐이다.
    if values.get("skill-use-evidence") not in {"verified", "unavailable"}:
        missing.append(USE_EVIDENCE_MARKER)
        # 진단은 marker 이름에 붙이지 않는다. 이 리스트는 runner가 "이 marker들을
        # artifact에 적어라"로 그대로 보여 주는 자리이고(`runner.py`가 `', '.join`으로
        # 출력), 이 강제는 값을 열거로 대조한다. `skill-use-evidence: verified|unavailable
        # (…)`을 통째로 옮겨 적으면 값이 열거에 없어 같은 자리에서 다시 막힌다 —
        # 관측으로 막던 base에서는 그 가지가 값을 안 봐서 무해했던 형태다.
        diagnosis = _use_evidence_diagnosis(project_root, since=since)
        if diagnosis:
            missing.append(diagnosis)

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

    # L4: profile 표로 붙었는데 host에 없는 skill. 설치를 강요하지는 않지만,
    #     "없다"는 사실이 artifact에 이름으로 남아야 한다. `none`으로 덮으면
    #     Android scope만 조용히 검증 없이 통과한다.
    routed_missing = _missing_routed_names(
        phase_id,
        profile=profile,
        changed_files=changed_files,
        task_text=task_text,
        resolution=resolution,
    )
    if routed_missing:
        declared = values.get("missing-required-profile-skills", "").strip()
        names = {
            token.strip().lower()
            for token in declared.replace(";", ",").split(",")
            if token.strip()
        }
        if not all(name.lower() in names for name in routed_missing):
            missing.append(
                "missing-required-profile-skills: " + ", ".join(routed_missing)
            )
    return missing


def _use_evidence_diagnosis(project_root: Path, *, since: float | None) -> str:
    """자기신고가 없어 막는 그 순간에만 붙는 원인 안내.

    관측할 수 없으면(로그 파일이 없거나 못 읽으면) 아무 말도 하지 않는다. 없는
    사실을 지어내지 않기 위해서다. hook 등록 여부는 조회하지 않으므로 "이 host에
    등록됐다/안 됐다"는 말할 수 없고, 말할 수 있는 사실은 "이번 phase 기록이 0건"뿐이다.
    """
    evidence = read_skill_evidence(project_root, since=since)
    if not evidence.available or evidence.read_paths or evidence.used_names:
        return ""
    # marker 모양(`key: value`)을 피한다. 이 문구는 marker 목록에 별 원소로 실려
    # 나가므로, 콜론이 있으면 그 자체가 적어야 할 marker로 읽힌다.
    return (
        "(nothing was recorded during this phase; if you did read them this host "
        "session has not loaded the read hook - restart it and report `unavailable` "
        "if records still do not appear)"
    )


def read_skill_evidence(project_root: Path, *, since: float | None = None) -> SkillReadEvidence:
    """Read hook이 남긴 기록을 읽는다.

    `available`은 **로그를 읽을 수 있었는가**일 뿐이다. 게이트 통과 여부를 정하지
    않고 진단을 할 수 있는지만 정한다. 파일이 없으면 이 host/체크아웃에서는 관측
    자체가 불가능한 상태로 본다.
    """
    log_path = project_root / SKILLS_READ_LOG
    if not log_path.is_file():
        return SkillReadEvidence(available=False, read_paths=frozenset())
    paths: set[str] = set()
    names: set[str] = set()
    try:
        # `errors="replace"`가 없으면 손상 바이트 하나가 UnicodeDecodeError를 낸다.
        # 그건 ValueError라 아래 `except OSError`를 그냥 통과해 `agent-flow status`를
        # 죽인다. 이 로그는 hook이 append-only로 쓰는 것이라 잘린 줄이 언제든 섞인다.
        raw = log_path.read_text(encoding="utf-8", errors="replace")
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
        name = entry.get("skill")
        if isinstance(name, str) and name:
            names.add(name)
    return SkillReadEvidence(
        available=True,
        read_paths=frozenset(paths),
        used_names=frozenset(names),
    )


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


def _marker_instruction(
    resolution: SkillResolution,
    *,
    enforced: bool = True,
    routed_missing: Sequence[str] = (),
) -> str:
    expected = ", ".join(skill.name for skill in resolution.available_required) or "n/a"
    availability = "degraded" if resolution.missing else "pass"
    if not enforced:
        return ""
    # 게이트가 요구하는 마커는 전부 여기 적는다. 하나라도 빠지면 계약대로 쓴
    # artifact가 반드시 한 번 거부되고, 그 순간 이 블록 전체가 신뢰를 잃는다.
    return "\n".join(
        [
            "",
            "The `## Completion Gate` must include:",
            "",
            "```text",
            f"skill-availability: {availability}",
            "skill-use-evidence: verified|unavailable",
            "project-local-skills: checked",
            f"project-local-skills-used: {expected}",
            APPLIED_MARKER,
            f"missing-required-profile-skills: {', '.join(routed_missing) or 'none'}",
            "```",
            "",
        ]
    )


def _missing_routed_names(
    phase_id: str,
    *,
    profile: dict | None,
    changed_files: Sequence[str],
    task_text: str,
    concerns: Sequence[str] = (),
    resolution: SkillResolution,
) -> list[str]:
    routed = {
        skill.name
        for skill in routed_profile_skills(
            profile,
            phase_id=phase_id,
            changed_files=changed_files,
            task_text=task_text,
            concerns=concerns,
        )
    }
    if not routed:
        return []
    return [skill.name for skill in resolution.missing if skill.name in routed]


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
