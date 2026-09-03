"""외부 skill이 카탈로그에 들어오는가, 그리고 그것만으로 required가 되지는 않는가.

두 성질이 함께여야 한다. 하나만 지키면 각각 다른 사고가 된다 — 카탈로그가 닫혀 있으면
host에 깔린 skill을 영원히 못 보고(실측 953개 중 `workflowPhases` 보유 0개), 카탈로그가
열렸는데 활성화 가드가 없으면 깔린 skill 전량이 선택자 없는 엔트리로 required가 된다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SRC = str(REPO / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core import skill_catalog
from agent_flow.core.phase_workflow import declared_phase_skills
from agent_flow.core.profiles import load_profile_payload
from agent_flow.core.skill_resolver import (
    CODE_PHASES,
    SkillRoot,
    discover_skill_catalog,
    resolve_phase_skills,
)

PROFILES_DIR = REPO / "src" / "agent_flow" / "profiles"


def _write_skill(directory: Path, name: str, body: str) -> Path:
    path = directory / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _upstream_skill(directory: Path, name: str, description: str) -> Path:
    """upstream이 실제로 쓰는 형태. `name` + `description`뿐이다."""
    return _write_skill(
        directory,
        name,
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
    )


def _host_root(directory: Path) -> SkillRoot:
    return SkillRoot(source="host", template=str(directory / "{skill}" / "SKILL.md"))


def _bundled_shipped_skill(project: Path, name: str) -> Path:
    """install이 kit skill을 앉히는 자리. source가 `bundled`라 스스로 선언해야 활성화된다."""
    return _write_skill(
        project / ".agent-flow" / "skills",
        name,
        (REPO / "skills" / name / "SKILL.md").read_text(encoding="utf-8"),
    )


def _required_for(
    project: Path,
    changed_files: list[str],
    *,
    profile: dict | None = None,
    task_text: str = "",
) -> set[str]:
    return {
        skill.name
        for skill in resolve_phase_skills(
            project_root=project,
            phase_id="implement",
            changed_files=changed_files,
            task_text=task_text,
            host="claude",
            profile=profile,
        ).required
    }




def test_bundled_skill_relative_markdown_links_resolve():
    link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    broken = []
    markdown_files = (
        *(REPO / "skills").glob("*/SKILL.md"),
        *(REPO / "skills").glob("*/references/**/*.md"),
    )

    for skill_file in sorted(markdown_files):
        for target in link_pattern.findall(skill_file.read_text(encoding="utf-8")):
            relative_target = target.split("#", 1)[0]
            if not relative_target or relative_target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (skill_file.parent / relative_target).resolve().exists():
                broken.append((skill_file.relative_to(REPO).as_posix(), target))

    assert broken == []

def test_external_skill_without_workflow_phases_enters_catalog(tmp_path):
    """현행 `_catalog_entry`의 early return으로는 반드시 실패하는 계약."""
    host = tmp_path / "host"
    _upstream_skill(host, "edge-to-edge", "Use when insets or system bars change.")

    catalog = discover_skill_catalog(tmp_path, (_host_root(host),))

    entry = next(item for item in catalog if item.name == "edge-to-edge")
    assert entry.description == "Use when insets or system bars change."
    assert entry.phase_declared is False


def test_catalog_keeps_upstream_keywords_when_present(tmp_path):
    host = tmp_path / "host"
    _write_skill(
        host,
        "camerax",
        "---\nname: camerax\ndescription: Camera guidance.\n"
        "metadata:\n  keywords:\n  - CameraX\n  - Camera2\n---\n",
    )

    catalog = discover_skill_catalog(tmp_path, (_host_root(host),))

    entry = next(item for item in catalog if item.name == "camerax")
    assert entry.keywords == ("camerax", "camera2")

def test_catalog_keeps_skill_release_governance_metadata(tmp_path):
    host = tmp_path / "host"
    _write_skill(
        host,
        "governed",
        "---\nname: governed\ndescription: Governed skill.\n"
        "version: 1.2.3\nowner: platform\nlifecycle: active\n"
        "approval: approved\nprovenance: internal\n---\n",
    )

    catalog = discover_skill_catalog(tmp_path, (_host_root(host),))

    entry = next(item for item in catalog if item.name == "governed")
    assert entry.version == "1.2.3"
    assert entry.owner == "platform"
    assert entry.lifecycle == "active"
    assert entry.approval == "approved"
    assert entry.provenance == "internal"



def test_host_skill_in_catalog_does_not_become_required(tmp_path, monkeypatch):
    """카탈로그를 여는 변경이 required 집합을 건드리지 않는다는 증명."""
    home = tmp_path / "home"
    _upstream_skill(home / ".claude" / "skills", "swiftui-pro", "Use when writing SwiftUI code.")
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "app"
    project.mkdir()

    resolution = resolve_phase_skills(
        project_root=project,
        phase_id="implement",
        task_text="write SwiftUI code",
        host="claude",
    )

    assert "swiftui-pro" not in {skill.name for skill in resolution.required}


def test_project_local_skill_without_declarations_still_activates(tmp_path):
    """`.agent-flow/local-skills/`는 거기 둔 것 자체가 선언이다. 가드가 이걸 깨서는 안 된다."""
    project = tmp_path / "app"
    _upstream_skill(project / ".agent-flow" / "local-skills", "house-rules", "Project rules.")

    resolution = resolve_phase_skills(
        project_root=project,
        phase_id="implement",
        task_text="anything",
        host="claude",
    )

    assert "house-rules" in {skill.name for skill in resolution.required}


def test_catalog_reflects_a_skill_edited_in_the_same_process(tmp_path):
    """캐시 키가 template뿐이면 갱신된 skill을 못 본다. 사용자가 `skills update`를 돌린 직후가 그 경로다."""
    host = tmp_path / "host"
    _upstream_skill(host, "diagnose", "Old description.")
    first = discover_skill_catalog(tmp_path, (_host_root(host),))
    _upstream_skill(host, "diagnose", "New description that is clearly longer.")

    second = discover_skill_catalog(tmp_path, (_host_root(host),))

    assert next(item for item in first if item.name == "diagnose").description == "Old description."
    assert (
        next(item for item in second if item.name == "diagnose").description
        == "New description that is clearly longer."
    )


def test_symlinked_duplicate_is_catalogued_once(tmp_path):
    """`npx skills`는 실파일을 `~/.agents/skills`에 두고 host 디렉터리에 symlink를 건다."""
    shared = tmp_path / "shared"
    real = _upstream_skill(shared, "shepherd", "Use when shepherding PRs.")
    host = tmp_path / "host"
    host.mkdir(parents=True, exist_ok=True)
    os.symlink(real.parent, host / "shepherd")

    catalog = discover_skill_catalog(
        tmp_path,
        (_host_root(host), SkillRoot(source="shared", template=str(shared / "{skill}" / "SKILL.md"))),
    )

    assert [item.name for item in catalog].count("shepherd") == 1


def test_lock_schema_mismatch_is_strict_until_scan_migrates_it(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    project = tmp_path / "app"
    result = skill_catalog.scan(project, profile=None, host="claude")
    skill_catalog.write_lock(project, result)

    path = skill_catalog.lock_path(project)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["version"] = skill_catalog.LOCK_VERSION + 1
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert skill_catalog.read_lock(project) == {}
    stale = skill_catalog.scan(project, profile=None, host="claude")
    stale_findings = [
        finding
        for finding in stale.findings
        if finding.kind == skill_catalog.LOCK_STALE
    ]
    assert len(stale_findings) == 1
    assert skill_catalog.strict_findings(stale_findings) == tuple(stale_findings)

    skill_catalog.write_lock(project, stale)
    assert skill_catalog.read_lock(project)["version"] == skill_catalog.LOCK_VERSION

def test_lock_tracks_reference_content_drift_per_host_profile_view(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    project = tmp_path / "app"
    reference = project / "skills" / "governed" / "references" / "contract.md"
    reference.parent.mkdir(parents=True)
    _write_skill(
        project / "skills",
        "governed",
        "---\nname: governed\ndescription: Governed skill.\n---\n",
    )
    reference.write_text("first\n", encoding="utf-8")
    profile = {"id": "python"}
    first = skill_catalog.scan(
        project,
        profile=profile,
        profile_ids=("python",),
        host="claude",
    )
    skill_catalog.write_lock(project, first)
    payload = skill_catalog.read_lock(project)
    view = next(iter(payload["views"].values()))
    first_digest = view["skills"]["governed"]["observedContentDigest"]

    reference.write_text("second\n", encoding="utf-8")
    second = skill_catalog.scan(
        project,
        profile=profile,
        profile_ids=("python",),
        host="claude",
    )

    assert any(
        finding.kind == skill_catalog.CONTENT_CHANGED
        and finding.name == "governed"
        for finding in second.findings
    )
    skill_catalog.write_lock(project, second)
    updated_view = next(iter(skill_catalog.read_lock(project)["views"].values()))
    assert (
        updated_view["skills"]["governed"]["observedContentDigest"]
        != first_digest
    )


def test_lock_preserves_independent_host_profile_views(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    project = tmp_path / "app"
    _upstream_skill(project / "skills", "governed", "Governed skill.")

    claude = skill_catalog.scan(
        project,
        profile={"id": "python"},
        profile_ids=("python",),
        host="claude",
    )
    skill_catalog.write_lock(project, claude)
    codex = skill_catalog.scan(
        project,
        profile={"id": "generic"},
        profile_ids=("generic",),
        host="codex",
    )

    assert [finding.kind for finding in codex.findings].count(
        skill_catalog.NEW_VIEW
    ) == 1
    skill_catalog.write_lock(project, codex)
    payload = skill_catalog.read_lock(project)

    assert len(payload["views"]) == 2
    assert {view["host"] for view in payload["views"].values()} == {
        "claude",
        "codex",
    }


def test_lock_write_lease_spans_view_merge_and_atomic_replace(
    tmp_path, monkeypatch
):
    from agent_flow.core.worktree_isolation import (
        FileLeaseUnavailable,
        shared_file_lease,
    )

    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    project = tmp_path / "app"
    result = skill_catalog.scan(project, profile=None, host="claude")
    lease_path = project / skill_catalog.LOCK_WRITE_LEASE_RELATIVE
    observed = {"load": False, "write": False}
    original_load = skill_catalog._load_lock
    original_write = skill_catalog.atomic_write_text

    def assert_lease_held(label):
        with pytest.raises(FileLeaseUnavailable), shared_file_lease(lease_path):
            pass
        observed[label] = True

    def guarded_load(root):
        assert_lease_held("load")
        return original_load(root)

    def guarded_write(path, content):
        assert_lease_held("write")
        return original_write(path, content)

    monkeypatch.setattr(skill_catalog, "_load_lock", guarded_load)
    monkeypatch.setattr(skill_catalog, "atomic_write_text", guarded_write)

    skill_catalog.write_lock(project, result)

    assert observed == {"load": True, "write": True}


def test_unreadable_lock_is_distinct_and_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    project = tmp_path / "app"
    path = skill_catalog.lock_path(project)
    path.parent.mkdir(parents=True)
    path.write_text("{}\n", encoding="utf-8")

    def deny_read(*_args, **_kwargs):
        raise PermissionError("read denied")

    monkeypatch.setattr(skill_catalog, "read_bounded_regular_file", deny_read)
    result = skill_catalog.scan(project, host="claude")

    unreadable = [
        finding
        for finding in result.findings
        if finding.kind == skill_catalog.LOCK_UNREADABLE
    ]
    assert len(unreadable) == 1
    assert skill_catalog.strict_findings(unreadable) == tuple(unreadable)
    with pytest.raises(ValueError, match="catalog lock is unreadable"):
        skill_catalog.write_lock(project, result)
    assert path.read_text(encoding="utf-8") == "{}\n"


def test_corrupt_lock_is_reported_and_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    project = tmp_path / "app"
    path = skill_catalog.lock_path(project)
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")

    result = skill_catalog.scan(project, host="claude")

    assert any(
        finding.kind == skill_catalog.LOCK_INVALID
        for finding in result.findings
    )
    with pytest.raises(ValueError, match="catalog lock"):
        skill_catalog.write_lock(project, result)
    assert path.read_text(encoding="utf-8") == "{"



def test_current_lock_schema_corruption_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    project = tmp_path / "app"
    path = skill_catalog.lock_path(project)
    path.parent.mkdir(parents=True)
    payload = {
        "version": skill_catalog.LOCK_VERSION,
        "views": {
            json.dumps(["claude"], separators=(",", ":")): {
                "host": "claude",
                "profiles": [],
                "stamp": "",
                "sources": {},
                "skills": {"broken": "not-a-record"},
            }
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = skill_catalog.scan(project, host="claude")

    assert any(
        finding.kind == skill_catalog.LOCK_INVALID
        for finding in result.findings
    )
    with pytest.raises(ValueError, match="catalog lock"):
        skill_catalog.write_lock(project, result)


def test_cli_doctor_strict_fails_only_for_hard_findings(
    tmp_path, monkeypatch
):
    from agent_flow import cli
    from agent_flow.core.phase_workflow import DeclaredPhaseSkills

    monkeypatch.setattr(cli, "active_profile_ids", lambda root, selected: ("generic",))
    monkeypatch.setattr(
        cli,
        "load_profile_payload",
        lambda profile_id, root: {"id": profile_id},
    )
    monkeypatch.setattr(
        cli,
        "_workflow_declarations",
        lambda: DeclaredPhaseSkills((), ()),
    )
    advisory = skill_catalog.CatalogScan(
        stamp="advisory",
        findings=(
            skill_catalog.CatalogFinding(skill_catalog.UNROUTED, "optional"),
        ),
    )
    monkeypatch.setattr(skill_catalog, "scan", lambda *args, **kwargs: advisory)

    assert (
        cli.main(
            ["skills", "doctor", "--root", str(tmp_path), "--strict"]
        )
        == 0
    )

    hard = skill_catalog.CatalogScan(
        stamp="hard",
        findings=(
            skill_catalog.CatalogFinding(
                skill_catalog.CONTENT_CHANGED, "governed"
            ),
        ),
    )
    monkeypatch.setattr(skill_catalog, "scan", lambda *args, **kwargs: hard)

    assert (
        cli.main(
            ["skills", "doctor", "--root", str(tmp_path), "--strict"]
        )
        == 1
    )


def test_cli_doctor_strict_fails_when_workflow_declarations_are_incomplete(
    tmp_path, monkeypatch
):
    from agent_flow import cli
    from agent_flow.core.phase_workflow import DeclaredPhaseSkills

    monkeypatch.setattr(cli, "active_profile_ids", lambda root, selected: ("generic",))
    monkeypatch.setattr(
        cli,
        "load_profile_payload",
        lambda profile_id, root: {"id": profile_id},
    )
    monkeypatch.setattr(
        cli,
        "_workflow_declarations",
        lambda: DeclaredPhaseSkills((), ("workflow broken: invalid YAML",)),
    )
    monkeypatch.setattr(
        skill_catalog,
        "scan",
        lambda *args, **kwargs: skill_catalog.CatalogScan(stamp="degraded"),
    )

    assert cli.main(["skills", "doctor", "--root", str(tmp_path)]) == 0
    assert (
        cli.main(["skills", "doctor", "--root", str(tmp_path), "--strict"])
        == 1
    )

def test_strict_findings_exclude_advisory_routing_observations():
    advisory = (
        skill_catalog.CatalogFinding(skill_catalog.UNROUTED, "optional"),
        skill_catalog.CatalogFinding(skill_catalog.SHADOWED, "alias"),
    )
    hard = (
        *advisory,
        skill_catalog.CatalogFinding(
            skill_catalog.CONTENT_CHANGED, "governed"
        ),
    )

    assert skill_catalog.strict_findings(advisory) == ()
    assert [finding.kind for finding in skill_catalog.strict_findings(hard)] == [
        skill_catalog.CONTENT_CHANGED
    ]


def test_doctor_reports_invalid_and_disallowed_routed_governance(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    project = tmp_path / "app"
    _write_skill(
        project / ".agent-flow" / "local-skills",
        "retired",
        "---\nname: retired\ndescription: Retired skill.\nversion: release\n"
        "lifecycle: retired\napproval: approved\n---\n",
    )
    _write_skill(
        project / ".agent-flow" / "local-skills",
        "pending",
        "---\nname: pending\ndescription: Pending skill.\n"
        "lifecycle: active\napproval: pending\n---\n",
    )

    result = skill_catalog.scan(project, host="claude")

    assert {
        (finding.kind, finding.name)
        for finding in result.findings
    } >= {
        (skill_catalog.INVALID_GOVERNANCE, "retired"),
        (skill_catalog.RETIRED_ROUTED, "retired"),
        (skill_catalog.UNAPPROVED_ROUTED, "pending"),
    }

def test_governance_view_excludes_inactive_hosts_and_external_invalid_is_advisory(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    project = tmp_path / "app"
    claude_root = tmp_path / "claude-skills"
    _write_skill(
        claude_root,
        "foreign",
        "---\nname: foreign\ndescription: Foreign skill.\nversion: release\n"
        "lifecycle: retired\napproval: pending\nworkflowPhases:\n  - implementation\n"
        "pathGlobs:\n  - '**/*.py'\n---\n",
    )
    roots = (
        SkillRoot("project", str(project / "skills" / "{skill}" / "SKILL.md")),
        SkillRoot(
            "host",
            str(claude_root / "{skill}" / "SKILL.md"),
            host="claude",
        ),
    )
    monkeypatch.setattr(skill_catalog, "skill_roots", lambda *args, **kwargs: roots)

    codex = skill_catalog.scan(project, host="codex")
    claude = skill_catalog.scan(project, host="claude")

    assert {entry.name for entry in codex.entries} == {"foreign"}
    assert "foreign" not in codex.skills
    assert not any(
        finding.kind == skill_catalog.INVALID_GOVERNANCE
        for finding in codex.findings
    )
    governance = [
        finding
        for finding in claude.findings
        if finding.kind
        in {
            skill_catalog.INVALID_GOVERNANCE,
            skill_catalog.RETIRED_ROUTED,
            skill_catalog.UNAPPROVED_ROUTED,
        }
    ]
    assert {(finding.kind, finding.name) for finding in governance} == {
        (skill_catalog.INVALID_GOVERNANCE, "foreign"),
        (skill_catalog.RETIRED_ROUTED, "foreign"),
        (skill_catalog.UNAPPROVED_ROUTED, "foreign"),
    }
    assert skill_catalog.strict_findings(governance) == ()


def test_owned_governance_rejects_numeric_prerelease_leading_zero(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    project = tmp_path / "app"
    _write_skill(
        project / ".agent-flow" / "local-skills",
        "candidate",
        "---\nname: candidate\ndescription: Candidate skill.\n"
        "version: 1.2.3-01\napproval: approved\n---\n",
    )

    result = skill_catalog.scan(project, host="claude")
    invalid = [
        finding
        for finding in result.findings
        if finding.kind == skill_catalog.INVALID_GOVERNANCE
    ]

    assert [finding.name for finding in invalid] == ["candidate"]
    assert skill_catalog.strict_findings(invalid) == tuple(invalid)


def test_owned_governance_rejects_structured_scalars(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    project = tmp_path / "app"
    _write_skill(
        project / ".agent-flow" / "local-skills",
        "structured",
        "---\nname: structured\ndescription: Structured governance.\n"
        "version:\n  - 1.2.3\nowner: [platform]\n"
        "lifecycle:\n  status: active\napproval: [approved]\n"
        "provenance:\n  source: internal\n---\n",
    )

    result = skill_catalog.scan(project, host="claude")
    invalid = [
        finding
        for finding in result.findings
        if finding.kind == skill_catalog.INVALID_GOVERNANCE
    ]

    assert [finding.name for finding in invalid] == ["structured"]
    assert all(
        f"{field}=structured" in invalid[0].detail
        for field in ("version", "owner", "lifecycle", "approval", "provenance")
    )
    assert skill_catalog.strict_findings(invalid) == tuple(invalid)


def test_owned_governance_uses_the_final_duplicate_value(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    project = tmp_path / "app"
    _write_skill(
        project / ".agent-flow" / "local-skills",
        "duplicate",
        "---\nname: duplicate\ndescription: Duplicate governance.\n"
        "approval: approved\napproval: [rejected]\n---\n",
    )

    result = skill_catalog.scan(project, host="claude")
    invalid = [
        finding
        for finding in result.findings
        if finding.kind == skill_catalog.INVALID_GOVERNANCE
    ]

    assert [finding.name for finding in invalid] == ["duplicate"]
    assert "approval=structured" in invalid[0].detail
    assert skill_catalog.strict_findings(invalid) == tuple(invalid)


@pytest.mark.parametrize(
    "frontmatter",
    (
        "---\nname: malformed\napproval: [\n---\n",
        "---\n- name\n- malformed\n---\n",
    ),
)
def test_owned_governance_rejects_malformed_frontmatter(
    frontmatter, tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    project = tmp_path / "app"
    _write_skill(
        project / ".agent-flow" / "local-skills",
        "malformed",
        frontmatter,
    )

    result = skill_catalog.scan(project, host="claude")
    invalid = [
        finding
        for finding in result.findings
        if finding.kind == skill_catalog.INVALID_GOVERNANCE
    ]

    assert [finding.name for finding in invalid] == ["malformed"]
    assert "approval=frontmatter-invalid" in invalid[0].detail
    assert skill_catalog.strict_findings(invalid) == tuple(invalid)


def test_unreadable_content_preserves_the_previous_lock_baseline(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    project = tmp_path / "app"
    _upstream_skill(project / "skills", "broken", "Broken skill.")
    initial = skill_catalog.scan(project, host="claude")
    skill_catalog.write_lock(project, initial)
    path = skill_catalog.lock_path(project)
    baseline = path.read_text(encoding="utf-8")

    def fail_digest(_directory):
        raise OSError("read denied")

    monkeypatch.setattr(
        skill_catalog,
        "skill_observed_content_digest",
        fail_digest,
    )

    result = skill_catalog.scan(project, host="claude")
    unreadable = [
        finding
        for finding in result.findings
        if finding.kind == skill_catalog.CONTENT_UNREADABLE
    ]

    assert [finding.name for finding in unreadable] == ["broken"]
    assert "broken" not in result.skills
    assert skill_catalog.strict_findings(unreadable) == tuple(unreadable)
    assert not any(
        finding.kind == skill_catalog.REMOVED and finding.name == "broken"
        for finding in result.findings
    )
    with pytest.raises(ValueError, match="unreadable skill content"):
        skill_catalog.write_lock(project, result)
    assert path.read_text(encoding="utf-8") == baseline


def test_profile_id_string_is_one_view_component(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))

    result = skill_catalog.scan(
        tmp_path / "app",
        profile_ids="python",
        host="claude",
    )

    assert result.profile_ids == ("python",)
    assert result.view_id == '["claude","python"]'

def test_lock_records_moving_ref_and_resolved_source_sha(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    monkeypatch.setattr(
        skill_catalog,
        "cached_source_sha",
        lambda source, env=None: "abc123",
    )
    project = tmp_path / "app"
    profile = {
        "id": "python",
        "skill_sources": [
            {
                "id": "upstream",
                "kind": "fetch",
                "url": "https://example.invalid/skills.git",
                "ref": "main",
                "layout": "skills/{skill}/SKILL.md",
            }
        ],
    }

    result = skill_catalog.scan(
        project,
        profile=profile,
        profile_ids=("python",),
        host="claude",
    )
    skill_catalog.write_lock(project, result)
    view = next(iter(skill_catalog.read_lock(project)["views"].values()))

    assert view["sources"]["upstream"] == {
        "kind": "fetch",
        "url": "https://example.invalid/skills.git",
        "ref": "main",
        "resolvedSha": "abc123",
    }


def test_catalog_content_and_source_sha_share_the_explicit_cache_env(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    project = tmp_path / "app"
    cache = tmp_path / "custom-cache"
    _upstream_skill(
        cache / "upstream" / "main" / "skills",
        "cached",
        "Skill from the explicit cache.",
    )
    env = {"AGENT_FLOW_SKILL_CACHE": str(cache)}
    observed_envs = []

    def source_sha(_source, env=None):
        observed_envs.append(env)
        return "custom-cache-sha"

    monkeypatch.setattr(skill_catalog, "cached_source_sha", source_sha)
    profile = {
        "id": "python",
        "skill_sources": [
            {
                "id": "upstream",
                "kind": "fetch",
                "url": "https://example.invalid/skills.git",
                "ref": "main",
                "layout": "skills/{skill}/SKILL.md",
            }
        ],
    }

    result = skill_catalog.scan(
        project,
        profile=profile,
        profile_ids=("python",),
        host="claude",
        env=env,
    )

    assert result.skills["cached"]["source"] == "fetched"
    assert result.sources["upstream"]["resolvedSha"] == "custom-cache-sha"
    assert observed_envs == [env]



def test_doctor_names_a_declared_skill_that_is_not_installed(tmp_path, monkeypatch):
    """`camera1-to-camerax`처럼 upstream이 지운 이름이 선언에 남은 상태를 이름으로 지목한다."""
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    project = tmp_path / "app"
    project.mkdir()
    profile = {
        "skills": {
            "required_review": [
                {
                    "group": "profile",
                    "skills": ["camera1-to-camerax"],
                    "task_terms": ["camerax"],
                }
            ]
        }
    }

    result = skill_catalog.scan(project, profile=profile, host="claude")

    dead = [
        finding.name
        for finding in result.findings
        if finding.kind == skill_catalog.DEAD_DECLARATION
    ]
    assert dead == ["camera1-to-camerax"]


def test_declared_skill_installed_only_for_another_host_is_missing(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    _upstream_skill(
        home / ".codex" / "skills",
        "codex-only",
        "Only the Codex host can load this skill.",
    )
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "app"
    project.mkdir()
    profile = {
        "skills": {
            "required_review": [
                {
                    "group": "profile",
                    "skills": ["codex-only"],
                    "task_terms": ["review"],
                }
            ]
        }
    }

    result = skill_catalog.scan(project, profile=profile, host="claude")

    assert [
        finding.name
        for finding in result.findings
        if finding.kind == skill_catalog.DEAD_DECLARATION
    ] == ["codex-only"]
    assert skill_catalog.strict_findings(result.findings)


def test_doctor_reports_an_installed_skill_that_no_declaration_routes(tmp_path, monkeypatch):
    """디스크에 있고 선언에 없는 것 — 오늘 영구히 안 보이는 12개가 이 종류다."""
    home = tmp_path / "home"
    _upstream_skill(home / ".claude" / "skills", "wear-compose-m3", "Use for Wear OS Material3.")
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "app"
    project.mkdir()

    result = skill_catalog.scan(project, profile={}, host="claude")

    unrouted = [
        finding.name for finding in result.findings if finding.kind == skill_catalog.UNROUTED
    ]
    assert "wear-compose-m3" in unrouted


def test_lock_diff_reports_new_and_removed_names(tmp_path, monkeypatch):
    home = tmp_path / "home"
    skills = home / ".claude" / "skills"
    _upstream_skill(skills, "diagnose", "Use when debugging.")
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "app"
    project.mkdir()
    skill_catalog.write_lock(project, skill_catalog.scan(project, profile={}, host="claude"))

    _upstream_skill(skills, "zoom-out", "Use when stepping back.")
    (skills / "diagnose" / "SKILL.md").unlink()
    result = skill_catalog.scan(project, profile={}, host="claude")

    kinds = {(finding.kind, finding.name) for finding in result.findings}
    assert (skill_catalog.NEW, "zoom-out") in kinds
    assert (skill_catalog.REMOVED, "diagnose") in kinds


def test_doctor_does_not_report_a_vocabulary_routable_skill_as_unrouted(tmp_path, monkeypatch):
    """실측: 어휘를 안 보던 doctor가 정상 라우팅되는 skill 122개를 미라우팅으로 보고했다."""
    home = tmp_path / "home"
    _upstream_skill(home / ".claude" / "skills", "edge-to-edge", "Use when insets overlap.")
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "app"
    project.mkdir()
    profile = {
        "skills": {
            "external": {
                "enabled": True,
                "domains": [
                    {"id": "ui", "terms": ["insets"], "phases": ["implementation"]}
                ],
            }
        }
    }

    result = skill_catalog.scan(project, profile=profile, host="claude")

    unrouted = [
        finding.name for finding in result.findings if finding.kind == skill_catalog.UNROUTED
    ]
    assert "edge-to-edge" not in unrouted


def test_doctor_reports_a_project_skill_that_shadows_an_installed_one(tmp_path, monkeypatch):
    """카탈로그는 우선순위 root의 것 하나만 담는다. 그 그림자가 조용하면 사용자는
    자기 skill이 왜 안 쓰이는지 알 수 없다."""
    home = tmp_path / "home"
    _upstream_skill(home / ".claude" / "skills", "edge-to-edge", "Upstream copy.")
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "app"
    _upstream_skill(project / "skills", "edge-to-edge", "Project copy.")

    result = skill_catalog.scan(project, profile={}, host="claude")

    collisions = [
        finding.name for finding in result.findings if finding.kind == skill_catalog.COLLISION
    ]
    assert collisions == ["edge-to-edge"]


def test_cross_host_skill_copies_do_not_collide(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _upstream_skill(
        home / ".claude" / "skills",
        "shared-name",
        "Claude-owned copy.",
    )
    _upstream_skill(
        home / ".codex" / "skills",
        "shared-name",
        "Codex-owned copy.",
    )
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "app"
    project.mkdir()

    result = skill_catalog.scan(project, profile={}, host="claude")

    assert not any(
        finding.kind == skill_catalog.COLLISION for finding in result.findings
    )


def test_doctor_reports_an_unrouted_bundled_skill(tmp_path, monkeypatch):
    """실측: `bundled`/`project`를 검사에서 빼 둔 탓에 profile이 설치하는 25개 중 21개가
    어느 phase에도 붙지 않는 상태로 방치됐다. 관측이 없으면 그 드리프트는 조용하다."""
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    project = tmp_path / "app"
    shipped = project / ".agent-flow" / "skills"
    _upstream_skill(shipped, "orphan-guide", "Use when nothing routes it.")
    _upstream_skill(shipped, "phase-declared-guide", "Use when a workflow phase declares it.")

    result = skill_catalog.scan(
        project,
        profile={},
        host="claude",
        workflow_skills=("phase-declared-guide",),
    )

    unrouted = [
        finding.name for finding in result.findings if finding.kind == skill_catalog.UNROUTED
    ]
    assert "orphan-guide" in unrouted
    assert "phase-declared-guide" not in unrouted


# 활성 profile을 함께 준다. profile 없이 판정하면 표(`required_review`)의 glob이
# skill frontmatter의 좁은 범위를 덮어쓰는 역전을 가드가 볼 수 없다.
_PRESENTATION_PROFILES = {
    "android-clean-presentation-architecture": "android",
    "flutter-clean-presentation-architecture": "flutter",
    "react-clean-presentation-architecture": "nextjs",
    "react-native-clean-presentation-architecture": "react-native",
    "ios-clean-presentation-architecture": "ios",
}

_PRESENTATION_CHANGES = {
    "android-clean-presentation-architecture": "feature/chat/presentation/src/main/java/io/levvels/samantha/feature/chat/presentation/ChatViewModel.kt",
    "flutter-clean-presentation-architecture": "lib/features/chat/presentation/chat/chat_notifier.dart",
    "react-clean-presentation-architecture": "src/features/chat/presentation/ChatContainer.tsx",
    "react-native-clean-presentation-architecture": "src/features/chat/presentation/ChatScreen.tsx",
    "ios-clean-presentation-architecture": "Sources/Features/Chat/Presentation/ChatViewModel.swift",
}

_DATA_LAYER_CHANGES = {
    "android-clean-presentation-architecture": "core/data/chat/src/main/java/io/levvels/samantha/core/data/chat/ChatRepositoryImpl.kt",
    "flutter-clean-presentation-architecture": "lib/core/data/chat/chat_repository_impl.dart",
    "react-clean-presentation-architecture": "src/core/data/chat/ChatRepositoryImpl.ts",
    "react-native-clean-presentation-architecture": "src/core/data/chat/ChatRepositoryImpl.ts",
    "ios-clean-presentation-architecture": "Sources/Core/Data/Chat/ChatRepositoryImpl.swift",
}


@pytest.mark.parametrize("name", sorted(_PRESENTATION_PROFILES))
def test_shipped_presentation_skills_activate_on_a_presentation_change(name, tmp_path, monkeypatch):
    """설치만 되고 활성화가 안 되면 상태 기반 UI 계약이 프롬프트에 영원히 안 들어온다.

    android만 frontmatter를 선언한 상태라 나머지 세 스택은 설치돼도 켜지지 않았다."""
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    project = tmp_path / name
    _bundled_shipped_skill(project, name)
    profile = load_profile_payload(_PRESENTATION_PROFILES[name])

    required = _required_for(project, [_PRESENTATION_CHANGES[name]], profile=profile)

    assert name in required


@pytest.mark.parametrize("name", sorted(_PRESENTATION_PROFILES))
def test_shipped_presentation_skills_stay_off_for_a_data_layer_change(name, tmp_path, monkeypatch):
    """반증: 표의 glob이 스택 파일 전체면 데이터 계층 변경에도 presentation skill이 붙는다.

    실측으로 ios 표가 `**/*.swift`와 함께 presentation skill 이름을 갖고 있었고, profile을
    안 넘기던 이 가드는 그 역전을 통과시켰다."""
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    project = tmp_path / name
    _bundled_shipped_skill(project, name)
    profile = load_profile_payload(_PRESENTATION_PROFILES[name])

    required = _required_for(project, [_DATA_LAYER_CHANGES[name]], profile=profile)

    assert name not in required


def _profile_ids() -> list[str]:
    return sorted(
        path.stem for path in PROFILES_DIR.glob("*.yaml") if not path.stem.startswith("_")
    )


def test_declared_phase_skills_degrades_on_an_unreadable_workflow(tmp_path):
    """반증: 깨진 workflow 하나에 예외가 새면 `skills doctor`가 traceback으로 죽는다.

    수집을 조용히 비우는 것도 안 된다 — 그러면 그 workflow만 선언한 skill이 미라우팅
    오탐으로 찍힌다. 읽은 것은 남기고 못 읽은 사유를 함께 돌려줘야 한다."""
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    shutil.copy(REPO / "src" / "agent_flow" / "workflows" / "default.yaml", workflows / "default.yaml")
    (workflows / "broken.yaml").write_text("phases: [\n", encoding="utf-8")

    declared = declared_phase_skills(tmp_path)

    assert "code-generation-discipline" in declared.names
    assert [error for error in declared.errors if error.startswith("workflow broken:")]


def test_every_code_phase_declares_the_invariant_core():
    """반증: 작은 workflow는 `skills:`를 아예 안 적어서 코드 생성 규율이 required가
    아니었다. 그 상태에서도 profile 표는 걸리므로 architecture 문서는 들어오고,
    정작 모든 코드 작업의 기준인 `code-generation-discipline`만 빠진다.

    phase 목록은 `CODE_PHASES`에서 가져온다. 여기에 리터럴 이름을 적으면 새 code
    phase가 생겼을 때 이 가드가 그것을 못 본다.
    """
    from agent_flow.core.phase_workflow import (
        load_phase_workflow_definition,
        workflow_names,
    )

    missing: dict[str, list[str]] = {}
    workflow_root = REPO / "src" / "agent_flow"
    for workflow in workflow_names(workflow_root):
        for phase in load_phase_workflow_definition(workflow_root, workflow).phases:
            if phase.id not in CODE_PHASES:
                continue
            declared = phase.skills.required if phase.skills else ()
            if "code-generation-discipline" not in declared:
                missing.setdefault(workflow, []).append(phase.id)

    assert missing == {}


def test_code_generation_discipline_covers_requested_principles():
    discipline = (
        REPO / "skills" / "code-generation-discipline" / "SKILL.md"
    ).read_text(encoding="utf-8").casefold()

    assert {
        "single responsibility",
        "side effects",
        "do not repeat yourself",
        "parameter grouping",
        "fail fast",
        "guard clauses",
        "single level of abstraction",
        "explicit receiver",
    } <= {
        line.strip().removeprefix("- ").split(" — ", 1)[0]
        for line in discipline.splitlines()
    }


def test_every_profile_install_name_is_activation_reachable(tmp_path, monkeypatch):
    """설치와 활성화는 다른 층이다. 실측으로 install 25개 중 4개만 활성화 가능했고,
    나머지 21개는 어느 phase에도 붙지 않았다. 이 테스트가 그 드리프트의 재발 가드다.

    판정은 doctor와 같은 함수로 한다 — 여기서 도달 가능성을 다시 정의하면 두 기준이
    갈라져 통과하면서 죽어 있는 상태가 다시 만들어진다."""
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    declared = declared_phase_skills(REPO).names

    unreachable: dict[str, list[str]] = {}
    for profile_id in _profile_ids():
        payload = load_profile_payload(profile_id)
        install = (payload.get("skills") or {}).get("install") or []
        if not install:
            continue
        project = tmp_path / profile_id
        for name in install:
            _bundled_shipped_skill(project, name)

        result = skill_catalog.scan(
            project, profile=payload, host="claude", workflow_skills=declared
        )
        unrouted = {
            finding.name for finding in result.findings if finding.kind == skill_catalog.UNROUTED
        }
        blocked = sorted(set(install) & unrouted)
        if blocked:
            unreachable[profile_id] = blocked

    assert unreachable == {}


def test_selector_terms_match_on_word_boundaries():
    """부분문자열로 보면 무관한 task가 skill을 required로 올린다."""
    from agent_flow.core.skill_resolver import selector_matches

    def matched(term: str, text: str) -> bool:
        return selector_matches(
            task_terms=[term], path_globs=[], changed_files=[], task_text=text
        )

    # 옛 substring 규칙에서 실제로 True였던 것들이다.
    assert not matched("uistate", "guistatemachine을 고친다")
    assert not matched("chart", "charting 라이브러리를 붙인다")
    assert not matched("state", "restated 요구사항 정리")
    # 복수형은 허용한다. skill 설명이 term을 복수로 쓰는 쪽이 흔하다.
    assert matched("modifier", "compose modifiers 정리")
    # 한글 조사가 붙어도 경계다. `\\w`로 잡으면 한국어 task가 전부 미매치가 된다.
    assert matched("상태", "화면 상태를 정리한다")
    # 한글은 ASCII 경계 밖이라 경계가 없는 것과 같다. 그 오탐은 구절로 좁혀서 막는다.
    assert matched("프레젠테이션", "팀 프레젠테이션으로 정리한다")



SHIPPED_SKILLS = REPO / "skills"

MIGRATED_DEPENDENCIES = {
    "clean-architecture": ["clean-architecture-core"],
    "android-clean-architecture": ["clean-architecture-core"],
    "ios-clean-architecture": ["clean-architecture-core"],
    "react-clean-architecture": ["clean-architecture-core"],
    "react-native-clean-architecture": ["clean-architecture-core"],
    "python-api-clean-architecture": ["clean-architecture-core"],
    "grill-with-docs": ["domain-modeling", "grilling"],
    "tdd": ["codebase-design", "code-review"],
    "app-shell-error-contract": ["clean-architecture-core"],
    "android-appshell-error-handling": ["app-shell-error-contract"],
    "ios-app-shell-error-handling": ["app-shell-error-contract"],
    "react-app-shell-error-handling": ["app-shell-error-contract"],
    "react-native-app-shell-error-handling": ["app-shell-error-contract"],
}


def _shipped_catalog():
    root = SkillRoot(source="project", template=str(SHIPPED_SKILLS / "{skill}" / "SKILL.md"))
    return discover_skill_catalog(REPO, (root,))


def test_flutter_development_guide_defines_contextual_responsive_layout_contract():
    guide = (
        SHIPPED_SKILLS / "flutter-development-guide" / "SKILL.md"
    ).read_text(encoding="utf-8")
    required_decisions = (
        "linear single-run",
        "bounded main axis",
        "direct child",
        "`Wrap` when items may reflow",
        "lazy `ListView`, `GridView`, or slivers",
        "`Flex.spacing`",
        "`TextScaler`",
        "`MediaQuery.sizeOf`",
        "`LayoutBuilder`",
        "`Padding`",
        "`SafeArea`",
    )

    missing = [decision for decision in required_decisions if decision not in guide]
    assert missing == []
    assert "hardware type or top-level orientation" in guide
    assert "blanket ban" in guide


def test_alias_expands_to_clean_architecture_core():
    """반증: JS 표에만 있으면 Python 런타임은 alias 한 벌만 읽고 정본을 놓친다."""
    from agent_flow.core.skill_resolver import expand_dependencies

    expanded = expand_dependencies(["clean-architecture"], _shipped_catalog())
    assert "clean-architecture-core" in expanded


def test_shipped_skill_dependencies_are_declared_in_frontmatter():
    """반증: 매핑 8개 중 하나라도 frontmatter 밖에 남으면 두 언어가 갈린다."""
    from agent_flow.core.skill_resolver import expand_dependencies

    catalog = _shipped_catalog()
    missing = {
        name: [dep for dep in deps if dep not in expand_dependencies([name], catalog)]
        for name, deps in MIGRATED_DEPENDENCIES.items()
    }
    assert {name: gap for name, gap in missing.items() if gap} == {}

_APP_SHELL_CASES = {
    "android-appshell-error-handling": ("android", "snackbar host"),
    "ios-app-shell-error-handling": ("ios", "alert host"),
    "react-app-shell-error-handling": ("nextjs", "error boundary"),
    "react-native-app-shell-error-handling": ("react-native", "navigation reset"),
}
_APP_SHELL_PATHS = {
    "android-appshell-error-handling": "app/src/main/kotlin/AppShell.kt",
    "ios-app-shell-error-handling": "ios/AppShell.swift",
    "react-app-shell-error-handling": "src/AppShell.tsx",
    "react-native-app-shell-error-handling": "src/App.tsx",
}



_APP_SHELL_PRESENTATION_PAIRS = {
    "android-appshell-error-handling": "android-clean-presentation-architecture",
    "ios-app-shell-error-handling": "ios-clean-presentation-architecture",
    "react-app-shell-error-handling": "react-clean-presentation-architecture",
    "react-native-app-shell-error-handling": "react-native-clean-presentation-architecture",
}


def test_app_shell_contract_selector_does_not_outgrow_platform_family():
    catalog = {entry.name: entry for entry in _shipped_catalog()}
    contract_terms = set(catalog["app-shell-error-contract"].task_terms)
    assert contract_terms
    platform_terms = {
        term
        for name in _APP_SHELL_CASES
        for term in catalog[name].task_terms
    }

    assert contract_terms <= platform_terms


def test_app_shell_platform_skills_define_path_selectors():
    catalog = {entry.name: entry for entry in _shipped_catalog()}

    for name in _APP_SHELL_CASES:
        assert catalog[name].path_globs


def test_clean_presentation_skills_depend_on_the_core_contract():
    catalog = {entry.name: entry for entry in _shipped_catalog()}

    for name in _APP_SHELL_PRESENTATION_PAIRS.values():
        assert "clean-architecture-core" in catalog[name].dependencies


def test_app_shell_and_presentation_skills_define_exclusive_ownership():
    for app_shell_name, presentation_name in _APP_SHELL_PRESENTATION_PAIRS.items():
        app_shell = (SHIPPED_SKILLS / app_shell_name / "SKILL.md").read_text(
            encoding="utf-8"
        )
        presentation = (
            SHIPPED_SKILLS / presentation_name / "SKILL.md"
        ).read_text(encoding="utf-8")

        assert f"use `{presentation_name}` instead" in app_shell
        assert f"use `{app_shell_name}` instead" in presentation




def test_app_shell_shared_semantics_are_not_repeated_by_platform_skills():
    contract = (SHIPPED_SKILLS / "app-shell-error-contract" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    shared_semantics = (
        "`pending`",
        "`handling`",
        "`consumed`",
        "retryable state",
    )
    metadata_fields = "`code`, `title`, `message`, and `requestId`"

    for semantic in shared_semantics:
        assert semantic in contract
    assert metadata_fields in contract
    for name in _APP_SHELL_CASES:
        platform = (SHIPPED_SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        assert all(semantic not in platform for semantic in shared_semantics)
        assert metadata_fields not in platform
        assert "source of truth for classification" in platform
    assert "Shared Presentation Contract" in contract
    assert "not a Core Domain" in contract
    core = (SHIPPED_SKILLS / "clean-architecture-core" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "Shared Presentation Contract" in core
    assert "shared-presentation-contract-placement: pass|fail|n/a" in core




def test_react_app_shell_review_rejects_unguarded_protected_history():
    text = (
        SHIPPED_SKILLS / "react-app-shell-error-handling" / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "Back navigation reaches a protected route without the auth guard" in text
    assert "Back navigation can reach only routes that the auth guard rejects" not in text


def test_app_shell_skills_defer_completion_markers_to_the_active_phase():
    for name in ("app-shell-error-contract", *_APP_SHELL_CASES):
        text = (SHIPPED_SKILLS / name / "SKILL.md").read_text(encoding="utf-8")

        assert "Use only the markers supplied by the active phase." in text
        assert "project-local-skills-used:" not in text
        assert "do not record it under `project-local-skills-used`" not in text


@pytest.mark.parametrize("name", sorted(_APP_SHELL_CASES))
def test_app_shell_skill_activates_its_shared_contract(name, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    project = tmp_path / name
    _bundled_shipped_skill(project, name)
    _bundled_shipped_skill(project, "app-shell-error-contract")
    profile_name, task_text = _APP_SHELL_CASES[name]

    required = _required_for(
        project,
        [],
        profile=load_profile_payload(profile_name),
        task_text=task_text,
    )

    assert {name, "app-shell-error-contract"} <= required
@pytest.mark.parametrize("name", sorted(_APP_SHELL_PATHS))
def test_app_shell_path_selector_activates_its_shared_contract(
    name, tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    project = tmp_path / name
    _bundled_shipped_skill(project, name)
    _bundled_shipped_skill(project, "app-shell-error-contract")
    profile_name, _ = _APP_SHELL_CASES[name]

    required = _required_for(
        project,
        [_APP_SHELL_PATHS[name]],
        profile=load_profile_payload(profile_name),
    )

    assert {name, "app-shell-error-contract"} <= required




def test_skill_selection_has_no_dependency_table():
    """반증: 표가 남아 있으면 정본이 둘이라 다음 skill 추가에서 다시 갈린다."""
    sources = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted((REPO / "lib").glob("*.mjs")) + sorted((REPO / "bin").glob("*.mjs"))
    }
    holders = [name for name, text in sources.items() if "SKILL_DEPENDENCIES" in text]
    assert holders == [], f"SKILL_DEPENDENCIES가 아직 남아 있다: {holders}"



def test_common_profile_skills_are_source_backed():
    source = (REPO / "lib" / "skill-selection.mjs").read_text(encoding="utf-8")
    common_profile_skills = source.split(
        "export const COMMON_PROFILE_SKILLS = new Set([", 1
    )[1].split("]);", 1)[0]
    names = re.findall(r'"([a-z0-9-]+)"', common_profile_skills)

    missing = [
        name for name in names if not (SHIPPED_SKILLS / name / "SKILL.md").is_file()
    ]

    assert missing == []


def test_workflow_skills_are_not_generated_in_javascript():
    source = "\n".join(
        (
            (REPO / "lib" / "installer-shared.mjs").read_text(encoding="utf-8"),
            (REPO / "bin" / "agent-flow-kit.mjs").read_text(encoding="utf-8"),
        )
    )
    generated_helpers = (
        "agentFlowSkillMarkdown",
        "architectureReviewerSkillMarkdown",
        "fullFeatureSkillMarkdown",
        "planReviewerSkillMarkdown",
        "productBriefSkillMarkdown",
        "pushWatchSkillMarkdown",
    )

    assert all(name not in source for name in generated_helpers)


def test_push_watch_skill_resumes_the_active_run():
    skill = (SHIPPED_SKILLS / "push-watch" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "agent-flow run push-watch" not in skill
    assert "agent-flow status" in skill
    assert "next_command" in skill


def test_workflow_required_skills_are_source_backed():
    missing: list[str] = []
    workflows = REPO / "src" / "agent_flow" / "workflows"
    for workflow_path in sorted(workflows.glob("*.yaml")):
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        for phase in workflow.get("phases", []):
            for name in phase.get("skills", {}).get("required", []):
                if not (SHIPPED_SKILLS / name / "SKILL.md").is_file():
                    missing.append(f"{workflow_path.name}:{phase['id']}:{name}")

    assert missing == []


def test_bundled_skills_use_requires_for_dependencies():
    offenders = []
    for skill_path in sorted(SHIPPED_SKILLS.glob("*/SKILL.md")):
        frontmatter = yaml.safe_load(
            skill_path.read_text(encoding="utf-8").split("---", 2)[1]
        ) or {}
        if "dependencies" in frontmatter:
            offenders.append(skill_path.parent.name)

    assert offenders == []


def test_common_skill_selection_does_not_duplicate_transitive_dependencies():
    source = (REPO / "lib" / "skill-selection.mjs").read_text(encoding="utf-8")
    common_profile_skills = source.split(
        "export const COMMON_PROFILE_SKILLS = new Set([", 1
    )[1].split("]);", 1)[0]
    transitive_dependencies = {
        "clean-architecture-core",
        "code-review",
        "codebase-design",
        "domain-modeling",
        "grilling",
        "write-for-work",
    }

    assert all(
        f'"{name}"' not in common_profile_skills
        for name in transitive_dependencies
    )



def test_profile_declared_skills_ignore_the_external_budget(tmp_path: Path) -> None:
    """반증: 예산이 정본 규범까지 떨구면 read gate에서 핵심 skill이 빠진다."""
    from agent_flow.core.skill_resolver import expand_dependencies

    catalog = _shipped_catalog()
    expanded = expand_dependencies(["clean-architecture", "tdd"], catalog)
    assert "clean-architecture-core" in expanded
    assert "codebase-design" in expanded



MANAGED_ADAPTER_BODY = "// agent-flow: managed omp extension\nexport default function agentFlowHooks(pi) {}\n"


def _global_adapter(home: Path, body: str = MANAGED_ADAPTER_BODY) -> Path:
    path = home / ".omp" / "agent" / "extensions" / "agent-flow-hooks.ts"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_doctor_reports_unowned_global_omp_adapter(tmp_path: Path) -> None:
    """반증: 못 보면 오늘처럼 kit이 만들지도 관리하지도 않는 adapter가 모든 tool을 막고도
    재설치로 낫지 않는다."""
    project = tmp_path / "project"
    (project / ".agent-flow" / "skills").mkdir(parents=True)
    home = tmp_path / "home"
    adapter = _global_adapter(home)

    result = skill_catalog.scan(project, home=home)

    findings = [item for item in result.findings if item.kind == skill_catalog.UNOWNED_ADAPTER]
    assert len(findings) == 1, [item.kind for item in result.findings]
    assert str(adapter) in findings[0].name + findings[0].detail


def test_doctor_ignores_a_home_without_a_global_adapter(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".agent-flow" / "skills").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()

    result = skill_catalog.scan(project, home=home)

    assert [item for item in result.findings if item.kind == skill_catalog.UNOWNED_ADAPTER] == []


def test_doctor_ignores_a_foreign_file_without_the_kit_marker(tmp_path: Path) -> None:
    """반증: marker 없이 보고하면 남의 확장을 kit 산출물로 지목한다."""
    project = tmp_path / "project"
    (project / ".agent-flow" / "skills").mkdir(parents=True)
    home = tmp_path / "home"
    _global_adapter(home, "export default function somebodyElse() {}\n")

    result = skill_catalog.scan(project, home=home)

    assert [item for item in result.findings if item.kind == skill_catalog.UNOWNED_ADAPTER] == []


def test_doctor_adapter_scan_does_not_touch_the_file(tmp_path: Path) -> None:
    """반증: 진단이 파일을 고치면 소유를 증명하지 못한 것을 훼손한다."""
    project = tmp_path / "project"
    (project / ".agent-flow" / "skills").mkdir(parents=True)
    home = tmp_path / "home"
    adapter = _global_adapter(home)
    before = (adapter.read_bytes(), adapter.stat().st_mtime_ns)

    skill_catalog.scan(project, home=home)

    assert (adapter.read_bytes(), adapter.stat().st_mtime_ns) == before


def test_doctor_ignores_a_symlinked_global_adapter(tmp_path: Path) -> None:
    """반증: 링크를 따라가면 이 자리에 없는 남의 파일로 소유권을 판정한다."""
    project = tmp_path / "project"
    (project / ".agent-flow" / "skills").mkdir(parents=True)
    home = tmp_path / "home"
    elsewhere = tmp_path / "elsewhere.ts"
    elsewhere.write_text(MANAGED_ADAPTER_BODY, encoding="utf-8")
    link = home / ".omp" / "agent" / "extensions" / "agent-flow-hooks.ts"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(elsewhere)

    result = skill_catalog.scan(project, home=home)

    assert [item for item in result.findings if item.kind == skill_catalog.UNOWNED_ADAPTER] == []


def test_crlf_skill_enters_the_catalog_with_metadata(tmp_path: Path) -> None:
    """반증: Python이 CRLF frontmatter를 못 읽으면 그 skill은 선언 없는 엔트리로 떨어진다."""
    host = tmp_path / "host"
    _write_skill(
        host,
        "crlf-skill",
        "---\r\nname: crlf-skill\r\ndescription: Compose guidance. Second sentence.\r\n"
        "requires:\r\n  - other-skill\r\n---\r\n\r\n# crlf-skill\r\n",
    )
    catalog = discover_skill_catalog(tmp_path, (_host_root(host),))
    entry = next(item for item in catalog if item.name == "crlf-skill")
    assert entry.description == "Compose guidance. Second sentence."
    assert entry.dependencies == ("other-skill",)


def test_crlf_skill_summary_matches_the_shared_rule(tmp_path: Path) -> None:
    from agent_flow.core.skill_resolver import skill_summary

    host = tmp_path / "host"
    path = _write_skill(
        host,
        "crlf-summary",
        "---\r\nname: crlf-summary\r\ndescription: First sentence. Second sentence.\r\n---\r\n",
    )
    assert skill_summary(path) == "First sentence."
