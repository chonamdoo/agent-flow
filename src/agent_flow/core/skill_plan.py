from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import unicodedata
from pathlib import Path
from typing import Any, Iterable

import yaml

from agent_flow.core.skill_compatibility import (
    SkillCompatibilityCatalog,
    SkillCompatibilityError,
    SkillResolutionError,
    normalize_skill_compatibility,
)
from agent_flow.core.profiles import load_project_profile_payload, primary_profile_id
from agent_flow.core.security import (
    ensure_child_path,
    is_portable_skill_name,
    validate_portable_skill_name,
    validate_safe_name,
)


CODE_SKILL_PHASES = frozenset(
    {
        "implement",
        "implement-fix",
        "red",
        "green",
        "refactor",
        "fix-loop",
        "final-review",
        "review",
        "pr-comment-fix",
        "pr-ci-fix",
        "multi-review",
        "architecture-review",
    }
)
EXPLICIT_ONLY_SKILLS = frozenset({"testing-localization"})
REVIEW_SKILL_PHASES = frozenset({"final-review", "review", "multi-review", "architecture-review"})
GENERIC_FILE_RULE_SCORE = 20
SKILL_PLAN_HASH_VERSION = 2
RESOLVED_SKILL_LOCK_VERSION = 1
SKILL_LINKS_COMMITMENT_VERSION = 2
MANAGED_HOST_FILES_VERSION = 1
MANAGED_HOST_FILES_COMMITMENT_VERSION = 1
MANAGED_HOOK_CONTRACT_VERSION = 3
MANAGED_HOOK_CONTRACT_COMMITMENT_VERSION = 2
MANAGED_HOOK_SCRIPT_NAMES = (
    "guard-worktree.sh",
    "guard-worktree-write.py",
    "guard-protected-branch.sh",
    "show-phase-status.sh",
    "comment-checker.py",
)
MANAGED_HOOK_CONFIG_PATHS = (
    ".Codex/hooks.json",
    ".codex/hooks.json",
    ".claude/settings.json",
)
WRITE_TOOL_MATCHER = (
    "^(apply_patch|Write|Edit|MultiEdit|NotebookEdit|Eval|Python|Notebook|"
    "write|edit|multi_edit|multiedit|notebook_edit|notebookedit|eval|python|notebook)$"
)
REQUIRED_MANAGED_HOST_FILES = (
    ".Codex/agents/code-reviewer.md",
    ".claude/agents/code-reviewer.md",
    ".omp/agents/code-reviewer.md",
    ".omp/extensions/agent-flow-hooks.ts",
)
PROJECT_SKILL_HOSTS = ("claude", "codex", "omp")
_SKILL_LINK_HOSTS = frozenset((*PROJECT_SKILL_HOSTS, "gemini", "antigravity"))
_SKILL_LINK_STATUSES = frozenset(
    {
        "linked",
        "copied",
        "removed-stale-linked",
        "removed-stale-copied",
    }
)


class SkillPlanSnapshotError(RuntimeError):
    """설치 또는 run 고정 skill snapshot을 신뢰할 수 없을 때 발생한다."""


class SkillDocumentResolutionError(SkillPlanSnapshotError, SkillResolutionError):
    def __init__(self, skill_name: str, path: Path, state: str) -> None:
        SkillResolutionError.__init__(
            self,
            (
                {
                    "reason": "skill_document_unavailable",
                    "requested": skill_name,
                    "canonical": skill_name,
                    "capabilities": [],
                    "state": state,
                    "path": str(path),
                    "repairable": False,
                },
            ),
        )




def indexed_external_exposure_skill_names(
    selection: object,
    *,
    legacy_fallback: bool = True,
) -> tuple[str, ...]:
    """명시적 외부 closure에 노출된 검증 완료 논리 이름을 반환한다."""
    if not isinstance(selection, dict):
        selection = {}
    has_exposure = "external_exposure_skills" in selection
    if has_exposure:
        raw_names = selection.get("external_exposure_skills")
    elif legacy_fallback:
        raw_names = selection.get("explicit_skills", [])
    else:
        raw_names = []
    if not isinstance(raw_names, list):
        raise SkillPlanSnapshotError(
            "blocked: installed external skill exposure list is invalid"
        )
    names: list[str] = []
    seen: set[str] = set()
    for raw_name in raw_names:
        try:
            name = validate_portable_skill_name(raw_name).lower()
        except (TypeError, ValueError) as exc:
            raise SkillPlanSnapshotError(
                f"blocked: invalid external skill exposure name: {raw_name!r}"
            ) from exc
        if name in seen:
            raise SkillPlanSnapshotError(
                f"blocked: duplicate external skill exposure name: {raw_name}"
            )
        seen.add(name)
        names.append(name)
    return tuple(names)


def compute_skill_plan_hash(
    index: dict[str, Any],
    index_root: Path,
    *,
    verify_trees: bool = False,
) -> str:
    """설치된 skill index의 Node v2 전체 plan hash를 반환한다."""
    return hashlib.sha256(
        canonical_skill_plan_bytes(index, index_root, verify_trees=verify_trees)
    ).hexdigest()


def canonical_skill_plan_bytes(
    index: dict[str, Any],
    index_root: Path,
    *,
    verify_trees: bool = False,
) -> bytes:
    """Node의 JSON.stringify 계약과 동일한 정규화 payload를 직렬화한다."""
    compatibility_catalog: SkillCompatibilityCatalog | None = None
    compatibility: dict[str, Any] | None = None
    if "compatibility" in index:
        try:
            compatibility_catalog = SkillCompatibilityCatalog.from_value(
                index.get("compatibility")
            )
            compatibility = compatibility_catalog.projection
        except SkillCompatibilityError as exc:
            raise SkillPlanSnapshotError(
                f"blocked: invalid skill compatibility metadata: {exc}"
            ) from exc
    selection = index.get("selection")
    if not isinstance(selection, dict):
        selection = {}
    raw_skills = index.get("skills")
    if raw_skills is None:
        raw_skills = []
    if not isinstance(raw_skills, list):
        raise SkillPlanSnapshotError("blocked: installed skill index has invalid skills")
    indexed_skills = _index_skills_by_logical_name(raw_skills)
    if compatibility_catalog is not None:
        try:
            compatibility_catalog.validate_concrete_ids(indexed_skills)
        except SkillCompatibilityError as exc:
            raise SkillPlanSnapshotError(
                f"blocked: invalid skill compatibility metadata: {exc}"
            ) from exc

    skills: list[list[Any]] = []
    for raw_skill in raw_skills:
        if not isinstance(raw_skill, dict):
            raise SkillPlanSnapshotError("blocked: installed skill index has invalid skill entry")
        skill_name = raw_skill.get("name")
        skill_path = _installed_skill_path(index_root, raw_skill)
        relative_skill_path = skill_path.relative_to(
            Path(os.path.abspath(index_root))
        ).as_posix()
        live_hash = (
            hash_skill_tree(skill_path.parent, authority_root=index_root)
            if verify_trees
            else raw_skill.get("tree_hash")
        )
        if verify_trees and raw_skill.get("tree_hash") != live_hash:
            raise SkillPlanSnapshotError(
                f"blocked: installed skill snapshot changed: {skill_name}"
            )
        record: list[Any] = [
            skill_name,
            relative_skill_path,
            raw_skill.get("source"),
            raw_skill.get("source_host"),
            live_hash,
            sorted(_snapshot_strings(raw_skill.get("profiles"))),
        ]
        if any(
            key in raw_skill
            for key in ("activation", "taskTerms", "pathGlobs")
        ):
            record.append(
                {
                    "activation": raw_skill.get("activation"),
                    "workflowPhases": sorted(
                        _routing_snapshot_strings(raw_skill, "workflowPhases")
                    ),
                    "taskTerms": sorted(
                        _routing_snapshot_strings(raw_skill, "taskTerms")
                    ),
                    "pathGlobs": sorted(
                        _routing_snapshot_strings(raw_skill, "pathGlobs")
                    ),
                }
            )
        skills.append(record)
    skills.sort(key=lambda item: str(item[0]))

    raw_required_review = selection.get("required_review")
    if raw_required_review is None:
        raw_required_review = {}
    if not isinstance(raw_required_review, dict):
        raise SkillPlanSnapshotError(
            "blocked: installed skill index has invalid required_review"
        )
    required_review = {
        str(profile): sorted(_snapshot_strings(names))
        for profile, names in sorted(
            raw_required_review.items(), key=lambda item: str(item[0])
        )
    }
    provider_metadata = (
        _canonical_runtime_skill_provider_metadata(index)
        if "provider_registry" in index or "skill_providers" in index
        else None
    )
    normalized = {
        "profiles": sorted(_snapshot_strings(selection.get("profiles"))),
        "skill_profiles": sorted(_snapshot_strings(selection.get("skill_profiles"))),
        "explicit_skills": sorted(_snapshot_strings(selection.get("explicit_skills"))),
        **(
            {
                "external_exposure_skills": sorted(
                    indexed_external_exposure_skill_names(
                        selection,
                        legacy_fallback=False,
                    )
                )
            }
            if "external_exposure_skills" in selection
            else {}
        ),
        **(
            {"profile_selection": selection.get("profile_selection")}
            if "profile_selection" in selection
            else {}
        ),
        "required_review": required_review,
        "conditional_skills": selection.get("conditional_skills") or {},
        "profile_routing": selection.get("profile_routing") or {},
        **(
            {"compatibility": compatibility}
            if compatibility is not None
            else {}
        ),
        **(provider_metadata or {}),
        "skills": skills,
    }
    try:
        text = json.dumps(
            _javascript_property_order(normalized),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise SkillPlanSnapshotError(
            "blocked: installed skill index cannot be canonically serialized"
        ) from exc
    return text.encode("utf-8")


def _filesystem_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _assert_pinned_skill_directories(
    directories: tuple[tuple[Path, os.stat_result], ...],
) -> None:
    for path, expected in directories:
        try:
            current = path.lstat()
        except OSError as exc:
            raise SkillPlanSnapshotError(
                f"blocked: skill source directory changed while hashing: {path}"
            ) from exc
        if (
            not stat.S_ISDIR(current.st_mode)
            or _filesystem_identity(current) != _filesystem_identity(expected)
        ):
            raise SkillPlanSnapshotError(
                f"blocked: skill source directory changed while hashing: {path}"
            )


def _stable_skill_tree_file_bytes(
    path: Path,
    expected: os.stat_result,
    directories: tuple[tuple[Path, os.stat_result], ...],
) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    failure = f"blocked: skill source changed or is unreadable while hashing: {path}"
    _assert_pinned_skill_directories(directories)
    try:
        initial = path.lstat()
    except OSError as exc:
        raise SkillPlanSnapshotError(failure) from exc
    if (
        not stat.S_ISREG(initial.st_mode)
        or initial.st_nlink != 1
        or _filesystem_identity(initial) != _filesystem_identity(expected)
    ):
        raise SkillPlanSnapshotError(failure)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        _assert_pinned_skill_directories(directories)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or _filesystem_identity(before) != _filesystem_identity(expected)
        ):
            raise SkillPlanSnapshotError(failure)
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        current = path.lstat()
        _assert_pinned_skill_directories(directories)
        if (
            not stat.S_ISREG(after.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or after.st_nlink != 1
            or current.st_nlink != 1
            or _filesystem_identity(before) != _filesystem_identity(after)
            or _filesystem_identity(after) != _filesystem_identity(current)
        ):
            raise SkillPlanSnapshotError(failure)
        return b"".join(chunks)
    except SkillPlanSnapshotError:
        raise
    except OSError as exc:
        raise SkillPlanSnapshotError(failure) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _pin_skill_tree_authority(
    authority_root: Path,
    root: Path,
) -> tuple[tuple[Path, os.stat_result], ...]:
    lexical_authority = Path(os.path.abspath(authority_root))
    lexical_root = Path(os.path.abspath(root))
    try:
        relative = lexical_root.relative_to(lexical_authority)
    except ValueError as exc:
        raise SkillPlanSnapshotError(
            f"blocked: skill source escapes authority root: {root}"
        ) from exc
    paths: list[Path] = [lexical_authority]
    cursor = lexical_authority
    for part in relative.parts:
        cursor /= part
        paths.append(cursor)
    directories: list[tuple[Path, os.stat_result]] = []
    for path in paths:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise SkillPlanSnapshotError(
                f"blocked: installed skill snapshot is unreadable: {path}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise SkillPlanSnapshotError(
                f"blocked: skill source may not use symlink ancestors: {path}"
            )
        if not stat.S_ISDIR(metadata.st_mode):
            raise SkillPlanSnapshotError(
                f"blocked: skill source ancestor is not a directory: {path}"
            )
        directories.append((path, metadata))
    try:
        lexical_root.resolve(strict=True).relative_to(
            lexical_authority.resolve(strict=True)
        )
    except (OSError, ValueError) as exc:
        raise SkillPlanSnapshotError(
            f"blocked: skill source escapes authority root: {root}"
        ) from exc
    pinned = tuple(directories)
    _assert_pinned_skill_directories(pinned)
    return pinned


def hash_skill_tree(root: Path, authority_root: Path | None = None) -> str:
    """Node hashSkillTree와 동일하게 상대 경로와 binary 내용을 hash한다."""
    files: list[
        tuple[Path, os.stat_result, tuple[tuple[Path, os.stat_result], ...]]
    ] = []
    directories: list[tuple[Path, os.stat_result]] = []

    def visit(
        directory_path: Path,
        expected: os.stat_result | None,
        ancestors: tuple[tuple[Path, os.stat_result], ...],
    ) -> None:
        try:
            metadata = directory_path.lstat()
        except OSError as exc:
            raise SkillPlanSnapshotError(
                f"blocked: installed skill snapshot is unreadable: {directory_path}"
            ) from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or (
                expected is not None
                and _filesystem_identity(metadata) != _filesystem_identity(expected)
            )
        ):
            raise SkillPlanSnapshotError(
                f"blocked: skill source directory changed while hashing: {directory_path}"
            )
        directory = (directory_path, metadata)
        current_ancestors = (*ancestors, directory)
        directories.append(directory)
        _assert_pinned_skill_directories(current_ancestors)
        try:
            entries = sorted(
                directory_path.iterdir(),
                key=lambda candidate: candidate.name,
            )
        except OSError as exc:
            raise SkillPlanSnapshotError(
                f"blocked: installed skill snapshot is unreadable: {directory_path}"
            ) from exc
        _assert_pinned_skill_directories(current_ancestors)
        for entry in entries:
            try:
                entry_metadata = entry.lstat()
            except OSError as exc:
                raise SkillPlanSnapshotError(
                    f"blocked: installed skill snapshot is unreadable: {entry}"
                ) from exc
            if stat.S_ISLNK(entry_metadata.st_mode):
                raise SkillPlanSnapshotError(
                    f"blocked: skill source may not contain symlinks: {entry}"
                )
            if stat.S_ISDIR(entry_metadata.st_mode):
                visit(entry, entry_metadata, current_ancestors)
            elif stat.S_ISREG(entry_metadata.st_mode):
                files.append((entry, entry_metadata, current_ancestors))
            else:
                raise SkillPlanSnapshotError(
                    "blocked: skill source must contain only regular files and "
                    f"directories: {entry}"
                )
            _assert_pinned_skill_directories(current_ancestors)
        _assert_pinned_skill_directories(current_ancestors)

    if authority_root is None:
        visit(root, None, ())
    else:
        pinned_authority = _pin_skill_tree_authority(authority_root, root)
        directories.extend(pinned_authority[:-1])
        visit(root, pinned_authority[-1][1], pinned_authority[:-1])
    digest = hashlib.sha256()
    for file, metadata, ancestors in sorted(
        files,
        key=lambda candidate: candidate[0].as_posix(),
    ):
        relative = file.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_stable_skill_tree_file_bytes(file, metadata, ancestors))
        digest.update(b"\0")
    _assert_pinned_skill_directories(tuple(directories))
    return digest.hexdigest()


