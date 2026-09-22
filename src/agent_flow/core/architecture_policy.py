"""프로젝트가 고른 아키텍처 기준과 그 규범 문서의 동일성.

이 모듈이 소유하는 것은 "어떤 구조 규범을 필수로 적용하는가" 하나다. workflow의
단계·순서·분기·완료 조건과 필수 스킬 준수 원칙은 여기서 바뀌지 않는다.

계층: 선택 구성과 규범 문서의 저장·검증을 담당하는 기술 어댑터다. 값 타입은 이
구성 형식의 계약이며 순수 도메인 모델이 아니다. `runner`, `adapters`, `cli`,
설치기를 import 하지 않고 실행·완료 정책은 호출자에게 남긴다.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import os
from pathlib import Path
import stat
from typing import Any

import yaml

from agent_flow.core.atomic_io import atomic_write_text, read_bounded_regular_file
from agent_flow.core.worktree_isolation import git_repo_state, git_safe, leader_root_for
from agent_flow.core.skill_metadata import (
    ARCHITECTURE_MODES,
    SkillMetadataError,
    parse_skill_metadata,
    reject_duplicate_keys,
    split_frontmatter,
)

# 정본 파일. `.agent-flow/` 안에 있으므로 gitignore되고 이 checkout의 결정이다 — 계약
# 문서(`skills/architecture/`)는 팀이 추적해 공유하지만, 어떤 모드를 켰는지는 머신마다
# 고른다. linked worktree는 생성 시 leader의 사본을 받는다(`ROOT_CONTEXT_FILES`).
PROJECT_ARCHITECTURE_FILE = ".agent-flow/project.yaml"
LEGACY_PROJECT_ARCHITECTURE_FILE = ".agent-flow.project.yaml"
SCHEMA_VERSION = 1
MAX_ARCHITECTURE_DOCUMENT_BYTES = 8 * 1024 * 1024

_DOCUMENT_KEYS = ("schema_version", "architecture")
_ARCHITECTURE_KEYS = ("mode", "skill")
# local 계약이 앉을 수 있는 자리는 둘뿐이다. `skills/`는 추적되는 팀 계약이라 clone·
# worktree·동료가 같은 규범을 본다. `.agent-flow/local-skills/`는 gitignore된 개인
# drop-box라 이 머신에서만 유효하다 — 그래서 추적 요구를 걸지 않고, linked worktree는
# leader의 사본을 읽는다.
TEAM_CONTRACT_PATH = "skills/architecture/SKILL.md"
PRIVATE_CONTRACT_PATH = ".agent-flow/local-skills/architecture/SKILL.md"
CONTRACT_PATHS = (TEAM_CONTRACT_PATH, PRIVATE_CONTRACT_PATH)


class ArchitectureMode(str, Enum):
    """프로젝트가 선언한 구조 기준. 닫힌 집합이다."""

    CLEAN, LOCAL, PENDING = ARCHITECTURE_MODES


@dataclass(frozen=True)
class ArchitectureSelection:
    """검증된 선택 하나.

    불변식을 생성자에 두어, 파일에서 왔든 CLI에서 왔든 같은 규칙을 통과한다.
    """

    mode: ArchitectureMode
    contract_path: str | None = None

    def __post_init__(self) -> None:
        """Validate and normalize the selected architecture mode and contract path."""
        if not isinstance(self.mode, ArchitectureMode):
            raise ValueError(
                f"architecture mode must be one of {_mode_values()}: {self.mode!r}"
            )
        if self.mode is ArchitectureMode.LOCAL:
            if self.contract_path is None:
                raise ValueError(
                    "architecture mode local requires a contract skill path "
                    f"({' or '.join(CONTRACT_PATHS)})"
                )
            _parse_contract_path(self.contract_path, source="architecture selection")
        elif self.contract_path is not None:
            raise ValueError(
                "architecture contract is supported only for mode local: "
                f"{self.mode.value} declares {self.contract_path!r}"
            )


def parse_architecture_selection(
    payload: Mapping[str, Any], *, source: str
) -> ArchitectureSelection:
    """이미 매핑으로 읽힌 선언을 검증된 선택으로 바꾼다."""
    if not isinstance(payload, Mapping):
        raise ValueError(f"{source}: architecture document must be a mapping")
    _reject_unsupported_keys(payload, _DOCUMENT_KEYS, source=source, scope="document")
    _validate_schema_version(payload.get("schema_version"), source=source)

    declared = payload.get("architecture")
    if not isinstance(declared, Mapping):
        raise ValueError(f"{source}: architecture must be a mapping")
    _reject_unsupported_keys(declared, _ARCHITECTURE_KEYS, source=source, scope="architecture")

    mode = _parse_mode(declared.get("mode"), source=source)
    if mode is not ArchitectureMode.LOCAL and "skill" in declared:
        raise ValueError(f"{source}: architecture skill is supported only for mode local")
    contract_path = declared.get("skill")
    try:
        return ArchitectureSelection(mode=mode, contract_path=contract_path)
    except ValueError as exc:
        raise ValueError(f"{source}: {exc}") from exc


def parse_architecture_document(text: str, *, source: str) -> ArchitectureSelection:
    """정본 파일 원문을 검증된 선택으로 바꾼다.

    `yaml.safe_load`는 중복 키를 last-wins로 삼킨다. 그러면 두 선언 중 무엇이
    유효한지 리뷰에서 보이지 않으므로, 매핑으로 접기 전에 노드에서 잡는다.
    """
    payload = _load_yaml_mapping(text, source=source)
    return parse_architecture_selection(payload, source=source)


def _mode_values() -> str:
    """Return the canonical architecture mode values."""
    return ", ".join(mode.value for mode in ArchitectureMode)


def _parse_mode(value: object, *, source: str) -> ArchitectureMode:
    """Parse an architecture mode from project configuration."""
    if not isinstance(value, str):
        raise ValueError(
            f"{source}: architecture mode must be one of {_mode_values()}: {value!r}"
        )
    try:
        return ArchitectureMode(value)
    except ValueError as exc:
        raise ValueError(
            f"{source}: architecture mode must be one of {_mode_values()}: {value!r}"
        ) from exc


def _parse_contract_path(value: object, *, source: str) -> str | None:
    """Validate and normalize a project-relative contract path."""
    if value is None:
        return None
    if not isinstance(value, str) or value not in CONTRACT_PATHS:
        raise ValueError(
            f"{source}: architecture contract path must be one of "
            f"{', '.join(CONTRACT_PATHS)}: {value!r}"
        )
    return value


def _validate_schema_version(value: object, *, source: str) -> None:
    # 모르는 버전에서 조용히 기본값으로 도는 것은 정책 변경이다. 비호환을 알린다.
    """Validate the architecture selection schema version."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"{source}: schema_version must be the integer {SCHEMA_VERSION}: {value!r}"
        )
    if value != SCHEMA_VERSION:
        raise ValueError(
            f"{source}: unsupported schema_version {value}; this kit reads "
            f"{SCHEMA_VERSION}. upgrade agent-flow instead of editing the declaration"
        )


