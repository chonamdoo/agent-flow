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
            {"state": "COMMENTED", "body": "x" * 500},
        ],
        "comments": [],
    })
    summary = snap.to_summary()
    body = summary["review_comments"][0]["body"]
    assert len(body) <= 200
