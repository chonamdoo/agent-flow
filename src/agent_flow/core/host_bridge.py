from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import stat as stat_module
import subprocess
from pathlib import Path
from typing import Any

from agent_flow.core.codex_trust import (
    ensure_codex_worktree_hook_trust,
    load_managed_hook_command_policy,
    trusted_managed_hook_command,
)


_MANIFEST_VERSION = 3
_LEGACY_MANIFEST_VERSION = 2
_FLOW_START = "<!-- agent-flow:start -->"
_FLOW_END = "<!-- agent-flow:end -->"
_EXCLUDE_START = "# agent-flow worktree host bridge:start"
_EXCLUDE_END = "# agent-flow worktree host bridge:end"
_MANAGED_HOOK_SCRIPTS = {
    "guard-worktree.sh",
    "guard-worktree-write.py",
    "guard-protected-branch.sh",
    "show-phase-status.sh",
    "comment-checker.py",
}

_BRIDGE_SPECS = (
    {
        "source": "AGENTS.md",
        "destination": "AGENTS.md",
        "kind": "file",
        "source_type": "file",
        "policy": "context",
    },
    {
        "source": "CLAUDE.md",
        "destination": "CLAUDE.md",
        "kind": "file",
        "source_type": "file",
        "policy": "context",
    },
    {
        "source": ".Codex/agents/code-reviewer.md",
        "destination": ".Codex/agents/code-reviewer.md",
        "kind": "file",
        "source_type": "file",
        "policy": "reviewer",
    },
    {
        "source": ".claude/agents/code-reviewer.md",
        "destination": ".claude/agents/code-reviewer.md",
        "kind": "file",
        "source_type": "file",
        "policy": "reviewer",
    },
    {
        "source": ".claude/agents/code-reviewer.md",
        "destination": ".omp/agents/code-reviewer.md",
        "kind": "file",
        "source_type": "file",
        "policy": "reviewer",
    },
    {
        "source": ".Codex/hooks.json",
        "destination": ".Codex/hooks.json",
        "kind": "file",
        "source_type": "file",
        "policy": "hook-json",
    },
    {
        "source": ".codex/hooks.json",
        "destination": ".codex/hooks.json",
        "kind": "file",
        "source_type": "file",
        "policy": "hook-json",
    },
    {
        "source": ".claude/settings.json",
        "destination": ".claude/settings.json",
        "kind": "file",
        "source_type": "file",
        "policy": "hook-json",
    },
    {
        "source": ".agent-flow/bin",
        "destination": ".agent-flow/bin",
        "kind": "symlink",
        "source_type": "directory",
        "policy": "runtime",
    },
    {
        "source": ".agent-flow/skills",
        "destination": ".agent-flow/skills",
        "kind": "symlink",
        "source_type": "directory",
        "policy": "runtime",
    },
    {
        "source": ".agents/skills",
        "destination": ".agents/skills",
        "kind": "symlink",
        "source_type": "directory",
        "policy": "host",
    },
    {
        "source": ".claude/skills",
        "destination": ".claude/skills",
        "kind": "symlink",
        "source_type": "directory",
        "policy": "host",
    },
    {
        "source": ".omp/extensions/agent-flow-hooks.ts",
        "destination": ".omp/extensions/agent-flow-hooks.ts",
        "kind": "symlink",
        "source_type": "file",
        "policy": "host",
    },
    {
        "source": ".omp/skills",
        "destination": ".omp/skills",
        "kind": "symlink",
        "source_type": "directory",
        "policy": "host",
    },
)
_EXCLUDE_PATHS = tuple(f"/{spec['destination']}" for spec in _BRIDGE_SPECS)


