"""Package a focused_run.py output into a committed attempt directory.

usage: package_attempt.py <run-output> <attempt-dir> <repo> <base-commit> <candidate-commit>

Writes the files verify.py rechecks (results, native evidence, checksums,
integrity audit) plus the summary and per-unit read analysis. Compressed files
use a fixed mtime so the same run packages to the same bytes.
"""
from __future__ import annotations

import collections
import gzip
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

KITS = ("before", "after")
FIELDS = ("input", "uncached_input", "cached_input", "output", "commands",
          "command_output_bytes", "pre_implementation_output_bytes", "extra_skill_reads")


def gz(body: bytes) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as handle:
        handle.write(body)
    return buffer.getvalue()


def change(before: float, after: float) -> float | None:
    return (after - before) / before * 100 if before else None


def aggregate(rows: list[dict], analyses: dict) -> dict:
    totals = {kit: collections.Counter() for kit in KITS}
    rates = {kit: [] for kit in KITS}
    broad = {kit: collections.Counter() for kit in KITS}
    for row in rows:
        kit = row["kit"]
        analysis = analyses[(kit, row["stack"], row["mode"], row["trial"])]
        totals[kit].update(row["usage"])
        totals[kit].update({
            "commands": analysis["context_outputs"]["command_count"],
            "command_output_bytes": analysis["context_outputs"]["command_output_bytes"],
            "pre_implementation_output_bytes": analysis["pre_implementation_command_output_bytes"],
            "extra_skill_reads": analysis["repeat_extra_reads"],
        })
        broad[kit].update({
            "catalog_outputs": analysis["context_outputs"]["skill_catalog_outputs"],
            "workflow_source_outputs": analysis["context_outputs"]["workflow_source_outputs"],
        })
        rates[kit].append(row["required_read_rate"])
    result = {
        kit: {**{field: totals[kit][field] for field in FIELDS[:4]},
              "trials": len(rates[kit]), "required_read_rate": rates[kit],
              "catalog_outputs": broad[kit]["catalog_outputs"],
              "workflow_source_outputs": broad[kit]["workflow_source_outputs"],
              **{field: totals[kit][field] for field in FIELDS[4:]}}
        for kit in KITS
    }
    result["change_percent"] = {
        field: change(result["before"][field], result["after"][field]) for field in FIELDS
    }
    return result


def main() -> int:
    run, attempt, repo = (Path(arg).resolve() for arg in sys.argv[1:4])
    base, candidate = sys.argv[4:6]
    attempt.mkdir(parents=True)
    rows = [json.loads(line) for line in (run / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    rows.sort(key=lambda row: (row["stack"], row["mode"], row["trial"], KITS.index(row["kit"])))
    analyses = {}
    for line in (run / "context-read-analysis.jsonl").read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        _, kit, stack, mode, _, trial = entry["unit"]
        analyses[(kit, stack, mode, trial)] = entry
    raw = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8")
    (attempt / "results.jsonl.gz").write_bytes(gz(raw))
    ordered = [analyses[(row["kit"], row["stack"], row["mode"], row["trial"])] for row in rows]
    (attempt / "context-read-analysis.json.gz").write_bytes(
        gz(json.dumps(ordered, ensure_ascii=False, indent=1).encode("utf-8")))

    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for unit in sorted((run / "evidence").iterdir()):
            for name in ("catalog.json", "command.json", "prompt.txt", "stderr.raw", "stdout.raw"):
                body = (unit / name).read_bytes()
                info = tarfile.TarInfo(f"evidence/{unit.name}/{name}")
                info.size, info.mode, info.mtime = len(body), 0o644, 0
                tar.addfile(info, io.BytesIO(body))
    (attempt / "native-evidence.tar.gz").write_bytes(gz(archive.getvalue()))

    summary = {}
    meta = json.loads((run / "meta.json").read_text(encoding="utf-8"))
    for stack, mode in meta["selected_cases"]:
        case_rows = [row for row in rows if (row["stack"], row["mode"]) == (stack, mode)]
        case = aggregate(case_rows, analyses)
        uncached = {(row["kit"], row["trial"]): row["usage"]["uncached_input"] for row in case_rows}
        case["pairs"] = [
            {"trial": trial, "before_uncached": uncached[("before", trial)],
             "after_uncached": uncached[("after", trial)],
             "change_percent": change(uncached[("before", trial)], uncached[("after", trial)])}
            for trial in range(1, meta["trials"] + 1)
        ]
        summary[f"{stack}/{mode}"] = case
    summary["total"] = aggregate(rows, analyses)
    (attempt / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")

    exit_codes: collections.Counter[str] = collections.Counter()
    provider_errors, models, failures = [], set(), []
    unknown_boundaries = []
    for row in rows:
        unit = ["author", row["kit"], row["stack"], row["mode"], "", row["trial"]]
        unit_id = hashlib.sha256(json.dumps(unit).encode()).hexdigest()
        events = [json.loads(line) for line in
                  (run / "evidence" / unit_id / "stdout.raw").read_text(encoding="utf-8").splitlines()]
        for event in events:
            item = event.get("item") or {}
            if event.get("type") == "item.completed" and item.get("type") == "command_execution":
                exit_codes[str(item.get("exit_code"))] += 1
            if event.get("type") in ("error", "turn.failed"):
                provider_errors.append({"unit": unit, "event": event})
            if isinstance(event.get("model"), str):
                models.add(event["model"])
        if row.get("error") or row.get("analysis_error") or row.get("rc") != 0:
            failures.append({"unit": unit, "error": row.get("error") or row.get("analysis_error")})
        if not analyses[(row["kit"], row["stack"], row["mode"], row["trial"])][
                "first_observed_implementation_mutation"]:
            unknown_boundaries.append(unit)
    lint = {kit: dict(collections.Counter(row["architecture_lint"]["status"]
                                          for row in rows if row["kit"] == kit)) for kit in KITS}
    oracle = {kit: {key: sum(bool(row["oracle"].get(key)) for row in rows if row["kit"] == kit)
                    for key in ("behavior", "plan", "norm")} for kit in KITS}
    complete = len(rows) == meta["units"] and not failures and not provider_errors
    (attempt / "integrity-audit.json").write_text(json.dumps({
        "status": "PASS" if complete else "FAIL",
        "failures": failures, "units": len(rows), "native_calls": len(rows),
        "results_sha256": hashlib.sha256(raw).hexdigest(),
        "provider_error_events": provider_errors,
        "tool_exit_codes": dict(exit_codes),
        "requested_model": meta["requested_model"],
        "models_reported_by_native_stream": sorted(models),
        "model_limit": "The requested flag is verified; absent native model identity remains unknown.",
        "lint": lint, "oracle_pass": oracle,
        "pre_implementation_unknown_boundaries": unknown_boundaries,
    }, indent=2), encoding="utf-8")

    versions = {tool: subprocess.run([tool, "--version"], capture_output=True, text=True).stdout.strip()
                for tool in ("codex", "node", "dart")}
    meta.update({"base_commit": base, "candidate_commit": candidate, "tool_versions": versions})
    (attempt / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    for name in ("run-status.json", "focused-evaluation.log"):
        shutil.copyfile(run / name, attempt / name)
    patch = subprocess.run(["git", "diff", base, candidate, "--", ".", ":!evals/results"],
                           cwd=repo, capture_output=True, check=True).stdout
    (attempt / "source.patch.gz").write_bytes(gz(patch))
    checksums = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in sorted(attempt.iterdir()) if path.name != "checksums.json"}
    (attempt / "checksums.json").write_text(json.dumps(checksums, indent=2), encoding="utf-8")
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
