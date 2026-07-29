"""profile gate의 phase 선언이 실제로 소비되는지 고정한다.

`profiles/*.yaml`은 게이트마다 pre-commit/pre-push를 선언한다. 파서나 필터 중
한쪽이라도 빠지면 그 선언은 죽은 설정이 되고, pre-commit 자리인 gates phase가
pre-push 게이트까지 돌린다(issue #130).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

KIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT_ROOT / "src"))

from agent_flow.cli import _profile_gate_commands  # noqa: E402
from agent_flow.core.profiles import (  # noqa: E402
    DEFAULT_GATE_PHASE,
    GATE_PHASE_ALL,
    GATE_PHASES,
    ProfileGate,
    _gate_from_payload,
    load_profile,
)

PROFILES_DIR = KIT_ROOT / "profiles"


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
