from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import FrozenInstanceError
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_flow.adapters.hosted import HostedAdapter, _reviewer_jobs
from agent_flow.core import skill_resolver
from agent_flow.core.architecture_policy import ArchitectureContractError, ContractDocument
from agent_flow.core.profiles import load_profile_payload
from agent_flow.core.skill_resolver import (
    NormativeDocument,
    PhaseSkills,
    ResolutionContext,
    SkillResolution,
    SkillRoute,
    resolve_phase_skills,
    skill_prompt_block,
)
from agent_flow.runner import Phase


@pytest.fixture(autouse=True)
def isolated_host(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLOW_HOST", "codex")


def skill(root, name, body):
    path = root / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def local_contract(root, body, references=()):
    declared = f"requires_docs: [{', '.join(references)}]\n" if references else ""
    path = skill(root, "architecture", f"---\nname: contract\n{declared}---\n{body}")
    (root / ".agent-flow.project.yaml").write_text(
        "schema_version: 1\narchitecture:\n  mode: local\n  skill: skills/architecture/SKILL.md\n",
        encoding="utf-8",
    )
    return path


def yaml_profile(root):
    path = root / ".agent-flow" / "profiles" / "yaml-values.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "id: yaml-values\n"
        "adopted: 2026-01-01\n"
        "metadata:\n  1: integer key\n  '1': string key\n"
        "labels: !!set\n  security: null\n  architecture: null\n"
        "execution:\n  reviewers:\n    - candidates:\n"
        "        - provider: codex\n          model: gpt-5.6-sol\n",
        encoding="utf-8",
    )
    return path


def resolve(root, context=None, **kwargs):
    return resolve_phase_skills(
        project_root=root, phase_id="implement", host="codex",
        context=context, **kwargs,
    )


def required_read_paths(prompt):
    return [line.split("`")[1] for line in prompt.splitlines() if line.startswith("- Read `")]


def test_exact_bodies_merge_all_document_identities_not_similar_text(tmp_path):
    payload = b"Do not cross the ownership boundary.\n"
    documents = (
        NormativeDocument(ContractDocument("one", "a", len(payload)), payload,
                          (SkillRoute("one", "phase-required", "implement"),), inline=True),
        NormativeDocument(ContractDocument("two", "a", len(payload)), payload,
                          (SkillRoute("two", "dependency", "parent"),), inline=True),
        NormativeDocument(ContractDocument("one", "b", len(payload) + 1), payload + b"\n", inline=True),
        NormativeDocument(ContractDocument("reference-only", "a", len(payload)), payload),
    )
    resolution = SkillResolution(normative_documents=documents)
    assert [group.content for group in resolution.delivery] == [payload, payload + b"\n"]
    assert resolution.delivery[0].documents == (documents[0], documents[1], documents[3])
    assert resolution.duplicated_normative_bytes == len(payload)
    assert resolution.delivered_normative_bytes == len(payload) * 2 + 1
    with pytest.raises(FrozenInstanceError):
        documents[0].content = b"replacement"


def test_required_references_keep_digests_and_all_phase_profile_dependency_routes(tmp_path):
    payload = "Every mutation requires authorization.\n"
    first = skill(tmp_path, "first", payload)
    second = skill(tmp_path, "second", payload)
    skill(tmp_path, "parent", "---\nrequires: [first, second]\n---\nParent contract.\n")
    profile = {"skills": {"required_review": [
        {"group": "api", "skills": ["first"], "path_globs": ["**/*.py"]},
        {"group": "security", "skills": ["first"], "path_globs": ["**/*.py"]},
    ]}}
    result = resolve(tmp_path, phase_skills=PhaseSkills(required=("first", "parent")),
                     profile=profile, changed_files=("app.py",))
    prompt = skill_prompt_block(tmp_path, result)
    assert payload not in prompt
    assert "Parent contract.\n" not in prompt
    assert not any(group.inline for group in result.delivery)
    reads = required_read_paths(prompt)
    assert reads == [str(first), str(tmp_path / "skills" / "parent" / "SKILL.md")]
    assert str(second) not in reads
    assert "provenance only, not additional read instructions" in prompt
    assert result.delivered_normative_bytes == result.duplicated_normative_bytes == 0
    documents = [item for item in result.normative_documents if item.content == payload.encode()]
    assert {item.document.path for item in documents} == {str(first), str(second)}
    for item in documents:
        assert item.document.path in prompt
        assert hashlib.sha256(payload.encode()).hexdigest() == item.document.sha256
        assert item.document.sha256 in prompt
        assert f"Bytes: {len(payload.encode())}." in prompt
        for route in item.routes:
            assert f"route: {route.kind} / {route.skill} / {route.detail}" in prompt
    first_routes = next(item.routes for item in documents if item.document.path == str(first))
    assert {route.kind for route in first_routes} >= {"phase-required", "profile", "dependency"}
    assert any("api" in route.detail for route in first_routes)
    assert any("security" in route.detail for route in first_routes)