def _reject_unsupported_keys(
    payload: Mapping[str, Any], supported: tuple[str, ...], *, source: str, scope: str
) -> None:
    """Reject keys outside the architecture selection schema."""
    unsupported = sorted(str(key) for key in payload if key not in supported)
    if unsupported:
        raise ValueError(
            f"{source}: unsupported {scope} keys: {', '.join(unsupported)}; "
            f"supported: {', '.join(supported)}"
        )


def _load_yaml_mapping(text: str, *, source: str) -> Mapping[str, Any]:
    """Load a YAML document as a mapping with strict syntax checks."""
    try:
        loader = yaml.SafeLoader(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"{source}: invalid YAML: {exc}") from exc
    try:
        document = loader.get_single_node()
        if document is None:
            return {}
        reject_duplicate_keys(document, source=source)
        payload = loader.construct_document(document)
    except (yaml.YAMLError, RecursionError) as exc:
        raise ValueError(f"{source}: invalid YAML: {exc}") from exc
    finally:
        loader.dispose()
    if not isinstance(payload, Mapping):
        raise ValueError(f"{source}: YAML document must be a mapping")
    return payload




class ArchitectureContractError(ValueError):
    """선택한 계약 문서에 도달할 수 없다.

    일반 skill 부재(`degraded`)와 구분한다. 그쪽은 설치 문제라 진행을 막지 않지만,
    명시적으로 선택한 계약이 없으면 "규범을 지켰다"가 검증 불가능한 주장이 된다.
    """


