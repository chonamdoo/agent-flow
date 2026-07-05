---
name: android-guides
description: Shared Android reference bundle for other android-* skills, covering architecture, Compose, data, Gradle, DI, concurrency, testing, and review checklists. Use when another Android skill tells you to read a specific guide; this is reference-only and not a standalone workflow skill.
---

# Android Guides

This is a reference-only bundle for Android profile projects, not a standalone
workflow skill. Pair it with a task-specific Android skill such as
`android-code-review`, `android-debugging`, `android-module-creator`, or
`android-mvi-feature`, and prefer the target project's own conventions when
they conflict with these defaults.

## Quick start

1. Do not use this as a standalone workflow.
2. Load the task-specific Android skill first.
3. Read only the reference file that matches the current Android concern.

## Reference index

Read only the one-level reference files that match the task:

- [architecture-rules-guide.md](references/architecture-rules-guide.md) when checking module boundaries, dependency direction, and layer ownership.
- [code-review-checklist.md](references/code-review-checklist.md) when performing Android review.
- [compose-performance-guide.md](references/compose-performance-guide.md) when investigating recomposition, stability, lazy lists, or frame-time work.
- [compose-ui-guide.md](references/compose-ui-guide.md) when implementing or reviewing Compose UI structure and semantics.
- [data-layer-guide.md](references/data-layer-guide.md) when touching repositories, DTO/domain mapping, caching, or source-of-truth behavior.
- [di-hilt-guide.md](references/di-hilt-guide.md) when adding modules, bindings, scopes, or injected Android components.
- [error-handling-guide.md](references/error-handling-guide.md) when modeling UI errors, common app errors, retries, or failure states.
- [gradle-build-performance-guide.md](references/gradle-build-performance-guide.md) when editing Gradle modules, convention plugins, dependencies, or build gates.
- [kotlin-concurrency-guide.md](references/kotlin-concurrency-guide.md) when reviewing coroutines, Flow, dispatchers, cancellation, or lifecycle collection.
- [module-creation-guide.md](references/module-creation-guide.md) when creating or registering Android modules.
- [mvi-feature-guide.md](references/mvi-feature-guide.md) when implementing feature screens, ViewModels, actions, and UI state.
- [testing-guide.md](references/testing-guide.md) when adding or reviewing Android unit, UI, screenshot, or smoke tests.
