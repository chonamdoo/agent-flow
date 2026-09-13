from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import hashlib
from importlib import resources
from pathlib import Path
import re
from typing import Any, Literal

import yaml

from agent_flow.core.markers import normalize_required_markers
from agent_flow.core.security import ensure_child_path, validate_safe_name
from agent_flow.core.skill_resolver import PhaseSkills


@dataclass(frozen=True)
class PhaseDefinition:
    id: str
    description: str
    prompt: str | None
    pause_after: bool
    optional: bool
    multi_review: bool
    routes: dict[str, str] | None
    required_markers: tuple[str, ...]
    artifact: str
    skills: PhaseSkills | None = None
    architecture_decision: str = "existing"


_PHASE_KEYS = frozenset(
    {
        "id",
        "description",
        "prompt",
        "pause_after",
        "optional",
        "multi_review",
        "routes",
        "required_markers",
        "artifact",
        "skills",
        "architecture_decision",
    }
)


@dataclass(frozen=True)
class PhaseWorkflowDefinition:
    id: str
    phases: tuple[PhaseDefinition, ...]
    source: str
    digest: str
    completion_disposition: Literal["local-handoff", "integrated-cleanup"] = "integrated-cleanup"
    source_bytes: bytes = b""
    kit_owned: bool = False

    def to_json_dict(self) -> dict[str, Any]:
        # digest를 빼면 export가 `meta.workflow_digest`와 대조할 수 없다. drift
        # 예외가 지목하는 값이 바로 이것이고, export는 유일한 기계 가독 뷰다.
        return {
            "id": self.id,
            "source": self.source,
            "digest": self.digest,
            "completion_disposition": self.completion_disposition,
            "phases": [asdict(phase) for phase in self.phases],
        }


class CorruptRunCursorError(ValueError):
    """run meta의 phase cursor를 현재 workflow로 해석할 수 없다."""


class WorkflowDriftError(ValueError):
    """run이 시작된 뒤 workflow 정의 자체가 바뀌었다."""


@dataclass(frozen=True)
class CursorScope:
    """커서 검증에 필요한 전부: index로 여는 phase id 순서와 원문 digest.

    정의 dataclass를 그대로 쓰면, 실제로 도는 목록이 정의와 다른 진입은 정의를
    합성해 넘겨야 한다. 그 합성본은 `digest`("원문 바이트의 sha256")를 유지한 채
    phase만 갈아 끼운 위조품이고, drift 검증이 그 위조된 불변식을 기준으로 돈다.
    """

    workflow_id: str
    source: str
    digest: str
    phase_ids: tuple[str, ...]

    @classmethod
    def of(
        cls,
        definition: PhaseWorkflowDefinition,
        phase_ids: Sequence[str] | None = None,
    ) -> CursorScope:
        return cls(
            definition.id,
            definition.source,
            definition.digest,
            tuple(phase_ids)
            if phase_ids is not None
            else tuple(phase.id for phase in definition.phases),
        )


