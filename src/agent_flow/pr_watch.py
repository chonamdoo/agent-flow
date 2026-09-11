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
import hashlib
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from agent_flow.artifact import ACTIVE_LOCK
from agent_flow.core.worktree_isolation import (
    WorktreeIsolationError, exclusive_file_lease, validate_run_artifact_target,
    write_run_artifact_text,
)


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
    repo: str = ""
    head: str = ""
    feedback_ids: list[str] = field(default_factory=list)
    ci_checks: dict[str, str] | None = None
    ci_revisions: dict[str, dict[str, str]] = field(default_factory=dict)

    def to_summary(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "state": self.state,
            "status": self.status,
            "repo": self.repo,
            "head": self.head,
            "feedback_ids": self.feedback_ids,
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
                    "feedback_id": c.get("feedback_id"),
                    "url": c.get("url"),
                    "author": (c.get("author") or {}).get("login"),
                    "state": c.get("state"),
                    "body": _truncate(c.get("body", ""), 200),
                }
                for c in self.review_comments
            ],
            "issue_comments_count": len(self.issue_comments),
            "issue_comments": [
                {
                    "feedback_id": c.get("feedback_id"),
                    "body": _truncate(c.get("body", ""), 200),
                    "url": c.get("url"),
                }
                for c in self.issue_comments
            ],
            "error": self.error,
        }


def fetch_pr(
    number: int,
    repo: str | None = None,
    *,
    required_checks: tuple[str, ...] = (),
    run_dir: Path | None = None,
) -> PRSnapshot:
    """Query and classify a PR, publishing feedback for ACK when run_dir is set.

    Query or feedback-storage failures return an explicit error snapshot.
    Publishing waits for the run lease; success is never returned before storage.
    """
    repository = repo or ""
    try:
        data = _fetch_pr_data(number, repo)
        url = urlsplit(str(data.get("url", "")))
        match = re.fullmatch(r"/([^/]+/[^/]+)/pull/(\d+)/?", url.path)
        if (
            url.scheme not in {"http", "https"} or not url.netloc
            or url.username is not None or url.password is not None
            or match is None or int(match.group(2)) != number
        ):
            raise ValueError("cannot resolve PR repository identity")
        repository = _normalize_repository(f"{url.netloc}/{match.group(1)}")
        if data.get("state") not in {"MERGED", "CLOSED"}:
            data["reviewThreads"] = _fetch_review_threads(number, repository)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return PRSnapshot(
            number, "", "UNKNOWN", "error", error=f"cannot observe PR: {exc}", repo=repository,
        )
    try:
        handled = _read_feedback_state(run_dir, repository, number).get("handled", [])
        snapshot = _classify(
            number, data, required_checks=required_checks, repo=repository,
            handled_ids=handled,
        )
        if run_dir is not None and snapshot.status != "error":
            _record_feedback_observation(run_dir, snapshot)
        return snapshot
    except (OSError, ValueError, WorktreeIsolationError) as exc:
        return PRSnapshot(
            number, "", "UNKNOWN", "error",
            error=f"cannot record PR feedback: {exc}", repo=repository,
        )


