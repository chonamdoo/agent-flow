"""프로젝트가 고른 아키텍처 기준(clean/local/pending)의 계약.

이 선택은 workflow 단계·순서·분기·완료 조건을 바꾸지 않는다. 바뀌는 것은
"어떤 구조 규범을 필수로 적용하는가" 하나뿐이다.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT_ROOT / "src"))

from agent_flow.core.architecture_policy import (  # noqa: E402
    PROJECT_ARCHITECTURE_FILE,
    ArchitectureContractError,
    ArchitectureMode,
    ArchitectureSelection,
    architecture_snapshot,
    load_architecture_selection,
    parse_architecture_document,
    parse_architecture_selection,
)
from agent_flow.core.local_skills import architecture_contract_required  # noqa: E402


def _payload(**architecture: object) -> dict[str, object]:
    """Build a serialized architecture selection payload."""
    return {"schema_version": 1, "architecture": architecture}


def _parse(**architecture: object) -> ArchitectureSelection:
    """Parse an architecture selection payload."""
    return parse_architecture_selection(_payload(**architecture), source="probe.yaml")


def test_mode_accepts_only_the_three_declared_values():
    """열린 문자열이면 오타가 조용히 새 정책이 된다. 셋 중 하나만 받는다."""
    assert _parse(mode="clean").mode is ArchitectureMode.CLEAN
    assert _parse(mode="pending").mode is ArchitectureMode.PENDING
    assert _parse(mode="local", skill="skills/architecture/SKILL.md").mode is ArchitectureMode.LOCAL

    for rejected in ("Clean", "none", "ddd", "", "  ", None, 1, ["clean"]):
        with pytest.raises(ValueError, match="architecture mode"):
            _parse(mode=rejected)


def test_local_requires_a_contract_path():
    """계약 없는 local은 "규범을 지키라"고만 하고 규범이 없는 상태다."""
    with pytest.raises(ValueError, match="mode local requires"):
        _parse(mode="local")


def test_clean_and_pending_reject_a_contract_path():
    """계약이 걸린 pending은 미정인지 아닌지 판정할 수 없다."""
    for mode in ("clean", "pending"):
        with pytest.raises(ValueError, match="only .*local"):
            _parse(mode=mode, skill="skills/architecture/SKILL.md")




def test_unknown_key_or_schema_version_is_a_declaration_error():
    """모르는 키를 흘리면 사용자는 선언이 반영됐다고 믿는다."""
    with pytest.raises(ValueError, match="unsupported"):
        parse_architecture_selection(
            {"schema_version": 1, "architecture": {"mode": "clean", "enforce": True}},
            source="probe.yaml",
        )
    with pytest.raises(ValueError, match="unsupported"):
        parse_architecture_selection(
            {"schema_version": 1, "architecture": {"mode": "clean"}, "extra": 1},
            source="probe.yaml",
        )
    for version in (0, 2, "1", None, True):
        with pytest.raises(ValueError, match="schema_version"):
            parse_architecture_selection(
                {"schema_version": version, "architecture": {"mode": "clean"}},
                source="probe.yaml",
            )


def test_a_future_schema_version_never_falls_back_to_clean():
    """구 버전이 새 선언을 못 읽으면 비호환을 알려야 한다. 조용한 Clean 복귀는 정책 변경이다."""
    with pytest.raises(ValueError) as excinfo:
        parse_architecture_selection(
            {"schema_version": 2, "architecture": {"mode": "local"}},
            source="probe.yaml",
        )

    assert "clean" not in str(excinfo.value).lower()


def test_duplicate_yaml_key_is_rejected_instead_of_last_wins():
    """YAML의 last-wins는 리뷰에서 안 보인다. 두 선언 중 뭐가 유효한지 알 수 없다."""
    text = "schema_version: 1\narchitecture:\n  mode: pending\narchitecture:\n  mode: clean\n"

    with pytest.raises(ValueError, match="duplicate"):
        parse_architecture_document(text, source="probe.yaml")


def test_duplicate_key_inside_the_architecture_block_is_rejected():
    """Verify that duplicate key inside the architecture block is rejected."""
    text = "schema_version: 1\narchitecture:\n  mode: clean\n  mode: pending\n"

    with pytest.raises(ValueError, match="duplicate"):
        parse_architecture_document(text, source="probe.yaml")


def test_document_parses_the_same_selection_as_the_mapping():
    """Verify that document parses the same selection as the mapping."""
    text = "schema_version: 1\narchitecture:\n  mode: local\n  skill: skills/architecture/SKILL.md\n"

    assert parse_architecture_document(text, source="probe.yaml") == _parse(
        mode="local", skill="skills/architecture/SKILL.md"
    )


@pytest.mark.parametrize(
    "contract_path",
    [
        "/etc/architecture/SKILL.md",
        "../outside/SKILL.md",
        "skills/../../escape/SKILL.md",
        "https://example.com/SKILL.md",
        "skills/architecture",
        "skills/architecture/README.md",
        "SKILL.md",
        "skills//SKILL.md",
        "",
    ],
)
def test_contract_path_escaping_the_repository_is_rejected(contract_path):
    """계약은 저장소가 추적하는 규범 파일이어야 팀과 리뷰어가 같은 것을 본다."""
    with pytest.raises(ValueError, match="contract"):
        _parse(mode="local", skill=contract_path)


def test_private_contract_path_is_accepted_and_names_the_architecture_skill():
    """개인 drop-box 계약도 skill 이름은 디렉터리(`architecture`)에서 온다.

    반증: `split('/')[1]`이면 `local-skills`가 계약 이름이 되어 catalog 제외와
    required 판정이 엉뚱한 이름을 본다.
    """
    from agent_flow.core.architecture_policy import contract_skill_name

    selection = _parse(mode="local", skill=".agent-flow/local-skills/architecture/SKILL.md")

    assert contract_skill_name(selection) == "architecture"
    assert contract_skill_name(_parse(mode="local", skill="skills/architecture/SKILL.md")) == "architecture"


@pytest.mark.parametrize(
    "contract_path",
    [".agent-flow/local-skills/SKILL.md", ".agent-flow/local-skills/other/SKILL.md", ".agent-flow/skills/architecture/SKILL.md"],
)
def test_only_the_architecture_drop_box_slot_is_a_private_contract(contract_path):
    """drop-box 안이라도 `architecture/` 한 자리만 계약이다. 아무 스킬이나 계약이 되면 안 된다."""
    with pytest.raises(ValueError, match="contract"):
        _parse(mode="local", skill=contract_path)


def _private_contract(root: Path, *, references: tuple[str, ...] = ()) -> None:
    """Provision a gitignored private architecture contract fixture."""
    declared = "".join(f"  - {name}\n" for name in references)
    requires = f"requires_docs:\n{declared}" if references else ""
    _write(root, ".gitignore", ".agent-flow/\n")
    _write(
        root,
        ".agent-flow/local-skills/architecture/SKILL.md",
        f"---\nname: architecture\ndescription: 이 머신에서만 쓰는 구조 규범\n{requires}---\n\n"
        "# Private architecture\n\nKeep transport in shared/api/.\n",
    )
    for name in references:
        _write(root, f".agent-flow/local-skills/architecture/{name}", "# Private reference\n\nNo barrels.\n")
    _write(
        root, PROJECT_ARCHITECTURE_FILE,
        "schema_version: 1\narchitecture:\n  mode: local\n  skill: .agent-flow/local-skills/architecture/SKILL.md\n",
    )


def test_private_contract_is_pinned_without_a_tracking_requirement(tmp_path):
    """gitignore된 자리에 있는 것이 개인 계약의 정의다. 추적을 요구하면 선택이 불가능하다.

    선언 파일도 함께 면제한다 — 추적을 요구하면 machine-local 경로를 동료에게 commit
    하라는 뜻이 된다. 내용은 그래도 digest로 고정돼 drift는 잡힌다.
    """
    root = _git_project(tmp_path)
    _private_contract(root, references=("references/a.md",))

    snapshot = architecture_snapshot(root)

    assert snapshot.untracked == ()
    assert snapshot.contract is not None
    assert [document.path for document in snapshot.contract.documents] == [
        ".agent-flow/local-skills/architecture/SKILL.md",
        ".agent-flow/local-skills/architecture/references/a.md",
    ]
    before = snapshot.digest
    _write(root, ".agent-flow/local-skills/architecture/references/a.md", "# changed\n\nBarrels allowed.\n")
    assert architecture_snapshot(root).digest != before


def test_private_contract_in_a_linked_worktree_reads_the_leader_copy(tmp_path):
    """`.agent-flow/`는 leader에만 있다. worktree가 자기 자리만 보면 개인 계약은 매 run 실패다."""
    from agent_flow.core.architecture_policy import contract_documents_root

    leader = _git_project(tmp_path, "leader")
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=leader, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=leader, check=True)
    _private_contract(leader)
    _track(leader, ".gitignore")
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=leader, check=True)
    checkout = tmp_path / "worktree"
    subprocess.run(["git", "worktree", "add", "-q", str(checkout), "HEAD"], cwd=leader, check=True)
    # worktree 생성이 leader의 선언 파일만 복사한다(ROOT_CONTEXT_FILES). local-skills는 오지 않는다.
    _write(checkout, PROJECT_ARCHITECTURE_FILE, (leader / PROJECT_ARCHITECTURE_FILE).read_text(encoding="utf-8"))
    assert not (checkout / ".agent-flow/local-skills").exists()

    snapshot = architecture_snapshot(checkout)

    assert contract_documents_root(checkout, snapshot.selection) == leader
    assert snapshot.contract is not None
    assert snapshot.contract.root.path == ".agent-flow/local-skills/architecture/SKILL.md"
    assert snapshot.digest == architecture_snapshot(leader).digest
    assert contract_documents_root(leader, snapshot.selection) == leader


@pytest.mark.parametrize("skill", ["skills/architecture/SKILL.md", ".agent-flow/local-skills/architecture/SKILL.md"])
@pytest.mark.parametrize("declared", [True, False], ids=["declared-clean", "legacy-default"])
def test_clean_never_runs_beside_a_project_contract(tmp_path, skill, declared):
    """계약 파일이 있는데 Clean이 켜지면 규범이 둘이다. 선언이 없는 legacy 기본값도 막는다."""
    from agent_flow.core.architecture_policy import prepare_architecture_selection

    root = _git_project(tmp_path)
    _write(root, skill, "---\nname: architecture\n---\n\nDomain never imports adapters.\n")
    if declared:
        _declare(root, "schema_version: 1\narchitecture:\n  mode: clean\n")

    with pytest.raises(ArchitectureContractError, match=f"exists at {skill}"):
        architecture_snapshot(root)
    with pytest.raises(ArchitectureContractError, match="cannot be selected"):
        prepare_architecture_selection(root, ArchitectureSelection(mode=ArchitectureMode.CLEAN))
    # 같은 저장소에서 계약을 고르면 통과한다.
    _declare(root, f"schema_version: 1\narchitecture:\n  mode: local\n  skill: {skill}\n")
    assert architecture_snapshot(root).contract is not None


def test_legacy_root_selection_is_read_until_migrated(tmp_path):
    """0.3.x가 루트에 둔 `.agent-flow.project.yaml`을 무시하면 pending이 조용히 clean이 된다."""
    from agent_flow.core.architecture_policy import LEGACY_PROJECT_ARCHITECTURE_FILE

    root = _git_project(tmp_path)
    _write(root, LEGACY_PROJECT_ARCHITECTURE_FILE, "schema_version: 1\narchitecture:\n  mode: pending\n")

    snapshot = architecture_snapshot(root)
    assert snapshot.selection.mode is ArchitectureMode.PENDING
    assert snapshot.declared is True
    assert snapshot.source_document is not None
    assert snapshot.source_document.path == LEGACY_PROJECT_ARCHITECTURE_FILE

    # 새 자리가 생기면 그쪽이 이긴다.
    _write(root, PROJECT_ARCHITECTURE_FILE, "schema_version: 1\narchitecture:\n  mode: clean\n")
    assert architecture_snapshot(root).selection.mode is ArchitectureMode.CLEAN


@pytest.mark.parametrize("parent_kind", ["symlink", "file", "missing"])
def test_unusable_skills_parent_is_not_a_project_contract(tmp_path, parent_kind):
    """`skills/`가 symlink·파일이면 계약 보유로 읽지 않는다. 그러면 clean도 local도 못 골라 출구가 없다."""
    from agent_flow.core.architecture_policy import find_project_contract

    root = _git_project(tmp_path)
    if parent_kind == "symlink":
        _write(tmp_path, "elsewhere/architecture/SKILL.md", "---\nname: architecture\n---\n\nOutside.\n")
        (root / "skills").symlink_to(tmp_path / "elsewhere")
    elif parent_kind == "file":
        _write(root, "skills", "not a directory\n")

    assert find_project_contract(root) is None
    assert architecture_snapshot(root).selection.mode is ArchitectureMode.CLEAN


@pytest.mark.parametrize("leaf_kind", ["directory", "symlink"])
def test_unreadable_contract_leaf_still_blocks_clean(tmp_path, leaf_kind):
    """잎 자리에 무언가 있으면 존재다 — 읽을 수 없는 계약 옆에서 Clean이 돌면 규범이 둘이다."""
    from agent_flow.core.architecture_policy import find_project_contract

    root = _git_project(tmp_path)
    leaf = root / "skills/architecture/SKILL.md"
    leaf.parent.mkdir(parents=True)
    if leaf_kind == "directory":
        leaf.mkdir()
    else:
        leaf.symlink_to(tmp_path / "nowhere.md")

    assert find_project_contract(root) == "skills/architecture/SKILL.md"
    with pytest.raises(ArchitectureContractError, match="exists at"):
        architecture_snapshot(root)



def _git_project(tmp_path: Path, name: str = "project") -> Path:
    """Create an initialized Git project fixture."""
    root = tmp_path / name
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def _track(root: Path, *relative: str) -> None:
    """Commit the current fixture files to Git."""
    subprocess.run(["git", "add", "--", *relative], cwd=root, check=True)


def _write(root: Path, relative: str, text: str) -> Path:
    """Write a file used by the current test fixture."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _declare(root: Path, body: str) -> None:
    """Write an architecture selection fixture."""
    _write(root, PROJECT_ARCHITECTURE_FILE, body)


