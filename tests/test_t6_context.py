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
    """Isolate host and home-based discovery state for every context test."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLOW_HOST", "codex")


def skill(root, name, body):
    """Create a minimal skill document under the supplied project root."""
    path = root / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def local_contract(root, body, references=()):
    """Install a local architecture contract with optional required references."""
    declared = f"requires_docs: [{', '.join(references)}]\n" if references else ""
    path = skill(root, "architecture", f"---\nname: contract\n{declared}---\n{body}")
    (root / ".agent-flow").mkdir(exist_ok=True)
    (root / ".agent-flow/project.yaml").write_text(
        "schema_version: 1\narchitecture:\n  mode: local\n  skill: skills/architecture/SKILL.md\n",
        encoding="utf-8",
    )
    return path


def yaml_profile(root):
    """Create a profile containing typed YAML values that must survive resolution."""
    path = root / ".agent-flow" / "profiles" / "yaml-values.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "id: yaml-values\n"
        "adopted: 2026-01-01\n"
        "labels: !!set\n  security: null\n  architecture: null\n"
        "execution:\n  reviewers:\n    - candidates:\n"
        "        - provider: codex\n          model: gpt-5.6-sol\n",
        encoding="utf-8",
    )
    return path


def resolve(root, context=None, **kwargs):
    """Resolve implementation-phase skills with the context-test defaults."""
    return resolve_phase_skills(
        project_root=root, phase_id="implement", host="codex",
        context=context, **kwargs,
    )


def required_read_paths(prompt):
    """Extract ordered skill paths from explicit prompt read instructions."""
    return [line.split("`")[1] for line in prompt.splitlines() if line.startswith("- Read `")]


def test_exact_bodies_merge_all_document_identities_not_similar_text(tmp_path):
    """Require deduplication by exact body while retaining every document identity."""
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
    """Preserve reference digests and every phase, profile, and dependency route."""
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
    """Deliver identical local-contract and reference bodies exactly once."""
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
    if role == "reviewer":
        assert "Bytes:" not in prompt and "route: " not in prompt
        return
    for item in result.normative_documents:
        assert item.document.path in prompt
        assert item.document.sha256 in prompt
        for route in item.routes:
            assert f"route: {route.kind} / {route.skill} / {route.detail}" in prompt


def test_each_author_and_provider_angle_delivers_its_own_complete_body(tmp_path):
    """Give each author and provider-angle prompt one complete normative body."""
    body = "AUTHOR_AND_REVIEWER_NORM_BODY\n"
    local_contract(tmp_path, body)
    ordinary = skill(tmp_path, "ordinary", "REFERENCE_ONLY_AUTHOR_REVIEWER_NORM\n")
    adapter = HostedAdapter("codex")
    phase = Phase(id="review", description="Review", skills=PhaseSkills(required=("contract", "ordinary")))
    run_dir = tmp_path / "run"
    author = adapter.render_envelope(phase, run_dir, tmp_path)
    jobs, _ = _reviewer_jobs(phase, run_dir, tmp_path, adapter, providers=("claude", "codex"))
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
    """Keep the author's specification quoted as reviewer evidence, not executable instructions."""
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
        for instruction in (task + "\n" + body).splitlines():
            if instruction:
                assert instruction in quoted
                assert instruction not in direct
    else:
        assert quoted == ""
        assert f"\n{body}\n" in rendered
        if task:
            assert task in rendered
    # completion marker는 controller 소유다. reviewer에게는 판단 근거가 아니다.
    assert ("atomicity: pass|fail" in rendered) is (role == "author")


def test_unknown_envelope_role_is_rejected(tmp_path):
    """Reject envelope roles outside the author and reviewer contract."""
    adapter = HostedAdapter("codex")
    phase = Phase(id="review", description="Review")
    with pytest.raises(ValueError, match="unknown envelope role"):
        adapter.render_envelope(phase, tmp_path / "run", tmp_path, role="observer")


