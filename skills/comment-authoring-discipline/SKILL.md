---
name: comment-authoring-discipline
description: Provides final comment-quality discipline for deciding whether changed code needs comments and removing low-value comment additions. Use when making the final comment-quality pass before final-review, multi-review, or architecture-review after code changes across Python, Kotlin, React Web, React Native, iOS, Swift, and SwiftUI; do not use it as a substitute for code review or comment-checker hooks.
---

# Comment Authoring Discipline

Use this only after implementation/fix work is complete and before review.

## Quick start

1. Inspect only the changed diff after implementation or fix work is complete.
2. Keep or add comments only for WHY, external constraints, workarounds, security, performance, concurrency/lifecycle, complex rules, complex algorithms/regex, or public API contracts.
3. Remove or avoid comments that merely restate code, then run `comment-checker` or the configured hook when available.

This is a final-pass discipline skill, not a standalone implementation or review workflow. It does not replace code review, architecture review, language/framework guidance, or the hook-backed `comment-checker`.

## Contract

- Default to adding no comments.
- Add or keep a comment only when code alone cannot explain the reason or contract.
- Remove or rewrite comments that merely describe what the next line does.
- Do not refactor, optimize performance, split modules, rename broadly, or expand scope.
- Do not expand into unrelated cleanup workflows.

## Keep Or Add

Use the platform's normal comment format only for:

- Business rules.
- External API or platform constraints.
- Workarounds.
- Security reasons.
- Performance reasons.
- Concurrency or lifecycle reasons.
- Complex algorithms or regular expressions.
- Public API contracts.

## Remove Or Avoid

- WHAT/HOW comments that restate code.
- Generic comments like "Initialize", "Set value", "Loop through", "Render UI", or "Handle click".
- Decorative section dividers.
- TODO/NOTE comments with no owner, trigger, or reason.
- Habitual AI comments that add no decision, constraint, or contract.

## Language Notes

- Python: keep public API docstrings, type/exception contracts, complex regex, concurrency, and IO constraints. Avoid function-internal WHAT comments.
- Kotlin/Android: keep KDoc/public API, lifecycle, Compose recomposition, coroutine, and threading constraints. Avoid setter or initializer narration.
- React Web/TypeScript: keep public component/hook contracts, hydration or server-client boundary notes, accessibility workarounds, and memoization reasons. Avoid JSX structure narration.
- React Native: keep platform-specific workarounds, native module, permission, lifecycle, and bridge-performance reasons. Avoid view tree narration.
- iOS/Swift/SwiftUI: keep DocC/public API, SwiftUI lifecycle/state identity, UIKit bridge, concurrency/MainActor, availability, privacy, and permission constraints. Avoid `body` structure narration.

## Final Pass

1. Inspect only the changed diff.
2. For each existing or candidate comment, ask: why is this not obvious from code?
3. If the answer is not one of the allowed reasons, remove or avoid the comment.
4. Run comment-checker or the configured hook when available.
5. Record `comment-authoring: applied` in the phase artifact.
6. Record `comment-checker: checked` when it ran, `unavailable` only when no project-local checker or hook command exists, and `n/a` only when the changed diff has no code files to inspect.