@pytest.mark.parametrize("role", ["author", "reviewer"])
def test_local_contract_and_equal_reference_paths_deliver_once(tmp_path, role):
    path = local_contract(tmp_path, "LOCAL_EXACT_BODY\n", ("references/one.md", "references/two.md"))
    payload = path.read_bytes()
    (path.parent / "references").mkdir()
    for name in ("one.md", "two.md"):
        (path.parent / "references" / name).write_bytes(payload)
    ordinary = skill(tmp_path, "ordinary", payload.decode())
    result = resolve(tmp_path, phase_skills=PhaseSkills(required=("ordinary",)))
    prompt = skill_prompt_block(tmp_path, result, role=role)
    assert prompt.count(payload.decode()) == 1
    assert required_read_paths(prompt) == []
    assert result.missing == ()
    assert [group.content for group in result.delivery] == [payload]
    assert {item.document.path for item in result.delivery[0].documents} == {
        str(ordinary), str(path),
        "skills/architecture/references/one.md", "skills/architecture/references/two.md",
    }
    assert len(result.normative_documents) == 4
    assert result.delivered_normative_bytes == len(payload)
    assert result.duplicated_normative_bytes == len(payload) * 2
    for item in result.normative_documents:
        assert item.document.path in prompt
        assert item.document.sha256 in prompt
        for route in item.routes:
            assert f"route: {route.kind} / {route.skill} / {route.detail}" in prompt


def test_each_author_and_provider_angle_delivers_its_own_complete_body(tmp_path):
    body = "AUTHOR_AND_REVIEWER_NORM_BODY\n"
    local_contract(tmp_path, body)
    ordinary = skill(tmp_path, "ordinary", "REFERENCE_ONLY_AUTHOR_REVIEWER_NORM\n")
    adapter = HostedAdapter("codex")
    phase = Phase(id="review", description="Review", skills=PhaseSkills(required=("contract", "ordinary")))
    run_dir = tmp_path / "run"
    author = adapter.render_envelope(phase, run_dir, tmp_path)
    jobs = _reviewer_jobs(phase, run_dir, tmp_path, adapter, providers=("claude", "codex"))
    assert author.count(body) == 1
    assert "REFERENCE_ONLY_AUTHOR_REVIEWER_NORM" not in author
    assert required_read_paths(author) == [str(ordinary)]
    assert len(jobs) >= 2
    for job in jobs:
        for provider in ("claude", "codex"):
            assert job.prompt_for(provider).count(body) == 1
            assert "REFERENCE_ONLY_AUTHOR_REVIEWER_NORM" not in job.prompt_for(provider)
            assert required_read_paths(job.prompt_for(provider)) == [str(ordinary)]
    assert adapter.render_envelope(phase, run_dir, tmp_path).count(body) == 1


@pytest.mark.parametrize("role", ["author", "reviewer"])
@pytest.mark.parametrize("task", [
    "",
    "Implement the payment change.\n\nWrite the release artifact.\nAdvance the workflow.",
], ids=["without-task", "multiline-task"])
def test_reviewer_composes_author_spec_as_evidence_not_execution(tmp_path, role, task):
    adapter = HostedAdapter("codex")
    adapter._task_text = task
    body = "Write the aggregate artifact.\nAdvance to the next phase.\nPreserve transaction atomicity."
    phase = Phase(id="review", description="Review", prompt=body,
                  required_markers=("atomicity: pass|fail",))
    rendered = adapter.render_envelope(phase, tmp_path / "run", tmp_path, role=role)
    lines = rendered.splitlines()
    quoted = "\n".join(line.removeprefix("> ") for line in lines if line.startswith("> "))
    direct = "\n".join(line for line in lines if not line.startswith("> "))
    if role == "reviewer":
        specification = f"**Task**: {task}\n\n{body}" if task else body
        assert quoted == specification
        assert "**Task**:" not in direct
        for instruction in (task + "\n" + body).splitlines():
            if instruction:
                assert instruction not in direct
    else:
        assert quoted == ""
        assert f"\n{body}\n" in rendered
        if task:
            assert f"**Task**: {task}\n" in rendered
        else:
            assert "**Task**:" not in rendered
    assert "atomicity: pass|fail" in rendered


