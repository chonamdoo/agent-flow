"""profile gate의 phase 선언이 실제로 소비되는지 고정한다.

`profiles/*.yaml`은 게이트마다 pre-commit/pre-push를 선언한다. 파서나 필터 중
한쪽이라도 빠지면 그 선언은 죽은 설정이 되고, pre-commit 자리인 gates phase가
pre-push 게이트까지 돌린다(issue #130).
"""
from __future__ import annotations

import copy
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
from agent_flow.core.local_skills import merged_profile_payload, resolved_profile  # noqa: E402
from agent_flow.core.profiles import (  # noqa: E402
    DEFAULT_GATE_PHASE,
    GATE_PHASE_ALL,
    GATE_PHASES,
    ProfileGate,
    _gate_from_payload,
    load_profile,
    load_profile_payload,
)
from agent_flow.core.reviewer_launch import (  # noqa: E402
    ReviewerLaunchError,
    validate_reviewer_launch_declaration,
)
from agent_flow.core.skill_catalog import discover_skill_catalog  # noqa: E402
from agent_flow.core.skill_matching import match_external  # noqa: E402
from agent_flow.core.skill_resolver import skill_roots  # noqa: E402
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


def test_bootstrap_names_the_profile_as_the_branching_source_of_truth():
    """반증: profile은 base main인데 local skill은 release/*를 지시했다. 정본이 둘이었다."""
    text = CANONICAL_BOOTSTRAP_TEMPLATE.read_text(encoding="utf-8")
    assert "branching과 PR 대상의 정본은 active profile의 `branching`/`pr`이다" in text
    assert "`pr.target_branch`로 표현" in text
    assert "`git branch -D`로 대체하지 않는다" in text


def test_bootstrap_block_has_exactly_one_template_for_both_root_files():
    """불변: 블록 본문은 한 벌이다.

    반증: 처음에는 두 템플릿의 바이트 동일성을 요구했다(같은 본문을 두 벌 유지하라는
    요구). 다음에는 `CLAUDE.md.template`을 `@AGENTS.md` **포인터만** 담은 파일로
    바꿨고, 그러면 Claude는 계약을 import로 받고 Codex/OMP는 직접 읽어 host마다
    로드되는 텍스트가 갈렸다. 파일이 한 벌이면 사본이 갈라질 자리가 없다.

    import는 이제 템플릿이 아니라 `rootBootstrapBlock`이 CLAUDE.md에만 붙인다 —
    템플릿에 두면 `AGENTS.md`가 자기 자신을 import한다.
    """
    assert CANONICAL_BOOTSTRAP_TEMPLATE.is_file()
    assert sorted(p.name for p in (KIT_ROOT / "bootstrap").glob("*.template")) == [
        "AGENTS.md.template"
    ]
    # 마커는 남아야 재설치가 멱등하다.
    text = CANONICAL_BOOTSTRAP_TEMPLATE.read_text(encoding="utf-8")
    assert "<!-- agent-flow:start -->" in text
    assert "<!-- agent-flow:end -->" in text
    assert "<!-- agent-flow:skills:start -->" in text
    assert "@AGENTS.md" not in text


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


def _profile_payload(profile_id: str) -> dict:
    return yaml.safe_load((PROFILES_DIR / f"{profile_id}.yaml").read_text(encoding="utf-8"))


def _profiles_declaring_execution() -> list[str]:
    return [
        profile_id
        for profile_id in _profile_ids()
        if "execution" in _profile_payload(profile_id)
    ]


@pytest.mark.parametrize("profile_id", _profile_ids())
def test_shipped_profile_execution_declaration_is_parseable(profile_id):
    """불변: 배포하는 reviewer launch 선언은 여기서 한 번 파싱된다.

    반증: `validate_reviewer_launch_declaration`은 프로젝트 override에만 걸려 있었다
    (`profiles.py`의 `_validate_override_execution`). 배포 profile의 선언은 리뷰가
    provider를 띄우기 직전(`multi_review._resolve_reviewer_launch`)에 처음 해석되고,
    그 자리는 fail-closed다 — 오타 한 글자가 설치된 **모든** 프로젝트의 리뷰를
    막고, 그것도 CI가 아니라 사용자 run에서 처음 드러난다.

    검사는 소비자가 쓰는 파서 그대로다. 여기 규칙을 따로 적으면 두 벌이 갈린다.
    `execution`을 선언하지 않은 profile은 검사할 것이 없으므로 통과한다.
    """
    payload = _profile_payload(profile_id)
    if "execution" not in payload:
        return
    validate_reviewer_launch_declaration(payload["execution"])


