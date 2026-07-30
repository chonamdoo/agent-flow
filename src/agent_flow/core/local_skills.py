from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from agent_flow.core.markers import completion_gate_marker_values
from agent_flow.core.commands import run_safe_command
from agent_flow.core.profiles import active_profile_ids, load_profile_payload
from agent_flow.core.profile_routing import routed_profile_skills
from agent_flow.core.worktree_isolation import (
    WorktreeIsolationError,
    git_repo_state,
    leader_root_for,
    list_registered_worktrees,
)
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
# 마커 키는 옛 artifact와 새 artifact가 한동안 섞인다. 리더는 둘 다 받는다 —
# 개명 하나로 진행 중인 run의 gate가 멈추면 안 된다.
USE_EVIDENCE_KEYS = ("skill-read-evidence", "skill-use-evidence")

# Read hook이 SKILL.md 읽기를 append-only로 기록하는 파일. O_APPEND라 read-modify-write race가 없다.
SKILLS_READ_LOG = Path(".agent-flow") / "skills-read.jsonl"


def changed_files(project_root: Path) -> tuple[str, ...]:
    """working tree + staged 변경 경로. git이 없거나 실패하면 빈 튜플로 degrade한다."""
    # -uall이 없으면 git이 untracked 디렉터리를 `app/sdui/` 한 줄로 접어서
    # pathGlobs가 파일 경로를 못 본다.
    result = run_safe_command(
        ["git", "status", "--porcelain=v1", "-z", "-uall"], cwd=project_root, timeout_s=10
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
    유지하고, 같은 그룹/domain 이름은 **먼저 온 profile이 이긴다** — detect 우선순위가
    곧 소유권이다.
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
                key = str(group.get("group", ""))
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
            load_profile_payload(profile_id)
            for profile_id in active_profile_ids(project_root, requested or "auto")
        ]
    except Exception as exc:  # yaml 오류까지 포함해야 status가 traceback으로 죽지 않는다
        print(f"agent-flow: profile을 읽지 못해 skill 판정이 축소됐다: {exc}", file=sys.stderr)
        return None
    return merged_profile_payload(payloads) if payloads else None


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
    # 같은 저장소의 다른 체크아웃들(leader와 worktree). 같은 skill이 여기 각각
    # 존재하므로 절대경로는 다르지만 체크아웃 기준 상대경로는 같다.
    checkout_roots: tuple[str, ...] = ()
    # Skill tool과 `skill://`은 경로를 주지 않는다. 이름만 관측되는 사용 경로다.
    used_names: frozenset[str] = frozenset()

    def covers(self, skill: ResolvedSkill) -> bool:
        if skill.name in self.used_names:
            return True
        if skill.path is None:
            return False
        resolved = skill.path.resolve()
        if str(resolved) in self.read_paths:
            return True
        # worktree 안에서 읽으면 같은 skill이라도 절대경로가 leader와 다르다.
        # 경로 동등비교만 하면 실제로 읽은 skill을 "안 읽었다"고 차단한다.
        #
        # 그렇다고 이름이나 경로 꼬리로 비교하면 안 된다. `skills/<n>/SKILL.md`
        # 꼬리는 project·bundled·host root 7곳이 전부 공유해서, 낡은 사본이나
        # `/tmp/skills/<n>/SKILL.md`를 읽어도 통과해 버린다. 체크아웃 기준
        # **상대경로**가 같을 때만 같은 skill로 본다.
        target = _checkout_relative(resolved, self.checkout_roots)
        if target is None:
            return False
        return any(
            _checkout_relative(Path(path), self.checkout_roots) == target
            for path in self.read_paths
        )


def _checkout_relative(path: Path, roots: Sequence[str]) -> str | None:
    """path를 담고 있는 체크아웃 root를 찾아 그 기준 상대경로를 돌려준다.

    **가장 긴** root가 이긴다. worktree 체크아웃은 leader 안에 있어서
    (`<leader>/.agent-flow/worktrees/<name>`) leader가 접두사로 먼저 걸리면
    상대경로가 `.agent-flow/worktrees/...`로 나와 같은 skill을 못 알아본다.
    """
    text = str(path)
    best: str | None = None
    best_len = -1
    for root in roots:
        prefix = root.rstrip("/") + "/"
        if text.startswith(prefix) and len(prefix) > best_len:
            best = text[len(prefix):]
            best_len = len(prefix)
    return best


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
    elif not any(values.get(key) in {"verified", "unavailable"} for key in USE_EVIDENCE_KEYS):
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


def read_skill_evidence(project_root: Path, *, since: float | None = None) -> SkillReadEvidence:
    """Read hook이 남긴 기록을 읽는다. 파일이 없으면 hook 미등록/미지원 host로 본다."""
    roots = _checkout_roots(project_root)
    log_path = project_root / SKILLS_READ_LOG
    if not log_path.is_file():
        return SkillReadEvidence(available=False, read_paths=frozenset(), checkout_roots=roots)
    paths: set[str] = set()
    names: set[str] = set()
    try:
        raw = log_path.read_text(encoding="utf-8")
    except OSError:
        return SkillReadEvidence(available=False, read_paths=frozenset(), checkout_roots=roots)
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
        checkout_roots=roots,
        used_names=frozenset(names),
    )


def _checkout_roots(project_root: Path) -> tuple[str, ...]:
    """같은 저장소의 체크아웃 root들.

    양방향이어야 한다. worktree에서 돌면 leader를, leader에서 돌면 그 아래
    관리형 worktree들을 함께 본다. 게이트는 항상 leader root로 불리는데
    (`cli.py`가 `config_root=root`를 넘긴다) agent는 worktree cwd에서 상대경로로
    읽는다. leader 쪽만 보면 정당한 읽음이 전부 미인정으로 차단된다.
    """
    roots = [str(project_root.resolve())]

    def add(path) -> None:
        resolved = str(Path(path).resolve())
        if resolved not in roots:
            roots.append(resolved)

    leader = leader_root_for(project_root)
    if leader is not None:
        add(leader)
    base = Path(leader if leader is not None else project_root)
    try:
        # 등록부가 authority다. 관리 루트 밖 linked worktree(Orca 워크스페이스 등)는
        # 디렉터리 스캔에 안 잡히고, 그러면 그 checkout에서 읽은 skill이 전부
        # 미인정으로 차단된다. 조회가 실패하면 아래 스캔 결과만 남는다 — 실패로
        # 목록을 넓히지는 않는다.
        for registered in list_registered_worktrees(base):
            add(registered.path)
    except WorktreeIsolationError:
        pass
    checkout_parent = base / ".agent-flow" / "worktrees"
    try:
        entries = sorted(checkout_parent.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        # 디렉터리라는 것만으로 신뢰하면 안 된다. 삭제 잔재나 누가 만들어 둔
        # 빈 디렉터리, 저장소 밖을 가리키는 symlink까지 "같은 체크아웃"으로
        # 인정돼 엉뚱한 파일을 읽고도 게이트를 통과한다. git이 인정한 linked
        # worktree만 받는다.
        if entry.is_symlink() or not entry.is_dir():
            continue
        if not (entry / ".git").exists():
            continue
        add(entry)
    return tuple(roots)


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
            "skill-read-evidence: verified|unavailable",
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
    resolution: SkillResolution,
) -> list[str]:
    routed = {
        skill.name
        for skill in routed_profile_skills(
            profile,
            phase_id=phase_id,
            changed_files=changed_files,
            task_text=task_text,
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