def _clean_contract(root: Path) -> None:
    """Provision the Clean architecture contract fixture."""
    _write(
        root, "skills/clean-architecture-core/SKILL.md",
        "---\nname: clean-architecture-core\n---\n\nKeep domain policy independent of I/O.\n",
    )
    _declare(root, "schema_version: 1\narchitecture:\n  mode: clean\n")
    _track(root, "skills")


def _local_contract(root: Path, *, references: tuple[str, ...] = ()) -> None:
    """Provision a project-local architecture contract fixture."""
    declared = "".join(f"  - {name}\n" for name in references)
    requires = f"requires_docs:\n{declared}" if references else ""
    patterns = (
        "Route-local hooks/ owns form, query and API orchestration; _lib/ is pure.\n"
        if (root / AGENCY_ROOT / "hooks").exists()
        else "Route-local _lib/ may contain hooks and pure transformations.\n"
    )
    patterns += (
        "Keep feature components in _ui/ and transport in shared/api/.\n"
        "Do not introduce repository ports or use-case layers merely to satisfy Clean.\n"
        "A pure payload mapper must never perform network I/O.\n"
    )
    _write(
        root,
        "skills/architecture/SKILL.md",
        f"---\nname: architecture\ndescription: 승인된 프로젝트 구조 규범\n{requires}---\n\n"
        "# Feature-local architecture\n\n" + patterns,
    )
    for name in references:
        _write(root, f"skills/architecture/{name}", f"# Approved patterns\n\n{patterns}")
    _declare(
        root,
        "schema_version: 1\narchitecture:\n  mode: local\n  skill: skills/architecture/SKILL.md\n",
    )
    _track(root, "skills")


def test_absent_declaration_is_legacy_not_pending(tmp_path):
    """선언이 없는 기존 설치를 pending으로 읽으면 Clean 강제가 조용히 사라진다."""
    root = _git_project(tmp_path)

    snapshot = architecture_snapshot(root)

    assert snapshot.declared is False
    assert snapshot.selection.mode is ArchitectureMode.CLEAN
    assert load_architecture_selection(root) is None


def test_pending_snapshot_has_no_documents_and_is_not_an_error(tmp_path):
    """명시적 pending은 정상 상태다. 계약 문서가 없다는 사실 자체는 오류가 아니다."""
    root = _git_project(tmp_path)
    _declare(root, "schema_version: 1\narchitecture:\n  mode: pending\n")

    snapshot = architecture_snapshot(root)

    assert snapshot.declared is True
    assert snapshot.selection.mode is ArchitectureMode.PENDING
    assert snapshot.contract is None
    assert snapshot.digest


def test_contract_digest_covers_root_and_declared_references_in_order(tmp_path):
    """root만 고정하면 참조 문서를 바꿔 다른 규칙을 읽게 할 수 있다."""
    root = _git_project(tmp_path)
    _local_contract(root, references=("references/a.md", "references/b.md"))

    contract = architecture_snapshot(root).contract

    assert contract is not None
    assert [document.path for document in contract.documents] == [
        "skills/architecture/SKILL.md",
        "skills/architecture/references/a.md",
        "skills/architecture/references/b.md",
    ]
    assert all(document.sha256 and document.bytes for document in contract.documents)


def test_reference_content_change_changes_the_digest(tmp_path):
    """참조 내용만 바뀌어도 작성자와 리뷰어가 읽는 규범이 달라진다."""
    root = _git_project(tmp_path)
    _local_contract(root, references=("references/a.md",))
    before = architecture_snapshot(root).digest

    _write(root, "skills/architecture/references/a.md", "# a\n\n금지 규칙 하나를 추가한다.\n")

    assert architecture_snapshot(root).digest != before


def test_reordering_references_changes_the_digest(tmp_path):
    """순서가 다르면 읽는 순서가 다르다. 같은 집합이라고 같은 계약으로 보지 않는다."""
    root = _git_project(tmp_path)
    _local_contract(root, references=("references/a.md", "references/b.md"))
    before = architecture_snapshot(root).digest

    _local_contract(root, references=("references/b.md", "references/a.md"))

    assert architecture_snapshot(root).digest != before


def test_missing_contract_root_is_an_error_not_a_pending_fallback(tmp_path):
    """문서가 없는데 진행하면 '규범을 지켰다'가 검증 불가능한 주장이 된다."""
    root = _git_project(tmp_path)
    _declare(
        root,
        "schema_version: 1\narchitecture:\n  mode: local\n  skill: skills/architecture/SKILL.md\n",
    )

    with pytest.raises(ArchitectureContractError):
        architecture_snapshot(root)


def test_missing_required_reference_is_an_error(tmp_path):
    """root만 있으면 통과시키면 필수 참조는 선택 사항이 된다."""
    root = _git_project(tmp_path)
    _local_contract(root, references=("references/a.md",))
    (root / "skills/architecture/references/a.md").unlink()

    with pytest.raises(ArchitectureContractError, match="references/a.md"):
        architecture_snapshot(root)


def test_symlinked_contract_root_is_rejected(tmp_path):
    """링크 대상만 갈아치우면 같은 경로로 다른 규범을 읽힐 수 있다."""
    root = _git_project(tmp_path)
    _write(root, "elsewhere/SKILL.md", "---\nname: architecture\n---\n\n# 다른 규범\n")
    (root / "skills" / "architecture").mkdir(parents=True)
    (root / "skills/architecture/SKILL.md").symlink_to(root / "elsewhere/SKILL.md")
    _declare(
        root,
        "schema_version: 1\narchitecture:\n  mode: local\n  skill: skills/architecture/SKILL.md\n",
    )

    with pytest.raises(ArchitectureContractError):
        architecture_snapshot(root)


def test_symlinked_contract_directory_is_rejected(tmp_path):
    """leaf만 보면 `skills/<name>`이 디렉터리 링크일 때 그대로 통과한다."""
    root = _git_project(tmp_path)
    _write(root, "elsewhere/SKILL.md", "---\nname: architecture\n---\n\n# 다른 규범\n")
    (root / "skills").mkdir()
    (root / "skills" / "architecture").symlink_to(root / "elsewhere", target_is_directory=True)
    _declare(
        root,
        "schema_version: 1\narchitecture:\n  mode: local\n  skill: skills/architecture/SKILL.md\n",
    )

    with pytest.raises(ArchitectureContractError):
        architecture_snapshot(root)