def skill_links_commitment(skill_plan_hash: object, links: object) -> str:
    """설치된 host skill link의 Node 호환 commitment를 반환한다."""
    if (
        not isinstance(skill_plan_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", skill_plan_hash) is None
    ):
        raise SkillPlanSnapshotError(
            "blocked: skill link commitment has an invalid skill plan hash"
        )
    payload = {
        "version": SKILL_LINKS_COMMITMENT_VERSION,
        "skill_plan_hash": skill_plan_hash,
        "links": _normalized_skill_links(_owned_skill_links(links)),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def validate_skill_host_links(
    index_root: Path,
    kit: dict[str, Any],
    index: dict[str, Any],
) -> None:
    """인증된 host link와 현재 destination을 대조 검증한다."""
    has_version = "skill_links_commitment_version" in kit
    has_commitment = "skill_links_commitment" in kit
    if not has_version and not has_commitment:
        return
    commitment = kit.get("skill_links_commitment")
    if (
        not has_version
        or not has_commitment
        or kit.get("skill_links_commitment_version")
        != SKILL_LINKS_COMMITMENT_VERSION
        or not isinstance(commitment, str)
        or re.fullmatch(r"[0-9a-f]{64}", commitment) is None
    ):
        raise SkillPlanSnapshotError(
            "blocked: previous skill link commitment is invalid"
        )
    links = index.get("links")
    if skill_links_commitment(kit.get("skill_plan_hash"), links) != commitment:
        raise SkillPlanSnapshotError(
            "blocked: previous skill links do not match kit commitment"
        )
    assert_committed_skill_host_links_applied(index_root, index, links)


def assert_committed_skill_host_links_applied(
    index_root: Path,
    index: dict[str, Any],
    links: object,
) -> None:
    """기록된 host link, copy, stale 삭제가 반영되지 않았으면 실패한다."""
    normalized_links = _normalized_skill_link_objects(_owned_skill_links(links))
    skills = index.get("skills")
    if not isinstance(skills, list):
        raise SkillPlanSnapshotError("blocked: installed skill index has invalid skills")
    root = Path(os.path.abspath(index_root))
    for link in normalized_links:
        destination = Path(os.path.abspath(root / link["path"]))
        _require_link_parent_directories(root, destination.parent, link["path"])
        snapshot = _skill_host_destination_snapshot(destination)
        if link["status"] in {"removed-stale-linked", "removed-stale-copied"}:
            if snapshot["kind"] != "absent":
                raise SkillPlanSnapshotError(
                    "blocked: committed stale skill link still exists: "
                    f"{link['path']}"
                )
            continue

        skill = next(
            (
                candidate
                for candidate in skills
                if isinstance(candidate, dict) and candidate.get("name") == link["name"]
            ),
            None,
        )
        if not isinstance(skill, dict) or not isinstance(skill.get("path"), str):
            raise SkillPlanSnapshotError(
                "blocked: committed skill link has no indexed source: "
                f"{link['name']}"
            )
        source = _installed_skill_path(root, skill).parent
        source_tree_hash = _require_tree_hash(skill.get("tree_hash"), link["name"])
        source_tree_integrity = _require_tree_hash(
            link["tree_integrity"], link["name"]
        )
        if (
            hash_skill_tree(source, authority_root=root) != source_tree_hash
            or _tree_integrity(source) != source_tree_integrity
        ):
            raise SkillPlanSnapshotError(
                f"blocked: committed skill link source changed: {link['name']}"
            )

        if link["status"] == "linked":
            expected_target = os.path.relpath(source, start=destination.parent)
            if (
                snapshot["kind"] != "symlink"
                or snapshot.get("target") != expected_target
                or snapshot.get("filesystem_identity")
                != link["filesystem_identity"]
            ):
                raise SkillPlanSnapshotError(
                    "blocked: committed skill symlink is not applied: "
                    f"{link['path']}"
                )
            continue
        if (
            link["status"] != "copied"
            or snapshot["kind"] != "directory"
            or snapshot.get("tree_hash") != source_tree_hash
            or snapshot.get("tree_integrity") != source_tree_integrity
            or snapshot.get("filesystem_identity")
            != link["filesystem_identity"]
        ):
            raise SkillPlanSnapshotError(
                "blocked: committed skill copy is not applied: "
                f"{link['path']}"
            )


def _normalized_skill_links(links: object) -> list[list[Any]]:
    rows = [
        [
            link["name"],
            link["host"],
            link["path"],
            link["status"],
            link["tree_integrity"],
            link["filesystem_identity"],
        ]
        for link in _normalized_skill_link_objects(links)
    ]
    return sorted(
        rows,
        key=lambda row: json.dumps(
            row,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ),
    )


def _owned_skill_links(links: object) -> list[dict[str, Any]]:
    if not isinstance(links, list):
        raise SkillPlanSnapshotError("blocked: installed skill links are invalid")
    owned_statuses = {
        "linked",
        "copied",
        "removed-stale-linked",
        "removed-stale-copied",
    }
    return [
        link
        for link in links
        if isinstance(link, dict) and link.get("status") in owned_statuses
    ]


def _normalized_skill_link_objects(links: object) -> list[dict[str, Any]]:
    if not isinstance(links, list):
        raise SkillPlanSnapshotError("blocked: installed skill links are invalid")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw_link in links:
        if not isinstance(raw_link, dict):
            raise SkillPlanSnapshotError("blocked: installed skill link is invalid")
        name = raw_link.get("name") if isinstance(raw_link.get("name"), str) else ""
        host = raw_link.get("host") if isinstance(raw_link.get("host"), str) else ""
        relative = raw_link.get("path") if isinstance(raw_link.get("path"), str) else ""
        status_value = (
            raw_link.get("status")
            if isinstance(raw_link.get("status"), str)
            else ""
        )
        integrity = raw_link.get("tree_integrity")
        filesystem_identity = raw_link.get("filesystem_identity")
        parts = relative.split("/")
        if (
            not name
            or not is_portable_skill_name(name)
            or len(name) > 128
            or host not in _SKILL_LINK_HOSTS
            or status_value not in _SKILL_LINK_STATUSES
            or "\\" in relative
            or relative.startswith("/")
            or any(not part or part in {".", ".."} for part in parts)
            or (
                integrity is not None
                and (
                    not isinstance(integrity, str)
                    or re.fullmatch(r"[0-9a-f]{64}", integrity) is None
                )
            )
            or (
                filesystem_identity is not None
                and (
                    not isinstance(filesystem_identity, dict)
                    or set(filesystem_identity) != {"device", "inode", "links", "mode"}
                    or any(
                        not isinstance(filesystem_identity.get(key), str)
                        or re.fullmatch(r"[0-9]+", filesystem_identity[key]) is None
                        for key in ("device", "inode", "links")
                    )
                    or not isinstance(filesystem_identity.get("mode"), int)
                )
            )
            or (
                status_value in {"linked", "copied"}
                and filesystem_identity is None
            )
        ):
            raise SkillPlanSnapshotError(
                f"blocked: installed skill link is invalid: {name or 'unknown'}"
            )
        expected_paths = _canonical_skill_link_paths(host, name)
        if relative not in expected_paths:
            raise SkillPlanSnapshotError(
                f"blocked: installed skill link path is noncanonical: {relative}"
            )
        identity = (host, name, relative)
        if identity in seen:
            raise SkillPlanSnapshotError(
                f"blocked: duplicate installed skill link: {host}:{name}"
            )
        seen.add(identity)
        normalized.append(
            {
                "name": name,
                "host": host,
                "path": relative,
                "status": status_value,
                "tree_integrity": integrity,
                "filesystem_identity": filesystem_identity,
            }
        )
    return normalized


def _canonical_skill_link_paths(host: str, name: str) -> set[str]:
    if host == "codex":
        return {
            f".agents/skills/{name}",
            f".Codex/skills/{name}",
            f".codex/skills/{name}",
        }
    if host == "antigravity":
        return {f".gemini/antigravity/skills/{name}"}
    return {f".{host}/skills/{name}"}


def _skill_host_destination_snapshot(destination: Path) -> dict[str, Any]:
    try:
        metadata = destination.lstat()
        mode = metadata.st_mode
    except FileNotFoundError:
        return {"kind": "absent"}
    except OSError as exc:
        raise SkillPlanSnapshotError(
            f"blocked: project skill host destination is unreadable: {destination}"
        ) from exc
    filesystem_identity = {
        "device": str(metadata.st_dev),
        "inode": str(metadata.st_ino),
        "links": str(metadata.st_nlink),
        "mode": stat.S_IMODE(metadata.st_mode),
    }
    if stat.S_ISLNK(mode):
        try:
            return {
                "kind": "symlink",
                "target": os.readlink(destination),
                "filesystem_identity": filesystem_identity,
            }
        except OSError as exc:
            raise SkillPlanSnapshotError(
                f"blocked: project skill host symlink is unreadable: {destination}"
            ) from exc
    if not stat.S_ISDIR(mode):
        raise SkillPlanSnapshotError(
            "blocked: project skill host destination is not a directory: "
            f"{destination}"
        )
    skill_file = destination / "SKILL.md"
    try:
        skill_mode = skill_file.lstat().st_mode
    except OSError as exc:
        raise SkillPlanSnapshotError(
            "blocked: project skill host directory has no regular SKILL.md: "
            f"{destination}"
        ) from exc
    if not stat.S_ISREG(skill_mode) or stat.S_ISLNK(skill_mode):
        raise SkillPlanSnapshotError(
            "blocked: project skill host directory has no regular SKILL.md: "
            f"{destination}"
        )
    return {
        "kind": "directory",
        "tree_hash": hash_skill_tree(destination),
        "tree_integrity": _tree_integrity(destination),
        "filesystem_identity": filesystem_identity,
    }


def _tree_integrity(root: Path) -> str:
    entries: list[dict[str, Any]] = []

    def walk(current: Path, relative: str) -> None:
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise SkillPlanSnapshotError(
                f"blocked: install transaction tree is unreadable: {current}"
            ) from exc
        if stat.S_ISLNK(mode):
            raise SkillPlanSnapshotError(
                f"blocked: install transaction tree contains a symlink: {current}"
            )
        if not stat.S_ISDIR(mode):
            raise SkillPlanSnapshotError(
                f"blocked: install transaction tree root is not a directory: {current}"
            )
        entries.append(
            {"path": relative, "type": "directory", "mode": mode & 0o777}
        )
        try:
            children = sorted(
                current.iterdir(),
                key=lambda child: _javascript_utf16_sort_key(child.name),
            )
        except OSError as exc:
            raise SkillPlanSnapshotError(
                f"blocked: install transaction tree is unreadable: {current}"
            ) from exc
        for child in children:
            child_relative = f"{relative}/{child.name}" if relative else child.name
            try:
                child_mode = child.lstat().st_mode
            except OSError as exc:
                raise SkillPlanSnapshotError(
                    f"blocked: install transaction tree is unreadable: {child}"
                ) from exc
            if stat.S_ISLNK(child_mode):
                raise SkillPlanSnapshotError(
                    f"blocked: install transaction tree contains a symlink: {child}"
                )
            if stat.S_ISDIR(child_mode):
                walk(child, child_relative)
            elif stat.S_ISREG(child_mode):
                try:
                    content = child.read_bytes()
                except OSError as exc:
                    raise SkillPlanSnapshotError(
                        f"blocked: install transaction tree is unreadable: {child}"
                    ) from exc
                entries.append(
                    {
                        "path": child_relative,
                        "type": "file",
                        "mode": child_mode & 0o777,
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                )
            else:
                raise SkillPlanSnapshotError(
                    f"blocked: install transaction tree contains a special file: {child}"
                )

    walk(root, "")
    entries.sort(key=lambda entry: _javascript_utf16_sort_key(entry["path"]))
    return hashlib.sha256(
        json.dumps(
            {"version": 1, "entries": entries},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _javascript_utf16_sort_key(value: str) -> bytes:
    return value.encode("utf-16-be", errors="surrogatepass")


def _require_tree_hash(value: object, name: str) -> str:
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    raise SkillPlanSnapshotError(
        f"blocked: project skill has no whole-tree hash: {name}"
    )


def _require_link_parent_directories(root: Path, parent: Path, relative: str) -> None:
    try:
        parts = parent.relative_to(root).parts
    except ValueError as exc:
        raise SkillPlanSnapshotError(
            f"blocked: committed skill link escapes the project: {relative}"
        ) from exc
    cursor = root
    for part in parts:
        cursor /= part
        try:
            mode = cursor.lstat().st_mode
        except FileNotFoundError:
            break
        except OSError as exc:
            raise SkillPlanSnapshotError(
                f"blocked: committed skill link parent is unreadable: {relative}"
            ) from exc
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise SkillPlanSnapshotError(
                f"blocked: committed skill link parent is unsafe: {relative}"
            )


def authenticated_installed_skill_index(index_root: Path) -> dict[str, Any] | None:
    """인증된 설치 index projection을 반환한다."""
    snapshot = _authenticated_installed_skill_snapshot(index_root)
    return snapshot[0] if snapshot is not None else None


def _authenticated_installed_skill_snapshot(
    index_root: Path,
) -> tuple[dict[str, Any], str] | None:
    index_path = index_root / ".agent-flow" / "skills" / "index.json"
    kit_path = index_root / ".agent-flow" / "kit.json"
    index_present = index_path.exists() or index_path.is_symlink()
    kit_present = kit_path.exists() or kit_path.is_symlink()
    if not index_present:
        if kit_present:
            raise SkillPlanSnapshotError(
                "blocked: installed skill index is missing while kit metadata exists"
            )
        return None
    if not kit_present:
        raise SkillPlanSnapshotError(
            "blocked: installed kit metadata is missing while skill index exists"
        )
    index = _read_snapshot_json(index_root, index_path, "installed skill index")
    current_hash = compute_skill_plan_hash(index, index_root, verify_trees=True)
    kit = _read_snapshot_json(index_root, kit_path, "installed kit metadata")
    if kit.get("skill_plan_hash_version") == SKILL_PLAN_HASH_VERSION:
        validate_managed_host_files(index_root, kit)
        _validate_installed_profile_selection(index_root, kit, index)
        _validate_installed_android_official_provenance(index_root, index)
        if kit.get("skill_plan_hash") != current_hash:
            raise SkillPlanSnapshotError(
                "blocked: installed skill index or snapshot no longer matches kit.json"
            )
    validate_skill_host_links(index_root, kit, index)
    return index, current_hash


def build_resolved_skill_lock(
    index: dict[str, Any], skill_plan_hash: str
) -> tuple[dict[str, Any], str]:
    """설치 index를 run 시점에 고정하는 결정적 resolved skill lock을 만든다."""
    selection = index.get("selection") or {}
    profiles = sorted(
        str(name) for name in (selection.get("profiles") or []) if isinstance(name, str)
    )
    providers_by_name: dict[str, dict[str, Any]] = {}
    for claim in index.get("skill_providers") or []:
        if isinstance(claim, dict) and isinstance(claim.get("concrete_id"), str):
            providers_by_name[claim["concrete_id"]] = claim
    skills: list[dict[str, Any]] = []
    for skill in index.get("skills") or []:
        if not isinstance(skill, dict):
            continue
        name = skill.get("name")
        if not isinstance(name, str):
            continue
        claim = providers_by_name.get(_portable_casefold(name)) or {}
        capabilities = skill.get("capabilities")
        skills.append(
            {
                "name": name,
                "path": skill.get("path"),
                "tree_hash": skill.get("tree_hash"),
                "source": skill.get("source"),
                "provider_id": claim.get("provider_id"),
                "provider_version": claim.get("provider_version"),
                "source_hash": claim.get("source_hash"),
                "trust_tier": claim.get("trust_tier"),
                "ownership": claim.get("ownership"),
                "capabilities": sorted(str(c) for c in capabilities)
                if isinstance(capabilities, list)
                else None,
            }
        )
    skills.sort(key=lambda entry: entry["name"])
    provider_registry = index.get("provider_registry")
    lock = {
        "version": RESOLVED_SKILL_LOCK_VERSION,
        "skill_plan_hash": skill_plan_hash,
        "catalog_fingerprint": index.get("catalog_fingerprint"),
        "provider_registry_fingerprint": provider_registry.get("fingerprint")
        if isinstance(provider_registry, dict)
        else None,
        "active_profiles": profiles,
        "skills": skills,
    }
    lock_hash = hashlib.sha256(
        json.dumps(
            lock, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()
    return lock, lock_hash


def installed_skill_plan_pin(index_root: Path) -> dict[str, Any]:
    """설치된 index와 tree를 검증하고 새 run용 metadata를 반환한다."""
    snapshot = _authenticated_installed_skill_snapshot(index_root)
    if snapshot is None:
        return {}
    index, current_hash = snapshot
    from agent_flow.core.local_skills import (
        LOCAL_SKILL_PLAN_HASH_VERSION,
        project_local_skill_plan_hash,
    )

    lock, lock_hash = build_resolved_skill_lock(index, current_hash)
    return {
        "skill_plan_hash": current_hash,
        "skill_plan_hash_version": SKILL_PLAN_HASH_VERSION,
        "local_skill_plan_hash": project_local_skill_plan_hash(index_root),
        "local_skill_plan_hash_version": LOCAL_SKILL_PLAN_HASH_VERSION,
        "resolved_skill_lock": lock,
        "resolved_skill_lock_hash": lock_hash,
        "resolved_skill_lock_version": RESOLVED_SKILL_LOCK_VERSION,
    }


def managed_host_files_commitment(kit: dict[str, Any]) -> str:
    """관리 reviewer와 hook provenance의 Node 호환 commitment를 반환한다."""
    skill_plan_hash = kit.get("skill_plan_hash")
    if not isinstance(skill_plan_hash, str) or re.fullmatch(r"[0-9a-f]{64}", skill_plan_hash) is None:
        raise SkillPlanSnapshotError(
            "blocked: managed host file commitment has an invalid skill plan hash"
        )
    files = _managed_host_file_entries(kit)
    payload = {
        "version": MANAGED_HOST_FILES_COMMITMENT_VERSION,
        "skill_plan_hash": skill_plan_hash,
        "files": [
            [relative, entry["source"], entry["sha256"]]
            for relative, entry in sorted(files.items())
        ],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def validate_managed_host_files(index_root: Path, kit: dict[str, Any]) -> None:
    """commitment, 필수 경로, 현재 관리 host bytes를 검증한다."""
    if (
        kit.get("managed_host_files_commitment_version")
        != MANAGED_HOST_FILES_COMMITMENT_VERSION
    ):
        raise SkillPlanSnapshotError(
            "blocked: installed managed host file commitment is invalid"
        )
    commitment = kit.get("managed_host_files_commitment")
    if not isinstance(commitment, str) or re.fullmatch(r"[0-9a-f]{64}", commitment) is None:
        raise SkillPlanSnapshotError(
            "blocked: installed managed host file commitment is invalid"
        )
    if managed_host_files_commitment(kit) != commitment:
        raise SkillPlanSnapshotError(
            "blocked: installed managed host file commitment does not match provenance"
        )
    files = _managed_host_file_entries(kit)
    missing = [relative for relative in REQUIRED_MANAGED_HOST_FILES if relative not in files]
    if missing:
        raise SkillPlanSnapshotError(
            f"blocked: installed managed host file provenance is missing: {missing[0]}"
        )
    root = Path(os.path.abspath(index_root))
    for relative, entry in files.items():
        destination = root.joinpath(*relative.split("/"))
        _require_installed_regular_file(
            root,
            destination,
            f"managed host file {relative}",
        )
        try:
            live_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
        except OSError as exc:
            raise SkillPlanSnapshotError(
                f"blocked: managed host file {relative} is unreadable: {destination}"
            ) from exc
        if live_hash != entry["sha256"]:
            raise SkillPlanSnapshotError(
                f"blocked: installed managed host file changed: {relative}"
            )
    validate_managed_hook_contract(index_root, kit)


def managed_hook_contract_commitment(kit: dict[str, Any]) -> str:
    """정규화된 hook과 script의 Node 호환 commitment를 반환한다."""
    skill_plan_hash = kit.get("skill_plan_hash")
    if not isinstance(skill_plan_hash, str) or re.fullmatch(r"[0-9a-f]{64}", skill_plan_hash) is None:
        raise SkillPlanSnapshotError(
            "blocked: managed hook commitment has an invalid skill plan hash"
        )
    configs, scripts = _normalized_managed_hook_contract(kit.get("managed_hook_contract"))
    payload = {
        "version": MANAGED_HOOK_CONTRACT_COMMITMENT_VERSION,
        "skill_plan_hash": skill_plan_hash,
        "configs": configs,
        "scripts": scripts,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def validate_managed_hook_contract(index_root: Path, kit: dict[str, Any]) -> None:
    """관리 hook 항목을 관련 없는 host 설정과 분리해 검증한다."""
    if (
        kit.get("managed_hook_contract_commitment_version")
        != MANAGED_HOOK_CONTRACT_COMMITMENT_VERSION
    ):
        raise SkillPlanSnapshotError(
            "blocked: installed managed hook commitment is invalid"
        )
    commitment = kit.get("managed_hook_contract_commitment")
    if not isinstance(commitment, str) or re.fullmatch(r"[0-9a-f]{64}", commitment) is None:
        raise SkillPlanSnapshotError(
            "blocked: installed managed hook commitment is invalid"
        )
    if managed_hook_contract_commitment(kit) != commitment:
        raise SkillPlanSnapshotError(
            "blocked: installed managed hook commitment does not match provenance"
        )
    configs, scripts = _normalized_managed_hook_contract(kit.get("managed_hook_contract"))
    root = Path(os.path.realpath(index_root))
    expected = _expected_managed_hook_projection()
    expected_script_hashes = {
        relative: expected_hash for relative, expected_hash, _required_mode in scripts
    }
    for relative, expected_hash in configs:
        destination = root.joinpath(*relative.split("/"))
        _require_installed_regular_file(root, destination, f"managed hook settings {relative}")
        try:
            settings = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SkillPlanSnapshotError(
                f"blocked: managed hook settings are unreadable: {relative}"
            ) from exc
        projection = _managed_hook_projection(
            root,
            settings,
            relative,
            expected_script_hashes,
        )
        projection_bytes = json.dumps(
            projection,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if projection != expected or hashlib.sha256(projection_bytes).hexdigest() != expected_hash:
            raise SkillPlanSnapshotError(
                f"blocked: installed managed hook settings changed: {relative}"
            )
    for relative, expected_hash, _required_mode in scripts:
        destination = root.joinpath(*relative.split("/"))
        _require_installed_regular_file(root, destination, f"managed hook script {relative}")
        try:
            executable = bool(destination.stat().st_mode & 0o111)
        except OSError as exc:
            raise SkillPlanSnapshotError(
                f"blocked: managed hook script is unreadable: {relative}"
            ) from exc
        if os.name != "nt" and not executable:
            raise SkillPlanSnapshotError(
                f"blocked: managed hook script is not executable: {relative}"
            )
        try:
            live_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
        except OSError as exc:
            raise SkillPlanSnapshotError(
                f"blocked: managed hook script is unreadable: {relative}"
            ) from exc
        if live_hash != expected_hash:
            raise SkillPlanSnapshotError(
                f"blocked: installed managed hook script changed: {relative}"
            )


def _normalized_managed_hook_contract(
    contract: object,
) -> tuple[list[list[str]], list[list[str]]]:
    if (
        not isinstance(contract, dict)
        or contract.get("version") != MANAGED_HOOK_CONTRACT_VERSION
    ):
        raise SkillPlanSnapshotError(
            "blocked: installed managed hook contract is invalid"
        )

    def normalize(
        raw: object,
        expected_paths: tuple[str, ...],
        label: str,
        required_mode: str | None = None,
    ) -> list[list[str]]:
        if not isinstance(raw, dict) or sorted(raw) != sorted(expected_paths):
            raise SkillPlanSnapshotError(
                f"blocked: installed managed hook {label} provenance is incomplete"
            )
        rows: list[list[str]] = []
        for relative in sorted(raw):
            entry = raw[relative]
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None
                or (required_mode is not None and entry.get("mode") != required_mode)
            ):
                raise SkillPlanSnapshotError(
                    f"blocked: installed managed hook {label} provenance is invalid: {relative}"
                )
            row = [relative, entry["sha256"]]
            if required_mode is not None:
                row.append(required_mode)
            rows.append(row)
        return rows

    script_paths = tuple(
        f".agent-flow/scripts/hooks/{name}" for name in MANAGED_HOOK_SCRIPT_NAMES
    )
    return (
        normalize(contract.get("configs"), MANAGED_HOOK_CONFIG_PATHS, "config"),
        normalize(
            contract.get("scripts"),
            script_paths,
            "script",
            required_mode="executable",
        ),
    )


def _expected_managed_hook_projection() -> list[list[str]]:
    return sorted(
        [
            ["PostToolUse", WRITE_TOOL_MATCHER, "command", "comment-checker.py"],
            ["PreToolUse", "Bash", "command", "guard-protected-branch.sh"],
            ["PreToolUse", "Bash", "command", "guard-worktree-write.py"],
            ["PreToolUse", "Bash", "command", "guard-worktree.sh"],
            ["PreToolUse", WRITE_TOOL_MATCHER, "command", "guard-worktree-write.py"],
            ["Stop", "", "command", "show-phase-status.sh"],
        ]
    )


def _managed_hook_projection(
    root: Path,
    settings: object,
    label: str,
    expected_script_hashes: dict[str, str] | None = None,
) -> list[list[str]]:
    if not isinstance(settings, dict) or not isinstance(settings.get("hooks"), dict):
        raise SkillPlanSnapshotError(
            f"blocked: managed hook settings are missing: {label}"
        )
    rows: list[list[str]] = []
    for event, entries in settings["hooks"].items():
        if not isinstance(event, str) or not isinstance(entries, list):
            raise SkillPlanSnapshotError(
                f"blocked: invalid managed hook settings: {label}"
            )
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                raise SkillPlanSnapshotError(
                    f"blocked: invalid managed hook settings: {label}"
                )
            matcher = entry.get("matcher", "")
            if not isinstance(matcher, str):
                raise SkillPlanSnapshotError(
                    f"blocked: invalid managed hook settings: {label}"
                )
            for hook in entry["hooks"]:
                if not isinstance(hook, dict):
                    raise SkillPlanSnapshotError(
                        f"blocked: invalid managed hook settings: {label}"
                    )
                script_name = _trusted_managed_hook_script_name(
                    root,
                    hook.get("command"),
                    expected_script_hashes,
                )
                if script_name:
                    rows.append([event, matcher, hook.get("type", ""), script_name])
                elif _managed_hook_candidate_script_name(hook.get("command")):
                    raise SkillPlanSnapshotError(
                        f"blocked: managed hook command is not immutable: {label}"
                    )
    try:
        return sorted(rows)
    except TypeError as exc:
        raise SkillPlanSnapshotError(
            f"blocked: invalid managed hook settings: {label}"
        ) from exc


def _trusted_managed_hook_script_name(
    root: Path,
    command: object,
    expected_script_hashes: dict[str, str] | None = None,
) -> str:
    if not isinstance(command, str):
        return ""

    normalized_root = Path(os.path.abspath(root))
    for script_name in MANAGED_HOOK_SCRIPT_NAMES:
        relative = f".agent-flow/scripts/hooks/{script_name}"
        script_path = normalized_root.joinpath(*relative.split("/"))
        if command not in {
            _managed_hook_command(normalized_root, script_name, "codex"),
            _managed_hook_command(normalized_root, script_name, "claude"),
        }:
            continue
        try:
            metadata = script_path.lstat()
            if not (
                stat.S_ISREG(metadata.st_mode)
                and not stat.S_ISLNK(metadata.st_mode)
            ):
                continue
            if expected_script_hashes is None:
                digest = hashlib.sha256(script_path.read_bytes()).hexdigest()
            else:
                digest = expected_script_hashes.get(relative, "")
        except OSError:
            continue
        if hashlib.sha256(script_path.read_bytes()).hexdigest() == digest:
            return script_name
    return ""


def _shell_quote(value: object) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"


def _managed_hook_command(root: Path, script_name: str, host: str) -> str:
    script_path = root / ".agent-flow" / "scripts" / "hooks" / script_name
    return f"{_shell_quote(script_path.as_posix())} --host {_shell_quote(host)}"


def _managed_hook_candidate_script_name(command: object) -> str:
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
                if script_name in MANAGED_HOOK_SCRIPT_NAMES:
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
    normalized = command.strip()
    if normalized.startswith("'") and normalized.endswith("'"):
        normalized = normalized[1:-1].replace("'\\''", "'")
    elif normalized.startswith('"') and normalized.endswith('"'):
        normalized = normalized[1:-1]
    normalized = normalized.replace("\\", "/").replace("'", "").replace('"', "")
    return next(
        (
            script_name
            for script_name in MANAGED_HOOK_SCRIPT_NAMES
            if f"/.agent-flow/scripts/hooks/{script_name}" in normalized
            or f"/scripts/hooks/{script_name}" in normalized
        ),
        "",
    )


def _managed_host_file_entries(kit: dict[str, Any]) -> dict[str, dict[str, str]]:
    manifest = kit.get("managed_host_files")
    if (
        not isinstance(manifest, dict)
        or manifest.get("version") != MANAGED_HOST_FILES_VERSION
        or not isinstance(manifest.get("files"), dict)
    ):
        raise SkillPlanSnapshotError(
            "blocked: installed managed host file provenance is invalid"
        )
    result: dict[str, dict[str, str]] = {}
    for raw_relative, raw_entry in manifest["files"].items():
        relative = _managed_host_relative_path(raw_relative)
        if (
            not isinstance(raw_entry, dict)
            or not isinstance(raw_entry.get("source"), str)
            or not raw_entry["source"].strip()
            or not isinstance(raw_entry.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", raw_entry["sha256"]) is None
        ):
            raise SkillPlanSnapshotError(
                f"blocked: installed managed host file provenance is invalid: {relative}"
            )
        result[relative] = {
            "source": raw_entry["source"],
            "sha256": raw_entry["sha256"],
        }
    return result


def _managed_host_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
        raise SkillPlanSnapshotError(
            f"blocked: invalid managed host file provenance path: {value!r}"
        )
    parts = value.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise SkillPlanSnapshotError(
            f"blocked: invalid managed host file provenance path: {value!r}"
        )
    valid = value == ".omp/extensions/agent-flow-hooks.ts" or any(
        value.startswith(prefix) and len(value) > len(prefix)
        for prefix in (".Codex/agents/", ".claude/agents/", ".omp/agents/")
    )
    if not valid:
        raise SkillPlanSnapshotError(
            f"blocked: invalid managed host file provenance path: {value!r}"
        )
    return value


def reconcile_skill_plan_pin(
    meta: dict[str, Any],
    index_root: Path,
) -> tuple[dict[str, Any], bool]:
    """v2 run pin을 검증하거나 추가 입력 없는 legacy migration을 준비한다."""
    current_pin = installed_skill_plan_pin(index_root)
    pinned_hash = meta.get("skill_plan_hash")
    pinned_version = meta.get("skill_plan_hash_version")
    if not current_pin:
        if pinned_hash:
            raise SkillPlanSnapshotError(
                "blocked: installed skill plan is missing during the active run"
            )
        return meta, False

    current_hash = current_pin["skill_plan_hash"]
    current_local_hash = current_pin["local_skill_plan_hash"]
    locked = isinstance(meta.get("resolved_skill_lock"), dict)
    if pinned_hash:
        if pinned_version == SKILL_PLAN_HASH_VERSION:
            pinned_local_hash = meta.get("local_skill_plan_hash")
            pinned_local_version = meta.get("local_skill_plan_hash_version")
            hash_drift = pinned_hash != current_hash
            local_drift = bool(pinned_local_hash) and (
                pinned_local_version != current_pin["local_skill_plan_hash_version"]
                or pinned_local_hash != current_local_hash
            )
            if locked:
                # run-scoped resolved skill lock은 run 수명 동안 동결된다.
                # catalog/project-local drift는 다음 run만 무효화한다.
                if hash_drift or local_drift:
                    observed = {
                        "skill_plan_hash": current_hash,
                        "local_skill_plan_hash": current_local_hash,
                    }
                    if meta.get("skill_plan_drift_observed") == observed:
                        return meta, False
                    deferred = dict(meta)
                    deferred["skill_plan_drift_observed"] = observed
                    return deferred, True
                if meta.get("skill_plan_drift_observed") is not None:
                    cleared = dict(meta)
                    cleared.pop("skill_plan_drift_observed", None)
                    return cleared, True
                return meta, False
            if hash_drift:
                repinned = dict(meta)
                repinned.update(current_pin)
                repinned["skill_plan_repin_from"] = pinned_hash
                return repinned, True
            if pinned_local_hash:
                if local_drift:
                    repinned = dict(meta)
                    repinned.update(current_pin)
                    repinned["local_skill_plan_repin_from"] = pinned_local_hash
                    return repinned, True
                return meta, False
            if "local_skill_plan_hash" in meta or "local_skill_plan_hash_version" in meta:
                raise SkillPlanSnapshotError(
                    "blocked: active run has an invalid project-local skill plan pin"
                )
            migrated = dict(meta)
            migrated.update(
                {
                    "local_skill_plan_hash": current_local_hash,
                    "local_skill_plan_hash_version": current_pin[
                        "local_skill_plan_hash_version"
                    ],
                    "local_skill_plan_pin_migrated": True,
                }
            )
            return migrated, True
    elif "skill_plan_hash" in meta or "skill_plan_hash_version" in meta:
        raise SkillPlanSnapshotError("blocked: active run has an invalid skill plan pin")

    migrated = dict(meta)
    migrated.update(current_pin)
    migrated["skill_plan_pin_migrated"] = True
    if pinned_hash:
        migrated["skill_plan_hash_migrated_from_version"] = pinned_version
    return migrated, True


def _installed_skill_path(index_root: Path, skill: dict[str, Any]) -> Path:
    raw_path = str(skill.get("path") or "")
    skill_name = str(skill.get("name") or "")
    root = Path(os.path.abspath(index_root))
    skill_path = Path(os.path.abspath(root / raw_path))
    try:
        skill_path.relative_to(root)
    except ValueError:
        raise SkillDocumentResolutionError(
            skill_name,
            skill_path,
            "invalid_path",
        ) from None
    if skill_path.name != "SKILL.md":
        raise SkillDocumentResolutionError(skill_name, skill_path, "invalid_path")
    try:
        _require_installed_regular_file(
            root,
            skill_path,
            f"installed skill snapshot {skill_name}",
        )
    except SkillPlanSnapshotError:
        raise SkillDocumentResolutionError(
            skill_name,
            skill_path,
            _installed_skill_document_state(skill_path),
        ) from None
    return skill_path


def _installed_skill_document_state(skill_path: Path) -> str:
    try:
        metadata = skill_path.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    if stat.S_ISLNK(metadata.st_mode):
        try:
            skill_path.stat()
        except FileNotFoundError:
            return "dangling_symlink"
        except OSError:
            pass
        return "symlink"
    if not stat.S_ISREG(metadata.st_mode):
        return "non_regular"
    return "unsafe_path"


def _read_snapshot_json(root: Path, path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(
            _read_installed_regular_bytes(root, path, label).decode("utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SkillPlanSnapshotError(f"blocked: {label} is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise SkillPlanSnapshotError(f"blocked: {label} must be a JSON object: {path}")
    return payload


def _validate_installed_android_official_provenance(
    root: Path,
    index: dict[str, Any],
) -> None:
    selection = index.get("selection")
    if not isinstance(selection, dict):
        return
    active_profiles = set(_logical_strings(selection.get("skill_profiles")))
    if not active_profiles:
        active_profiles = set(_logical_strings(selection.get("profiles")))
    if "android" not in active_profiles:
        return

    skills_root = root / ".agent-flow" / "skills"
    lock = _read_snapshot_json(
        root,
        skills_root / "upstream-lock.json",
        "installed upstream skill lock",
    )
    official = lock.get("android_official")
    if not isinstance(official, dict):
        raise SkillPlanSnapshotError(
            "blocked: installed Android official skill lock is missing"
        )
    snapshots = official.get("snapshots")
    if (
        official.get("source") != "https://github.com/android/skills"
        or re.fullmatch(r"[0-9a-f]{40}", str(official.get("commit") or "")) is None
        or official.get("runtime_fetch") is not False
        or official.get("catalog")
        != "profiles/android.yaml#android_skills.implementation"
        or official.get("runtime_tree_verification") != "installed-index"
        or not isinstance(snapshots, dict)
    ):
        raise SkillPlanSnapshotError(
            "blocked: installed Android official skill lock is invalid"
        )
    profile = _read_snapshot_yaml(
        root,
        root / ".agent-flow" / "profiles" / "android.yaml",
        "installed Android profile",
    )
    android_skills = profile.get("android_skills")
    implementation = (
        android_skills.get("implementation")
        if isinstance(android_skills, dict)
        else None
    )
    if not isinstance(implementation, list):
        raise SkillPlanSnapshotError(
            "blocked: installed Android official skill catalog is invalid"
        )
    catalog_names = sorted(
        {
            _portable_casefold(item.get("skill"))
            for item in implementation
            if isinstance(item, dict) and item.get("skill")
        }
    )
    snapshot_names = sorted(_portable_casefold(name) for name in snapshots)
    if catalog_names != snapshot_names:
        raise SkillPlanSnapshotError(
            "blocked: installed Android official skill catalog does not match lock coverage"
        )
    source_policy = _read_snapshot_yaml(
        root,
        skills_root / "source-policy.yaml",
        "installed skill source policy",
    )
    official_policy = source_policy.get("official_project_snapshots")
    if not isinstance(official_policy, dict) or any(
        official_policy.get(key) != expected
        for key, expected in {
            "source": official.get("source"),
            "commit": official.get("commit"),
            "catalog": official.get("catalog"),
            "install_policy": official.get("policy"),
            "runtime_fetch": False,
            "offline_validation": "required",
            "runtime_tree_verification": official.get("runtime_tree_verification"),
        }.items()
    ):
        raise SkillPlanSnapshotError(
            "blocked: installed Android official source policy does not match lock provenance"
        )
    license_reference = official.get("license_reference")
    if not isinstance(license_reference, str) or not _safe_relative_path(license_reference):
        raise SkillPlanSnapshotError(
            "blocked: installed Android official license provenance is invalid"
        )
    license_path = skills_root / license_reference
    license_bytes = _read_installed_regular_bytes(
        root,
        license_path,
        "installed Android official license",
    )
    expected_license_hash = official.get("license_sha256")
    if (
        not isinstance(expected_license_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_license_hash) is None
        or hashlib.sha256(license_bytes).hexdigest() != expected_license_hash
    ):
        raise SkillPlanSnapshotError(
            "blocked: installed Android official license provenance changed"
        )
    installed_names = set(_index_skills_by_logical_name(index.get("skills")))
    missing = sorted(set(snapshot_names) - installed_names)
    if missing:
        raise SkillPlanSnapshotError(
            "blocked: installed Android official skill snapshots are missing: "
            + ", ".join(missing)
        )
    for name in snapshot_names:
        provenance = snapshots.get(name)
        upstream_name = provenance.get("upstream_name", name) if isinstance(provenance, dict) else ""
        snapshot_mode = provenance.get("snapshot_mode") if isinstance(provenance, dict) else None
        project_tree_hash = provenance.get("project_tree_hash") if isinstance(provenance, dict) else None
        if not isinstance(provenance, dict) or (
            not is_portable_skill_name(upstream_name)
            or not _safe_relative_path(str(provenance.get("upstream_path") or ""))
            or re.fullmatch(r"[0-9a-f]{64}", str(provenance.get("upstream_tree_hash") or ""))
            is None
            or re.fullmatch(
                r"[0-9a-f]{64}",
                str(provenance.get("upstream_skill_sha256") or ""),
            )
            is None
            or snapshot_mode not in {"bundled-adapter", "install-time-indexed"}
            or (
                snapshot_mode == "bundled-adapter"
                and re.fullmatch(r"[0-9a-f]{64}", str(project_tree_hash or "")) is None
            )
            or (snapshot_mode == "install-time-indexed" and project_tree_hash is not None)
        ):
            raise SkillPlanSnapshotError(
                f"blocked: installed Android official skill provenance is invalid: {name}"
            )


def _read_snapshot_yaml(root: Path, path: Path, label: str) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(
            _read_installed_regular_bytes(root, path, label).decode("utf-8")
        )
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SkillPlanSnapshotError(f"blocked: {label} is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise SkillPlanSnapshotError(f"blocked: {label} must be a mapping: {path}")
    return payload


def _safe_relative_path(value: str) -> bool:
    return (
        bool(value)
        and not Path(value).is_absolute()
        and "\\" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _validate_installed_profile_selection(
    root: Path,
    kit: dict[str, Any],
    index: dict[str, Any],
) -> None:
    kit_profiles = kit.get("profiles")
    selection = index.get("selection")
    index_profiles = selection.get("profiles") if isinstance(selection, dict) else None
    if not isinstance(kit_profiles, list) or not isinstance(index_profiles, list):
        raise SkillPlanSnapshotError(
            "blocked: installed kit profiles do not match the skill index selection"
        )
    try:
        if kit.get("profile") is not None:
            validate_safe_name(kit["profile"], "profile")
        validated_kit = [validate_safe_name(profile, "profile") for profile in kit_profiles]
        validated_index = [validate_safe_name(profile, "profile") for profile in index_profiles]
    except (TypeError, ValueError) as exc:
        raise SkillPlanSnapshotError(
            "blocked: installed kit or skill index has invalid profile names"
        ) from exc
    if validated_kit != validated_index:
        raise SkillPlanSnapshotError(
            "blocked: installed kit profiles do not match the skill index selection"
        )
    kit_selection = kit.get("profile_selection")
    index_selection = selection.get("profile_selection")
    if (kit_selection is not None or index_selection is not None) and (
        kit_selection not in {"auto", "explicit"}
        or kit_selection != index_selection
    ):
        raise SkillPlanSnapshotError(
            "blocked: installed kit profile selection does not match the skill index selection"
        )
    try:
        primary_profile_id(root)
    except (TypeError, ValueError) as exc:
        raise SkillPlanSnapshotError(
            f"blocked: installed primary profile is invalid: {exc}"
        ) from exc


def _require_installed_regular_file(root: Path, path: Path, label: str) -> None:
    lexical_root = Path(os.path.abspath(root))
    lexical_path = Path(os.path.abspath(path))
    try:
        relative = lexical_path.relative_to(lexical_root)
    except ValueError as exc:
        raise SkillPlanSnapshotError(
            f"blocked: {label} escapes the project: {path}"
        ) from exc
    cursor = lexical_root
    for index, part in enumerate(relative.parts):
        cursor /= part
        try:
            mode = cursor.lstat().st_mode
        except OSError as exc:
            raise SkillPlanSnapshotError(
                f"blocked: {label} is unreadable: {cursor}"
            ) from exc
        if stat.S_ISLNK(mode):
            raise SkillPlanSnapshotError(
                f"blocked: {label} may not use symlinks: {cursor}"
            )
        final = index == len(relative.parts) - 1
        if (final and not stat.S_ISREG(mode)) or (not final and not stat.S_ISDIR(mode)):
            raise SkillPlanSnapshotError(
                f"blocked: {label} has an invalid path component: {cursor}"
            )
    try:
        lexical_path.resolve(strict=True).relative_to(lexical_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise SkillPlanSnapshotError(
            f"blocked: {label} escapes the project: {path}"
        ) from exc

def _read_installed_regular_bytes(root: Path, path: Path, label: str) -> bytes:
    lexical_root = Path(os.path.abspath(root))
    lexical_path = Path(os.path.abspath(path))
    try:
        relative = lexical_path.relative_to(lexical_root)
    except ValueError as exc:
        raise SkillPlanSnapshotError(
            f"blocked: {label} escapes the project: {path}"
        ) from exc
    directories: list[tuple[Path, os.stat_result]] = []
    try:
        root_metadata = lexical_root.lstat()
    except OSError as exc:
        raise SkillPlanSnapshotError(
            f"blocked: {label} is unreadable: {lexical_root}"
        ) from exc
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise SkillPlanSnapshotError(
            f"blocked: {label} has an invalid authority root: {lexical_root}"
        )
    directories.append((lexical_root, root_metadata))
    cursor = lexical_root
    initial: os.stat_result | None = None
    for index, part in enumerate(relative.parts):
        cursor /= part
        try:
            metadata = cursor.lstat()
        except OSError as exc:
            raise SkillPlanSnapshotError(
                f"blocked: {label} is unreadable: {cursor}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise SkillPlanSnapshotError(
                f"blocked: {label} may not use symlinks: {cursor}"
            )
        final = index == len(relative.parts) - 1
        if final:
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise SkillPlanSnapshotError(
                    f"blocked: {label} has an invalid path component: {cursor}"
                )
            initial = metadata
        elif stat.S_ISDIR(metadata.st_mode):
            directories.append((cursor, metadata))
        else:
            raise SkillPlanSnapshotError(
                f"blocked: {label} has an invalid path component: {cursor}"
            )
    if initial is None:
        raise SkillPlanSnapshotError(
            f"blocked: {label} has an invalid path component: {lexical_path}"
        )
    try:
        lexical_path.resolve(strict=True).relative_to(
            lexical_root.resolve(strict=True)
        )
    except (OSError, ValueError) as exc:
        raise SkillPlanSnapshotError(
            f"blocked: {label} escapes the project: {path}"
        ) from exc

    def assert_directories() -> None:
        for directory, expected in directories:
            try:
                current = directory.lstat()
            except OSError as exc:
                raise SkillPlanSnapshotError(
                    f"blocked: {label} authority changed while reading: {directory}"
                ) from exc
            if (
                not stat.S_ISDIR(current.st_mode)
                or _filesystem_identity(current) != _filesystem_identity(expected)
            ):
                raise SkillPlanSnapshotError(
                    f"blocked: {label} authority changed while reading: {directory}"
                )

    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    descriptor = -1
    try:
        assert_directories()
        descriptor = os.open(lexical_path, flags)
        before = os.fstat(descriptor)
        assert_directories()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or _filesystem_identity(before) != _filesystem_identity(initial)
        ):
            raise SkillPlanSnapshotError(
                f"blocked: {label} changed while reading: {lexical_path}"
            )
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        current = lexical_path.lstat()
        assert_directories()
        if (
            not stat.S_ISREG(after.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or after.st_nlink != 1
            or current.st_nlink != 1
            or _filesystem_identity(before) != _filesystem_identity(after)
            or _filesystem_identity(after) != _filesystem_identity(current)
        ):
            raise SkillPlanSnapshotError(
                f"blocked: {label} changed while reading: {lexical_path}"
            )
        return b"".join(chunks)
    except SkillPlanSnapshotError:
        raise
    except OSError as exc:
        raise SkillPlanSnapshotError(
            f"blocked: {label} is unreadable: {lexical_path}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)




def _snapshot_strings(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SkillPlanSnapshotError("blocked: installed skill index has invalid list metadata")
    return [str(item) for item in value]


def _routing_snapshot_strings(skill: dict[str, Any], key: str) -> list[str]:
    value = skill.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SkillPlanSnapshotError(
            f"blocked: installed skill has invalid {key}: {skill.get('name')}"
        )
    return value


def _javascript_property_order(value: Any) -> Any:
    """JSON.stringify의 정수 index 우선 object 열거 순서를 재현한다."""
    if isinstance(value, list):
        return [_javascript_property_order(item) for item in value]
    if not isinstance(value, dict):
        return value
    integer_keys: list[tuple[int, str]] = []
    string_keys: list[str] = []
    for raw_key in value:
        key = str(raw_key)
        if _javascript_array_index(key) is not None:
            integer_keys.append((_javascript_array_index(key) or 0, key))
        else:
            string_keys.append(key)
    ordered_keys = [key for _number, key in sorted(integer_keys)] + string_keys
    return {
        key: _javascript_property_order(value[key])
        for key in ordered_keys
    }


def _javascript_array_index(key: str) -> int | None:
    if not key or (len(key) > 1 and key.startswith("0")) or not key.isascii() or not key.isdigit():
        return None
    value = int(key)
    if value >= 2**32 - 1 or str(value) != key:
        return None
    return value


_RUNTIME_PROVIDER_SOURCE_KINDS = frozenset(
    {
        "bundled",
        "host-bootstrap",
        "local",
        "project",
        "project-snapshot",
        "shared",
    }
)


def _runtime_provider_compatibility(value: object) -> dict[str, Any] | None:
    if not _has_exact_keys(value, {"hosts", "profiles", "registry", "source_kinds"}):
        return None
    assert isinstance(value, dict)
    registry_version = value["registry"]
    if (
        not isinstance(registry_version, (int, float))
        or isinstance(registry_version, bool)
        or registry_version != 1
    ):
        return None

    def valid_selectors(entries: object) -> bool:
        return (
            isinstance(entries, list)
            and bool(entries)
            and all(
                isinstance(entry, str)
                and (
                    entry == "*"
                    or (
                        is_portable_skill_name(entry)
                        and _portable_casefold(entry) == entry
                    )
                )
                for entry in entries
            )
            and len(set(entries)) == len(entries)
        )

    profiles = value["profiles"]
    hosts = value["hosts"]
    source_kinds = value["source_kinds"]
    if not valid_selectors(profiles) or not valid_selectors(hosts):
        return None
    if (
        not isinstance(source_kinds, list)
        or not source_kinds
        or any(
            not isinstance(source_kind, str)
            or source_kind not in _RUNTIME_PROVIDER_SOURCE_KINDS
            for source_kind in source_kinds
        )
        or len(set(source_kinds)) != len(source_kinds)
    ):
        return None
    return {
        "registry": 1,
        "profiles": list(profiles),
        "hosts": list(hosts),
        "source_kinds": list(source_kinds),
    }


def _is_runtime_provider_evidence(claim: dict[str, Any]) -> bool:
    def safe_locator(value: object) -> bool:
        return (
            isinstance(value, str)
            and bool(value)
            and not any(
                character.isspace()
                or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
                for character in value
            )
        )

    catalog_ref = claim["catalog_ref"]
    catalog_hash = claim["catalog_hash"]
    catalog_valid = (
        catalog_ref is None
        and catalog_hash is None
    ) or (
        safe_locator(catalog_ref)
        and isinstance(catalog_hash, str)
        and re.fullmatch(r"[0-9a-f]{64}", catalog_hash) is not None
    )
    source_host = claim["source_host"]
    return (
        catalog_valid
        and isinstance(claim["content_hash_mode"], str)
        and claim["content_hash_mode"] in {"observed", "pinned", "verified"}
        and isinstance(claim["source_kind"], str)
        and claim["source_kind"] in _RUNTIME_PROVIDER_SOURCE_KINDS
        and (
            source_host is None
            or (
                is_portable_skill_name(source_host)
                and _portable_casefold(source_host) == source_host
            )
        )
        and safe_locator(claim["source_locator"])
    )


def _runtime_skill_provider_index(
    index: dict[str, Any],
) -> dict[str, dict[str, Any]] | None:
    registry_present = "provider_registry" in index
    claims_present = "skill_providers" in index
    if not registry_present and not claims_present:
        return None
    registry = index.get("provider_registry")
    claims = index.get("skill_providers")
    if (
        not registry_present
        or not claims_present
        or not _has_exact_keys(registry, {"fingerprint", "quarantined", "version"})
        or isinstance(registry["version"], bool)
        or not isinstance(registry["version"], (int, float))
        or registry["version"] != 1
        or not isinstance(registry["fingerprint"], str)
        or re.fullmatch(r"[0-9a-f]{64}", registry["fingerprint"]) is None
        or not isinstance(registry["quarantined"], list)
        or any(
            not _is_runtime_provider_diagnostic(diagnostic)
            for diagnostic in registry["quarantined"]
        )
        or not isinstance(claims, list)
    ):
        raise SkillPlanSnapshotError("blocked: invalid skill provider index")
    fingerprint = registry["fingerprint"]
    by_concrete_id: dict[str, dict[str, Any]] = {}
    claim_fields = {
        "adapter",
        "aliases",
        "compatibility",
        "catalog_hash",
        "catalog_ref",
        "concrete_id",
        "ownership",
        "provenance_revision",
        "content_hash_mode",
        "provider_id",
        "provider_version",
        "registry_fingerprint",
        "source",
        "source_hash",
        "source_host",
        "source_kind",
        "source_locator",
        "status",
        "trust_tier",
    }
    for claim in claims:
        if not _has_exact_keys(claim, claim_fields):
            raise SkillPlanSnapshotError("blocked: invalid skill provider claim")
        aliases = claim["aliases"]
        compatibility = _runtime_provider_compatibility(claim["compatibility"])
        concrete_id_value = claim["concrete_id"]
        provider_id_value = claim["provider_id"]
        adapter_value = claim["adapter"]
        source = claim["source"]
        valid = (
            is_portable_skill_name(concrete_id_value)
            and _portable_casefold(concrete_id_value) == concrete_id_value
            and is_portable_skill_name(provider_id_value)
            and _portable_casefold(provider_id_value) == provider_id_value
            and is_portable_skill_name(adapter_value)
            and _portable_casefold(adapter_value) == adapter_value
            and isinstance(claim["provider_version"], str)
            and re.fullmatch(
                r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?",
                claim["provider_version"],
            )
            is not None
            and isinstance(claim["trust_tier"], str)
            and claim["trust_tier"]
            in {"user", "project", "organization", "official"}
            and isinstance(claim["ownership"], str)
            and claim["ownership"]
            in {"user", "project", "organization", "upstream"}
            and (
                claim["provenance_revision"] is None
                or (
                    isinstance(claim["provenance_revision"], str)
                    and re.fullmatch(
                        r"[0-9a-f]{40}",
                        claim["provenance_revision"],
                    )
                    is not None
                )
            )
            and isinstance(source, str)
            and bool(source)
            and not any(
                character.isspace()
                or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
                for character in source
            )
            and isinstance(claim["source_hash"], str)
            and re.fullmatch(r"[0-9a-f]{64}", claim["source_hash"]) is not None
            and _is_runtime_provider_evidence(claim)
            and claim["status"]
            == ("observed" if claim["content_hash_mode"] == "observed" else "verified")
            and claim["registry_fingerprint"] == fingerprint
            and compatibility is not None
            and isinstance(aliases, list)
            and all(
                is_portable_skill_name(alias)
                and _portable_casefold(alias) == alias
                for alias in aliases
            )
            and len(set(aliases)) == len(aliases)
            and concrete_id_value not in aliases
        )
        if not valid:
            raise SkillPlanSnapshotError("blocked: invalid skill provider claim")
        concrete_id = concrete_id_value
        if concrete_id in by_concrete_id:
            raise SkillPlanSnapshotError(
                f"blocked: duplicate skill provider claim: {concrete_id}"
            )
        by_concrete_id[concrete_id] = {
            "id": provider_id_value,
            "version": claim["provider_version"],
            "trust_tier": claim["trust_tier"],
            "ownership": claim["ownership"],
            "provenance_revision": claim["provenance_revision"],
            "source": source,
            "adapter": adapter_value,
            "source_hash": claim["source_hash"],
            "source_host": claim["source_host"],
            "source_kind": claim["source_kind"],
            "source_locator": claim["source_locator"],
            "content_hash_mode": claim["content_hash_mode"],
            "catalog_ref": claim["catalog_ref"],
            "catalog_hash": claim["catalog_hash"],
            "status": claim["status"],
            "registry_fingerprint": fingerprint,
            "compatibility": compatibility,
        }
    return by_concrete_id
def _canonical_runtime_skill_provider_metadata(
    index: dict[str, Any],
) -> dict[str, Any] | None:
    if _runtime_skill_provider_index(index) is None:
        return None
    registry = index["provider_registry"]
    claims = index["skill_providers"]
    assert isinstance(registry, dict)
    assert isinstance(claims, list)
    normalized_claims = []
    for claim in claims:
        compatibility = _runtime_provider_compatibility(claim["compatibility"])
        assert compatibility is not None
        normalized_claims.append(
            {
                **claim,
                "aliases": list(claim["aliases"]),
                "compatibility": compatibility,
            }
        )
    normalized_claims.sort(
        key=lambda claim: (claim["concrete_id"], claim["provider_id"])
    )
    return {
        "provider_registry": {
            "version": 1,
            "fingerprint": registry["fingerprint"],
            "quarantined": registry["quarantined"],
        },
        "skill_providers": normalized_claims,
    }




def _has_exact_keys(value: object, expected: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == expected


def _is_runtime_provider_diagnostic(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    expected = {
        "detail",
        "metadata_path",
        "provider_id",
        "reason",
        "repairable",
    }
    if "concrete_id" in value:
        expected.add("concrete_id")
    if set(value) != expected:
        return False

    def valid_name(name: object) -> bool:
        return (
            name is None
            or (
                is_portable_skill_name(name)
                and _portable_casefold(name) == name
            )
        )

    def safe_text(text: object) -> bool:
        return (
            isinstance(text, str)
            and bool(text)
            and not any(
                unicodedata.category(character) in {"Cc", "Cf", "Cs"}
                for character in text
            )
        )

    return (
        is_portable_skill_name(value["reason"])
        and _portable_casefold(value["reason"]) == value["reason"]
        and valid_name(value["provider_id"])
        and (
            "concrete_id" not in value
            or valid_name(value["concrete_id"])
        )
        and safe_text(value["detail"])
        and safe_text(value["metadata_path"])
        and isinstance(value["repairable"], bool)
    )


def resolve_runtime_skill_plan(
    index: dict[str, Any],
    phase_id: str,
    changed_files: Iterable[str] = (),
    task_scope: str = "",
    required_skills: Iterable[str] = (),
) -> dict[str, Any]:
    normalized_task_scope = _normalize_task_scope(task_scope)
    phase_required = set(_logical_strings(list(required_skills)))
    empty_plan = {
        "phase": phase_id,
        "active_profiles": [],
        "touched_profiles": [],
        "changed_files": [],
        "task_scope": normalized_task_scope,
        "skills": [],
        "missing": [],
        "missing_profiles": [],
        "resolution_errors": [],
    }
    if phase_id not in CODE_SKILL_PHASES and not phase_required:
        return empty_plan
    if not isinstance(index.get("selection"), dict):
        return empty_plan
    try:
        compatibility = SkillCompatibilityCatalog.from_value(
            index.get("compatibility")
        )
    except SkillCompatibilityError as exc:
        raise SkillPlanSnapshotError(
            f"blocked: invalid skill compatibility metadata: {exc}"
        ) from exc
    providers_by_name = _runtime_skill_provider_index(index)
    selection = index.get("selection") if isinstance(index.get("selection"), dict) else {}
    active_profiles = sorted(_logical_strings(selection.get("profiles")))
    raw_skill_profiles = selection.get("skill_profiles")
    installed_profiles = (
        set(_logical_strings(raw_skill_profiles))
        if isinstance(raw_skill_profiles, list)
        else set(active_profiles)
    )
    normalized_files = sorted({str(file).replace("\\", "/") for file in changed_files if file})
    routing = selection.get("profile_routing")
    if not isinstance(routing, dict):
        routing = {}
    touched_profiles = _resolve_touched_profiles(
        active_profiles,
        normalized_files,
        normalized_task_scope,
        routing,
    )
    missing_profiles = sorted(touched_profiles - installed_profiles)
    raw_required = set(phase_required)
    if phase_id in CODE_SKILL_PHASES:
        raw_required.add("code-generation-discipline")
    required_review = selection.get("required_review")
    if not isinstance(required_review, dict):
        required_review = {}
    by_name = _index_skills_by_logical_name(index.get("skills"))
    for profile in touched_profiles:
        raw_required.update(_logical_strings(required_review.get(profile)))
        conditional_skills = selection.get("conditional_skills")
        if not isinstance(conditional_skills, dict):
            conditional_skills = {}
        profile_catalog = conditional_skills.get(profile)
        if not isinstance(profile_catalog, dict):
            profile_catalog = {}
        mode = "review" if phase_id in REVIEW_SKILL_PHASES else "implementation"
        catalog = set(_logical_strings(profile_catalog.get(mode)))
        profiles_config = routing.get("profiles")
        if not isinstance(profiles_config, dict):
            profiles_config = {}
        profile_config = profiles_config.get(profile)
        if not isinstance(profile_config, dict):
            profile_config = {}
        routes = profile_config.get("skill_routes")
        if not isinstance(routes, list):
            routes = []
        for route in routes:
            if not isinstance(route, dict) or (
                not _files_match_rules(normalized_files, route.get("file_rules"))
                and not _task_matches_terms(normalized_task_scope, route.get("task_terms"))
            ):
                continue
            raw_required.update(
                name
                for name in _logical_strings(route.get("skills"))
                # activate routed skills that are catalog members or installed profile skills.
                if name in catalog or name in by_name
            )
    try:
        compatibility.validate_concrete_ids(by_name)
    except SkillCompatibilityError as exc:
        raise SkillPlanSnapshotError(
            f"blocked: invalid skill compatibility metadata: {exc}"
        ) from exc
    raw_explicit = set(_logical_strings(selection.get("explicit_skills")))
    explicit = {
        resolution.canonical
        for name in raw_explicit
        if (resolution := compatibility.resolve(name)).resolved
    }
    for name, skill in by_name.items():
        phases = _strings(skill.get("workflowPhases"))
        phase_matches = not phases or phase_id in phases
        activation = str(skill.get("activation") or "on-demand")
        if name in explicit:
            raw_required.add(name)
        elif activation == "always" and phase_matches:
            raw_required.add(name)
        elif activation == "conditional" and phase_matches and (
            _task_matches_terms(normalized_task_scope, skill.get("taskTerms"))
            or any(
                _path_matches_glob(file, glob)
                for file in normalized_files
                for glob in _strings(skill.get("pathGlobs"))
            )
        ):
            raw_required.add(name)

    required: set[str] = set()
    requests_by_canonical: dict[str, set[str]] = {}
    unresolved_names: set[str] = set()
    resolution_errors: dict[tuple[str, str], dict[str, Any]] = {}

    def add_required(reference: str) -> bool:
        resolution = compatibility.resolve(reference)
        if not resolution.resolved:
            unresolved_names.add(resolution.requested)
            diagnostic = resolution.diagnostic()
            resolution_errors[(resolution.requested, str(resolution.reason))] = diagnostic
            return False
        requests_by_canonical.setdefault(resolution.canonical, set()).add(
            resolution.requested
        )
        if resolution.canonical in required:
            return False
        required.add(resolution.canonical)
        return True

    for name in sorted(raw_required):
        add_required(name)

    changed = True
    while changed:
        changed = False
        for name in tuple(required):
            skill = by_name.get(name)
            if skill is None:
                continue
            for dependency in _logical_strings(skill.get("requires")):
                changed = add_required(dependency) or changed
    required.difference_update(EXPLICIT_ONLY_SKILLS - explicit)
    skills: list[dict[str, Any]] = []
    missing = set(unresolved_names)
    for name in sorted(required):
        skill = by_name.get(name)
        resolution = compatibility.resolve(name)
        if skill is None:
            missing.add(name)
            for requested in requests_by_canonical.get(name, {name}):
                diagnostic = {
                    "reason": "canonical_not_installed",
                    "requested": requested,
                    "canonical": name,
                    "capabilities": list(resolution.capabilities),
                    "repairable": False,
                }
                resolution_errors[(requested, "canonical_not_installed")] = diagnostic
            continue
        record = {
            "name": name,
            "path": str(skill.get("path") or ""),
            "tree_hash": skill.get("tree_hash"),
        }
        if providers_by_name is not None:
            provider = providers_by_name.get(name)
            if provider is None:
                raise SkillPlanSnapshotError(
                    f"blocked: missing skill provider claim: {name}"
                )
            if provider["source_hash"] != skill.get("tree_hash"):
                raise SkillPlanSnapshotError(
                    f"blocked: skill provider source hash mismatch: {name}"
                )
            record["provider"] = provider
        if resolution.capabilities:
            record["capabilities"] = list(resolution.capabilities)
        skills.append(record)
    return {
        "phase": phase_id,
        "active_profiles": active_profiles,
        "touched_profiles": sorted(touched_profiles),
        "changed_files": normalized_files,
        "task_scope": normalized_task_scope,
        "skills": skills,
        "missing": sorted(missing),
        "missing_profiles": missing_profiles,
        "resolution_errors": [
            resolution_errors[key] for key in sorted(resolution_errors)
        ],
    }


def _path_matches_glob(file: str, glob: str) -> bool:
    if not glob or "\\" in glob or Path(glob).is_absolute() or ".." in glob.split("/"):
        return False
    pattern = re.escape(glob)
    pattern = pattern.replace(r"\*\*", ".*")
    pattern = pattern.replace(r"\*", "[^/]*")
    pattern = pattern.replace(r"\?", "[^/]")
    return re.fullmatch(pattern, file) is not None


def _resolve_touched_profiles(
    active_profiles: list[str],
    changed_files: list[str],
    task_scope: str,
    routing: dict[str, Any],
) -> set[str]:
    if not changed_files and not task_scope:
        return set(active_profiles)
    touched: set[str] = set()
    profiles_config = routing.get("profiles")
    if not isinstance(profiles_config, dict):
        profiles_config = {}
    escalations = routing.get("escalations")
    if not isinstance(escalations, dict):
        escalations = {}
    task_profiles = _resolve_profiles_for_task(active_profiles, task_scope, profiles_config)
    for profile in _resolve_profiles_for_files(active_profiles, changed_files, profiles_config):
        if _task_overrides_generic_file_profile(
            profile,
            task_profiles,
            changed_files,
            profiles_config,
        ):
            continue
        touched.add(profile)
    touched.update(task_profiles)
    for profile in active_profiles:
        profile_escalations = escalations.get(profile)
        if not isinstance(profile_escalations, list):
            profile_escalations = []
        for escalation in profile_escalations:
            if not isinstance(escalation, dict) or (
                not _files_match_rules(changed_files, escalation.get("file_rules"))
                and not _task_matches_terms(task_scope, escalation.get("task_terms"))
            ):
                continue
            touched.add(profile)
            target = escalation.get("profile")
            if isinstance(target, str) and target:
                touched.add(target)
    if not touched and not changed_files:
        touched.update(active_profiles)
    elif not touched and len(active_profiles) == 1:
        touched.add(active_profiles[0])
    return touched


def _task_overrides_generic_file_profile(
    profile: str,
    task_profiles: set[str],
    changed_files: list[str],
    profiles_config: dict[str, Any],
) -> bool:
    if profile in task_profiles:
        return False
    config = profiles_config.get(profile)
    if not isinstance(config, dict):
        config = {}
    family = str(config.get("family") or profile)
    has_explicit_family_profile = False
    for candidate in task_profiles:
        candidate_config = profiles_config.get(candidate)
        if not isinstance(candidate_config, dict):
            candidate_config = {}
        if str(candidate_config.get("family") or candidate) == family:
            has_explicit_family_profile = True
            break
    if not has_explicit_family_profile:
        return False
    score = max(
        (_best_file_rule_score(file, config.get("file_rules")) for file in changed_files),
        default=-1,
    )
    return score <= GENERIC_FILE_RULE_SCORE


def _resolve_profiles_for_files(
    active_profiles: list[str],
    changed_files: list[str],
    profiles_config: dict[str, Any],
) -> set[str]:
    touched: set[str] = set()
    for file in changed_files:
        candidates: list[dict[str, Any]] = []
        for profile in active_profiles:
            config = profiles_config.get(profile)
            if not isinstance(config, dict):
                config = {}
            score = _best_file_rule_score(file, config.get("file_rules"))
            if score < 0:
                continue
            candidates.append(
                {
                    "profile": profile,
                    "family": str(config.get("family") or profile),
                    "priority": _profile_priority(config.get("priority")),
                    "fallback": config.get("fallback") is True,
                    "score": score,
                }
            )
        scoped_candidates = (
            [candidate for candidate in candidates if not candidate["fallback"]]
            if any(not candidate["fallback"] for candidate in candidates)
            else candidates
        )
        touched.update(
            candidate["profile"] for candidate in _highest_priority_per_family(scoped_candidates)
        )
    return touched


def _resolve_profiles_for_task(
    active_profiles: list[str],
    task_scope: str,
    profiles_config: dict[str, Any],
) -> set[str]:
    if not task_scope:
        return set()
    candidates: list[dict[str, Any]] = []
    for profile in active_profiles:
        config = profiles_config.get(profile)
        if not isinstance(config, dict):
            config = {}
        matches = _task_term_matches(task_scope, config.get("task_terms"))
        score = max((int(match["length"]) for match in matches), default=-1)
        if score < 0:
            continue
        candidates.append(
            {
                "profile": profile,
                "family": str(config.get("family") or profile),
                "priority": _profile_priority(config.get("priority")),
                "score": score,
                "matches": matches,
            }
        )
    return _explicit_task_profiles(candidates)


def _explicit_task_profiles(candidates: list[dict[str, Any]]) -> set[str]:
    matches = sorted(
        (
            {**candidate, **match}
            for candidate in candidates
            for match in candidate["matches"]
        ),
        key=lambda match: (
            -int(match["length"]),
            int(match["start"]),
            -int(match["priority"]),
            str(match["profile"]),
        ),
    )
    claimed: list[dict[str, Any]] = []
    selected: set[str] = set()
    for match in matches:
        overlaps_claimed_term = any(
            current["family"] == match["family"]
            and int(match["start"]) < int(current["end"])
            and int(current["start"]) < int(match["end"])
            for current in claimed
        )
        if overlaps_claimed_term:
            continue
        claimed.append(match)
        selected.add(str(match["profile"]))
    return selected


def _highest_priority_per_family(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    winners: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        family = str(candidate["family"])
        current = winners.get(family)
        if current is None or int(candidate["score"]) > int(current["score"]) or (
            int(candidate["score"]) == int(current["score"])
            and int(candidate["priority"]) > int(current["priority"])
        ) or (
            int(candidate["score"]) == int(current["score"])
            and int(candidate["priority"]) == int(current["priority"])
            and str(candidate["profile"]) < str(current["profile"])
        ):
            winners[family] = candidate
    return list(winners.values())


def _profile_priority(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _files_match_rules(files: list[str], rules: object) -> bool:
    if not isinstance(rules, list) or not rules:
        return False
    return any(
        _file_matches_rule(file, rule)
        for file in files
        for rule in rules
        if isinstance(rule, dict)
    )


def _best_file_rule_score(file: str, rules: object) -> int:
    if not isinstance(rules, list):
        return -1
    best = -1
    for rule in rules:
        if not isinstance(rule, dict) or not _file_matches_rule(file, rule):
            continue
        score = 1 if rule.get("any_file") is True else 0
        if _strings(rule.get("extensions")):
            score += 20
        if _strings(rule.get("names")):
            score += 50
        if _strings(rule.get("path_terms")):
            score += 60
        if _strings(rule.get("prefixes")):
            score += 80
        best = max(best, score)
    return best


def _normalize_task_scope(task_scope: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(task_scope or "")).split())


def _task_matches_terms(task_scope: str, terms: object) -> bool:
    return _best_task_term_score(task_scope, terms) >= 0


def _best_task_term_score(task_scope: str, terms: object) -> int:
    return max(
        (int(match["length"]) for match in _task_term_matches(task_scope, terms)),
        default=-1,
    )


def _task_term_matches(task_scope: str, terms: object) -> list[dict[str, int]]:
    if not task_scope or not isinstance(terms, list):
        return []
    normalized = task_scope.lower()
    matches: list[dict[str, int]] = []
    for raw_term in terms:
        term = " ".join(unicodedata.normalize("NFKC", str(raw_term)).split()).lower()
        if not term:
            continue
        matches.extend(_task_term_ranges(normalized, term))
    return matches


def _task_term_ranges(task_scope: str, term: str) -> list[dict[str, int]]:
    prefix = r"(?<![a-z0-9])" if term[0].isascii() and term[0].isalnum() else ""
    suffix = r"(?![a-z0-9])" if term[-1].isascii() and term[-1].isalnum() else ""
    pattern = re.compile(f"{prefix}{re.escape(term)}{suffix}")
    matches: list[dict[str, int]] = []
    offset = 0
    while match := pattern.search(task_scope, offset):
        matches.append({"start": match.start(), "end": match.end(), "length": len(term)})
        offset = match.start() + 1
    return matches


def _file_matches_rule(file: str, rule: dict[str, Any]) -> bool:
    normalized = file.lower()
    basename = normalized.rsplit("/", 1)[-1]
    checks: list[bool] = []
    if rule.get("any_file") is True:
        checks.append(True)
    prefixes = _strings(rule.get("prefixes"))
    if prefixes:
        checks.append(any(normalized.startswith(value.lower()) for value in prefixes))
    extensions = _strings(rule.get("extensions"))
    if extensions:
        checks.append(any(normalized.endswith(value.lower()) for value in extensions))
    names = _strings(rule.get("names"))
    if names:
        checks.append(any(basename == value.lower() for value in names))
    path_terms = _strings(rule.get("path_terms"))
    if path_terms:
        checks.append(any(value.lower() in normalized for value in path_terms))
    return bool(checks) and all(checks)


def profile_skill_prompt_block(
    index_root: Path,
    phase_id: str,
    worktree_root: Path,
    task_scope: str | None = None,
    base_commit: str | None = None,
    required_skills: Iterable[str] = (),
) -> str:
    phase_required = tuple(required_skills)
    if phase_id not in CODE_SKILL_PHASES and not phase_required:
        return ""
    index_path = index_root / ".agent-flow" / "skills" / "index.json"
    kit_path = index_root / ".agent-flow" / "kit.json"
    index_present = index_path.exists() or index_path.is_symlink()
    kit_present = kit_path.exists() or kit_path.is_symlink()
    if not index_present:
        if kit_present:
            raise SkillPlanSnapshotError(
                "blocked: installed skill index is missing while kit metadata exists"
            )
        return ""
    index = _read_snapshot_json(index_root, index_path, "installed skill index")
    plan = resolve_runtime_skill_plan(
        index,
        phase_id,
        runtime_changed_files(
            index_root,
            worktree_root,
            base_commit,
        ),
        _configured_task_scope(index_root) if task_scope is None else task_scope,
        phase_required,
    )
    if plan["missing_profiles"]:
        raise RuntimeError(
            "missing required skill profiles in project snapshot: "
            + ", ".join(plan["missing_profiles"])
            + "; finish the active run, reinstall from the leader checkout, and start a new run"
        )
    if plan["missing"]:
        raise RuntimeError(
            "missing required profile skills in project snapshot: "
            + ", ".join(plan["missing"])
        )
    if not plan["skills"]:
        return ""
    changed = plan["changed_files"][:20]
    lines = [
        "\n## Required profile skills",
        "",
        "Read every listed project snapshot before writing or reviewing code. "
        "Do not resolve skills from host-global directories at runtime.",
        f"Active profiles: {', '.join(plan['active_profiles']) or 'generic'}",
        f"Touched profiles: {', '.join(plan['touched_profiles']) or 'generic'}",
    ]
    if changed:
        extra = len(plan["changed_files"]) - len(changed)
        lines.append(f"Changed files: {', '.join(changed)}{f' (+{extra})' if extra else ''}")
    lines.append("")
    lines.extend(
        f"- `{skill['path']}` (`{skill['name']}`) — "
        f"`{_verified_profile_skill_prompt_path(index_root, skill)}`"
        for skill in plan["skills"]
    )
    lines.append("")
    return "\n".join(lines)


def _verified_profile_skill_prompt_path(
    index_root: Path,
    skill: dict[str, Any],
) -> Path:
    skill_path = _installed_skill_path(index_root, skill)
    try:
        mode = skill_path.lstat().st_mode
    except OSError as exc:
        raise SkillPlanSnapshotError(
            f"blocked: installed skill snapshot is unreadable: {skill_path}"
        ) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise SkillPlanSnapshotError(
            f"blocked: installed skill snapshot is not a regular file: {skill_path}"
        )
    expected_hash = skill.get("tree_hash")
    if not isinstance(expected_hash, str) or not expected_hash:
        raise SkillPlanSnapshotError(
            f"blocked: installed skill snapshot has no tree hash: {skill.get('name')}"
        )
    if hash_skill_tree(skill_path.parent, authority_root=index_root) != expected_hash:
        raise SkillPlanSnapshotError(
            f"blocked: installed skill snapshot changed: {skill.get('name')}"
        )
    return skill_path


def runtime_changed_files(
    index_root: Path,
    worktree_root: Path,
    base_commit: str | None = None,
) -> tuple[str, ...]:
    return _changed_files(
        worktree_root,
        base_commit or _configured_base_commit(index_root, worktree_root),
    )


def _changed_files(root: Path, base_commit: str | None = None) -> tuple[str, ...]:
    if base_commit:
        _validate_git_revision(base_commit, "base commit")
    try:
        probe = subprocess.run(
            [_git_executable(), "rev-parse", "--show-toplevel"],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise SkillPlanSnapshotError(
            f"blocked: changed-file query failed: {exc}"
        ) from exc
    if probe.returncode != 0:
        detail = f"{probe.stderr}\n{probe.stdout}"
        if probe.returncode == 128 and "not a git repository" in detail.lower():
            return ()
        raise SkillPlanSnapshotError(
            "blocked: changed-file query failed: git rev-parse --show-toplevel"
        )
    git_root = Path(probe.stdout.strip())
    if not probe.stdout.strip():
        raise SkillPlanSnapshotError(
            "blocked: changed-file query failed: git rev-parse returned no repository root"
        )

    files: set[str] = set()

    def collect(args: list[str]) -> None:
        try:
            result = subprocess.run(
                [_git_executable(), *args],
                cwd=git_root,
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            raise SkillPlanSnapshotError(
                f"blocked: changed-file query failed: {exc}"
            ) from exc
        if result.returncode != 0:
            raise SkillPlanSnapshotError(
                "blocked: changed-file query failed: git " + " ".join(args)
            )
        files.update(line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip())

    collect(["diff", "--name-only", "--diff-filter=ACMRD", "HEAD"])
    collect(["diff", "--name-only", "--diff-filter=ACMRD", "--cached"])
    collect(["ls-files", "--others", "--exclude-standard"])
    if base_commit:
        collect(["diff", "--name-only", "--diff-filter=ACMRD", f"{base_commit}...HEAD"])
    return tuple(sorted(files))


def _configured_base_commit(index_root: Path, worktree_root: Path) -> str | None:
    state = _read_json(index_root / ".agent-flow" / "state" / "current-run.json")
    pinned = state.get("base_commit") if isinstance(state, dict) else None
    if isinstance(pinned, str) and pinned:
        return _validate_git_revision(pinned, "pinned base commit")
    try:
        profile = primary_profile_id(index_root)
        payload = load_project_profile_payload(index_root, profile)
    except ValueError as exc:
        if "profile base ref" in str(exc) or "branching.base" in str(exc):
            raise SkillPlanSnapshotError(str(exc)) from exc
        raise
    base_ref = "HEAD"
    if isinstance(payload, dict):
        branching = payload.get("branching")
        configured = branching.get("base") if isinstance(branching, dict) else None
        if isinstance(configured, str) and configured:
            base_ref = _validate_git_revision(configured, "profile base ref")
    git = _git_executable()
    command = [git, "rev-parse", "HEAD"] if base_ref == "HEAD" else [git, "merge-base", "HEAD", base_ref]
    result = subprocess.run(
        command,
        cwd=worktree_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    fallback = subprocess.run(
        [git, "rev-parse", "HEAD"],
        cwd=worktree_root,
        text=True,
        capture_output=True,
        check=False,
    )
    return fallback.stdout.strip() if fallback.returncode == 0 and fallback.stdout.strip() else None


def _configured_task_scope(index_root: Path) -> str:
    state = _read_json(index_root / ".agent-flow" / "state" / "current-run.json")
    task = state.get("task") if isinstance(state, dict) else None
    return task if isinstance(task, str) else ""


def _git_executable() -> str:
    configured = os.environ.get("AGENT_FLOW_GIT_EXECUTABLE")
    return configured if configured and Path(configured).is_absolute() else "git"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item]


def _portable_casefold(value: object) -> str:
    try:
        name = validate_portable_skill_name(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise SkillPlanSnapshotError(
            f"blocked: installed skill metadata has an invalid portable name: {value!r}"
        ) from exc
    return name.lower()


def _logical_strings(value: object) -> list[str]:
    return list(dict.fromkeys(_portable_casefold(item) for item in _strings(value)))


def _index_skills_by_logical_name(value: object) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        raise SkillPlanSnapshotError("blocked: installed skill index has invalid skills")
    by_name: dict[str, dict[str, Any]] = {}
    for skill in value:
        if not isinstance(skill, dict) or not skill.get("name"):
            raise SkillPlanSnapshotError(
                "blocked: installed skill index has invalid skill entry"
            )
        raw_name = str(skill["name"])
        try:
            validate_portable_skill_name(raw_name)
        except ValueError as exc:
            raise SkillPlanSnapshotError(
                "blocked: installed skill index has invalid skill name"
            ) from exc
        logical_name = _portable_casefold(raw_name)
        existing = by_name.get(logical_name)
        if existing is not None:
            descriptions = sorted(
                f"{candidate.get('name')}:{candidate.get('path') or '<no-path>'}"
                for candidate in (existing, skill)
            )
            raise SkillPlanSnapshotError(
                "blocked: conflicting installed skill index logical skill name: "
                f"{logical_name} ({', '.join(descriptions)})"
            )
        by_name[logical_name] = skill
    return by_name


def _validate_git_revision(value: str, label: str) -> str:
    if value.startswith("-") or any(character.isspace() or ord(character) < 32 for character in value):
        raise SkillPlanSnapshotError(f"blocked: invalid {label}: {value!r}")
    return value