def ensure_worktree_host_bridge(*, leader_root: Path, worktree_root: Path) -> None:
    leader = leader_root.resolve(strict=True)
    worktree = worktree_root.resolve(strict=True)
    if leader == worktree:
        return
    _assert_registered_worktree(leader, worktree)

    common_dir = _resolve_git_path(leader, _git_output(leader, "rev-parse", "--git-common-dir"))
    git_dir = _resolve_git_path(worktree, _git_output(worktree, "rev-parse", "--git-dir"))
    try:
        identity = git_dir.relative_to(common_dir).as_posix()
    except ValueError as exc:
        raise RuntimeError(
            f"blocked: worktree git dir is outside the leader git common dir: {git_dir}"
        ) from exc
    if not identity:
        raise RuntimeError(f"blocked: invalid worktree git dir: {git_dir}")
    manifest_path = (
        common_dir
        / "agent-flow"
        / "worktree-host-bridges"
        / f"{_hash_bytes(identity.encode('utf-8'))}.json"
    )
    _assert_safe_parents(common_dir, manifest_path)
    manifest_snapshot = _snapshot_plain_file(manifest_path, "host bridge manifest")
    previous = (
        _parse_manifest(
            manifest_snapshot["content"], manifest_path, leader, worktree
        )
        if manifest_snapshot["exists"]
        else None
    )
    sources = _resolve_sources(leader)
    _validate_previous_links(previous, sources)
    previous_links = {
        entry["destination"]: entry for entry in previous.get("links", [])
    } if previous else {}
    actions: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []

    for source in sources:
        destination = worktree / source["destination"]
        _assert_safe_parents(worktree, destination)
        destination_stat = _lstat_if_exists(destination)
        tracked = bool(_tracked_paths(worktree, source["destination"]))
        old = previous_links.get(source["destination"])
        if source["policy"] == "context":
            planned = _plan_context_file(source, destination, destination_stat, old)
        elif source["policy"] == "hook-json":
            planned = _plan_hook_json(source, destination, destination_stat, tracked, old)
        else:
            planned = _plan_host_path(source, destination, destination_stat, tracked, old)
        if planned["action"] is not None:
            actions.append(planned["action"])
        links.append(planned["link"])

    exclude_path = common_dir / "info" / "exclude"
    _assert_safe_parents(common_dir, exclude_path)
    exclude_snapshot = _snapshot_plain_file(exclude_path, "git info exclude")
    exclude_content = _merged_exclude(
        exclude_snapshot["content"] if exclude_snapshot["exists"] else b"",
        exclude_path,
    )
    unsigned_manifest = {
        "version": _MANIFEST_VERSION,
        "leader_root": str(leader),
        "workspace_root": str(worktree),
        "links": links,
    }
    manifest = {
        **unsigned_manifest,
        "manifest_hash": _hash_bytes(_canonical_json(unsigned_manifest).encode("utf-8")),
    }
    manifest_content = (
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    created_directories: list[Path] = []
    applied_actions: list[dict[str, Any]] = []
    exclude_applied_snapshot: dict[str, Any] | None = None
    manifest_applied_snapshot: dict[str, Any] | None = None

    try:
        for action in actions:
            _ensure_safe_parent_directories(
                worktree, action["destination"], created_directories
            )
            _apply_action(action)
            action["applied"] = _applied_action_snapshot(action)
            applied_actions.append(action)
        _ensure_safe_parent_directories(common_dir, exclude_path, created_directories)
        if not _bytes_equal_snapshot(exclude_content, exclude_snapshot):
            exclude_applied_snapshot = _publish_snapshot_update(
                exclude_path,
                exclude_content,
                exclude_snapshot,
                "git info exclude",
            )
        _ensure_safe_parent_directories(common_dir, manifest_path, created_directories)
        if not _bytes_equal_snapshot(manifest_content, manifest_snapshot):
            manifest_applied_snapshot = _publish_snapshot_update(
                manifest_path,
                manifest_content,
                manifest_snapshot,
                "host bridge manifest",
            )
        ensure_codex_worktree_hook_trust(
            leader_root=leader,
            worktree_root=worktree,
        )
    except BaseException as exc:
        rollback_errors: list[str] = []
        if manifest_applied_snapshot is not None:
            _restore_snapshot(
                manifest_path,
                manifest_snapshot,
                manifest_applied_snapshot,
                rollback_errors,
            )
        if exclude_applied_snapshot is not None:
            _restore_snapshot(
                exclude_path,
                exclude_snapshot,
                exclude_applied_snapshot,
                rollback_errors,
            )
        for action in reversed(applied_actions):
            _rollback_action(action, rollback_errors)
        for directory in reversed(created_directories):
            _remove_empty_directory(directory, rollback_errors)
        if rollback_errors:
            raise RuntimeError(
                f"{exc}; rollback failed: {'; '.join(rollback_errors)}"
            ) from exc
        raise


def _resolve_sources(leader: Path) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    case_destinations: dict[str, Path] = {}
    hook_policy = load_managed_hook_command_policy(leader)
    for spec in _BRIDGE_SPECS:
        source = leader / spec["source"]
        source_stat = _assert_safe_source(leader, source, spec["source_type"])
        destination_key = spec["destination"].casefold()
        existing = case_destinations.get(destination_key)
        if existing is not None and _same_existing_path(existing, source):
            continue
        case_destinations.setdefault(destination_key, source)
        content = source.read_bytes() if spec["kind"] == "file" else None
        flow_block = (
            _extract_flow_block(content, source)
            if spec["policy"] == "context"
            else None
        )
        hook_config = (
            _parse_hook_config(content, source)
            if spec["policy"] == "hook-json"
            else None
        )
        managed_hooks = (
            _managed_hook_entries(hook_config, source, hook_policy)
            if hook_config is not None
            else None
        )
        unmanaged_hooks = (
            _without_managed_hooks(hook_config, hook_policy)
            if hook_config is not None
            else None
        )
        sources.append(
            {
                **spec,
                "source_relative": spec["source"],
                "source_path": source,
                "source_mode": source_stat.st_mode,
                "source_is_directory": stat_module.S_ISDIR(source_stat.st_mode),
                "content": content,
                "source_hash": _hash_bytes(content) if content is not None else None,
                "flow_block": flow_block,
                "flow_hash": _hash_bytes(flow_block) if flow_block is not None else None,
                "hook_config": hook_config,
                "hook_policy": hook_policy,
                "managed_hooks": managed_hooks,
                "managed_hash": (
                    _hash_bytes(_canonical_json(managed_hooks).encode("utf-8"))
                    if managed_hooks is not None
                    else None
                ),
                "unmanaged_hooks": unmanaged_hooks,
            }
        )
    return sources


def _plan_context_file(
    source: dict[str, Any],
    destination: Path,
    destination_stat: os.stat_result | None,
    old: dict[str, Any] | None,
) -> dict[str, Any]:
    if destination_stat is not None and (
        stat_module.S_ISLNK(destination_stat.st_mode)
        or not stat_module.S_ISREG(destination_stat.st_mode)
    ):
        raise RuntimeError(f"blocked: unsafe worktree context path: {destination}")
    current = destination.read_bytes() if destination_stat is not None else b""
    if old is not None:
        _assert_old_link(old, source)
        if old["ownership"] != "managed-block":
            raise RuntimeError(
                f"blocked: invalid host bridge ownership for {source['destination']}"
            )
        if destination_stat is None:
            raise RuntimeError(
                f"blocked: managed worktree context file is missing: {destination}"
            )
        current_block = _extract_flow_block(current, destination)
        if _hash_bytes(current_block) != old["hash"]:
            raise RuntimeError(
                f"blocked: managed worktree context block was modified: {destination}"
            )
    desired = _merge_flow_block(
        current, source["flow_block"], destination, Path(source["destination"]).name
    )
    action = None
    if desired != current:
        action = _file_action(
            destination,
            desired,
            destination_stat.st_mode if destination_stat is not None else source["source_mode"],
            destination_stat,
        )
    return {
        "action": action,
        "link": _manifest_link(source, "managed-block", source["flow_hash"]),
    }


def _plan_hook_json(
    source: dict[str, Any],
    destination: Path,
    destination_stat: os.stat_result | None,
    tracked: bool,
    old: dict[str, Any] | None,
) -> dict[str, Any]:
    if old is not None and old.get("ownership") == "bridge":
        return _plan_host_path(source, destination, destination_stat, tracked, old)
    if destination_stat is not None and (
        stat_module.S_ISLNK(destination_stat.st_mode)
        or not stat_module.S_ISREG(destination_stat.st_mode)
    ):
        raise RuntimeError(f"blocked: unsafe hook settings path: {destination}")
    if destination_stat is None:
        if old is not None:
            raise RuntimeError(
                f"blocked: managed hook settings file is missing: {destination}"
            )
        if tracked:
            raise RuntimeError(f"blocked: tracked host bridge path is missing: {destination}")
        return {
            "action": _file_action(
                destination, source["content"], source["source_mode"], None
            ),
            "link": _manifest_link(source, "bridge", source["source_hash"]),
        }

    current_content = destination.read_bytes()
    current_config = _parse_hook_config(current_content, destination)
    current_managed = _managed_hook_entries(
        current_config,
        destination,
        source["hook_policy"],
        require_managed=False,
    )
    if old is not None:
        _assert_old_link(old, source)
        if old.get("ownership") != "managed-json":
            raise RuntimeError(
                f"blocked: invalid host bridge ownership for {source['destination']}"
            )
        current_managed_hash = _hash_bytes(
            _canonical_json(current_managed).encode("utf-8")
        )
        if current_managed_hash != old["hash"]:
            raise RuntimeError(f"blocked: managed hook settings were modified: {destination}")
    current_unmanaged = _without_managed_hooks(
        current_config, source["hook_policy"]
    )
    if _canonical_json(current_unmanaged) != _canonical_json(source["unmanaged_hooks"]):
        raise RuntimeError(
            f"blocked: user hook settings differ from the leader source: {destination}"
        )
    desired_config = _merge_managed_hooks(
        current_config,
        source["managed_hooks"],
        source["hook_policy"],
    )
    desired_content = (
        json.dumps(desired_config, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    action = None
    if _canonical_json(desired_config) != _canonical_json(current_config):
        action = _file_action(
            destination, desired_content, destination_stat.st_mode, destination_stat
        )
    return {
        "action": action,
        "link": _manifest_link(source, "managed-json", source["managed_hash"]),
    }


def _plan_host_path(
    source: dict[str, Any],
    destination: Path,
    destination_stat: os.stat_result | None,
    tracked: bool,
    old: dict[str, Any] | None,
) -> dict[str, Any]:
    if old is not None:
        _assert_old_link(old, source)
        if old["ownership"] == "tracked":
            if (
                not tracked
                or destination_stat is None
                or stat_module.S_ISLNK(destination_stat.st_mode)
                or not stat_module.S_ISREG(destination_stat.st_mode)
            ):
                raise RuntimeError(f"blocked: tracked host bridge path changed: {destination}")
            current = destination.read_bytes()
            if _hash_bytes(current) != old["hash"]:
                raise RuntimeError(
                    f"blocked: tracked host bridge path was modified: {destination}"
                )
            if current != source["content"]:
                raise RuntimeError(
                    f"blocked: tracked reviewer is stale and cannot be overwritten: {destination}"
                )
            return {
                "action": None,
                "link": _manifest_link(source, "tracked", source["source_hash"]),
            }
        if old["ownership"] != "bridge" or tracked:
            raise RuntimeError(
                f"blocked: invalid host bridge ownership for {source['destination']}"
            )
        if destination_stat is None:
            raise RuntimeError(f"blocked: managed host bridge path is missing: {destination}")
        if source["kind"] == "file":
            if stat_module.S_ISLNK(destination_stat.st_mode) or not stat_module.S_ISREG(
                destination_stat.st_mode
            ):
                raise RuntimeError(
                    f"blocked: managed host bridge file type changed: {destination}"
                )
            current = destination.read_bytes()
            if _hash_bytes(current) != old["hash"]:
                raise RuntimeError(
                    f"blocked: managed host bridge file was modified: {destination}"
                )
            action = None
            if current != source["content"]:
                action = _file_action(
                    destination,
                    source["content"],
                    source["source_mode"],
                    destination_stat,
                )
            return {
                "action": action,
                "link": _manifest_link(source, "bridge", source["source_hash"]),
            }
        desired_target = _relative_link_target(destination, source["source_path"])
        if not stat_module.S_ISLNK(destination_stat.st_mode):
            raise RuntimeError(
                f"blocked: managed host bridge symlink type changed: {destination}"
            )
        current_target = os.readlink(destination)
        if _hash_bytes(current_target.encode("utf-8")) != old["hash"]:
            raise RuntimeError(
                f"blocked: managed host bridge symlink was modified: {destination}"
            )
        if not _same_existing_path(destination.parent / current_target, source["source_path"]):
            raise RuntimeError(
                f"blocked: managed host bridge symlink escaped its source: {destination}"
            )
        if current_target != desired_target:
            raise RuntimeError(
                f"blocked: managed host bridge symlink is non-canonical: {destination}"
            )
        return {
            "action": None,
            "link": _manifest_link(
                source, "bridge", _hash_bytes(desired_target.encode("utf-8"))
            ),
        }

    if destination_stat is not None:
        if (
            tracked
            and source["policy"] == "reviewer"
            and stat_module.S_ISREG(destination_stat.st_mode)
            and not stat_module.S_ISLNK(destination_stat.st_mode)
            and destination.read_bytes() == source["content"]
        ):
            return {
                "action": None,
                "link": _manifest_link(source, "tracked", source["source_hash"]),
            }
        raise RuntimeError(f"blocked: unmanaged host bridge path already exists: {destination}")
    if tracked:
        raise RuntimeError(f"blocked: tracked host bridge path is missing: {destination}")
    if source["kind"] == "file":
        return {
            "action": _file_action(
                destination, source["content"], source["source_mode"], None
            ),
            "link": _manifest_link(source, "bridge", source["source_hash"]),
        }
    desired_target = _relative_link_target(destination, source["source_path"])
    return {
        "action": {
            "kind": "symlink",
            "destination": destination,
            "target": desired_target,
            "source_is_directory": source["source_is_directory"],
            "before": {"exists": False},
        },
        "link": _manifest_link(
            source, "bridge", _hash_bytes(desired_target.encode("utf-8"))
        ),
    }


def _file_action(
    destination: Path,
    content: bytes,
    mode: int,
    destination_stat: os.stat_result | None,
) -> dict[str, Any]:
    before = {"exists": False}
    if destination_stat is not None:
        before = {
            "exists": True,
            "content": destination.read_bytes(),
            "mode": destination_stat.st_mode & 0o777,
            "dev": destination_stat.st_dev,
            "ino": destination_stat.st_ino,
            "nlink": destination_stat.st_nlink,
        }
    return {
        "kind": "file",
        "destination": destination,
        "content": content,
        "mode": mode & 0o777,
        "before": before,
    }


def _manifest_link(
    source: dict[str, Any], ownership: str, content_hash: str
) -> dict[str, Any]:
    return {
        "destination": source["destination"],
        "kind": source["kind"],
        "ownership": ownership,
        "policy": source["policy"],
        "source": source["source_relative"],
        "target": str(source["source_path"]),
        "hash": content_hash,
    }


def _validate_previous_links(
    previous: dict[str, Any] | None, sources: list[dict[str, Any]]
) -> None:
    if previous is None:
        return
    sources_by_destination = {source["destination"]: source for source in sources}
    expected_destinations = sorted(
        source["destination"]
        for source in sources
        if previous["version"] == _MANIFEST_VERSION or source["policy"] != "runtime"
    )
    raw_destinations = [
        entry.get("destination") if isinstance(entry, dict) else None
        for entry in previous["links"]
    ]
    if (
        any(not isinstance(destination, str) for destination in raw_destinations)
        or sorted(raw_destinations) != expected_destinations
    ):
        label = (
            "legacy host bridge"
            if previous["version"] == _LEGACY_MANIFEST_VERSION
            else "host bridge"
        )
        raise RuntimeError(f"blocked: invalid {label} manifest: link set changed")
    expected_keys = {
        "destination", "hash", "kind", "ownership", "policy", "source", "target"
    }
    for entry in previous["links"]:
        if not isinstance(entry, dict) or set(entry) != expected_keys:
            raise RuntimeError("blocked: invalid host bridge manifest: invalid link schema")
        source = sources_by_destination.get(entry.get("destination"))
        if source is None or (
            previous["version"] == _LEGACY_MANIFEST_VERSION
            and source["policy"] == "runtime"
        ):
            raise RuntimeError("blocked: invalid host bridge manifest: unexpected link")
        _assert_old_link(entry, source)
        if not isinstance(entry["hash"], str) or not _is_sha256(entry["hash"]):
            raise RuntimeError("blocked: invalid host bridge manifest: invalid link hash")


def _assert_old_link(entry: dict[str, Any], source: dict[str, Any]) -> None:
    if (
        entry.get("destination") != source["destination"]
        or entry.get("kind") != source["kind"]
        or entry.get("policy") != source["policy"]
        or entry.get("source") != source["source_relative"]
        or entry.get("target") != str(source["source_path"])
        or entry.get("ownership") not in {
            "bridge", "tracked", "managed-block", "managed-json"
        }
    ):
        raise RuntimeError(
            f"blocked: invalid host bridge manifest entry: {source['destination']}"
        )


def _parse_manifest(
    content: bytes, manifest_path: Path, leader: Path, worktree: Path
) -> dict[str, Any]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"blocked: invalid host bridge manifest {manifest_path}: {exc}"
        ) from exc
    expected_keys = {
        "leader_root", "links", "manifest_hash", "version", "workspace_root"
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected_keys
        or value.get("version") not in {_LEGACY_MANIFEST_VERSION, _MANIFEST_VERSION}
        or value.get("leader_root") != str(leader)
        or value.get("workspace_root") != str(worktree)
        or not isinstance(value.get("links"), list)
        or not isinstance(value.get("manifest_hash"), str)
    ):
        raise RuntimeError(
            f"blocked: invalid host bridge manifest {manifest_path}: invalid schema"
        )
    unsigned = {
        "version": value["version"],
        "leader_root": value["leader_root"],
        "workspace_root": value["workspace_root"],
        "links": value["links"],
    }
    if _hash_bytes(_canonical_json(unsigned).encode("utf-8")) != value["manifest_hash"]:
        raise RuntimeError(
            f"blocked: invalid host bridge manifest {manifest_path}: hash mismatch"
        )
    return value


def _extract_flow_block(content: bytes, file: Path) -> bytes:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"blocked: worktree context is not valid UTF-8: {file}") from exc
    start = text.find(_FLOW_START)
    end = text.find(_FLOW_END)
    if (
        start < 0
        or end < start
        or text.find(_FLOW_START, start + len(_FLOW_START)) >= 0
        or text.find(_FLOW_END, end + len(_FLOW_END)) >= 0
    ):
        raise RuntimeError(f"blocked: malformed agent-flow context block in {file}")
    return text[start : end + len(_FLOW_END)].encode("utf-8")


def _merge_flow_block(current: bytes, desired_block: bytes, file: Path, label: str) -> bytes:
    try:
        text = current.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"blocked: worktree context is not valid UTF-8: {file}") from exc
    start = text.find(_FLOW_START)
    end = text.find(_FLOW_END)
    if (start < 0) != (end < 0):
        raise RuntimeError(f"blocked: malformed agent-flow context block in {file}")
    if (
        (start >= 0 and end < start)
        or (start >= 0 and text.find(_FLOW_START, start + len(_FLOW_START)) >= 0)
        or (end >= 0 and text.find(_FLOW_END, end + len(_FLOW_END)) >= 0)
    ):
        raise RuntimeError(f"blocked: malformed agent-flow context block in {file}")
    if start >= 0:
        block = desired_block.decode("utf-8")
        return (
            text[:start] + block + text[end + len(_FLOW_END) :]
        ).encode("utf-8")
    if not current:
        return f"# {label}\n\n".encode("utf-8") + desired_block + b"\n"
    if current.endswith((b"\r\n\r\n", b"\n\n")):
        separator = b""
    elif current.endswith(b"\r\n"):
        separator = b"\r\n"
    elif current.endswith(b"\n"):
        separator = b"\n"
    else:
        newline = b"\r\n" if b"\r\n" in current else b"\n"
        separator = newline * 2
    return current + separator + desired_block + b"\n"


