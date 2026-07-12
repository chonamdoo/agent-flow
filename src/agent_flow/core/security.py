from __future__ import annotations

import re
from pathlib import Path


_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_PORTABLE_SKILL_NAME = re.compile(r"[A-Za-z0-9._-]+")


def validate_safe_name(value: str, kind: str) -> str:
    if not _SAFE_NAME.fullmatch(value):
        raise ValueError(f"invalid {kind} name: {value!r}")
    return value


def is_portable_skill_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and _PORTABLE_SKILL_NAME.fullmatch(value) is not None
        and not value.startswith(".")
        and ".." not in value
    )


def validate_portable_skill_name(value: str, kind: str = "skill") -> str:
    if not is_portable_skill_name(value):
        raise ValueError(f"invalid {kind} name: {value!r}")
    return value


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
