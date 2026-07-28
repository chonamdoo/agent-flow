"""Consent gate for reusing the checkout the user is already standing in.

`_resolve_cli_root_context` infers a worktree name from cwd, so starting a run
inside a worktree silently attaches to it. Silent is the problem: the user
cannot tell reuse from creation, and a run that lands in the wrong checkout is
only visible after it has written there.

The decision is pure so the caller can prove that refusing changes nothing —
no branch, no worktree, no runtime state. Prompting is the caller's job.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROMPT = "Reuse this worktree for this run? [y/N] "

NOT_APPLICABLE = "not-applicable"
CONSENT_REQUIRED = "consent-required"
GRANTED = "granted"
REFUSED = "refused"

_AFFIRMATIVE = frozenset({"y", "yes"})


@dataclass(frozen=True)
class CurrentCheckout:
    path: Path
    branch: str | None
    dirty: bool
    state_key: str
    is_leader: bool


@dataclass(frozen=True)
class ReuseDecision:
    outcome: str
    checkout: CurrentCheckout | None
    reason: str = ""

    @property
    def needs_prompt(self) -> bool:
        return self.outcome == CONSENT_REQUIRED

    @property
    def blocks_run(self) -> bool:
        return self.outcome == REFUSED


def decide_worktree_reuse(
    *,
    checkout: CurrentCheckout | None,
    explicit_selector: bool,
    interactive: bool,
) -> ReuseDecision:
    if checkout is None:
        return ReuseDecision(NOT_APPLICABLE, None, "not inside a linked worktree")
    if checkout.is_leader:
        return ReuseDecision(NOT_APPLICABLE, checkout, "leader checkout")
    if explicit_selector:
        # `--worktree`/`--reuse-current` names the target, so the user has
        # already said which checkout to use. Asking again would break every
        # non-interactive caller for no added information.
        return ReuseDecision(GRANTED, checkout, "explicit selector")
    if not interactive:
        # Fail closed. Defaulting to reuse here would reintroduce the silent
        # attach in exactly the automated contexts that cannot notice it.
        return ReuseDecision(
            REFUSED,
            checkout,
            "no interactive terminal; pass --worktree or --reuse-current to reuse this checkout",
        )
    return ReuseDecision(CONSENT_REQUIRED, checkout, "implicit reuse of the current worktree")


def consent_granted(answer: str) -> bool:
    return answer.strip().lower() in _AFFIRMATIVE


def render_checkout_summary(checkout: CurrentCheckout) -> str:
    """The four facts that distinguish one checkout from another.

    Path alone is not enough: two runs can share a path prefix, and the state
    key is what decides where runtime state lands.
    """
    return "\n".join(
        (
            f"Current worktree: {checkout.path}",
            f"  branch    : {checkout.branch or '(detached HEAD)'}",
            f"  dirty     : {'yes' if checkout.dirty else 'no'}",
            f"  state key : {checkout.state_key}",
        )
    )
