#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


WRITE_TOOLS = {
    "apply_patch",
    "edit",
    "eval",
    "multiedit",
    "multi_edit",
    "notebook",
    "notebookedit",
    "notebook_edit",
    "python",
    "write",
}


def _load_boundary_module() -> tuple[type[Exception], object, object]:
    cwd = Path.cwd()
    runtime_root = _leader_root(cwd) / ".agent-flow" / "runtime" / "python"
    if runtime_root.is_dir():
        sys.path.insert(0, str(runtime_root))
    try:
        from agent_flow.core.workspace_boundary import (
            WorkspaceBoundaryError,
            find_active_pinned_workspace,
            resolve_mutation_path,
        )
    except ImportError as exc:
        raise RuntimeError("pinned workspace guard runtime is unavailable") from exc
    return WorkspaceBoundaryError, find_active_pinned_workspace, resolve_mutation_path


def _leader_root(cwd: Path) -> Path:
    result = subprocess.run(
        ("git", "-C", str(cwd), "rev-parse", "--path-format=absolute", "--git-common-dir"),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        return cwd.resolve()
    common = Path(result.stdout.strip()).resolve(strict=False)
    return common.parent if common.name == ".git" else cwd.resolve()


def _tool_name(payload: dict[str, object]) -> str:
    for key in ("tool_name", "toolName", "name"):
        value = payload.get(key)
        if isinstance(value, str):
            return value.lower()
    return ""


def _tool_input(payload: dict[str, object]) -> dict[str, object]:
    value = payload.get("tool_input")
    if not isinstance(value, dict):
        value = payload.get("input")
    return value if isinstance(value, dict) else {}


def _requested_paths(tool_input: dict[str, object]) -> list[str]:
    paths: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        for key in ("file_path", "filePath", "filename", "path"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                paths.append(candidate)
        patch = value.get("patch")
        if isinstance(patch, str):
            paths.extend(
                match.group(1).strip()
                for match in re.finditer(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", patch, re.MULTILINE)
            )
        edits = value.get("edits")
        if isinstance(edits, list):
            visit(edits)

    visit(tool_input)
    return list(dict.fromkeys(paths))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be an object")
        if _tool_name(payload) not in WRITE_TOOLS:
            return 0
        boundary_error, find_active, resolve_path = _load_boundary_module()
        cwd_value = payload.get("cwd")
        cwd = Path(cwd_value) if isinstance(cwd_value, str) and cwd_value else Path.cwd()
        active = find_active(_leader_root(cwd))
        if active is None:
            return 0
        paths = _requested_paths(_tool_input(payload))
        if not paths:
            raise boundary_error("write boundary rejected: write tool did not declare a target path")
        host = str(payload.get("host") or os.environ.get("AGENT_FLOW_ACTIVE_HOST") or "unknown")
        phase = str(payload.get("phase") or "unknown")
        for requested in paths:
            resolve_path(active.identity, requested, host=host, phase=phase)
        return 0
    except Exception as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
