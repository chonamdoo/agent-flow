from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from tests.test_architecture_selection import (
    AGENCY_ROOT,
    _agency_project,
    _local_contract,
    _track,
    _write,
)
from tests.test_custom_skill_install import KIT_ROOT, _install
from tests.test_runner_smoke import _run_cli

from agent_flow.adapters.hosted import HostedAdapter, _reviewer_jobs
from agent_flow.artifact import _missing_completion_markers, read_meta
from agent_flow.core.architecture_policy import architecture_snapshot
from agent_flow.core.skill_resolver import resolve_phase_skills
from agent_flow.core.worktrees import plan_worktree, worktree_runtime_root
from agent_flow.runner import Runner


POLICY = ".agent-flow.project.yaml"
CONTRACT = "skills/architecture/SKILL.md"
REFERENCE = "skills/architecture/references/patterns.md"


@pytest.fixture
def isolated_environment(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("AGENT_FLOW_") or key in {"CLAUDECODE", "CLAUDE_CLI", "CODEX_CLI"}:
            monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENT_FLOW_HOST", "codex")
    monkeypatch.setenv("AGENT_FLOW_ADAPTER", "generic")
    monkeypatch.setenv("AGENT_FLOW_GENERIC_MODE", "")
    monkeypatch.setenv("AGENT_FLOW_REVIEWERS", "codex")
    monkeypatch.setenv("AGENT_FLOW_NO_UPDATE_CHECK", "1")
    return home


def _installed_project(tmp_path: Path, mode: str) -> Path:
    root = _agency_project(tmp_path)
    result = _install(root, "--profile", "nextjs", "--architecture-mode", mode, "--hooks")
    assert result.returncode == 0, result.stderr
    return root


def _cli(root: Path, *args: str):
    return _run_cli(
        list(args), root,
        {
            "AGENT_FLOW_GENERIC_MODE": "",
            "AGENT_FLOW_NO_UPDATE_CHECK": "1",
            "AGENT_FLOW_HOST": "codex",
            "AGENT_FLOW_REVIEWERS": "codex",
            "PYTHONPATH": str(root / ".agent-flow/runtime/python"),
        },
    )


def _select(root: Path, mode: str, *, worktree: str | None = None):
    args = ["architecture", "select", "--mode", mode]
    if mode == "local":
        args.extend(("--skill", CONTRACT))
    if worktree is not None:
        args.extend(("--worktree", worktree))
    return _cli(root, *args)


def _assert_refused(root: Path, mode: str, expected: str, original: bytes) -> None:
    result = _select(root, mode)
    assert result.returncode == 2, result.stdout + result.stderr
    assert expected in result.stderr
    assert (root / POLICY).read_bytes() == original


def _assert_prompts(root: Path, run_dir: Path, mode: str, *, config_root: Path | None = None) -> None:
    runner = Runner(root, config_root=config_root, workflow="development", run_dir=run_dir)
    adapter = HostedAdapter("codex")
    adapter._profile_snapshot = runner.profile
    adapter._profile_id = runner.profile_id
    adapter._config_root = config_root or root
    adapter._concerns = ("architecture",)
    adapter._changed_files = (f"{AGENCY_ROOT}/_lib/toPayload.ts",)
    implement = next(phase for phase in runner.phases if phase.id == "implement")
    review = next(phase for phase in runner.phases if phase.id == "review")
    author = adapter.render_envelope(implement, run_dir, root, skill_host="codex")
    jobs = _reviewer_jobs(review, run_dir, root, adapter, providers=("codex", "claude"))
    assert "architecture-design" in {job.angle_id for job in jobs}
    prompts = [author, *(job.prompt_for(host) for job in jobs for host in ("codex", "claude"))]
    snapshot = architecture_snapshot(root)
    for prompt in prompts:
        assert f"Mode: `{mode}`" in prompt
        assert snapshot.digest in prompt
        if mode == "local":
            for document, content in zip(snapshot.contract.documents, snapshot.contract.contents):
                assert document.sha256 in prompt
                assert content in prompt
    for phase, host in ((implement, "codex"), (review, "codex"), (review, "claude")):
        resolution = resolve_phase_skills(
            project_root=config_root or root, architecture_root=root,
            phase_id=phase.id, phase_skills=phase.skills, profile=runner.profile,
            concerns=("architecture",), host=host,
        )
        names = {skill.name for skill in resolution.required}
        assert "code-generation-discipline" in names
        if mode == "clean":
            assert {"clean-architecture-core", "react-clean-architecture"} <= names
            assert "architecture" not in names
            for name in ("clean-architecture-core", "react-clean-architecture"):
                skill = next(skill for skill in resolution.required if skill.name == name)
                assert skill.exists
                if phase is implement:
                    assert str(skill.path) in author
                else:
                    assert all(str(skill.path) in job.prompt_for(host) for job in jobs)
        else:
            assert "architecture" in names
            assert not {"clean-architecture-core", "react-clean-architecture"} & names


def _commit_install(root: Path) -> None:
    for key, value in (("user.name", "Test User"), ("user.email", "test@example.com")):
        subprocess.run(["git", "config", key, value], cwd=root, check=True)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "Install workspace contract"], cwd=root, check=True)


