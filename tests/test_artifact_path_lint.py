"""#131: run artifact의 로컬 절대 경로와 context-lint 검사 범위.

두 갈래를 한 파일에서 본다. (1) `write_gate_results`가 gate 출력의 절대 경로를
command와 **같은 규칙으로** 상대화한다. (2) `check-context-docs`가 gitignore된
artifact를 검사 대상에서 뺀다 — 커밋될 수 없는 파일은 경로를 흘릴 수 없다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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

# `scripts/check-context-docs.mjs`의 ABSOLUTE_PATH_RE. 검사기가 red로 만드는
# 조건을 그대로 재현해야 "정규화했다"가 검사기 기준으로 참인지 알 수 있다.
ABSOLUTE_PATH_RE = re.compile(
    r"(?<![\w.-])"
    r"(?:/Users/|/home/|/private/var/|/workspace/|/tmp/|/var/|/opt/|/mnt/|[A-Za-z]:[\\/])"
)

ARTIFACT_FAILURE = "absolute local path in Agent Flow artifact"


def _write_minimal_context_docs(root: Path) -> None:
    root.joinpath("CONTEXT.md").write_text(
        "# Context\n\n## Current Vocabulary\n\n- Project\n\n## Future Vocabulary\n\n- Worker\n",
        encoding="utf-8",
    )
    context_root = root / ".Codex" / "rules" / "context"
    context_root.mkdir(parents=True, exist_ok=True)
    records = []
    for name in (
        "domain-glossary-full.md",
        "research-context.md",
        "paper-runtime-context.md",
        "agent-flow-context-map.md",
        "context-maintenance.md",
    ):
        rel = f".Codex/rules/context/{name}"
        (context_root / name).write_text(f"# {name}\n\nMinimal context.\n", encoding="utf-8")
        records.append({"id": name, "path": rel, "summary": "Minimal context.", "parent": None})
    tree = root / ".Codex" / "context" / "tree.jsonl"
    tree.parent.mkdir(parents=True, exist_ok=True)
    tree.write_text("".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8")


def _seed_installed_project(root: Path, *, gitignore: str | None) -> Path:
    """설치형 레이아웃(`<root>/.agent-flow/scripts/`)의 git 저장소를 만든다."""
    scripts = root / ".agent-flow" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "check-context-docs.mjs", scripts / "check-context-docs.mjs")
    _write_minimal_context_docs(root)
    artifact = root / ".agent-flow" / "runs" / "default" / "run" / "artifacts" / "gate-results.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        json.dumps({"stdout": "ImportError while importing test module '/Users/dev/proj/tests/x.py'"}),
        encoding="utf-8",
    )
    if gitignore is not None:
        (root / ".gitignore").write_text(gitignore, encoding="utf-8")
    subprocess.run(("git", "init", "-q"), cwd=root, check=True, capture_output=True)
    return scripts / "check-context-docs.mjs"


def _run_lint(script: Path, root: Path) -> subprocess.CompletedProcess[str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    return subprocess.run(
        (node, str(script)), cwd=root, text=True, capture_output=True, check=False
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
    # 상대화 결과가 검사기를 통과해야 게이트가 자기 산출물로 red가 되지 않는다.
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
    leaked = checkout / "scripts" / "check-context-docs.mjs"

    path = write_gate_results(
        run_dir=run_dir,
        results=[GateResult("context-lint", ("node", "x.mjs"), False, 1, f"cannot read {leaked}\n", "")],
        cwd=checkout,
    )

    stdout = json.loads(path.read_text(encoding="utf-8"))["results"][0]["stdout"]
    assert stdout.strip() == "cannot read scripts/check-context-docs.mjs"


def test_command_and_output_normalization_share_one_rule(tmp_path):
    base = tmp_path.resolve()
    target = base / "scripts" / "check-context-docs.mjs"

    assert _recorded_gate_command(("node", str(target)), base)[1] == relativize_local_path(
        str(target), base
    )
    assert relativize_local_paths(f"see {target}", base) == "see scripts/check-context-docs.mjs"


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



def test_gitignored_artifact_is_not_linted(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    script = _seed_installed_project(root, gitignore=".agent-flow/\n")

    result = _run_lint(script, root)

    assert ARTIFACT_FAILURE not in result.stdout
    assert result.returncode == 0, result.stdout + result.stderr


def test_committable_artifact_still_fails_the_lint(tmp_path):
    """검사 범위를 줄여도 커밋 가능한 artifact는 계속 잡아야 한다."""
    root = tmp_path / "project"
    root.mkdir()
    script = _seed_installed_project(root, gitignore=None)

    result = _run_lint(script, root)

    assert result.returncode == 1
    assert f"{ARTIFACT_FAILURE}: .agent-flow/runs/default/run/artifacts/gate-results.json" in result.stdout