def _merged_exclude(current: bytes, exclude_path: Path) -> bytes:
    try:
        text = current.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(
            f"blocked: git info exclude is not valid UTF-8: {exclude_path}"
        ) from exc
    block = "\n".join((_EXCLUDE_START, *_EXCLUDE_PATHS, _EXCLUDE_END))
    start = text.find(_EXCLUDE_START)
    end = text.find(_EXCLUDE_END)
    if (
        (start < 0) != (end < 0)
        or (start >= 0 and end < start)
        or (start >= 0 and text.find(_EXCLUDE_START, start + len(_EXCLUDE_START)) >= 0)
        or (end >= 0 and text.find(_EXCLUDE_END, end + len(_EXCLUDE_END)) >= 0)
    ):
        raise RuntimeError(
            f"blocked: malformed agent-flow host bridge block in {exclude_path}"
        )
    if start < 0:
        prefix = text.rstrip()
        return f"{prefix}{chr(10) * 2 if prefix else ''}{block}\n".encode("utf-8")
    next_value = text[:start] + block + text[end + len(_EXCLUDE_END) :]
    if not next_value.endswith("\n"):
        next_value += "\n"
    return next_value.encode("utf-8")


def _parse_hook_config(content: bytes, file: Path) -> dict[str, Any]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"blocked: invalid hook settings JSON {file}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(
            f"blocked: invalid hook settings JSON {file}: expected an object"
        )
    return value