def test_same_timestamp_content_change_invalidates_resolution(tmp_path):
    """Invalidate captured resolution when bytes change without an mtime change."""
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
    """Invalidate reused selection when a Clean reference manifest changes."""
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
    """Bind cached resolutions to profile scope, provider authority, and roots."""
    # 배치 활성화가 섞이면 profile 표 라우팅이라는 이 테스트의 대상이 가려진다.
    # 절대 안 맞는 선택자를 둬서 표 라우팅으로만 required가 되게 한다.
    skill(tmp_path, "one", "---\npathGlobs: ['never/**']\n---\nFirst norm\n")
    skill(tmp_path, "two", "---\npathGlobs: ['never/**']\n---\nSecond norm\n")
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
    """Prevent one provider host from borrowing another host's reference authority."""
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
    """Use one immutable capture for ordinary dependencies and references."""
    original = "---\nrequires: [original-dependency]\n---\nORIGINAL_RULE\n"
    path = skill(tmp_path, "contract", original)
    skill(tmp_path, "original-dependency", "DEPENDENCY_RULE\n")
    original_discover = skill_resolver.discover_skill_catalog

    def replace_after_capture(*args, **kwargs):
        """Replace the source only after the resolver has captured its bytes."""
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
    """Keep install-recovery validation active even when resolution hits the cache."""
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
    """Fail closed instead of silently migrating an obsolete custom required alias."""
    skill(tmp_path, "clean-architecture", "Legacy norm\n")
    with pytest.raises(ArchitectureContractError, match="obsolete required skill"):
        resolve(tmp_path, phase_skills=PhaseSkills(required=("clean-architecture",)))
    pinned = resolve(tmp_path, phase_skills=PhaseSkills(required=("clean-architecture",), pinned_legacy=True))
    assert {item.name for item in pinned.required} == {"clean-architecture"}
    assert hashlib.sha256(b"Legacy norm\n").hexdigest() in skill_prompt_block(tmp_path, pinned)
    assert "Legacy norm" not in skill_prompt_block(tmp_path, pinned)


def test_external_domains_and_terms_retain_every_selected_route(tmp_path):
    """Retain every route selected through external domains and task terms."""
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
    """Emit one read for equal required files while preserving both provenance routes."""
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
    if role == "reviewer":
        assert "Bytes:" not in prompt and "route: " not in prompt
        return
    for item in result.normative_documents:
        assert item.document.path in prompt
        assert item.document.sha256 in prompt
        for route in item.routes:
            assert f"route: {route.kind} / {route.skill} / {route.detail}" in prompt


def test_bundled_author_read_plan_keeps_aliases_and_digest_without_repeating_routes(tmp_path):
    paths = []
    for name in ("first", "alias"):
        path = tmp_path / ".agent-flow" / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("BUNDLED_SHARED_RULE\n", encoding="utf-8")
        paths.append(path)
    result = resolve(tmp_path, phase_skills=PhaseSkills(required=("first", "alias")))
    assert {item.source for item in result.required} == {"bundled"}
    identities = result.required_document_ids
    prompt = skill_prompt_block(tmp_path, result)
    assert required_read_paths(prompt) == [str(paths[0])]
    assert prompt.count(str(paths[0])) == 1
    assert "`first` (bundled) — body 1" in prompt
    assert "`alias` (bundled) — body 1" in prompt
    assert hashlib.sha256(paths[0].read_bytes()).hexdigest() in prompt
    assert "route: " not in prompt and "Bytes:" not in prompt
    assert result.required_document_ids == identities
    assert {item.document.path for item in result.normative_documents} == set(map(str, paths))
    assert {route.skill for item in result.normative_documents for route in item.routes} == {"first", "alias"}
    reviewer = skill_prompt_block(tmp_path, result, role="reviewer")
    assert required_read_paths(reviewer) == [str(paths[0])]
    assert "Body 1." not in reviewer


