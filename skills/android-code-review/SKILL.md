---
name: android-code-review
description: |
  Use when reviewing Android Kotlin or Compose changes. Focuses on architecture
  boundaries, MVI/state correctness, Compose stability, coroutine safety, data
  layer behavior, testing, and Gradle hygiene.
---

# Android Code Review

Review only changed behavior unless the user asks for a broader audit. Read the
diff and relevant existing patterns directly before deciding.

## Review Order

1. Scope: changed files, affected modules, generated files, Gradle changes.
2. Requirement fit: what the user asked for versus what changed.
3. Architecture: dependency direction and layer ownership.
4. UI state: loading/success/empty/error, event handling, lifecycle collection.
5. Compose: stability, recomposition risk, lazy list keys, remembered work.
6. Coroutine/Flow: cancellation, dispatcher choice, race conditions.
7. Data: DTO/domain separation, repository source-of-truth behavior, errors.
8. Tests/gates: unit tests, UI tests, build, lint, and missing edge cases.

## Findings

Lead with bugs and regressions. Use file and line references. Do not request
changes for style preferences that already match the repository.

```markdown
## Android Code Review

### Findings
- [P1] file.kt:123 - concrete issue, impact, and fix

### Open Questions
- ...

### Test Gaps
- ...
```

## Approval Bar

Approve only when:

- Changed code follows the project's Android architecture.
- No new framework dependency leaks into domain or shared pure modules.
- UI states cover failure and empty data where applicable.
- Coroutine cancellation is not swallowed.
- The relevant Android profile gates have a credible path to pass.

## References

- `../android-guides/references/code-review-checklist.md`
- `../android-guides/references/architecture-rules-guide.md`
- `../android-guides/references/compose-performance-guide.md`
- `../android-guides/references/kotlin-concurrency-guide.md`
- `../android-guides/references/data-layer-guide.md`
- `../android-guides/references/testing-guide.md`