def test_unknown_envelope_role_is_rejected(tmp_path):
    adapter = HostedAdapter("codex")
    phase = Phase(id="review", description="Review")
    with pytest.raises(ValueError, match="unknown envelope role"):
        adapter.render_envelope(phase, tmp_path / "run", tmp_path, role="observer")


def test_same_timestamp_content_change_invalidates_resolution(tmp_path):
    path = skill(tmp_path, "contract", "old obligation\n")
    context = ResolutionContext()
    args = {"phase_skills": PhaseSkills(required=("contract",))}
    before = resolve(tmp_path, context, **args)
    assert resolve(tmp_path, context, **args) is before
    stamp = path.stat()
    path.write_text("new obligation\n", encoding="utf-8")
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    after = resolve(tmp_path, context, **args)
    assert after is not before
    assert hashlib.sha256(b"new obligation\n").hexdigest() in skill_prompt_block(tmp_path, after)
    assert hashlib.sha256(b"old obligation\n").hexdigest() in skill_prompt_block(tmp_path, before)


def test_clean_reference_manifest_invalidates_unchanged_selection(tmp_path):
    path = skill(tmp_path, "clean-architecture-core", "Core obligation.\n")
    ref = path.parent / "references" / "contract.md"
    ref.parent.mkdir()
    ref.write_text("first", encoding="utf-8")
    context = ResolutionContext()
    args = {"phase_skills": PhaseSkills(required=("clean-architecture-core",))}
    before = resolve(tmp_path, context, **args)
    stamp = ref.stat()
    ref.write_text("other", encoding="utf-8")
    os.utime(ref, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    after = resolve(tmp_path, context, **args)
    assert before.architecture_snapshot.digest == after.architecture_snapshot.digest
    assert before.architecture_norms != after.architecture_norms
    added = ref.parent / "additional.md"
    added.write_text("Additional exception", encoding="utf-8")
    grown = resolve(tmp_path, context, **args)
    assert str(added) in {doc.path for doc in grown.architecture_norms}
    added.unlink()
    assert resolve(tmp_path, context, **args).architecture_norms == after.architecture_norms


def test_profile_scope_provider_authority_and_roots_invalidate_reuse(tmp_path):
    skill(tmp_path, "one", "First norm\n")
    skill(tmp_path, "two", "Second norm\n")
    context = ResolutionContext()
    profile = {"skills": {"required_review": [
        {"group": "api", "skills": ["one"], "path_globs": ["**/*.py"]}
    ]}}
    first = resolve(tmp_path, context, profile=profile, changed_files=("app.py",), provider_authority="first")
    authority = resolve(tmp_path, context, profile=profile, changed_files=("app.py",), provider_authority="second")
    assert authority is not first
    profile["skills"]["required_review"][0]["skills"] = ["two"]
    second = resolve(tmp_path, context, profile=profile, changed_files=("app.py",), provider_authority="second")
    assert {item.name for item in first.required} == {"one"}
    assert {item.name for item in second.required} == {"two"}
    assert not resolve(tmp_path, context, profile=profile, changed_files=("app.txt",)).required
    other_root = tmp_path / "other"
    other_root.mkdir()
    assert not resolve(other_root, context, profile=profile, changed_files=("app.txt",)).required


def test_provider_host_does_not_borrow_another_authority_reference(tmp_path):
    home = Path.home()
    for provider in ("claude", "codex"):
        path = home / f".{provider}" / "skills" / "contract" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"{provider.upper()}_ONLY_NORM\n", encoding="utf-8")
    context = ResolutionContext()
    phase_skills = PhaseSkills(required=("contract",))
    for provider in ("claude", "codex", "claude"):
        result = resolve_phase_skills(project_root=tmp_path, phase_id="review",
                                      phase_skills=phase_skills, host=provider, context=context)
        prompt = skill_prompt_block(tmp_path, result)
        content = f"{provider.upper()}_ONLY_NORM\n".encode()
        assert result.normative_documents[0].content == content
        assert hashlib.sha256(content).hexdigest() in prompt
        assert str(home / f".{provider}" / "skills" / "contract" / "SKILL.md") in prompt
        other = "codex" if provider == "claude" else "claude"
        assert hashlib.sha256(f"{other.upper()}_ONLY_NORM\n".encode()).hexdigest() not in prompt
        assert f"{provider.upper()}_ONLY_NORM" not in prompt
        assert f"{other.upper()}_ONLY_NORM" not in prompt


