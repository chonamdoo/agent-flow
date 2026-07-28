"""kit root 해석과 워크플로 정의 로딩이 자산 **배치**에 의존하지 않는지 본다.

지금까지 kit root는 "`workflows/`와 `profiles/`를 둘 다 가진 조상"으로 정의됐다.
배치가 곧 정의라서, 그 두 디렉터리를 한 벌로 줄이면 탐지가 함께 깨진다. 여기서
고정하는 것은 그 결합을 끊었다는 사실이다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core.phase_workflow import (
    find_kit_root,
    load_phase_workflow_definition,
)


def _kit_tree(root: Path) -> Path:
    """kit 서명을 갖춘 최소 트리. 워크플로/프로파일 사본은 일부러 두지 않는다."""
    (root / "bin").mkdir(parents=True, exist_ok=True)
    (root / "bin" / "agent-flow-kit.mjs").write_text("// kit\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='agent-flow'\n", encoding="utf-8")
    module = root / "src" / "agent_flow" / "core" / "phase_workflow.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("", encoding="utf-8")
    return module


def test_find_kit_root_does_not_depend_on_root_yaml_layout(tmp_path: Path):
    """반증: 루트 `workflows/`+`profiles/`가 없으면 kit root를 못 찾는다."""
    kit = tmp_path / "kit"
    module = _kit_tree(kit)
    assert not (kit / "workflows").exists()
    assert not (kit / "profiles").exists()

    assert find_kit_root(module) == kit


def test_find_kit_root_does_not_mistake_a_user_project_for_the_kit(tmp_path: Path):
    """불변: 사용자 프로젝트를 kit으로 오인하면 남의 워크플로를 돌린다.

    `pyproject.toml`이나 `package.json`만 보고 판정하면 Python+Node를 함께 쓰는
    평범한 프로젝트가 전부 kit root 후보가 된다. kit 고유 서명을 함께 요구한다.
    """
    project = tmp_path / "user-project"
    (project / "src" / "agent_flow" / "core").mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='theirs'\n", encoding="utf-8")
    (project / "package.json").write_text('{"name":"theirs"}\n', encoding="utf-8")
    module = project / "src" / "agent_flow" / "core" / "phase_workflow.py"
    module.write_text("", encoding="utf-8")

    # kit 서명(`bin/agent-flow-kit.mjs`)이 없으므로 이 트리는 kit root가 아니다.
    assert find_kit_root(module) != project


def test_workflow_loads_when_the_kit_root_has_no_workflow_copy(tmp_path: Path):
    """반증: 정의가 `<kit_root>/workflows`에만 있으면 사본을 못 지운다.

    정의의 정본은 설치 가능한 패키지 자원이다. kit root에 사본이 없어도 로딩은
    성공해야, 루트 사본을 지우는 다음 슬라이스가 성립한다.
    """
    definition = load_phase_workflow_definition(tmp_path, "default")
    assert definition.id == "default"
    assert definition.phases, "phase가 비면 정의를 읽은 것이 아니다"


def test_kit_root_copy_still_wins_over_the_package(tmp_path: Path):
    """불변: 설치본이 제 워크플로를 덮어쓸 수 있어야 한다.

    패키지 자원으로 폴백하는 것이 kit root 사본을 무시한다는 뜻이면, 사용자가
    설치본에 둔 정의가 조용히 무시된다.
    """
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "default.yaml").write_text(
        "id: default\nphases:\n  - id: only\n    description: d\n    prompt: p\n",
        encoding="utf-8",
    )

    definition = load_phase_workflow_definition(tmp_path, "default")
    assert [phase.id for phase in definition.phases] == ["only"]


def test_unknown_workflow_still_fails_closed(tmp_path: Path):
    """불변: 없는 워크플로가 조용히 빈 정의로 떨어지면 phase 체인이 사라진다."""
    with pytest.raises(FileNotFoundError):
        load_phase_workflow_definition(tmp_path, "no-such-workflow")


def test_profile_loads_when_the_kit_root_has_no_profile_copy(tmp_path: Path):
    """반증: profile이 `<kit_root>/profiles`에만 있으면 루트 사본을 못 지운다."""
    from agent_flow.runner import _load_single_profile

    profile_id, raw = _load_single_profile(
        tmp_path,
        "python",
        strict_missing=True,
        explicit_fallback=False,
        source="test",
    )
    assert profile_id == "python"
    assert raw.get("id") == "python"


def test_unknown_profile_still_fails_closed(tmp_path: Path):
    """불변: kit.json 오타가 조용히 generic으로 떨어지면 잘못된 스택으로 전 워크플로가 돈다."""
    from agent_flow.runner import _load_single_profile

    with pytest.raises(FileNotFoundError):
        _load_single_profile(
            tmp_path,
            "no-such-profile",
            strict_missing=True,
            explicit_fallback=False,
            source="test",
        )