def _start(root: Path, task: str, *, command: str = "run"):
    args = (
        ["run", task, "--workflow", "development"]
        if command == "run"
        else ["start", "development", "--task", task]
    )
    started = _cli(root, *args)
    assert started.returncode == 0, started.stderr
    assert "status: awaiting_host" in started.stdout
    plan = plan_worktree(root=root, name=task)
    runtime = worktree_runtime_root(root=root, name=plan.name)
    run_dir = next(path for path in (runtime / ".agent-flow/runs").iterdir() if path.is_dir())
    assert read_meta(run_dir)["current_phase"] == "explore"
    return plan, run_dir, started


def test_pending_clean_selection_requires_core_and_platform_before_prompt_delivery(
    tmp_path, isolated_environment,
):
    root = _installed_project(tmp_path, "pending")
    pending = (root / POLICY).read_bytes()
    _assert_refused(root, "clean", "clean-architecture-core", pending)
    provisioned = _install(root, "--profile", "nextjs", "--architecture-mode", "clean", "--hooks")
    assert provisioned.returncode == 0, provisioned.stderr
    assert _select(root, "pending").returncode == 0
    pending = (root / POLICY).read_bytes()
    platform = root / ".agent-flow/skills/react-clean-architecture/SKILL.md"
    original = platform.read_bytes()
    platform.unlink()
    _assert_refused(root, "clean", "react-clean-architecture", pending)
    platform.write_bytes(original)
    selected = _select(root, "clean")
    assert selected.returncode == 0, selected.stderr
    _commit_install(root)
    plan, run_dir, _ = _start(root, "provision clean contract", command="start")
    _assert_prompts(plan.path, run_dir, "clean", config_root=root)


def test_pending_local_selection_requires_documents_and_active_host_transitive_norms(
    tmp_path, isolated_environment,
):
    root = _installed_project(tmp_path, "pending")
    pending = (root / POLICY).read_bytes()
    _assert_refused(root, "local", "architecture", pending)
    _local_contract(root, references=("references/patterns.md",))
    assert _select(root, "pending").returncode == 0
    pending = (root / POLICY).read_bytes()
    reference = root / REFERENCE
    content = reference.read_bytes()
    reference.unlink()
    _assert_refused(root, "local", "patterns.md", pending)
    reference.write_bytes(content)
    contract = root / CONTRACT
    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            "name: architecture\n", "name: architecture\nrequires: [workspace-policy]\n",
        ), encoding="utf-8",
    )
    _write(
        isolated_environment, ".agents/skills/workspace-policy/SKILL.md",
        "---\nname: workspace-policy\nrequires: [transport-policy]\n---\n"
        "Transport validates payloads before crossing the network boundary.\n",
    )
    _write(
        isolated_environment, ".claude/skills/transport-policy/SKILL.md",
        "---\nname: transport-policy\n---\nReject malformed transport payloads.\n",
    )
    _assert_refused(root, "local", "transport-policy", pending)
    _write(
        isolated_environment, ".agents/skills/transport-policy/SKILL.md",
        "---\nname: transport-policy\n---\nReject malformed transport payloads.\n",
    )
    selected = _select(root, "local")
    assert selected.returncode == 0, selected.stderr
    _commit_install(root)
    plan, run_dir, _ = _start(root, "provision local contract")
    _assert_prompts(plan.path, run_dir, "local", config_root=root)
    pins = read_meta(run_dir)["architecture_norm_documents"]
    assert str(isolated_environment / ".agents/skills/transport-policy/SKILL.md") in pins


