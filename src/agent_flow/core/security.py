from __future__ import annotations

import re
from pathlib import Path


_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def validate_safe_name(value: str, kind: str) -> str:
    if not _SAFE_NAME.fullmatch(value):
        raise ValueError(f"invalid {kind} name: {value!r}")
    return value


def validate_git_branch(value: str) -> None:
    invalid_chars = set(" ~^:?*[\\")
    invalid = (
        not value
        or value.startswith("-")
        or value.startswith(".")
        or value == "@"
        or value == "HEAD"
        or value.startswith("/")
        or value.endswith("/")
        or value.endswith(".")
        or value.endswith(".lock")
        or ".." in value
        or "//" in value
        or "@{" in value
        or any(
            component.startswith(".") or component.endswith(".lock")
            for component in value.split("/")
        )
        or any(ord(ch) < 32 or ch == "\x7f" or ch in invalid_chars for ch in value)
    )
    if invalid:
        raise ValueError(f"unsafe worktree branch: {value}")


def ensure_child_path(root: Path, path: Path, kind: str) -> Path:
    root_resolved = root.resolve()
    path_resolved = path.resolve()
    if path_resolved.parent != root_resolved:
        raise ValueError(f"{kind} path escapes {root}: {path}")
    return path


def resolve_project_path(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return root / path
