"""사용 증거는 경로축만으로는 부족하다.

Skill tool로 skill을 쓰면 `SKILL.md` Read가 발생하지 않는다 — Claude Code 문서가
"does not re-read the skill file on later turns"라고 명시한다. 경로만 관측하면 정상
사용이 "한 번도 안 열었다"로 차단된다. 그래서 이름축을 함께 본다.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = str(REPO / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

HOOK = REPO / "scripts" / "hooks" / "record-skill-read.py"

from agent_flow.core.local_skills import missing_local_skill_markers, read_skill_evidence
from agent_flow.core.skill_resolver import PhaseSkills, ResolvedSkill


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".agent-flow").mkdir(parents=True)
    (root / ".agent-flow" / "kit.json").write_text("{}\n", encoding="utf-8")
    return root


def _run_hook(root: Path, payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"cwd": str(root), **payload}),
        capture_output=True,
        text=True,
        cwd=str(root),
        timeout=30,
    )


def _log(root: Path) -> list[dict]:
    path = root / ".agent-flow" / "skills-read.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_skill_tool_call_is_recorded_by_name(tmp_path):
    root = _project(tmp_path)

    result = _run_hook(
        root, {"tool_name": "Skill", "tool_input": {"skill": "compose-side-effects"}}
    )

    assert result.returncode == 0, result.stderr
    assert [entry["skill"] for entry in _log(root)] == ["compose-side-effects"]


def test_plugin_scoped_skill_records_the_bare_name(tmp_path):
    """plugin skill은 `<plugin>:<skill>`로 스코프된다. 카탈로그 키는 뒤쪽 이름이다."""
    root = _project(tmp_path)

    _run_hook(root, {"tool_name": "Skill", "tool_input": {"skill": "anthropic-skills:pdf"}})

    assert [entry["skill"] for entry in _log(root)] == ["pdf"]


def test_skill_uri_read_is_recorded_by_name(tmp_path):
    """OMP는 `read skill://<name>`으로 연다. 파일 경로가 오지 않는다."""
    root = _project(tmp_path)

    _run_hook(root, {"tool_name": "read", "tool_input": {"path": "skill://diagnose"}})

    assert [entry["skill"] for entry in _log(root)] == ["diagnose"]


def test_shell_read_of_a_skill_file_is_recorded(tmp_path):
    """Codex에는 Read tool이 없다. 셸로 열는 경로를 놓치면 그 host의 증거가 항상 비어 있다."""
    root = _project(tmp_path)
    skill = root / "skills" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# alpha\n", encoding="utf-8")

    _run_hook(root, {"tool_name": "Bash", "tool_input": {"command": f"cat {skill}"}})

    assert [entry["path"] for entry in _log(root)] == [str(skill.resolve())]


def test_unsafe_skill_name_is_not_recorded(tmp_path):
    """이름이 곧 로그 값이다. 경로 조각이 이름으로 들어오면 증거를 위조할 수 있다."""
    root = _project(tmp_path)

    _run_hook(root, {"tool_name": "Skill", "tool_input": {"skill": "../../etc/passwd"}})

    assert _log(root) == []


def test_evidence_covers_a_skill_used_through_the_skill_tool(tmp_path):
    root = _project(tmp_path)
    _run_hook(root, {"tool_name": "Skill", "tool_input": {"skill": "alpha"}})

    evidence = read_skill_evidence(root)

    assert evidence.available
    assert evidence.covers(
        ResolvedSkill(
            name="alpha",
            path=root / "skills" / "alpha" / "SKILL.md",
            source="project",
            exists=True,
        )
    )


def _gate(evidence_line: str) -> str:
    return (
        "## Completion Gate\n"
        "skill-availability: pass\n"
        f"{evidence_line}\n"
        "project-local-skills: checked\n"
        "project-local-skills-used: alpha\n"
        "project-local-skill-docs: applied\n"
        "missing-required-profile-skills: none\n"
    )


def _markers(root: Path, gate: str) -> list[str]:
    return missing_local_skill_markers(
        gate,
        root,
        "implement",
        phase_skills=PhaseSkills(required=("alpha",)),
        profile={},
    )


def test_legacy_read_evidence_key_is_rejected(tmp_path):
    """구 키를 계속 받아 주면 개명이 영구 별칭이 된다. 인정 키는 하나뿐이다."""
    root = _project(tmp_path)

    legacy = _markers(root, _gate("skill-read-evidence: unavailable"))
    current = _markers(root, _gate("skill-use-evidence: unavailable"))

    assert [item for item in legacy if item.startswith("skill-use-evidence")]
    assert not [item for item in current if item.startswith("skill-use-evidence")]


def test_reader_still_demands_an_evidence_marker_when_neither_key_is_present(tmp_path):
    root = _project(tmp_path)

    missing = _markers(root, _gate("skills-checked: true"))

    assert any(item.startswith("skill-use-evidence") for item in missing)


def test_shell_commands_that_do_not_read_are_not_evidence(tmp_path):
    """경로가 커맨드에 있다는 것만으로 인정하면 파일을 열지 않고 게이트를 통과할 수 있다."""
    root = _project(tmp_path)
    skill = root / "skills" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# alpha\n", encoding="utf-8")

    for command in (f"ls -la {skill}", f"stat {skill}", f"echo {skill}", f"rm -f {skill}.bak"):
        _run_hook(root, {"tool_name": "Bash", "tool_input": {"command": command}})

    assert _log(root) == []
