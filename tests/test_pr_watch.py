"""PR-watch tests — classification logic against fixture JSON.

The `gh pr view --json ...` shape is well-defined; we test the classifier
against synthetic snapshots covering each branch.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


KIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT_ROOT / "src"))

from agent_flow.pr_watch import _classify  # noqa: E402


def test_merged():
    snap = _classify(1, {"state": "MERGED", "title": "x"})
    assert snap.status == "merged"


def test_closed_not_merged():
    snap = _classify(1, {"state": "CLOSED", "title": "x"})
    assert snap.status == "closed"


def test_green():
    snap = _classify(1, {
        "state": "OPEN", "title": "x",
        "statusCheckRollup": [
            {"name": "build", "conclusion": "SUCCESS", "status": "COMPLETED"},
            {"name": "test", "conclusion": "SUCCESS", "status": "COMPLETED"},
        ],
        "reviews": [],
        "comments": [],
    })
    assert snap.status == "green"


@pytest.mark.parametrize(("checks", "decision", "expected"), [
    (None, "APPROVED", "pending"),
    ([], "APPROVED", "pending"),
    ([{"name": "unit", "conclusion": "SUCCESS"}], None, "pending"),
    ([{"name": "unit", "conclusion": "SUCCESS"}], "REVIEW_REQUIRED", "pending"),
    ([{"name": "unit", "conclusion": "SUCCESS"}], "CHANGES_REQUESTED", "has_comments"),
    ([{"name": "unit", "conclusion": "SUCCESS"}], "APPROVED", "green"),
])
def test_fetch_pr_requires_checks_and_approval_only_when_opted_in(
    monkeypatch, checks, decision, expected,
):
    from agent_flow.pr_watch import fetch_pr

    data = {
        "state": "OPEN", "url": "https://github.com/owner/repo/pull/7",
        "statusCheckRollup": checks, "reviewDecision": decision,
    }
    monkeypatch.setattr("agent_flow.pr_watch._fetch_pr_data", lambda *args: data)
    monkeypatch.setattr("agent_flow.pr_watch._fetch_review_threads", lambda *args: [])

    assert fetch_pr(7).status == "green"
    snapshot = fetch_pr(7, require_ready=True)
    assert snapshot.status == expected
    assert snapshot.pending_checks == []


@pytest.mark.parametrize(("conclusion", "decision", "comments", "required", "expected"), [
    ("FAILURE", "CHANGES_REQUESTED", [], (), "ci_failed"),
    ("NEUTRAL", "CHANGES_REQUESTED", [], ("unit",), "ci_failed"),
    (None, "CHANGES_REQUESTED", [], (), "has_comments"),
    (None, "REVIEW_REQUIRED", [{"id": "c1", "body": "please explain"}], (), "has_comments"),
    (None, "APPROVED", [], (), "pending"),
])
def test_require_ready_preserves_ci_failure_feedback_and_pending_precedence(
    conclusion, decision, comments, required, expected,
):
    snapshot = _classify(7, {
        "state": "OPEN", "reviewDecision": decision, "comments": comments,
        "statusCheckRollup": [{
            "name": "unit", "conclusion": conclusion,
            "status": "IN_PROGRESS" if conclusion is None else "COMPLETED",
        }],
    }, require_ready=True, required_checks=required)
    assert snapshot.status == expected


def test_watch_require_ready_waits_for_checks_then_approval(tmp_path, monkeypatch):
    from agent_flow.pr_watch import _read_feedback_state, watch_pr

    polls = iter([
        {"statusCheckRollup": [], "reviewDecision": "APPROVED"},
        {"statusCheckRollup": [{"name": "unit", "conclusion": "SUCCESS"}]},
        {
            "statusCheckRollup": [{"name": "unit", "conclusion": "SUCCESS"}],
            "reviewDecision": "APPROVED",
        },
    ])
    monkeypatch.setattr("agent_flow.pr_watch._fetch_pr_data", lambda *args: {
        "state": "OPEN", "url": "https://github.com/owner/repo/pull/7",
        "headRefOid": "head-1", **next(polls),
    })
    monkeypatch.setattr("agent_flow.pr_watch._fetch_review_threads", lambda *args: [])
    monkeypatch.setattr("agent_flow.pr_watch.time.sleep", lambda seconds: None)

    snapshot = watch_pr(7, require_ready=True, run_dir=tmp_path, max_poll_count=3)
    assert snapshot.status == "green"
    observed = _read_feedback_state(tmp_path, "owner/repo", 7)
    assert observed["observation_sequence"] == 3
    assert observed["status"] == "green"


@pytest.mark.parametrize("conclusion", ["NEUTRAL", "SKIPPED", "STALE"])
@pytest.mark.parametrize("pending", [
    {"status": "IN_PROGRESS"}, {"state": "PENDING"},
])
def test_pending_check_cannot_be_accepted_by_its_terminal_conclusion(conclusion, pending):
    snapshot = _classify(7, {
        "state": "OPEN", "reviewDecision": "APPROVED",
        "statusCheckRollup": [{"name": "unit", "conclusion": conclusion, **pending}],
    }, require_ready=True)
    assert snapshot.status == "pending"
    assert snapshot.ci_checks == {'["check","","unit"]': "pending"}


@pytest.mark.parametrize("conclusion", ["ACTION_REQUIRED", "STARTUP_FAILURE"])
def test_completed_failure_requires_ci_repair_instead_of_waiting(conclusion):
    snapshot = _classify(1, {
        "state": "OPEN", "statusCheckRollup": [
            {"name": "pytest", "status": "COMPLETED", "conclusion": conclusion},
        ],
    }, required_checks=("pytest",))
    assert snapshot.status == "ci_failed"
    assert [check["name"] for check in snapshot.failed_checks] == ["pytest"]
    assert not snapshot.pending_checks


def test_empty_checks_remain_pending_when_deferred_ci_is_required():
    snap = _classify(
        1,
        {
            "state": "OPEN",
            "title": "x",
            "statusCheckRollup": [],
            "reviews": [],
            "comments": [],
        },
        required_checks=("pytest",),
    )

    assert snap.status == "pending"
    assert snap.pending_checks == [
        {
            "name": "deferred CI gate: pytest",
            "status": "EXPECTED",
            "conclusion": None,
        }
    ]
    assert snap.to_summary()["pending_checks"][0]["name"] == (
        "deferred CI gate: pytest"
    )


def test_unrelated_green_check_does_not_satisfy_deferred_gate():
    snap = _classify(
        1,
        {
            "state": "OPEN",
            "title": "x",
            "statusCheckRollup": [
                {
                    "name": "parity",
                    "conclusion": "SUCCESS",
                    "status": "COMPLETED",
                }
            ],
            "reviews": [],
            "comments": [],
        },
        required_checks=("pytest",),
    )

    assert snap.status == "pending"
    assert snap.pending_checks[0]["name"] == "deferred CI gate: pytest"


@pytest.mark.parametrize(
    "check_name",
    ["pytest-old", "not-pytest", "py-test", "Py Test"],
)
def test_similar_green_check_does_not_satisfy_deferred_gate(check_name: str):
    snap = _classify(
        1,
        {
            "state": "OPEN",
            "title": "x",
            "statusCheckRollup": [
                {
                    "name": check_name,
                    "conclusion": "SUCCESS",
                    "status": "COMPLETED",
                }
            ],
            "reviews": [],
            "comments": [],
        },
        required_checks=("pytest",),
    )

    assert snap.status == "pending"
    assert snap.pending_checks[0]["name"] == "deferred CI gate: pytest"


def test_required_checks_rejects_bare_string():
    with pytest.raises(TypeError, match="must be a tuple"):
        _classify(
            1,
            {"state": "OPEN", "title": "x"},
            required_checks="pytest",
        )


def test_required_checks_rejects_non_string_elements():
    with pytest.raises(TypeError, match="non-empty check names"):
        _classify(
            1,
            {"state": "OPEN", "title": "x"},
            required_checks=(1, 2),
        )


def test_matching_green_check_satisfies_deferred_gate():
    snap = _classify(
        1,
        {
            "state": "OPEN",
            "title": "x",
            "statusCheckRollup": [
                {
                    "name": "pytest",
                    "conclusion": "SUCCESS",
                    "status": "COMPLETED",
                }
            ],
            "reviews": [],
            "comments": [],
        },
        required_checks=("pytest",),
    )

    assert snap.status == "green"


def test_workflow_name_does_not_satisfy_a_declared_job_gate():
    """반증: workflow 이름을 신원으로 인정하면 그 안의 아무 green job 하나가
    선언된 gate를 충족한다. 여기서 선언된 job(`pytest`)은 rollup에 아예 없고
    green인 것은 같은 workflow의 `lint`뿐이므로 판정은 pending이어야 한다."""
    snap = _classify(
        1,
        {
            "state": "OPEN",
            "title": "x",
            "statusCheckRollup": [
                {
                    "name": "lint",
                    "workflowName": "pytest",
                    "conclusion": "SUCCESS",
                    "status": "COMPLETED",
                }
            ],
            "reviews": [],
            "comments": [],
        },
        required_checks=("pytest",),
    )

    assert snap.status == "pending"
    assert snap.pending_checks[0]["name"] == "deferred CI gate: pytest"


@pytest.mark.parametrize("conclusion", ["SKIPPED", "NEUTRAL", "STALE"])
def test_terminal_non_successful_required_check_fails_ci(
    conclusion: str,
):
    snap = _classify(
        1,
        {
            "state": "OPEN",
            "title": "x",
            "statusCheckRollup": [
                {
                    "name": "pytest",
                    "conclusion": conclusion,
                    "status": "COMPLETED",
                }
            ],
            "reviews": [],
            "comments": [],
        },
        required_checks=("pytest",),
    )
    assert snap.status == "ci_failed"
    assert snap.failed_checks[0]["name"] == "pytest"



def test_running_required_check_is_not_duplicated():
    snap = _classify(
        1,
        {
            "state": "OPEN",
            "title": "x",
            "statusCheckRollup": [
                {
                    "name": "pytest",
                    "conclusion": None,
                    "status": "IN_PROGRESS",
                }
            ],
            "reviews": [],
            "comments": [],
        },
        required_checks=("pytest",),
    )

    assert snap.status == "pending"
    assert [check["name"] for check in snap.pending_checks] == ["pytest"]


def test_pending_no_failure():
    snap = _classify(1, {
        "state": "OPEN", "title": "x",
        "statusCheckRollup": [
            {"name": "build", "conclusion": "SUCCESS", "status": "COMPLETED"},
            {"name": "test", "conclusion": None, "status": "IN_PROGRESS"},
        ],
        "reviews": [],
        "comments": [],
    })
    assert snap.status == "pending"
    assert len(snap.pending_checks) == 1


def test_ci_failed_takes_priority_over_comments():
    snap = _classify(1, {
        "state": "OPEN", "title": "x",
        "statusCheckRollup": [
            {"name": "test", "conclusion": "FAILURE", "status": "COMPLETED"},
        ],
        "reviews": [{"state": "COMMENTED", "body": "nit"}],
        "comments": [],
    })
    assert snap.status == "ci_failed"
    assert len(snap.failed_checks) == 1


def test_has_comments_when_review_changes_requested():
    snap = _classify(1, {
        "state": "OPEN", "title": "x",
        "statusCheckRollup": [
            {"name": "test", "conclusion": "SUCCESS", "status": "COMPLETED"},
        ],
        "reviews": [
            {"state": "CHANGES_REQUESTED", "body": "fix this"},
        ],
        "comments": [],
    })
    assert snap.status == "has_comments"


def test_coderabbit_summary_comment_is_not_actionable():
    snap = _classify(1, {
        "state": "OPEN", "title": "x",
        "statusCheckRollup": [
            {"name": "test", "conclusion": "SUCCESS", "status": "COMPLETED"},
        ],
        "reviews": [],
        "comments": [
            {"author": {"login": "coderabbitai"}, "body": "Summary by CodeRabbit"},
        ],
    })
    assert snap.status == "green"


def test_external_ci_pending_classified():
    """External CI (commit-status protocol) uses `state: PENDING`, not `status`."""
    snap = _classify(1, {
        "state": "OPEN", "title": "x",
        "statusCheckRollup": [
            {"state": "PENDING", "context": "ci/circleci"},  # external CI shape
        ],
        "reviews": [],
        "comments": [],
    })
    assert snap.status == "pending"
    assert len(snap.pending_checks) == 1


def test_external_ci_failure_classified():
    snap = _classify(1, {
        "state": "OPEN", "title": "x",
        "statusCheckRollup": [
            {"state": "FAILURE", "context": "ci/jenkins"},
        ],
        "reviews": [],
        "comments": [],
    })
    assert snap.status == "ci_failed"


def test_timed_out_check_is_failure():
    snap = _classify(1, {
        "state": "OPEN", "title": "x",
        "statusCheckRollup": [
            {"name": "test", "conclusion": "TIMED_OUT", "status": "COMPLETED"},
        ],
        "reviews": [],
        "comments": [],
    })
    assert snap.status == "ci_failed"


def test_to_summary_truncates_bodies():
    snap = _classify(1, {
        "state": "OPEN", "title": "x",
        "statusCheckRollup": [],
        "reviews": [
            {"state": "CHANGES_REQUESTED", "body": "x" * 500},
        ],
        "comments": [],
    })
    summary = snap.to_summary()
    body = summary["review_comments"][0]["body"]
    assert len(body) <= 200


def test_resolved_threads_and_superseded_reviews_do_not_requeue():
    data = {
        "state": "OPEN", "reviews": [
            {"id": "r1", "author": {"login": "reviewer"}, "state": "CHANGES_REQUESTED", "submittedAt": "1"},
            {"id": "r2", "author": {"login": "reviewer"}, "state": "APPROVED", "submittedAt": "2"},
        ],
        "reviewThreads": [{"id": "t1", "isResolved": True}],
    }
    assert _classify(1, data).status == "green"
    data["reviewThreads"][0]["isResolved"] = False
    assert _classify(1, data).status == "has_comments"


def test_feedback_acknowledgement_is_scoped_and_new_revision_requeues(tmp_path):
    from agent_flow.pr_watch import (
        _read_feedback_state, _record_feedback_observation, acknowledge_pr_feedback,
    )

    data = {"state": "OPEN", "headRefOid": "head-1", "comments": [
        {"id": "c1", "body": "please clarify", "updatedAt": "1"},
    ]}
    snapshot = _classify(7, data, repo="owner/repo")
    _record_feedback_observation(tmp_path, snapshot)
    with pytest.raises(ValueError, match="does not match"):
        acknowledge_pr_feedback(
            tmp_path, repo="other/repo", number=7, head="head-1", feedback_ids=snapshot.feedback_ids,
        )
    with pytest.raises(ValueError, match="does not match"):
        acknowledge_pr_feedback(
            tmp_path, repo="owner/repo", number=7, head="stale", feedback_ids=snapshot.feedback_ids,
        )
    acknowledge_pr_feedback(
        tmp_path, repo="owner/repo", number=7, head="head-1", feedback_ids=snapshot.feedback_ids,
    )
    handled = _read_feedback_state(tmp_path, "owner/repo", 7)["handled"]
    assert _classify(7, data, repo="owner/repo", handled_ids=handled).status == "green"
    data["headRefOid"] = "head-2"
    assert _classify(7, data, repo="owner/repo", handled_ids=handled).status == "green"
    data["comments"][0]["body"] = "one more question"
    data["comments"][0]["updatedAt"] = "2"
    assert _classify(7, data, repo="owner/repo", handled_ids=handled).status == "has_comments"


def test_ci_check_identity_is_stable_across_heads_and_separates_workflows_and_statuses(tmp_path):
    from agent_flow.pr_watch import _read_feedback_state, _record_feedback_observation

    data = {
        "state": "OPEN", "headRefOid": "head-1", "statusCheckRollup": [
            {"name": "test", "workflowName": "linux", "conclusion": "FAILURE", "detailsUrl": "run-1"},
            {"name": "test", "workflowName": "macos", "conclusion": None, "status": "IN_PROGRESS"},
            {"context": "test", "state": "SUCCESS"},
        ],
    }
    first = _classify(7, data, repo="owner/repo")
    assert first.status == "ci_failed"
    assert first.pending_checks[0]["workflowName"] == "macos"
    _record_feedback_observation(tmp_path, first)
    data["headRefOid"] = "head-2"
    data["statusCheckRollup"][0]["detailsUrl"] = "run-2"
    data["statusCheckRollup"][0]["conclusion"] = "SUCCESS"
    second = _classify(7, data, repo="owner/repo")
    assert second.status == "pending"
    _record_feedback_observation(tmp_path, second)
    observed = _read_feedback_state(tmp_path, "owner/repo", 7)
    assert first.ci_checks.keys() == observed["ci_checks"].keys()
    assert list(first.ci_checks.values()) == ["failed", "pending", "success"]
    assert list(observed["ci_checks"].values()) == ["success", "pending", "success"]
    assert next(iter(first.ci_revisions.values())) == {"detailsUrl": "run-1"}
    assert next(iter(observed["ci_revisions"].values())) == {"detailsUrl": "run-2"}


@pytest.mark.parametrize("checks", [
    {"name": "test"}, ["not-a-check"], [{"conclusion": "FAILURE"}],
    [{"name": "test", "conclusion": "FAILURE"}, {"name": "test", "conclusion": "SUCCESS"}],
])
def test_malformed_or_ambiguous_ci_evidence_is_not_green(checks):
    snapshot = _classify(7, {"state": "OPEN", "statusCheckRollup": checks})
    assert snapshot.status == "error"


@pytest.mark.parametrize("check", [
    {"name": "unit", "status": "COMPLETED"},
    {"name": "unit", "status": "COMPLETED", "conclusion": "UNKNOWN"},
])
def test_incomplete_or_unknown_terminal_ci_result_remains_pending(check):
    assert _classify(7, {"state": "OPEN", "statusCheckRollup": [check]}).status == "pending"


def test_failed_observation_does_not_erase_last_known_ci_checks(tmp_path, monkeypatch):
    from agent_flow.pr_watch import _record_feedback_observation, fetch_pr

    snapshot = _classify(7, {
        "state": "OPEN", "headRefOid": "head-1",
        "statusCheckRollup": [{"name": "unit", "conclusion": "FAILURE"}],
    }, repo="owner/repo")
    _record_feedback_observation(tmp_path, snapshot)
    path = tmp_path / "pr-feedback.json"
    before = path.read_bytes()

    def unavailable(*args, **kwargs):
        raise ValueError("GitHub unavailable")

    monkeypatch.setattr("agent_flow.pr_watch._fetch_pr_data", unavailable)
    assert fetch_pr(7, repo="owner/repo", run_dir=tmp_path).status == "error"
    assert path.read_bytes() == before


def test_fetch_pr_reads_all_thread_pages_and_records_feedback(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from agent_flow.pr_watch import fetch_pr, _read_feedback_state

    view = {
        "state": "OPEN", "url": "https://github.com/owner/repo/pull/7", "headRefOid": "head-1",
        "reviews": [], "comments": [],
    }
    def page(thread, has_next):
        return {"data": {"repository": {"pullRequest": {"reviewThreads": {
            "nodes": [thread], "pageInfo": {"hasNextPage": has_next, "endCursor": "cursor"},
        }}}}}
    pages = [
        page({"id": "t1", "isResolved": True}, True),
        page({"id": "t2", "isResolved": False, "comments": {"nodes": [
            {"id": "c2", "body": "unsafe boundary", "url": "https://github.com/thread"},
        ], "totalCount": 1, "commentPageInfo": {"hasNextPage": False}}}, False),
    ]
    replies = iter([json.dumps(view), json.dumps(pages)])
    monkeypatch.setattr(
        "agent_flow.pr_watch.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=next(replies), stderr=""),
    )
    snapshot = fetch_pr(7, run_dir=tmp_path)
    assert snapshot.status == "has_comments"
    assert snapshot.to_summary()["review_comments"][0]["body"] == "unsafe boundary"
    assert _read_feedback_state(tmp_path, "owner/repo", 7)["feedback_ids"] == snapshot.feedback_ids


def test_commented_review_body_requires_ack_and_edited_revision_requeues(tmp_path):
    from agent_flow.pr_watch import (
        _read_feedback_state, _record_feedback_observation, acknowledge_pr_feedback,
    )

    review = {
        "id": "r1", "author": {"login": "reviewer"}, "state": "COMMENTED",
        "body": "Handle the missing token before saving", "submittedAt": "1",
    }
    data = {"state": "OPEN", "headRefOid": "head-1", "reviews": [review]}
    snapshot = _classify(7, data, repo="owner/repo")
    assert snapshot.status == "has_comments"
    assert snapshot.to_summary()["review_comments"][0]["body"] == review["body"]
    _record_feedback_observation(tmp_path, snapshot)
    acknowledge_pr_feedback(
        tmp_path, repo="owner/repo", number=7, head="head-1",
        feedback_ids=snapshot.feedback_ids,
    )
    handled = _read_feedback_state(tmp_path, "owner/repo", 7)["handled"]
    assert _classify(7, data, handled_ids=handled).status == "green"
    review["body"] = "The token can also expire while saving"
    assert _classify(7, data, handled_ids=handled).status == "has_comments"


def test_later_commented_review_does_not_erase_unacknowledged_bodies():
    data = {"state": "OPEN", "reviews": [
        {"id": "r1", "author": {"login": "reviewer"}, "state": "COMMENTED",
         "body": "Fix token expiry", "submittedAt": "1"},
        {"id": "r2", "author": {"login": "reviewer"}, "state": "COMMENTED",
         "body": "Also handle cancellation", "submittedAt": "2"},
    ]}
    snapshot = _classify(7, data)
    assert {item["body"] for item in snapshot.review_comments} == {
        "Fix token expiry", "Also handle cancellation",
    }


def test_fetch_pr_preserves_rejection_after_commented_followup(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from agent_flow.pr_watch import fetch_pr, acknowledge_pr_feedback

    rejection = {
        "id": "r1", "author": {"login": "reviewer"}, "state": "CHANGES_REQUESTED",
        "body": "Fix the lost update", "submittedAt": "1",
    }
    followup = {
        "id": "r2", "author": {"login": "reviewer"}, "state": "COMMENTED",
        "body": "", "submittedAt": "2",
    }
    view = {
        "state": "OPEN", "url": "https://github.com/owner/repo/pull/7",
        "headRefOid": "head-1", "reviewDecision": "CHANGES_REQUESTED",
    }
    def respond(cmd, **kwargs):
        if cmd[1:3] == ["pr", "view"]:
            payload = dict(view)
            fields = cmd[cmd.index("--json") + 1].split(",")
            if "reviews" in fields:
                payload["reviews"] = [rejection, followup]
            if "latestReviews" in fields:
                payload["latestReviews"] = [followup]
        else:
            payload = [{"data": {"repository": {"pullRequest": {"reviewThreads": {
                "nodes": [], "pageInfo": {"hasNextPage": False},
            }}}}}]
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("agent_flow.pr_watch.subprocess.run", respond)
    snapshot = fetch_pr(7, run_dir=tmp_path)
    assert snapshot.status == "has_comments"
    assert snapshot.to_summary()["review_comments"][0]["body"] == rejection["body"]
    acknowledge_pr_feedback(
        tmp_path, repo="owner/repo", number=7, head="head-1",
        feedback_ids=snapshot.feedback_ids,
    )
    assert fetch_pr(7, run_dir=tmp_path).status == "green"


@pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
def test_fetch_terminal_pr_does_not_depend_on_review_threads(state, monkeypatch):
    import json
    from types import SimpleNamespace
    from agent_flow.pr_watch import fetch_pr

    def respond(cmd, **kwargs):
        if cmd[1:3] == ["pr", "view"]:
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
                "state": state, "url": "https://github.com/owner/repo/pull/7",
                "headRefOid": "head-1",
            }))
        return SimpleNamespace(returncode=1, stdout="", stderr="GraphQL unavailable")

    monkeypatch.setattr("agent_flow.pr_watch.subprocess.run", respond)
    snapshot = fetch_pr(7)
    assert snapshot.status == state.lower()
    assert snapshot.head == "head-1"


def test_fetch_pr_returns_error_snapshot_when_feedback_lease_is_unavailable(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from agent_flow.core.worktree_isolation import FileLeaseUnavailable
    from agent_flow.pr_watch import fetch_pr

    monkeypatch.setattr(
        "agent_flow.pr_watch.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
            "state": "OPEN", "url": "https://github.com/owner/repo/pull/7",
            "headRefOid": "head-1",
        })),
    )
    monkeypatch.setattr("agent_flow.pr_watch._fetch_review_threads", lambda *args: [])
    def unavailable(*args, **kwargs):
        raise FileLeaseUnavailable("feedback lease is busy")

    monkeypatch.setattr("agent_flow.pr_watch.exclusive_file_lease", unavailable)
    snapshot = fetch_pr(7, run_dir=tmp_path)
    assert snapshot.status == "error"
    assert "feedback lease is busy" in snapshot.error


def test_fetch_open_pr_keeps_unavailable_threads_fail_closed(monkeypatch):
    import json
    from types import SimpleNamespace
    from agent_flow.pr_watch import fetch_pr

    replies = iter([
        SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
            "state": "OPEN", "url": "https://github.com/owner/repo/pull/7",
        })),
        SimpleNamespace(returncode=1, stdout="", stderr="GraphQL unavailable"),
    ])
    monkeypatch.setattr(
        "agent_flow.pr_watch.subprocess.run", lambda *args, **kwargs: next(replies),
    )
    snapshot = fetch_pr(7)
    assert snapshot.status == "error"
    assert "GraphQL unavailable" in snapshot.error


def test_approval_clears_rejection_but_not_unacknowledged_commented_body():
    data = {
        "state": "OPEN", "reviewDecision": "APPROVED", "reviews": [
            {"id": "r1", "author": {"login": "reviewer"}, "state": "CHANGES_REQUESTED",
             "body": "Fix token expiry", "submittedAt": "1"},
            {"id": "r2", "author": {"login": "reviewer"}, "state": "COMMENTED",
             "body": "Document cancellation", "submittedAt": "2"},
        ],
    }
    snapshot = _classify(7, data)
    assert [item["body"] for item in snapshot.review_comments] == ["Document cancellation"]
    assert _classify(7, data, handled_ids=snapshot.feedback_ids).status == "green"


def test_commented_followup_does_not_reverse_reviewer_approval():
    data = {"state": "OPEN", "reviews": [
        {"id": "r1", "author": {"login": "reviewer"}, "state": "CHANGES_REQUESTED",
         "body": "Fix token expiry", "submittedAt": "1"},
        {"id": "r2", "author": {"login": "reviewer"}, "state": "APPROVED",
         "submittedAt": "2"},
        {"id": "r3", "author": {"login": "reviewer"}, "state": "COMMENTED",
         "body": "", "submittedAt": "3"},
    ]}
    assert _classify(7, data).status == "green"


def _thread_transport(monkeypatch, comments, *, host="github.com", incomplete=False):
    import json
    from types import SimpleNamespace

    def connection(nodes, has_next=False, cursor=None):
        return {
            "nodes": nodes, "totalCount": len(comments),
            "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        }

    def respond(cmd, **kwargs):
        if cmd[1:3] == ["pr", "view"]:
            payload = {
                "state": "OPEN", "url": f"https://{host}/owner/repo/pull/7",
                "headRefOid": "head-1",
            }
        else:
            requested_host = cmd[cmd.index("--hostname") + 1] if "--hostname" in cmd else "github.com"
            if requested_host != host:
                return SimpleNamespace(returncode=1, stdout="", stderr="repository not found on this host")
            query = next(arg.removeprefix("query=") for arg in cmd if arg.startswith("query="))
            if "reviewThreads(" in query:
                first = comments[-1:] if "comments(last:1)" in query else comments[:100]
                initial = connection(first, len(first) < len(comments), "c100")
                if "commentPageInfo:pageInfo" in query:
                    initial["commentPageInfo"] = initial.pop("pageInfo")
                payload = [{"data": {"repository": {"pullRequest": {"reviewThreads": {
                    "nodes": [{
                        "id": "t1", "isResolved": False,
                        "comments": initial,
                    }],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }}}}}]
            else:
                remaining = comments[100:]
                payload = [
                    {"data": {"node": {
                        "id": "t1",
                        "comments": connection(
                            remaining[start:start + 100], start + 100 < len(remaining),
                            f"c{start + 200}",
                        ),
                    }}}
                    for start in range(0, len(remaining), 100)
                ]
                if incomplete:
                    payload = payload[:-1]
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("agent_flow.pr_watch.subprocess.run", respond)


def test_acknowledged_thread_requeues_when_earlier_comment_is_edited(tmp_path, monkeypatch):
    from agent_flow.pr_watch import acknowledge_pr_feedback, fetch_pr

    comments = [
        {"id": "c1", "body": "Original finding", "updatedAt": "1"},
        {"id": "c2", "body": "Reply", "updatedAt": "2"},
    ]
    _thread_transport(monkeypatch, comments)
    observed = fetch_pr(7, run_dir=tmp_path)
    assert observed.status == "has_comments"
    acknowledge_pr_feedback(
        tmp_path, repo=observed.repo, number=7, head=observed.head,
        feedback_ids=observed.feedback_ids,
    )
    assert fetch_pr(7, run_dir=tmp_path).status == "green"
    comments[0].update(body="Edited original finding", updatedAt="3")
    assert fetch_pr(7, run_dir=tmp_path).status == "has_comments"


@pytest.mark.parametrize("edited_index", [0, 150, 200], ids=["first-page", "middle-page", "last-page"])
def test_thread_revision_includes_every_comment_page(tmp_path, monkeypatch, edited_index):
    from agent_flow.pr_watch import acknowledge_pr_feedback, fetch_pr

    comments = [{"id": f"c{i}", "body": f"Finding {i}", "updatedAt": "1"} for i in range(201)]
    _thread_transport(monkeypatch, comments)
    observed = fetch_pr(7, run_dir=tmp_path)
    assert observed.status == "has_comments"
    acknowledge_pr_feedback(
        tmp_path, repo=observed.repo, number=7, head=observed.head,
        feedback_ids=observed.feedback_ids,
    )
    assert fetch_pr(7, run_dir=tmp_path).status == "green"
    comments[edited_index].update(body="Changed finding", updatedAt="2")
    assert fetch_pr(7, run_dir=tmp_path).status == "has_comments"


def test_incomplete_nested_comment_pages_fail_closed(tmp_path, monkeypatch):
    from agent_flow.pr_watch import fetch_pr

    comments = [{"id": f"c{i}", "body": f"Finding {i}"} for i in range(201)]
    _thread_transport(monkeypatch, comments, incomplete=True)
    snapshot = fetch_pr(7, run_dir=tmp_path)
    assert snapshot.status == "error"
    assert not (tmp_path / "pr-feedback.json").exists()


@pytest.mark.parametrize(("repo", "host"), [
    ("github.com/owner/repo", "github.com"),
    ("owner/repo", "git.example.com"),
    ("git.example.com/owner/repo", "git.example.com"),
    (None, "git.example.com"),
])
def test_repository_host_routes_threads_and_scopes_ack(tmp_path, monkeypatch, repo, host):
    from agent_flow.pr_watch import acknowledge_pr_feedback, fetch_pr

    _thread_transport(monkeypatch, [{"id": "c1", "body": "Fix this"}], host=host)
    observed = fetch_pr(7, repo=repo, run_dir=tmp_path)
    assert observed.status == "has_comments"
    assert observed.to_summary()["repo"] == f"{host}/owner/repo"
    acknowledge_pr_feedback(
        tmp_path, repo=observed.repo, number=7, head=observed.head,
        feedback_ids=observed.feedback_ids,
    )
    assert fetch_pr(7, repo=repo, run_dir=tmp_path).status == "green"


def test_same_repository_on_other_host_does_not_share_ack(tmp_path, monkeypatch):
    from agent_flow.pr_watch import acknowledge_pr_feedback, fetch_pr

    comments = [{"id": "c1", "body": "Fix this"}]
    _thread_transport(monkeypatch, comments)
    public = fetch_pr(7, run_dir=tmp_path)
    acknowledge_pr_feedback(
        tmp_path, repo=public.repo, number=7, head=public.head, feedback_ids=public.feedback_ids,
    )
    _thread_transport(monkeypatch, comments, host="git.example.com")
    enterprise = fetch_pr(7, run_dir=tmp_path)
    assert enterprise.status == "has_comments"
    with pytest.raises(ValueError, match="does not match"):
        acknowledge_pr_feedback(
            tmp_path, repo=public.repo, number=7, head=public.head,
            feedback_ids=public.feedback_ids,
        )


@pytest.mark.parametrize("target_kind", ["dangling-symlink", "directory"])
def test_unsafe_feedback_target_returns_error_without_writing(tmp_path, monkeypatch, target_kind):
    from agent_flow.pr_watch import fetch_pr

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    target = run_dir / "pr-feedback.json"
    outside = tmp_path / "outside.json"
    if target_kind == "dangling-symlink":
        target.symlink_to(outside)
    else:
        target.mkdir()
    _thread_transport(monkeypatch, [{"id": "c1", "body": "Fix this"}])
    snapshot = fetch_pr(7, run_dir=run_dir)
    assert snapshot.status == "error"
    assert snapshot.error
    assert not outside.exists()
    assert target.is_symlink() if target_kind == "dangling-symlink" else target.is_dir()


def test_watch_waits_for_observation_lease_before_returning_green(tmp_path, monkeypatch):
    import json
    import threading
    from agent_flow.artifact import ACTIVE_LOCK
    from agent_flow.core.worktree_isolation import exclusive_file_lease
    from agent_flow.pr_watch import watch_pr

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    entered = threading.Event()
    finished = threading.Event()
    snapshots = []
    failures = []
    monkeypatch.setattr(
        "agent_flow.pr_watch._fetch_pr_data",
        lambda *args: {"state": "OPEN", "url": "https://github.com/owner/repo/pull/7",
                      "headRefOid": "head-1"},
    )
    monkeypatch.setattr("agent_flow.pr_watch._fetch_review_threads", lambda *args: [])

    def observe_lease(*args, **kwargs):
        entered.set()
        return exclusive_file_lease(*args, **kwargs)

    def watch():
        try:
            snapshots.append(watch_pr(7, run_dir=run_dir, max_poll_count=1))
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    monkeypatch.setattr("agent_flow.pr_watch.exclusive_file_lease", observe_lease)
    worker = threading.Thread(target=watch, daemon=True)
    try:
        with exclusive_file_lease(run_dir.parent / ACTIVE_LOCK):
            worker.start()
            assert entered.wait(2)
            assert not finished.wait(0.1)
            assert not (run_dir / "pr-feedback.json").exists()
    finally:
        worker.join(2)
    assert finished.is_set()
    assert not failures
    assert snapshots[0].status == "green"
    observation = json.loads((run_dir / "pr-feedback.json").read_text())
    assert observation["head"] == snapshots[0].head
    assert observation["repo"] == snapshots[0].repo