def _managed_hook_entries(
    config: dict[str, Any],
    file: Path,
    hook_policy: dict[str, Any],
    require_managed: bool = True,
) -> list[dict[str, Any]]:
    if "hooks" not in config:
        if require_managed:
            raise RuntimeError(
                f"blocked: leader hook bridge source has no managed hooks: {file}"
            )
        return []
    hooks = config["hooks"]
    if not isinstance(hooks, dict):
        raise RuntimeError(f"blocked: invalid hook settings schema: {file}")
    result: list[dict[str, Any]] = []
    managed_matchers: set[str] = set()
    for event, entries in hooks.items():
        if not isinstance(event, str) or not isinstance(entries, list):
            raise RuntimeError(f"blocked: invalid hook settings schema: {file}")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                raise RuntimeError(f"blocked: invalid hook settings schema: {file}")
            managed: list[dict[str, Any]] = []
            for hook in entry["hooks"]:
                if not isinstance(hook, dict):
                    raise RuntimeError(f"blocked: invalid hook settings schema: {file}")
                candidate_name = _managed_hook_script_name(hook.get("command"))
                trusted = trusted_managed_hook_command(
                    hook_policy, hook.get("command")
                )
                if trusted is None:
                    if candidate_name:
                        raise RuntimeError(
                            f"blocked: managed hook command is not immutable in {file}"
                        )
                    continue
                if hook.get("type") != "command":
                    raise RuntimeError(f"blocked: invalid managed hook type in {file}")
                managed.append(_json_clone(hook))
            if not managed:
                continue
            matcher = entry.get("matcher") if isinstance(entry.get("matcher"), str) else ""
            matcher_key = f"{event}\0{matcher}"
            if matcher_key in managed_matchers:
                raise RuntimeError(f"blocked: duplicate managed hook matcher in {file}")
            managed_matchers.add(matcher_key)
            result.append({"event": event, "matcher": matcher, "hooks": managed})
    if require_managed and not result:
        raise RuntimeError(
            f"blocked: leader hook bridge source has no managed hooks: {file}"
        )
    return result


