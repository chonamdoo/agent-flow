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
from agent_flow.core.architecture_lint import main as architecture_lint_main  # noqa: E402
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
from agent_flow.runner import _load_profile as load_runner_profile  # noqa: E402

PROFILES_DIR = KIT_ROOT / "src" / "agent_flow" / "profiles"


def _schema_gate_phases() -> list[str]:
    schema = yaml.safe_load((PROFILES_DIR / "_schema.yaml").read_text(encoding="utf-8"))
    return [value.strip() for value in schema["optional"]["gates"][0]["phase"].split("|")]


def _profile_ids() -> list[str]:
    return sorted(path.stem for path in PROFILES_DIR.glob("*.yaml") if not path.stem.startswith("_"))


def _gate(**overrides: object) -> ProfileGate:
    return _gate_from_payload(
        {"id": "probe", "command": ["true"], **overrides},
        profile_id="probe-profile",
    )


def test_schema_declared_phases_are_the_consumed_phases():
    assert _schema_gate_phases() == list(GATE_PHASES)


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


BOOTSTRAP_TEMPLATES = (
    KIT_ROOT / "bootstrap" / "AGENTS.md.template",
    KIT_ROOT / "bootstrap" / "CLAUDE.md.template",
)


def test_bootstrap_names_the_profile_as_the_branching_source_of_truth():
    """반증: profile은 base main인데 local skill은 release/*를 지시했다. 정본이 둘이었다."""
    texts = [path.read_text(encoding="utf-8") for path in BOOTSTRAP_TEMPLATES]
    # 두 템플릿은 parity 검사가 동일성을 요구한다. 한쪽만 고치면 install 결과가 갈린다.
    assert texts[0] == texts[1]
    for text in texts:
        assert "branching과 PR 대상의 정본은 active profile의 `branching`/`pr`이다" in text
        assert "`pr.target_branch`로 표현" in text
        assert "`git branch -D`로 대체하지 않는다" in text


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

    gates를 여기서 받으면 `agent-flow gates`는 root 없이 payload를 읽으므로 override가
    무시되고, 사용자는 걸렸다고 믿는다.
    """
    path = _write_override(tmp_path, "android", "gates: []\n")

    with pytest.raises(ValueError) as excinfo:
        load_profile_payload("android", tmp_path)

    assert "gates" in str(excinfo.value)
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
