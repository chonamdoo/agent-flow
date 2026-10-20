"""개선 사이클 기록 도구. 틀리면 히스토리의 점수·유의성·토큰 비교와 보관 자료가 잘못 남는다."""

from __future__ import annotations

import json
import sys
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools" / "autoresearch"))

import archive_runs  # noqa: E402
import cycle_report  # noqa: E402


def _author(stack: str, kit: str, trial: int, passed: bool, provider: str = "claude", **extra) -> dict:
    return {"scenario": "author", "stack": stack, "mode": "clean", "kit": kit, "trial": trial, "provider": provider,
            "oracle": {"behavior": True, "plan": passed, "norm": True}, "turns": 20 if provider == "claude" else None,
            "usage": {"uncached_input": 1000, "output": 100}, "cost_usd": 0.25 if provider == "claude" else None,
            "overclaims": [], **extra}


def _claude_stream(*calls: tuple[str, dict]) -> str:
    blocks = [{"type": "tool_use", "name": name, "input": data} for name, data in calls]
    return json.dumps({"type": "assistant", "message": {"content": blocks}}) + "\n"


def _codex_stream(*commands: str) -> str:
    return "".join(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": c}}) + "\n"
                   for c in commands)


def _call(root: Path, name: str, project: str, stream: str) -> None:
    call = root / name
    call.mkdir(parents=True)
    (call / "prompt.txt").write_text(f"**Project root**: {project}\n", encoding="utf-8")
    (call / "stdout.raw").write_text(stream, encoding="utf-8")


def test_fisher_matches_the_recorded_fixloop_result():
    # 2/8 -> 7/8 was reported as p=0.020; equal rates give 1.
    assert round(cycle_report.fisher_greater(2, 8, 7, 8), 4) == 0.0203
    assert round(cycle_report.fisher_greater(2, 8, 8, 8), 4) == 0.0035
    assert cycle_report.fisher_greater(5, 5, 5, 5) == 1.0


def test_author_table_keeps_providers_apart_and_compares_within_one():
    rows = [_author("fixloop", "before", t, t <= 2) for t in range(1, 9)]
    rows += [_author("fixloop", "after", t, t <= 7, _evidence={"tool_calls": 10, "plan_searches": 1 if t == 1 else 0})
             for t in range(1, 9)]
    rows += [_author("fixloop", kit, t, True, "codex") for kit in ("before", "after") for t in range(1, 9)]
    rows.append(_author("devflow", "after", 1, True, required_unread=["tdd"]))
    table = "\n".join(cycle_report.author_table(rows, "before"))
    assert "| fixloop | claude | after | 7/8 | 8 | 7 | 8 | 0.020 | 20.0 | 10.0 |" in table
    assert "| fixloop | claude | before | 2/8 | 8 | 2 | 8 | — |" in table
    assert "| fixloop | codex | after | 8/8 | 8 | 8 | 8 | 1.000 | n/a | n/a |" in table
    assert "| n/a | 0 (0) | — |" in table  # Codex reports no dollar cost
    assert "| 0 (1) |" in table  # an unread required skill makes the row overclaim-eligible
    assert "1 in 1/8 runs" in table


def test_review_table_reports_providers_unparsed_rate_and_disagreement():
    rows = [{"scenario": "review", "kit": "after", "variant": "defect", "valid": True, "correct": True,
             "defects": {"bug": True, "rule": False},
             "angles": [{"angle": "types", "provider": "claude", "verdict": "request-changes", "cost_usd": 0.1,
                         "usage": {"uncached_input": 10}},
                        {"angle": "types", "provider": "codex", "verdict": "approve"},
                        {"angle": "io", "provider": "claude", "verdict": None, "cost_usd": 0.1}]},
            {"scenario": "review", "kit": "after", "variant": "clean", "valid": False, "defects": {},
             "angles": [{"angle": "types", "provider": "claude", "verdict": "request-changes", "cost_usd": 0.1},
                        {"angle": "types", "provider": "codex", "verdict": "request-changes"}]}]
    table = cycle_report.review_table(rows)
    assert "| after | claude | 1/1 | 1/1 | 1/3 (33%) | 10 | $0.30 |" in table
    assert "| after | codex | 0/1 | 1/1 | 0/2 (0%) | 0 | n/a |" in table
    assert table[-1] == "| after | 1/2 | 1/1 | 1/2 | 1/2 |"


def test_evidence_stats_read_claude_and_codex_and_ignore_the_case_name(tmp_path):
    claude = "/tmp/af-phase-eval-x/author-fixloop-wrongplan-clean-after-3"
    codex = "/tmp/af-phase-eval-y/author-devflow-clean-before-2"
    _call(tmp_path, "a", claude, _claude_stream(
        ("Read", {"file_path": f"{claude}/src/shop/cart/domain.py"}),
        ("Glob", {"pattern": ".agent-flow/**/*slice*", "path": claude}),
        ("Edit", {"file_path": f"{claude}/plan-notes.md"})))
    _call(tmp_path, "b", codex, _codex_stream(f"sh -c 'cat {codex}/.agent-flow/runs/r/slice-plan.md'", "sh -c 'ls'"))
    assert cycle_report.evidence_stats([tmp_path]) == {
        ("fixloop-wrongplan", "after", 3): {"tool_calls": 3, "plan_searches": 1},
        ("devflow", "before", 2): {"tool_calls": 2, "plan_searches": 1},
    }


def test_archive_sanitises_paths_and_secrets_and_keeps_evidence_stats(tmp_path):
    run = tmp_path / "run"
    row = _author("fixloop", "after", 1, True, tooling={"path": "/root/.local/bin:/tmp/af-phase-eval-q1/author-x"},
                  note="key sk-ant-api03-abcdefghijkl token=ghp_" + "a" * 30 + " by someone@example.com")
    run.mkdir()
    (run / "results.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    _call(run / "evidence", "c1", "author-fixloop-clean-after-1", _claude_stream(("Read", {"file_path": "slice-plan.md"})))
    tsv = tmp_path / "results.tsv"
    tsv.write_text("iteration\tcommit\n1\tabc\n", encoding="utf-8")
    output = tmp_path / "raw.tar.gz"
    assert archive_runs.main(["--output", str(output), f"fix={run}", "--file", f"loop/results.tsv={tsv}"]) == 0
    with tarfile.open(output) as archive:
        names = sorted(archive.getnames())
        text = archive.extractfile("fix/results.jsonl").read().decode()
        stats = json.loads(archive.extractfile("fix/evidence-stats.json").read())
    assert names == ["fix/evidence-stats.json", "fix/results.jsonl", "loop/results.tsv"]
    for leaked in ("/root", "/tmp/af-", "sk-ant-", "ghp_", "someone@example.com"):
        assert leaked not in text
    assert "<home>/.local/bin:<work>" in text and "<redacted>" in text
    assert stats == [{"stack": "fixloop", "kit": "after", "trial": 1, "tool_calls": 1, "plan_searches": 1}]
    # An archived run reads back with its evidence stats attached to the row.
    extracted = tmp_path / "extracted"
    with tarfile.open(output) as archive:
        archive.extractall(extracted, filter="data")
    rows = cycle_report.load([str(extracted / "fix")])
    assert rows[0]["_evidence"] == {"tool_calls": 1, "plan_searches": 1}