@dataclass(frozen=True)
class ContractDocument:
    """규범 문서 하나의 동일성. 경로가 아니라 내용으로 고정한다."""

    path: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class ArchitectureContract:
    """선택이 가리키는 규범 문서 집합.

    `documents[0]`은 항상 root이고, 나머지는 root가 선언한 순서를 유지한다.
    순서까지 digest에 넣는 이유는 읽는 순서가 규범의 일부이기 때문이다.
    """

    documents: tuple[ContractDocument, ...]
    untracked: tuple[str, ...]
    contents: tuple[str, ...]

    @property
    def root(self) -> ContractDocument:
        """Return the document that defines the architecture contract root."""
        return self.documents[0]


@dataclass(frozen=True)
class ArchitectureSnapshot:
    """run 하나가 고정한 선택과 그 규범 내용.

    `declared`는 정본 파일에 선언이 있었는지다. 선언 없음을 pending으로 읽으면
    기존 설치의 Clean 강제가 조용히 사라지므로, 두 상태를 합치지 않는다.
    """

    selection: ArchitectureSelection
    declared: bool
    contract: ArchitectureContract | None
    digest: str
    source_document: ContractDocument | None = None
    untracked: tuple[str, ...] = ()


def load_architecture_selection(root: Path) -> ArchitectureSelection | None:
    """정본 파일의 선택. 선언이 없으면 `None`(legacy)이다."""
    with _repository_directory(root) as repository_fd:
        selection, _ = _load_selection_document(repository_fd)
        return selection


def _load_selection_document(
    repository_fd: int,
) -> tuple[ArchitectureSelection | None, ContractDocument | None]:
    """Load and validate the project's architecture selection document.

    0.3.x는 선언을 저장소 루트 `.agent-flow.project.yaml`에 두었다. 새 자리가 비어 있으면
    그 파일을 읽는다 — 무시하면 `pending`이던 프로젝트가 조용히 legacy `clean`이 된다.
    설치기가 새 자리로 복사하고, 그 뒤로는 새 자리가 이긴다.
    """
    for relative in (PROJECT_ARCHITECTURE_FILE, LEGACY_PROJECT_ARCHITECTURE_FILE):
        with _open_document(repository_fd, relative, allow_missing=True) as descriptor:
            if descriptor is None:
                continue
            payload = _read_document_bytes(descriptor, relative)
        document, payload = _validate_contract_document(relative, payload, kind="selection")
        selection = parse_architecture_document(payload.decode("utf-8"), source=relative)
        return selection, document
    return None, None


def resolve_architecture_contract(
    root: Path, selection: ArchitectureSelection
) -> ArchitectureContract | None:
    """local 계약의 실제 파일 내용을 읽어 고정한다.

    clean은 kit이 배포하는 규범을 쓰고 pending은 아직 규범이 없으므로 둘 다 `None`이다.
    그 `None`은 "계약 없음"이지 "검증 면제"가 아니다.
    """
    with _repository_directory(root) as repository_fd:
        return _resolve_architecture_contract(root, selection, repository_fd)


def is_private_contract(selection: ArchitectureSelection) -> bool:
    """계약이 gitignore된 개인 drop-box에 있는가."""
    return selection.contract_path == PRIVATE_CONTRACT_PATH


def contract_documents_root(root: Path, selection: ArchitectureSelection) -> Path:
    """계약 문서 경로(`ContractDocument.path`)의 기준 디렉터리.

    팀 계약은 checkout마다 추적 사본이 있으니 `root` 그대로다. 개인 계약은 `.agent-flow/`
    아래라 linked worktree에는 없다 — 설치기가 leader에만 심는 다른 `.agent-flow/` 자산과
    같은 이유로 leader 것을 읽는다.
    """
    if not is_private_contract(selection):
        return root
    return leader_root_for(root) or root


