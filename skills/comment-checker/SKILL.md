---
name: comment-checker
description: Use when installing, configuring, or reviewing agent-flow comment-checker hooks that detect newly added low-value comments without blocking WHY, constraint, workaround, public API, security, performance, or concurrency comments.
---

# Comment Checker

Use this for the hook-backed guard that watches edit-like tool calls.

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
