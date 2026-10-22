"""Attribute a controller session's own token use to the phases of one agent-flow run.

The runner never sees the controller's tokens: the host CLI that drives the run reports them
only in its own transcript. This tool joins that transcript with the run's `events.jsonl`
afterwards. It changes no run state and calls no model.

    python evals/run_usage.py report --run-dir <run> --transcript <host transcript> \
        [--outcome success|blocked|failed] --output <usage.json>
    python evals/run_usage.py summarize <usage.json>...

Supported transcripts:
- Claude Code session JSONL (`~/.claude/projects/<project>/<session>.jsonl`). One API response is
  written as several lines that share `message.id` and repeat the same usage; each id counts once.
  Sub-agents (the `Task` tool) are not in that file: Claude Code 2.1.308 writes each one to
  `<project>/<session>/subagents/agent-*.jsonl` (checked on macOS and on Linux, where the main file held no
  `isSidechain` rows). Their messages are read with the session and count toward the same phase. A subagent
  folder that exists but cannot be read makes the controller `unmeasured`, never a smaller total.
- Codex rollout JSONL (`~/.codex/sessions/.../rollout-*.jsonl`). `token_count` events carry a
  cumulative `total_token_usage`; the tool counts its increase, so repeated events add nothing. A
  total that drops means the counter restarted, and the new total counts in full.

A message is attributed to the phase the run was in at the message's timestamp: the phase of the
latest `phase_entered`, or the target of the latest unblocked `phase_exited`. Messages before the
first event go to `pre-run`. After the runner exits the last phase (`to_phase: complete`) the run
is over, and later messages go to `post-run`.

`usage` keeps the historical four numbers (`uncached_input` = fresh input + cache writes). `usage_components` splits
them so any price table can be applied later: `fresh_input`, `cache_write` (with its `cache_write_5m`/`cache_write_1h`
TTL split when the transcript reports it), `cache_read` and `output`. A part the provider does not report is `null`:
Codex rollouts report no cache writes, so their `cache_write` is `null`, not 0.

Anything that cannot be observed is `null` with a reason, never 0. Runner reviewer subprocesses
print plain text without usage, so their tokens are `unmeasured`; `review-metrics-*.json` still
supplies how many reviewer jobs ran. Gates run commands, not models, so they use no model tokens.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1
USAGE_KEYS = ("input", "uncached_input", "cached_input", "output")
COMPONENT_KEYS = ("fresh_input", "cache_write", "cache_write_5m", "cache_write_1h", "cache_read", "output")
PRE_RUN = "pre-run"
POST_RUN = "post-run"
OUTCOMES = ("success", "blocked", "failed")


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _count(usage: object, key: str) -> int | None:
    value = usage.get(key) if isinstance(usage, dict) else None
    return value if type(value) is int and value >= 0 else None


def _jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _usage(uncached: int | None, cached: int | None, output: int | None) -> dict[str, int | None]:
    total = uncached + cached if uncached is not None and cached is not None else None
    return {"input": total, "uncached_input": uncached, "cached_input": cached, "output": output}


def claude_messages(rows: list[dict]) -> list[dict]:
    """One record per API response. Same uncached/cached split as `phase_eval.py`."""
    messages: dict[str, dict] = {}
    for row in rows:
        message = row.get("message")
        if row.get("type") != "assistant" or not isinstance(message, dict):
            continue
        usage = message.get("usage")
        message_id = message.get("id")
        at = _timestamp(row.get("timestamp"))
        if not isinstance(usage, dict) or not isinstance(message_id, str) or at is None:
            continue
        if message_id in messages:
            continue
        input_tokens = _count(usage, "input_tokens")
        creation = _count(usage, "cache_creation_input_tokens")
        uncached = input_tokens + creation if input_tokens is not None and creation is not None else None
        by_ttl = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
        messages[message_id] = {
            "at": at,
            "model": message.get("model") if isinstance(message.get("model"), str) else None,
            "usage": _usage(uncached, _count(usage, "cache_read_input_tokens"), _count(usage, "output_tokens")),
            "components": {"fresh_input": input_tokens, "cache_write": creation,
                           "cache_write_5m": _count(by_ttl, "ephemeral_5m_input_tokens"),
                           "cache_write_1h": _count(by_ttl, "ephemeral_1h_input_tokens"),
                           "cache_read": _count(usage, "cache_read_input_tokens"),
                           "output": _count(usage, "output_tokens")},
        }
    return list(messages.values())


def codex_messages(rows: list[dict]) -> list[dict]:
    """One record per increase of the cumulative total. Codex `input_tokens` includes cached input."""
    messages = []
    model = None
    previous = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if row.get("type") == "turn_context" and isinstance(payload.get("model"), str):
            model = payload["model"]
        if row.get("type") != "event_msg" or payload.get("type") != "token_count":
            continue
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        total = info.get("total_token_usage")
        at = _timestamp(row.get("timestamp"))
        current = {key: _count(total, key) for key in previous}
        if at is None or any(value is None for value in current.values()):
            continue
        delta = {key: current[key] - previous[key] for key in previous}
        if any(value < 0 for value in delta.values()):
            # 누적값이 줄었으면 카운터가 처음부터 다시 센 것이다. 그 뒤의 합계 전체가 새 사용량이다.
            delta = dict(current)
        previous = current
        if all(value == 0 for value in delta.values()):
            continue
        cached = delta["cached_input_tokens"]
        uncached = delta["input_tokens"] - cached if cached <= delta["input_tokens"] else None
        # rollout에는 cache write가 없다. 0이 아니라 null이다.
        messages.append({"at": at, "model": model, "usage": _usage(uncached, cached, delta["output_tokens"]),
                         "components": {"fresh_input": uncached, "cache_write": None, "cache_write_5m": None,
                                        "cache_write_1h": None, "cache_read": cached,
                                        "output": delta["output_tokens"]}})
    return messages


def subagent_transcripts(transcript: Path) -> tuple[list[Path], str]:
    """The session's sub-agent transcripts and whether they could be read: `none`, `read` or `unreadable`."""
    folder = transcript.with_suffix("") / "subagents"
    if not folder.exists():
        return [], "none"
    try:
        files = sorted(folder.glob("agent-*.jsonl"))
        for path in files:
            with path.open("rb"):
                pass
    except OSError:
        return [], "unreadable"
    return files, "read"