def _resolve_architecture_contract(
    root: Path, selection: ArchitectureSelection, repository_fd: int
) -> ArchitectureContract | None:
    """Resolve the selected local contract and its normative documents."""
    if selection.mode is not ArchitectureMode.LOCAL:
        return None
    assert selection.contract_path is not None  # 생성자 불변식
    documents_root = contract_documents_root(root, selection)
    if documents_root != root:
        with _repository_directory(documents_root) as leader_fd:
            return _read_contract(documents_root, selection, leader_fd)
    return _read_contract(root, selection, repository_fd)


def _read_contract(
    root: Path, selection: ArchitectureSelection, repository_fd: int
) -> ArchitectureContract:
    """Pin the contract root and its declared references beneath one directory."""
    contract_root = selection.contract_path
    assert contract_root is not None
    root_document, root_bytes = _read_contract_document(repository_fd, contract_root, kind="contract")
    documents = [root_document]
    contents = [root_bytes.decode("utf-8")]
    base = contract_root.rsplit("/", 1)[0]
    for reference in _declared_references(root_bytes, source=contract_root):
        document, content = _read_contract_document(
            repository_fd, f"{base}/{reference}", kind="contract reference"
        )
        documents.append(document)
        contents.append(content.decode("utf-8"))
    # 개인 계약은 gitignore된 자리에 있는 것이 정의다. 추적을 요구하면 선택 자체가 불가능하다.
    untracked = () if is_private_contract(selection) else _untracked_paths(
        root, [document.path for document in documents]
    )
    return ArchitectureContract(
        documents=tuple(documents),
        untracked=untracked,
        contents=tuple(contents),
    )


def _is_norm_manifest(value: object) -> bool:
    """Return whether a path names a pinned architecture norm manifest."""
    return isinstance(value, dict) and all(
        isinstance(path, str) and Path(path).is_absolute()
        and isinstance(digest, str) and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
        for path, digest in value.items()
    )


def architecture_snapshot(root: Path) -> ArchitectureSnapshot:
    """이 checkout의 선택과 규범 내용을 한 번에 고정한다."""
    with _repository_directory(root) as repository_fd:
        declared_selection, source_document = _load_selection_document(repository_fd)
        # 선언이 없는 기존 설치는 종전 유효 정책을 유지한다. 여기서 pending으로 읽으면
        # 재설치도 사용자 결정도 없이 강제가 사라진다.
        selection = declared_selection or ArchitectureSelection(mode=ArchitectureMode.CLEAN)
        return _snapshot_from_selection(root, selection, source_document, repository_fd)


def architecture_snapshot_block_reason(
    snapshot: ArchitectureSnapshot, pinned_digest: object
) -> str | None:
    """Return why an architecture snapshot cannot currently be used."""
    if pinned_digest is None:
        return "architecture_policy_unpinned"
    if pinned_digest != snapshot.digest:
        return "architecture_policy_drift"
    if snapshot.untracked:
        return "architecture_contract_untracked"
    return None


def architecture_norm_block_reason(
    pinned_documents: Any, pinned_phases: Any
) -> str | None:
    if not _is_norm_manifest(pinned_documents):
        raise ValueError("meta.json architecture_norm_documents must map document paths to SHA-256 digests")
    if not isinstance(pinned_phases, dict) or any(
        not isinstance(phase_id, str) or not isinstance(hosts, dict) or any(
            not isinstance(host, str) or not _is_norm_manifest(manifest)
            for host, manifest in hosts.items()
        )
        for phase_id, hosts in pinned_phases.items()
    ):
        raise ValueError("meta.json architecture_norm_phases must map phases and hosts to norm manifests")
    if any(
        pinned_documents.get(path) != digest
        for phase_hosts in pinned_phases.values()
        for manifest in phase_hosts.values()
        for path, digest in manifest.items()
    ):
        raise ValueError("phase architecture norms disagree with the run's pinned documents")
    for path, digest in pinned_documents.items():
        content, _ = read_bounded_regular_file(
            Path(path), max_bytes=MAX_ARCHITECTURE_DOCUMENT_BYTES,
        )
        if hashlib.sha256(content).hexdigest() != digest:
            return "architecture_policy_drift"
    return None