def _without_managed_hooks(
    config: dict[str, Any], hook_policy: dict[str, Any]
) -> dict[str, Any]:
    clean = _json_clone(config)
    hooks = clean.get("hooks")
    if not isinstance(hooks, dict):
        return clean
    for event, entries in list(hooks.items()):
        remaining_entries: list[dict[str, Any]] = []
        for entry in entries:
            remaining_hooks = []
            for hook in entry["hooks"]:
                candidate_name = _managed_hook_script_name(hook.get("command"))
                trusted = trusted_managed_hook_command(
                    hook_policy, hook.get("command")
                )
                if candidate_name and trusted is None:
                    raise RuntimeError("blocked: managed hook command is not immutable")
                if trusted is None:
                    remaining_hooks.append(hook)
            other_keys = [key for key in entry if key not in {"matcher", "hooks"}]
            if not remaining_hooks and not other_keys:
                continue
            remaining_entries.append({**entry, "hooks": remaining_hooks})
        if remaining_entries:
            hooks[event] = remaining_entries
        else:
            del hooks[event]
    if not hooks:
        del clean["hooks"]
    return clean


def _merge_managed_hooks(
    current: dict[str, Any],
    managed_entries: list[dict[str, Any]],
    hook_policy: dict[str, Any],
) -> dict[str, Any]:
    desired = _without_managed_hooks(current, hook_policy)
    hooks = desired.setdefault("hooks", {})
    for managed in managed_entries:
        entries = hooks.setdefault(managed["event"], [])
        entry = next(
            (
                candidate
                for candidate in entries
                if (
                    candidate.get("matcher")
                    if isinstance(candidate.get("matcher"), str)
                    else ""
                )
                == managed["matcher"]
            ),
            None,
        )
        if entry is None:
            entry = {
                **({"matcher": managed["matcher"]} if managed["matcher"] else {}),
                "hooks": [],
            }
            entries.append(entry)
        entry["hooks"].extend(_json_clone(managed["hooks"]))
    return desired


