from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agent_flow.adapters.hosted import _reviewer_jobs, _write_review_input_snapshot
from agent_flow.artifact import create_run, read_meta
from agent_flow.core.architecture_policy import ArchitectureMode, architecture_snapshot
from agent_flow.core.local_skills import changed_files
from agent_flow.core.skill_resolver import (
    SkillResolution,
    active_host_roots,
    resolve_skill,
    skill_roots,
)
from agent_flow.runner import Runner
from tests.test_architecture_install_scenarios import _adapter, _phase
from tests.test_architecture_selection import _track
from tests.test_custom_skill_install import KIT_ROOT, _install_with, _node
from tests.test_mixed_architecture_example import FIXTURE, mixed_project


CONTRACT_ROOT = "skills/architecture/SKILL.md"
COMMON = "skills/architecture/references/common.md"
MAIN = "skills/architecture/references/main-fsd.md"
BO = "skills/architecture/references/bo-routes.md"
FORM = "skills/architecture/references/bo-forms.md"
CONTRACT_DOCUMENTS = {CONTRACT_ROOT, COMMON, MAIN, BO, FORM}
SCOPES = (
    ("main-only", ("apps/main/src/app/page.tsx",), {CONTRACT_ROOT, COMMON, MAIN}),
    ("bo-general", ("apps/bo/src/app/demo/orders/page.tsx",), {CONTRACT_ROOT, COMMON, BO}),
    (
        "bo-form",
        ("apps/bo/src/app/demo/orders/new/page.tsx",),
        {CONTRACT_ROOT, COMMON, BO, FORM},
    ),
    (
        "mixed",
        ("apps/main/src/app/page.tsx", "apps/bo/src/app/demo/orders/new/page.tsx"),
        {CONTRACT_ROOT, COMMON, MAIN, BO, FORM},
    ),
    ("shared-package", ("packages/http/src/index.ts",), {CONTRACT_ROOT, COMMON}),
)


def _assert_local_selection(
    project: Path, resolution: SkillResolution, expected: set[str],
) -> None:
    assert resolution.architecture_snapshot is not None
    assert resolution.architecture_snapshot.selection.mode is ArchitectureMode.LOCAL
    assert resolution.architecture_snapshot.selection.contract_path == CONTRACT_ROOT
    assert resolution.architecture_contract
    assert not any(
        "clean-architecture" in skill.name or "clean-presentation-architecture" in skill.name
        for skill in resolution.required
    )
    selected = set()
    for document in resolution.normative_documents:
        path = Path(document.document.path)
        relative = path.relative_to(project).as_posix() if path.is_absolute() else path.as_posix()
        if document.selected and relative in CONTRACT_DOCUMENTS:
            selected.add(relative)
    assert selected == expected


def _assert_contract_delivery(
    prompt: str, originals: dict[str, str], expected: set[str], *, context: str,
) -> None:
    for relative, body in originals.items():
        if relative in expected:
            assert relative in prompt, f"{context}: missing complete document path {relative}"
            assert body in prompt, f"{context}: missing original document body {relative}"
        else:
            assert relative not in prompt, f"{context}: wrong-scope document path {relative}"
            assert body not in prompt, f"{context}: wrong-scope document body {relative}"


