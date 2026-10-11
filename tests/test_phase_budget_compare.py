"""`evals/phase_budget_compare.py`는 PR CI의 판정자다. 놓치면 회귀가 초록으로 지나간다."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("phase_budget_compare", REPO / "evals" / "phase_budget_compare.py")
assert _SPEC is not None and _SPEC.loader is not None
compare_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(compare_module)


def _phase(**overrides) -> dict:
    phase = {
        "author_bytes": 1000,
        "required": ["code-generation-discipline", "tdd"],
        "required_read_bytes": 2000,
        "roles_in_envelope": False,
        "read_plan_in_envelope": True,
    }
    phase.update(overrides)
    return phase


def _review_phase() -> dict:
    return _phase(
        required=["code-review"],
        read_plan_in_envelope=False,
        reviewer_prompt_bytes_by_provider={"claude": [500, 500], "codex": [500, 500]},
        reviewer_read_bytes_by_provider={"claude": 300, "codex": 300},
    )


def _result() -> dict:
    phases = {"green": _phase(), "multi-review": _review_phase()}
    return {
        "results": [{
            "profile": "react-native",
            "mode": "local",
            "conditions": {
                "layer": {"changed_files": [], "phases": copy.deepcopy(phases)},
                "committed": {"changed_files": [], "phases": copy.deepcopy(phases)},
            },
        }],
    }


def _write_measurements(tmp_path: Path, before: dict, after: dict) -> tuple[Path, Path]:
    before_path, after_path = tmp_path / "before.json", tmp_path / "after.json"
    before_path.write_text(json.dumps(before), encoding="utf-8")
    after_path.write_text(json.dumps(after), encoding="utf-8")
    return before_path, after_path


def _run(tmp_path: Path, before: dict, after: dict) -> list[str]:
    before_path, after_path = _write_measurements(tmp_path, before, after)
    _, failures = compare_module.compare(before_path, after_path)
    return failures


def _layer(result: dict) -> dict:
    return result["results"][0]["conditions"]["layer"]["phases"]


def test_identical_measurements_pass(tmp_path):
    assert _run(tmp_path, _result(), _result()) == []


def test_a_lost_required_skill_fails_even_when_bytes_shrink(tmp_path):
    after = _result()
    after["results"][0]["conditions"]["committed"]["phases"]["green"]["required"] = ["tdd"]
    after["results"][0]["conditions"]["committed"]["phases"]["green"]["author_bytes"] = 10
    failures = _run(tmp_path, _result(), after)
    assert any("green/code-generation-discipline" in item for item in failures)


@pytest.mark.parametrize("condition", ["layer", "committed"])
def test_clean_roles_in_a_non_clean_envelope_fail_in_any_condition(tmp_path, condition):
    after = _result()
    after["results"][0]["conditions"][condition]["phases"]["green"]["roles_in_envelope"] = True
    failures = _run(tmp_path, _result(), after)
    assert any(f"react-native:local/{condition}/green" in item for item in failures)


@pytest.mark.parametrize("condition", ["layer", "committed"])
def test_growth_in_one_condition_fails_even_if_another_shrinks(tmp_path, condition):
    """반증: 합계만 보면 커밋 후 조건의 증가가 계층 경로 조건의 감소에 묻힌다."""
    after = _result()
    other = "committed" if condition == "layer" else "layer"
    phases = after["results"][0]["conditions"]
    phases[condition]["phases"]["multi-review"]["reviewer_prompt_bytes_by_provider"]["codex"] = [900, 900]
    phases[other]["phases"]["green"]["author_bytes"] = 1
    failures = _run(tmp_path, _result(), after)
    assert any(f"total bytes grew in `{condition}`" in item for item in failures)
    assert not any(f"`{other}`" in item for item in failures)


@pytest.mark.parametrize("breakage", ["error", "missing"])
def test_a_broken_head_phase_is_not_read_as_a_saving(tmp_path, breakage):
    """반증: 리뷰어 job 생성이 실패한 phase는 리뷰어 바이트 0으로 들어가 절감으로 보인다."""
    after = _result()
    if breakage == "error":
        review = _layer(after)["multi-review"]
        review.pop("reviewer_prompt_bytes_by_provider")
        review.pop("reviewer_read_bytes_by_provider")
        review["error"] = "RuntimeError: reviewer jobs failed"
    else:
        del _layer(after)["multi-review"]
    failures = _run(tmp_path, _result(), after)
    assert any("head measurement incomplete" in item and "layer/multi-review" in item for item in failures)


def test_a_combination_that_stopped_installing_fails(tmp_path):
    after = {"results": [{"profile": "react-native", "mode": "local", "install_error": "refused"}]}
    assert any("measured before, not after" in item for item in _run(tmp_path, _result(), after))


@pytest.mark.parametrize("failure_kind", ["install_error", "worker_error", None], ids=["install", "worker", "empty"])
def test_a_base_that_measured_nothing_cannot_pass(tmp_path, monkeypatch, capsys, failure_kind):
    before = {"results": []}
    if failure_kind is not None:
        before["results"].append({"profile": "react-native", "mode": "local", failure_kind: "baseline failed"})
    before_path, after_path = _write_measurements(tmp_path, before, _result())

    lines, failures = compare_module.compare(before_path, after_path)

    assert len(failures) == 1
    assert "base measured nothing" in failures[0]
    if failure_kind is not None:
        assert "react-native:local" in failures[0]
        assert "baseline failed" in failures[0]
    else:
        assert "no measurement rows" in failures[0]
    assert "### Findings" in lines
    assert "No skill-delivery regression found." not in lines
    monkeypatch.setattr("sys.argv", ["phase_budget_compare.py", str(before_path), str(after_path)])
    assert compare_module.main() == 1
    assert "base measured nothing" in capsys.readouterr().out


def test_accept_keeps_empty_base_findings_and_summary(tmp_path, monkeypatch, capsys):
    before = {"results": [{"profile": "react-native", "mode": "local", "install_error": "baseline failed"}]}
    before_path, after_path = _write_measurements(tmp_path, before, _result())
    summary_path = tmp_path / "summary.md"
    argv = ["phase_budget_compare.py", str(before_path), str(after_path), "--summary", str(summary_path)]
    monkeypatch.setattr("sys.argv", argv)
    assert compare_module.main() == 1
    rejected = capsys.readouterr()
    report = summary_path.read_text(encoding="utf-8")
    assert rejected.out == report + "\n"
    assert "### Findings" in report
    assert "base measured nothing" in report
    assert "baseline failed" in report

    monkeypatch.setattr("sys.argv", [*argv, "--accept"])
    assert compare_module.main() == 0
    accepted = capsys.readouterr()
    assert accepted.out == report + "\n"
    assert summary_path.read_text(encoding="utf-8") == report + report
    assert "findings accepted by the phase-budget-accepted label" in accepted.err


def test_a_partially_failed_base_keeps_comparing_successful_measurements(tmp_path, monkeypatch, capsys):
    before = _result()
    before["results"].append({"profile": "python", "mode": "clean", "install_error": "unsupported base mode"})
    before_path, after_path = _write_measurements(tmp_path, before, _result())

    lines, failures = compare_module.compare(before_path, after_path)

    assert failures == []
    assert any("1 profile × mode combinations" in line for line in lines)
    assert any("before python:clean; after none" in line for line in lines)
    assert "No skill-delivery regression found." in lines
    monkeypatch.setattr("sys.argv", ["phase_budget_compare.py", str(before_path), str(after_path)])
    assert compare_module.main() == 0
    assert "No skill-delivery regression found." in capsys.readouterr().out