def _managed_hook_script_name(command: Any) -> str:
    if not isinstance(command, str):
        return ""
    for match in re.finditer(
        r"(?:^|[\s'\"])([A-Za-z0-9+/]+={0,2})(?=$|[\s'\"])", command
    ):
        try:
            decoded = base64.b64decode(match.group(1), validate=True)
            decoded_path = decoded.decode("utf-8")
            if base64.b64encode(decoded).decode("ascii") == match.group(1):
                script_name = Path(decoded_path).name
                if script_name in _MANAGED_HOOK_SCRIPTS:
                    return script_name
        except (UnicodeError, ValueError):
            pass
    if all(
        marker in command
        for marker in (
            "AGENT_FLOW_MANAGED_HOOK_SOURCE_PATH",
            "agent-flow-managed-hook-",
            "descriptor execution unavailable",
        )
    ):
        return "__managed-verifier__"
    normalized = _normalized_hook_command(command).replace("'", "").replace('"', "")
    for script_name in _MANAGED_HOOK_SCRIPTS:
        if (
            f"/.agent-flow/scripts/hooks/{script_name}" in normalized
            or f"/scripts/hooks/{script_name}" in normalized
        ):
            return script_name
    return ""


def _normalized_hook_command(command: Any) -> str:
    normalized = str(command).strip()
    if normalized.startswith("'") and normalized.endswith("'"):
        normalized = normalized[1:-1].replace("'\\''", "'")
    elif normalized.startswith('"') and normalized.endswith('"'):
        normalized = normalized[1:-1]
    return normalized.replace("\\", "/")


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _apply_action(action: dict[str, Any]) -> None:
    _assert_action_unchanged(action)
    destination = action["destination"]
    if action["kind"] == "file":
        if action["before"]["exists"]:
            _replace_file_no_clobber(action)
        else:
            _create_file_exclusive(destination, action["content"], action["mode"])
        return
    destination.symlink_to(
        action["target"], target_is_directory=action["source_is_directory"]
    )


def _applied_action_snapshot(action: dict[str, Any]) -> dict[str, Any]:
    destination = action["destination"]
    current = _lstat_if_exists(destination)
    if action["kind"] == "symlink":
        if (
            current is None
            or not stat_module.S_ISLNK(current.st_mode)
            or os.readlink(destination) != action["target"]
        ):
            raise RuntimeError(
                f"blocked: host bridge symlink changed after publish: {destination}"
            )
        return {
            "kind": "symlink",
            "target": action["target"],
            "dev": current.st_dev,
            "ino": current.st_ino,
            "nlink": current.st_nlink,
        }
    snapshot = _snapshot_plain_file(destination, "published host bridge file")
    if snapshot["content"] != action["content"] or snapshot["mode"] != action["mode"]:
        raise RuntimeError(
            f"blocked: host bridge file changed after publish: {destination}"
        )
    return snapshot


