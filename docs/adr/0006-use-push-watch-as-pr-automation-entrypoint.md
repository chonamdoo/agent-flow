# Use push-watch as PR automation entrypoint

## Status

Accepted

## Context

The full-feature workflow already models PR watch, comment fix, CI fix, merge, and handoff phases. A separate user-facing entrypoint is still needed for the practical commit, push, PR creation, and watch loop.

## Decision

Use `push-watch` as the installed skill and CLI entrypoint for PR automation. Keep the existing phase ids (`push-pr`, `pr-watch`, `pr-comment-fix`, `pr-ci-fix`, `merge`) as the workflow contract.

`push-watch` may automate status collection and routing artifacts, but merge remains approval-gated.

## Consequences

- Users get one clear command for the PR automation loop.
- Existing workflow phase names stay stable.
- The runner can add GitHub CLI integration without coupling the whole full-feature workflow to unattended merge behavior.
