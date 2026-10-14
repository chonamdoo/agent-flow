"""Run the focused author cases for two kits in one executor (attempt-3).

usage: focused_run.py <evaluator-tree> <before-kit> <after-kit> <output-dir>

All 18 units (backend/clean, app/clean, rn/local x 3 trials x 2 kits) share one
thread pool, so before and after see the same scheduling window. Each unit uses
`evals/phase_eval.run_author` from the evaluator tree unchanged. While the
fixture project still exists, this records the installed catalog and the
per-unit read analysis that needs the project files:

- successful SKILL.md reads: `phase_eval._codex_read_paths`, the same check the
  evaluator uses for the required-read rate, counted per skill directory name;
- first implementation mutation: the first completed `file_change` outside
  `.agent-flow/`, or a command that writes into `src/` or `lib/` through a
  redirect or `tee`. This rule reproduces all 18 recorded attempt-2 boundaries
  from their native streams.
"""
from __future__ import annotations

import collections
import concurrent.futures
import hashlib
import json
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

CASES = (("backend", "clean"), ("app", "clean"), ("rn", "local"))
TRIALS = 3
MODEL = "gpt-6.1-sol"
CONCURRENCY = 8
SOURCE_WRITE = re.compile(r"""(?:>{1,2}|\btee(?:\s+-a)?)\s*['"]?(?:\./)?(?:src|lib)/""")


def implementation_boundary(events: list[dict]) -> tuple[int | None, dict]:
    for index, event in enumerate(events):
        if event.get("type") != "item.completed":
            continue
        item = event.get("item") or {}
        if item.get("type") == "file_change":
            paths = [change.get("path", "") for change in item.get("changes", [])]
            if any("/.agent-flow/" not in path for path in paths):
                return index, {"event_index": index, "kind": "file_change", "paths": paths}
        if item.get("type") == "command_execution" and SOURCE_WRITE.search(item.get("command", "")):
            return index, {"event_index": index, "kind": "command_execution",
                           "command": item["command"]}
    return None, {}


def analyse(pe, context_outputs, row: dict, evidence: Path, project: Path) -> dict:
    stdout = (evidence / "stdout.raw").read_text(encoding="utf-8")
    events = [json.loads(line) for line in stdout.splitlines()]
    reads: collections.Counter[str] = collections.Counter()
    for event in events:
        item = event.get("item") or {}
        if event.get("type") == "item.completed" and item.get("type") == "command_execution":
            observed, _ = pe._codex_read_paths(item, project)
            for path in observed:
                if path.endswith("/SKILL.md"):
                    reads[Path(path).parent.name] += 1
    boundary, mutation = implementation_boundary(events)
    pre = [event["item"] for event in events[:boundary]
           if event.get("type") == "item.completed"
           and (event.get("item") or {}).get("type") == "command_execution"]
    repeated = {name: count for name, count in reads.items() if count > 1}
    return {
        "unit": ["author", row["kit"], row["stack"], row["mode"], "", row["trial"]],
        "usage": row.get("usage"),
        "prompt_bytes": row.get("prompt_bytes"),
        "context_outputs": context_outputs(evidence / "stdout.raw", catalog=evidence / "catalog.json"),
        "successful_skill_read_counts": dict(reads),
        "repeated_skill_reads": repeated,
        "repeat_extra_reads": sum(count - 1 for count in repeated.values()),
        "first_observed_implementation_mutation": mutation,
        "pre_implementation_command_count": len(pre),
        "pre_implementation_command_output_bytes": sum(
            len(item.get("aggregated_output", "").encode("utf-8")) for item in pre),
        "pre_implementation_commands": [
            {"command": item.get("command"),
             "output_bytes": len(item.get("aggregated_output", "").encode("utf-8")),
             "exit_code": item.get("exit_code")} for item in pre],
        "limit": "Native usage is terminal-only. No prefix token attribution; successful "
                 "skill-read paths do not prove full-body/reference coverage.",
    }


def main() -> int:
    evaluator, before, after, output = (Path(arg).resolve() for arg in sys.argv[1:5])
    sys.path.insert(0, str(evaluator / "evals"))
    import phase_eval as pe
    from phase_eval_context_reads import context_outputs
    from phase_eval_fingerprint import evaluator_sha256

    sys.path.insert(0, str(evaluator / "src"))
    from agent_flow.core.kit_digest import kit_source_digest

    kits = {"before": before, "after": after}
    output.mkdir(parents=True)
    pe.REQUESTED_PROVIDERS.update(author="codex", review=["codex"])
    pe.REQUESTED_MODELS.update(claude=None, codex=MODEL)
    pe._slots = threading.Semaphore(CONCURRENCY)
    work = Path(tempfile.mkdtemp(prefix="af-phase-eval-focused-"))
    units = [(kit, stack, mode, trial)
             for trial in range(1, TRIALS + 1) for stack, mode in CASES for kit in kits]
    meta = {
        "units": len(units),
        "scope": "Focused author regression; not a complete release gate.",
        "selected_cases": [list(case) for case in CASES],
        "trials": TRIALS, "requested_model": MODEL, "concurrency": CONCURRENCY,
        "kits": {name: str(path) for name, path in kits.items()},
        "kit_digests": {name: kit_source_digest(path) for name, path in kits.items()},
        "evaluator_sha256": evaluator_sha256(evaluator),
        "diagnostic_sha256": hashlib.sha256(
            (evaluator / "evals" / "phase_eval_context_reads.py").read_bytes()).hexdigest(),
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "work": str(work),
    }
    (output / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    lock = threading.Lock()
    done = 0
    errors = []

    def execute(unit):
        nonlocal done
        kit, stack, mode, trial = unit
        identity = ["author", kit, stack, mode, "", trial]
        evidence = output / "evidence" / hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        project = work / f"author-{stack}-{mode}-{kit}-{trial}"
        row = pe.run_author(kit, kits[kit], pe.CASES / stack, mode, trial, work, 1500, evidence=evidence)
        analysis = None
        try:
            shutil.copyfile(project / ".agent-flow" / "skills" / "index.json", evidence / "catalog.json")
            analysis = analyse(pe, context_outputs, row, evidence, project)
            row["context_outputs"] = analysis["context_outputs"]
        except Exception as error:  # recorded as an invalid unit, never as zero reads
            row["analysis_error"] = f"{type(error).__name__}: {error}"[-1200:]
        with lock:
            done += 1
            if row.get("error") or row.get("analysis_error"):
                errors.append({"unit": identity, "error": row.get("error") or row.get("analysis_error")})
            with (output / "results.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            if analysis is not None:
                with (output / "context-read-analysis.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(analysis, ensure_ascii=False) + "\n")
            with (output / "focused-evaluation.log").open("a", encoding="utf-8") as handle:
                handle.write(f"{done}/{len(units)} {stack}/{mode} {kit} t{trial}: {row.get('oracle')}\n")
            print(f"[{time.strftime('%H:%M:%S')}] {done}/{len(units)} {stack}/{mode} {kit} t{trial}: "
                  f"{'ERROR' if row.get('error') or row.get('analysis_error') else 'ok'}", flush=True)

    with pe._cancellation_scope():
        with concurrent.futures.ThreadPoolExecutor(CONCURRENCY) as pool:
            list(pool.map(execute, units))
    (output / "run-status.json").write_text(json.dumps({
        "units": len(units), "errors": errors,
        "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
    }, indent=2), encoding="utf-8")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