def _assert_action_unchanged(action: dict[str, Any]) -> None:
    current = _lstat_if_exists(action["destination"])
    if not action["before"]["exists"]:
        if current is not None:
            raise RuntimeError(
                f"blocked: host bridge path changed during creation: {action['destination']}"
            )
        return
    if (
        current is None
        or stat_module.S_ISLNK(current.st_mode)
        or not stat_module.S_ISREG(current.st_mode)
        or action["destination"].read_bytes() != action["before"]["content"]
        or current.st_mode & 0o777 != action["before"]["mode"]
        or ("dev" in action["before"] and current.st_dev != action["before"]["dev"])
        or ("ino" in action["before"] and current.st_ino != action["before"]["ino"])
        or ("nlink" in action["before"] and current.st_nlink != action["before"]["nlink"])
    ):
        raise RuntimeError(
            f"blocked: host bridge path changed during refresh: {action['destination']}"
        )


def _replace_file_no_clobber(action: dict[str, Any]) -> None:
    destination = action["destination"]
    token = secrets.token_hex(8)
    replacement = destination.with_name(f".{destination.name}.bridge-new-{token}")
    displaced = destination.with_name(f".{destination.name}.bridge-old-{token}")
    descriptor = os.open(
        replacement,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        action["mode"] & 0o777,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(action["content"])
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(replacement, action["mode"] & 0o777)
        _assert_action_unchanged(action)
        os.replace(destination, displaced)
        previous = _snapshot_plain_file(displaced, "displaced host bridge file")
        before = action["before"]
        matches = (
            previous["content"] == before["content"]
            and previous["mode"] == before["mode"]
            and ("dev" not in before or previous["dev"] == before["dev"])
            and ("ino" not in before or previous["ino"] == before["ino"])
            and ("nlink" not in before or previous["nlink"] == before["nlink"])
        )
        if not matches:
            if _lstat_if_exists(destination) is None:
                os.link(displaced, destination)
            raise RuntimeError(
                f"blocked: host bridge path changed during refresh: {destination}"
            )
        try:
            os.link(replacement, destination)
        except FileExistsError as exc:
            raise RuntimeError(
                f"blocked: host bridge path changed during publish: {destination}"
            ) from exc
    finally:
        replacement.unlink(missing_ok=True)
        displaced.unlink(missing_ok=True)


def _rollback_action(action: dict[str, Any], errors: list[str]) -> None:
    destination = action["destination"]
    try:
        current = _lstat_if_exists(destination)
        applied = action.get("applied")
        if not isinstance(applied, dict):
            return
        if action["kind"] == "symlink":
            if (
                current is not None
                and stat_module.S_ISLNK(current.st_mode)
                and os.readlink(destination) == applied["target"]
                and current.st_dev == applied["dev"]
                and current.st_ino == applied["ino"]
                and current.st_nlink == applied["nlink"]
            ):
                destination.unlink()
            return
        if not _snapshot_matches(destination, applied):
            return
        if action["before"]["exists"]:
            _replace_file_no_clobber(
                {
                    "kind": "file",
                    "destination": destination,
                    "content": action["before"]["content"],
                    "mode": action["before"]["mode"],
                    "before": applied,
                }
            )
        else:
            _remove_file_no_clobber(destination, applied)
    except (OSError, RuntimeError) as exc:
        errors.append(f"{destination}: {exc}")


def _snapshot_plain_file(path: Path, label: str) -> dict[str, Any]:
    current = _lstat_if_exists(path)
    if current is None:
        return {"exists": False, "mode": 0o644, "content": None}
    if stat_module.S_ISLNK(current.st_mode) or not stat_module.S_ISREG(current.st_mode):
        raise RuntimeError(f"blocked: unsafe {label}: {path}")
    return {
        "exists": True,
        "mode": current.st_mode & 0o777,
        "content": path.read_bytes(),
        "dev": current.st_dev,
        "ino": current.st_ino,
        "nlink": current.st_nlink,
    }


def _assert_snapshot_unchanged(
    path: Path, snapshot: dict[str, Any], label: str
) -> None:
    if not _snapshot_matches(path, snapshot):
        raise RuntimeError(f"blocked: {label} changed during host bridge update: {path}")


def _publish_snapshot_update(
    path: Path,
    content: bytes,
    snapshot: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    action = {
        "kind": "file",
        "destination": path,
        "content": content,
        "mode": int(snapshot.get("mode", 0o644)),
        "before": snapshot,
    }
    _assert_snapshot_unchanged(path, snapshot, label)
    if snapshot["exists"]:
        _replace_file_no_clobber(action)
    else:
        _create_file_exclusive(path, content, action["mode"])
    return _snapshot_plain_file(path, label)


def _restore_snapshot(
    path: Path,
    snapshot: dict[str, Any],
    applied_snapshot: dict[str, Any],
    errors: list[str],
) -> None:
    try:
        if not _snapshot_matches(path, applied_snapshot):
            return
        if snapshot["exists"]:
            _publish_snapshot_update(
                path,
                snapshot["content"],
                applied_snapshot,
                "host bridge rollback file",
            )
        else:
            _remove_file_no_clobber(path, applied_snapshot)
    except (OSError, RuntimeError) as exc:
        errors.append(f"{path}: {exc}")


def _snapshot_matches(path: Path, snapshot: dict[str, Any]) -> bool:
    current = _lstat_if_exists(path)
    if not snapshot["exists"]:
        return current is None
    return bool(
        current is not None
        and not stat_module.S_ISLNK(current.st_mode)
        and stat_module.S_ISREG(current.st_mode)
        and path.read_bytes() == snapshot["content"]
        and current.st_mode & 0o777 == snapshot["mode"]
        and ("dev" not in snapshot or current.st_dev == snapshot["dev"])
        and ("ino" not in snapshot or current.st_ino == snapshot["ino"])
        and ("nlink" not in snapshot or current.st_nlink == snapshot["nlink"])
    )


def _remove_file_no_clobber(path: Path, snapshot: dict[str, Any]) -> None:
    displaced = path.with_name(
        f"{path.name}.agent-flow-remove-{os.getpid()}-{secrets.token_hex(6)}.tmp"
    )
    _assert_snapshot_unchanged(path, snapshot, "host bridge rollback file")
    os.replace(path, displaced)
    try:
        if not _snapshot_matches(displaced, snapshot):
            if _lstat_if_exists(path) is None:
                os.link(displaced, path)
            raise RuntimeError(
                f"blocked: host bridge rollback file changed during removal: {path}"
            )
    finally:
        displaced.unlink(missing_ok=True)


def _bytes_equal_snapshot(content: bytes, snapshot: dict[str, Any]) -> bool:
    return bool(snapshot["exists"] and content == snapshot["content"])


def _assert_registered_worktree(leader: Path, worktree: Path) -> None:
    listed = _git_output(leader, "worktree", "list", "--porcelain")
    registered = any(
        line.startswith("worktree ")
        and _same_existing_path(Path(line.removeprefix("worktree ")), worktree)
        for line in listed.splitlines()
    )
    if not registered:
        raise RuntimeError(
            f"blocked: host bridge target is not a registered git worktree: {worktree}"
        )


def _tracked_paths(worktree: Path, relative: str) -> list[str]:
    result = subprocess.run(
        ("git", "ls-files", "--", relative),
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or f"git ls-files failed for {relative}").strip())
    return [line for line in result.stdout.splitlines() if line]


def _assert_safe_source(
    leader: Path, source: Path, expected_type: str
) -> os.stat_result:
    try:
        relative = source.relative_to(leader)
    except ValueError as exc:
        raise RuntimeError(
            f"blocked: leader host bridge source escapes the leader checkout: {source}"
        ) from exc
    current = leader
    for index, part in enumerate(relative.parts):
        current /= part
        stat = _lstat_if_exists(current)
        if stat is None:
            raise RuntimeError(f"blocked: leader host bridge source is missing: {source}")
        if stat_module.S_ISLNK(stat.st_mode):
            raise RuntimeError(
                f"blocked: unsafe leader host bridge source symlink: {current}"
            )
        if index < len(relative.parts) - 1 and not stat_module.S_ISDIR(stat.st_mode):
            raise RuntimeError(
                f"blocked: unsafe leader host bridge source parent: {current}"
            )
    stat = source.lstat()
    valid = (
        stat_module.S_ISREG(stat.st_mode)
        if expected_type == "file"
        else expected_type == "directory" and stat_module.S_ISDIR(stat.st_mode)
    )
    if not valid:
        raise RuntimeError(f"blocked: invalid leader host bridge source type: {source}")
    return stat


def _assert_safe_parents(boundary: Path, destination: Path) -> None:
    try:
        relative = destination.parent.relative_to(boundary)
    except ValueError as exc:
        raise RuntimeError(
            f"blocked: host bridge destination escapes its boundary: {destination}"
        ) from exc
    current = boundary
    for part in relative.parts:
        current /= part
        stat = _lstat_if_exists(current)
        if stat is not None and (
            stat_module.S_ISLNK(stat.st_mode) or not stat_module.S_ISDIR(stat.st_mode)
        ):
            raise RuntimeError(f"blocked: unsafe host bridge parent path: {current}")


def _ensure_safe_parent_directories(
    boundary: Path, destination: Path, created: list[Path]
) -> None:
    _assert_safe_parents(boundary, destination)
    relative = destination.parent.relative_to(boundary)
    current = boundary
    for part in relative.parts:
        current /= part
        stat = _lstat_if_exists(current)
        if stat is not None:
            continue
        try:
            current.mkdir()
            created.append(current)
        except FileExistsError:
            raced = _lstat_if_exists(current)
            if raced is None or stat_module.S_ISLNK(raced.st_mode) or not stat_module.S_ISDIR(
                raced.st_mode
            ):
                raise


def _remove_empty_directory(directory: Path, errors: list[str]) -> None:
    try:
        stat = _lstat_if_exists(directory)
        if (
            stat is not None
            and stat_module.S_ISDIR(stat.st_mode)
            and not stat_module.S_ISLNK(stat.st_mode)
            and not any(directory.iterdir())
        ):
            directory.rmdir()
    except OSError as exc:
        errors.append(f"{directory}: {exc}")


def _atomic_write_bytes(path: Path, content: bytes, mode: int) -> None:
    temporary = _temporary_path(path)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode & 0o777)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _create_file_exclusive(path: Path, content: bytes, mode: int) -> None:
    temporary = _temporary_path(path)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode & 0o777)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _temporary_path(path: Path) -> Path:
    return path.with_name(
        f"{path.name}.agent-flow-{os.getpid()}-{secrets.token_hex(6)}.tmp"
    )


def _relative_link_target(destination: Path, source: Path) -> str:
    return os.path.relpath(source, destination.parent)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _hash_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _resolve_git_path(cwd: Path, value: str) -> Path:
    candidate = Path(value)
    return (candidate if candidate.is_absolute() else cwd / candidate).resolve(strict=True)


def _git_output(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or f"git {' '.join(args)} failed").strip())
    return result.stdout.strip()


def _same_existing_path(left: Path | str, right: Path | str) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return Path(left).resolve(strict=False) == Path(right).resolve(strict=False)


def _lstat_if_exists(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