@dataclass(frozen=True)
class RunCursor:
    """run이 어느 phase에 서 있는지에 대한 검증된 값.

    `phase_index == len(phases)`는 마지막 phase를 지난 **완료 커서**다. 그 자리는
    cleanup이 막혔을 때 재개가 다시 지나가는 정당한 상태라 유효 범위에 든다.
    그때 `phase_id`는 ``None``이어야 한다 — 완료 커서에 phase 이름이 남아 있으면
    두 필드가 서로 다른 이야기를 하는 것이고, 그건 손상이다.

    `phase_id`가 `str | None`인 이유: ``None`` 하나가 "meta가 어떤 phase도 지목하지
    않는다"를 뜻하고, 그 안의 세 자리(키 없음·`current_phase: null`·아직 진입 전
    새 run)는 `phase_index`가 이미 구분한다(0이면 진입 전, `len`이면 완료). 반면
    빈 문자열은 "이름이 있는데 비었다"이고 그런 phase는 어떤 workflow도 정의할 수
    없다. 예전 `raw_phase or ""`는 둘을 한 값으로 접어 그 손상을 "이름 없음"으로
    통과시켰다.
    """

    workflow_digest: str
    phase_index: int
    phase_id: str | None

    @classmethod
    def from_meta(
        cls,
        meta: Mapping[str, Any],
        scope: CursorScope,
    ) -> RunCursor:
        raw_index = meta.get("phase_index", 0)
        if raw_index is None:
            raw_index = 0
        # bool은 int의 하위형이라 먼저 걸러야 `True`가 index 1로 통과하지 않는다.
        if isinstance(raw_index, bool) or not isinstance(raw_index, int):
            raise CorruptRunCursorError(
                f"run cursor phase_index must be an integer, got {raw_index!r}"
            )
        raw_phase = meta.get("current_phase")
        if raw_phase is not None and not isinstance(raw_phase, str):
            raise CorruptRunCursorError(
                f"run cursor current_phase must be a string, got {raw_phase!r}"
            )
        if raw_phase == "":
            # 이름 없는 phase는 어떤 workflow도 정의하지 못한다. "이름이 없다"로
            # 흡수하면 index만 남은 채 재개해 앞선 phase를 통째로 건너뛴다.
            raise CorruptRunCursorError(
                "run cursor current_phase is an empty string, which names no phase of "
                f"workflow {scope.workflow_id}. Restore meta.json from backup, or clear "
                f"the run with `agent-flow abort`."
            )
        recorded_digest = meta.get("workflow_digest")
        if recorded_digest is not None and not isinstance(recorded_digest, str):
            raise CorruptRunCursorError(
                f"run cursor workflow_digest must be a string, got {recorded_digest!r}"
            )
        phase_id = raw_phase
        if recorded_digest and recorded_digest != scope.digest:
            raise WorkflowDriftError(
                f"workflow {scope.workflow_id} changed after this run started: run "
                f"recorded {recorded_digest} but {scope.source} now hashes to "
                f"{scope.digest}. Restore the definition it started with, or start "
                "a separate run for the current definition. Existing records and "
                "approvals remain unchanged."
            )
        cursor = cls(scope.digest, raw_index, phase_id)
        cursor.validate(scope)
        return cursor

    def validate(self, scope: CursorScope) -> None:
        total = len(scope.phase_ids)
        if not 0 <= self.phase_index <= total:
            raise CorruptRunCursorError(
                f"run cursor phase_index {self.phase_index} is outside workflow "
                f"{scope.workflow_id} (0..{total})"
            )
        if self.phase_index == total:
            if self.phase_id is not None:
                raise CorruptRunCursorError(
                    f"run cursor is past the last phase of workflow "
                    f"{scope.workflow_id} but still names phase {self.phase_id!r}"
                )
            return
        expected = scope.phase_ids[self.phase_index]
        if self.phase_id is None:
            # index 0은 아직 어떤 phase도 찍지 않은 새 run이라 이름이 없는 게 정상이다.
            # 그 밖에서 이름이 없으면 남은 근거가 숫자뿐이고, 숫자만 믿고 재개하면
            # 앞선 필수 phase를 통째로 건너뛴다.
            if self.phase_index == 0:
                return
            raise CorruptRunCursorError(
                f"run cursor phase_index {self.phase_index} claims phase {expected!r} of "
                f"workflow {scope.workflow_id} but meta records no current_phase; "
                f"resuming on the number alone would skip every phase before it. "
                f"Restore meta.json from backup, or clear the run with `agent-flow abort`."
            )
        if self.phase_id != expected:
            raise CorruptRunCursorError(
                f"run cursor phase_index {self.phase_index} names phase {expected!r} in "
                f"workflow {scope.workflow_id} but meta records {self.phase_id!r}"
            )


@dataclass(frozen=True)
class DeclaredPhaseSkills:
    """workflow가 이름으로 선언한 skill과, 읽지 못한 workflow의 사유.

    수집을 조용히 비우면 doctor가 정상 선언된 skill을 미라우팅으로 오탐한다. 그래서
    부분 실패를 값으로 들고 나가고, 그것을 어떻게 알릴지는 호출자가 정한다.
    """

    names: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


def find_kit_root(start: Path | None = None) -> Path:
    """Locate the agent-flow kit root.

    kit을 **자산 배치**로 알아보면 배치가 곧 정의가 된다. 예전 술어는
    `workflows/`와 `profiles/`를 둘 다 가진 조상이었고, 그래서 그 두 디렉터리를
    한 벌로 줄이는 순간 탐지가 함께 깨졌다. 대신 kit 고유 서명을 본다.

    `pyproject.toml`이나 `package.json` 하나만으로는 부족하다 — Python과 Node를
    함께 쓰는 평범한 사용자 프로젝트가 전부 후보가 되고, 그러면 남의 워크플로를
    돌린다. 설치된 패키지 트리에는 그 서명이 없으므로 패키지 디렉터리로 떨어진다.
    """
    here = (start or Path(__file__)).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() and (
            parent / "bin" / "agent-flow-kit.mjs"
        ).is_file():
            return parent
    package_dir = package_root()
    if package_dir is not None:
        return package_dir
    raise RuntimeError("Could not locate agent-flow kit root from " + str(here))


