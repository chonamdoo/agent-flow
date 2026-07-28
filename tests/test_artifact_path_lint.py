"""#131: run artifact의 로컬 절대 경로를 상대 경로로 기록하는 계약."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SRC = str(REPO / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core.artifacts import write_gate_results
from agent_flow.core.gates import (
    GateResult,
    _recorded_gate_command,
    relativize_local_path,
    relativize_local_paths,
)

# artifact에서 제거해야 하는 운영체제별 로컬 절대 경로 접두사를 재현한다.
ABSOLUTE_PATH_RE = re.compile(
    r"(?<![\w.-])"
    r"(?:/Users/|/home/|/private/var/|/workspace/|/tmp/|/var/|/opt/|/mnt/|[A-Za-z]:[\\/])"
)



def test_gate_output_absolute_paths_become_repo_relative(tmp_path):
    root = tmp_path / "project"
    run_dir = root / ".agent-flow" / "runs" / "default" / "run"
    run_dir.mkdir(parents=True)
    root = root.resolve()
    inside = root / "src" / "agent_flow" / "cli.py"
    outside = tmp_path.resolve() / "pytest-tmp" / "case0"

    path = write_gate_results(
        run_dir=run_dir,
        results=[
            GateResult(
                "test",
                ("pytest", "-q"),
                False,
                1,
                f"ImportError while importing test module '{inside}'\n",
                f"tmp_path = PosixPath('{outside}')\n",
            )
        ],
    )

    recorded = json.loads(path.read_text(encoding="utf-8"))["results"][0]
    assert "src/agent_flow/cli.py" in recorded["stdout"]
    assert str(inside) not in recorded["stdout"]
    assert str(outside) not in recorded["stderr"]
    # 상대화 뒤에는 개발자 컴퓨터의 절대 경로가 남지 않아야 한다.
    assert ABSOLUTE_PATH_RE.search(recorded["stdout"]) is None
    assert ABSOLUTE_PATH_RE.search(recorded["stderr"]) is None
    # 구버전 리더가 읽는 legacy 사본도 같은 규칙을 받아야 한다.
    legacy = json.loads((run_dir / "gate-results.json").read_text(encoding="utf-8"))[0]
    assert legacy["stdout"] == recorded["stdout"]


def test_explicit_gate_cwd_overrides_the_derived_base(tmp_path):
    """`agent-flow gates`는 run_dir과 gate cwd가 갈릴 수 있어 기준을 직접 준다."""
    checkout = (tmp_path / "checkout").resolve()
    checkout.mkdir()
    run_dir = tmp_path / "runtime" / "run"
    run_dir.mkdir(parents=True)
    leaked = checkout / "scripts" / "verify.mjs"

    path = write_gate_results(
        run_dir=run_dir,
        results=[GateResult("lint", ("node", "x.mjs"), False, 1, f"cannot read {leaked}\n", "")],
        cwd=checkout,
    )

    stdout = json.loads(path.read_text(encoding="utf-8"))["results"][0]["stdout"]
    assert stdout.strip() == "cannot read scripts/verify.mjs"


def test_command_and_output_normalization_share_one_rule(tmp_path):
    base = tmp_path.resolve()
    target = base / "scripts" / "verify.mjs"

    assert _recorded_gate_command(("node", str(target)), base)[1] == relativize_local_path(
        str(target), base
    )
    assert relativize_local_paths(f"see {target}", base) == "see scripts/verify.mjs"


def test_foreign_platform_absolute_path_is_deidentified(tmp_path):
    """반증: 다른 플랫폼의 절대 경로를 그대로 두면 artifact가 계속 red다."""
    base = tmp_path.resolve()

    recorded = relativize_local_paths(r"tmpdir: D:\work\project\run.log", base)

    assert recorded == r"tmpdir: work\project\run.log"
    assert not ABSOLUTE_PATH_RE.search(recorded)


def test_unrelativizable_path_never_raises(tmp_path, monkeypatch):
    """Windows 교차 드라이브에서 relpath는 ValueError다. 그러면 결과가 안 남는다."""
    base = tmp_path.resolve()
    target = base / "run.log"

    def cross_drive(*_args, **_kwargs):
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    monkeypatch.setattr(os.path, "relpath", cross_drive)
    recorded = relativize_local_paths(f"see {target}", base)

    assert recorded == f"see {str(target).lstrip('/')}"
    assert not ABSOLUTE_PATH_RE.search(recorded)


