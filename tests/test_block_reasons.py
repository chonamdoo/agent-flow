import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
import block_reasons  # noqa: E402


def _run(tmp_path: Path, name: str, message: str, ui: dict, **extra) -> Path:
    run = tmp_path / name
    run.mkdir()
    result = {"arm": "B", "declaration": "declared-blocked", "final_message": message, "ui": ui, **extra}
    (run / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return run


def test_reasons_and_verdicts(tmp_path):
    arch = _run(tmp_path, "a", "done\nEVAL-STATUS: blocked: review keeps asking because architecture is `pending`",
                {"S": 0, "G": 0.95, "failed_checks": ["x", "y"]}, phases_entered=["review", "fix-loop", "review"])
    cap = _run(tmp_path, "b", "EVAL-STATUS: blocked: still request-changes; fix-loop exceeded 3 rounds",
               {"S": 1, "G": 1.0}, fix_loop_cap_stop=True)
    guard = _run(tmp_path, "c", "EVAL-STATUS: blocked: the guard hook blocks every command", {"S": 0, "G": 0.0,
                                                                                            "failed_browser": ["z"]})
    rows = {row["run"]: row for row in map(block_reasons.classify, (arch, cap, guard))}
    assert rows["a"]["reason"] == "architecture" and rows["a"]["verdict"] == "blocked-with-defects"
    assert rows["a"]["failed"] == 2 and rows["a"]["reviews"] == 2
    assert rows["b"]["reason"] == "review-cap" and rows["b"]["verdict"] == "false-block"
    assert rows["c"]["reason"] == "guard" and rows["c"]["verdict"] == "no-build" and rows["c"]["failed"] == 1


def test_completed_runs_are_skipped(tmp_path):
    run = _run(tmp_path, "d", "EVAL-STATUS: complete", {"S": 0, "G": 0.9}, declaration="declared-complete")
    assert block_reasons.classify(run) is None
