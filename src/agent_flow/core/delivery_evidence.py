"""commit·push-pr artifact가 실제 배달을 증명하는가.

marker 존재가 아니라 **저장소와 원격의 상태**를 본다: commit OID가 있는가, 제목이
Conventional Commit인가, 보호 브랜치로 밀지 않았는가, PR이 profile이 선언한 target을
가리키는가. 그래서 이 층은 텍스트 파서가 아니라 git/gh 관측이고, route 파싱과 다른
자리에 있어야 한다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agent_flow.core.commands import run_safe_command
from agent_flow.core.markers import unfenced_markdown_text
from agent_flow.core.worktree_isolation import git_safe, sanitized_worker_env

PROTECTED_BRANCHES = frozenset({"main", "master", "develop"})
CONVENTIONAL_COMMIT_RE = re.compile(
    r"^(?:feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)"
    r"(?:\([^)]+\))?!?: \S.*$"
)


def missing_delivery_evidence(
    project_root: Path,
    phase_id: str,
    text: str,
    *,
    profile: dict[str, Any] | None = None,
) -> list[str]:
    if phase_id == "commit":
        return _missing_commit_evidence(project_root, text)
    if phase_id == "push-pr":
        target_branch = _profile_pr_target(profile)
        if target_branch is None:
            return ["delivery evidence: profile pr.target_branch is unavailable"]
        return _missing_push_pr_evidence(
            project_root,
            text,
            target_branch=target_branch,
        )
    return []


def _delivery_fields(
    text: str, names: tuple[str, ...]
) -> tuple[dict[str, str], list[str]]:
    body = unfenced_markdown_text(text)
    fields: dict[str, str] = {}
    errors: list[str] = []
    for name in names:
        values = [
            match.group(1).strip()
            for match in re.finditer(
                rf"^[ \t]*{re.escape(name)}[ \t]*:[ \t]*(.*?)[ \t]*$",
                body,
                re.MULTILINE,
            )
            if match.group(1).strip()
        ]
        if len(values) != 1:
            errors.append(
                f"delivery evidence: {name}: requires exactly one non-empty value"
            )
            continue
        fields[name] = values[0]
    return fields, errors


def _missing_commit_evidence(project_root: Path, text: str) -> list[str]:
    fields, errors = _delivery_fields(text, ("commit-oid", "commit-subject"))
    if errors:
        return errors

    head = git_safe(
        "rev-parse", "--verify", "HEAD^{commit}",
        cwd=project_root,
        optional_locks=False,
    )
    if not head.ok or not head.stdout.strip():
        return ["delivery evidence: cannot prove current git HEAD"]
    head_oid = head.stdout.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40,64}", fields["commit-oid"].lower()):
        errors.append("delivery evidence: commit-oid must be a full git object id")
    elif fields["commit-oid"].lower() != head_oid:
        errors.append("delivery evidence: commit-oid does not match current HEAD")

    branch = git_safe(
        "symbolic-ref", "--quiet", "--short", "HEAD",
        cwd=project_root,
        optional_locks=False,
    )
    if not branch.ok or not branch.stdout.strip():
        errors.append("delivery evidence: detached or unknown current branch")
    elif branch.stdout.strip() in PROTECTED_BRANCHES:
        errors.append(
            f"delivery evidence: protected branch {branch.stdout.strip()} cannot be committed"
        )

    status = git_safe(
        "status", "--porcelain=v1", "--untracked-files=normal",
        cwd=project_root,
        optional_locks=False,
    )
    if not status.ok:
        errors.append("delivery evidence: cannot prove a clean git worktree")
    elif status.stdout.strip():
        errors.append("delivery evidence: git worktree is not clean")

    subject = git_safe(
        "show", "-s", "--format=%s", head_oid,
        cwd=project_root,
        optional_locks=False,
    )
    if not subject.ok:
        errors.append("delivery evidence: cannot read the committed subject")
    else:
        actual_subject = subject.stdout.rstrip("\r\n")
        if fields["commit-subject"] != actual_subject:
            errors.append(
                "delivery evidence: commit-subject does not match current HEAD"
            )
        if not CONVENTIONAL_COMMIT_RE.fullmatch(actual_subject):
            errors.append(
                "delivery evidence: current HEAD subject is not a Conventional Commit"
            )
    return errors


def _profile_pr_target(profile: dict[str, Any] | None) -> str | None:
    pr = profile.get("pr") if isinstance(profile, dict) else None
    target = pr.get("target_branch") if isinstance(pr, dict) else None
    return target.strip() if isinstance(target, str) and target.strip() else None


def _missing_push_pr_evidence(
    project_root: Path,
    text: str,
    *,
    target_branch: str,
) -> list[str]:
    fields, errors = _delivery_fields(
        text,
        ("remote", "branch", "remote-oid", "pr-url", "pr-base"),
    )
    if errors:
        return errors
    if fields["pr-base"] != target_branch:
        errors.append(
            f"delivery evidence: pr-base must match profile target {target_branch}"
        )
    if not re.fullmatch(r"https://[^\s]+", fields["pr-url"]):
        errors.append("delivery evidence: pr-url must be an HTTPS URL")

    head = git_safe(
        "rev-parse", "--verify", "HEAD^{commit}",
        cwd=project_root,
        optional_locks=False,
    )
    branch = git_safe(
        "symbolic-ref", "--quiet", "--short", "HEAD",
        cwd=project_root,
        optional_locks=False,
    )
    if not head.ok or not head.stdout.strip():
        errors.append("delivery evidence: cannot prove current git HEAD")
    if not branch.ok or not branch.stdout.strip():
        errors.append("delivery evidence: detached or unknown current branch")
    if errors:
        return errors

    head_oid = head.stdout.strip().lower()
    branch_name = branch.stdout.strip()
    if fields["branch"] != branch_name:
        errors.append("delivery evidence: branch does not match the current branch")
    if fields["remote-oid"].lower() != head_oid:
        errors.append("delivery evidence: remote-oid does not match local HEAD")

    remotes = git_safe("remote", cwd=project_root, optional_locks=False)
    if not remotes.ok or fields["remote"] not in remotes.stdout.splitlines():
        errors.append("delivery evidence: named git remote is unavailable")
    else:
        remote_ref = f"refs/heads/{branch_name}"
        remote = git_safe(
            "ls-remote", "--heads", fields["remote"], remote_ref,
            cwd=project_root,
            timeout_s=30,
            optional_locks=False,
        )
        rows = [line.split() for line in remote.stdout.splitlines()] if remote.ok else []
        matching = [
            row[0].lower()
            for row in rows
            if len(row) == 2 and row[1] == remote_ref
        ]
        if len(matching) != 1:
            errors.append("delivery evidence: cannot prove the pushed remote branch OID")
        elif matching[0] != head_oid or matching[0] != fields["remote-oid"].lower():
            errors.append(
                "delivery evidence: local HEAD, remote-oid, and pushed branch OID differ"
            )

    pr = run_safe_command(
        (
            "gh", "pr", "view", fields["pr-url"], "--json",
            "url,baseRefName,headRefName,headRefOid",
        ),
        cwd=project_root,
        env=sanitized_worker_env(),
        timeout_s=30,
    )
    if not pr.ok:
        errors.append("delivery evidence: gh cannot prove the pull request")
        return errors
    try:
        payload = json.loads(pr.stdout)
    except json.JSONDecodeError:
        errors.append("delivery evidence: gh returned invalid pull request evidence")
        return errors
    if not isinstance(payload, dict):
        return [*errors, "delivery evidence: gh returned invalid pull request evidence"]

    actual_url = payload.get("url")
    if (
        not isinstance(actual_url, str)
        or not actual_url.startswith("https://")
        or actual_url.rstrip("/") != fields["pr-url"].rstrip("/")
    ):
        errors.append("delivery evidence: pr-url does not match the live pull request")
    if payload.get("baseRefName") != target_branch:
        errors.append(
            f"delivery evidence: live pull request base is not {target_branch}"
        )
    if payload.get("headRefName") != branch_name:
        errors.append("delivery evidence: live pull request head branch differs")
    gh_head_oid = payload.get("headRefOid")
    if (
        not isinstance(gh_head_oid, str)
        or gh_head_oid.lower() != head_oid
        or gh_head_oid.lower() != fields["remote-oid"].lower()
    ):
        errors.append(
            "delivery evidence: local HEAD, remote-oid, and PR headRefOid differ"
        )
    return errors
