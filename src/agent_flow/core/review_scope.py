from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re

from agent_flow.core.worktree_isolation import git_safe


_COMMIT_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def publication_review_base(run_meta: Mapping[str, object]) -> str | None:
    if "review_scope" not in run_meta:
        return None
    scope = run_meta["review_scope"]
    if (
        not isinstance(scope, dict)
        or set(scope) != {"kind", "base_oid"}
        or scope.get("kind") != "publication"
        or not isinstance(scope.get("base_oid"), str)
        or not _COMMIT_OID.fullmatch(scope["base_oid"])
    ):
        raise ValueError("publication review scope is malformed")
    return scope["base_oid"]


def validate_publication_review_base(project_root: Path, base_oid: str) -> None:
    if not _COMMIT_OID.fullmatch(base_oid):
        raise ValueError("publication review requires a full commit OID, not a mutable ref")
    commit = git_safe(
        "cat-file", "-t", base_oid, cwd=project_root, optional_locks=False,
    )
    if not commit.ok or commit.stdout.strip() != "commit":
        raise ValueError("publication review base must identify an existing commit")
    ancestor = git_safe(
        "merge-base", "--is-ancestor", base_oid, "HEAD",
        cwd=project_root, optional_locks=False,
    )
    if not ancestor.ok:
        raise ValueError("publication review base must be an ancestor of the current HEAD")


def review_scope_block_reason(run_meta: Mapping[str, object], phase_id: str) -> str | None:
    try:
        base_oid = publication_review_base(run_meta)
    except ValueError:
        return "invalid_review_scope"
    if base_oid is not None and phase_id in {"merge", "merge-approval"}:
        return "publication_review_cannot_approve_merge"
    return None