def prepare_architecture_selection(
    root: Path, selection: ArchitectureSelection
) -> ArchitectureSnapshot:
    """저장 전에 후보 선택과 모든 규범 파일을 검증한다."""
    with _repository_directory(root) as repository_fd:
        with _open_document(repository_fd, PROJECT_ARCHITECTURE_FILE, allow_missing=True):
            pass
        payload = architecture_selection_document(selection).encode("utf-8")
        source_document = _document_identity(PROJECT_ARCHITECTURE_FILE, payload)
        return _snapshot_from_selection(root, selection, source_document, repository_fd)


def find_project_contract(root: Path) -> str | None:
    """저장소에 있는 프로젝트 계약 후보의 경로. 팀 자리를 먼저 본다.

    개인 자리는 linked worktree에 없으므로, 이 checkout에 아무것도 없을 때만 leader를 본다.
    """
    for relative in CONTRACT_PATHS:
        if _document_present(root, relative):
            return relative
    leader = leader_root_for(root)
    if leader is not None and _document_present(leader, PRIVATE_CONTRACT_PATH):
        return PRIVATE_CONTRACT_PATH
    return None


def _document_present(root: Path, relative: str) -> bool:
    """정확히 그 자리에 무언가 있는가.

    부모가 symlink·파일·없음이면 "없음"이다 — `skills/`가 symlink인 저장소를 계약 보유로
    읽으면 clean도 local도 못 고르는 상태가 된다. 잎은 symlink·디렉터리라도 "있음"이다;
    읽을 수 없는 것이 그 자리에 있는데 Clean을 켜면 어느 규범이 유효한지 아무도 답할 수 없다.
    """
    current = root
    segments = relative.split("/")
    for segment in segments[:-1]:
        current = current / segment
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode) or not current.is_dir():
                return False
        except OSError:
            return False
    try:
        os.lstat(current / segments[-1])
    except OSError:
        return False
    return True


def _snapshot_from_selection(
    root: Path,
    selection: ArchitectureSelection,
    source_document: ContractDocument | None,
    repository_fd: int,
) -> ArchitectureSnapshot:
    """Build a pinned architecture snapshot from a validated selection."""
    if selection.mode is ArchitectureMode.CLEAN:
        # 프로젝트 계약이 있는데 Clean을 켜면 규범이 둘이 된다. 선언이 없는 legacy 기본값도
        # 예외가 아니다 — 파일을 둔 것이 곧 "이 규범을 쓰라"는 뜻이다.
        existing = find_project_contract(root)
        if existing is not None:
            raise ArchitectureContractError(
                f"project architecture contract exists at {existing}; clean architecture "
                f"cannot be selected or installed beside it. Run "
                f"`agent-flow architecture select --mode local --skill {existing}`"
            )
    contract = _resolve_architecture_contract(root, selection, repository_fd)
    # 선언 파일은 gitignore된 자리에 있다. 추적 요구는 계약 문서에만 건다.
    return ArchitectureSnapshot(
        selection=selection,
        declared=source_document is not None,
        contract=contract,
        digest=_snapshot_digest(selection, source_document is not None, contract, source_document),
        source_document=source_document,
        untracked=contract.untracked if contract else (),
    )


