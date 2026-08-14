---
name: comment-checker
description: Use when installing, configuring, or reviewing agent-flow comment-checker hooks that detect newly added low-value comments without blocking WHY, constraint, workaround, public API, security, performance, or concurrency comments.
---

# Comment Checker

Use this for the hook-backed guard that watches edit-like tool calls.

## Hook Contract

Use `comment-authoring-discipline` as the semantic source for comment quality. This skill only defines the hook behavior:

- inspect newly added comments from edit-like tool calls;
- block additions that the canonical policy classifies under `Remove Or Avoid`;
- preserve additions that the canonical policy classifies under `Keep Or Add`;
- accept comment-free code;
- recognize normal comment syntax for Python, Kotlin, TypeScript/React, React Native, Swift, and SwiftUI.