def transcript_format(rows: list[dict]) -> str | None:
    if any(row.get("type") == "session_meta" or row.get("type") == "event_msg" for row in rows):
        return "codex"
    if any(row.get("type") == "assistant" and isinstance(row.get("message"), dict) for row in rows):
        return "claude"
    return None


def phase_timeline(events: list[dict]) -> list[tuple[datetime, str]]:
    """(time, phase in effect from that time), in time order."""
    timeline = []
    for event in events:
        at = _timestamp(event.get("ts"))
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        if at is None:
            continue
        if event.get("event") == "phase_entered" and isinstance(details.get("phase"), str):
            timeline.append((at, details["phase"]))
        elif event.get("event") == "phase_exited" and not details.get("blocked"):
            # runner는 마지막 phase를 나갈 때 to_phase를 "complete"로 적는다.
            target = details.get("to_phase")
            done = not isinstance(target, str) or not target or target == "complete"
            timeline.append((at, POST_RUN if done else target))
    timeline.sort(key=lambda item: item[0])
    return timeline


def phase_at(timeline: list[tuple[datetime, str]], at: datetime) -> str:
    index = bisect.bisect_right([moment for moment, _ in timeline], at)
    return timeline[index - 1][1] if index else PRE_RUN


def _add(total: dict[str, int | None], usage: dict[str, int | None], keys: tuple[str, ...] = USAGE_KEYS) -> None:
    for key in keys:
        total[key] = total[key] + usage[key] if total[key] is not None and usage.get(key) is not None else None


def _empty(keys: tuple[str, ...] = USAGE_KEYS) -> dict[str, int | None]:
    return {key: 0 for key in keys}


def add_components(total: dict[str, int | None], components: dict[str, int | None]) -> None:
    _add(total, components, COMPONENT_KEYS)


def empty_components() -> dict[str, int | None]:
    return _empty(COMPONENT_KEYS)


def reviewer_usage(run_dir: Path) -> dict:
    jobs = 0
    files = sorted(run_dir.glob("review-metrics-*.json"))
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        jobs += len(payload.get("jobs") or [])
    return {
        "status": "unmeasured",
        "reason": "runner reviewer subprocesses print plain text without usage",
        "usage": {key: None for key in USAGE_KEYS},
        "review_waves": len(files),
        "observed_jobs": jobs,
    }