@pytest.mark.parametrize(
    "change", ["pending-clean", "pending-local", "clean-local", "local-clean", "contract-body", "reference-body"],
)
def test_active_install_contract_transition_blocks_old_completion_until_restored(
    tmp_path, isolated_environment, change,
):
    root = _installed_project(tmp_path, "clean")
    _local_contract(root, references=("references/patterns.md",))
    initial = change.split("-")[0] if change in {"pending-clean", "pending-local", "clean-local", "local-clean"} else "local"
    assert _select(root, initial).returncode == 0
    _commit_install(root)
    plan, run_dir, _ = _start(root, "inspect workspace boundaries")
    before = read_meta(run_dir)
    selected = _select(root, initial, worktree=plan.name)
    assert selected.returncode == 0, selected.stderr
    unchanged = _cli(root, "continue", "--worktree", plan.name)
    assert "status: awaiting_host" in unchanged.stdout
    assert read_meta(run_dir)["phase_entered_at"] == before["phase_entered_at"]
    evidence = run_dir / "explore.md"
    evidence.write_text(
        "# Explore\n\nThe catalog route keeps components in _ui and payload mapping in _lib.\n"
        "The proposed change stays within the existing payload mapper.\n",
        encoding="utf-8",
    )
    assert _missing_completion_markers(
        run_dir, "development", "explore", config_root=root, project_root=plan.path,
    ) == []
    old_evidence = evidence.read_bytes()
    target = plan.path / (CONTRACT if change == "contract-body" else REFERENCE if change == "reference-body" else POLICY)
    original = target.read_bytes()
    if change in {"contract-body", "reference-body"}:
        target.write_bytes(original + b"\nPayload mappers must validate required identifiers.\n")
    else:
        selected = _select(root, change.split("-")[1], worktree=plan.name)
        assert selected.returncode == 0, selected.stderr
    changed = target.read_bytes()
    for command in ("status", "continue"):
        result = _cli(root, command, "--worktree", plan.name)
        assert result.returncode == 0, result.stderr
        assert "reason: architecture_policy_drift" in result.stdout
    blocked = read_meta(run_dir)
    assert blocked["architecture_digest"] == before["architecture_digest"]
    assert blocked["current_phase"] == "explore"
    assert blocked["phase_entered_at"] == before["phase_entered_at"]
    assert evidence.read_bytes() == old_evidence
    assert not (run_dir / "implement.md").exists()
    target.write_bytes(original)
    restored = _cli(root, "status", "--worktree", plan.name)
    assert "architecture_policy_drift" not in restored.stdout
    resumed = _cli(root, "continue", "--worktree", plan.name)
    assert resumed.returncode == 0, resumed.stderr
    assert "status: awaiting_host" in resumed.stdout
    assert read_meta(run_dir)["current_phase"] == "implement"
    if initial != "pending":
        _assert_prompts(plan.path, run_dir, initial, config_root=root)
    aborted = _cli(root, "abort", "--worktree", plan.name, "--yes")
    assert aborted.returncode == 0, aborted.stderr
    target.write_bytes(changed)
    fresh = _cli(
        root, "start", "development", "--task", "inspect revised workspace contract",
        "--worktree", plan.name, "--run-id", "revised-contract",
    )
    assert fresh.returncode == 0, fresh.stderr
    assert "status: awaiting_host" in fresh.stdout
    fresh_dir = run_dir.parent / "revised-contract"
    fresh_meta = read_meta(fresh_dir)
    assert fresh_meta["architecture_digest"] != before["architecture_digest"]
    assert fresh_meta["current_phase"] == "explore"
    assert not (fresh_dir / "explore.md").exists()
    assert evidence.read_bytes() == old_evidence
    selected_mode = architecture_snapshot(plan.path).selection.mode.value
    _assert_prompts(plan.path, fresh_dir, selected_mode, config_root=root)


