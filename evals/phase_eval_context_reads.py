"""Report broad catalog/workflow outputs from native author command evidence.

This is a diagnostic for phase-scoped author fixtures, not an execution filter
or a replacement for required-read, behavior, plan, norm or release gates.
It detects content emitted to the model; a path mention alone is not a read.
A skill catalog output counts only when it equals the installed index, so a
filtered or selected extract is not a full catalog read. A catalog wrapped
inside another JSON object is not detected.
The workflow check covers escaped source_text metadata, not every YAML read.

Run against a phase-scoped author's native stdout.raw with the fixture's
installed index. Exit 1 reports broad outputs. Malformed, empty or incomplete
evidence exits 2 and is never reported as zero reads.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


class EvidenceError(ValueError):
    """Native evidence or catalog cannot support a classification."""


def _load_catalog(path: Path) -> dict:
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, RecursionError) as error:
        raise EvidenceError(f"{path}: catalog is not decodable JSON ({error})") from error
    if not isinstance(catalog, dict):
        raise EvidenceError(f"{path}: catalog is not a JSON object")
    return catalog


def _events(stdout: Path) -> list[dict]:
    events = []
    for number, line in enumerate(stdout.read_text(encoding="utf-8").splitlines(), 1):
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, RecursionError) as error:
            raise EvidenceError(f"{stdout}:{number}: event is not decodable JSON ({error})") from error
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise EvidenceError(f"{stdout}:{number}: event is not a typed JSON object")
        events.append(event)
    if not any(event.get("type") == "turn.completed" for event in events):
        raise EvidenceError(f"{stdout}: native evidence does not contain a completed turn")
    return events


def _has_catalog(output: str, catalog: dict) -> bool:
    decoder = json.JSONDecoder()
    offset = 0
    while (start := output.find("{", offset)) >= 0:
        try:
            value, end = decoder.raw_decode(output, start)
        except (json.JSONDecodeError, RecursionError):
            offset = start + 1
            continue
        if value == catalog:
            return True
        offset = end
    return False


def context_outputs(stdout: Path, *, catalog: Path) -> dict:
    """Classify completed Codex commands using output content, including errors."""
    full_catalog = _load_catalog(catalog)
    broad = []
    total_bytes = 0
    commands = 0
    for number, event in enumerate(_events(stdout), 1):
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            raise EvidenceError(f"{stdout}:{number}: completed item is not a typed JSON object")
        if item["type"] != "command_execution":
            continue
        command = item.get("command")
        output = item.get("aggregated_output")
        if not isinstance(command, str) or not isinstance(output, str):
            raise EvidenceError(f"{stdout}:{number}: command or output is not a string")
        try:
            size = len(output.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise EvidenceError(f"{stdout}:{number}: output is not valid Unicode ({error})") from error
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stdout", type=Path, help="Native Codex stdout.raw")
    parser.add_argument("--catalog", type=Path, required=True,
                        help="Full installed skills/index.json of the same fixture")
    args = parser.parse_args(argv)
    try:
        result = context_outputs(args.stdout, catalog=args.catalog)
    except (EvidenceError, OSError, UnicodeDecodeError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(bool(result["broad_context_outputs"]))


if __name__ == "__main__":
    raise SystemExit(main())
