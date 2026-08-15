"""hook 로그 fail-open, ambient git env 차단, 원자적 쓰기의 경계 조건.

세 가지 모두 "정상 경로는 이미 통과한다"가 아니라 "비정상 입력에서 무너지지
않는다"를 본다. 여기 있는 테스트는 고치기 전 코드에서 실패한다.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT_ROOT / "src"))

from agent_flow.core.architecture_lint import changed_files  # noqa: E402
from agent_flow.core.artifacts import write_gate_results, write_handoff  # noqa: E402
from agent_flow.core.atomic_io import atomic_write_text  # noqa: E402
from agent_flow.core.command_evidence import (  # noqa: E402
    COMMANDS_RUN_LOG,
    missing_test_evidence_markers,
    read_command_evidence,
)
from agent_flow.core.gates import GateResult  # noqa: E402
from agent_flow.core.local_skills import SKILLS_READ_LOG, read_skill_evidence  # noqa: E402
from agent_flow.core.phase_workflow import load_phase_workflow_definition  # noqa: E402
from agent_flow.core.worktree_isolation import (  # noqa: E402
    WorktreeIsolationError,
    resolve_run_subpath,
    write_run_subpath_text,
)
from agent_flow.runner import GATE_MALFORMED, Phase, Runner  # noqa: E402


# UTF-8로 디코드할 수 없는 바이트열. hook은 O_APPEND로 쓰므로 프로세스가 줄
# 중간에 죽으면 이런 잔재가 실제로 남는다.
_CORRUPT = b"\xff\xfe not utf-8\n"


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(("git", *args), cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(root: Path, marker: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-q", "-m", "base", cwd=root)
    (root / marker).write_text("x\n", encoding="utf-8")


def _write_log(path: Path, chunks: list[bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(chunks))


def test_corrupt_bytes_do_not_kill_command_evidence(tmp_path: Path) -> None:
    log = tmp_path / COMMANDS_RUN_LOG
    _write_log(
        log,
        [
            json.dumps({"command": "pytest -q", "at": 1.0, "exit_code": 0}).encode() + b"\n",
            _CORRUPT,
            json.dumps({"command": "ruff check .", "at": 2.0, "exit_code": 0}).encode() + b"\n",
        ],
    )

    evidence = read_command_evidence(tmp_path)

    assert evidence.available is True
    assert [run.command for run in evidence.runs] == ["pytest -q", "ruff check ."]


def test_corrupt_bytes_do_not_kill_skill_read_evidence(tmp_path: Path) -> None:
    log = tmp_path / SKILLS_READ_LOG
    _write_log(
        log,
        [
            json.dumps({"path": "/skills/alpha/SKILL.md", "at": 1.0}).encode() + b"\n",
            _CORRUPT,
            json.dumps({"path": "/skills/beta/SKILL.md", "at": 2.0}).encode() + b"\n",
        ],
    )

    evidence = read_skill_evidence(tmp_path)

    assert evidence.available is True
    assert evidence.read_paths == frozenset(
        {"/skills/alpha/SKILL.md", "/skills/beta/SKILL.md"}
    )


def test_missing_command_log_stays_unavailable(tmp_path: Path) -> None:
    # fail-open은 "손상을 무시한다"까지다. 파일이 없는 host는 여전히 관측 불가여야
    # 하고, 그 구분이 사라지면 hook 미지원 host에서 phase가 통째로 막힌다.
    assert read_command_evidence(tmp_path).available is False


def test_ambient_git_env_cannot_redirect_changed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    decoy = tmp_path / "decoy"
    _init_repo(target, "alpha.kt")
    _init_repo(decoy, "beta.kt")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))

    files = changed_files(target)

    assert "alpha.kt" in files
    assert "beta.kt" not in files


def test_evidence_from_a_sibling_checkout_is_not_this_run(tmp_path: Path) -> None:
    project_root = tmp_path / "leader"
    sibling = tmp_path / "sibling"
    project_root.mkdir()
    sibling.mkdir()
    _write_log(
        project_root / COMMANDS_RUN_LOG,
        [
            json.dumps(
                {"command": "pytest -q", "at": 1.0, "exit_code": 1, "cwd": str(sibling)}
            ).encode()
            + b"\n",
        ],
    )

    markers = missing_test_evidence_markers(
        project_root, "red", "", cwd_root=project_root
    )

    assert markers and markers[0].startswith("test-run-evidence:")


def test_failed_atomic_write_leaves_the_old_file_and_no_temp(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("previous\n", encoding="utf-8")

    with pytest.raises(UnicodeEncodeError):
        # ascii로 인코딩할 수 없는 내용이라 임시 파일에 쓰는 도중 터진다.
        atomic_write_text(target, "한글\n", encoding="ascii")

    assert target.read_text(encoding="utf-8") == "previous\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]


def _handoff(root: Path, run_dir: Path, from_stage: str) -> Path:
    return write_handoff(
        root=root,
        run_dir=run_dir,
        from_stage=from_stage,
        to_stage="review",
        decided="",
        rejected="",
        risks="",
        files="",
        remaining="",
    )


def test_handoff_stage_names_cannot_escape_the_run(tmp_path: Path) -> None:
    """반증: stage 이름은 run 안팎 두 경로의 마지막 컴포넌트가 된다. 검증 없이
    쓰면 `mkdir -p`가 `..`를 따라 run 밖에 디렉터리를 파고 그 자리에 쓴다.
    """
    root = tmp_path / "project"
    run_dir = root / ".agent-flow" / "runs" / "run-1"
    run_dir.mkdir(parents=True)

    with pytest.raises(ValueError):
        _handoff(root, run_dir, "../../../../outside/pwned")

    assert not (tmp_path / "outside").exists()
    assert list(run_dir.iterdir()) == []


def test_handoff_still_writes_a_normal_stage_pair(tmp_path: Path) -> None:
    # 봉쇄가 정상 이름까지 막으면 handoff 명령 자체가 죽는다.
    root = tmp_path / "project"
    run_dir = root / ".agent-flow" / "runs" / "run-1"
    run_dir.mkdir(parents=True)

    written = _handoff(root, run_dir, "implement")

    assert written == run_dir / "handoffs" / "implement-to-review.md"
    assert written.read_text(encoding="utf-8").startswith("# Handoff: implement -> review")
    assert (root / ".agent-flow" / "handoffs" / "implement-to-review.md").exists()


def test_gate_results_refuse_a_symlinked_artifacts_directory(tmp_path: Path) -> None:
    """반증: target의 **자기 부모**를 run root라고 넘기면 직계-자식 단언이 항상
    참이 되어, `artifacts/`가 run 밖을 가리키는 symlink여도 그대로 따라간다.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (run_dir / "artifacts").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorktreeIsolationError):
        write_gate_results(
            run_dir=run_dir,
            results=[
                GateResult(
                    gate_id="lint",
                    command=("true",),
                    passed=True,
                    exit_code=0,
                    stdout="",
                    stderr="",
                )
            ],
        )

    assert list(outside.iterdir()) == []