def test_ordinary_dependencies_and_references_use_same_captured_bytes(tmp_path, monkeypatch):
    original = "---\nrequires: [original-dependency]\n---\nORIGINAL_RULE\n"
    path = skill(tmp_path, "contract", original)
    skill(tmp_path, "original-dependency", "DEPENDENCY_RULE\n")
    original_discover = skill_resolver.discover_skill_catalog

    def replace_after_capture(*args, **kwargs):
        catalog = original_discover(*args, **kwargs)
        path.write_text("---\nrequires: [replacement]\n---\nREPLACEMENT_RULE\n", encoding="utf-8")
        return catalog

    monkeypatch.setattr(skill_resolver, "discover_skill_catalog", replace_after_capture)
    result = resolve(tmp_path, phase_skills=PhaseSkills(required=("contract",)))
    prompt = skill_prompt_block(tmp_path, result)
    assert original not in prompt
    assert "DEPENDENCY_RULE" not in prompt
    captured = next(item for item in result.normative_documents if item.document.path == str(path))
    assert captured.content == original.encode()
    assert captured.document.sha256 == hashlib.sha256(original.encode()).hexdigest()
    assert captured.document.sha256 in prompt
    assert "REPLACEMENT_RULE" not in prompt
    assert {item.name for item in result.required} == {"contract", "original-dependency"}


def test_cache_hit_keeps_install_recovery_boundary_live(tmp_path):
    skill(tmp_path, "contract", "Required norm\n")
    context = ResolutionContext()
    args = {"phase_skills": PhaseSkills(required=("contract",))}
    resolve(tmp_path, context, **args)
    marker = tmp_path / ".agent-flow" / "install-recovery" / "manifest.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="install recovery"):
        resolve(tmp_path, context, **args)


def test_obsolete_custom_required_alias_is_not_silently_migrated(tmp_path):
    skill(tmp_path, "clean-architecture", "Legacy norm\n")
    with pytest.raises(ArchitectureContractError, match="obsolete required skill"):
        resolve(tmp_path, phase_skills=PhaseSkills(required=("clean-architecture",)))
    pinned = resolve(tmp_path, phase_skills=PhaseSkills(required=("clean-architecture",), pinned_legacy=True))
    assert {item.name for item in pinned.required} == {"clean-architecture"}
    assert hashlib.sha256(b"Legacy norm\n").hexdigest() in skill_prompt_block(tmp_path, pinned)
    assert "Legacy norm" not in skill_prompt_block(tmp_path, pinned)


def test_external_domains_and_terms_retain_every_selected_route(tmp_path):
    path = Path.home() / ".codex" / "skills" / "audit" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ndescription: security ownership audit\n---\nExternal norm.\n", encoding="utf-8")
    profile = {"skills": {"external": {
        "enabled": True, "required_budget_bytes": 0,
        "domains": [
            {"id": "security", "terms": ["security", "ownership"]},
            {"id": "architecture", "terms": ["ownership"]},
        ],
    }}}
    result = resolve(tmp_path, profile=profile, concerns=("security", "architecture"))
    routes = [route.detail for route in result.routes if route.kind == "external"]
    assert len(routes) == 3
    assert any("domain='architecture'" in route for route in routes)
    assert any("term='security'" in route for route in routes)
    assert any("term='ownership'" in route for route in routes)
    prompt = skill_prompt_block(tmp_path, result)
    assert "External norm.\n" not in prompt
    assert str(path) in prompt
    assert hashlib.sha256(path.read_bytes()).hexdigest() in prompt
    assert required_read_paths(prompt) == [str(path)]
    assert not any(group.inline for group in result.delivery)