def architecture_plan_payload(snapshot: ArchitectureSnapshot) -> dict[str, Any]:
    """설치기가 소비하는 공개 계약.

    JS가 YAML을 다시 파싱해 정책을 재구성하면 해석이 둘이 되고, 설치와 실행이
    다른 규칙으로 갈린다. 정규화는 여기서 끝나고 설치기는 이 모양만 읽는다.
    """
    contract = snapshot.contract
    return {
        "schema_version": SCHEMA_VERSION,
        "declared": snapshot.declared,
        "mode": snapshot.selection.mode.value,
        "contract": snapshot.selection.contract_path,
        "documents": [document.path for document in contract.documents] if contract else [],
        "document_manifest": [asdict(document) for document in contract.documents] if contract else [],
        "source_document": asdict(snapshot.source_document) if snapshot.source_document else None,
        "selection_document": architecture_selection_document(snapshot.selection) if snapshot.declared else None,
        "excluded_skills": sorted(CLEAN_ARCHITECTURE_SKILLS) if snapshot.selection.mode is not ArchitectureMode.CLEAN else [],
        "untracked": list(snapshot.untracked),
        "digest": snapshot.digest,
    }


def write_architecture_selection(root: Path, selection: ArchitectureSelection) -> Path:
    """검증된 선택을 정본 파일에 원자적으로 기록한다.

    호출자는 기록 전에 계약을 해석해 두어야 한다. 검증 없이 먼저 쓰면 다음 run이
    도달할 수 없는 계약을 유효한 선택으로 읽는다.

    선택은 누적이 아니라 교체다. 이전 mode의 잔여 키를 남기면 두 선언이 동시에
    유효해 보인다.
    """
    _validate_document_path(root, PROJECT_ARCHITECTURE_FILE, allow_missing=True)
    path = root / PROJECT_ARCHITECTURE_FILE
    path.parent.mkdir(exist_ok=True)
    try:
        atomic_write_text(path, architecture_selection_document(selection))
    except OSError as exc:
        raise ValueError(f"{PROJECT_ARCHITECTURE_FILE}: cannot write: {exc}") from exc
    return path


def architecture_selection_document(selection: ArchitectureSelection) -> str:
    """Serialize an architecture selection to its canonical project document."""
    lines = [f"schema_version: {SCHEMA_VERSION}", "architecture:", f"  mode: {selection.mode.value}"]
    if selection.contract_path is not None:
        lines.append(f"  skill: {selection.contract_path}")
    return "\n".join(lines) + "\n"


CLEAN_ARCHITECTURE_SKILLS = frozenset({
    "clean-architecture",
    "clean-architecture-core",
    "android-clean-architecture",
    "android-clean-presentation-architecture",
    "flutter-clean-architecture",
    "flutter-clean-presentation-architecture",
    "ios-clean-architecture",
    "ios-clean-presentation-architecture",
    "python-api-clean-architecture",
    "react-clean-architecture",
    "react-clean-presentation-architecture",
    "react-native-clean-architecture",
    "react-native-clean-presentation-architecture",
})


def is_clean_architecture_skill(name: str) -> bool:
    """Return whether a skill belongs to the Clean architecture contract."""
    return name in CLEAN_ARCHITECTURE_SKILLS


def contract_skill_name(selection: ArchitectureSelection) -> str | None:
    """local 계약 문서의 skill 이름. 다른 모드에는 프로젝트 계약이 없다."""
    if selection.mode is not ArchitectureMode.LOCAL or selection.contract_path is None:
        return None
    return selection.contract_path.split("/")[-2]


def contract_names_in(required_names: Sequence[str], selection: ArchitectureSelection) -> tuple[str, ...]:
    """required 중 이 선택이 구조 계약으로 인정하는 이름."""
    if selection.mode is ArchitectureMode.CLEAN:
        return tuple(name for name in required_names if is_clean_architecture_skill(name))
    contract = contract_skill_name(selection)
    if contract is None:
        return ()
    return tuple(name for name in required_names if name == contract)


def clean_role_lint_applies(snapshot: ArchitectureSnapshot) -> bool:
    """Clean role 토폴로지 lint를 이 프로젝트에 적용하는가.

    local 프로젝트에 Clean 경로 규칙을 강요하면 선택이 의미를 잃는다. 선언이 없는
    기존 설치는 종전 동작을 유지한다.
    """
    return snapshot.selection.mode is ArchitectureMode.CLEAN