def package_root() -> Path | None:
    """설치된 `agent_flow` 패키지 디렉터리. 워크플로 정의의 정본이 사는 자리다."""
    try:
        root = resources.files("agent_flow")
    except (ImportError, ModuleNotFoundError, TypeError):
        return None
    try:
        return Path(str(root))
    except TypeError:
        return None


def _packaged_workflow_path(name: str) -> Path | None:
    package_dir = package_root()
    if package_dir is None:
        return None
    path = package_dir / "workflows" / f"{name}.yaml"
    ensure_child_path(package_dir / "workflows", path, "workflow")
    return path if path.is_file() else None


def workflow_names(kit_root: Path) -> tuple[str, ...]:
    """읽을 수 있는 workflow 이름 전부. 정의가 어디 사는지 아는 곳은 이 모듈뿐이다."""
    directories = [kit_root / "workflows"]
    package_dir = package_root()
    if package_dir is not None:
        directories.append(package_dir / "workflows")
    names: set[str] = set()
    for directory in directories:
        if not directory.is_dir():
            continue
        names.update(path.stem for path in directory.glob("*.yaml") if not path.stem.startswith("_"))
    return tuple(sorted(names))


def declared_phase_skills(kit_root: Path) -> DeclaredPhaseSkills:
    """모든 workflow의 phase가 required·optional로 선언한 skill 이름."""
    names: list[str] = []
    errors: list[str] = []
    for name in workflow_names(kit_root):
        try:
            definition = load_phase_workflow_definition(kit_root, name)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            errors.append(f"workflow {name}: {exc}")
            continue
        for phase in definition.phases:
            if phase.skills is None:
                continue
            names.extend(phase.skills.required)
            names.extend(phase.skills.optional)
    return DeclaredPhaseSkills(tuple(dict.fromkeys(names)), tuple(errors))


def load_phase_workflow_definition(
    kit_root: Path, name: str, *, expected_digest: str | None = None
) -> PhaseWorkflowDefinition:
    """Load and validate a named workflow definition."""
    validate_safe_name(name, "workflow")
    path = kit_root / "workflows" / f"{name}.yaml"
    ensure_child_path(kit_root / "workflows", path, "workflow")
    packaged = _packaged_workflow_path(name)
    if not path.exists():
        # 정의의 정본은 설치 가능한 패키지 자원이다. kit root 사본은 설치본이
        # 덮어쓸 수 있는 자리라 먼저 보지만, 없다고 실패하면 그 사본을 지울 수 없다.
        if packaged is None:
            raise FileNotFoundError(f"Workflow not found: {path}")
        path = packaged
    source_bytes = path.read_bytes()
    digest = hashlib.sha256(source_bytes).hexdigest()
    if expected_digest is not None and digest != expected_digest:
        raise WorkflowDriftError(
            f"workflow {name}: recorded definition {expected_digest} is unavailable; "
            f"{path} hashes to {digest}. Restore the original definition or start "
            "a new run. Existing approvals cannot be reused for changed definitions."
        )
    return parse_phase_workflow_definition(
        source_bytes,
        source=path,
        name=name,
        kit_owned=packaged is not None
        and (path.resolve() == packaged.resolve() or source_bytes == packaged.read_bytes()),
        pinned_legacy=expected_digest is not None,
    )


