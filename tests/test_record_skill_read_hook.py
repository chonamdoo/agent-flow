"""Read hook을 **프로세스로** 구동한다.

`_strip_selector`를 함수로만 부르는 테스트는 hook이 import 단계에서 죽어도
통과한다. 실제로 그런 일이 있었다 — 상수 하나가 사라져 모든 Read에서
`NameError`가 났는데 전체 스위트와 parity가 그대로 통과했다.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "scripts" / "hooks" / "record-skill-read.py"


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".agent-flow").mkdir(parents=True)
    (root / ".agent-flow" / "kit.json").write_text("{}\n", encoding="utf-8")
    skill = root / "skills" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# alpha\n", encoding="utf-8")
    return root


def _invoke(root: Path, file_path: str, tool: str = "Read") -> subprocess.CompletedProcess:
    payload = {"tool_name": tool, "cwd": str(root), "tool_input": {"file_path": file_path}}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(root),
        timeout=30,
    )


def _log(root: Path) -> list[dict]:
    path = root / ".agent-flow" / "skills-read.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_hook_records_a_plain_read(tmp_path):
    """불변: hook은 실제로 실행돼서 기록을 남긴다."""
    root = _project(tmp_path)
    skill = root / "skills" / "alpha" / "SKILL.md"
    result = _invoke(root, str(skill))
    assert result.returncode == 0, result.stderr
    assert result.stderr == "", result.stderr
    assert [entry["path"] for entry in _log(root)] == [str(skill.resolve())]


@pytest.mark.parametrize(
    "selector",
    [":10", ":10-40", ":50-", ":1-5,10-20", ":50+150", ":raw", ":raw:2-4", ":2-4:raw", ":conflicts"],
)
def test_hook_strips_read_selectors(tmp_path, selector):
    """불변: 읽기 선택자가 붙어도 같은 skill로 기록된다."""
    root = _project(tmp_path)
    skill = root / "skills" / "alpha" / "SKILL.md"
    result = _invoke(root, f"{skill}{selector}")
    assert result.returncode == 0, result.stderr
    assert [entry["path"] for entry in _log(root)] == [str(skill.resolve())]


@pytest.mark.parametrize("suffix", [".bak", ".orig", "~", "x"])
def test_hook_does_not_record_sibling_files(tmp_path, suffix):
    """반증: `SKILL.md.bak`을 읽고 형제 `SKILL.md`로 기록되면 게이트가 위조된다."""
    root = _project(tmp_path)
    decoy = root / "skills" / "alpha" / f"SKILL.md{suffix}"
    decoy.write_text("decoy\n", encoding="utf-8")
    result = _invoke(root, str(decoy))
    assert result.returncode == 0, result.stderr
    assert _log(root) == []


def test_hook_ignores_non_read_tools(tmp_path):
    root = _project(tmp_path)
    skill = root / "skills" / "alpha" / "SKILL.md"
    result = _invoke(root, str(skill), tool="Write")
    assert result.returncode == 0, result.stderr
    assert _log(root) == []


def test_hook_never_blocks_on_garbage_input(tmp_path):
    """불변: 관측 전용이다. 어떤 입력에도 exit 0이고 stderr를 더럽히지 않는다."""
    root = _project(tmp_path)
    for payload in ("", "not json", "[]", '{"tool_name":"Read"}', '{"tool_name":"Read","tool_input":{}}'):
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input=payload, capture_output=True, text=True, cwd=str(root), timeout=30,
        )
        assert result.returncode == 0, (payload, result.stderr)
        assert result.stderr == "", (payload, result.stderr)


def test_hook_does_not_record_a_nonexistent_skill(tmp_path):
    """반증: 열지도 못한 경로가 증거가 되면 게이트를 위조할 수 있다."""
    root = _project(tmp_path)
    ghost = root / "skills" / "ghost" / "SKILL.md"
    result = _invoke(root, str(ghost))
    assert result.returncode == 0, result.stderr
    assert _log(root) == []


def test_hook_does_not_record_a_directory_named_skill_md(tmp_path):
    root = _project(tmp_path)
    weird = root / "skills" / "dir" / "SKILL.md"
    weird.mkdir(parents=True)
    result = _invoke(root, str(weird))
    assert result.returncode == 0, result.stderr
    assert _log(root) == []
