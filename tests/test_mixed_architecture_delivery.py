from __future__ import annotations

import os
import shutil
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
from tests.test_architecture_install_scenarios import (
    _adapter,
    _phase,
    _isolate_install_environment,
)
from tests.test_architecture_selection import _track, _write
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
    project: Path, resolution: SkillResolution, expected: set[str], *,
    documents: set[str] | None = None,
) -> None:
    if documents is None:
        documents = CONTRACT_DOCUMENTS
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
        if document.selected and relative in documents:
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
    if clean_installed:
        shutil.rmtree(project / "skills" / "architecture")
        (project / ".agent-flow" / "project.yaml").unlink()
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=project, check=True,
        capture_output=True, text=True,
    )
    if not clean_installed:
        _track(project, "skills")
    monkeypatch.setenv("AGENT_FLOW_HOST", "omp")
    flags = ["--profile", "generic", "--architecture-mode", "clean" if clean_installed else "local"]
    if not clean_installed:
        flags.extend(("--architecture-skill", CONTRACT_ROOT))
    installation = _install_with(binary, project, *flags, env=dict(os.environ))
    assert installation.returncode == 0, installation.stdout + installation.stderr
    if clean_installed:
        for relative, body in originals.items():
            path = project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        _track(project, "skills")
        selected = subprocess.run(
            [
                _node(), str(KIT_ROOT / "bin/agent-flow-kit.mjs"), "architecture", "select",
                "--mode", "local", "--skill", CONTRACT_ROOT,
            ],
            cwd=project, env=dict(os.environ), text=True, capture_output=True,
            check=False, timeout=30,
        )
        assert selected.returncode == 0, selected.stdout + selected.stderr
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


SERVICE_COMMON = "skills/architecture/references/cross-runtime.md"
SERVICE_FRONTEND = "skills/architecture/references/browser-fsd.md"
SERVICE_BACKEND = "skills/architecture/references/service-structure.md"
SERVICE_SHARED = "skills/architecture/references/shared-protocol.md"
SERVICE_DOCUMENTS = {
    CONTRACT_ROOT: (
        "---\nname: architecture\ndescription: Apply frontend FSD and backend service ownership by changed path.\n"
        "requires_docs:\n"
        "  - references/cross-runtime.md\n"
        "  - path: references/browser-fsd.md\n"
        '    pathGlobs: ["apps/browser/**"]\n'
        "  - path: references/service-structure.md\n"
        '    pathGlobs: ["services/catalog/**"]\n'
        "  - path: references/shared-protocol.md\n"
        '    pathGlobs: ["packages/protocol/**"]\n'
        "---\n\n# Cross-runtime architecture\n\n"
        "Apply each scoped policy only to its owning paths. Unmatched paths receive common rules, "
        "not a claim that frontend or backend boundaries were checked.\n"
    ),
    SERVICE_COMMON: (
        "# Common ownership\n\n"
        "Runtime-specific implementations must not enter shared protocol modules.\n"
        "Keep transport compatibility explicit at each runtime boundary.\n"
    ),
    SERVICE_FRONTEND: (
        "# Browser FSD\n\n"
        "Browser features may depend on entities and shared modules, never on sibling feature internals.\n"
        "Use each slice's public index when crossing slice ownership.\n"
        "Entity models own server-state query keys; feature UI owns transient interaction state.\n"
    ),
    SERVICE_BACKEND: (
        "# Catalog service\n\n"
        "HTTP endpoints call service actions; services own transactions and coordinate persistence adapters.\n"
        "Persistence adapters must not call HTTP endpoints or serialize responses.\n"
        "Keep this service-oriented structure; do not transplant frontend FSD slices into the backend.\n"
    ),
    SERVICE_SHARED: (
        "# Shared protocol\n\n"
        "Protocol modules contain versioned wire values and pure validation, never browser state or service transactions.\n"
        "A breaking wire change requires updating both runtime consumers.\n"
    ),
}
SERVICE_SOURCES = {
    "apps/browser/src/features/catalog/ui/Catalog.tsx": "export const title = 'Catalog';\n",
    "services/catalog/src/services/catalog.py": "def catalog_title():\n    return 'Catalog'\n",
    "packages/protocol/src/catalog.ts": "export type Catalog = { title: string };\n",
    "tools/release.txt": "Release catalog\n",
}
SERVICE_SCOPES = (
    ("frontend-only", ("apps/browser/src/features/catalog/ui/Catalog.tsx",), {CONTRACT_ROOT, SERVICE_COMMON, SERVICE_FRONTEND}),
    ("backend-only", ("services/catalog/src/services/catalog.py",), {CONTRACT_ROOT, SERVICE_COMMON, SERVICE_BACKEND}),
    (
        "both",
        ("apps/browser/src/features/catalog/ui/Catalog.tsx", "services/catalog/src/services/catalog.py"),
        {CONTRACT_ROOT, SERVICE_COMMON, SERVICE_FRONTEND, SERVICE_BACKEND},
    ),
    ("shared", ("packages/protocol/src/catalog.ts",), {CONTRACT_ROOT, SERVICE_COMMON, SERVICE_SHARED}),
    ("unmatched", ("tools/release.txt",), {CONTRACT_ROOT, SERVICE_COMMON}),
)