def test_contract_with_a_bom_still_declares_its_references(tmp_path):
    """Windows 편집기가 남긴 BOM에 필수 참조가 통째로 사라지면 안 된다."""
    root = _git_project(tmp_path)
    _local_contract(root, references=("references/a.md",))
    body = (root / "skills/architecture/SKILL.md").read_text(encoding="utf-8")
    (root / "skills/architecture/SKILL.md").write_text("\ufeff" + body, encoding="utf-8")

    contract = architecture_snapshot(root).contract

    assert contract is not None
    assert [document.path for document in contract.documents] == [
        "skills/architecture/SKILL.md",
        "skills/architecture/references/a.md",
    ]


def test_non_utf8_contract_is_an_error_not_a_silent_reference_drop(tmp_path):
    """디코딩 실패를 빈 목록으로 접으면 참조가 사라지고 이후 변경도 drift로 안 잡힌다."""
    root = _git_project(tmp_path)
    _local_contract(root, references=("references/a.md",))
    (root / "skills/architecture/SKILL.md").write_bytes(b"\xff\xfe---\nrequires_docs:\n---\n")

    with pytest.raises(ArchitectureContractError, match="UTF-8"):
        architecture_snapshot(root)


def test_untracked_contract_file_is_reported(tmp_path):
    """추적되지 않은 규범은 동료·리뷰어·새 clone에 도달하지 않는다."""
    root = _git_project(tmp_path)
    _local_contract(root, references=("references/a.md",))
    _write(root, "skills/architecture/references/b.md", "# b\n")
    _write(
        root,
        "skills/architecture/SKILL.md",
        "---\nname: architecture\nrequires_docs:\n  - references/a.md\n  - references/b.md\n---\n\n# 구조 규범\n",
    )

    contract = architecture_snapshot(root).contract

    assert contract is not None
    assert contract.untracked == ("skills/architecture/references/b.md",)


def test_clean_mode_needs_no_project_contract_document(tmp_path):
    """Clean은 kit이 배포하는 규범을 쓴다. 프로젝트 문서를 요구하지 않는다."""
    root = _git_project(tmp_path)
    _declare(root, "schema_version: 1\narchitecture:\n  mode: clean\n")

    snapshot = architecture_snapshot(root)

    assert snapshot.selection.mode is ArchitectureMode.CLEAN
    assert snapshot.contract is None


def test_snapshot_digest_separates_the_three_modes(tmp_path):
    """선택이 다르면 증거도 달라야 구 계약 승인을 재사용할 수 없다."""
    clean_root = _git_project(tmp_path, "clean")
    _declare(clean_root, "schema_version: 1\narchitecture:\n  mode: clean\n")

    pending_root = _git_project(tmp_path, "pending")
    _declare(pending_root, "schema_version: 1\narchitecture:\n  mode: pending\n")

    local_root = _git_project(tmp_path, "local")
    _local_contract(local_root)

    digests = {
        architecture_snapshot(root).digest
        for root in (clean_root, pending_root, local_root)
    }

    assert len(digests) == 3


def _cli(*args: str) -> int:
    """Invoke the CLI with the supplied project arguments."""
    from agent_flow.cli import main

    return main(list(args))


def _export(root: Path, capsys) -> dict:
    """Export the current architecture selection through the CLI."""
    assert _cli("architecture", "export", "--root", str(root)) == 0
    return json.loads(capsys.readouterr().out)


def test_export_returns_the_normalized_plan_as_json(tmp_path, capsys):
    """설치기는 YAML을 다시 해석하지 않는다. 이 JSON 하나가 공개 계약이다."""
    root = _git_project(tmp_path)
    _local_contract(root, references=("references/a.md",))

    plan = _export(root, capsys)

    assert plan["mode"] == "local"
    assert plan["declared"] is True
    assert plan["contract"] == "skills/architecture/SKILL.md"
    assert plan["documents"] == [
        "skills/architecture/SKILL.md",
        "skills/architecture/references/a.md",
    ]
    assert plan["digest"] == architecture_snapshot(root).digest
    assert plan["schema_version"] == 1


def test_export_reports_legacy_absence_without_inventing_pending(tmp_path, capsys):
    """Verify that export reports legacy absence without inventing pending."""
    root = _git_project(tmp_path)

    plan = _export(root, capsys)

    assert plan["declared"] is False
    assert plan["mode"] == "clean"


def test_export_never_writes_the_selection_file(tmp_path, capsys):
    """읽기 전용 파생이다. export가 선택을 만들면 조회가 정책 변경이 된다."""
    root = _git_project(tmp_path)

    _export(root, capsys)

    assert not (root / PROJECT_ARCHITECTURE_FILE).exists()


def test_select_records_the_selection_and_export_reads_it_back(tmp_path, capsys):
    """Verify that select records the selection and export reads it back."""
    root = _git_project(tmp_path)
    _write(root, "skills/architecture/SKILL.md", "---\nname: architecture\n---\n\n# 구조 규범\n")

    assert _cli(
        "architecture", "select", "--root", str(root),
        "--mode", "local", "--skill", "skills/architecture/SKILL.md",
    ) == 0
    capsys.readouterr()

    assert _export(root, capsys)["mode"] == "local"


def test_select_rejects_an_unresolvable_contract_before_writing(tmp_path):
    """검증 전에 기록하면 다음 run이 없는 계약을 유효한 선택으로 읽는다."""
    root = _git_project(tmp_path)

    assert _cli(
        "architecture", "select", "--root", str(root),
        "--mode", "local", "--skill", "skills/architecture/SKILL.md",
    ) != 0
    assert not (root / PROJECT_ARCHITECTURE_FILE).exists()


def test_select_rejects_a_contract_path_for_clean_and_pending(tmp_path, capsys):
    """Verify that select rejects a contract path for clean and pending."""
    root = _git_project(tmp_path)

    assert _cli(
        "architecture", "select", "--root", str(root),
        "--mode", "pending", "--skill", "skills/architecture/SKILL.md",
    ) != 0
    assert not (root / PROJECT_ARCHITECTURE_FILE).exists()


def test_select_replaces_a_previous_choice_without_merging(tmp_path, capsys):
    """선택은 누적이 아니라 교체다. 두 모드가 동시에 유효할 수 없다."""
    root = _git_project(tmp_path)
    _local_contract(root)

    assert _cli("architecture", "select", "--root", str(root), "--mode", "pending") == 0
    capsys.readouterr()

    plan = _export(root, capsys)
    assert plan["mode"] == "pending"
    assert plan["contract"] is None


def test_export_reports_schema_version_incompatibility_instead_of_defaulting(tmp_path, capsys):
    """구 kit이 새 선언을 Clean으로 읽으면 그것이 조용한 정책 변경이다."""
    root = _git_project(tmp_path)
    _declare(root, "schema_version: 2\narchitecture:\n  mode: local\n")

    assert _cli("architecture", "export", "--root", str(root)) != 0
    captured = capsys.readouterr()
    assert "schema_version" in captured.err
    assert captured.out.strip() == ""


def test_run_architecture_flag_keeps_its_own_meaning(tmp_path, capsys):
    """기존 `run --architecture default|ddd|service-layer`는 프롬프트 서술용이다.

    새 선택이 같은 플래그에 얹히면 의미가 둘이 되어 어느 쪽도 신뢰할 수 없다.
    """
    with pytest.raises(SystemExit):
        _cli("run", "probe", "--root", str(tmp_path), "--architecture", "clean")

    assert "invalid choice" in capsys.readouterr().err


IMPLEMENT_DECLARED = ("code-generation-discipline", "tdd", "clean-architecture-core")


def _resolution(root: Path, monkeypatch, *, phase_id: str = "implement"):
    """설치된 카탈로그가 머신마다 다르면 결과가 달라진다. HOME을 비워 고정한다."""
    from agent_flow.core.skill_resolver import PhaseSkills, resolve_phase_skills

    monkeypatch.setenv("HOME", str(root / "empty-home"))
    return resolve_phase_skills(
        project_root=root,
        phase_id=phase_id,
        phase_skills=PhaseSkills(required=IMPLEMENT_DECLARED, replaceable_architecture=True),
    )


def _required_names(resolution) -> set[str]:
    """Return the required skill names from a resolution."""
    return {skill.name for skill in resolution.required}


def test_three_modes_resolve_distinct_required_contracts(tmp_path, monkeypatch):
    """SPEC-1. 선택이 실제로 다른 필수 규범을 만든다."""
    clean_root = _git_project(tmp_path, "clean")
    _clean_contract(clean_root)

    pending_root = _git_project(tmp_path, "pending")
    _declare(pending_root, "schema_version: 1\narchitecture:\n  mode: pending\n")

    local_root = _git_project(tmp_path, "local")
    _local_contract(local_root)

    clean = _required_names(_resolution(clean_root, monkeypatch))
    pending = _required_names(_resolution(pending_root, monkeypatch))
    local = _required_names(_resolution(local_root, monkeypatch))

    assert "clean-architecture-core" in clean
    assert "clean-architecture-core" not in pending
    assert "clean-architecture-core" not in local
    assert "architecture" in local
    # 공통 개발 규율은 세 모드에서 모두 남는다. 선택은 구조 규범만 바꾼다.
    assert {"code-generation-discipline", "tdd"} <= clean & pending & local


def test_pending_does_not_exempt_the_common_required_skills(tmp_path, monkeypatch):
    """SPEC-4의 절반. pending은 면제가 아니다."""
    root = _git_project(tmp_path)
    _declare(root, "schema_version: 1\narchitecture:\n  mode: pending\n")

    required = _required_names(_resolution(root, monkeypatch))

    assert {"code-generation-discipline", "tdd"} <= required


def test_legacy_absence_keeps_the_clean_obligation(tmp_path, monkeypatch):
    """선언 없는 기존 설치에서 Clean이 사라지면 그것이 조용한 정책 변경이다."""
    root = _git_project(tmp_path)

    assert "clean-architecture-core" in _required_names(_resolution(root, monkeypatch))