def evaluate_workflow_compatibility(
    selection: ArchitectureSelection, *, architecture_decision: str
) -> str | None:
    """이 phase를 현재 선택으로 끝낼 수 있는가. 없으면 사유를 준다.

    pending은 면제가 아니다. 구조 결정을 요구하는 단계는 판정할 기준이 없으므로,
    빼거나 통과시키지 않고 결정될 때까지 그 단계에서 대기한다.
    """
    if selection.mode is ArchitectureMode.PENDING and architecture_decision == "required":
        return "architecture_decision_pending"
    return None


def _snapshot_digest(
    selection: ArchitectureSelection,
    declared: bool,
    contract: ArchitectureContract | None,
    source_document: ContractDocument | None,
) -> str:
    """Compute the digest binding a selection to its contract documents."""
    lines = [
        f"schema_version={SCHEMA_VERSION}",
        f"declared={'yes' if declared else 'no'}",
        f"mode={selection.mode.value}",
        f"contract={selection.contract_path or '-'}",
    ]
    if source_document is not None:
        lines.append(f"source={source_document.path}\0{source_document.sha256}\0{source_document.bytes}")
    for document in contract.documents if contract else ():
        lines.append(f"document={document.path}\0{document.sha256}\0{document.bytes}")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _validate_document_path(
    root: Path, relative: str, *, allow_missing: bool = False
) -> Path:
    """Validate a normative document path relative to the contract root."""
    with _repository_directory(root) as repository_fd:
        with _open_document(repository_fd, relative, allow_missing=allow_missing):
            return root / relative