@pytest.fixture
def installed_service_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    _isolate_install_environment(tmp_path, monkeypatch)
    project = tmp_path / "frontend-and-service"
    project.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=project, check=True,
        capture_output=True, text=True,
    )
    for relative, body in {**SERVICE_DOCUMENTS, **SERVICE_SOURCES}.items():
        _write(project, relative, body)
    _track(project, *SERVICE_DOCUMENTS)
    monkeypatch.setenv("AGENT_FLOW_HOST", "omp")
    installation = _install_with(
        "agent-flow-install.mjs", project, "--profile", "generic",
        "--architecture-mode", "local", "--architecture-skill", CONTRACT_ROOT,
        env=dict(os.environ),
    )
    assert installation.returncode == 0, installation.stdout + installation.stderr
    _track(project, ".")
    subprocess.run(
        [
            "git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-qm", "Install frontend and service contracts",
        ],
        cwd=project, check=True, capture_output=True, text=True,
    )
    return project


@pytest.mark.parametrize(("scope", "paths", "expected"), SERVICE_SCOPES, ids=[scope for scope, _, _ in SERVICE_SCOPES])
def test_installed_frontend_service_contract_scope_delivery(
    installed_service_project: Path, scope: str, paths: tuple[str, ...], expected: set[str],
) -> None:
    project = installed_service_project
    run_dir = create_run(project, "development", "Update scoped behavior", run_id="service-delivery")
    for relative in paths:
        _write(project, relative, SERVICE_SOURCES[relative] + "\n")
    assert set(changed_files(project)) == set(paths)
    runner = Runner(project, workflow="development", run_dir=run_dir)
    author = _phase(runner, "implement")
    resolution = runner._required_skill_resolution(author, read_meta(run_dir))
    _assert_local_selection(project, resolution, expected, documents=set(SERVICE_DOCUMENTS))
    adapter = _adapter(runner, "omp")
    _assert_contract_delivery(
        adapter.render_envelope(author, run_dir, project, resolution=resolution),
        SERVICE_DOCUMENTS, expected, context=f"{scope}/author",
    )
    review = _phase(runner, "review")
    snapshot = _write_review_input_snapshot(project, run_dir, review.id, base_branch=runner.profile["branching"]["base"])
    assert set(snapshot.document_scope or ()) == set(paths)
    jobs, _ = _reviewer_jobs(
        review, run_dir, project, adapter, review_input=snapshot, providers=("claude", "codex"),
    )
    assert {provider for job in jobs for provider in job.prompt_by_provider} == {"claude", "codex"}
    for provider in ("claude", "codex"):
        resolution = adapter.phase_resolution(
            review, project, skill_host=provider, document_scope=snapshot.document_scope,
        )
        _assert_local_selection(project, resolution, expected, documents=set(SERVICE_DOCUMENTS))
        for job in jobs:
            _assert_contract_delivery(
                job.prompt_by_provider[provider], SERVICE_DOCUMENTS, expected,
                context=f"{scope}/{provider}/{job.angle_id}",
            )


def test_installed_service_author_accumulates_norms_but_reviewers_keep_captured_scope(
    installed_service_project: Path,
) -> None:
    project = installed_service_project
    frontend, backend, shared, _ = SERVICE_SOURCES
    run_dir = create_run(project, "development", "Update scoped behavior", run_id="captured-service-delivery")
    runner = Runner(project, workflow="development", run_dir=run_dir)
    author = _phase(runner, "implement")
    review = _phase(runner, "review")
    _write(project, frontend, SERVICE_SOURCES[frontend] + "\n")
    runner._grown_skill_names(author)
    captured = _write_review_input_snapshot(
        project, run_dir, review.id, base_branch=runner.profile["branching"]["base"],
    )
    assert captured.document_scope == (frontend,)
    _write(project, backend, SERVICE_SOURCES[backend] + "\n")
    runner._grown_skill_names(author)
    _write(project, backend, SERVICE_SOURCES[backend])
    expected_author = {CONTRACT_ROOT, SERVICE_COMMON, SERVICE_FRONTEND, SERVICE_BACKEND}
    resolution = runner._required_skill_resolution(author, read_meta(run_dir))
    _assert_local_selection(project, resolution, expected_author, documents=set(SERVICE_DOCUMENTS))
    adapter = _adapter(runner, "omp")
    _assert_contract_delivery(
        adapter.render_envelope(author, run_dir, project, resolution=resolution),
        SERVICE_DOCUMENTS, expected_author, context="accumulated/author",
    )
    _write(project, shared, SERVICE_SOURCES[shared] + "\n")
    for refresh in (False, True):
        expected = {CONTRACT_ROOT, SERVICE_COMMON, SERVICE_FRONTEND}
        snapshot = captured
        if refresh:
            snapshot = _write_review_input_snapshot(
                project, run_dir, review.id, base_branch=runner.profile["branching"]["base"],
            )
            assert set(snapshot.document_scope or ()) == {frontend, shared}
            expected.add(SERVICE_SHARED)
        jobs, _ = _reviewer_jobs(
            review, run_dir, project, adapter, review_input=snapshot, providers=("claude", "codex"),
        )
        assert {provider for job in jobs for provider in job.prompt_by_provider} == {"claude", "codex"}
        for job in jobs:
            for provider, prompt in job.prompt_by_provider.items():
                _assert_contract_delivery(
                    prompt, SERVICE_DOCUMENTS, expected, context=f"captured/{provider}/{job.angle_id}",
                )