def test_run_subpath_write_refuses_a_traversal_target(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with pytest.raises(WorktreeIsolationError):
        write_run_subpath_text(run_dir, run_dir / "handoffs" / ".." / ".." / "escaped.md", "x\n")

    assert not (tmp_path / "escaped.md").exists()
    # 거부는 부작용 없이 끝나야 한다. 봉쇄 전에 디렉터리를 파면 이미 진 것이다.
    assert list(run_dir.iterdir()) == []


def test_run_subpath_write_creates_nested_artifacts_inside_the_run(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    target = run_dir / "handoffs" / "a-to-b.md"

    write_run_subpath_text(run_dir, target, "body\n")

    assert target.read_text(encoding="utf-8") == "body\n"


def test_run_subpath_write_creates_the_run_directory_on_demand(tmp_path: Path) -> None:
    """`agent-flow gates --run-dir <아직 없는 경로>`는 첫 artifact와 함께 run
    디렉터리를 만든다. 봉쇄 대상은 호출부가 준 run root가 아니라 그 안의 경로다.
    """
    run_dir = tmp_path / ".agent-flow" / "runs" / "manual"
    target = run_dir / "artifacts" / "gate-results.json"

    write_run_subpath_text(run_dir, target, "{}\n")

    assert target.read_text(encoding="utf-8") == "{}\n"


def test_resolve_run_subpath_bounds_a_path_without_creating_anything(tmp_path: Path) -> None:
    # 삭제·읽기 쪽 호출부가 봉쇄 규칙을 다시 적지 않도록 공개한 자리다.
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    assert resolve_run_subpath(run_dir, run_dir / "a" / "b.md") == run_dir.resolve() / "a" / "b.md"
    assert list(run_dir.iterdir()) == []

    with pytest.raises(WorktreeIsolationError):
        resolve_run_subpath(run_dir, run_dir / ".." / "escaped.md")


def test_resolve_run_subpath_refuses_a_symlinked_subdirectory(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (run_dir / "handoffs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorktreeIsolationError):
        resolve_run_subpath(run_dir, run_dir / "handoffs" / "x.md")


def test_run_subpath_write_refuses_a_symlinked_ancestor_before_creating_anything(
    tmp_path: Path,
) -> None:
    """반증: 아직 없는 디렉터리라고 검증을 건너뛰면, `mkdir(parents=True)`가 run
    안의 symlink 조상을 따라가 run **밖에** 디렉터리를 실제로 만든 뒤에야 거부가
    일어난다. 거부한 쓰기가 부작용을 남기면 봉쇄가 아니다.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (run_dir / "nested").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorktreeIsolationError):
        write_run_subpath_text(run_dir, run_dir / "nested" / "new" / "f.md", "x\n")

    assert list(outside.iterdir()) == []


def test_run_subpath_write_still_creates_a_deep_missing_directory_chain(
    tmp_path: Path,
) -> None:
    """symlink 조상 봉쇄가 정상 경로를 잡아먹으면 안 된다. 아직 없는 중첩
    디렉터리를 파며 쓰는 것은 `artifacts/gate-results.json`의 정당한 사용이다.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    target = run_dir / "artifacts" / "gates" / "nested" / "results.json"

    write_run_subpath_text(run_dir, target, "{}\n")

    assert target.read_text(encoding="utf-8") == "{}\n"



def test_atomic_write_keeps_the_existing_file_mode(tmp_path: Path) -> None:
    """반증: `tempfile.mkstemp`가 만든 0600을 `os.replace`로 실어 보내면 사용자가
    넓혀 둔(또는 좁혀 둔) 기존 권한이 쓸 때마다 조용히 갈린다.
    """
    target = tmp_path / "state.json"
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o640)

    atomic_write_text(target, "new\n")

    assert target.read_text(encoding="utf-8") == "new\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_atomic_write_creates_new_files_under_the_umask(tmp_path: Path) -> None:
    """반증: 신규 파일을 0600으로 고정하면 JS 쌍둥이와 답이 갈린다. 두 writer는
    같은 이름을 쓰고 parity가 쌍으로 묶으므로 권한 정책도 하나여야 한다.
    """
    target = tmp_path / "fresh.json"
    previous = os.umask(0o022)
    try:
        atomic_write_text(target, "x\n")
    finally:
        os.umask(previous)

    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_atomic_write_does_not_inherit_a_symlink_mode(tmp_path: Path) -> None:
    # `os.replace`가 갈아 끼우는 것은 링크 자신이다. 링크의 0777을 물려받으면
    # 아무도 요구하지 않은 권한 확대가 된다.
    real = tmp_path / "real.json"
    real.write_text("old\n", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(real)

    previous = os.umask(0o022)
    try:
        atomic_write_text(link, "new\n")
    finally:
        os.umask(previous)

    assert not link.is_symlink()
    assert stat.S_IMODE(link.stat().st_mode) == 0o644
    assert real.read_text(encoding="utf-8") == "old\n"


def test_atomic_write_creates_the_parent_directory_like_its_js_twin(tmp_path: Path) -> None:
    """반증: JS 쌍둥이는 `mkdirSync(recursive)`를 하고 docstring은 두 writer가 같은
    규칙을 따른다고 말한다. Python만 `FileNotFoundError`를 내면, parity를 믿고
    부르는 다음 호출자가 자기 자리에 mkdir를 한 벌 더 적는다.
    """
    target = tmp_path / "nested" / "deeper" / "state.json"

    atomic_write_text(target, "{}\n")

    assert target.read_text(encoding="utf-8") == "{}\n"


def _route_runner(run_dir: Path, phase: Phase) -> Runner:
    """route 판정에 필요한 최소 조립. `_next_index`는 run_dir과 phases만 본다."""
    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.phases = [phase, Phase(id="fix-loop", description="fix")]
    return runner


def test_undecodable_route_bytes_do_not_kill_the_run(tmp_path: Path) -> None:
    """반증: route 입력을 `read_text(encoding="utf-8")`로 읽으면 잘못된 바이트 하나가
    `UnicodeDecodeError`(=`ValueError`)로 `Runner.run()`까지 올라가 run이 사유 없는
    오류로 멈춘다. hook 로그·원장과 같은 부류의 fail-open 자리다.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    phase = Phase(
        id="review",
        description="review",
        routes={"approve": "", "request-changes": "fix-loop"},
    )
    (run_dir / "review.md").write_bytes(b"verdict: approve\n" + _CORRUPT)
    runner = _route_runner(run_dir, phase)

    decision = runner._next_index(0, phase)

    assert (decision.to_index, decision.blocked, decision.route_key) == (1, False, "approve")


def test_an_unreadable_gate_results_file_blocks_instead_of_raising(tmp_path: Path) -> None:
    """반증: 존재하지만 열 수 없는 결과 파일(여기서는 같은 이름의 디렉터리)은
    `OSError`로 run을 세웠다. 판정 불가는 예외가 아니라 blocked이고, 사유는
    이번에 만든 malformed-results다.
    """
    run_dir = tmp_path / "run"
    (run_dir / "artifacts" / "gate-results.json").mkdir(parents=True)
    phase = Phase(
        id="gates",
        description="gates",
        routes={"green": "", "default": "fix-loop"},
        artifact="artifacts/gate-results.json",
    )
    runner = _route_runner(run_dir, phase)

    decision = runner._next_index(0, phase)

    assert (decision.to_index, decision.blocked, decision.route_key) == (0, True, GATE_MALFORMED)


def _workflow_kit(root: Path, body: str) -> Path:
    workflows = root / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / "default.yaml").write_text(body, encoding="utf-8")
    return root


def test_a_traversing_phase_artifact_is_rejected_at_load(tmp_path: Path) -> None:
    """반증: `artifact`를 자유 문자열로 받으면 `artifact: ../../escape.md`가 loader를
    통과해 run이 **시작된 뒤** 쓰기 시점에야 드러난다. 형제 필드(phase/skill 이름,
    workflow 파일)는 전부 compile 시점에 봉쇄된다. 여기도 그래야 한다.
    """
    kit_root = _workflow_kit(
        tmp_path,
        "id: default\n"
        "phases:\n"
        "  - id: only\n"
        "    description: d\n"
        "    artifact: ../../escape.md\n",
    )

    with pytest.raises(ValueError, match="artifact"):
        load_phase_workflow_definition(kit_root, "default")


@pytest.mark.parametrize(
    "artifact",
    ["/etc/passwd", "C:/tmp/escape.md", "a\\b.md", "", "artifacts/", "./escape.md"],
)
def test_absolute_and_degenerate_phase_artifacts_are_rejected_at_load(
    tmp_path: Path, artifact: str
) -> None:
    kit_root = _workflow_kit(
        tmp_path,
        "id: default\n"
        "phases:\n"
        "  - id: only\n"
        "    description: d\n"
        f"    artifact: {artifact!r}\n",
    )

    with pytest.raises(ValueError, match="artifact"):
        load_phase_workflow_definition(kit_root, "default")


def test_a_nested_relative_phase_artifact_still_loads(tmp_path: Path) -> None:
    """불변: `artifacts/gate-results.json`은 배포된 정의가 실제로 쓰는 값이다."""
    kit_root = _workflow_kit(
        tmp_path,
        "id: default\n"
        "phases:\n"
        "  - id: gates\n"
        "    description: d\n"
        "    artifact: artifacts/gate-results.json\n",
    )

    definition = load_phase_workflow_definition(kit_root, "default")

    assert definition.phases[0].artifact == "artifacts/gate-results.json"


def test_every_shipped_workflow_still_loads() -> None:
    """불변: 새 검증이 배포된 정의를 하나라도 막으면 harness 전체가 죽는다."""
    names = sorted(
        path.stem for path in (KIT_ROOT / "src" / "agent_flow" / "workflows").glob("*.yaml")
    )

    assert names, "배포된 workflow를 못 찾았으면 이 테스트는 아무것도 보지 않는다"
    for name in names:
        definition = load_phase_workflow_definition(KIT_ROOT, name)
        assert definition.phases, name
        for phase in definition.phases:
            assert phase.artifact and not phase.artifact.startswith("/"), (name, phase.id)


def test_an_automatic_artifact_cannot_be_written_outside_the_run(tmp_path: Path) -> None:
    """반증: `_write_automatic_artifact`는 `run_dir / phase.artifact`에 맨
    `mkdir(parents=True)` + `write_text`를 했다. 같은 변경에서 `_apply_transition`과
    generic adapter는 정본 writer로 옮겼는데 이 자리만 봉쇄 밖에 남아 있었다.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    project_root = tmp_path / "proj"
    project_root.mkdir()
    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.project_root = project_root
    runner.architecture = "default"
    phase = Phase(id="commit", description="commit", artifact="../../escape.md")

    with pytest.raises(WorktreeIsolationError):
        runner._write_automatic_artifact(phase)

    assert not (tmp_path / "escape.md").exists()
    assert not (tmp_path.parent / "escape.md").exists()
    assert list(run_dir.iterdir()) == []


def test_an_automatic_artifact_still_lands_in_a_nested_run_subdirectory(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    project_root = tmp_path / "proj"
    project_root.mkdir()
    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.project_root = project_root
    runner.architecture = "default"
    phase = Phase(id="commit", description="commit", artifact="artifacts/commit.md")

    assert runner._write_automatic_artifact(phase) is True

    written = (run_dir / "artifacts" / "commit.md").read_text(encoding="utf-8")
    assert "status: skipped" in written
