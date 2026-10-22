"""`evals/run_usage.py`는 controller 토큰을 phase에 나눠 적는다. 틀리면 비용 비교 전체가 틀린다."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("run_usage", REPO / "evals" / "run_usage.py")
assert _SPEC is not None and _SPEC.loader is not None
run_usage = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(run_usage)


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _run_dir(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    (run / "context").mkdir(parents=True)
    (run / "meta.json").write_text(json.dumps({"run_id": "r1", "workflow": "development"}), encoding="utf-8")
    _write_jsonl(run / "context" / "events.jsonl", [
        {"event": "phase_entered", "details": {"phase": "explore"}, "ts": "2026-10-22T10:00:00+00:00"},
        {"event": "phase_exited", "details": {"phase": "explore", "to_phase": "implement", "blocked": False},
         "ts": "2026-10-22T10:10:00+00:00"},
        {"event": "phase_exited", "details": {"phase": "implement", "to_phase": "implement", "blocked": True},
         "ts": "2026-10-22T10:15:00+00:00"},
        {"event": "phase_exited", "details": {"phase": "implement", "to_phase": "complete", "blocked": False},
         "ts": "2026-10-22T10:20:00+00:00"},
    ])
    return run


def _claude(message_id: str, at: str, *, uncached: int, creation: int, cached: int, output: int) -> dict:
    return {"type": "assistant", "timestamp": at, "message": {
        "id": message_id, "model": "claude-test",
        "usage": {"input_tokens": uncached, "cache_creation_input_tokens": creation,
                  "cache_read_input_tokens": cached, "output_tokens": output}}}


def test_claude_transcript_counts_each_response_once_and_splits_by_phase(tmp_path: Path) -> None:
    run = _run_dir(tmp_path)
    first = _claude("m1", "2026-10-22T10:05:00Z", uncached=2, creation=10, cached=100, output=5)
    transcript = _write_jsonl(tmp_path / "s.jsonl", [
        _claude("m0", "2026-10-22T09:59:00Z", uncached=1, creation=0, cached=7, output=1),
        first, first,  # 같은 응답의 content block이 줄마다 반복된다
        _claude("m2", "2026-10-22T10:16:00Z", uncached=3, creation=0, cached=200, output=9),
        _claude("m3", "2026-10-22T10:21:00Z", uncached=1, creation=0, cached=50, output=1),
    ])

    result = run_usage.report(run, [transcript], "success")

    phases = result["tokens_by_phase"]
    assert phases["explore"] == {"messages": 1, "usage": {
        "input": 112, "uncached_input": 12, "cached_input": 100, "output": 5}}
    assert phases["implement"]["usage"]["cached_input"] == 200
    assert set(phases) == {"pre-run", "explore", "implement", "post-run"}
    assert result["tokens_controller"]["usage"] == {
        "input": 315, "uncached_input": 15, "cached_input": 300, "output": 14}
    assert result["transcripts"][0]["models"] == ["claude-test"]


def test_codex_rollout_counts_increase_of_the_cumulative_total(tmp_path: Path) -> None:
    run = _run_dir(tmp_path)

    def tokens(at: str, total: int, cached: int, output: int) -> dict:
        return {"type": "event_msg", "timestamp": at, "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": total, "cached_input_tokens": cached, "output_tokens": output}}}}

    transcript = _write_jsonl(tmp_path / "rollout.jsonl", [
        {"type": "session_meta", "timestamp": "2026-10-22T10:00:01Z", "payload": {}},
        {"type": "turn_context", "timestamp": "2026-10-22T10:00:02Z", "payload": {"model": "codex-test"}},
        tokens("2026-10-22T10:01:00Z", 100, 80, 10),
        tokens("2026-10-22T10:02:00Z", 100, 80, 10),  # 같은 합계의 반복 보고
        tokens("2026-10-22T10:12:00Z", 250, 200, 25),
    ])

    phases = run_usage.report(run, [transcript], None)["tokens_by_phase"]

    assert phases["explore"] == {"messages": 1, "usage": {
        "input": 100, "uncached_input": 20, "cached_input": 80, "output": 10}}
    assert phases["implement"] == {"messages": 1, "usage": {
        "input": 150, "uncached_input": 30, "cached_input": 120, "output": 15}}


def test_missing_observation_is_unmeasured_not_zero(tmp_path: Path) -> None:
    run = _run_dir(tmp_path)
    (run / "review-metrics-a.json").write_text(json.dumps({"jobs": [{}, {}]}), encoding="utf-8")
    empty = _write_jsonl(tmp_path / "other.jsonl", [{"type": "user"}])

    result = run_usage.report(run, [empty], "blocked")

    assert result["tokens_controller"]["status"] == "unmeasured"
    assert result["tokens_controller"]["usage"]["output"] is None
    assert result["tokens_reviewers"]["status"] == "unmeasured"
    assert result["tokens_reviewers"]["observed_jobs"] == 2
    summary = run_usage.summarize([result])
    assert summary["controller_total"]["output"] is None
    assert summary["controller_per_success"]["output"] is None


def test_a_response_without_cache_counts_leaves_input_unmeasured(tmp_path: Path) -> None:
    run = _run_dir(tmp_path)
    partial = _claude("m1", "2026-10-22T10:05:00Z", uncached=2, creation=0, cached=0, output=5)
    del partial["message"]["usage"]["cache_read_input_tokens"]
    transcript = _write_jsonl(tmp_path / "s.jsonl", [partial])

    usage = run_usage.report(run, [transcript], None)["tokens_controller"]["usage"]

    assert usage == {"input": None, "uncached_input": 2, "cached_input": None, "output": 5}


def test_summary_divides_all_runs_by_successful_runs(tmp_path: Path) -> None:
    run = _run_dir(tmp_path)
    transcript = _write_jsonl(tmp_path / "s.jsonl", [
        _claude("m1", "2026-10-22T10:05:00Z", uncached=10, creation=0, cached=90, output=4)])
    success = run_usage.report(run, [transcript], "success")
    blocked = run_usage.report(run, [transcript], "blocked")

    summary = run_usage.summarize([success, blocked])

    assert summary["outcomes"]["success"] == 1 and summary["outcomes"]["blocked"] == 1
    assert summary["controller_per_success"] == {
        "input": 200, "uncached_input": 20, "cached_input": 180, "output": 8}


def test_codex_counter_restart_counts_the_new_total(tmp_path: Path) -> None:
    run = _run_dir(tmp_path)

    def tokens(at: str, total: int, output: int) -> dict:
        return {"type": "event_msg", "timestamp": at, "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": total, "cached_input_tokens": 0, "output_tokens": output}}}}

    transcript = _write_jsonl(tmp_path / "rollout.jsonl", [
        {"type": "session_meta", "timestamp": "2026-10-22T10:00:01Z", "payload": {}},
        tokens("2026-10-22T10:01:00Z", 100, 10),
        tokens("2026-10-22T10:02:00Z", 20, 2),  # 카운터가 다시 시작했다
        tokens("2026-10-22T10:03:00Z", 50, 5),
    ])

    usage = run_usage.report(run, [transcript], None)["tokens_controller"]["usage"]

    assert usage["input"] == 150 and usage["output"] == 15


def test_a_recognised_transcript_without_usage_is_unmeasured(tmp_path: Path) -> None:
    run = _run_dir(tmp_path)
    transcript = _write_jsonl(tmp_path / "rollout.jsonl", [
        {"type": "session_meta", "timestamp": "2026-10-22T10:00:01Z", "payload": {}}])

    result = run_usage.report(run, [transcript], "success")

    assert result["tokens_controller"]["status"] == "unmeasured"
    assert run_usage.summarize([result])["controller_per_success"]["input"] is None


def test_cache_writes_are_kept_apart_from_fresh_input(tmp_path: Path) -> None:
    run = _run_dir(tmp_path)
    row = _claude("m1", "2026-10-22T10:05:00+00:00", uncached=10, creation=300, cached=1_000, output=40)
    row["message"]["usage"]["cache_creation"] = {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 200}
    transcript = _write_jsonl(tmp_path / "session.jsonl", [row])
    report = run_usage.report(run, [transcript], "success")
    controller = report["tokens_controller"]
    # 예전 합계는 그대로다: uncached = fresh input + cache write.
    assert controller["usage"] == {"input": 1_310, "uncached_input": 310, "cached_input": 1_000, "output": 40}
    assert controller["usage_components"] == {"fresh_input": 10, "cache_write": 300, "cache_write_5m": 100,
                                              "cache_write_1h": 200, "cache_read": 1_000, "output": 40}
    assert run_usage.summarize([report])["controller_components_total"]["cache_write"] == 300


def test_codex_reports_no_cache_writes_so_they_are_null(tmp_path: Path) -> None:
    rows = [{"type": "turn_context", "payload": {"model": "gpt-x"}, "timestamp": "2026-10-22T10:01:00+00:00"},
            {"type": "event_msg", "timestamp": "2026-10-22T10:02:00+00:00", "payload": {
                "type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": 500, "cached_input_tokens": 200, "output_tokens": 30}}}}]
    message = run_usage.codex_messages(rows)[0]
    assert message["components"] == {"fresh_input": 300, "cache_write": None, "cache_write_5m": None,
                                     "cache_write_1h": None, "cache_read": 200, "output": 30}
