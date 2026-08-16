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
from agent_flow.core.skill_resolver import PhaseSkills


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


def test_a_skill_used_through_the_skill_tool_is_read_back_by_name(tmp_path):
    """hook이 쓴 이름이 진단 입력까지 이어지는지. 경로 대조는 이제 없다."""
    root = _project(tmp_path)
    _run_hook(root, {"tool_name": "Skill", "tool_input": {"skill": "alpha"}})

    evidence = read_skill_evidence(root)

    assert evidence.available
    assert "alpha" in evidence.used_names


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


# --- L2 계약: 강제는 자기신고, 관측은 진단 -----------------------------------
#
# 관측으로 막던 시절에는 read hook이 로드되지 않은 host 세션이 영원히 막혔다.
# 그 세션은 기록을 남길 수 없는데, 다른 세션이 만들어 둔 로그 파일 때문에 관측이
# "가능"으로 판정되어 자기가 만들 수 없는 marker를 요구받았다.


def _installed_skill(root: Path) -> Path:
    skill = root / "skills" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("# alpha\n", encoding="utf-8")
    return skill


def _stale_log(root: Path, *, at: float) -> None:
    """다른(예전) 세션이 남겨 둔 로그. 이번 phase 기록은 하나도 없는 상태다."""
    log = root / ".agent-flow" / "skills-read.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        json.dumps({"path": str(root / "skills" / "other" / "SKILL.md"), "at": at}) + "\n",
        encoding="utf-8",
    )


def _phase_markers(root: Path, gate: str, *, since: float | None = None) -> list[str]:
    return missing_local_skill_markers(
        gate,
        root,
        "implement",
        phase_skills=PhaseSkills(required=("alpha",)),
        profile={},
        since=since,
    )


def test_unread_required_skill_does_not_block_when_self_reported(tmp_path):
    """관측을 이유로 막지 않는다. 낮춘 지점 자신이다."""
    root = _project(tmp_path)
    _installed_skill(root)
    _run_hook(root, {"tool_name": "Skill", "tool_input": {"skill": "unrelated"}})

    assert _phase_markers(root, _gate("skill-use-evidence: verified")) == []


def test_missing_self_report_still_blocks(tmp_path):
    """뭐라도 신고하게 하는 강제는 남는다."""
    root = _project(tmp_path)
    _installed_skill(root)
    _run_hook(root, {"tool_name": "Skill", "tool_input": {"skill": "alpha"}})

    missing = _phase_markers(root, _gate("skills-checked: true"))

    assert [item for item in missing if item.startswith("skill-use-evidence")]


def test_blocker_points_at_the_session_when_the_phase_recorded_nothing(tmp_path):
    """로그는 읽히는데 이번 phase 기록이 0건이면 원인을 알려 준다."""
    root = _project(tmp_path)
    _installed_skill(root)
    started = 10_000.0
    _stale_log(root, at=started - 100)

    missing = _phase_markers(root, _gate("skills-checked: true"), since=started)

    joined = "\n".join(missing)
    assert "nothing was recorded during this phase" in joined
    assert "restart it" in joined


def test_the_diagnosis_is_not_glued_onto_a_marker_name(tmp_path):
    """반증: 산문을 marker 원소에 붙이면 그것을 그대로 옮겨 적은 artifact가 영구 차단된다.

    이 리스트는 runner가 "이 marker들을 artifact에 적어라"로 그대로 보여 주는
    계약 표면이다. 이 강제는 값을 열거(`verified`/`unavailable`)로 대조하므로
    `skill-use-evidence: verified|unavailable (…)`을 옮겨 적으면 값이 열거에 없어
    같은 자리에서 다시 막힌다. 그래서 marker 원소는 정확히 marker 이름이어야 하고,
    진단은 marker 모양(`key: value`)을 갖지 않는 별 원소로 나가야 한다.
    """
    root = _project(tmp_path)
    _installed_skill(root)
    started = 10_000.0
    _stale_log(root, at=started - 100)

    missing = _phase_markers(root, _gate("skills-checked: true"), since=started)

    marker = [item for item in missing if item.startswith("skill-use-evidence")]
    assert marker == ["skill-use-evidence: verified|unavailable"]
    diagnosis = next(item for item in missing if "nothing was recorded" in item)
    assert ":" not in diagnosis, diagnosis


def test_every_marker_shaped_element_carries_a_gate_key(tmp_path):
    """불변: 원소가 `key: value` 모양이면 그 key는 게이트가 실제로 읽는 marker 이름이다.

    반증: 위 테스트만 있으면 다른 층이 같은 실수를 반복해도 조용하다.
    """
    root = _project(tmp_path)
    _installed_skill(root)
    started = 10_000.0
    _stale_log(root, at=started - 100)

    missing = _phase_markers(root, _gate("skills-checked: true"), since=started)

    read_keys = {
        "skill-availability",
        "skill-use-evidence",
        "project-local-skills",
        "project-local-skills-used",
        "project-local-skill-docs",
        "missing-required-profile-skills",
    }
    for item in missing:
        key, separator, _value = item.partition(":")
        if separator:
            assert key in read_keys, item


