"""Report broad catalog/workflow outputs from native author command evidence.

This is a diagnostic for phase-scoped author fixtures, not an execution filter
or a replacement for required-read, behavior, plan, norm or release gates.
It detects content emitted to the model; a path mention alone is not a read.
The workflow check covers escaped source_text metadata, not every YAML read.

Run against a phase-scoped author's native stdout.raw. Exit 1 reports broad
outputs; malformed evidence is an error, not an empty successful report.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


def _has_catalog(output: str, catalog: dict) -> bool:
    decoder = json.JSONDecoder()
    offset = 0
    while (start := output.find("{", offset)) >= 0:
        try:
            value, end = decoder.raw_decode(output, start)
        except json.JSONDecodeError:
            offset = start + 1
            continue
        offset = end
        if not isinstance(value, dict):
            continue
        selection = value.get("selection")
        if (isinstance(selection, dict)
                and isinstance(selection.get("selected_skills"), list)
                and isinstance(value.get("skills"), list)
                and isinstance(value.get("links"), list)):
            if value == catalog:
                return True
    return False


def context_outputs(stdout: Path, *, catalog: Path) -> dict:
    """Classify completed Codex commands using output content, including errors."""
    broad = []
    full_catalog = json.loads(catalog.read_text(encoding="utf-8"))
    total_bytes = 0
    commands = 0
    for line in stdout.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        item = event.get("item", {})
        if event.get("type") != "item.completed" or item.get("type") != "command_execution":
            continue
        command = item.get("command", "")
        output = item.get("aggregated_output", "")
        size = len(output.encode("utf-8"))
        total_bytes += size
        commands += 1
        kinds = []
        if _has_catalog(output, full_catalog):
            kinds.append("skill-catalog")
        # grep can emit the entire escaped workflow on a single source_text line.
        # Keep that observable output even when another input made grep exit 2.
        if (re.search(r'["\']source_text["\']\s*:', output)
                and "phases:" in output):
            kinds.append("workflow-source")
        if kinds:
            broad.append({"kinds": kinds, "command": command,
                          "output_bytes": size, "exit_code": item.get("exit_code")})
    return {"command_count": commands, "command_output_bytes": total_bytes,
            "broad_context_outputs": broad,
            "skill_catalog_outputs": sum("skill-catalog" in e["kinds"] for e in broad),
            "workflow_source_outputs": sum("workflow-source" in e["kinds"] for e in broad)}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stdout", type=Path, help="Native Codex stdout.raw")
    parser.add_argument("--catalog", required=True, type=Path, help="Full installed skills/index.json")
    args = parser.parse_args(argv)
    result = context_outputs(args.stdout, catalog=args.catalog)
    events = (json.loads(line) for line in args.stdout.read_text(encoding="utf-8").splitlines())
    if not any(event.get("type") == "turn.completed" for event in events):
        parser.error("native evidence does not contain a completed turn")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(bool(result["broad_context_outputs"]))


if __name__ == "__main__":
    raise SystemExit(main())
