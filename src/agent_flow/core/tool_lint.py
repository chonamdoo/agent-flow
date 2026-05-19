from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ToolLintFinding:
    path: str
    severity: str
    message: str


def lint_tools(root: Path) -> list[ToolLintFinding]:
    findings: list[ToolLintFinding] = []
    specs = _load_specs(root)
    by_name: dict[str, list[dict[str, Any]]] = {}
    for spec in specs:
        path = str(spec["_path"])
        name = spec.get("name")
        if not isinstance(name, str) or not name.strip():
            findings.append(ToolLintFinding(path, "error", "missing name"))
            continue
        by_name.setdefault(name, []).append(spec)
        description = spec.get("description")
        if not isinstance(description, str) or len(description.strip()) < 12:
            findings.append(ToolLintFinding(path, "error", "description is missing or too short"))
        schema = spec.get("input_schema") or spec.get("schema") or spec.get("parameters")
        if schema is not None and not isinstance(schema, dict):
            findings.append(ToolLintFinding(path, "error", "schema must be an object"))
    for name, variants in sorted(by_name.items()):
        schemas = {_stable_json(v.get("input_schema") or v.get("schema") or v.get("parameters") or {}) for v in variants}
        hosts = sorted({str(v.get("host", "default")) for v in variants})
        if len(schemas) > 1:
            joined_hosts = ", ".join(hosts)
            findings.append(ToolLintFinding(name, "error", f"schema drift across hosts: {joined_hosts}"))
    return findings


def _load_specs(root: Path) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for directory in (root / "tools", root / ".agent-flow" / "tools"):
        if not directory.exists():
            continue
        for path in sorted([*directory.glob("*.json"), *directory.glob("*.yaml"), *directory.glob("*.yml")]):
            payload = _read_spec(path)
            if isinstance(payload, dict):
                payload["_path"] = path
                specs.append(payload)
            elif isinstance(payload, list):
                for index, item in enumerate(payload):
                    if isinstance(item, dict):
                        item = dict(item)
                        item["_path"] = f"{path}#{index}"
                        specs.append(item)
    return specs


def _read_spec(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def _stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
