from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import hashlib
from importlib import resources
from pathlib import Path
import re
from typing import Any

import yaml

from agent_flow.core.markers import normalize_required_markers, unfenced_markdown_text
from agent_flow.core.security import ensure_child_path, validate_safe_name
from agent_flow.core.skill_resolver import PhaseSkills

# drift 탈출구의 이름. 예외 메시지가 이 문자열을 지목하므로 flag를 세우는 CLI와
# 같은 상수를 봐야 안내와 실제 명령이 갈리지 않는다.
ACCEPT_WORKFLOW_DRIFT_FLAG = "--accept-workflow-drift"


@dataclass(frozen=True)
class PhaseDefinition:
    id: str
    description: str
    prompt: str | None
    pause_after: bool
    optional: bool
    multi_review: bool
    cite_lore: bool
    routes: dict[str, str] | None
    required_markers: tuple[str, ...]
    artifact: str
    skills: PhaseSkills | None = None


@dataclass(frozen=True)
class PhaseWorkflowDefinition:
    id: str
    phases: tuple[PhaseDefinition, ...]
    source: str
    digest: str

    def to_json_dict(self) -> dict[str, Any]:
        # digest를 빼면 export가 `meta.workflow_digest`와 대조할 수 없다. drift
        # 예외가 지목하는 값이 바로 이것이고, export는 유일한 기계 가독 뷰다.
        return {
            "id": self.id,
            "source": self.source,
            "digest": self.digest,
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
    # 승인된 drift가 이름으로 자리를 다시 잡았을 때 그 **옛** index. 옮기지 않았으면
    # ``None``. 호출자가 "기록된 index != 커서 index"로 재배치를 추론하면 안 되기
    # 때문에 사실을 값으로 들려 보낸다 — 그 비교는 digest가 어긋난 안에서만 참이 될
    # 수 있어서 재배치의 결정적 근거가 못 된다.
    reanchored_from: int | None = None

    @classmethod
    def from_meta(
        cls,
        meta: Mapping[str, Any],
        scope: CursorScope,
        *,
        accept_workflow_drift: bool = False,
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
        reanchored_from: int | None = None
        if recorded_digest and recorded_digest != scope.digest:
            if not accept_workflow_drift:
                # workflow YAML은 kit이 배포한다. 업그레이드 한 번이 모든 프로젝트의
                # 진행 중인 run을 막으므로 탈출구를 지목한다. "finish"는 이 예외가
                # 막는 바로 그것이라 안내가 될 수 없다.
                raise WorkflowDriftError(
                    f"workflow {scope.workflow_id} changed after this run started: run "
                    f"recorded {recorded_digest} but {scope.source} now hashes to "
                    f"{scope.digest}. Re-baseline this run to the current definition with "
                    f"`agent-flow continue {ACCEPT_WORKFLOW_DRIFT_FLAG}`, restore the "
                    f"definition it started with, or abort the run."
                )
            # 승인된 drift에서 index는 더 이상 기준이 아니다. 새 정의가 현재 phase
            # 앞에 phase를 끼워 넣거나 순서를 바꿨으면 옛 index는 다른 phase를
            # 가리키고, 그대로 대조하면 우리가 안내한 그 명령이
            # `CorruptRunCursorError`로 죽는다. 이름이 정본이므로 이름으로 자리를
            # 다시 잡는다.
            anchored = _reanchor_index(scope, raw_index, phase_id)
            if anchored != raw_index:
                reanchored_from, raw_index = raw_index, anchored
        # digest 기록이 **없는** 예전 run은 drift가 아니다. 형식이 없던 시절의
        # run을 drift로 보고하면 진행 중인 run이 근거 없이 막힌다. 호출자가 이
        # 값으로 meta를 채워 넣는다.
        cursor = cls(scope.digest, raw_index, phase_id, reanchored_from)
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


def _reanchor_index(scope: CursorScope, phase_index: int, phase_id: str | None) -> int:
    """승인된 drift에서 기록된 phase 이름이 새 정의에서 앉는 자리.

    이름이 없으면(새 run·완료 커서) 옮길 근거가 없으므로 기록된 index를 그대로
    돌려주고 판정은 `validate`에 맡긴다.
    """
    if phase_id is None:
        return phase_index
    try:
        return scope.phase_ids.index(phase_id)
    except ValueError:
        # 재배치할 자리가 없다. 여기서 drift 승인을 다시 권하면 사용자는 방금
        # 실행해 실패한 명령을 또 실행하게 된다.
        raise CorruptRunCursorError(
            f"run cursor names phase {phase_id!r}, which workflow "
            f"{scope.workflow_id} no longer defines ({scope.source}); accepting the "
            f"drift cannot place this run. Restore the definition it started with, or "
            f"clear the run with `agent-flow abort`."
        ) from None


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


def load_phase_workflow_definition(kit_root: Path, name: str) -> PhaseWorkflowDefinition:
    validate_safe_name(name, "workflow")
    path = kit_root / "workflows" / f"{name}.yaml"
    ensure_child_path(kit_root / "workflows", path, "workflow")
    if not path.exists():
        # 정의의 정본은 설치 가능한 패키지 자원이다. kit root 사본은 설치본이
        # 덮어쓸 수 있는 자리라 먼저 보지만, 없다고 실패하면 그 사본을 지울 수 없다.
        packaged = _packaged_workflow_path(name)
        if packaged is None:
            raise FileNotFoundError(f"Workflow not found: {path}")
        path = packaged
    source_bytes = path.read_bytes()
    # 파싱 결과가 아니라 **원문 바이트**를 해싱한다. prompt 문구나 순서만 바뀐 편집도
    # 이 run이 실행하기로 한 정의가 바뀐 것이고, 정규화된 구조만 해싱하면 그 변경이
    # 같은 값으로 접혀 drift 검출이 조용히 뚫린다.
    digest = hashlib.sha256(source_bytes).hexdigest()
    raw = yaml.safe_load(source_bytes.decode("utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"workflow {path}: top-level must be a mapping")
    workflow_id = raw.get("id", name)
    if not isinstance(workflow_id, str) or not workflow_id:
        raise ValueError(f"workflow {path}: id must be a non-empty string")
    phases_raw = raw.get("phases") or []
    if not isinstance(phases_raw, list) or not phases_raw:
        raise ValueError(f"workflow {path}: missing or empty `phases`")
    phases = _normalize_phases(phases_raw, path, workflow_id)
    _validate_routes(phases, path)
    return PhaseWorkflowDefinition(
        id=workflow_id, phases=tuple(phases), source=str(path), digest=digest
    )


def _normalize_phases(phases_raw: list[object], path: Path, workflow_id: str) -> list[PhaseDefinition]:
    out: list[PhaseDefinition] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(phases_raw):
        if not isinstance(item, dict) or "id" not in item:
            raise ValueError(f"workflow {path}: phase {index} missing `id` (got {item!r})")
        phase_id = _string_field(item, "id", path, index)
        if phase_id in seen_ids:
            raise ValueError(
                f"workflow {path}: duplicate phase id {phase_id!r} at index {index}. "
                "Each phase id must be unique."
            )
        seen_ids.add(phase_id)
        routes = _routes(item.get("routes"), path, phase_id)
        out.append(
            PhaseDefinition(
                id=phase_id,
                description=_optional_string(item.get("description"), ""),
                prompt=_optional_string_or_none(item.get("prompt")),
                pause_after=_bool_field(item.get("pause_after", False), path, phase_id, "pause_after"),
                optional=_bool_field(item.get("optional", False), path, phase_id, "optional"),
                multi_review=_bool_field(item.get("multi_review", False), path, phase_id, "multi_review"),
                cite_lore=_bool_field(item.get("cite_lore", False), path, phase_id, "cite_lore"),
                routes=routes,
                required_markers=normalize_required_markers(item.get("required_markers")),
                artifact=_optional_string(item.get("artifact"), _default_artifact_for_phase(workflow_id, phase_id)),
                skills=_phase_skills(item.get("skills"), path, phase_id),
            )
        )
    return out


def _phase_skills(value: object, path: Path, phase_id: str) -> PhaseSkills | None:
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


def _optional_string_or_none(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("workflow phase prompt must be a string")
    return value


def overall_review_route_key(text: str) -> str:
    in_overall_section = False
    verdicts: list[str] = []
    overall_sections = 0
    for line in unfenced_markdown_text(text).splitlines():
        stripped = line.strip()
        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped, re.IGNORECASE)
        if heading:
            in_overall_section = bool(
                heading.group(1) == "##"
                and re.fullmatch(
                    r"(?:overall|final)(?:\s+verdict)?",
                    heading.group(2).strip(),
                    re.IGNORECASE,
                )
            )
            if in_overall_section:
                overall_sections += 1
            continue
        if not in_overall_section:
            continue
        match = re.fullmatch(
            r"verdict:\s*(approve|request-changes)",
            stripped,
            re.IGNORECASE,
        )
        if match:
            verdicts.append(match.group(1).lower())
    if overall_sections > 1 or len(verdicts) > 1:
        return "invalid-verdict"
    return verdicts[0] if verdicts else "default"



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