# 선언 안에서 한 글자가 틀리는 방식들. 값을 고르는 기준은 "소비자가 실제로 거부하는
# 모양"이고, 어느 것도 `reviewers[0]`이 mapping이라는 것 밖의 구조를 가정하지 않는다 —
# 새 profile이 다른 모양으로 선언해도 이 검사가 헛돌지 않게.
_EXECUTION_TYPOS: tuple[tuple[str, object], ...] = (
    ("execution key", lambda execution: execution.update({"reviewer": []})),
    ("rule key", lambda execution: execution["reviewers"][0].update({"candidate": []})),
    (
        "match key",
        lambda execution: execution["reviewers"][0].update({"match": {"angel": "x"}}),
    ),
    (
        "match value",
        lambda execution: execution["reviewers"][0].update(
            {"match": {"phase": "review", "angle": 5}}
        ),
    ),
    (
        "candidate provider",
        lambda execution: execution["reviewers"][0].update(
            {"candidates": [{"provider": "claud"}]}
        ),
    ),
    (
        "candidate model",
        lambda execution: execution["reviewers"][0].update(
            {"candidates": [{"provider": "claude", "model": "-opus"}]}
        ),
    ),
    (
        "candidate effort",
        lambda execution: execution["reviewers"][0].update(
            {"candidates": [{"provider": "claude", "effort": "maximum"}]}
        ),
    ),
)


@pytest.mark.parametrize("profile_id", _profiles_declaring_execution())
@pytest.mark.parametrize("label,typo", _EXECUTION_TYPOS, ids=[label for label, _ in _EXECUTION_TYPOS])
def test_the_shipped_execution_sweep_rejects_a_typo(profile_id, label, typo):
    """불변: 위 sweep은 실제로 문다.

    반증: 통과만 관측하면 "검사가 걸려 있다"와 "검사가 아무것도 보지 않는다"가 같은
    모양이다. 배포 선언의 사본에 오타를 하나씩 넣어 그 sweep이 그때는 터지는지 본다.

    `match value`가 특히 이 sweep의 사각지대였다: `rule_matches`가 비교하다 말고
    검사하던 동안, phase가 어긋난 rule의 `angle` 오타는 그 phase에 들어서기 전까지
    한 번도 읽히지 않았다. 검사는 `validated_match`가 비교와 무관하게 한다.

    `execution`을 선언한 배포 profile이 하나도 없으면 이 검사는 skip이고, 그때는
    위 sweep도 검사할 대상이 없다.
    """
    execution = copy.deepcopy(_profile_payload(profile_id)["execution"])
    typo(execution)

    with pytest.raises(ReviewerLaunchError):
        validate_reviewer_launch_declaration(execution)



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
        # 키가 있고 값이 없는 경우. `None`을 "선언 안 함"으로 접으면 배포본 표가
        # `None`으로 교체되고 lint가 finding 0개를 돌려준다.
        "architecture:\n  roles:\n",
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
        # list 자리의 스칼라. `match_role`/`role_owns_module`이 조용히 건너뛴다.
        "architecture:\n  roles:\n    - id: core-domain\n      paths: core/domain\n",
        "architecture:\n  roles:\n    - id: core-domain\n      paths: [\"core/domain\"]\n"
        "      modules: \":core:domain\"\n",
        "architecture:\n  roles:\n    - id: core-domain\n      paths: [\"core/domain\"]\n"
        "      forbidden: Dto\n",
        "architecture:\n  roles:\n    - id: core-domain\n      paths: [\"\"]\n",
        # id가 빠지면 gradle 규칙이 `role_id=\"\"`로 조회돼 통째로 꺼진다.
        "architecture:\n  roles:\n    - paths: [\"core/domain\"]\n"
        "      modules: [\":core:domain\"]\n",
        "architecture:\n  roles:\n    - id: \"\"\n      paths: [\"core/domain\"]\n",
    ],
)
def test_override_rejects_a_role_field_the_lint_would_skip(tmp_path, body):
    """불변: 소비자가 침묵하는 모양은 선언 자리에서 막는다.

    `paths: "core/domain"`은 `isinstance(..., list)`에서 건너뛰어져 role이 아무
    파일도 잡지 못하고, id 누락은 role id로 키가 잡힌 gradle 규칙 전부를 끈다.
    둘 다 필수 gate가 "무위반"으로 보이는 결과가 된다.
    """
    path = _android_override(tmp_path, body)

    with pytest.raises(ValueError) as excinfo:
        load_profile_payload("android", tmp_path)

    assert "roles" in str(excinfo.value)
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


def _external_domains(payload: dict) -> dict[str, tuple[str, ...]]:
    block = (payload.get("skills") or {}).get("external") or {}
    return {
        str(domain.get("id")): tuple(domain.get("terms") or [])
        for domain in block.get("domains") or []
        if isinstance(domain, dict)
    }


def test_shipped_profiles_never_share_a_domain_id_with_different_vocabulary():
    """불변: external domain id는 그 어휘의 소유자다.

    반증: `merged_profile_payload`는 같은 id를 **먼저 온 profile 것만** 남긴다
    (`core/local_skills.py`). 서로 다른 어휘를 적은 두 profile이 id를 공유하면
    합집합의 어휘가 profile 탐지 순서로 정해지고, 뒤에 온 쪽 어휘는 통째로
    사라진다. 실측으로 `nextjs`+`react-native`가 `react-component-design`을
    공유하던 동안 RN의 `performance optimization`이 그렇게 없어졌다.
    """
    by_id: dict[str, dict[str, tuple[str, ...]]] = {}
    for profile_id in _profile_ids():
        payload = yaml.safe_load((PROFILES_DIR / f"{profile_id}.yaml").read_text(encoding="utf-8"))
        for domain_id, terms in _external_domains(payload).items():
            by_id.setdefault(domain_id, {})[profile_id] = terms

    conflicts = {
        domain_id: sorted(owners)
        for domain_id, owners in by_id.items()
        if len(set(owners.values())) > 1
    }

    assert conflicts == {}, (
        f"같은 domain id를 다른 어휘로 선언한 profile이 있다: {conflicts}. "
        "id를 profile별로 나누거나 어휘를 일치시켜라."
    )