@pytest.mark.parametrize("mode", ["local", "pending", "clean"])
@pytest.mark.parametrize("unsafe_kind", ["symlink", "oversized"])
def test_unselected_unsafe_clean_catalog_does_not_block_other_work(tmp_path, mode, unsafe_kind):
    """Ignore unsafe Clean catalog entries that the active work did not select."""
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
    (tmp_path / ".agent-flow").mkdir(exist_ok=True)
    (tmp_path / ".agent-flow/project.yaml").write_text(
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
    """Retain source routes for equal templates without changing precedence."""
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
    """Keep equal-template host routes distinct and scoped to their host."""
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
    """Preserve YAML value types and invalidate capture when a date becomes a string."""
    profile_path = yaml_profile(tmp_path)
    skill(tmp_path, "ordinary", "YAML_PROFILE_REQUIRED_NORM\n")
    profile = load_profile_payload("yaml-values", tmp_path)
    assert profile["adopted"] == date(2026, 1, 1)
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
    """Preserve typed YAML values when reviewer launch authority is captured."""
    profile_path = yaml_profile(tmp_path)
    monkeypatch.setenv("AGENT_FLOW_PROFILE", "yaml-values")
    local_contract(tmp_path, "YAML_LAUNCH_LOCAL_NORM\n")
    adapter = HostedAdapter("codex")
    phase = Phase(id="review", description="Review", skills=PhaseSkills(required=("contract",)))
    run_dir = tmp_path / "run"
    jobs, _ = _reviewer_jobs(phase, run_dir, tmp_path, adapter, providers=("claude", "codex"))
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


@pytest.mark.parametrize("mode", ["clean", "stack", "local", "pending"])
def test_clean_roles_reach_only_clean_mode_envelopes(tmp_path, mode):
    """Clean role lint applies only in Clean; elsewhere the profile roles are a competing norm."""
    adapter = HostedAdapter("codex")
    adapter._profile_id = "sample"
    adapter._profile_snapshot = {
        "architecture": {"roles": {"domain": ["ROLE_GLOB_MARKER/**"]}, "contract": "CONTRACT_MARKER"},
    }
    block = adapter._render_profile_block(Phase(id="implement", description="Implement"), architecture_mode=mode)
    assert ("ROLE_GLOB_MARKER" in block) is (mode == "clean")
    assert ("CONTRACT_MARKER" in block) is (mode == "clean")


def test_reviewer_keeps_read_plan_and_profile_checks_without_controller_markers(tmp_path):
    """Reviewers apply norms plus the profile gates, branching and PR target their angles check;
    aggregate markers, commit convention, angle assignment and reviewer launch configuration
    stay with the controller/runner."""
    ordinary = skill(tmp_path, "ordinary", "ORDINARY_NORM\n")
    adapter = HostedAdapter("codex")
    adapter._profile_id = "sample"
    adapter._profile_snapshot = {
        "gates": [{"id": "test", "command": "GATE_COMMAND_MARKER", "execution": "ci"}],
        "branching": {"base": "BRANCH_MARKER"},
        "pr": {"target_branch": "PR_TARGET_MARKER"},
        "review_angles": [{"id": "ANGLE_MARKER"}],
        "execution": {"reviewers": [{"candidates": [{"provider": "claude", "model": "LAUNCH_MODEL_MARKER"}]}]},
    }
    phase = Phase(id="review", description="Review", skills=PhaseSkills(required=("ordinary",)),
                  required_markers=("atomicity: pass|fail",), multi_review=True)
    reviewer = adapter.render_envelope(phase, tmp_path / "run", tmp_path, role="reviewer")
    assert "## Required-read plan" in reviewer
    assert required_read_paths(reviewer) == [str(ordinary)]
    assert "GATE_COMMAND_MARKER" in reviewer
    assert "execution: ci" in reviewer
    assert "BRANCH_MARKER" in reviewer
    assert "PR_TARGET_MARKER" in reviewer
    assert "ANGLE_MARKER" not in reviewer
    assert "LAUNCH_MODEL_MARKER" not in reviewer
    assert "project-local-skills: checked" not in reviewer
    assert "atomicity: pass|fail" not in reviewer

    controller = adapter.render_envelope(phase, tmp_path / "run", tmp_path)
    assert required_read_paths(controller) == []
    assert "`ordinary`" in controller
    assert "project-local-skills-used: ordinary" in controller
    assert "atomicity: pass|fail" in controller
    assert "ANGLE_MARKER" in controller
    assert "LAUNCH_MODEL_MARKER" in controller
    # controller는 읽지 않은 규범의 읽음/적용을 자기신고하지 않는다. 게이트가 전달 기록을 본다.
    assert "skill-use-evidence" not in controller
    assert "project-local-skill-docs: applied" not in controller


def test_reviewer_assignment_omits_the_controller_completion_gate(tmp_path):
    """The real multi-review prompt: reviewers keep every criterion but not the aggregate gate template."""
    from agent_flow.core.phase_workflow import load_phase_workflow_definition
    from agent_flow.runner import _phases_from_definition

    kit = Path(__file__).resolve().parents[1]
    phase = next(
        item for item in _phases_from_definition(load_phase_workflow_definition(kit, "full-feature"))
        if item.id == "multi-review"
    )
    assert "## Completion Gate" in phase.prompt
    reviewer = HostedAdapter("codex").render_envelope(phase, tmp_path / "run", tmp_path, role="reviewer")
    assignment = reviewer.split("## Current review-phase assignment", 1)[1]
    assert "## Completion Gate" not in reviewer
    assert "codex-claude-parity-check" not in reviewer
    assert "Include the Completion Gate" not in reviewer
    assert "Completion gate below" not in assignment
    assert "Pending mode does not authorize structural decisions." in assignment
    assert "local mode assesses its own complete" in assignment
    assert "must-avoid violation or failed required criterion" in assignment
    assert "Any request-changes from any reviewer makes the overall verdict" in assignment
    assert "`verdict: approve` or `verdict: request-changes`." in assignment


def test_reviewer_assignment_omits_a_top_level_completion_gate_section():
    """반증: 들여쓴 템플릿만 지우면 custom workflow가 최상위 `## Completion Gate` 절로 적은
    controller marker가 reviewer에게 그대로 간다. 다음 같은 수준 제목부터는 남는다."""
    from agent_flow.adapters.base import _without_completion_gate

    body = (
        "Review the change.\n\n"
        "## Completion Gate\n"
        "codex-claude-parity-check: pass\n"
        "- aggregate-verdict: approve|request-changes\n\n"
        "## Criteria\n"
        "Reject any must-avoid violation.\n"
    )
    stripped = _without_completion_gate(body)
    assert "Completion Gate" not in stripped
    assert "codex-claude-parity-check" not in stripped
    assert "aggregate-verdict" not in stripped
    assert "## Criteria\nReject any must-avoid violation." in stripped
    assert stripped.startswith("Review the change.")


@pytest.mark.parametrize(
    ("body", "kept"),
    [
        pytest.param(
            "Follow the effective Completion gate below for this run. Local mode assesses its own "
            "contract instead. Pending mode does not authorize structural decisions.",
            "Local mode assesses its own contract instead. Pending mode does not authorize structural decisions.",
            id="same-line",
        ),
        pytest.param(
            "Follow the effective Completion gate below for this run. Reject any must-avoid violation.",
            "Reject any must-avoid violation.",
            id="no-terminator",
        ),
    ],
)
def test_reviewer_assignment_keeps_criteria_around_the_gate_pointer(body, kept):
    """반증: 게이트를 가리키는 문장과 함께 뒤따르는 계약 적용 기준까지 지우면 reviewer가 그 기준을 잃는다."""
    from agent_flow.adapters.base import _without_completion_gate

    stripped = _without_completion_gate(body)
    assert kept in stripped
    assert "Completion gate below" not in stripped