@pytest.mark.parametrize(
    ("binary", "clean_installed"),
    [("agent-flow-kit.mjs", False), ("agent-flow-install.mjs", True)],
    ids=["clean-absent-local-install", "clean-installed-then-local-selection"],
)
def test_installed_mixed_contract_delivery(
    mixed_project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    binary: str, clean_installed: bool,
) -> None:
    project = mixed_project
    originals = {
        relative: (FIXTURE / relative).read_text(encoding="utf-8")
        for relative in CONTRACT_DOCUMENTS
    }
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=project, check=True,
        capture_output=True, text=True,
    )
    _track(project, ".agent-flow.project.yaml", "skills")
    monkeypatch.setenv("AGENT_FLOW_HOST", "omp")
    flags = ["--profile", "generic", "--architecture-mode", "clean" if clean_installed else "local"]
    if not clean_installed:
        flags.extend(("--architecture-skill", CONTRACT_ROOT))
    installation = _install_with(binary, project, *flags, env=dict(os.environ))
    assert installation.returncode == 0, installation.stdout + installation.stderr
    if clean_installed:
        selected = subprocess.run(
            [
                _node(), str(KIT_ROOT / "bin/agent-flow-kit.mjs"), "architecture", "select",
                "--mode", "local", "--skill", CONTRACT_ROOT,
            ],
            cwd=project, env=dict(os.environ), text=True, capture_output=True,
            check=False, timeout=30,
        )
        assert selected.returncode == 0, selected.stdout + selected.stderr
    _track(project, ".agent-flow.project.yaml")
    snapshot = architecture_snapshot(project)
    assert snapshot.selection.mode is ArchitectureMode.LOCAL
    assert snapshot.selection.contract_path == CONTRACT_ROOT
    assert snapshot.contract is not None
    assert snapshot.contract.untracked == ()
    assert {document.path for document in snapshot.contract.documents} == CONTRACT_DOCUMENTS
    assert dict(zip(
        (document.path for document in snapshot.contract.documents),
        snapshot.contract.contents, strict=True,
    )) == originals
    run_dir = create_run(project, "development", "Update scoped fixture behavior", run_id="mixed-delivery")
    installed = Runner(project, workflow="development", run_dir=run_dir)
    for host in ("omp", "claude", "codex"):
        roots = active_host_roots(skill_roots(project, profile=installed.profile, host=host), host)
        skill = resolve_skill("clean-architecture-core", roots)
        assert skill.exists is clean_installed, (host, skill)
    _track(project, ".")
    subprocess.run(
        [
            "git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-qm", "Install mixed architecture baseline",
        ],
        cwd=project, check=True, capture_output=True, text=True,
    )
    source_bodies = {
        relative: (project / relative).read_text(encoding="utf-8")
        for _, paths, _ in SCOPES for relative in paths
    }
    for scope, paths, expected in SCOPES:
        for relative in paths:
            (project / relative).write_text(source_bodies[relative] + "\n", encoding="utf-8")
        try:
            assert set(changed_files(project)) == set(paths), scope
            runner = Runner(project, workflow="development", run_dir=run_dir)
            author_phase = _phase(runner, "implement")
            author_resolution = runner._required_skill_resolution(author_phase, read_meta(run_dir))
            _assert_local_selection(project, author_resolution, expected)
            adapter = _adapter(runner, "omp")
            capsys.readouterr()
            adapter.execute(author_phase, run_dir, project, resolution=author_resolution)
            author_context = capsys.readouterr().out
            _assert_contract_delivery(author_context, originals, expected, context=f"{scope}/author")

            review_phase = _phase(runner, "review")
            review_input = _write_review_input_snapshot(
                project, run_dir, review_phase.id, base_branch=runner.profile["branching"]["base"],
            )
            assert set(review_input.document_scope or ()) == set(paths), scope
            jobs, _ = _reviewer_jobs(
                review_phase, run_dir, project, adapter,
                review_input=review_input, providers=("claude", "codex"),
            )
            assert {provider for job in jobs for provider in job.prompt_by_provider} == {"claude", "codex"}
            for provider in ("claude", "codex"):
                resolution = adapter.phase_resolution(
                    review_phase, project, skill_host=provider, document_scope=review_input.document_scope,
                )
                _assert_local_selection(project, resolution, expected)
                for job in jobs:
                    _assert_contract_delivery(
                        job.prompt_by_provider[provider], originals, expected,
                        context=f"{scope}/{provider}/{job.angle_id}",
                    )
        finally:
            for relative in paths:
                (project / relative).write_text(source_bodies[relative], encoding="utf-8")