def _architecture_angle_runs(root: Path, monkeypatch) -> bool:
    """계약 의존 review angle이 이 프로젝트에서 등록되는가."""
    from types import SimpleNamespace

    from agent_flow.adapters import hosted
    from agent_flow.core.skill_resolver import PhaseSkills

    monkeypatch.setenv("HOME", str(root / "empty-home"))
    phase = SimpleNamespace(
        id="review",
        description="d",
        prompt="p",
        artifact=None,
        multi_review=True,
        required_markers=(),
        skills=PhaseSkills(required=IMPLEMENT_DECLARED, replaceable_architecture=True),
    )
    angles = [
        {"id": "architecture-design", "prompt": "", "requires": "clean-architecture"},
        {"id": "types", "prompt": ""},
    ]
    applicable = hosted._applicable_angles(
        angles, phase, root, hosted.HostedAdapter("omp"), providers=("claude",)
    )
    return "architecture-design" in {str(angle["id"]) for angle in applicable}


def test_contract_dependent_angle_follows_the_selection(tmp_path, monkeypatch):
    """작성자 게이트와 reviewer angle이 갈리면, 요구받은 marker를 검증할 angle이
    등록되지 않는 상태가 생긴다. 두 판정은 같은 선택을 본다."""
    from agent_flow.core.local_skills import architecture_contract_required

    for name, declare in (
        ("clean", _clean_contract),
        ("pending", lambda root: _declare(root, "schema_version: 1\narchitecture:\n  mode: pending\n")),
        ("local", _local_contract),
    ):
        root = _git_project(tmp_path, name)
        declare(root)

        author_gate = architecture_contract_required(_resolution(root, monkeypatch))

        assert author_gate is _architecture_angle_runs(root, monkeypatch), name


def test_local_contract_keeps_the_architecture_angle_registered(tmp_path, monkeypatch):
    """local이라고 구조 리뷰가 사라지면 안 된다. 기준만 바뀐다."""
    root = _git_project(tmp_path)
    _local_contract(root)

    assert _architecture_angle_runs(root, monkeypatch) is True


def test_pending_has_no_contract_to_review_against(tmp_path, monkeypatch):
    """계약이 없는데 계약 리뷰를 등록하면 리뷰어가 기준 없이 판정하게 된다."""
    root = _git_project(tmp_path)
    _declare(root, "schema_version: 1\narchitecture:\n  mode: pending\n")

    assert _architecture_angle_runs(root, monkeypatch) is False


def test_read_marker_alone_does_not_satisfy_contract_gate(tmp_path, monkeypatch):
    """Verify that read marker alone does not satisfy contract gate."""
    from agent_flow.core.review_evidence import review_route_evidence

    root = _git_project(tmp_path)
    _local_contract(root)
    skills = ", ".join(sorted(_required_names(_resolution(root, monkeypatch))))
    artifact = (
        "## Completion Gate\n"
        "skill-availability: pass\n"
        "skill-use-evidence: verified\n"
        "project-local-skills: checked\n"
        f"project-local-skills-used: {skills}\n"
        "project-local-skill-docs: applied\n"
        "architecture-contract-check: pass\n"
        "## Overall\n"
        "verdict: approve\n"
    )

    _, route = review_route_evidence(root, "final-review", artifact, run_meta={})

    assert route == "missing-reviewer"


def test_private_same_named_skill_cannot_shadow_the_selected_contract(tmp_path, monkeypatch):
    """일반 skill은 private 경로가 우선이지만, 명시 선택 계약은 그 경로로 고정한다."""
    root = _git_project(tmp_path)
    _local_contract(root)
    private = root / ".agent-flow" / "skills" / "architecture"
    private.mkdir(parents=True)
    (private / "SKILL.md").write_text(
        "---\nname: architecture\n---\n\n# 다른 회사의 규범\n", encoding="utf-8"
    )

    contract = architecture_snapshot(root).contract

    assert contract is not None
    assert contract.root.path == "skills/architecture/SKILL.md"
    assert contract.root.sha256 == hashlib.sha256(
        (root / "skills/architecture/SKILL.md").read_bytes()
    ).hexdigest()


def test_clean_role_lint_applies_only_in_clean_mode(tmp_path):
    """local 프로젝트에 Clean 토폴로지를 강요하면 선택이 의미를 잃는다."""
    from agent_flow.core.architecture_policy import clean_role_lint_applies

    clean_root = _git_project(tmp_path, "clean")
    _declare(clean_root, "schema_version: 1\narchitecture:\n  mode: clean\n")
    pending_root = _git_project(tmp_path, "pending")
    _declare(pending_root, "schema_version: 1\narchitecture:\n  mode: pending\n")
    local_root = _git_project(tmp_path, "local")
    _local_contract(local_root)
    legacy_root = _git_project(tmp_path, "legacy")

    assert clean_role_lint_applies(architecture_snapshot(clean_root)) is True
    assert clean_role_lint_applies(architecture_snapshot(legacy_root)) is True
    assert clean_role_lint_applies(architecture_snapshot(pending_root)) is False
    assert clean_role_lint_applies(architecture_snapshot(local_root)) is False


@pytest.mark.parametrize("workflow", ["default", "development", "bugfix", "review", "full-feature"])
def test_mode_never_changes_phase_graph_or_routes(tmp_path, workflow):
    """Verify that mode never changes phase graph or routes."""
    from agent_flow.runner import Runner

    graphs = []
    for mode in ("clean", "local", "pending"):
        root = _git_project(tmp_path, mode)
        if mode == "local":
            _local_contract(root)
        else:
            _declare(root, f"schema_version: 1\narchitecture:\n  mode: {mode}\n")
        runner = Runner(project_root=root, workflow=workflow)
        graphs.append([
            (
                phase.id, phase.routes, phase.optional, phase.pause_after,
                phase.multi_review, phase.artifact, phase.required_markers,
            )
            for phase in runner.phases
        ])
    assert graphs[0] == graphs[1] == graphs[2]


def _clean_violating_project(tmp_path: Path, name: str) -> Path:
    """python profile의 role 표에 걸리는 구조. 매핑 밖 경로 하나를 둔다."""
    root = _git_project(tmp_path, name)
    _write(root, "src/core/domain/orders/model.py", "class Order:\n    pass\n")
    _write(root, "src/core/unmapped/thing.py", "value = 1\n")
    _track(root, "src")
    return root


def _lint(root: Path) -> list:
    """Run architecture linting against the fixture project."""
    from agent_flow.core.architecture_lint import lint_project

    return lint_project(
        root, "python", files=["src/core/unmapped/thing.py"], profile_root=KIT_ROOT
    )


def test_clean_mode_still_reports_role_mapping_violations(tmp_path):
    """Clean을 고른 프로젝트에서 기존 검사가 약해지면 안 된다."""
    root = _clean_violating_project(tmp_path, "clean")
    _declare(root, "schema_version: 1\narchitecture:\n  mode: clean\n")

    assert [finding.path for finding in _lint(root)] == ["src/core/unmapped/thing.py"]


@pytest.mark.parametrize("body", [
    "schema_version: 1\narchitecture:\n  mode: pending\n",
    None,
])
def test_non_clean_selection_does_not_apply_clean_role_topology(tmp_path, body):
    """local·pending에 Clean 경로 규칙을 강요하면 선택이 의미를 잃는다."""
    root = _clean_violating_project(tmp_path, "probe")
    if body is None:
        _local_contract(root)
    else:
        _declare(root, body)

    assert _lint(root) == []


def _pinned_runner(root: Path):
    """run이 계약을 고정한 직후 상태의 runner."""
    from agent_flow.artifact import write_meta
    from agent_flow.runner import ResumeMode, Runner

    run_dir = root / ".agent-flow" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    write_meta(run_dir, {"task": "구조 작업"})

    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.config_root = root
    runner.project_root = root
    runner.profile = {}
    runner._initialize_architecture_policy(ResumeMode.START)
    return runner




def test_runner_blocks_when_the_contract_content_changes_mid_run(tmp_path):
    """SPEC-7. 규범이 바뀌면 기존 증거로 그 phase를 끝낼 수 없다."""
    root = _git_project(tmp_path)
    _local_contract(root, references=("references/a.md",))
    runner = _pinned_runner(root)

    _write(root, "skills/architecture/references/a.md", "# a\n\n규칙이 바뀌었다.\n")

    assert runner._architecture_policy_block_reason() == "architecture_policy_drift"


def test_runner_blocks_a_mode_switch_used_to_escape_a_failed_review(tmp_path):
    """I4. local에서 막힌 뒤 pending으로 바꿔 재심받는 우회를 닫는다."""
    root = _git_project(tmp_path)
    _local_contract(root)
    runner = _pinned_runner(root)

    _declare(root, "schema_version: 1\narchitecture:\n  mode: pending\n")

    assert runner._architecture_policy_block_reason() == "architecture_policy_drift"


def test_runner_blocks_an_unreadable_declaration_instead_of_assuming_clean(tmp_path):
    """Verify that runner blocks an unreadable declaration instead of assuming clean."""
    root = _git_project(tmp_path)
    _declare(root, "schema_version: 1\narchitecture:\n  mode: clean\n")
    runner = _pinned_runner(root)

    _declare(root, "schema_version: 1\narchitecture:\n  mode: whatever\n")

    assert runner._architecture_policy_block_reason() == "architecture_policy_unreadable"


AGENCY_ROOT = "apps/console/src/app/workspace/catalog"
AGENCY_SHARED = "apps/console/src/shared/api/catalog.ts"
AGENCY_FILES = {
    "page.tsx": (
        'import { CatalogForm } from "./_ui/CatalogForm";\n'
        'export default function Page() { return <CatalogForm />; }\n'
    ),
    "_lib/toPayload.ts": (
        'export type CatalogDraft = { name: string };\n'
        'export function toPayload(draft: CatalogDraft) {\n'
        '  return { name: draft.name.trim() };\n'
        '}\n'
    ),
    "_lib/__tests__/toPayload.test.ts": (
        'import { expect, test } from "vitest";\n'
        'import { toPayload } from "../toPayload";\n'
        'test("trims user input", () => {\n'
        '  expect(toPayload({ name: "  Atlas " })).toEqual({ name: "Atlas" });\n'
        '});\n'
    ),
}
AGENCY_API = (
    'export async function loadCatalog(url: string): Promise<{ name: string }> {\n'
    '  const response = await fetch(url);\n'
    '  if (!response.ok) throw new Error(`Load failed: ${response.status}`);\n'
    '  return response.json();\n'
    '}\n'
    'export async function saveCatalog(payload: { name: string }) {\n'
    '  const response = await fetch("/api/catalog", {\n'
    '    method: "POST", headers: { "Content-Type": "application/json" },\n'
    '    body: JSON.stringify(payload),\n'
    '  });\n'
    '  if (!response.ok) throw new Error(`Save failed: ${response.status}`);\n'
    '}\n'
)
AGENCY_HOOK = (
    '"use client";\n'
    'import { useForm } from "react-hook-form";\n'
    'import { useMutation } from "@tanstack/react-query";\n'
    'import { saveCatalog } from "@/shared/api/catalog";\n'
    'import { toPayload, type CatalogDraft } from "../_lib/toPayload";\n'
    'export function useCatalogForm(initialValues: CatalogDraft = { name: "" }) {\n'
    '  const form = useForm<CatalogDraft>({ defaultValues: initialValues });\n'
    '  const mutation = useMutation({ mutationFn: saveCatalog });\n'
    '  const submit = form.handleSubmit(draft => mutation.mutate(toPayload(draft)));\n'
    '  return { form, mutation, submit };\n'
    '}\n'
)