def test_no_diagnosis_when_the_log_cannot_be_read(tmp_path):
    """관측할 수 없으면 진단하지 않는다. 없는 사실을 말하지 않는다."""
    root = _project(tmp_path)
    _installed_skill(root)

    missing = _phase_markers(root, _gate("skills-checked: true"))

    blocker = next(item for item in missing if item.startswith("skill-use-evidence"))
    assert blocker == "skill-use-evidence: verified|unavailable"


def test_hookless_session_passes_by_reporting_unavailable(tmp_path):
    """원래 결함의 직접 반증.

    이 세션에는 read hook이 없어 기록을 남길 수 없고, 로그 파일은 다른 세션이 이미
    만들어 뒀다. 예전에는 이 조합이 `unavailable` 자기신고를 무시하고 영원히 막았다.
    """
    root = _project(tmp_path)
    _installed_skill(root)
    started = 10_000.0
    _stale_log(root, at=started - 100)

    assert _phase_markers(root, _gate("skill-use-evidence: unavailable"), since=started) == []


# --- skill L2와 test evidence는 반대 계약이다 --------------------------------


def test_the_skill_and_command_evidence_layers_do_not_share_one_contract(tmp_path):
    """반증: `command_evidence` 모듈 docstring이 "L2와 같은 계약"이라고 적어 뒀다.

    skill 쪽 L2만 자기신고로 낮췄으므로 두 층은 이제 반대다. 관측이 가능한 같은
    조건(로그는 읽히고 이번 phase의 해당 기록은 0건)에서 답이 갈리는 것을 못 박고,
    docstring이 다시 둘을 같다고 말하지 않는지 함께 본다. 문서만 검사하면 강제가
    갈라져도 조용하고, 행동만 검사하면 문서가 거짓이어도 조용하다.
    """
    from agent_flow.core import command_evidence
    from agent_flow.core.command_evidence import missing_test_evidence_markers

    root = _project(tmp_path)
    _installed_skill(root)
    started = 10_000.0
    _stale_log(root, at=started - 100)
    # 실행 로그는 읽히지만 테스트 명령은 하나도 없다.
    commands = root / ".agent-flow" / "commands-run.jsonl"
    commands.write_text(
        json.dumps({"command": "git status", "cwd": str(root), "at": started + 1, "exit_code": 0})
        + "\n",
        encoding="utf-8",
    )

    # skill 쪽: 자기신고가 관측을 이긴다.
    assert _phase_markers(root, _gate("skill-use-evidence: verified"), since=started) == []
    # test 쪽: 관측이 자기신고를 이긴다.
    assert missing_test_evidence_markers(
        root,
        "implement",
        "## Completion Gate\ntest-run-evidence: verified\n",
        profile={},
        since=started,
        cwd_root=root,
    )

    doc = command_evidence.__doc__ or ""
    assert "L2와 같은 계약이다" not in doc
    assert "같은 계약이 아니다" in doc


def _clean_architecture_skill(root: Path) -> Path:
    skill = root / "skills" / "clean-architecture" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("# clean-architecture\n", encoding="utf-8")
    return skill


def test_the_architecture_marker_accepts_n_a_when_the_skill_is_not_required(tmp_path):
    """축소가 marker에서 되살아나면 안 된다.

    경계 경로를 건드리지 않는 변경은 계층 계약 문서를 required로 받지 않는다. 그
    상태에서 `clean-architecture: applied`를 강요하면 읽지 않은 것을 적게 된다.
    """
    root = _project(tmp_path)
    _installed_skill(root)

    missing = missing_local_skill_markers(
        _gate("skill-use-evidence: verified")
        + "clean-architecture: n/a\n"
        + "must-avoid-check: n/a\n",
        root,
        "implement",
        phase_skills=PhaseSkills(required=("alpha",)),
        profile={},
    )

    assert missing == []


def test_the_architecture_marker_rejects_n_a_when_the_skill_is_required(tmp_path):
    """불변: required인 문서를 `n/a`로 넘기면 그건 축소가 아니라 게이트 제거다."""
    root = _project(tmp_path)
    _installed_skill(root)
    _clean_architecture_skill(root)

    missing = missing_local_skill_markers(
        _gate("skill-use-evidence: verified")
        + "project-local-skills-used: alpha, clean-architecture\n"
        + "clean-architecture: n/a\n"
        + "must-avoid-check: n/a\n",
        root,
        "implement",
        phase_skills=PhaseSkills(required=("alpha", "clean-architecture")),
        profile={},
    )

    assert "clean-architecture: applied" in missing
    # `must-avoid-check`는 그 angle의 산출물이다. angle이 돌아야 하는 phase에서
    # `n/a`로 넘기면 리뷰가 없었다는 사실이 조용해진다.
    assert "must-avoid-check: pass|fail" in missing
