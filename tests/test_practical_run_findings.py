"""실사용 run(beauty-store, full-feature, 2026-10-20)에서 확인된 결함의 재현.

각 테스트는 올바른 동작을 단언하고 오늘의 코드에서 실패한다. `xfail(strict=True)`라
CI는 초록으로 남고, 고치면 XPASS가 실패로 바뀌어 mark를 지우라고 알린다.
근거: maintainer/improvement-history/ui-task-runs/2026-10-20-beauty-store/failures.md
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = KIT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent_flow import cli as CLI
from agent_flow.artifact import write_meta
from agent_flow.cli_detect import CliInfo
from agent_flow.core.local_skills import missing_local_skill_markers
from agent_flow.core.phase_workflow import load_phase_workflow_definition
from agent_flow.core.profile_resolution import resolve_profile
from agent_flow.core.skill_resolver import (
    ContractDocument,
    NormativeDocument,
    PhaseSkills,
    SkillRoute,
)
from agent_flow.core.worktrees import plan_worktree
from agent_flow.multi_review import ReviewerJob, distribute, eligible_reviewer_names
from agent_flow.runner import Runner, _phase_available_clis, _phases_from_definition

RECORD = "maintainer/improvement-history/ui-task-runs/2026-10-20-beauty-store/failures.md"


def _finding(finding_id: str, summary: str):
    return pytest.mark.xfail(strict=True, reason=f"{finding_id}: {summary}; {RECORD}")


def _full_feature():
    return load_phase_workflow_definition(KIT_ROOT, "full-feature")


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.com",
         "commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
    )
    return path


@_finding("F4", "NormativeDocument.identity hashes routes, so an unchanged body gets a new id")
def test_same_skill_body_keeps_its_identity_when_a_path_route_is_added():
    """반증: 첫 `.tsx`를 만든 것만으로 같은 본문이 새 id를 얻으면 skill_scope_grew가 거짓으로 뜬다."""
    document = ContractDocument(
        "/k/.agent-flow/skills/react-development-guide/SKILL.md", "ab" * 32, 100
    )
    routes = (SkillRoute("react-development-guide", "phase-required", "red"),)
    before = NormativeDocument(document, b"body", routes)
    after = NormativeDocument(
        document,
        b"body",
        routes + (SkillRoute("react-development-guide", "path-selector", "**/*.tsx:tests/a.test.tsx"),),
    )

    assert after.identity == before.identity, (
        "same path+sha256 must keep one document identity; "
        f"got {before.identity[:8]} -> {after.identity[:8]}"
    )


@_finding("F5", "red offers `project-local-skills: checked|n/a` but the gate demands exactly `checked`")
def test_red_accepts_the_project_local_skills_value_its_workflow_offers(tmp_path):
    """반증: prompt가 허용한 `n/a`를 적었는데 제출 뒤 `checked`만 받으면 author는 맞출 방법이 없다."""
    red = next(phase for phase in _full_feature().phases if phase.id == "red")
    assert "project-local-skills: checked|n/a" in red.required_markers

    root = tmp_path / "proj"
    skill = root / "skills" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: alpha\ndescription: Alpha rules.\n---\n# alpha\n", encoding="utf-8")
    artifact = (
        "## Completion Gate\n"
        "skills_checked: true\n"
        "profile-skill-selection: applied\n"
        "active-profiles: nextjs\n"
        "changed-file-skill-resolution: applied\n"
        "required-profile-skills: checked\n"
        "missing-required-profile-skills: none\n"
        "project-local-skills: n/a\n"
        "project-local-skills-used: n/a\n"
        "regression-test: t\n"
        "red-observed: 1\n"
        "test-run-evidence: verified\n"
    )

    missing = missing_local_skill_markers(
        artifact, root, "red", phase_skills=PhaseSkills(required=("alpha",)), profile={}
    )

    assert "project-local-skills: checked" not in missing, missing


@_finding("F6", "full-feature prd prompt never states the comma-separated `spec-items:` format")
def test_full_feature_prd_prompt_states_the_spec_items_format():
    """반증: 형식이 오류 메시지에서만 드러나면 첫 제출이 `spec-items: 11`로 막힌다."""
    prd = next(phase for phase in _full_feature().phases if phase.id == "prd")
    text = "\n".join([prd.description, prd.prompt or "", *prd.required_markers])

    assert "comma-separated SPEC ids" in text, (
        "full-feature prd must state the spec-items format (default.yaml does)"
    )


TASK = (
    "Implement the Store in docs/source-prd.md: all five screens and the header, "
    "every Spec Item (SPEC-1..SPEC-9) and the exact data-testid contract, "
    "reading fixtures/catalog.json"
)


def test_task_derived_branch_respects_the_profile_slug_limit(tmp_path, monkeypatch):
    """반증: nextjs profile이 60을 선언해도 task 원문 길이(≈168자, `.md`/`.json` 포함)가 브랜치가 된다.

    `plan_worktree`는 profile을 받지 않으므로 CLI가 run 시작에서 밟는 경로
    (`_derive_worktree_selector` → `plan_worktree`)로 본다.
    """
    monkeypatch.delenv("AGENT_FLOW_HOST", raising=False)
    monkeypatch.setenv("AGENT_FLOW_PROFILE", "nextjs")
    root = _git_repo(tmp_path / "repo")
    _profile_id, profile = resolve_profile(KIT_ROOT, root)
    assert profile["branching"]["naming"]["max_slug_length"] == 60

    selector = CLI._derive_worktree_selector(root=root, task=TASK)
    slug = plan_worktree(root=root, name=selector).branch.removeprefix("feat/")

    assert len(slug) <= 60 and "." not in slug, f"{len(slug)} chars: {slug}"


def test_forward_prd_to_slice_plan_is_not_a_fix_round(tmp_path):
    """반증: 정상 전진이 fix 라운드로 세이면 FIX_LOOP_MAX_ROUNDS를 소모하고 지표가 거짓이 된다."""
    run_dir = tmp_path / ".agent-flow" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    definition = _full_feature()
    phases = _phases_from_definition(definition)
    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.config_root = run_dir
    runner.phases = phases
    runner.workflow = definition
    index = next(i for i, phase in enumerate(phases) if phase.id == "prd")
    write_meta(run_dir, {"phase_index": index, "current_phase": "prd", "phase_entered_at": "a1"})
    artifact = runner._artifact_path(phases[index])
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("# prd\n", encoding="utf-8")

    transition = runner._plan_transition(index, phases[index])

    assert transition.to_phase == "slice-plan" and not transition.blocked
    assert transition.fix_round == 0, f"forward prd -> slice-plan planned fix_round={transition.fix_round}"


def _fake_codex(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "codex"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "login" ] && [ "$2" = "status" ]; then echo "Not logged in"; exit 1; fi\n'
        'if [ "$2" = "--help" ]; then echo "usage"; exit 0; fi\n'
        "exit 1\n",
        encoding="utf-8",
    )
    script.chmod(0o755)


@_finding("F3", "reviewer availability is binary presence; an unauthenticated codex still gets jobs")
def test_unauthenticated_codex_is_not_assigned_reviewer_jobs(tmp_path, monkeypatch):
    """반증: `codex login status`가 Not logged in인데 절반의 reviewer job을 받으면 그 job은 모두 실패한다."""
    bin_dir = tmp_path / "bin"
    _fake_codex(bin_dir)
    monkeypatch.setenv("PATH", str(bin_dir))
    for name in ("AGENT_FLOW_REVIEWERS", "CODEX_CLI", "CODEX_HOME", "CLAUDECODE", "CLAUDE_CLI", "OMP_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    assert subprocess.run([str(bin_dir / "codex"), "login", "status"], capture_output=True).returncode == 1
    jobs = [ReviewerJob("generalist", "prompt", tmp_path / "generalist.md", tmp_path)]

    distribution = distribute(jobs, host="claude", phase_id="multi-review")

    assert "codex" not in eligible_reviewer_names(), eligible_reviewer_names()
    assert "codex" not in distribution.by_cli, sorted(distribution.by_cli)


def test_review_availability_reports_missing_sandbox_off_macos(monkeypatch):
    """반증: Linux에서 claude/codex가 리뷰 가능으로 보이다가 12 phase 뒤 sandbox 검사에서야 막힌다.

    run 시작에 workflow 전체를 보는 preflight는 없다(`_preflight_entry_profile`은 gate
    명령만 본다). 가장 이른 결정적 표면은 run header의 `available` 줄을 만드는
    `_phase_available_clis`이고, review phase에 대해 이 함수는 sandbox backend 없이도
    reviewer를 launch 가능으로 내놓는다. `verify_provider_sandbox_backend`가 실패하는
    플랫폼에서는 review phase의 launch 가능 reviewer가 없어야 한다.
    """
    from agent_flow.core.worktree_isolation import WorktreeIsolationError
    from agent_flow.providers.subprocess import verify_provider_sandbox_backend

    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(WorktreeIsolationError):
        verify_provider_sandbox_backend()
    review = next(phase for phase in _phases_from_definition(_full_feature()) if phase.multi_review)
    clis = [CliInfo("claude", ("claude",), ("-p",)), CliInfo("codex", ("codex",), ("exec",))]

    launchable = [cli.name for cli in _phase_available_clis(clis, review)]

    assert launchable == [], (
        f"{review.id} reports reviewers {launchable} as available although "
        "no provider write-sandbox backend exists on this platform"
    )


def test_start_banner_warns_once_about_every_review_phase_off_macos(monkeypatch):
    """F1: fix-loop에서 재개해도 route로 되돌아갈 리뷰 phase를 알리고, 현재 phase가 리뷰면 중복해 말하지 않는다."""
    from agent_flow.runner import _review_sandbox_warning

    monkeypatch.setattr(sys, "platform", "linux")
    phases = _phases_from_definition(_full_feature())
    fix_loop = next(phase for phase in phases if phase.id == "fix-loop")
    review = next(phase for phase in phases if phase.multi_review)

    warning = _review_sandbox_warning(phases, fix_loop)

    assert warning is not None and review.id in warning
    assert _review_sandbox_warning(phases, review) is None
    monkeypatch.setattr("agent_flow.runner._review_sandbox_problem", lambda: None)
    assert _review_sandbox_warning(phases, fix_loop) is None