def _agency_project(tmp_path: Path, name: str = "agency", *, separate_hooks=False) -> Path:
    """Create the anonymized agency project fixture."""
    root = _git_project(tmp_path, name)
    for relative, content in AGENCY_FILES.items():
        _write(root, f"{AGENCY_ROOT}/{relative}", content)
    if separate_hooks:
        _write(
            root, f"{AGENCY_ROOT}/page.tsx",
            'import { QueryClient } from "@tanstack/react-query";\n'
            'import { loadCatalog } from "@/shared/api/catalog";\n'
            'import { CatalogForm } from "./_ui/CatalogForm";\n'
            'export default async function Page() {\n'
            '  const url = process.env.CATALOG_API_URL;\n'
            '  if (!url) throw new Error("CATALOG_API_URL is required");\n'
            '  const queryClient = new QueryClient();\n'
            '  const draft = await queryClient.fetchQuery({\n'
            '    queryKey: ["catalog"], queryFn: () => loadCatalog(url),\n'
            '  });\n'
            '  return <CatalogForm initialValues={draft} />;\n'
            '}\n',
        )
    hook_dir = "hooks" if separate_hooks else "_lib"
    hook = AGENCY_HOOK if separate_hooks else AGENCY_HOOK.replace("../_lib/toPayload", "./toPayload")
    _write(root, f"{AGENCY_ROOT}/{hook_dir}/useCatalogForm.ts", hook)
    _write(
        root, f"{AGENCY_ROOT}/_ui/CatalogForm.tsx",
        '"use client";\n'
        f'import {{ useCatalogForm }} from "../{hook_dir}/useCatalogForm";\n'
        'export function CatalogForm({ initialValues }: { initialValues?: { name: string } }) {\n'
        '  const { form, mutation, submit } = useCatalogForm(initialValues);\n'
        '  return <form onSubmit={submit}>\n'
        '    <label>Name<input {...form.register("name", { required: true })} /></label>\n'
        '    <button disabled={mutation.isPending}>Save</button>\n'
        '    {mutation.error ? <p role="alert">{mutation.error.message}</p> : null}\n'
        '  </form>;\n'
        '}\n',
    )
    _write(root, AGENCY_SHARED, AGENCY_API)
    _track(root, "apps")
    return root


@pytest.mark.parametrize("separate_hooks", [False, True], ids=["route-lib", "route-hooks"])
def test_agency_fixture_uses_anonymized_naming(tmp_path, separate_hooks):
    """Verify that agency fixture uses anonymized naming."""
    root = _agency_project(tmp_path, separate_hooks=separate_hooks)
    paths = set(subprocess.run(
        ["git", "ls-files"], cwd=root, check=True, text=True, capture_output=True,
    ).stdout.splitlines())
    assert all(path.startswith(f"{AGENCY_ROOT}/") or path == AGENCY_SHARED for path in paths)
    hook_dir = "hooks" if separate_hooks else "_lib"
    assert f"{AGENCY_ROOT}/{hook_dir}/useCatalogForm.ts" in paths
    assert f"{AGENCY_ROOT}/_ui/CatalogForm.tsx" in paths
    assert f"{AGENCY_ROOT}/_lib/__tests__/toPayload.test.ts" in paths


def test_case_personal_project_keeps_clean_obligations(tmp_path, monkeypatch):
    """케이스 A. 개인 프로젝트에서 Clean을 고르면 기존 의무가 그대로다."""
    root = _clean_violating_project(tmp_path, "personal")
    _clean_contract(root)

    required = _required_names(_resolution(root, monkeypatch))

    assert "clean-architecture-core" in required
    assert architecture_contract_required(_resolution(root, monkeypatch)) is True
    assert _architecture_angle_runs(root, monkeypatch) is True
    assert [finding.path for finding in _lint(root)] == ["src/core/unmapped/thing.py"]


@pytest.mark.parametrize("separate_hooks", [False, True], ids=["route-lib", "route-hooks"])
def test_case_agency_local_contract_replaces_clean_obligations(tmp_path, monkeypatch, separate_hooks):
    """케이스 B. 외주의 비Clean 구조에서 Clean 전용 의무는 빠지고 선택 계약이 필수가 된다."""
    root = _agency_project(tmp_path, separate_hooks=separate_hooks)
    _local_contract(root, references=("references/approved-patterns.md",))

    resolution = _resolution(root, monkeypatch)
    required = _required_names(resolution)
    snapshot = architecture_snapshot(root)

    assert "clean-architecture-core" not in required
    assert "architecture" in required
    assert architecture_contract_required(resolution) is True
    assert _architecture_angle_runs(root, monkeypatch) is True
    assert [document.path for document in snapshot.contract.documents] == [
        "skills/architecture/SKILL.md",
        "skills/architecture/references/approved-patterns.md",
    ]
    assert {"code-generation-discipline", "tdd"} <= required


def test_pending_blocks_structural_decision_and_allows_local_change(tmp_path, monkeypatch):
    """SPEC-4. pending은 면제가 아니고, 미평가를 pass로 적지도 않는다."""
    root = _agency_project(tmp_path, "undecided")
    _declare(root, "schema_version: 1\narchitecture:\n  mode: pending\n")

    resolution = _resolution(root, monkeypatch)
    snapshot = architecture_snapshot(root)

    # 구조 결정을 판정할 계약이 없다. 그 상태는 "통과"가 아니라 "기준 없음"이다.
    assert snapshot.contract is None
    assert architecture_contract_required(resolution) is False
    assert _architecture_angle_runs(root, monkeypatch) is False
    assert {"code-generation-discipline", "tdd"} <= _required_names(resolution)
    runner = _pinned_runner(root)
    assert runner._architecture_decision_block_reason(_workflow_phase("design")) == "architecture_decision_pending"
    assert runner._architecture_decision_block_reason(_workflow_phase("implement")) is None


def test_case_pending_then_decided_requires_new_evidence(tmp_path, monkeypatch):
    """케이스 C. 미정으로 시작해 나중에 확정되면, 그 run의 기존 증거로 끝낼 수 없다."""
    root = _agency_project(tmp_path, "later-decided")
    _declare(root, "schema_version: 1\narchitecture:\n  mode: pending\n")
    runner = _pinned_runner(root)
    assert architecture_contract_required(_resolution(root, monkeypatch)) is False

    # 회사가 구조를 확정하고 규범 문서를 승인한다.
    _local_contract(root, references=("references/approved-patterns.md",))

    # 진행 중이던 run은 옛 기준의 증거를 재사용하지 못한다.
    assert runner._architecture_policy_block_reason() == "architecture_policy_drift"
    # 새 run은 확정된 계약을 필수로 받는다.
    assert architecture_contract_required(_resolution(root, monkeypatch)) is True
    assert _architecture_angle_runs(root, monkeypatch) is True


THREE_CASE_NODE_IDS = (
    "tests/test_architecture_selection.py::test_case_personal_project_keeps_clean_obligations",
    "tests/test_architecture_selection.py::test_case_agency_local_contract_replaces_clean_obligations",
    "tests/test_architecture_selection.py::test_case_pending_then_decided_requires_new_evidence",
)


def test_three_cases_are_registered_in_frozen_contracts():
    """SPEC-8. 전 스위트의 통과/실패 하나로는 '이 계약이 사라졌다'를 못 본다.

    없는 테스트는 실패하지 않는다. 이름이 바뀌어 조용히 사라지는 경우를 붉은 줄로
    바꾸는 자리는 `frozen-invariants` job이 읽는 이 목록뿐이다.
    """
    registry = (KIT_ROOT / "tests" / "frozen_contracts.txt").read_text(encoding="utf-8")
    registered = {
        line.split()[1]
        for line in registry.splitlines()
        if line.strip() and not line.lstrip().startswith("#") and len(line.split()) == 2
    }

    assert set(THREE_CASE_NODE_IDS) <= registered


def test_no_new_ci_workflow_is_added():
    """SPEC-9. 회귀 감지는 기존 job이 맡는다. 새 workflow 파일을 만들지 않는다."""
    workflows = {path.name for path in (KIT_ROOT / ".github" / "workflows").iterdir()}

    assert workflows == {"tests.yml", "release.yml", "parity.yml"}


def test_ddd_stays_required_in_every_mode(tmp_path, monkeypatch):
    """설치 목록과 런타임 required가 갈리면 아무도 지울 수 없는 degraded가 남는다.

    `ddd-architecture`는 도메인 모델링이고 workflow의 DDD 단계가 선택과 무관하게
    요구한다. 설치기가 그것을 Clean 팩으로 묶어 빼면 그 상태가 된다.
    """
    from agent_flow.core.skill_resolver import PhaseSkills, resolve_phase_skills

    declared = ("code-generation-discipline", "clean-architecture-core", "ddd-architecture")
    for name, declare in (
        ("clean", _clean_contract),
        ("pending", lambda root: _declare(root, "schema_version: 1\narchitecture:\n  mode: pending\n")),
        ("local", _local_contract),
    ):
        root = _git_project(tmp_path, name)
        declare(root)
        monkeypatch.setenv("HOME", str(root / "empty-home"))

        resolution = resolve_phase_skills(
            project_root=root,
            phase_id="implement",
            phase_skills=PhaseSkills(required=declared, replaceable_architecture=True),
        )

        assert "ddd-architecture" in {skill.name for skill in resolution.required}, name