def watch_pr(
    number: int,
    repo: str | None = None,
    project_root: Path | None = None,
    poll_interval_s: int = 30,
    max_poll_count: int = 120,
    max_interval_s: int = 300,
    *,
    required_checks: tuple[str, ...] = (),
    run_dir: Path | None = None,
) -> PRSnapshot:
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
    last = PRSnapshot(
        number, "", "UNKNOWN", "error",
        error="watch requires at least one poll", repo=repo or "",
    )
    interval = float(poll_interval_s)
    max_loop_seconds = poll_interval_s * max_poll_count  # rough budget
    elapsed = 0.0

    for i in range(max_poll_count):
        snap = fetch_pr(number, repo, required_checks=required_checks, run_dir=run_dir)
        last = snap
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
    repo: str = "",
    handled_ids: list[str] | tuple[str, ...] = (),
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
    identity: dict[str, Any] = {"repo": repo, "head": str(data.get("headRefOid", ""))}

    if state == "MERGED":
        return PRSnapshot(
            number=number, title=title, state=state, status="merged", **identity,
        )
    if state == "CLOSED":
        return PRSnapshot(
            number=number, title=title, state=state, status="closed", **identity,
        )

    rollup = data.get("statusCheckRollup")
    if rollup is not None and (
        not isinstance(rollup, list) or any(not isinstance(check, dict) for check in rollup)
    ):
        return PRSnapshot(
            number, title, state, "error", error="malformed CI check observation", **identity,
        )
    observed_checks = rollup is not None
    rollup = rollup or []
    # GitHub returns Actions/custom check runs and external commit statuses.
    # Any nonterminal result is pending; explicit failure signals win.
    def _is_failure(check: dict[str, Any]) -> bool:
        return (
            check.get("conclusion") in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE")
            or check.get("status") == "FAILURE"
            or check.get("state") in ("FAILURE", "ERROR")
        )

    def _is_pending(check: dict[str, Any]) -> bool:
        if check.get("conclusion") not in (
            None, "", "SUCCESS", "FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED",
            "STARTUP_FAILURE", "NEUTRAL", "SKIPPED", "STALE",
        ):
            return True
        if check.get("status") in ("IN_PROGRESS", "PENDING", "QUEUED"):
            return check.get("conclusion") in (None, "")
        if check.get("state") in ("PENDING", "EXPECTED"):
            return True
        return (
            check.get("conclusion") in (None, "")
            and check.get("state") in (None, "")
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
    ci_checks: dict[str, str] = {}
    ci_revisions: dict[str, dict[str, str]] = {}
    for check in rollup:
        name = check.get("name") or check.get("context")
        workflow = check.get("workflowName")
        if workflow is None:
            workflow = ""
        if not isinstance(name, str) or not name.strip() or not isinstance(workflow, str):
            return PRSnapshot(
                number, title, state, "error", error="CI check identity is missing", **identity,
            )
        check_id = json.dumps(
            ["check" if check.get("name") else "status", workflow, name],
            ensure_ascii=False, separators=(",", ":"),
        )
        outcome = (
            "failed" if check in failed else
            "success" if check.get("conclusion") == "SUCCESS" or check.get("state") == "SUCCESS"
            else "pending"
        )
        if check_id in ci_checks:
            return PRSnapshot(
                number, title, state, "error",
                error=f"ambiguous duplicate CI check identity: {check_id}", **identity,
            )
        ci_checks[check_id] = outcome
        ci_revisions[check_id] = {
            key: str(value) for key in (
                "id", "databaseId", "detailsUrl", "targetUrl", "startedAt", "completedAt", "createdAt",
            )
            if isinstance(value := check.get(key), (str, int)) and not isinstance(value, bool)
            and value and not str(value).startswith("0001-")
        }
    identity["ci_checks"] = ci_checks if observed_checks else None
    identity["ci_revisions"] = ci_revisions

    reviews = data.get("reviews") or []
    decisions: dict[str, dict[str, Any]] = {}
    review_comments = []
    ordered = sorted(
        (review for review in reviews if isinstance(review, dict)),
        key=lambda item: str(item.get("submittedAt", "")),
    )
    for index, review in enumerate(ordered):
        review_state = review.get("state")
        if review_state == "COMMENTED":
            if str(review.get("body") or "").strip():
                review_comments.append(dict(review, feedback_id=_feedback_id("review", review)))
        elif review_state in {"CHANGES_REQUESTED", "APPROVED", "DISMISSED"}:
            author = (review.get("author") or {}).get("login") or str(index)
            decisions[author] = review
    review_comments.extend(
        dict(review, feedback_id=_feedback_id("review", review))
        for review in decisions.values()
        if review.get("state") == "CHANGES_REQUESTED"
        and data.get("reviewDecision") != "APPROVED"
    )
    for thread in data.get("reviewThreads") or []:
        if not thread.get("isResolved"):
            comments = (thread.get("comments") or {}).get("nodes") or []
            comment = comments[-1] if comments else {}
            review_comments.append(
                dict(comment, feedback_id=_feedback_id("thread", {
                    "thread": thread["id"],
                    "comments": [
                        {key: item.get(key) for key in ("id", "body", "updatedAt")}
                        for item in comments
                    ],
                }))
            )
    review_comments = [
        comment for comment in review_comments if comment["feedback_id"] not in handled_ids
    ]
    # Bot summaries and CI reporters are not actionable review feedback.
    issue_comments = []
    for comment in data.get("comments") or []:
        if not isinstance(comment, dict) or _is_bot_comment(comment):
            continue
        feedback_id = _feedback_id("comment", comment)
        if feedback_id not in handled_ids:
            issue_comments.append(dict(comment, feedback_id=feedback_id))
    identity["feedback_ids"] = [
        comment["feedback_id"] for comment in (*review_comments, *issue_comments)
    ]

    if failed:
        return PRSnapshot(
            number=number,
            title=title,
            state=state,
            status="ci_failed",
            **identity,
            failed_checks=failed,
            pending_checks=pending,
            review_comments=review_comments,
            issue_comments=issue_comments,
        )
    if review_comments or issue_comments:
        return PRSnapshot(
            number=number,
            title=title,
            state=state,
            status="has_comments",
            **identity,
            review_comments=review_comments,
            pending_checks=pending,
            issue_comments=issue_comments,
        )
    if pending:
        return PRSnapshot(
            number=number,
            title=title,
            state=state,
            status="pending",
            **identity,
            pending_checks=pending,
        )
    return PRSnapshot(
        number=number, title=title, state=state, status="green", **identity,
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
    # `workflowName`은 신원이 아니다. 한 workflow에는 여러 job이 있어서, 그것을
    # 인정하면 이름이 같은 workflow 안의 아무 green job 하나가 선언된 gate를
    # 충족시킨다 — gate가 요구한 job이 skip이어도 green으로 보고된다. 표시용
    # 이름은 `to_summary`가 따로 fallback하므로(:59, :68) pending 분류는 그대로다.
    names = [
        " ".join(value.casefold().split())
        for key in ("name", "context")
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


def _feedback_id(kind: str, item: dict[str, Any]) -> str:
    revision = {
        key: item.get(key) for key in ("id", "thread", "body", "updatedAt", "submittedAt", "state")
    }
    if kind == "thread":
        revision["comments"] = item.get("comments")
    content = json.dumps(revision, sort_keys=True, separators=(",", ":"))
    return f"{kind}:{hashlib.sha256(content.encode()).hexdigest()}"


def _fetch_pr_data(number: int, repo: str | None) -> dict[str, Any]:
    cmd = [
        "gh", "pr", "view", str(number), "--json",
        "number,title,state,url,headRefOid,reviewDecision,statusCheckRollup,reviews,comments",
    ]
    if repo:
        cmd += ["--repo", repo]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, check=False,
        )
    except FileNotFoundError as exc:
        raise ValueError("gh CLI not found on PATH. Install: https://cli.github.com/") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("gh pr view timed out after 30s") from exc
    if result.returncode:
        raise ValueError(f"gh pr view failed: {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"gh output not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("gh PR output is not an object")
    return data


def _normalize_repository(repo: str) -> str:
    parts = repo.split("/")
    if len(parts) == 2:
        parts.insert(0, "github.com")
    if len(parts) != 3 or any(
        not part or any(char.isspace() or char in "\\?#@" for char in part)
        for part in parts
    ):
        raise ValueError("repository must be [HOST/]OWNER/REPO")
    return "/".join(parts).lower()


def _fetch_graphql_pages(query: str, host: str, fields: list[str]) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["gh", "api", "graphql", "--hostname", host, "--paginate", "--slurp",
         "-f", f"query={query}", *fields],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
    )
    if result.returncode:
        raise ValueError(f"cannot fetch PR review threads: {result.stderr.strip()}")
    pages = json.loads(result.stdout)
    if not isinstance(pages, list) or not pages:
        raise ValueError("PR review threads response is incomplete")
    if any(not isinstance(page, dict) or page.get("errors") for page in pages):
        raise ValueError("PR review threads query failed")
    return pages


def _connection_nodes(connection: Any, *, has_next: bool | None = None) -> list[dict[str, Any]]:
    if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
        raise ValueError("PR review connection is incomplete")
    page_info = connection.get("pageInfo")
    if not isinstance(page_info, dict) or type(page_info.get("hasNextPage")) is not bool:
        raise ValueError("PR review pagination is incomplete")
    if has_next is not None and page_info["hasNextPage"] is not has_next:
        raise ValueError("PR review pagination is incomplete")
    if page_info["hasNextPage"] and (
        not isinstance(page_info.get("endCursor"), str) or not page_info["endCursor"]
    ):
        raise ValueError("PR review pagination cursor is missing")
    nodes = connection["nodes"]
    if any(
        not isinstance(node, dict) or not isinstance(node.get("id"), str) or not node["id"]
        for node in nodes
    ):
        raise ValueError("PR review node identity is malformed")
    return nodes


def _fetch_thread_comments(thread: dict[str, Any], host: str) -> list[dict[str, Any]]:
    initial = thread.get("comments")
    if not isinstance(initial, dict):
        raise ValueError("PR review thread comments are incomplete")
    initial = dict(initial, pageInfo=initial.get("commentPageInfo"))
    _connection_nodes(initial)
    connections = [initial]
    if initial["pageInfo"]["hasNextPage"]:
        query = """query($thread:ID!,$endCursor:String){
          node(id:$thread){... on PullRequestReviewThread{
            id comments(first:100,after:$endCursor){
              totalCount nodes{id body updatedAt url author{login}}
              pageInfo{hasNextPage endCursor}
            }
          }}
        }"""
        pages = _fetch_graphql_pages(query, host, [
            "-f", f"thread={thread['id']}", "-f", f"endCursor={initial['pageInfo']['endCursor']}",
        ])
        for page in pages:
            try:
                node = page["data"]["node"]
                if node["id"] != thread["id"]:
                    raise ValueError("PR review thread identity changed during pagination")
                connections.append(node["comments"])
            except (KeyError, TypeError) as exc:
                raise ValueError("PR review thread comments are incomplete") from exc
    comments: list[dict[str, Any]] = []
    expected_count = initial.get("totalCount")
    if type(expected_count) is not int or expected_count < 0:
        raise ValueError("PR review comment count is missing")
    cursors: set[str] = set()
    for index, connection in enumerate(connections):
        nodes = _connection_nodes(connection, has_next=index < len(connections) - 1)
        if connection.get("totalCount") != expected_count:
            raise ValueError("PR review comment count changed during pagination")
        if any(not isinstance(comment.get("body"), str) for comment in nodes):
            raise ValueError("PR review comment body is malformed")
        if connection["pageInfo"]["hasNextPage"]:
            cursor = connection["pageInfo"]["endCursor"]
            if cursor in cursors:
                raise ValueError("PR review comment pagination repeated a cursor")
            cursors.add(cursor)
        comments.extend(nodes)
    if len(comments) != expected_count or len({comment["id"] for comment in comments}) != expected_count:
        raise ValueError("PR review comment pagination is incomplete")
    return comments


def _fetch_review_threads(number: int, repo: str) -> list[dict[str, Any]]:
    host, owner, name = _normalize_repository(repo).split("/")
    query = """query($owner:String!,$name:String!,$number:Int!,$endCursor:String){
      repository(owner:$owner,name:$name){pullRequest(number:$number){
        reviewThreads(first:100,after:$endCursor){
          nodes{id isResolved comments(first:100){
            totalCount nodes{id body updatedAt url author{login}}
            commentPageInfo:pageInfo{hasNextPage endCursor}
          }}
          pageInfo{hasNextPage endCursor}
        }
      }}
    }"""
    # gh paginates the first pageInfo it finds; nested comment cursors must be aliased.
    pages = _fetch_graphql_pages(query, host, [
        "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={number}",
    ])
    threads: list[dict[str, Any]] = []
    thread_ids: set[str] = set()
    cursors: set[str] = set()
    for index, page in enumerate(pages):
        try:
            connection = page["data"]["repository"]["pullRequest"]["reviewThreads"]
            nodes = _connection_nodes(connection, has_next=index < len(pages) - 1)
        except (KeyError, TypeError) as exc:
            raise ValueError("PR review threads response is incomplete") from exc
        if any(
            not isinstance(thread, dict)
            or not isinstance(thread.get("id"), str)
            or type(thread.get("isResolved")) is not bool
            for thread in nodes
        ):
            raise ValueError("PR review thread identity or resolution state is malformed")
        if connection["pageInfo"]["hasNextPage"]:
            cursor = connection["pageInfo"]["endCursor"]
            if cursor in cursors:
                raise ValueError("PR review threads pagination repeated a cursor")
            cursors.add(cursor)
        for thread in nodes:
            if thread["id"] in thread_ids:
                raise ValueError("PR review threads pagination repeated a thread")
            thread_ids.add(thread["id"])
            if not thread["isResolved"]:
                thread["comments"]["nodes"] = _fetch_thread_comments(thread, host)
            threads.append(thread)
    return threads


def _read_feedback_state(run_dir: Path | None, repo: str, number: int) -> dict[str, Any]:
    if run_dir is None:
        return {}
    repo = _normalize_repository(repo)
    _, path = validate_run_artifact_target(run_dir, run_dir.resolve() / "pr-feedback.json")
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("PR feedback state is not an object")
    sequence = payload.get("observation_sequence", 0)
    if type(sequence) is not int or sequence < 0:
        raise ValueError("PR feedback observation sequence is malformed")
    if payload.get("repo") != repo or payload.get("number") != number:
        if _ci_execution_history(payload, sequence):
            raise ValueError("PR CI execution history belongs to a different PR; use a separate run")
        return {}
    if not isinstance(payload.get("handled"), list) or not all(
        isinstance(item, str) for item in payload["handled"]
    ):
        raise ValueError("PR feedback acknowledgements are malformed")
    return payload


def _valid_ci_revision(revision: object) -> bool:
    return isinstance(revision, dict) and all(
        key in {"id", "databaseId", "detailsUrl", "targetUrl", "startedAt", "completedAt", "createdAt"}
        and isinstance(value, str) and bool(value)
        for key, value in revision.items()
    )


def _ci_execution_history(observation: dict[str, Any], sequence: int) -> dict[str, Any]:
    history = observation.get("ci_executions", {} if sequence == 0 else None)
    if not isinstance(history, dict):
        raise ValueError("PR CI execution history is missing or malformed")
    for head, checks in history.items():
        if not isinstance(head, str) or not head or not isinstance(checks, dict):
            raise ValueError("PR CI execution history is malformed")
        for check, entries in checks.items():
            if not isinstance(check, str) or not check or not isinstance(entries, list):
                raise ValueError("PR CI execution history is malformed")
            revisions = set()
            for entry in entries:
                if (
                    not isinstance(entry, dict) or not _valid_ci_revision(entry.get("revision"))
                    or type(entry.get("observation_sequence")) is not int
                    or not 0 < entry["observation_sequence"] <= sequence
                    or not isinstance(entry.get("outcome"), str)
                    or entry["outcome"] not in {"failed", "success"}
                ):
                    raise ValueError("PR CI execution history is malformed")
                revision = frozenset(entry["revision"].items())
                if revision in revisions:
                    raise ValueError("PR CI execution has multiple first observations")
                revisions.add(revision)
    return history


def _ci_execution_entry(
    history: dict[str, Any], head: str, check: str, revision: dict[str, str],
) -> dict[str, Any] | None:
    return next((
        entry for entry in history.get(head, {}).get(check, ())
        if entry["revision"] == revision
    ), None)


def _record_feedback_observation(run_dir: Path, snapshot: PRSnapshot) -> None:
    repository = _normalize_repository(snapshot.repo)
    with exclusive_file_lease(run_dir.parent / ACTIVE_LOCK, wait=True):
        previous = _read_feedback_state(run_dir, repository, snapshot.number)
        sequence = previous.get("observation_sequence", 0)
        executions = _ci_execution_history(previous, sequence)
        for check, outcome in (snapshot.ci_checks or {}).items():
            if outcome not in {"failed", "success"}:
                continue
            revision = snapshot.ci_revisions.get(check, {})
            entry = _ci_execution_entry(executions, snapshot.head, check, revision)
            if entry is None:
                entry = {"observation_sequence": sequence + 1, "revision": revision, "outcome": outcome}
                executions.setdefault(snapshot.head, {}).setdefault(check, []).append(entry)
        write_run_artifact_text(
            run_dir, run_dir.resolve() / "pr-feedback.json",
            json.dumps({
                "repo": repository, "number": snapshot.number, "head": snapshot.head,
                "feedback_ids": snapshot.feedback_ids, "handled": previous.get("handled", []),
                "status": snapshot.status, "ci_checks": snapshot.ci_checks,
                "ci_revisions": snapshot.ci_revisions,
                "ci_executions": executions,
                "observation_sequence": sequence + 1,
            }),
        )


def acknowledge_pr_feedback(
    run_dir: Path, *, repo: str, number: int, head: str, feedback_ids: list[str]
) -> None:
    repo = _normalize_repository(repo)
    with exclusive_file_lease(run_dir.parent / ACTIVE_LOCK):
        state = _read_feedback_state(run_dir, repo, number)
        if (
            not state or state.get("head") != head
            or not feedback_ids or not set(feedback_ids).issubset(state.get("feedback_ids", []))
        ):
            raise ValueError("feedback acknowledgement does not match the observed PR, head, and feedback")
        state["handled"] = sorted(set(state["handled"]) | set(feedback_ids))
        write_run_artifact_text(
            run_dir, run_dir.resolve() / "pr-feedback.json", json.dumps(state),
        )
