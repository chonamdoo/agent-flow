"""PR-watch — polling and status classification via the `gh` CLI.

Wraps `gh pr view <num> --json ...` to fetch the PR snapshot and classify
into one of:

  green        — all checks succeeded, no unresolved review comments
  has_comments — open / changes-requested review comments present
  ci_failed    — at least one check rolled up to FAILURE
  pending      — checks still running; no failure yet
  merged       — PR.state == MERGED
  closed       — PR.state == CLOSED (not merged)

Design choices:

  - We use the `gh` CLI (not the GitHub REST/GraphQL API directly) so the
    user inherits whatever auth they already have configured for `gh`.
    No agent-flow-side token management.
  - Polling is the simplest model. WebSockets / webhooks would be cleaner
    but require running infrastructure; polling fits a CLI tool.
  - Each phase that consumes pr-watch output reads a single artifact.
    Watch loop terminates as soon as the status leaves `pending`.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


PRStatus = Literal[
    "green", "has_comments", "ci_failed", "pending", "merged", "closed", "error",
]


@dataclass
class PRSnapshot:
    number: int
    title: str
    state: str  # OPEN / MERGED / CLOSED
    status: PRStatus
    failed_checks: list[dict[str, Any]] = field(default_factory=list)
    pending_checks: list[dict[str, Any]] = field(default_factory=list)
    review_comments: list[dict[str, Any]] = field(default_factory=list)
    issue_comments: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def to_summary(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "state": self.state,
            "status": self.status,
            "failed_checks": [
                {
                    "name": c.get("name") or c.get("workflowName"),
                    "conclusion": c.get("conclusion"),
                    "url": c.get("detailsUrl") or c.get("targetUrl"),
                }
                for c in self.failed_checks
            ],
            "pending_checks_count": len(self.pending_checks),
            "pending_checks": [
                {
                    "name": c.get("name") or c.get("workflowName"),
                    "status": c.get("status") or c.get("state"),
                    "conclusion": c.get("conclusion"),
                    "url": c.get("detailsUrl") or c.get("targetUrl"),
                }
                for c in self.pending_checks
            ],
            "review_comments": [
                {
                    "author": c.get("author", {}).get("login"),
                    "state": c.get("state"),
                    "body": _truncate(c.get("body", ""), 200),
                }
                for c in self.review_comments
            ],
            "issue_comments_count": len(self.issue_comments),
            "error": self.error,
        }


def fetch_pr(
    number: int,
    repo: str | None = None,
    *,
    required_checks: tuple[str, ...] = (),
) -> PRSnapshot | None:
    """Fetch a single PR snapshot via `gh pr view --json`.

    Returns None if the gh subprocess fails (gh not installed, auth missing,
    PR not found).
    """
    cmd = [
        "gh", "pr", "view", str(number),
        "--json",
        "number,title,state,statusCheckRollup,reviews,comments",
    ]
    if repo:
        cmd += ["--repo", repo]
    try:
        # 디코드를 로케일에 맡기면 비 UTF-8 로케일에서 `gh` 출력이
        # UnicodeDecodeError를 낸다. 그건 아래 두 handler 어느 쪽도 아니라
        # 호출부까지 그대로 올라간다.
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        return PRSnapshot(
            number=number, title="", state="UNKNOWN", status="error",
            error="gh CLI not found on PATH. Install: https://cli.github.com/",
        )
    except subprocess.TimeoutExpired:
        return PRSnapshot(
            number=number, title="", state="UNKNOWN", status="error",
            error="gh pr view timed out after 30s",
        )
    if result.returncode != 0:
        return PRSnapshot(
            number=number, title="", state="UNKNOWN", status="error",
            error=f"gh pr view failed: {result.stderr.strip()}",
        )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        return PRSnapshot(
            number=number, title="", state="UNKNOWN", status="error",
            error=f"gh output not JSON: {e}",
        )
    return _classify(number, data, required_checks=required_checks)


def watch_pr(
    number: int,
    repo: str | None = None,
    project_root: Path | None = None,
    poll_interval_s: int = 30,
    max_poll_count: int = 120,
    max_interval_s: int = 300,
    *,
    required_checks: tuple[str, ...] = (),
) -> PRSnapshot | None:
    """Poll a PR until status leaves `pending` or max_poll_count exceeded.

    Backoff: each failed/pending poll grows the interval by 1.5×, capped at
    `max_interval_s` (default 5 min). Plus ±20% jitter to avoid thundering
    herds from multiple concurrent watchers. Reduces GitHub API pressure
    when 10 users x 10 PRs are watching simultaneously.

    Progress messages go to stderr so the polling output doesn't pollute
    `agent-flow pr-watch 123 > status.json` redirects. Final snapshot is
    the function return — caller prints it to stdout if needed.
    """
    import random
    last: PRSnapshot | None = None
    interval = float(poll_interval_s)
    max_loop_seconds = poll_interval_s * max_poll_count  # rough budget
    elapsed = 0.0

    for i in range(max_poll_count):
        snap = fetch_pr(number, repo, required_checks=required_checks)
        last = snap
        if snap is None:
            time.sleep(interval)
            elapsed += interval
            continue
        if snap.status == "error":
            print(f"  PR #{number}: error — {snap.error}", file=sys.stderr)
            return snap
        if snap.status != "pending":
            return snap
        # pending → wait with backoff + jitter
        jitter = random.uniform(-0.2, 0.2) * interval
        sleep_for = max(5.0, interval + jitter)
        print(
            f"  PR #{number}: pending "
            f"({len(snap.pending_checks)} running, poll {i+1}/{max_poll_count}, "
            f"next in {sleep_for:.0f}s)...",
            file=sys.stderr,
        )
        time.sleep(sleep_for)
        elapsed += sleep_for
        interval = min(interval * 1.5, float(max_interval_s))
        if elapsed > max_loop_seconds * 1.5:
            # Hard ceiling so backoff can't extend forever
            break
    return last


# ─────────────────────────── helpers ────────────────────────────────


def _classify(
    number: int,
    data: dict[str, Any],
    *,
    required_checks: tuple[str, ...] = (),
) -> PRSnapshot:
    if (
        not isinstance(required_checks, tuple)
        or not all(
            isinstance(check_name, str) and check_name.strip()
            for check_name in required_checks
        )
    ):
        raise TypeError("required_checks must be a tuple of non-empty check names")
    state = str(data.get("state", "UNKNOWN"))
    title = str(data.get("title", ""))

    if state == "MERGED":
        return PRSnapshot(
            number=number, title=title, state=state, status="merged",
        )
    if state == "CLOSED":
        return PRSnapshot(
            number=number, title=title, state=state, status="closed",
        )

    rollup = data.get("statusCheckRollup") or []
    # GitHub returns Actions/custom check runs and external commit statuses.
    # Any nonterminal result is pending; explicit failure signals win.
    def _is_failure(check: dict[str, Any]) -> bool:
        return (
            check.get("conclusion") in ("FAILURE", "TIMED_OUT", "CANCELLED")
            or check.get("status") == "FAILURE"
            or check.get("state") in ("FAILURE", "ERROR")
        )

    def _is_pending(check: dict[str, Any]) -> bool:
        if check.get("status") in ("IN_PROGRESS", "PENDING", "QUEUED"):
            return check.get("conclusion") in (None, "")
        if check.get("state") in ("PENDING", "EXPECTED"):
            return True
        return (
            check.get("conclusion") in (None, "")
            and check.get("state") in (None, "")
            and check.get("status") not in ("COMPLETED",)
        )

    failed = [
        check
        for check in rollup
        if isinstance(check, dict) and _is_failure(check)
    ]
    pending = [
        check
        for check in rollup
        if isinstance(check, dict) and _is_pending(check)
    ]
    for check_name in required_checks:
        matching = [
            check
            for check in rollup
            if isinstance(check, dict)
            and _check_name_matches(check, check_name)
        ]
        if any(_check_matches_gate(check, check_name) for check in matching):
            continue
        if any(_is_pending(check) for check in matching):
            continue
        if matching:
            failed.extend(
                check for check in matching if check not in failed
            )
            continue
        pending.append(
            {
                "name": f"deferred CI gate: {check_name}",
                "status": "EXPECTED",
                "conclusion": None,
            }
        )

    reviews = data.get("reviews") or []
    review_comments = [
        review
        for review in reviews
        if isinstance(review, dict)
        and review.get("state") in ("COMMENTED", "CHANGES_REQUESTED")
    ]
    # Bot summaries and CI reporters are not actionable review feedback.
    issue_comments = [
        comment
        for comment in (data.get("comments") or [])
        if isinstance(comment, dict) and not _is_bot_comment(comment)
    ]

    if failed:
        return PRSnapshot(
            number=number,
            title=title,
            state=state,
            status="ci_failed",
            failed_checks=failed,
            review_comments=review_comments,
            issue_comments=issue_comments,
        )
    if review_comments or issue_comments:
        return PRSnapshot(
            number=number,
            title=title,
            state=state,
            status="has_comments",
            review_comments=review_comments,
            issue_comments=issue_comments,
        )
    if pending:
        return PRSnapshot(
            number=number,
            title=title,
            state=state,
            status="pending",
            pending_checks=pending,
        )
    return PRSnapshot(
        number=number, title=title, state=state, status="green",
    )


def _check_matches_gate(check: dict[str, Any], check_name: str) -> bool:
    succeeded = (
        check.get("conclusion") == "SUCCESS"
        or check.get("state") == "SUCCESS"
    )
    return succeeded and _check_name_matches(check, check_name)


def _check_name_matches(check: dict[str, Any], check_name: str) -> bool:
    expected = " ".join(check_name.casefold().split())
    if not expected:
        return False
    names = [
        " ".join(value.casefold().split())
        for key in ("name", "context", "workflowName")
        if isinstance((value := check.get(key)), str)
    ]
    return any(name == expected for name in names)


def _is_bot_comment(comment: dict[str, Any]) -> bool:
    author = comment.get("author")
    if not isinstance(author, dict):
        return False
    if author.get("is_bot") or author.get("isBot"):
        return True
    login = str(author.get("login") or "")
    return login.endswith("[bot]") or login.endswith("-bot") or login in {
        "github-actions",
        "dependabot",
        "codecov",
        "coderabbitai",
        "coveralls",
        "sonarcloud",
        "vercel",
        "netlify",
    }


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"