def test_selected_contract_resolves_to_its_declared_path(tmp_path, monkeypatch):
    """같은 이름의 drop-box 문서가 규범을 대체하면 digest와 실제 전달본이 갈린다."""
    root = _git_project(tmp_path)
    _local_contract(root)
    shadow = root / ".agent-flow" / "local-skills" / "architecture"
    shadow.mkdir(parents=True)
    (shadow / "SKILL.md").write_text("---\nname: architecture\n---\n\n# 다른 규범\n", encoding="utf-8")

    resolution = _resolution(root, monkeypatch)
    contract = next(skill for skill in resolution.required if skill.name == "architecture")

    assert contract.path == root / "skills/architecture/SKILL.md"
    assert contract.exists is True


def test_non_clean_lint_reports_not_applicable_instead_of_passed(tmp_path, capsys):
    """미평가를 `passed`로 찍으면 필수 gate가 검증하지 않은 것을 검증했다고 기록한다."""
    from agent_flow.core.architecture_lint import main as architecture_lint_main

    root = _clean_violating_project(tmp_path, "probe")
    _declare(root, "schema_version: 1\narchitecture:\n  mode: pending\n")

    exit_code = architecture_lint_main(
        ["--root", str(root), "--profile-root", str(KIT_ROOT), "--profile", "python",
         "--files", "src/core/unmapped/thing.py"]
    )
    printed = capsys.readouterr().out

    assert exit_code == 0
    assert "architecture lint n/a (architecture selection: pending)" in printed
    assert "passed" not in printed


def test_non_clean_lint_rejects_project_architecture_override(tmp_path, capsys):
    from agent_flow.core.architecture_lint import main as architecture_lint_main

    root = _clean_violating_project(tmp_path, "probe")
    _declare(root, "schema_version: 1\narchitecture:\n  mode: pending\n")
    override = root / ".agent-flow/profiles/python.local.yaml"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text("architecture:\n  roles: []\n", encoding="utf-8")

    exit_code = architecture_lint_main(
        ["--root", str(root), "--profile-root", str(KIT_ROOT), "--profile", "python",
         "--files", "src/core/unmapped/thing.py"]
    )
    printed = capsys.readouterr()

    assert exit_code == 1
    assert "architecture" in printed.err
    assert "n/a" not in printed.out

    from agent_flow.core.architecture_lint import lint_profiles, lint_project

    with pytest.raises(ValueError, match="legacy architecture override conflicts"):
        lint_project(root, "python", files=[], profile_root=KIT_ROOT)
    with pytest.raises(ValueError, match="legacy architecture override conflicts"):
        lint_profiles(root, ["python"], files=[], profile_root=KIT_ROOT)


def _workflow_phase(phase_id):
    """Return a workflow phase configured for the test scenario."""
    from agent_flow.core.phase_workflow import load_phase_workflow_definition
    from agent_flow.runner import _phases_from_definition

    definition = load_phase_workflow_definition(KIT_ROOT, "default")
    return next(phase for phase in _phases_from_definition(definition) if phase.id == phase_id)


def test_pending_waits_at_a_phase_that_must_decide_boundaries(tmp_path):
    """Verify that pending waits at a phase that must decide boundaries."""
    root = _git_project(tmp_path)
    _declare(root, "schema_version: 1\narchitecture:\n  mode: pending\n")
    runner = _pinned_runner(root)

    assert runner._architecture_decision_block_reason(
        _workflow_phase("design")
    ) == "architecture_decision_pending"


def test_pending_does_not_block_a_phase_that_decides_nothing_structural(tmp_path):
    """Verify that pending does not block a phase that decides nothing structural."""
    root = _git_project(tmp_path)
    _declare(root, "schema_version: 1\narchitecture:\n  mode: pending\n")
    runner = _pinned_runner(root)

    assert runner._architecture_decision_block_reason(_workflow_phase("implement")) is None


@pytest.mark.parametrize("declare", [
    lambda root: _declare(root, "schema_version: 1\narchitecture:\n  mode: clean\n"),
    _local_contract,
])
def test_a_decided_project_passes_the_same_phase(tmp_path, declare):
    """Verify that a decided project passes the same phase."""
    root = _git_project(tmp_path)
    declare(root)
    runner = _pinned_runner(root)

    assert runner._architecture_decision_block_reason(_workflow_phase("design")) is None


def test_contract_change_during_first_author_turn_blocks_resume(tmp_path, monkeypatch):
    """Verify that contract change during first author turn blocks resume."""
    from agent_flow.artifact import read_meta
    from agent_flow.core.worktrees import plan_worktree, worktree_runtime_root
    from tests.test_runner_smoke import _init_git_project, _run_cli

    root = tmp_path / "tracked-project"
    root.mkdir()
    _init_git_project(root)
    _local_contract(root, references=("references/patterns.md",))
    subprocess.run(
        ["git", "commit", "-qm", "Declare architecture"], cwd=root, check=True,
    )
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    task = "architecture contract first turn"
    environment = {"AGENT_FLOW_GENERIC_MODE": "", "AGENT_FLOW_REVIEWERS": "codex"}
    started = _run_cli(["run", task, "--workflow", "development"], root, environment)
    assert started.returncode == 0, started.stderr
    assert "status: awaiting_host" in started.stdout
    plan = plan_worktree(root=root, name=task)
    runtime = worktree_runtime_root(root=root, name=plan.name)
    run_dir = next(path for path in (runtime / ".agent-flow/runs").iterdir() if path.is_dir())
    meta = read_meta(run_dir)
    checkout = plan.path
    _write(
        checkout, "skills/architecture/references/patterns.md",
        "# Changed rules\n\nThe existing layout is no longer accepted.\n",
    )

    resumed = _run_cli(["continue", "--worktree", plan.name], root, environment)

    assert "reason: architecture_policy_drift" in resumed.stdout
    assert read_meta(run_dir)["current_phase"] == meta["current_phase"]


def test_transitive_local_dependency_change_blocks_existing_run_evidence(tmp_path, monkeypatch):
    """Verify that transitive local dependency change blocks existing run evidence."""
    from agent_flow.artifact import read_meta
    from agent_flow.core.worktrees import plan_worktree, worktree_runtime_root
    from tests.test_runner_smoke import _init_git_project, _run_cli

    root = tmp_path / "dependency-project"
    root.mkdir()
    _init_git_project(root)
    _local_contract(root)
    _write(
        root, "skills/architecture/SKILL.md",
        "---\nname: architecture\nrequires: [common-rule]\n---\n\nFeature-local ownership.\n",
    )
    home = tmp_path / "empty-home"
    for name, metadata in (
        ("common-rule", "dependencies: [legacy-rule]\n"),
        ("legacy-rule", "requires_by_architecture:\n  local: [required-rule]\n  clean: [clean-rule]\n"),
        ("required-rule", ""),
        ("clean-rule", ""),
        ("optional-rule", ""),
    ):
        _write(
            home, f".agents/skills/{name}/SKILL.md",
            f"---\nname: {name}\n{metadata}---\n\nKeep authorized ownership.\n",
        )
    _track(root, "skills")
    subprocess.run(["git", "commit", "-qm", "Declare dependency norms"], cwd=root, check=True)
    monkeypatch.setenv("HOME", str(home))
    task = "local dependency evidence"
    environment = {"AGENT_FLOW_GENERIC_MODE": "", "AGENT_FLOW_REVIEWERS": "codex"}
    started = _run_cli(["run", task, "--workflow", "development"], root, environment)
    assert started.returncode == 0, started.stderr
    assert "status: awaiting_host" in started.stdout
    plan = plan_worktree(root=root, name=task)
    runtime = worktree_runtime_root(root=root, name=plan.name)
    run_dir = next(path for path in (runtime / ".agent-flow/runs").iterdir() if path.is_dir())
    meta = read_meta(run_dir)
    for name in ("optional-rule", "clean-rule"):
        _write(
            home, f".agents/skills/{name}/SKILL.md",
            f"---\nname: {name}\n---\n\nUnselected guidance changed.\n",
        )

    unchanged = _run_cli(["continue", "--worktree", plan.name], root, environment)

    assert "status: awaiting_host" in unchanged.stdout
    assert read_meta(run_dir)["phase_entered_at"] == meta["phase_entered_at"]
    artifact = run_dir / f"{meta['current_phase']}.md"
    old_evidence = "# Explore\n\nApproved under the original dependency norm.\n"
    artifact.write_text(old_evidence, encoding="utf-8")
    _write(
        home, ".agents/skills/required-rule/SKILL.md",
        "---\nname: required-rule\n---\n\nThe previous ownership rule is no longer accepted.\n",
    )

    resumed = _run_cli(["continue", "--worktree", plan.name], root, environment)

    assert "reason: architecture_policy_drift" in resumed.stdout
    assert read_meta(run_dir)["current_phase"] == meta["current_phase"]
    assert artifact.read_text(encoding="utf-8") == old_evidence


@pytest.mark.parametrize(
    ("profile_id", "workflow", "phase_id", "local_path", "structural_path"),
    [
        ("python", "development", "implement", "app/title.py", "src/domain/catalog.py"),
        ("python", "development", "review", "app/title.py", "src/domain/catalog.py"),
        (
            "spring", "development", "implement",
            "src/main/kotlin/com/example/Title.kt", "src/main/kotlin/com/example/domain/Catalog.kt",
        ),
        (
            "spring", "development", "review",
            "src/main/java/com/example/Title.java", "src/main/java/com/example/application/Checkout.java",
        ),
        (
            "spring", "bugfix", "implement-fix",
            "src/main/kotlin/com/example/Title.kt", "src/main/kotlin/com/example/application/Checkout.kt",
        ),
        (
            "spring", "bugfix", "review",
            "src/main/java/com/example/Title.java", "src/main/java/com/example/domain/Catalog.java",
        ),
        (
            "spring", "review", "review",
            "src/main/kotlin/Title.kt", "src/main/kotlin/domain/Catalog.kt",
        ),
    ],
)
def test_pending_blocks_grown_boundary_scope(
    tmp_path, profile_id, workflow, phase_id, local_path, structural_path,
):
    """Verify that pending blocks grown boundary scope."""
    from agent_flow.core.phase_workflow import load_phase_workflow_definition
    from agent_flow.core.profiles import load_profile_payload
    from agent_flow.runner import _phases_from_definition

    root = _git_project(tmp_path)
    _declare(root, "schema_version: 1\narchitecture:\n  mode: pending\n")
    runner = _pinned_runner(root)
    runner.profile = load_profile_payload(profile_id)
    phase = next(
        phase for phase in _phases_from_definition(load_phase_workflow_definition(KIT_ROOT, workflow))
        if phase.id == phase_id
    )
    _write(root, local_path, 'TITLE = "Updated title"\n' if profile_id == "python" else "class Title {}\n")
    assert runner._architecture_decision_block_reason(phase) is None

    _write(root, structural_path, "class Catalog:\n    pass\n" if profile_id == "python" else "class Catalog {}\n")

    assert runner._architecture_decision_block_reason(phase) == "architecture_decision_pending"


