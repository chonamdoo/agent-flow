"""YAML 한 mapping 안에서 키는 한 번만 나온다.

PyYAML `safe_load`는 같은 키가 두 번 나오면 뒤엣것으로 덮는다. 사람이 파일에서 본 값과
실행이 쓰는 값이 갈려도 아무 신호가 없다. 그래서 공용 검사기가 노드 단계에서 막고,
어느 파일의 몇 번째 줄인지까지 말한다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

KIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT_ROOT / "src"))

from agent_flow.core.architecture_policy import parse_architecture_document  # noqa: E402
from agent_flow.core.profiles import load_profile_payload  # noqa: E402
from agent_flow.core.skill_metadata import (  # noqa: E402
    parse_skill_metadata,
    reject_duplicate_keys,
)


def _read_skill(root: Path, path: Path) -> None:
    parse_skill_metadata(path.read_text(encoding="utf-8"), source=str(path))


def _read_architecture(root: Path, path: Path) -> None:
    parse_architecture_document(path.read_text(encoding="utf-8"), source=str(path))


def _read_profile_override(root: Path, path: Path) -> None:
    load_profile_payload("python", root)


@pytest.mark.parametrize(
    ("relative", "text", "expected", "read"),
    (
        (
            # frontmatter는 파일 2번째 줄에서 시작한다. 줄은 파일 기준이어야 사람이 찾는다.
            "skills/probe/SKILL.md",
            "---\nname: probe\nrequires: [a]\nrequires: [b]\n---\nbody\n",
            "{source}:4: duplicate key 'requires' (first at line 3)",
            _read_skill,
        ),
        (
            ".agent-flow/architecture.yaml",
            "schema_version: 1\narchitecture:\n  mode: clean\n  mode: pending\n",
            "{source}:4: duplicate key 'mode' (first at line 3)",
            _read_architecture,
        ),
        (
            ".agent-flow/profiles/python.local.yaml",
            "pr:\n  merge_strategy: squash\n  merge_strategy: merge\n",
            "{source}:3: duplicate key 'merge_strategy' (first at line 2)",
            _read_profile_override,
        ),
    ),
)
def test_duplicate_key_message_names_file_line_and_key(tmp_path, relative, text, expected, read):
    """반증: 키만 말하면 같은 이름이 여러 블록에 있는 파일에서 어느 줄을 고칠지 모른다.
    frontmatter처럼 파일 중간에서 시작하는 문서가 문서 기준 줄을 내면 한 줄씩 어긋난다.
    """
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        read(tmp_path, path)

    assert str(excinfo.value) == expected.format(source=path)


def _shipped_yaml() -> list[Path]:
    files = json.loads((KIT_ROOT / "package.json").read_text(encoding="utf-8"))["files"]
    found: list[Path] = []
    for entry in files:
        root = KIT_ROOT / entry
        if root.is_dir():
            found.extend(
                path
                for pattern in ("*.yaml", "*.yml")
                for path in root.rglob(pattern)
                if path.is_file()
            )
        elif root.suffix in (".yaml", ".yml") and root.is_file():
            found.append(root)
    return sorted(found)


def test_shipped_yaml_has_no_duplicate_keys():
    """반증: 배포 workflow·profile YAML의 중복 키는 설치된 모든 프로젝트에서 조용히
    뒤엣것으로 덮인다. 런타임 workflow 로더는 이 검사를 하지 않으므로 여기서 막는다.
    배포 목록(`package.json` `files`)에서 찾으므로 새 YAML도 따로 등록하지 않아도 검사된다.
    """
    shipped = _shipped_yaml()
    assert shipped, "package.json files 아래에서 YAML을 하나도 찾지 못했다"

    for path in shipped:
        source = str(path.relative_to(KIT_ROOT))
        loader = yaml.SafeLoader(path.read_text(encoding="utf-8"))
        try:
            node = loader.get_single_node()
            if node is not None:
                reject_duplicate_keys(node, source=source)
        finally:
            loader.dispose()
