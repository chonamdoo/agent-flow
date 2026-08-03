from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

_BINDING_VERSION = 1
_SAFE_DIGEST = re.compile(r"[0-9a-f]{64}")


def _agent_flow_home() -> Path:
    configured = os.environ.get("AGENT_FLOW_HOME") or os.environ.get("AGENT_FLOW_SHARED_HOME")
    return Path(configured).expanduser().resolve() if configured else Path.home() / ".agent-flow"


def _binding_path(run_path: Path) -> tuple[Path, str]:
    canonical = str(run_path.resolve())
    key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return _agent_flow_home() / "run-bindings" / f"{key}.json", canonical


def _ensure_binding_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    identity = directory.lstat()
    if not stat.S_ISDIR(identity.st_mode) or directory.is_symlink():
        raise RuntimeError(f"run binding directory is unsafe: {directory}")
    if identity.st_uid != os.getuid() or stat.S_IMODE(identity.st_mode) != 0o700:
        raise RuntimeError(f"run binding directory has unsafe ownership or mode: {directory}")


def _read_binding(path: Path) -> dict[str, object]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        identity = os.fstat(descriptor)
        if (
            not stat.S_ISREG(identity.st_mode)
            or identity.st_uid != os.getuid()
            or identity.st_nlink != 1
            or stat.S_IMODE(identity.st_mode) != 0o600
        ):
            raise RuntimeError(f"run binding is unsafe: {path}")
        with os.fdopen(os.dup(descriptor), "r", encoding="utf-8") as stream:
            payload = json.load(stream)
    finally:
        os.close(descriptor)
    if not isinstance(payload, dict):
        raise RuntimeError(f"run binding is invalid: {path}")
    return payload


def bind_run_runtime(run_path: Path, digest: str) -> Path:
    if _SAFE_DIGEST.fullmatch(digest) is None:
        raise ValueError("invalid hook runtime digest")
    target, canonical = _binding_path(run_path)
    _ensure_binding_directory(target.parent)
    payload = {
        "protocol_version": _BINDING_VERSION,
        "run_path": canonical,
        "runtime_digest": digest,
    }
    if target.exists():
        existing = _read_binding(target)
        if existing != payload:
            raise RuntimeError(f"run runtime binding conflicts with private state: {canonical}")
        return target
    staging = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            content = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
            remaining = memoryview(content)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0 or written > len(remaining):
                    raise OSError("failed to write complete run runtime binding")
                remaining = remaining[written:]
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(staging, target)
    finally:
        try:
            staging.unlink()
        except FileNotFoundError:
            pass
    return target


def unbind_run_runtime(run_path: Path) -> None:
    target, canonical = _binding_path(run_path)
    if not target.exists():
        return
    payload = _read_binding(target)
    if payload.get("run_path") != canonical:
        raise RuntimeError(f"run runtime binding path mismatch: {canonical}")
    target.unlink()