def test_pending_allows_local_work_beside_existing_structural_roots(tmp_path):
    """Verify that pending allows local work beside existing structural roots."""
    from agent_flow.core.profiles import load_profile_payload

    root = _git_project(tmp_path)
    _declare(root, "schema_version: 1\narchitecture:\n  mode: pending\n")
    _write(root, "src/main/kotlin/com/example/domain/Catalog.kt", "class Catalog {}\n")
    _track(root, "src")
    subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-qm", "Existing domain model"], cwd=root, check=True,
    )
    runner = _pinned_runner(root)
    runner.profile = load_profile_payload("spring")
    _write(root, "src/main/kotlin/com/example/Title.kt", 'const val TITLE = "Updated title"\n')

    assert runner._architecture_decision_block_reason(_workflow_phase("implement")) is None


def test_pending_checks_structural_roles_in_a_profile_union(tmp_path):
    """Verify that pending checks structural roles in a profile union."""
    from agent_flow.core.profiles import load_profile_payload

    root = _git_project(tmp_path)
    _declare(root, "schema_version: 1\narchitecture:\n  mode: pending\n")
    runner = _pinned_runner(root)
    runner.profile = {
        "id": "multi-profile",
        "profiles": [load_profile_payload("generic"), load_profile_payload("spring")],
    }
    _write(root, "src/main/java/com/example/domain/Catalog.java", "class Catalog {}\n")

    assert runner._architecture_decision_block_reason(_workflow_phase("worktree")) is None
    assert runner._architecture_decision_block_reason(_workflow_phase("implement")) == "architecture_decision_pending"


def test_pending_generic_requires_explicit_structural_phase_evidence(tmp_path):
    """Verify that pending generic requires explicit structural phase evidence."""
    from agent_flow.core.profiles import load_profile_payload

    root = _git_project(tmp_path)
    _declare(root, "schema_version: 1\narchitecture:\n  mode: pending\n")
    runner = _pinned_runner(root)
    runner.profile = load_profile_payload("generic")
    _write(root, "src/domain/catalog.py", "class Catalog:\n    pass\n")

    assert runner._architecture_decision_block_reason(_workflow_phase("implement")) is None
    assert runner._architecture_decision_block_reason(_workflow_phase("design")) == "architecture_decision_pending"


def test_pending_blocks_declared_wiring_decision_outside_role_paths(tmp_path):
    """Verify that pending blocks declared wiring decision outside role paths."""
    from agent_flow.artifact import read_meta, write_meta
    from agent_flow.core.profiles import load_profile_payload

    root = _git_project(tmp_path)
    _declare(root, "schema_version: 1\narchitecture:\n  mode: pending\n")
    runner = _pinned_runner(root)
    runner.profile = load_profile_payload("python")
    _write(root, "app/bootstrap.py", "from domain.catalog import Catalog\n")
    meta = read_meta(runner.run_dir)
    meta["concerns"] = ["architecture"]
    write_meta(runner.run_dir, meta)

    assert runner._architecture_decision_block_reason(_workflow_phase("implement")) == "architecture_decision_pending"


@pytest.mark.parametrize("initial_norm", [False, True], ids=["empty", "existing"])
def test_grown_norms_require_new_attempt_without_repining_old_bytes(tmp_path, monkeypatch, initial_norm):
    """Verify that grown norms require new attempt without repining old bytes."""
    from agent_flow.artifact import read_meta, write_meta
    from agent_flow.core.local_skills import local_skill_prompt_block
    from agent_flow.core.profiles import load_profile_payload
    from agent_flow.core.skill_resolver import PhaseSkills
    from agent_flow.runner import Phase

    root = _git_project(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    _declare(root, "schema_version: 1\narchitecture:\n  mode: clean\n")
    core = _write(
        root, "skills/clean-architecture-core/SKILL.md",
        "---\nname: clean-architecture-core\n---\n\nDomain invariants own behavior.\n",
    )
    # 선택자가 있어야 "변경 범위가 자라서" required가 된다. 선택자 없는 `skills/` 문서는
    # 배치만으로 처음부터 켜져 이 테스트의 대상(범위 성장)이 사라진다.
    _write(
        root, "skills/python-api-clean-architecture/SKILL.md",
        "---\nname: python-api-clean-architecture\nrequires: [clean-architecture-core]\n"
        "workflowPhases: [implement]\npathGlobs: ['src/**/*.py']\n---\n\n"
        "Python handlers invoke application use cases.\n",
    )
    _track(root, "skills")
    subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-qm", "Declare initial policy"], cwd=root, check=True,
    )
    runner = _pinned_runner(root)
    runner.profile = load_profile_payload("python")
    runner._adapter_name = "codex"
    phase = Phase(
        id="implement", description="Apply existing patterns", prompt="",
        skills=PhaseSkills(required=("clean-architecture-core",) if initial_norm else ()),
    )
    runner.phases = [phase]
    assert runner._refresh_architecture_norms(phase, pin=True) is None
    artifact = runner.run_dir / "implement.md"
    artifact.write_text("Old completion before the boundary changed.\n", encoding="utf-8")
    meta = read_meta(runner.run_dir)
    meta["phase_approval"] = {"approved": True}
    meta["phase_approval_request"] = {"phase": phase.id}
    write_meta(runner.run_dir, meta)
    _write(root, "src/domain/catalog.py", "class Catalog:\n    pass\n")

    assert runner._refresh_architecture_norms(phase, pin=False) == "skill_scope_grew"
    assert artifact.read_text(encoding="utf-8") == "Old completion before the boundary changed.\n"
    assert runner._invalidate_architecture_evidence_for_reentry(phase) is None
    assert not artifact.exists()
    assert any(
        path.read_text(encoding="utf-8") == "Old completion before the boundary changed.\n"
        for path in runner.run_dir.glob("*.md")
    )
    assert "phase_approval" not in read_meta(runner.run_dir)
    assert "phase_approval_request" not in read_meta(runner.run_dir)
    assert runner._refresh_architecture_norms(phase, pin=True) is None
    prompt = local_skill_prompt_block(
        root, phase.id, phase_skills=phase.skills, profile=runner.profile,
        changed_files=("src/domain/catalog.py",), host="codex",
    )
    assert str(root / "skills/python-api-clean-architecture/SKILL.md") in prompt
    artifact.write_text("Reapplied the newly delivered Python contract.\n", encoding="utf-8")
    assert runner._stale_artifact_block_reason(artifact, read_meta(runner.run_dir)) is None

    core.write_text("Silently weakened old rules.\n", encoding="utf-8")
    assert runner._refresh_architecture_norms(phase, pin=True) == "architecture_policy_drift"
    assert artifact.read_text(encoding="utf-8") == "Reapplied the newly delivered Python contract.\n"


def test_status_checks_the_bound_contract_not_the_leader_selection(tmp_path, monkeypatch):
    """Verify that status checks the bound contract not the leader selection."""
    from agent_flow.artifact import _missing_completion_markers, write_meta
    from agent_flow.core.phase_workflow import load_phase_workflow_definition
    from agent_flow.core.workflow_pin import workflow_pin_metadata

    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    leader = _git_project(tmp_path, "leader")
    checkout = _git_project(tmp_path, "checkout")
    _local_contract(checkout)
    _declare(
        leader,
        "schema_version: 1\narchitecture:\n  mode: local\n  skill: skills/architecture/SKILL.md\n",
    )
    run_dir = leader / ".agent-flow/runs/status-contract"
    run_dir.mkdir(parents=True)
    definition = load_phase_workflow_definition(KIT_ROOT, "default")
    write_meta(run_dir, {
        "workflow": "default",
        "current_phase": "implement",
        "phase_index": next(i for i, phase in enumerate(definition.phases) if phase.id == "implement"),
        "task": "Update title",
        "architecture_digest": architecture_snapshot(checkout).digest,
        **workflow_pin_metadata(definition, workflow="default"),
    })
    artifact = run_dir / "implement.md"
    content = (
        "## Completion Gate\n"
        "skill-availability: pass\nskill-use-evidence: unavailable\n"
        "project-local-skills: checked\nproject-local-skill-docs: applied\n"
        "project-local-skills-used: n/a\n"
    )
    artifact.write_text(content, encoding="utf-8")

    missing = _missing_completion_markers(
        run_dir, "default", "implement", config_root=leader, project_root=checkout,
    )

    assert "project-local-skills-used: architecture" in missing
    assert not any("unreadable" in marker for marker in missing)
    artifact.write_text(content.replace("skills-used: n/a", "skills-used: architecture"), encoding="utf-8")
    applied = _missing_completion_markers(
        run_dir, "default", "implement", config_root=leader, project_root=checkout,
    )
    assert "project-local-skills-used: architecture" not in applied


@pytest.mark.parametrize("group", ["architecture", "domain-policy"])
def test_pending_honors_the_same_declared_task_selector_as_resolution(tmp_path, group):
    """Verify that pending honors the same declared task selector as resolution."""
    from agent_flow.artifact import read_meta, write_meta
    from agent_flow.runner import Phase

    root = _git_project(tmp_path)
    _declare(root, "schema_version: 1\narchitecture:\n  mode: pending\n")
    runner = _pinned_runner(root)
    runner.profile = {"skills": {"required_review": [
        {"group": group, "skills": ["clean-architecture-core"], "task_terms": ["boundary"]},
    ]}}
    meta = read_meta(runner.run_dir)
    meta["task"] = "Adjust a boundary"
    write_meta(runner.run_dir, meta)
    phase = Phase(id="review", description="Review existing scope", prompt="")

    assert runner._architecture_decision_block_reason(phase) == "architecture_decision_pending"