@pytest.mark.parametrize("damage", ["missing-contract", "invalid-utf8", "invalid-metadata", "untracked-reference", "missing-host-norm", "changed-host-norm", "malformed-host-norm"])
def test_active_install_contract_damage_refuses_execution_until_restored(
    tmp_path, isolated_environment, damage,
):
    root = _installed_project(tmp_path, "pending")
    _local_contract(root, references=("references/patterns.md",))
    contract = root / CONTRACT
    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            "name: architecture\n", "name: architecture\nrequires: [workspace-policy]\n",
        ), encoding="utf-8",
    )
    host_norm = _write(
        isolated_environment, ".agents/skills/workspace-policy/SKILL.md",
        "---\nname: workspace-policy\n---\nPayload mappers remain pure.\n",
    )
    assert _select(root, "local").returncode == 0
    _commit_install(root)
    plan, run_dir, _ = _start(root, "inspect installed norm")
    before = read_meta(run_dir)
    target = host_norm if damage in {"missing-host-norm", "changed-host-norm", "malformed-host-norm"} else plan.path / CONTRACT
    original = target.read_bytes()
    if damage in {"missing-contract", "missing-host-norm"}:
        target.unlink()
    elif damage == "invalid-utf8":
        target.write_bytes(b"\xff\xfe")
    elif damage in {"invalid-metadata", "malformed-host-norm"}:
        target.write_text("---\nname: architecture\nrequires: [\n---\n", encoding="utf-8")
    elif damage == "untracked-reference":
        subprocess.run(["git", "rm", "--cached", "--", REFERENCE], cwd=plan.path, check=True)
    else:
        target.write_bytes(original + b"\nTransport must reject unknown identifiers.\n")
    expected = (
        "architecture_contract_untracked" if damage == "untracked-reference"
        else "architecture_policy_drift" if damage in {"changed-host-norm", "malformed-host-norm"}
        else "architecture_policy_unreadable"
    )
    for command in ("status", "continue"):
        result = _cli(root, command, "--worktree", plan.name)
        assert result.returncode == 0, result.stderr
        assert f"reason: {expected}" in result.stdout
        if command == "status":
            assert read_meta(run_dir) == before
    assert read_meta(run_dir)["current_phase"] == "explore"
    assert read_meta(run_dir)["architecture_digest"] == before["architecture_digest"]
    if damage == "untracked-reference":
        _track(plan.path, REFERENCE)
    else:
        target.write_bytes(original)
    restored = _cli(root, "continue", "--worktree", plan.name)
    assert restored.returncode == 0, restored.stderr
    assert "status: awaiting_host" in restored.stdout
    assert read_meta(run_dir)["phase_entered_at"] == before["phase_entered_at"]


@pytest.mark.parametrize("selection", ["legacy", "clean", "local"])
def test_non_git_install_selection_keeps_compatibility_without_claiming_run_isolation(
    tmp_path, isolated_environment, selection,
):
    root = tmp_path / "plain-project"
    root.mkdir()
    installed = _install(root, "--profile", "nextjs", "--architecture-mode", "clean", "--no-hooks")
    assert installed.returncode == 0, installed.stderr
    if selection == "legacy":
        (root / POLICY).unlink()
    elif selection == "local":
        _write(root, CONTRACT, "---\nname: architecture\n---\nKeep payload mapping pure.\n")
        selected = _select(root, "local")
        assert selected.returncode == 0, selected.stderr
    exported = _cli(root, "architecture", "export")
    assert exported.returncode == 0, exported.stderr
    snapshot = architecture_snapshot(root)
    assert snapshot.selection.mode.value == ("clean" if selection == "legacy" else selection)
    assert snapshot.untracked == ()
    assert snapshot.declared is (selection != "legacy")
    refused = _cli(root, "run", "inspect plain project", "--workflow", "development")
    assert refused.returncode == 2
    assert "worktree runs require a git repository" in refused.stderr
    assert not (root / ".agent-flow/runs/active").exists()