@pytest.mark.parametrize("role", ["author", "reviewer"])
@pytest.mark.parametrize("alias_kind", ["symlink", "copy"])
def test_equal_required_files_have_one_read_and_keep_both_routes(tmp_path, role, alias_kind):
    path = skill(tmp_path, "original", "SHARED_FILE_NORM\n")
    alias = tmp_path / "skills" / "another" / "SKILL.md"
    alias.parent.mkdir()
    if alias_kind == "symlink":
        alias.symlink_to(path)
    else:
        alias.write_bytes(path.read_bytes())
    result = resolve(tmp_path, phase_skills=PhaseSkills(required=("original", "another")))
    assert {item.name for item in result.required} == {"original", "another"}
    prompt = skill_prompt_block(tmp_path, result, role=role)
    assert "SHARED_FILE_NORM\n" not in prompt
    assert {item.document.path for item in result.normative_documents} == {str(path), str(alias)}
    assert required_read_paths(prompt) == [str(path)]
    assert "provenance only, not additional read instructions" in prompt
    assert {item.name for item in result.available_required} == {"original", "another"}
    assert result.missing == ()
    assert {route.skill for item in result.normative_documents for route in item.routes} == {
        "original", "another",
    }
    assert not any(group.inline for group in result.delivery)
    assert result.duplicated_normative_bytes == 0
    for item in result.normative_documents:
        assert item.document.path in prompt
        assert item.document.sha256 in prompt
        for route in item.routes:
            assert f"route: {route.kind} / {route.skill} / {route.detail}" in prompt


@pytest.mark.parametrize("mode", ["local", "pending", "clean"])
@pytest.mark.parametrize("unsafe_kind", ["symlink", "oversized"])
def test_unselected_unsafe_clean_catalog_does_not_block_other_work(tmp_path, mode, unsafe_kind):
    path = Path.home() / ".codex" / "skills" / "clean-architecture-core" / "SKILL.md"
    path.parent.mkdir(parents=True)
    if unsafe_kind == "symlink":
        outside = tmp_path / "outside.md"
        outside.write_text("UNSELECTED_UNSAFE_NORM", encoding="utf-8")
        path.symlink_to(outside)
    else:
        with path.open("wb") as stream:
            stream.truncate(skill_resolver.MAX_ARCHITECTURE_DOCUMENT_BYTES + 1)
    contract_line = "  skill: skills/architecture/SKILL.md\n" if mode == "local" else ""
    (tmp_path / ".agent-flow.project.yaml").write_text(
        f"schema_version: 1\narchitecture:\n  mode: {mode}\n{contract_line}", encoding="utf-8",
    )
    if mode == "local":
        skill(tmp_path, "architecture", "---\nname: architecture\n---\nLOCAL_SELECTED_NORM\n")
    skill(tmp_path, "ordinary", "ORDINARY_REQUIRED_NORM\n")
    context = ResolutionContext()
    for _ in range(2):
        result = resolve_phase_skills(
            project_root=tmp_path, phase_id="explore", host="codex", context=context,
            phase_skills=PhaseSkills(required=("ordinary",)),
        )
        prompt = skill_prompt_block(tmp_path, result)
        assert {item.name for item in result.required} >= {"ordinary"}
        assert hashlib.sha256(b"ORDINARY_REQUIRED_NORM\n").hexdigest() in prompt
        assert "ORDINARY_REQUIRED_NORM" not in prompt
        assert "UNSELECTED_UNSAFE_NORM" not in prompt
    if mode == "clean":
        with pytest.raises(ArchitectureContractError, match="cannot read required skill"):
            resolve(tmp_path, context, phase_skills=PhaseSkills(required=("clean-architecture-core",)))


def test_same_template_retains_source_routes_without_changing_precedence(tmp_path):
    path = skill(tmp_path, "contract", "ONE_BODY_MULTIPLE_AUTHORITIES\n")
    template = str(tmp_path / "skills" / "{skill}" / "SKILL.md")
    profile = {"skill_sources": [
        {"id": "installed-view", "roots": [template]},
        {"id": "second-view", "roots": [template]},
    ]}
    result = resolve(tmp_path, profile=profile, phase_skills=PhaseSkills(required=("contract",)))
    assert result.required[0].source == "project"
    assert result.required[0].path == path
    routes = [route.detail for route in result.routes if route.kind == "root"]
    assert f"project::{template}:" in routes
    assert f"host::{template}:installed-view" in routes
    assert f"host::{template}:second-view" in routes
    prompt = skill_prompt_block(tmp_path, result)
    assert "ONE_BODY_MULTIPLE_AUTHORITIES\n" not in prompt
    assert hashlib.sha256(path.read_bytes()).hexdigest() in prompt
    assert all(route in prompt for route in routes)