@pytest.mark.parametrize(("field", "value"), [
    ("architecture_norm_documents", []),
    ("architecture_norm_phases", []),
    ("architecture_norm_phases", {"implement": {"codex": []}}),
])
def test_malformed_norm_pins_block_without_removing_evidence(tmp_path, monkeypatch, field, value):
    """Verify that malformed norm pins block without removing evidence."""
    from agent_flow.artifact import read_meta, write_meta
    from agent_flow.runner import Phase

    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    root = _git_project(tmp_path)
    runner = _pinned_runner(root)
    runner._adapter_name = "codex"
    phase = Phase(id="implement", description="Apply existing patterns", prompt="")
    runner.phases = [phase]
    artifact = runner.run_dir / "implement.md"
    artifact.write_text("Previous evidence\n", encoding="utf-8")
    meta = read_meta(runner.run_dir)
    meta[field] = value
    write_meta(runner.run_dir, meta)

    assert runner._refresh_architecture_norms(phase, pin=True) == "architecture_policy_unreadable"
    assert artifact.read_text(encoding="utf-8") == "Previous evidence\n"


def test_retired_norm_replaced_by_symlink_cannot_reuse_its_digest(tmp_path, monkeypatch):
    """Verify that retired norm replaced by symlink cannot reuse its digest."""
    from agent_flow.artifact import read_meta, write_meta
    from agent_flow.runner import Phase

    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    root = _git_project(tmp_path)
    runner = _pinned_runner(root)
    runner._adapter_name = "codex"
    phase = Phase(id="implement", description="Apply existing patterns", prompt="")
    original = _write(root, "retired-norm.md", "Pinned rules\n")
    replacement = _write(root, "replacement.md", "Pinned rules\n")
    meta = read_meta(runner.run_dir)
    meta["architecture_norm_documents"] = {
        str(original): hashlib.sha256(original.read_bytes()).hexdigest(),
    }
    write_meta(runner.run_dir, meta)
    original.unlink()
    original.symlink_to(replacement)

    assert runner._refresh_architecture_norms(phase, pin=True) == "architecture_policy_unreadable"


def _started_norm_run(tmp_path, monkeypatch, *, declared=False):
    """Create a started run with pinned normative documents."""
    from agent_flow.core.worktrees import plan_worktree, worktree_runtime_root
    from tests.test_runner_smoke import _init_git_project, _run_cli

    root = tmp_path / "norm-project"
    root.mkdir()
    _init_git_project(root)
    _write(
        root, "skills/clean-architecture-core/SKILL.md",
        "---\nname: clean-architecture-core\nworkflowPhases: [explore]\n---\n\n"
        "Keep domain invariants independent of I/O.\n",
    )
    _track(root, "skills")
    if declared:
        _declare(root, "schema_version: 1\narchitecture:\n  mode: clean\n")
    subprocess.run(["git", "commit", "-qm", "Install architecture norm"], cwd=root, check=True)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    task = "fresh architecture evidence"
    started = _run_cli(
        ["run", task, "--workflow", "development"], root,
        {"AGENT_FLOW_GENERIC_MODE": "", "AGENT_FLOW_REVIEWERS": "codex"},
    )
    assert started.returncode == 0, started.stderr
    assert "status: awaiting_host" in started.stdout
    plan = plan_worktree(root=root, name=task)
    runtime = worktree_runtime_root(root=root, name=plan.name)
    run_dir = next(path for path in (runtime / ".agent-flow/runs").iterdir() if path.is_dir())
    return root, plan, run_dir


def test_legacy_run_requires_fresh_norm_evidence(tmp_path, monkeypatch):
    """Verify that legacy run requires fresh norm evidence."""
    from agent_flow.artifact import read_meta, write_meta
    from tests.test_runner_smoke import _run_cli

    root, plan, run_dir = _started_norm_run(tmp_path, monkeypatch)
    meta = read_meta(run_dir)
    for field in (
        "architecture_digest", "architecture_mode",
        "architecture_norm_documents", "architecture_norm_phases",
    ):
        meta.pop(field, None)
    previous_attempt = meta["phase_entered_at"]
    meta["fix_loop_rounds"] = {"fix-loop": 2}
    write_meta(run_dir, meta)
    old_evidence = "# Explore\n\nOld approval before norm pinning.\nverdict: approve\n"
    artifact = run_dir / "explore.md"
    artifact.write_text(old_evidence, encoding="utf-8")
    environment = {"AGENT_FLOW_GENERIC_MODE": "", "AGENT_FLOW_REVIEWERS": "codex"}

    resumed = _run_cli(["continue", "--worktree", plan.name], root, environment)

    assert "reason: architecture_norms_unpinned" in resumed.stdout
    assert not artifact.exists()
    assert any(
        path.read_text(encoding="utf-8") == old_evidence
        for path in run_dir.glob("*.md")
    )
    migrated = read_meta(run_dir)
    assert migrated["current_phase"] == "explore"
    assert migrated["phase_entered_at"] != previous_attempt
    assert migrated["fix_loop_rounds"] == {"fix-loop": 2}
    awaiting = _run_cli(["continue", "--worktree", plan.name], root, environment)
    assert "status: awaiting_host" in awaiting.stdout
    assert str(plan.path / "skills/clean-architecture-core/SKILL.md") in awaiting.stdout
    assert str(root / "skills/clean-architecture-core/SKILL.md") not in awaiting.stdout
    assert not artifact.exists()
    artifact.write_text("# Explore\n\nFresh inspection under the delivered norm.\n", encoding="utf-8")
    advanced = _run_cli(["continue", "--worktree", plan.name], root, environment)
    assert "status: awaiting_host" in advanced.stdout
    assert read_meta(run_dir)["current_phase"] == "implement"


def test_declared_unpinned_policy_requires_new_run_without_restore_advice(tmp_path, monkeypatch):
    """Verify that declared unpinned policy requires new run without restore advice."""
    from agent_flow.artifact import read_meta, write_meta
    from tests.test_runner_smoke import _run_cli

    root, plan, run_dir = _started_norm_run(tmp_path, monkeypatch, declared=True)
    meta = read_meta(run_dir)
    meta.pop("architecture_digest")
    write_meta(run_dir, meta)
    artifact = run_dir / "explore.md"
    artifact.write_text("Previous approval\n", encoding="utf-8")

    resumed = _run_cli(
        ["continue", "--worktree", plan.name], root,
        {"AGENT_FLOW_GENERIC_MODE": "", "AGENT_FLOW_REVIEWERS": "codex"},
    )

    assert "reason: architecture_policy_unpinned" in resumed.stdout
    assert "new run" in resumed.stdout.lower()
    assert "restore" not in resumed.stdout.lower()
    assert artifact.read_text(encoding="utf-8") == "Previous approval\n"
    assert "architecture_digest" not in read_meta(run_dir)


@pytest.mark.parametrize("damage", ["corrupt-phase", "missing-global-pin"])
def test_existing_norm_pin_damage_preserves_previous_evidence(tmp_path, monkeypatch, damage):
    """Verify that existing norm pin damage preserves previous evidence."""
    from agent_flow.artifact import read_meta, write_meta
    from tests.test_runner_smoke import _run_cli

    root, plan, run_dir = _started_norm_run(tmp_path, monkeypatch)
    meta = read_meta(run_dir)
    artifact = run_dir / "explore.md"
    artifact.write_text("Previous approval\n", encoding="utf-8")
    if damage == "corrupt-phase":
        meta["architecture_norm_phases"]["explore"]["generic"] = []
        write_meta(run_dir, meta)
    else:
        meta["architecture_norm_documents"] = {}
        write_meta(run_dir, meta)

    resumed = _run_cli(
        ["continue", "--worktree", plan.name], root,
        {"AGENT_FLOW_GENERIC_MODE": "", "AGENT_FLOW_REVIEWERS": "codex"},
    )

    assert "reason: architecture_policy_" in resumed.stdout
    assert artifact.read_text(encoding="utf-8") == "Previous approval\n"
    assert read_meta(run_dir)["phase_entered_at"] == meta["phase_entered_at"]


def test_optional_reviewer_cli_with_same_norms_preserves_approval(tmp_path, monkeypatch):
    """Verify that optional reviewer CLI with same norms preserves approval."""
    from agent_flow.core.skill_resolver import PhaseSkills
    from agent_flow.runner import Phase

    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    root = _git_project(tmp_path)
    _write(
        root, "skills/clean-architecture-core/SKILL.md",
        "---\nname: clean-architecture-core\n---\n\nDomain owns invariants.\n",
    )
    runner = _pinned_runner(root)
    runner._adapter_name = "codex"
    phase = Phase(
        id="review", description="Review scope", prompt="", multi_review=True,
        skills=PhaseSkills(required=("clean-architecture-core",)),
    )
    runner.phases = [phase]
    monkeypatch.setattr("agent_flow.runner.eligible_reviewer_names", lambda: ("codex",))
    assert runner._refresh_architecture_norms(phase, pin=True) is None
    artifact = runner.run_dir / "review.md"
    artifact.write_text("Approved under the unchanged norm.\n", encoding="utf-8")
    monkeypatch.setattr("agent_flow.runner.eligible_reviewer_names", lambda: ("codex", "claude"))

    assert runner._refresh_architecture_norms(phase, pin=False) is None
    assert artifact.read_text(encoding="utf-8") == "Approved under the unchanged norm.\n"


@pytest.mark.parametrize(
    ("allowed_phase", "phase_id", "blocked"),
    [("review", "explore", False), ("explore", "explore", True), ("explore", "review", False)],
)
def test_pending_architecture_routing_honors_declared_skill_phases(
    tmp_path, monkeypatch, allowed_phase, phase_id, blocked,
):
    """Verify that pending architecture routing honors declared skill phases."""
    from agent_flow.runner import Phase

    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    root = _git_project(tmp_path)
    _declare(root, "schema_version: 1\narchitecture:\n  mode: pending\n")
    _write(
        root, "skills/clean-architecture-core/SKILL.md",
        "---\nname: clean-architecture-core\n"
        f"workflowPhases: [{allowed_phase}]\n---\n\nDomain owns invariants.\n",
    )
    runner = _pinned_runner(root)
    runner.profile = {"skills": {"required_review": [
        {"group": "architecture", "skills": ["clean-architecture-core"], "task_terms": ["구조"]},
    ]}}
    phase = Phase(id=phase_id, description="Inspect scope", prompt="")

    reason = runner._architecture_decision_block_reason(phase)

    assert reason == ("architecture_decision_pending" if blocked else None)
