"""수치 대조 gate.

관측자는 git이다. 원장은 agent가 쓰지만 diff는 아니다. 그래서 테스트도
"원장에 16dp라고 쓰고 12dp를 구현했다"를 반증한다.

토큰 경유(`Spacing.m`)를 위반으로 들면 정상 구현이 fix-loop에 갇힌다. 그래서
토큰은 금지가 아니라 명시를 요구하고, 명시한 이름이 diff에 있는지는 다시 git이
판정한다 — 그 경계도 함께 반증한다.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = str(REPO / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core.design_ledger import capture_design_ledger
from agent_flow.core.design_value_check import (
    declared_tokens,
    missing_design_value_implementations,
)

LEDGER_SOURCE = """## Design Values

horizontal-padding: 16dp
brand-primary: #FF6B00
"""

GATE = "## Completion Gate\n\nverdict: approve\n"


def _git(*args, cwd):
    return subprocess.run(("git", *args), cwd=str(cwd), capture_output=True, text=True, check=True)


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _git("init", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-m", "init", cwd=root)
    return root


@pytest.fixture()
def run_dir(tmp_path):
    path = tmp_path / "run"
    path.mkdir()
    capture_design_ledger(path, "prd", LEDGER_SOURCE)
    return path


def _write_code(project: Path, body: str) -> None:
    (project / "Screen.kt").write_text(body, encoding="utf-8")


def test_literal_values_in_the_diff_pass(project, run_dir):
    _write_code(project, "val pad = 16.dp // 16dp\nval brand = Color(0xFF6B00) // #FF6B00\n")
    assert missing_design_value_implementations(project, run_dir, "final-review", GATE) == []


def test_wrong_value_is_reported(project, run_dir):
    """반증: 16dp를 보고 12dp를 쓰면 아무도 안 잡던 자리다."""
    _write_code(project, "val pad = 12.dp\nval brand = Color(0xFF6B00) // #FF6B00\n")
    missing = missing_design_value_implementations(project, run_dir, "final-review", GATE)
    assert missing and "horizontal-padding=16dp" in missing[0]
    assert "brand-primary" not in missing[0]


def test_hex_color_case_is_ignored(project, run_dir):
    _write_code(project, "val pad = 16dp\nval brand = 0xff6b00 // #ff6b00\n")
    assert missing_design_value_implementations(project, run_dir, "final-review", GATE) == []


def test_declared_token_present_in_the_diff_passes(project, run_dir):
    """`Spacing.m`(=16dp)을 위반으로 들면 정상 구현이 fix-loop에 갇힌다."""
    _write_code(project, "val pad = Spacing.m\nval brand = BrandColors.primary\n")
    text = GATE + "design-values-implemented: horizontal-padding=Spacing.m, brand-primary=BrandColors.primary\n"
    assert missing_design_value_implementations(project, run_dir, "final-review", text) == []


def test_declared_token_absent_from_the_diff_is_reported(project, run_dir):
    """반증: 토큰 이름을 대는 것만으로 통과하면 그건 다시 자기신고다."""
    _write_code(project, "val pad = 4.dp\nval brand = 0xFF6B00\n")
    text = GATE + "design-values-implemented: horizontal-padding=Spacing.m\n"
    missing = missing_design_value_implementations(project, run_dir, "final-review", text)
    assert missing and "declared token is not in the diff" in missing[0]


def test_untouched_code_does_not_count_as_evidence(project, run_dir):
    """반증: 원래부터 있던 값이 증거가 되면 코드 0줄로도 통과한다."""
    (project / "Theme.kt").write_text("val pad = 16dp\nval brand = #FF6B00\n", encoding="utf-8")
    _git("add", ".", cwd=project)
    _git("commit", "-m", "pre-existing", cwd=project)
    _write_code(project, "val nothing = 1\n")
    missing = missing_design_value_implementations(project, run_dir, "final-review", GATE)
    assert missing and "horizontal-padding=16dp" in missing[0]


def test_committed_work_on_a_branch_still_counts(project, run_dir):
    """작업이 이미 커밋됐다고 증거가 사라지면 안 된다. merge-base부터 본다."""
    _git("checkout", "-b", "feat/x", cwd=project)
    _write_code(project, "val pad = 16dp\nval brand = #FF6B00\n")
    _git("add", ".", cwd=project)
    _git("commit", "-m", "impl", cwd=project)
    assert missing_design_value_implementations(project, run_dir, "final-review", GATE) == []


def test_other_phases_are_not_checked(project, run_dir):
    _write_code(project, "val nothing = 1\n")
    assert missing_design_value_implementations(project, run_dir, "green", GATE) == []


def test_empty_ledger_checks_nothing(project, tmp_path):
    empty = tmp_path / "empty-run"
    empty.mkdir()
    capture_design_ledger(empty, "prd", "## Design Values\n\n")
    _write_code(project, "val nothing = 1\n")
    assert missing_design_value_implementations(project, empty, "final-review", GATE) == []


def test_non_git_project_degrades(tmp_path, run_dir):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert missing_design_value_implementations(plain, run_dir, "final-review", GATE) == []


def test_declared_tokens_parsing():
    text = "## Completion Gate\n\ndesign-values-implemented: a=Spacing.m, b=Brand.primary\n"
    assert declared_tokens(text) == {"a": "Spacing.m", "b": "Brand.primary"}
    assert declared_tokens("## Completion Gate\n\ndesign-values-implemented: none\n") == {}