@contextmanager
def _repository_directory(root: Path) -> Iterator[int]:
    """Open the repository directory used for descriptor-relative reads."""
    if not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK")):
        raise ArchitectureContractError("architecture documents require no-follow directory opening")
    try:
        checkout = root.resolve(strict=True)
        descriptor = os.open(checkout, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise ArchitectureContractError(f"architecture repository is not a directory: {root}")
            yield descriptor
        finally:
            os.close(descriptor)
    except (OSError, RuntimeError) as exc:
        raise ArchitectureContractError(f"architecture repository cannot be accessed: {root}: {exc}") from exc




@contextmanager
def _open_document(
    repository_fd: int, relative: str, *, allow_missing: bool = False
) -> Iterator[int | None]:
    """Open a contract document without following symlinks."""
    segments = relative.split("/")
    if any(part in {"", ".", ".."} for part in segments) or "\\" in relative:
        raise ArchitectureContractError(f"architecture document path is not canonical: {relative!r}")
    try:
        with ExitStack() as opened:
            parent_fd = repository_fd
            for segment in segments[:-1]:
                try:
                    parent_fd = os.open(
                        segment, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
                    )
                except FileNotFoundError:
                    # `.agent-flow/`가 아직 없는 미설치 checkout. 선언 없음과 같다.
                    if not allow_missing:
                        raise
                    yield None
                    return
                opened.callback(os.close, parent_fd)
                if not stat.S_ISDIR(os.fstat(parent_fd).st_mode):
                    raise ArchitectureContractError(f"architecture document parent is not a directory: {relative}")
                try:
                    os.stat(".git", dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise ArchitectureContractError(
                        f"architecture document must not use a submodule or nested repository: {relative}"
                    )
            try:
                descriptor = os.open(
                    segments[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd
                )
            except FileNotFoundError:
                if not allow_missing:
                    raise
                yield None
                return
            opened.callback(os.close, descriptor)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ArchitectureContractError(f"architecture document must be a regular file: {relative}")
            yield descriptor
    except OSError as exc:
        raise ArchitectureContractError(
            f"architecture document cannot be accessed without symlinks: {relative}: {exc}"
        ) from exc


def _document_identity(relative: str, payload: bytes) -> ContractDocument:
    """Return the stable filesystem identity of an opened document."""
    return ContractDocument(
        path=relative,
        sha256=hashlib.sha256(payload).hexdigest(),
        bytes=len(payload),
    )


def _read_document_bytes(descriptor: int, relative: str) -> bytes:
    """Read an opened contract document within the configured size limit."""
    identity = os.fstat(descriptor)
    if not stat.S_ISREG(identity.st_mode):
        raise ArchitectureContractError(f"architecture document must be a regular file: {relative}")
    if identity.st_size > MAX_ARCHITECTURE_DOCUMENT_BYTES:
        raise ArchitectureContractError(f"architecture document is too large: {relative}")
    chunks: list[bytes] = []
    total = 0
    while chunk := os.read(descriptor, min(64 * 1024, MAX_ARCHITECTURE_DOCUMENT_BYTES - total + 1)):
        total += len(chunk)
        if total > MAX_ARCHITECTURE_DOCUMENT_BYTES:
            raise ArchitectureContractError(f"architecture document is too large: {relative}")
        chunks.append(chunk)
    return b"".join(chunks)


def _read_contract_document(
    repository_fd: int, relative: str, *, kind: str
) -> tuple[ContractDocument, bytes]:
    """Read and pin one validated architecture contract document."""
    with _open_document(repository_fd, relative) as descriptor:
        assert descriptor is not None
        payload = _read_document_bytes(descriptor, relative)
    return _validate_contract_document(relative, payload, kind=kind)


def _validate_contract_document(
    relative: str, payload: bytes, *, kind: str
) -> tuple[ContractDocument, bytes]:
    """Validate a contract document's path, metadata, and content."""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArchitectureContractError(
            f"architecture {kind} cannot be read as UTF-8: {relative}: {exc}"
        ) from exc
    if not text.lstrip("\ufeff").strip():
        raise ArchitectureContractError(f"architecture {kind} is empty: {relative}")
    if kind != "selection":
        try:
            _, body = split_frontmatter(text, source=relative)
        except SkillMetadataError as exc:
            raise ArchitectureContractError(str(exc)) from exc
        if not body.strip():
            raise ArchitectureContractError(f"architecture {kind} has no normative body: {relative}")
    return _document_identity(relative, payload), payload




def _declared_references(contract_bytes: bytes, *, source: str) -> tuple[str, ...]:
    """계약 root가 `requires_docs`로 선언한 필수 참조.

    본문의 링크는 승격하지 않는다. 참고 자료가 자동으로 필수 규범이 되면 전달
    범위가 문서를 고칠 때마다 조용히 넓어진다.

    디코딩 실패를 빈 목록으로 접으면 필수 참조가 통째로 사라지고, 사라진 문서는
    digest에도 없어 이후 변경이 drift로도 잡히지 않는다.
    """
    try:
        text = contract_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArchitectureContractError(f"{source}: must be UTF-8: {exc}") from exc
    try:
        frontmatter = parse_skill_metadata(text, source=source, strict_duplicates=True)
    except SkillMetadataError as exc:
        raise ArchitectureContractError(f"{source}: invalid frontmatter: {exc}") from exc
    return tuple(
        entry["path"] if isinstance(entry, dict) else entry
        for entry in frontmatter.get("requires_docs", ())
    ) if frontmatter is not None else ()




def _untracked_paths(root: Path, relatives: list[str]) -> tuple[str, ...]:
    """규범 문서 중 git이 추적하지 않는 것.

    추적되지 않은 규범은 동료·리뷰어·새 clone에 도달하지 않는다. 여기서는 사실만
    보고하고, 그것으로 무엇을 막을지는 run 경계가 정한다.
    """
    if not relatives:
        return ()
    result = git_safe("ls-files", "--stage", "-z", "--", *relatives, cwd=root, optional_locks=False)
    if not result.ok:
        if git_repo_state(root) == "non-repo":
            return ()
        raise ArchitectureContractError(
            f"architecture document tracking cannot be determined: {result.stderr.strip()}"
        )
    tracked: set[str] = set()
    for entry in result.stdout.split("\0"):
        if not entry:
            continue
        metadata, separator, path = entry.partition("\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise ArchitectureContractError("architecture document tracking returned invalid index data")
        mode, _, stage = fields
        if mode not in {"100644", "100755"} or stage != "0":
            raise ArchitectureContractError(f"architecture document must be an unconflicted regular index file: {path}")
        tracked.add(path)
    return tuple(relative for relative in relatives if relative not in tracked)
