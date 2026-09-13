from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

KIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT_ROOT / "src"))

from agent_flow.core import architecture_policy as policy  # noqa: E402


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """Create an isolated Git repository fixture."""
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def _write(root: Path, relative: str, payload: bytes) -> Path:
    """Write a file used by the current test fixture."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _declare(root: Path, mode: str = "local") -> Path:
    """Write an architecture selection fixture."""
    skill = "  skill: skills/architecture/SKILL.md\n" if mode == "local" else ""
    return _write(
        root,
        policy.PROJECT_ARCHITECTURE_FILE,
        f"schema_version: 1\narchitecture:\n  mode: {mode}\n{skill}".encode(),
    )


def _contract(root: Path, header: str = "") -> Path:
    """Write a local architecture contract fixture."""
    frontmatter = f"---\n{header}\n---\n" if header else ""
    return _write(root, "skills/architecture/SKILL.md", (frontmatter + "# Approved rules\n").encode())


def _cli(*args: str) -> int:
    """Invoke the CLI with the supplied project arguments."""
    from agent_flow.cli import main

    return main(list(args))


def _status_run(root: Path) -> Path:
    from agent_flow.artifact import create_run, read_meta, write_meta
    from agent_flow.core.phase_workflow import load_phase_workflow_definition

    definition = load_phase_workflow_definition(KIT_ROOT, "default")
    run_dir = create_run(root, "default", "Update title", workflow_definition=definition)
    meta = read_meta(run_dir)
    meta.update(
        current_phase="implement",
        phase_index=next(i for i, phase in enumerate(definition.phases) if phase.id == "implement"),
    )
    write_meta(run_dir, meta)
    return run_dir


def test_non_git_selection_remains_runnable(tmp_path, monkeypatch):
    """Verify that a non-Git selection remains runnable."""
    from agent_flow.artifact import create_run
    from agent_flow.runner import ResumeMode, Runner

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "non-git-project"
    root.mkdir()
    contract = _contract(root)
    assert _cli("architecture", "select", "--root", str(root), "--mode", "local", "--skill", "skills/architecture/SKILL.md") == 0
    run_dir = create_run(root, "default", "Update local rules")
    runner = Runner(root, run_dir=run_dir)
    runner._initialize_architecture_policy(ResumeMode.START)

    assert runner._architecture_policy_block_reason() is None
    contract.write_text("# Changed rules\n", encoding="utf-8")
    assert runner._architecture_policy_block_reason() == "architecture_policy_drift"


@pytest.mark.parametrize("failure", ["declaration", "interrupted-install"])
def test_status_reports_architecture_failure_separately_from_markers(repository, monkeypatch, capsys, failure):
    """Verify that status reports architecture failure separately from markers."""
    from agent_flow.artifact import ActiveRun

    monkeypatch.setenv("HOME", str(repository.parent / "home"))
    run_dir = _status_run(repository)
    if failure == "declaration":
        source = _write(repository, policy.PROJECT_ARCHITECTURE_FILE, b"schema_version: 1\narchitecture: {mode: unknown}\n")
    else:
        source = _write(repository, ".agent-flow/install-recovery/manifest.json", b"{}")
    (run_dir / "implement.md").write_text("## Completion Gate\n", encoding="utf-8")
    active = ActiveRun(run_dir, run_dir.name, "default", "Update title", "")

    active.print_status(config_root=repository, project_root=repository)

    payload = json.loads(next(line.removeprefix("status_json: ") for line in capsys.readouterr().out.splitlines() if line.startswith("status_json: ")))
    assert payload["status"] == "blocked"
    assert payload["reason"] == "architecture_policy_unreadable"
    assert str(source.relative_to(repository)) in payload["detail"]
    assert not payload.get("missing_completion_markers")


@pytest.mark.parametrize("artifact_exists", [False, True])
@pytest.mark.parametrize("changed_source", ["selection", "reference"])
def test_status_reports_pinned_drift_before_artifact_readiness(
    repository, monkeypatch, capsys, artifact_exists, changed_source
):
    """Verify that status reports pinned drift before artifact readiness."""
    from agent_flow.artifact import ActiveRun, read_meta, write_meta
    from agent_flow.runner import Runner

    monkeypatch.setenv("HOME", str(repository.parent / "home"))
    selection = _declare(repository)
    _contract(repository, "requires_docs: [references/rules.md]")
    reference = _write(
        repository, "skills/architecture/references/rules.md", b"# Approved boundary\n"
    )
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    run_dir = _status_run(repository)
    meta = read_meta(run_dir)
    meta["architecture_digest"] = policy.architecture_snapshot(repository).digest
    write_meta(run_dir, meta)
    if artifact_exists:
        (run_dir / "implement.md").write_text("## Completion Gate\n", encoding="utf-8")
    if changed_source == "selection":
        selection.write_text(
            "schema_version: 1\narchitecture: {mode: pending}\n", encoding="utf-8"
        )
    else:
        reference.write_bytes(b"# Changed boundary\n")
    active = ActiveRun(run_dir, run_dir.name, "default", "Update title", "")

    active.print_status(config_root=repository, project_root=repository)

    payload = json.loads(next(
        line.removeprefix("status_json: ")
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("status_json: ")
    ))
    assert payload["status"] == "blocked"
    assert payload["reason"] == "architecture_policy_drift"
    assert not payload.get("missing_completion_markers")
    assert Runner(repository, run_dir=run_dir)._architecture_policy_block_reason() == payload["reason"]


def test_status_allows_unchanged_pinned_policy_to_await_artifact(
    repository, monkeypatch, capsys
):
    """Verify that status allows unchanged pinned policy to await artifact."""
    from agent_flow.artifact import ActiveRun, read_meta, write_meta

    monkeypatch.setenv("HOME", str(repository.parent / "home"))
    _declare(repository, "pending")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    run_dir = _status_run(repository)
    meta = read_meta(run_dir)
    meta["architecture_digest"] = policy.architecture_snapshot(repository).digest
    write_meta(run_dir, meta)

    ActiveRun(run_dir, run_dir.name, "default", "Update title", "").print_status(
        config_root=repository, project_root=repository
    )

    payload = json.loads(next(
        line.removeprefix("status_json: ")
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("status_json: ")
    ))
    assert payload["status"] == "awaiting_host"
    assert payload["reason"] == "missing_phase_artifact"


@pytest.mark.parametrize(
    "skill",
    ["absolute", "docs/rules.md", "skills/custom/SKILL.md", "skills/architecture/../SKILL.md", ""],
)
def test_invalid_cli_skill_preserves_a_usable_selection(repository, capsys, skill):
    """Verify that invalid CLI skill preserves a usable selection."""
    selection_path = _declare(repository, "clean")
    previous = selection_path.read_bytes()
    ordinary_file = _write(repository, "docs/rules.md", b"# Rules\n")
    if skill == "absolute":
        skill = str(ordinary_file)

    assert _cli("architecture", "select", "--root", str(repository), "--mode", "local", "--skill", skill) == 2
    assert selection_path.read_bytes() == previous
    assert policy.architecture_snapshot(repository).selection.mode is policy.ArchitectureMode.CLEAN
    assert capsys.readouterr().err


def test_constructor_and_yaml_reject_the_same_noncanonical_contract():
    """Verify that constructor and YAML reject the same noncanonical contract."""
    with pytest.raises(ValueError):
        policy.ArchitectureSelection(policy.ArchitectureMode.LOCAL, "skills/custom/SKILL.md")
    with pytest.raises(ValueError):
        policy.parse_architecture_document(
            "schema_version: 1\narchitecture: {mode: local, skill: skills/custom/SKILL.md}\n",
            source="selection",
        )


@pytest.mark.parametrize(
    "header",
    [
        "requires_docs: [references/missing.md]\nrequires_docs: []",
        "metadata:\n  - {requires_docs: [references/missing.md], requires_docs: []}",
        "requires_docs: &cycle [*cycle]",
        "metadata: &cycle {nested: *cycle}",
        "defaults: &defaults {requires_docs: [references/missing.md]}\n<<: *defaults\nrequires_docs: []",
        "requires_docs: null",
        "[requires_docs, references/missing.md]",
    ],
)
def test_invalid_frontmatter_cannot_erase_required_documents(repository, header):
    """Verify that invalid frontmatter cannot erase required documents."""
    _declare(repository)
    _contract(repository, header)

    with pytest.raises(policy.ArchitectureContractError):
        policy.architecture_snapshot(repository)


def test_recursive_selection_yaml_is_a_declaration_error():
    """Verify that recursive selection YAML is a declaration error."""
    with pytest.raises(ValueError, match="recursive YAML"):
        policy.parse_architecture_document("schema_version: 1\narchitecture: &cycle {mode: clean, nested: *cycle}\n", source="selection")


@pytest.mark.parametrize("kind", ["directory", "dangling-symlink", "symlink"])
def test_invalid_selection_file_never_becomes_legacy_clean(repository, kind):
    """Verify that invalid selection file never becomes legacy clean."""
    path = repository / policy.PROJECT_ARCHITECTURE_FILE
    if kind == "directory":
        path.mkdir()
    else:
        target = repository / "other.yaml"
        if kind == "symlink":
            target.write_text("schema_version: 1\narchitecture: {mode: pending}\n", encoding="utf-8")
        path.symlink_to(target)

    with pytest.raises(policy.ArchitectureContractError):
        policy.architecture_snapshot(repository)


@pytest.mark.parametrize("relative", ["skills/architecture/SKILL.md", "skills/architecture/references/rules.md"])
@pytest.mark.parametrize("payload", [b"", b" \n\t", b"\xff", b"---\nname: architecture\n---\n"])
def test_unusable_normative_documents_never_produce_a_snapshot(repository, relative, payload):
    """Verify that unusable normative documents never produce a snapshot."""
    _declare(repository)
    _contract(repository, "requires_docs: [references/rules.md]")
    _write(repository, "skills/architecture/references/rules.md", b"# Required rules\n")
    _write(repository, relative, payload)

    with pytest.raises(policy.ArchitectureContractError):
        policy.architecture_snapshot(repository)


@pytest.mark.parametrize(
    "reference",
    ["rules.md", "references/./rules.md", "references/../rules.md", "references//rules.md", "references/rules.txt", " references/rules.md", "references/rules.md\n"],
)
def test_references_must_be_canonical_markdown_paths(repository, reference):
    """Verify that references must be canonical markdown paths."""
    _declare(repository)
    _contract(repository, f"requires_docs: [{json.dumps(reference)}]")
    _write(repository, "skills/architecture/references/rules.md", b"# Rules\n")

    with pytest.raises(policy.ArchitectureContractError):
        policy.architecture_snapshot(repository)


@pytest.mark.parametrize("linked_part", ["root", "references-directory", "reference"])
def test_normative_symlinks_are_rejected_even_inside_repository(repository, linked_part):
    """Verify that normative symlinks are rejected even inside repository."""
    _declare(repository)
    _contract(repository, "requires_docs: [references/rules.md]")
    reference = _write(repository, "skills/architecture/references/rules.md", b"# Rules\n")
    if linked_part == "root":
        path = repository / "skills/architecture/SKILL.md"
    elif linked_part == "references-directory":
        path = reference.parent
    else:
        path = reference
    target = path.with_name(path.name + "-actual")
    path.rename(target)
    path.symlink_to(target.name, target_is_directory=target.is_dir())

    with pytest.raises(policy.ArchitectureContractError, match="symlink"):
        policy.architecture_snapshot(repository)


def test_contract_cannot_come_from_a_nested_repository(repository):
    """Verify that contract cannot come from a nested repository."""
    _declare(repository)
    contract = _contract(repository)
    subprocess.run(["git", "init", "-q"], cwd=contract.parent, check=True)

    with pytest.raises(policy.ArchitectureContractError, match="nested repository"):
        policy.architecture_snapshot(repository)


def test_export_reports_untracked_selection_and_exact_document_bytes(repository, capsys):
    """Verify that export reports untracked selection and exact document bytes."""
    selection = _declare(repository)
    contract = _contract(repository, "requires_docs: [references/rules.md]")
    reference = _write(repository, "skills/architecture/references/rules.md", "# 실제 규칙\r\n".encode())
    subprocess.run(["git", "add", "--", "skills/architecture/SKILL.md"], cwd=repository, check=True)

    assert _cli("architecture", "export", "--root", str(repository)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["untracked"] == [policy.PROJECT_ARCHITECTURE_FILE, "skills/architecture/references/rules.md"]
    assert payload["source_document"] == {
        "path": policy.PROJECT_ARCHITECTURE_FILE,
        "sha256": hashlib.sha256(selection.read_bytes()).hexdigest(),
        "bytes": len(selection.read_bytes()),
    }
    assert payload["document_manifest"] == [
        {"path": str(path.relative_to(repository)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": len(path.read_bytes())}
        for path in (contract, reference)
    ]


def test_git_failure_cannot_publish_a_new_selection(repository, monkeypatch, capsys):
    """Verify that Git failure cannot publish a new selection."""
    selection = _declare(repository, "clean")
    previous = selection.read_bytes()
    _contract(repository)
    monkeypatch.setattr(policy, "git_safe", lambda *args, **kwargs: SimpleNamespace(ok=False, stdout="", stderr="index unreadable"))

    assert _cli("architecture", "select", "--root", str(repository), "--mode", "local", "--skill", "skills/architecture/SKILL.md") == 2
    assert selection.read_bytes() == previous
    assert "tracking cannot be determined" in capsys.readouterr().err


def test_selection_byte_change_invalidates_identical_semantic_policy(repository):
    """Verify that selection byte change invalidates identical semantic policy."""
    selection = _declare(repository, "pending")
    pinned = policy.architecture_snapshot(repository)
    selection.write_bytes(selection.read_bytes() + b"# Changed approval source\n")

    current = policy.architecture_snapshot(repository)

    assert current.selection == pinned.selection
    assert current.digest != pinned.digest
    assert current.source_document != pinned.source_document


def test_candidate_export_validates_without_publishing_and_round_trips(repository, capsys):
    """Verify that candidate export validates without publishing and round trips."""
    selection = _declare(repository, "clean")
    previous = selection.read_bytes()
    _contract(repository)
    arguments = ("--root", str(repository), "--mode", "local", "--skill", "skills/architecture/SKILL.md")

    assert _cli("architecture", "export", *arguments) == 0
    candidate = json.loads(capsys.readouterr().out)
    assert selection.read_bytes() == previous
    assert candidate["mode"] == "local"
    assert _cli("architecture", "select", *arguments) == 0
    capsys.readouterr()
    assert selection.read_text(encoding="utf-8") == candidate["selection_document"]
    assert policy.architecture_snapshot(repository).digest == candidate["digest"]


def test_candidate_export_failure_preserves_selection(repository, capsys):
    """Verify that candidate export failure preserves selection."""
    selection = _declare(repository, "pending")
    previous = selection.read_bytes()

    assert _cli("architecture", "export", "--root", str(repository), "--mode", "local", "--skill", "skills/architecture/SKILL.md") == 2
    assert selection.read_bytes() == previous
    assert not capsys.readouterr().out


@pytest.mark.parametrize(
    "relative",
    [
        policy.PROJECT_ARCHITECTURE_FILE,
        "skills/architecture/SKILL.md",
        "skills/architecture/references/rules.md",
        "skills",
        "skills/architecture/references",
    ],
)
def test_symlink_replacement_at_open_cannot_pin_outside_bytes(repository, monkeypatch, relative):
    """Verify that symlink replacement at open cannot pin outside bytes."""
    _declare(repository)
    _contract(repository, "requires_docs: [references/rules.md]")
    _write(repository, "skills/architecture/references/rules.md", b"# Approved reference\n")
    outside = repository.parent / "outside"
    _declare(outside, "pending")
    _write(outside, "skills/architecture/SKILL.md", b"# Outside contract\n")
    _write(outside, "skills/architecture/references/rules.md", b"# Outside reference\n")
    target = repository / relative
    is_directory = target.is_dir()
    original_open = os.open
    replaced = False

    def replace_before_open(path, flags, *args, **kwargs):
        """Replace the target immediately before it is opened."""
        nonlocal replaced
        if not replaced and kwargs.get("dir_fd") is not None and path == target.name:
            replaced = True
            target.rename(target.with_name(target.name + "-original"))
            target.symlink_to(outside / relative, target_is_directory=is_directory)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_before_open)
    with pytest.raises(policy.ArchitectureContractError):
        policy.architecture_snapshot(repository)


@pytest.mark.parametrize("relative", ["skills", "skills/architecture/references"])
def test_opened_parent_survives_outside_symlink_replacement(repository, monkeypatch, relative):
    """Verify that opened parent survives outside symlink replacement."""
    _declare(repository)
    _contract(repository, "requires_docs: [references/rules.md]")
    _write(repository, "skills/architecture/references/rules.md", b"# Approved reference\n")
    approved = policy.architecture_snapshot(repository)
    outside = repository.parent / "outside"
    _write(outside, "skills/architecture/SKILL.md", b"# Outside contract\n")
    _write(outside, "skills/architecture/references/rules.md", b"# Outside reference\n")
    target = repository / relative
    original_open = os.open
    replaced = False

    def replace_after_open(path, flags, *args, **kwargs):
        """Replace the path after its descriptor has been opened."""
        nonlocal replaced
        descriptor = original_open(path, flags, *args, **kwargs)
        if not replaced and kwargs.get("dir_fd") is not None and path == target.name:
            replaced = True
            target.rename(target.with_name(target.name + "-original"))
            target.symlink_to(outside / relative, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(os, "open", replace_after_open)
    try:
        current = policy.architecture_snapshot(repository)
    except policy.ArchitectureContractError:
        pass
    else:
        assert current.contract == approved.contract
        assert current.digest == approved.digest
    assert target.is_symlink()


def test_selection_and_source_digest_use_the_same_opened_bytes(repository, monkeypatch):
    """Verify that selection and source digest use the same opened bytes."""
    declaration = _declare(repository, "clean")
    approved_bytes = declaration.read_bytes()
    original_open = os.open

    def replace_after_open(path, flags, *args, **kwargs):
        """Replace the path after its descriptor has been opened."""
        descriptor = original_open(path, flags, *args, **kwargs)
        if kwargs.get("dir_fd") is not None and path == declaration.name:
            declaration.unlink()
            _declare(repository, "pending")
        return descriptor

    monkeypatch.setattr(os, "open", replace_after_open)
    snapshot = policy.architecture_snapshot(repository)

    assert declaration.read_bytes() != approved_bytes
    assert snapshot.selection.mode is policy.ArchitectureMode.CLEAN
    assert snapshot.source_document.sha256 == hashlib.sha256(approved_bytes).hexdigest()
    assert snapshot.source_document.bytes == len(approved_bytes)


@pytest.mark.parametrize("replacement", ["directory", "fifo"])
def test_opened_contract_must_still_be_a_regular_file(repository, monkeypatch, replacement):
    """Verify that opened contract must still be a regular file."""
    _declare(repository)
    contract = _contract(repository)
    original_open = os.open

    def replace_before_open(path, flags, *args, **kwargs):
        """Replace the target immediately before it is opened."""
        if kwargs.get("dir_fd") is not None and path == contract.name:
            contract.unlink()
            if replacement == "directory":
                contract.mkdir()
            else:
                os.mkfifo(contract)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_before_open)
    with pytest.raises(policy.ArchitectureContractError, match="regular file"):
        policy.architecture_snapshot(repository)


@pytest.mark.parametrize("mode", ["legacy", "clean", "local", "pending"])
def test_interrupted_install_blocks_cli_until_recovery(repository, mode, capsys):
    """Verify that interrupted install blocks CLI until recovery."""
    if mode != "legacy":
        _declare(repository, mode)
    if mode == "local":
        _contract(repository)
    assert _cli("architecture", "export", "--root", str(repository)) == 0
    approved = json.loads(capsys.readouterr().out)
    manifest = _write(repository, ".agent-flow/install-recovery/manifest.json", b"[]")

    assert _cli("architecture", "export", "--root", str(repository)) == 2
    blocked = capsys.readouterr()
    assert not blocked.out
    assert "install recovery" in blocked.err

    manifest.unlink()
    assert _cli("architecture", "export", "--root", str(repository)) == 0
    assert json.loads(capsys.readouterr().out) == approved


def test_install_starting_during_cli_snapshot_blocks_the_result(repository, monkeypatch, capsys):
    """Verify that install starting during CLI snapshot blocks the result."""
    _declare(repository, "clean")
    original_open = os.open

    def interrupt_install(path, flags, *args, **kwargs):
        """Simulate installation beginning during contract resolution."""
        descriptor = original_open(path, flags, *args, **kwargs)
        if kwargs.get("dir_fd") is not None and path == policy.PROJECT_ARCHITECTURE_FILE:
            _write(repository, ".agent-flow/install-recovery/manifest.json", b"[]")
        return descriptor

    monkeypatch.setattr(os, "open", interrupt_install)
    assert _cli("architecture", "export", "--root", str(repository)) == 2
    blocked = capsys.readouterr()
    assert not blocked.out
    assert "install recovery" in blocked.err



@pytest.mark.parametrize(
    "relative",
    [policy.PROJECT_ARCHITECTURE_FILE, "skills/architecture/SKILL.md", "skills/architecture/references/rules.md"],
)
def test_oversized_documents_are_rejected_before_read(repository, monkeypatch, relative):
    """Verify that oversized documents are rejected before read."""
    _declare(repository)
    _contract(repository, "requires_docs: [references/rules.md]")
    _write(repository, "skills/architecture/references/rules.md", b"Required rule.")
    path = repository / relative
    with path.open("r+b") as stream:
        stream.truncate(policy.MAX_ARCHITECTURE_DOCUMENT_BYTES + 1)
    identity = path.stat()
    original_read = os.read

    def reject_oversized_read(descriptor, size):
        """Simulate a document exceeding the read size limit."""
        opened = os.fstat(descriptor)
        assert (opened.st_dev, opened.st_ino) != (identity.st_dev, identity.st_ino)
        return original_read(descriptor, size)

    monkeypatch.setattr(os, "read", reject_oversized_read)
    with pytest.raises(policy.ArchitectureContractError, match="too large"):
        policy.architecture_snapshot(repository)


def test_document_growth_after_stat_is_still_bounded(repository, monkeypatch):
    """Verify that document growth after stat is still bounded."""
    _declare(repository)
    contract = _contract(repository)
    identity = contract.stat()
    original_read = os.read
    delivered = 0

    def growing_document(descriptor, size):
        """Simulate a document growing after its initial size check."""
        nonlocal delivered
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) == (identity.st_dev, identity.st_ino):
            delivered += size
            return b"x" * size
        return original_read(descriptor, size)

    monkeypatch.setattr(os, "read", growing_document)
    with pytest.raises(policy.ArchitectureContractError, match="too large"):
        policy.architecture_snapshot(repository)
    assert delivered == policy.MAX_ARCHITECTURE_DOCUMENT_BYTES + 1