def test_same_template_host_routes_remain_distinct_and_host_scoped(tmp_path, monkeypatch):
    authority = tmp_path / "authority"
    skill(authority, "contract", "HOST_SCOPED_SHARED_PATH_NORM\n")
    template = str(authority / "skills" / "{skill}" / "SKILL.md")
    monkeypatch.setattr(skill_resolver, "_HOST_TEMPLATES", {
        "claude": (template,), "codex": (template,), "omp": (),
    })
    declarations = PhaseSkills(required=("contract",))
    all_hosts = resolve_phase_skills(
        project_root=tmp_path, phase_id="implement", host="", phase_skills=declarations,
    )
    all_routes = [route.detail for route in all_hosts.routes if route.kind == "root"]
    assert f"host:claude:{template}:" in all_routes
    assert f"host:codex:{template}:" in all_routes
    codex = resolve(tmp_path, phase_skills=declarations)
    codex_routes = [route.detail for route in codex.routes if route.kind == "root"]
    assert f"host:codex:{template}:" in codex_routes
    assert f"host:claude:{template}:" not in codex_routes
    prompt = skill_prompt_block(tmp_path, codex)
    assert "HOST_SCOPED_SHARED_PATH_NORM\n" not in prompt
    assert hashlib.sha256(b"HOST_SCOPED_SHARED_PATH_NORM\n").hexdigest() in prompt
    assert f"host:codex:{template}:" in prompt
    assert f"host:claude:{template}:" not in prompt


def test_yaml_profile_values_resolve_and_date_to_string_invalidates_capture(tmp_path):
    profile_path = yaml_profile(tmp_path)
    skill(tmp_path, "ordinary", "YAML_PROFILE_REQUIRED_NORM\n")
    profile = load_profile_payload("yaml-values", tmp_path)
    assert profile["adopted"] == date(2026, 1, 1)
    assert profile["metadata"] == {1: "integer key", "1": "string key"}
    assert profile["labels"] == {"security", "architecture"}
    context = ResolutionContext()
    args = {"phase_skills": PhaseSkills(required=("ordinary",))}
    before = resolve(tmp_path, context, profile=profile, **args)
    assert {item.name for item in before.required} == {"ordinary"}
    assert resolve(
        tmp_path, context, profile=load_profile_payload("yaml-values", tmp_path), **args,
    ) is before
    profile_path.write_text(
        profile_path.read_text(encoding="utf-8").replace(
            "adopted: 2026-01-01", "adopted: '2026-01-01'",
        ),
        encoding="utf-8",
    )
    string_profile = load_profile_payload("yaml-values", tmp_path)
    assert string_profile["adopted"] == "2026-01-01"
    after = resolve(tmp_path, context, profile=string_profile, **args)
    assert after is not before
    assert after.required == before.required
    assert resolve(tmp_path, context, profile=string_profile, **args) is after


def test_reviewer_launch_authority_accepts_yaml_values_and_preserves_date_type(tmp_path, monkeypatch):
    profile_path = yaml_profile(tmp_path)
    monkeypatch.setenv("AGENT_FLOW_PROFILE", "yaml-values")
    local_contract(tmp_path, "YAML_LAUNCH_LOCAL_NORM\n")
    adapter = HostedAdapter("codex")
    phase = Phase(id="review", description="Review", skills=PhaseSkills(required=("contract",)))
    run_dir = tmp_path / "run"
    jobs = _reviewer_jobs(phase, run_dir, tmp_path, adapter, providers=("claude", "codex"))
    before = adapter.phase_resolution(phase, tmp_path, skill_host="codex")
    date_authority = adapter._provider_launch_authority
    assert jobs
    for job in jobs:
        for provider in ("claude", "codex"):
            assert job.prompt_for(provider).count("YAML_LAUNCH_LOCAL_NORM\n") == 1
    _reviewer_jobs(phase, run_dir, tmp_path, adapter, providers=("claude", "codex"))
    assert adapter.phase_resolution(phase, tmp_path, skill_host="codex") is before
    profile_path.write_text(
        profile_path.read_text(encoding="utf-8").replace(
            "adopted: 2026-01-01", "adopted: '2026-01-01'",
        ),
        encoding="utf-8",
    )
    _reviewer_jobs(phase, run_dir, tmp_path, adapter, providers=("claude", "codex"))
    assert adapter._provider_launch_authority != date_authority
    assert adapter.phase_resolution(phase, tmp_path, skill_host="codex") is not before
