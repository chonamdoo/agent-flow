"""kit root 해석과 워크플로 정의 로딩이 자산 **배치**에 의존하지 않는지 본다.

지금까지 kit root는 "`workflows/`와 `profiles/`를 둘 다 가진 조상"으로 정의됐다.
배치가 곧 정의라서, 그 두 디렉터리를 한 벌로 줄이면 탐지가 함께 깨진다. 여기서
고정하는 것은 그 결합을 끊었다는 사실이다.
"""
from __future__ import annotations

import subprocess
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
    from agent_flow.core.profile_resolution import load_single_profile

    profile_id, raw = load_single_profile(
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
    from agent_flow.core.profile_resolution import load_single_profile

    with pytest.raises(FileNotFoundError):
        load_single_profile(
            tmp_path,
            "no-such-profile",
            strict_missing=True,
            explicit_fallback=False,
            source="test",
        )


def _tracked_files() -> list[str]:
    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ("git", "ls-files"),
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.split()


@pytest.mark.parametrize("kind", ["workflows", "profiles", "roles"])
def test_repository_tracks_one_copy_of_each_definition(kind: str):
    """반증: 같은 정의가 두 벌 추적되면 둘을 맞추는 도구가 계속 필요하다.

    루트 사본과 패키지 사본이 바이트 동일하다는 것을 parity 스크립트가 지켜 왔다.
    사본이 한 벌이면 지킬 것이 없어지고 그 검사도 함께 죽는다.
    """
    tracked = [
        path
        for path in _tracked_files()
        if path.endswith(".yaml") and f"{kind}/" in path
    ]
    assert tracked, f"{kind} 정의가 하나도 추적되지 않는다"

    by_name: dict[str, list[str]] = {}
    for path in tracked:
        by_name.setdefault(Path(path).name, []).append(path)

    duplicated = {name: paths for name, paths in by_name.items() if len(paths) > 1}
    assert not duplicated, f"{kind} 정의가 두 벌 이상 추적된다: {duplicated}"


def test_skill_selection_reads_the_packaged_profile_yaml():
    """반증: 경로가 틀려도 하드코딩 맵으로 폴백해서 조용히 통과한다.

    `lib/skill-selection.mjs`는 profile YAML을 못 읽으면 `PROFILE_SKILLS` 맵으로
    떨어진다. 그래서 경로가 깨져도 테스트가 녹색이고, YAML이 정본이라는 계약만
    소리 없이 사라진다. 경로 자체가 실재하는지를 직접 본다.
    """
    repo = Path(__file__).resolve().parents[1]
    profiles = sorted(
        path.stem
        for path in (repo / "src" / "agent_flow" / "profiles").glob("*.yaml")
        if not path.stem.startswith("_")
    )
    assert profiles, "패키지에 profile 정의가 없다"

    script = (
        "import { profileYamlPath } from './lib/skill-selection.mjs';\n"
        "import fs from 'node:fs';\n"
        f"const names = {list(profiles)!r}.map(String);\n"
        "const missing = names.filter((name) => "
        "!fs.existsSync(profileYamlPath(process.cwd(), name)));\n"
        "console.log(JSON.stringify(missing));\n"
    ).replace("'", "'")
    result = subprocess.run(
        ("node", "--input-type=module", "-e", script),
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]", (
        f"skill-selection이 못 찾는 profile YAML: {result.stdout.strip()}"
    )
