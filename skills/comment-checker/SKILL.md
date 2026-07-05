---
name: comment-checker
description: Installs, configures, or reviews the agent-flow comment-checker hook that detects newly added low-value comments while allowing WHY, constraint, workaround, public API, security, performance, concurrency, and complex-rule comments. Use when working on comment-checker hook setup, policy, or review; pair with comment-authoring-discipline for the human final pass.
---

# Comment Checker

Use this for the hook-backed guard that watches edit-like tool calls.

## Quick start

1. Use this only for the hook-backed guard that inspects newly added comments from edit-like tool calls.
2. Configure or review the hook to block clear low-value additions while allowing useful WHY/constraint/workaround/security/performance/concurrency/public API comments.
3. Pair with `comment-authoring-discipline` for the final human judgment; this is a hook-policy skill, not a standalone code-review workflow, and it does not replace comment authoring discipline.

## Purpose

- Detect newly added unnecessary comments.
- Preserve useful WHY, external constraint, workaround, security, performance, concurrency, complex rule, and public API comments.
- Avoid pushing agents toward more comments.
- Support common comment syntax for Python, Kotlin, TypeScript/React, React Native, Swift, and SwiftUI.

## Blocking Scope

Block only clear low-value additions:

- Comments that restate obvious code behavior.
- Decorative section dividers.
- Generic AI habit comments.
- TODO/NOTE comments with no reason.

Do not block comment-free code.
