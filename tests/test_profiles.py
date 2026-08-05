"""profile gate의 phase 선언이 실제로 소비되는지 고정한다.

`profiles/*.yaml`은 게이트마다 pre-commit/pre-push를 선언한다. 파서나 필터 중
한쪽이라도 빠지면 그 선언은 죽은 설정이 되고, pre-commit 자리인 gates phase가
pre-push 게이트까지 돌린다(issue #130).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

KIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT_ROOT / "src"))

from agent_flow.cli import _profile_gate_commands  # noqa: E402
from agent_flow.core.architecture_lint import (  # noqa: E402
    lint_project,
    main as architecture_lint_main,
    profile_lint_context,
)
from agent_flow.core.local_skills import resolved_profile  # noqa: E402
from agent_flow.core.profiles import (  # noqa: E402
    DEFAULT_GATE_PHASE,
    GATE_PHASE_ALL,
    GATE_PHASES,
    ProfileGate,
    _gate_from_payload,
    load_profile,
    load_profile_payload,
)
from agent_flow.core.worktree_isolation import LEADER_SWEEP_SCOPES  # noqa: E402
from agent_flow.runner import _load_profile as load_runner_profile  # noqa: E402

PROFILES_DIR = KIT_ROOT / "src" / "agent_flow" / "profiles"


def _schema_gate_phases() -> list[str]:
    schema = yaml.safe_load((PROFILES_DIR / "_schema.yaml").read_text(encoding="utf-8"))
    return [value.strip() for value in schema["optional"]["gates"][0]["phase"].split("|")]


def _schema_leader_tripwire_scopes() -> list[str]:
    schema = yaml.safe_load((PROFILES_DIR / "_schema.yaml").read_text(encoding="utf-8"))
    return [value.strip() for value in schema["optional"]["branching"]["leader_tripwire"].split("|")]


def _profile_ids() -> list[str]:
    return sorted(path.stem for path in PROFILES_DIR.glob("*.yaml") if not path.stem.startswith("_"))


def _gate(**overrides: object) -> ProfileGate:
    return _gate_from_payload(
        {"id": "probe", "command": ["true"], **overrides},
        profile_id="probe-profile",
    )


def test_schema_declared_phases_are_the_consumed_phases():
    assert _schema_gate_phases() == list(GATE_PHASES)


def test_schema_declared_leader_tripwire_scopes_are_the_consumed_scopes():
    """불변: schema가 적은 값 목록이 코드가 받는 목록이다.

    반증: 값이 세 번째 사본으로 갈리면 schema를 읽고 쓴 선언이 런타임에서 거부된다.
    `_schema_gate_phases`와 같은 대조다.
    """
    assert _schema_leader_tripwire_scopes() == list(LEADER_SWEEP_SCOPES)


@pytest.mark.parametrize("phase", _schema_gate_phases())
def test_schema_declared_phase_survives_parsing(phase):
    assert _gate(phase=phase).phase == phase


@pytest.mark.parametrize("profile_id", _profile_ids())
def test_shipped_profile_gate_phases_round_trip(profile_id):
    payload = yaml.safe_load((PROFILES_DIR / f"{profile_id}.yaml").read_text(encoding="utf-8"))
    declared = [item.get("phase", DEFAULT_GATE_PHASE) for item in payload.get("gates") or []]

    assert [gate.phase for gate in load_profile(profile_id).gates] == declared


@pytest.mark.parametrize("payload", [{}, {"phase": None}, {"phase": ""}, {"phase": "   "}])
def test_missing_phase_defaults_to_pre_commit(payload):
    assert _gate(**payload).phase == DEFAULT_GATE_PHASE == "pre-commit"


def test_profile_gate_default_phase_matches_the_parser_default():
    assert ProfileGate("probe", ("true",)).phase == DEFAULT_GATE_PHASE


@pytest.mark.parametrize("phase", ["prepush", "pre_push", "PRE-COMMIT", 3, True, ["pre-commit"]])
def test_unknown_phase_is_rejected(phase):
    with pytest.raises(ValueError, match="profile gate phase must be one of"):
        _gate(phase=phase)


def test_default_gate_run_excludes_the_pre_push_test_gate():
    commands = _profile_gate_commands(["python"])

    assert [command.gate_id for command in commands] == ["type", "architecture-lint", "lint"]
    assert not any("pytest" in part for command in commands for part in command.command)


def test_phase_all_runs_the_pre_push_test_gate():
    by_id = {command.gate_id: command.command for command in _profile_gate_commands(["python"], phase=GATE_PHASE_ALL)}

    assert by_id["test"] == (sys.executable, "-m", "pytest", "-q")
    assert "architecture-lint" in by_id


def test_explicit_phase_selects_only_that_phase():
    assert [command.gate_id for command in _profile_gate_commands(["python"], phase="pre-push")] == ["test"]
    assert _profile_gate_commands(["python"], phase="post-merge") == []


def test_phase_filter_applies_across_a_profile_union():
    union = [command.gate_id for command in _profile_gate_commands(["android", "react-native"])]

    assert "android:build" not in union
    assert "react-native:android-build" not in union
    assert "react-native:ios-build" not in union
    assert "react-native:test" not in union
    assert "android:test" in union


CANONICAL_BOOTSTRAP_TEMPLATE = KIT_ROOT / "bootstrap" / "AGENTS.md.template"
POINTER_BOOTSTRAP_TEMPLATE = KIT_ROOT / "bootstrap" / "CLAUDE.md.template"


def test_bootstrap_names_the_profile_as_the_branching_source_of_truth():
    """반증: profile은 base main인데 local skill은 release/*를 지시했다. 정본이 둘이었다."""
    text = CANONICAL_BOOTSTRAP_TEMPLATE.read_text(encoding="utf-8")
    assert "branching과 PR 대상의 정본은 active profile의 `branching`/`pr`이다" in text
    assert "`pr.target_branch`로 표현" in text
    assert "`git branch -D`로 대체하지 않는다" in text


def test_claude_bootstrap_points_at_agents_instead_of_copying_it():
    """불변: 블록 본문은 한 벌이다.

    반증: 예전에는 두 템플릿의 바이트 동일성을 요구했다. 그건 같은 본문을 두 벌
    유지하라는 요구였고, 두 벌은 곧 둘이 갈라졌는지 보는 검사를 또 요구했다.
    """
    pointer = POINTER_BOOTSTRAP_TEMPLATE.read_text(encoding="utf-8")
    assert "`AGENTS.md`를 먼저 읽고" in pointer
    assert "canonical instruction file" in pointer
    # 마커는 남아야 재설치가 멱등하고 기존 설치본이 포인터로 수렴한다.
    assert "<!-- agent-flow:start -->" in pointer
    assert "<!-- agent-flow:end -->" in pointer
    # 본문 중복 없음은 블록의 구조 표지로 본다.
    assert "### Workflow Contract" not in pointer
    assert "### Context Economy" not in pointer
    assert "<!-- agent-flow:skills:start -->" not in pointer


@pytest.mark.parametrize("profile_id", _profile_ids())
def test_every_profile_declares_the_branching_contract_it_owns(profile_id):
    """정본을 profile이라 선언했으므로 그 값이 실제로 있어야 한다."""
    payload = yaml.safe_load((PROFILES_DIR / f"{profile_id}.yaml").read_text(encoding="utf-8"))
    branching = payload.get("branching") or {}
    pr = payload.get("pr") or {}
    assert branching.get("strategy") in {"trunk", "gitflow", "release-first"}
    assert branching.get("base")
    assert branching.get("integration")
    assert pr.get("target_branch")


def _write_override(root: Path, profile_id: str, body: str) -> Path:
    target = root / ".agent-flow" / "profiles" / f"{profile_id}.local.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def test_project_override_replaces_the_branch_contract(tmp_path):
    """불변: 재설치가 덮는 파일 밖에서 프로젝트가 base/PR target을 정할 수 있다.

    반증: 배포 profile을 직접 고치면 다음 install이 `base: main`으로 되돌리고,
    되돌아간 것을 아무도 모른 채 worktree가 다른 줄기에서 갈라졌다.
    """
    _write_override(
        tmp_path,
        "android",
        "branching:\n"
        "  strategy: release-first\n"
        '  base: "release/26.7.10.x"\n'
        '  integration: "release/26.7.10.x"\n'
        "pr:\n"
        '  target_branch: "release/26.7.10.x"\n',
    )

    payload = load_profile_payload("android", tmp_path)

    assert payload["branching"]["strategy"] == "release-first"
    assert payload["branching"]["base"] == "release/26.7.10.x"
    assert payload["pr"]["target_branch"] == "release/26.7.10.x"
    # 선언하지 않은 형제 값은 배포 profile 그대로 남는다.
    assert payload["branching"]["worktree_setup"]["copy"] == ["local.properties"]
    assert payload["pr"]["merge_strategy"] == "merge"
    assert payload["gates"] == load_profile_payload("android")["gates"]


def test_override_is_ignored_without_a_project_root():
    """불변: root 없이 부르는 경로(gates/lint)는 배포 profile만 본다."""
    assert load_profile_payload("android")["branching"]["base"] == "main"


def test_override_rejects_keys_it_would_not_apply(tmp_path):
    """불변: 반영되지 않는 선언은 조용히 삼키지 않는다.

    `skills`를 여기서 받으면 갈린다. 설치 대상을 정하는 쪽은 Python이 아니라 installer
    이고(`lib/skill-selection.mjs`의 `profileSkillsFromSource`는 kit의 `<id>.yaml`만
    읽는다) override는 Python 런타임만 통과한다. 선언한 목록과 설치된 목록이 어긋난
    사실은 라우팅이 빈 skill을 가리킬 때까지 보이지 않는다.
    """
    path = _write_override(tmp_path, "android", "skills:\n  install: []\n")

    with pytest.raises(ValueError) as excinfo:
        load_profile_payload("android", tmp_path)

    assert "skills" in str(excinfo.value)
    assert str(path) in str(excinfo.value)


def test_override_rejects_a_foreign_profile_id(tmp_path):
    """불변: 파일 이름과 다른 profile을 선언하면 어느 profile을 고친 건지 알 수 없다."""
    _write_override(tmp_path, "android", "id: ios\nbranching:\n  base: develop\n")

    with pytest.raises(ValueError, match="id mismatch"):
        load_profile_payload("android", tmp_path)



def test_override_rejects_a_target_that_differs_from_integration(tmp_path):
    path = _write_override(
        tmp_path,
        "android",
        "pr:\n"
        '  target_branch: "develop"\n',
    )

    with pytest.raises(ValueError, match="branching.integration") as excinfo:
        load_profile_payload("android", tmp_path)

    assert str(path) in str(excinfo.value)


@pytest.mark.parametrize(
    "body",
    [
        "branching: null\n",
        "pr: null\n",
        "branching:\n  base: null\n",
        "branching:\n  integration: null\n",
        "pr:\n  target_branch: null\n",
        "pr:\n  merge_strategy: null\n",
        "pr:\n  merge_strategy: octopus\n",
    ],
    ids=[
        "null-branching",
        "null-pr",
        "null-base",
        "null-integration",
        "null-target",
        "null-strategy",
        "unknown-strategy",
    ],
)
def test_override_rejects_an_invalid_branch_contract(tmp_path, body):
    path = _write_override(tmp_path, "android", body)

    with pytest.raises(ValueError, match="branch contract") as excinfo:
        load_profile_payload("android", tmp_path)

    assert str(path) in str(excinfo.value)


@pytest.mark.parametrize(
    "body",
    [
        'branching:\n  base: "release branch"\n',
        'branching:\n  base: "foo..bar"\n',
        'branching:\n  integration: "release branch"\n'
        'pr:\n  target_branch: "release branch"\n',
        'branching:\n  integration: "foo..bar"\n'
        'pr:\n  target_branch: "foo..bar"\n',
        'branching:\n  base: "release/.candidate"\n',
        'branching:\n  base: "release.lock/next"\n',
        'branching:\n  base: HEAD\n  integration: HEAD\n'
        'pr:\n  target_branch: HEAD\n',
        'branching:\n  base: refs/heads/release\n'
        '  integration: refs/heads/release\n'
        'pr:\n  target_branch: refs/heads/release\n',
    ],
    ids=[
        "base-space",
        "base-double-dot",
        "target-space",
        "target-double-dot",
        "dot-component",
        "lock-component",
        "head",
        "full-ref",
    ],
)
def test_override_rejects_an_unsafe_git_branch(tmp_path, body):
    path = _write_override(tmp_path, "android", body)

    with pytest.raises(ValueError, match="unsafe worktree branch") as excinfo:
        load_profile_payload("android", tmp_path)

    assert str(path) in str(excinfo.value)


def test_runner_profile_loading_applies_the_project_override(tmp_path):
    _write_override(
        tmp_path,
        "android",
        "branching:\n"
        '  base: "release/26.7.10.x"\n'
        '  integration: "release/26.7.10.x"\n'
        "pr:\n"
        '  target_branch: "release/26.7.10.x"\n',
    )
    (tmp_path / ".agent-flow" / "kit.json").write_text(
        '{"profiles":["android"]}\n', encoding="utf-8"
    )

    profile_id, payload = load_runner_profile(
        KIT_ROOT / "src" / "agent_flow",
        tmp_path,
    )

    assert profile_id == "android"
    assert payload["branching"]["base"] == "release/26.7.10.x"
    assert payload["pr"]["target_branch"] == "release/26.7.10.x"


def test_runner_profile_loading_prefers_an_installed_custom_profile(tmp_path):
    profiles = tmp_path / ".agent-flow" / "profiles"
    lint_root = tmp_path / "worktree"
    lint_root.mkdir()
    profiles.mkdir(parents=True)
    (profiles / "my-stack.yaml").write_text(
        "id: my-stack\n"
        "gates:\n"
        "  - id: architecture-lint\n"
        '    command: ["agent-flow", "architecture-lint", "--profile", "my-stack"]\n'
        "    required: true\n"
        "branching:\n"
        "  base: develop\n"
        "  integration: develop\n"
        "pr:\n"
        "  target_branch: develop\n"
        "  merge_strategy: merge\n",
        encoding="utf-8",
    )
    (tmp_path / ".agent-flow" / "kit.json").write_text(
        '{"profiles":["my-stack"]}\n',
        encoding="utf-8",
    )

    profile_id, payload = load_runner_profile(
        KIT_ROOT / "src" / "agent_flow",
        tmp_path,
    )

    assert profile_id == "my-stack"
    assert payload["branching"]["base"] == "develop"
    assert payload["pr"]["target_branch"] == "develop"
    commands = _profile_gate_commands(["my-stack"], root=tmp_path)
    assert commands[0].gate_id == "architecture-lint"
    assert commands[0].command[-2:] == ("--profile-root", str(tmp_path))
    lint = subprocess.run(
        commands[0].command,
        cwd=lint_root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert lint.returncode == 0, lint.stderr


def test_project_profile_rejects_an_id_that_differs_from_its_filename(tmp_path):
    profiles = tmp_path / ".agent-flow" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "my-stack.yaml").write_text(
        "id: other\n",
        encoding="utf-8",
    )
    (tmp_path / ".agent-flow" / "kit.json").write_text(
        '{"profiles":["my-stack"]}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="profile id mismatch: my-stack"):
        load_profile_payload("my-stack", tmp_path)
    with pytest.raises(ValueError, match="profile id mismatch: my-stack"):
        load_runner_profile(KIT_ROOT / "src" / "agent_flow", tmp_path)


def test_project_profile_rejects_an_invalid_branch_contract_without_override(tmp_path):
    profiles = tmp_path / ".agent-flow" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "my-stack.yaml").write_text(
        "id: my-stack\n"
        "branching:\n"
        "  base: null\n"
        "  integration: develop\n"
        "pr:\n"
        "  target_branch: develop\n"
        "  merge_strategy: merge\n",
        encoding="utf-8",
    )
    (tmp_path / ".agent-flow" / "kit.json").write_text(
        '{"profiles":["my-stack"]}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="branch contract"):
        load_profile_payload("my-stack", tmp_path)
    with pytest.raises(ValueError, match="branch contract"):
        load_runner_profile(KIT_ROOT / "src" / "agent_flow", tmp_path)


def test_resolved_profile_applies_the_project_override(tmp_path):
    _write_override(
        tmp_path,
        "android",
        "branching:\n"
        "  base: develop\n"
        "  integration: develop\n"
        "pr:\n"
        "  target_branch: develop\n",
    )
    (tmp_path / ".agent-flow" / "kit.json").write_text(
        '{"profiles":["android"]}\n',
        encoding="utf-8",
    )

    profile = resolved_profile(tmp_path)

    assert profile is not None
    assert profile["branching"]["base"] == "develop"


def _android_override(root: Path, body: str) -> Path:
    return _write_override(root, "android", body)


def test_android_build_gate_does_not_require_a_product_flavor():
    """반증: `assembleDevDebug`는 `dev` productFlavor를 선언한 저장소에만 있는 태스크라
    flavor 없는 저장소에서는 태스크 이름 단계에서 exit=1이 됐다(실측). 필수 gate가
    빌드 상태를 한 번도 관측하지 못한 채 항상 실패했다.
    """
    commands = {gate.gate_id: gate.command for gate in load_profile("android").gates}

    assert commands["build"] == ("./gradlew", "assembleDebug")


def test_override_declares_the_project_architecture(tmp_path):
    """불변: 평면 모듈 구조를 쓰는 저장소가 자기 role 표를 선언할 수 있다.

    반증: shipped 표는 `core/domain/<context>` 중첩을 가정한다. 평면 `core/domain`
    저장소에 그대로 걸리면 변경 소스 전량이 `outside profile architecture role mapping`
    으로 나왔다(실측 436개 중 182개). 배포 profile을 직접 고치면 install이 덮는다.
    """
    _android_override(
        tmp_path,
        "architecture:\n"
        "  activation_roots: [\"core/domain\"]\n"
        "  roles:\n"
        "    - id: core-domain\n"
        "      paths: [\"core/domain\"]\n"
        "      modules: [\":core:domain\"]\n"
        "    - id: core-model\n"
        "      paths: [\"core/model\"]\n"
        "      modules: [\":core:model\"]\n",
    )

    payload = load_profile_payload("android", tmp_path)
    architecture = payload["architecture"]

    assert [role["id"] for role in architecture["roles"]] == ["core-domain", "core-model"]
    assert architecture["activation_roots"] == ["core/domain"]
    # 선언하지 않은 형제 값은 배포 profile 그대로 남는다.
    assert architecture["contract"] == "clean-architecture-core"
    assert architecture["strict_when_roots_present"] is True


def test_override_architecture_reaches_the_lint_consumer(tmp_path):
    """불변: override가 lint가 실제로 읽는 표까지 닿는다.

    반증: 반영되는 경로와 무시되는 경로가 갈리면 사용자는 선언이 걸렸다고 믿은 채
    같은 오탐을 계속 본다. lint 소비자는 root를 넘기므로 갈리지 않는다.
    """
    _android_override(
        tmp_path,
        "architecture:\n"
        "  roles:\n"
        "    - id: core-domain\n"
        "      paths: [\"core/domain\"]\n"
        "      modules: [\":core:domain\"]\n",
    )

    roles, managed_roots = profile_lint_context("android", tmp_path)

    assert [role["id"] for role in roles] == ["core-domain"]
    # root 파생 규칙(family parent 등)은 lint가 소유한다. 여기서 못 박는 것은
    # override가 선언한 경로가 그 파생의 입력이 됐다는 사실 하나다.
    assert "core/domain" in managed_roots
    assert not any(root.startswith("feature/") for root in managed_roots)


def test_override_declares_the_project_build_gate(tmp_path):
    """불변: 프로젝트가 자기 빌드/테스트 태스크를 선언할 수 있고, 그것이 gates 실행
    경로까지 닿는다. `agent-flow gates`는 profile root를 넘기므로 override가 산다.
    """
    _android_override(
        tmp_path,
        "gates:\n"
        "  - id: build\n"
        "    command: [\"./gradlew\", \":app:assembleProdRelease\"]\n"
        "    required: true\n"
        "    phase: pre-push\n",
    )

    commands = _profile_gate_commands(["android"], root=tmp_path, phase="pre-push")

    assert [command.gate_id for command in commands] == ["build"]
    assert commands[0].command == ("./gradlew", ":app:assembleProdRelease")


def test_override_lists_replace_instead_of_appending(tmp_path):
    """불변: list는 교체다.

    반증: 이어붙이면 배포본의 role 표와 gate가 남는다. 프로젝트가 자기 구조를
    선언해도 맞지 않는 shipped 표가 계속 미매핑 finding을 내고, 지운 적 없는 gate가
    계속 돈다. 교체만이 "이 저장소의 구조는 이것이다"를 표현한다.
    """
    shipped = load_profile_payload("android")
    assert len(shipped["architecture"]["roles"]) > 1
    assert len(shipped["gates"]) > 1

    _android_override(
        tmp_path,
        "gates:\n"
        "  - id: test\n"
        "    command: [\"./gradlew\", \"testDebugUnitTest\"]\n"
        "architecture:\n"
        "  roles:\n"
        "    - id: core-domain\n"
        "      paths: [\"core/domain\"]\n"
        "      modules: [\":core:domain\"]\n",
    )

    payload = load_profile_payload("android", tmp_path)

    assert [gate["id"] for gate in payload["gates"]] == ["test"]
    assert [role["id"] for role in payload["architecture"]["roles"]] == ["core-domain"]


@pytest.mark.parametrize(
    "body",
    [
        "architecture: []\n",
        "architecture: android\n",
        "architecture:\n  roles: core-domain\n",
        "architecture:\n  roles:\n    - core-domain\n",
    ],
)
def test_override_rejects_an_architecture_shape_the_lint_would_drop(tmp_path, body):
    """불변: lint는 모양이 안 맞는 architecture를 조용히 버리고 finding 0개를 낸다
    (`lint_project`). 그러면 오타 하나가 "통과"로 보이므로 선언한 자리에서 던진다.
    """
    path = _android_override(tmp_path, body)

    with pytest.raises(ValueError) as excinfo:
        load_profile_payload("android", tmp_path)

    assert "architecture" in str(excinfo.value)
    assert str(path) in str(excinfo.value)


@pytest.mark.parametrize(
    "body",
    [
        "gates: build\n",
        "gates:\n  - build\n",
        "gates:\n  - command: [\"./gradlew\", \"assembleDebug\"]\n",
        "gates:\n  - id: build\n    command: []\n",
        "gates:\n  - id: build\n    command: [\"./gradlew\"]\n    phase: prepush\n",
    ],
)
def test_override_rejects_a_gate_the_runner_could_not_execute(tmp_path, body):
    """불변: gate 실행이 쓰는 파서로 검사한다. 여기서 통과한 선언이 실행 시점에
    터지면 오류가 선언한 자리에서 멀어진다.
    """
    path = _android_override(tmp_path, body)

    with pytest.raises(ValueError) as excinfo:
        load_profile_payload("android", tmp_path)

    assert str(path) in str(excinfo.value)



def _flat_feature_repo(root: Path) -> None:
    """`:feature:journey:api`에 의존하지 않는 presentation 모듈 하나."""
    api = root / "feature" / "journey" / "api"
    presentation = root / "feature" / "journey" / "presentation"
    api.mkdir(parents=True)
    presentation.mkdir(parents=True)
    (api / "build.gradle.kts").write_text("dependencies {\n}\n", encoding="utf-8")
    (presentation / "build.gradle.kts").write_text(
        'dependencies {\n    implementation(project(":core:ui"))\n}\n',
        encoding="utf-8",
    )


# `feature-api`의 `modules` 한 줄만 갈아 끼우는 틀. 나머지는 고정이라 두 케이스의
# 차이가 그 한 줄뿐임을 테스트가 스스로 보인다.
_FEATURE_ROLES_OVERRIDE = (
    "architecture:\n"
    '  activation_roots: ["feature"]\n'
    "  roles:\n"
    "    - id: feature-api\n"
    '      paths: ["feature/<feature>/api"]\n'
    "{api_modules}"
    "    - id: feature-presentation\n"
    '      paths: ["feature/<feature>/presentation"]\n'
    '      modules: [":feature:<feature>:presentation"]\n'
)
_MUST_DEPEND_ON = "feature-presentation must depend on :feature:journey:api"


def _presentation_findings(root: Path) -> list[str]:
    return [
        finding.message
        for finding in lint_project(
            root,
            "android",
            ["feature/journey/presentation/build.gradle.kts"],
            profile_root=root,
        )
    ]


def test_override_that_restates_modules_keeps_the_must_depend_on_rule(tmp_path):
    """불변: 배포본이 보고하던 규칙은 override 뒤에도 보고된다."""
    _flat_feature_repo(tmp_path)
    _android_override(
        tmp_path,
        _FEATURE_ROLES_OVERRIDE.format(
            api_modules='      modules: [":feature:<feature>:api"]\n'
        ),
    )

    assert _MUST_DEPEND_ON in _presentation_findings(tmp_path)


def test_override_cannot_drop_a_modules_declaration_the_shipped_role_made(tmp_path):
    """불변: 배포본이 선언한 모듈 소유권을 조용히 버릴 수 없다.

    반증: `REQUIRED_GRADLE_MODULES`는 role id로 키가 잡혀 있고 override로 바뀌지
    않는다. 그 표의 required 항목은 `role_owns_module`로 gating되므로, 소유자 role의
    `modules`만 빠지면 규칙은 표에 남은 채 조용히 꺼진다 — 실측으로 배포 profile은
    이 규칙을 보고했고 modules만 뺀 override에서는 must-depend-on이 0건이었다.
    """
    path = _android_override(tmp_path, _FEATURE_ROLES_OVERRIDE.format(api_modules=""))

    with pytest.raises(ValueError) as excinfo:
        load_profile_payload("android", tmp_path)

    message = str(excinfo.value)
    assert "feature-api" in message
    assert "modules" in message
    assert str(path) in message


def test_override_may_drop_the_rule_only_by_declaring_it_empty(tmp_path):
    """불변: 규칙을 끄는 길은 막지 않는다 — 다만 적어야 한다.

    빈 목록은 리뷰에서 보이는 선언이다. 키를 빼는 것과 달리 "이 저장소에는 그
    모듈이 없다"를 누가 언제 정했는지 diff에 남는다.
    """
    _flat_feature_repo(tmp_path)
    _android_override(
        tmp_path,
        _FEATURE_ROLES_OVERRIDE.format(api_modules="      modules: []\n"),
    )

    assert _MUST_DEPEND_ON not in _presentation_findings(tmp_path)


def test_override_may_add_a_role_the_shipped_profile_never_declared(tmp_path):
    """불변: 배포본에 없는 role id는 대조 대상이 없으므로 막지 않는다.

    막는 것은 "있던 선언을 빼는 것" 하나다. 새 role까지 막으면 평면 레이아웃
    저장소가 자기 구조를 선언할 수 없다.
    """
    _android_override(
        tmp_path,
        "architecture:\n"
        "  roles:\n"
        "    - id: core-model\n"
        '      paths: ["core/model"]\n',
    )

    payload = load_profile_payload("android", tmp_path)

    assert [role["id"] for role in payload["architecture"]["roles"]] == ["core-model"]


@pytest.mark.parametrize(
    "value",
    ["tracked_only", "trackedonly", "off", "none", "\"\"", "[tracked-only]"],
)
def test_override_rejects_a_leader_tripwire_scope_the_sweep_would_refuse(tmp_path, value):
    """불변: 오타는 선언한 자리에서 터진다.

    반증: 소비 자리(`Runner.__init__`)에서만 터지면 run 경로의 실패 처리가 방금 만든
    worktree를 지운다. 오타 하나의 값이 작업 트리 삭제일 수는 없다.
    """
    path = _android_override(tmp_path, f"branching:\n  leader_tripwire: {value}\n")

    with pytest.raises(ValueError) as excinfo:
        load_profile_payload("android", tmp_path)

    message = str(excinfo.value)
    assert "leader_tripwire" in message
    assert str(path) in message


@pytest.mark.parametrize("value", LEADER_SWEEP_SCOPES)
def test_override_accepts_every_scope_the_sweep_consumes(tmp_path, value):
    _android_override(tmp_path, f"branching:\n  leader_tripwire: {value}\n")

    payload = load_profile_payload("android", tmp_path)

    assert payload["branching"]["leader_tripwire"] == value