@pytest.mark.parametrize(
    "order", [("nextjs", "react-native"), ("react-native", "nextjs")]
)
def test_profile_union_vocabulary_is_order_independent(order):
    """불변: 합집합의 어휘는 profile 순서와 무관하다.

    `performance optimization`은 RN에서는 필요하고(React Native용 best-practices
    skill이 그 어휘로만 잡힌다) 웹에서는 뺀 어휘다(같은 term이 그 RN skill을 Next.js
    프로젝트로 끌어온다). 그래서 공통 어휘와 RN 전용 어휘는 다른 domain에 있어야 한다.
    """
    merged = merged_profile_payload([load_profile_payload(profile_id) for profile_id in order])
    domains = _external_domains(merged)

    assert domains["rn-component-performance"] == ("performance optimization",)
    assert "performance optimization" not in domains["component-design"]
    assert "compound component" in domains["component-design"]


def test_no_shipped_external_domain_is_a_superset_of_another():
    """불변: 한 domain의 어휘가 다른 domain 어휘를 통째로 포함하지 않는다.

    반증: `match_external`은 skill을 **먼저 걸린 domain**에 배정하고 `_truncate`가
    domain별 round-robin으로 자른다. superset domain이 먼저 순회되면 subset domain에도
    걸릴 skill까지 그 bucket으로 빨려 들어가 bucket 분포가 달라지고, required cap에
    닿는 순간 살아남는 skill 집합이 profile 순서로 갈린다. id를 나눠도 이 경로는 남는다.
    """
    declared: dict[str, frozenset[str]] = {}
    for profile_id in _profile_ids():
        payload = yaml.safe_load((PROFILES_DIR / f"{profile_id}.yaml").read_text(encoding="utf-8"))
        for domain_id, terms in _external_domains(payload).items():
            declared.setdefault(domain_id, frozenset(terms))

    supersets = [
        (outer, inner)
        for outer, outer_terms in declared.items()
        for inner, inner_terms in declared.items()
        if outer != inner and inner_terms and inner_terms < outer_terms
    ]

    assert supersets == [], (
        f"어휘가 다른 domain을 통째로 포함하는 domain이 있다: {supersets}. "
        "공통 어휘를 별도 domain으로 빼라."
    )


def test_external_routing_is_identical_under_either_profile_order():
    """불변: 실제 라우팅 결과가 profile 탐지 순서와 무관하다.

    선언만 보는 검사로는 부족하다. 같은 어휘를 선언해도 skill이 **어느 domain에**
    배정되는지가 순서로 갈리면 round-robin 절단의 입력이 달라진다. 그래서 이름이
    아니라 `(name, domain, tier)`까지 대조한다.
    """
    root = Path(".")
    catalog = discover_skill_catalog(root, skill_roots(root))
    task = (
        "compound component 리팩터링과 performance optimization, "
        "bundle optimization, render prop 정리"
    )
    seen = set()
    for order in (("nextjs", "react-native"), ("react-native", "nextjs")):
        merged = merged_profile_payload([load_profile_payload(profile_id) for profile_id in order])
        matches = match_external(
            merged,
            catalog,
            phase_id="implement",
            changed_files=["src/app/page.tsx"],
            task_text=task,
        )
        seen.add(tuple(sorted((match.name, match.domain, match.tier) for match in matches)))

    assert len(seen) == 1, f"profile 순서가 라우팅을 바꾼다: {seen}"


def test_profile_loading_does_not_import_the_environment_probes():
    """profile 로딩은 선언을 읽는 일이다. PATH probe를 끌고 오면 안 된다.

    `cli_detect`는 `shutil.which`와 `subprocess`로 설치된 CLI를 뒤지는 환경 탐지고,
    `multi_review`는 reviewer 프로세스를 띄우는 자리다. reviewer 어휘 상수 하나를
    빌리려고 그것들을 import하면, gate 나열이나 skill 해석처럼 CLI와 무관한 경로가
    프로세스를 띄우는 모듈을 통째로 적재한다.

    같은 프로세스는 이미 두 모듈을 import했을 수 있으므로 새 인터프리터에서 본다.
    """
    probe = (
        "import sys, agent_flow.core.profiles;"
        "print('agent_flow.cli_detect' in sys.modules,"
        " 'agent_flow.multi_review' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(KIT_ROOT),
        env={"PYTHONPATH": str(KIT_ROOT / "src"), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "False False", result.stderr