def parse_phase_workflow_definition(
    source_bytes: bytes,
    *,
    source: Path,
    name: str,
    kit_owned: bool = False,
    pinned_legacy: bool = False,
) -> PhaseWorkflowDefinition:
    path = source
    digest = hashlib.sha256(source_bytes).hexdigest()
    raw = yaml.safe_load(source_bytes.decode("utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"workflow {path}: top-level must be a mapping")
    workflow_id = raw.get("id", name)
    if not isinstance(workflow_id, str) or not workflow_id:
        raise ValueError(f"workflow {path}: id must be a non-empty string")
    completion_disposition = raw.get("completion_disposition", "integrated-cleanup")
    if completion_disposition not in ("local-handoff", "integrated-cleanup"):
        raise ValueError(
            f"workflow {path}: completion_disposition must be local-handoff or integrated-cleanup"
        )
    phases_raw = raw.get("phases") or []
    if not isinstance(phases_raw, list) or not phases_raw:
        raise ValueError(f"workflow {path}: missing or empty `phases`")
    phases = _normalize_phases(
        phases_raw,
        path,
        workflow_id,
        replaceable_architecture=kit_owned,
        pinned_legacy=pinned_legacy,
    )
    _validate_routes(phases, path)
    return PhaseWorkflowDefinition(
        id=workflow_id,
        phases=tuple(phases),
        source=str(path),
        digest=digest,
        completion_disposition=completion_disposition,
        source_bytes=source_bytes,
        kit_owned=kit_owned,
    )


def _normalize_phases(
    phases_raw: list[object],
    path: Path,
    workflow_id: str,
    *,
    replaceable_architecture: bool = False,
    pinned_legacy: bool = False,
) -> list[PhaseDefinition]:
    """Parse and validate workflow phase definitions."""
    out: list[PhaseDefinition] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(phases_raw):
        if not isinstance(item, dict) or "id" not in item:
            raise ValueError(f"workflow {path}: phase {index} missing `id` (got {item!r})")
        phase_id = _string_field(item, "id", path, index)
        validate_safe_name(phase_id, "workflow phase id")
        non_string_keys = [key for key in item if not isinstance(key, str)]
        if non_string_keys:
            raise ValueError(
                f"workflow {path}: phase {phase_id} has non-string key(s): "
                + ", ".join(repr(key) for key in non_string_keys)
            )
        unknown = sorted(set(item) - _PHASE_KEYS)
        if unknown:
            raise ValueError(
                f"workflow {path}: phase {phase_id} has unknown key(s): "
                + ", ".join(unknown)
                + ". Remove unsupported keys; if this is an installed copy, "
                "run `agent-flow-kit install` from the leader checkout."
            )
        if phase_id in seen_ids:
            raise ValueError(
                f"workflow {path}: duplicate phase id {phase_id!r} at index {index}. "
                "Each phase id must be unique."
            )
        seen_ids.add(phase_id)
        routes = _routes(item.get("routes"), path, phase_id)
        architecture_decision = item.get("architecture_decision", "existing")
        if architecture_decision not in ("existing", "required"):
            raise ValueError(
                f"workflow {path}: phase {phase_id} architecture_decision "
                "must be existing or required"
            )
        out.append(
            PhaseDefinition(
                id=phase_id,
                description=_optional_string(item.get("description"), ""),
                prompt=_optional_string_or_none(item.get("prompt")),
                pause_after=_bool_field(item.get("pause_after", False), path, phase_id, "pause_after"),
                optional=_bool_field(item.get("optional", False), path, phase_id, "optional"),
                multi_review=_bool_field(item.get("multi_review", False), path, phase_id, "multi_review"),
                routes=routes,
                required_markers=normalize_required_markers(item.get("required_markers")),
                artifact=_artifact_field(
                    item.get("artifact"),
                    path,
                    phase_id,
                    _default_artifact_for_phase(workflow_id, phase_id),
                ),
                skills=_phase_skills(
                    item.get("skills"),
                    path,
                    phase_id,
                    replaceable_architecture=replaceable_architecture,
                    pinned_legacy=pinned_legacy,
                ),
                architecture_decision=architecture_decision,
            )
        )
    return out


def _phase_skills(
    value: object,
    path: Path,
    phase_id: str,
    *,
    replaceable_architecture: bool = False,
    pinned_legacy: bool = False,
) -> PhaseSkills | None:
    """Return the skills declared for a workflow phase."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"workflow {path}: phase {phase_id} `skills` must be a mapping")
    unknown = set(value) - {"required", "optional"}
    if unknown:
        raise ValueError(
            f"workflow {path}: phase {phase_id} `skills` has unknown keys {sorted(unknown)}"
        )
    skills = PhaseSkills(
        required=_skill_names(value.get("required"), path, phase_id, "required"),
        optional=_skill_names(value.get("optional"), path, phase_id, "optional"),
        replaceable_architecture=replaceable_architecture,
        pinned_legacy=pinned_legacy,
    )
    if "clean-architecture" in skills.required and not pinned_legacy:
        raise ValueError(
            f"workflow {path}: phase {phase_id} requires obsolete skill "
            "'clean-architecture'; migration required: replace the required name "
            "with 'clean-architecture-core'. Installed aliases do not authorize "
            "this declaration. Existing runs must use their pinned definition."
        )
    return None if skills.is_empty() else skills


def _skill_names(value: object, path: Path, phase_id: str, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"workflow {path}: phase {phase_id} `skills.{field}` must be a list")
    names: list[str] = []
    for item in value:
        name = str(item)
        validate_safe_name(name, f"phase {phase_id} skills.{field}")
        names.append(name)
    return tuple(dict.fromkeys(names))


def _validate_routes(phases: list[PhaseDefinition], path: Path) -> None:
    phase_ids = {phase.id for phase in phases}
    for phase in phases:
        if not phase.routes:
            continue
        for key, target in phase.routes.items():
            if target == "block":
                continue
            if target not in phase_ids:
                raise ValueError(f"workflow {path}: phase {phase.id} route {key!r} targets unknown phase {target!r}")


def _string_field(item: dict[str, object], field: str, path: Path, index: int) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"workflow {path}: phase {index} `{field}` must be a non-empty string")
    return value


def _optional_string(value: object, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError("workflow phase string field must be a string")
    return value


# `C:` 같은 드라이브 접두사. POSIX `PurePath`는 이것을 절대 경로로 보지 않아
# lexical 봉쇄를 그냥 통과한다.
_ARTIFACT_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


def _artifact_field(value: object, path: Path, phase_id: str, default: str) -> str:
    """`artifact`는 run 디렉터리 기준 상대 경로다. **compile 시점에** 봉쇄한다.

    형제 필드는 이미 통제된다 — phase/skill 이름은 `validate_safe_name`, workflow
    파일 자체는 `ensure_child_path`. artifact만 자유 문자열이라 `artifact: ../../x`가
    loader를 통과했고, 그 값은 `run_dir / phase.artifact`로 그대로 쓰였다. 잘못된
    정의는 run을 시작하기 전에 죽어야 한다. 중첩 상대경로(`artifacts/gate-results.json`)
    는 정당하므로 계속 통과한다.

    기본값도 같은 검사를 거친다 — 기본값은 `phase_id`에서 만들어지고 이 loader는
    phase id를 safe name으로 제한하지 않으므로, 기본값 경로도 같은 탈출로다.
    """
    candidate = default if value is None else value
    if not isinstance(candidate, str):
        raise ValueError(f"workflow {path}: phase {phase_id} `artifact` must be a string")
    reason = _artifact_reject_reason(candidate)
    if reason is not None:
        raise ValueError(
            f"workflow {path}: phase {phase_id} `artifact` {reason}: {candidate!r}"
        )
    return candidate


def _artifact_reject_reason(value: str) -> str | None:
    if value != value.strip():
        return "must not be padded with whitespace"
    if "\x00" in value:
        return "must not contain a NUL byte"
    # `\`는 POSIX에서 평범한 파일명 문자지만 Windows에서는 구분자다. 한쪽에서만
    # 상대 경로인 값은 플랫폼에 따라 다른 자리를 가리킨다.
    if "\\" in value:
        return "must use `/` as its only path separator"
    if _ARTIFACT_DRIVE_PREFIX.match(value):
        return "must not start with a drive letter"
    # 빈 값, 절대 경로(`/x` -> 첫 조각이 빈 문자열), `//`, 디렉터리를 가리키는
    # 뒤 슬래시, `.`/`..` 탈출을 한 번에 거른다.
    if any(segment in ("", ".", "..") for segment in value.split("/")):
        return "must be a non-empty relative path with no empty, `.`, or `..` segment"
    return None


def _optional_string_or_none(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("workflow phase prompt must be a string")
    return value


def _bool_field(value: object, path: Path, phase_id: str, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"workflow {path}: phase {phase_id} `{field}` must be boolean")
    return value


def _routes(value: object, path: Path, phase_id: str) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"workflow {path}: phase {phase_id} `routes` must be a mapping")
    routes: dict[str, str] = {}
    for key, target in value.items():
        if not isinstance(key, str) or not isinstance(target, str):
            raise ValueError(f"workflow {path}: phase {phase_id} routes must map strings to strings")
        routes[key] = target
    return routes


def _default_artifact_for_phase(workflow_id: str, phase_id: str) -> str:
    if workflow_id != "full-feature":
        return f"{phase_id}.md"
    if phase_id == "red":
        return "artifacts/red.log"
    if phase_id == "green":
        return "artifacts/green.log"
    if phase_id == "gates":
        return "artifacts/gate-results.json"
    return f"artifacts/{phase_id}.md"