def report(run_dir: Path, transcripts: list[Path], outcome: str | None) -> dict:
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    # runner는 `context/events.jsonl`에 쓴다. 보존 기록(ui-task-runs)은 run 루트에 펼쳐 둔다.
    events = next((path for path in (run_dir / "context" / "events.jsonl", run_dir / "events.jsonl")
                   if path.is_file()), None)
    if events is None:
        raise SystemExit(f"no events.jsonl under {run_dir}")
    timeline = phase_timeline(_jsonl(events))
    by_phase: dict[str, dict] = {}
    controller = _empty()
    controller_parts = empty_components()
    parts_by_phase: dict[str, dict] = {}
    sources = []
    expanded: list[tuple[Path, str | None]] = []
    unreadable = []
    for path in transcripts:
        expanded.append((path, None))
        extra, state = subagent_transcripts(path)
        if state == "unreadable":
            unreadable.append(path.name)
        expanded += [(item, path.name) for item in extra]
    for path, parent in expanded:
        rows = _jsonl(path)
        kind = transcript_format(rows)
        messages = claude_messages(rows) if kind == "claude" else codex_messages(rows) if kind == "codex" else []
        sources.append({
            "path_name": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "format": kind,
            "messages": len(messages),
            "models": sorted({message["model"] for message in messages if message["model"]}),
            **({"subagent_of": parent} if parent else {}),
        })
        for message in messages:
            phase = phase_at(timeline, message["at"])
            entry = by_phase.setdefault(phase, {"messages": 0, "usage": _empty()})
            entry["messages"] += 1
            _add(entry["usage"], message["usage"])
            add_components(parts_by_phase.setdefault(phase, empty_components()), message["components"])
            if phase not in (PRE_RUN, POST_RUN):
                _add(controller, message["usage"])
                add_components(controller_parts, message["components"])
    # 형식만 알아보고 usage가 하나도 없으면 측정이 아니다. 0토큰으로 적으면 누락이 무사용으로 보인다.
    measured = any(source["format"] and source["messages"] for source in sources) and not unreadable
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": meta.get("run_id"),
        "workflow": meta.get("workflow"),
        "outcome": outcome,
        "transcripts": sources,
        "tokens_by_phase": by_phase,
        "usage_components_by_phase": parts_by_phase,
        "tokens_controller": {
            "status": "measured" if measured else "unmeasured",
            "reason": None if measured else (f"unreadable subagent folder for {', '.join(unreadable)}" if unreadable
                                             else "no supported host transcript"),
            "usage": controller if measured else {key: None for key in USAGE_KEYS},
            "usage_components": controller_parts if measured else {key: None for key in COMPONENT_KEYS},
        },
        "tokens_reviewers": reviewer_usage(run_dir),
        "tokens_gates": {"status": "not_applicable", "reason": "gates run commands, not models"},
    }


def summarize(reports: list[dict]) -> dict:
    """Totals across runs and tokens per successful run. A run with an unmeasured part makes the total unmeasured."""
    totals = _empty()
    parts = empty_components()
    counts = {key: 0 for key in (*OUTCOMES, "unknown")}
    reviewers_measured = True
    for item in reports:
        counts[item.get("outcome") if item.get("outcome") in OUTCOMES else "unknown"] += 1
        _add(totals, item["tokens_controller"]["usage"])
        # 구성요소가 없는 예전 보고서는 합계를 null로 만든다. 없는 값을 0으로 채우지 않는다.
        add_components(parts, item["tokens_controller"].get("usage_components") or {})
        reviewers_measured = reviewers_measured and item["tokens_reviewers"]["status"] == "measured"
    successes = counts["success"]
    per_success = {
        key: (round(totals[key] / successes) if totals[key] is not None and successes else None)
        for key in USAGE_KEYS
    }
    return {
        "runs": len(reports),
        "outcomes": counts,
        "controller_total": totals,
        "controller_components_total": parts,
        "controller_per_success": per_success,
        "reviewers": "measured" if reviewers_measured and reports else "unmeasured",
    }


def _cell(value: int | None) -> str:
    return "unmeasured" if value is None else f"{value:,}"


def markdown(item: dict) -> str:
    lines = ["| phase | messages | uncached input | cached input | output |", "|---|---:|---:|---:|---:|"]
    for phase, entry in item["tokens_by_phase"].items():
        usage = entry["usage"]
        lines.append(f"| {phase} | {entry['messages']} | {_cell(usage['uncached_input'])} | "
                     f"{_cell(usage['cached_input'])} | {_cell(usage['output'])} |")
    for label, part in (("controller (run phases)", item["tokens_controller"]),
                        ("reviewers", item["tokens_reviewers"])):
        usage = part["usage"]
        lines.append(f"| **{label}** | | {_cell(usage['uncached_input'])} | {_cell(usage['cached_input'])} | "
                     f"{_cell(usage['output'])} |")
    lines.append("| **gates** | | no model tokens | | |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("report")
    one.add_argument("--run-dir", type=Path, required=True)
    one.add_argument("--transcript", type=Path, action="append", default=[])
    one.add_argument("--outcome", choices=OUTCOMES)
    one.add_argument("--output", type=Path, required=True)
    many = sub.add_parser("summarize")
    many.add_argument("reports", type=Path, nargs="+")
    args = parser.parse_args(argv)
    if args.command == "report":
        result = report(args.run_dir, args.transcript, args.outcome)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        args.output.with_suffix(".md").write_text(markdown(result), encoding="utf-8")
        print(markdown(result), end="")
        return 0
    loaded = [json.loads(path.read_text(encoding="utf-8")) for path in args.reports]
    print(json.dumps(summarize(loaded), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
