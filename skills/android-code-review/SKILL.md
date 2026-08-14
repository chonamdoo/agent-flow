---
name: android-code-review
description: Android Kotlin and Jetpack Compose review checklist for changed code, covering architecture boundaries, MVI/state correctness, Compose stability, coroutine safety, data layer behavior, testing, and Gradle hygiene. Use when reviewing Android, Kotlin, Compose, or KMP changes; do not use as a debugging workflow or for non-Android React/TypeScript/Python reviews.
---

# Android Code Review

Review only changed behavior unless the user asks for a broader audit. Read the
diff and relevant existing patterns directly before deciding.

## Quick start

1. Confirm the diff includes Android, Kotlin, Compose, Gradle, or KMP changes.
2. Read the user request, changed files, and closest existing implementation before judging.
3. Load only the Android/Compose/Kotlin reference material that matches the diff.
4. Report blocking findings with file/line evidence; leave style preferences as suggestions.

## Non-goals

- Do not use this skill to diagnose an unlocalized bug; use `android-debugging` first.
- Do not use it for React Web, React Native JavaScript/TypeScript, Python, or generic TypeScript review unless Android native code is also changed.
- Do not approve broad rewrites or new architecture solely from checklist preference.

## Skills For The Diff

The phase prompt lists the skills matched to this change — required ones with
paths, in-scope ones by name. Read the required ones as plain text before
approving; use an in-scope one only when the diff actually touches it. No list
of upstream skill names is kept in this repo: matching runs per diff over the
stack vocabulary the Android profile declares and the `name`/`description` of
the skills installed on this machine. Cover these angles when the diff touches
them:

- Compose state/effects: state ownership, hoisting, state-holder/UI split,
  effect keys, one-shot event handling
- Compose performance: recomposition scope, stability, deferred reads, strong
  skipping compatibility
- Compose UI APIs: modifier placement and order, layout, slot APIs, animation,
  focus navigation
- Compose UI testing: semantics and test coverage
- Kotlin concurrency: structured concurrency, cancellation, dispatcher choice,
  Flow state/event modeling
- KMP and domain types: expect/actual boundaries, value class suitability

Resolve every required skill through the phase prompt and installed skill index. Read the exact path the resolver supplies; do not construct host home-directory paths or search another host's installation.

If a required skill is unresolved, stop approval and report `missing local <group>: <skill>` with the configured source URL. Record the resolved paths actually read in the review artifact's `Calibration` section.

## Review Order

1. Scope: changed files, affected modules, generated files, Gradle changes.
2. Requirement fit: what the user asked for versus what changed.
3. Architecture: apply `clean-architecture` for dependency direction and layer
   ownership, then Android-specific guides for platform details.
4. UI state: loading/success/empty/error, event handling, lifecycle collection.
5. Compose: stability, recomposition risk, lazy list keys, remembered work.
6. Coroutine/Flow: cancellation, dispatcher choice, race conditions.
7. Data: DTO/domain separation, repository source-of-truth behavior, errors.
   For app-wide common errors, apply `android-appshell-error-handling`.
8. Tests/gates: unit tests, UI tests, build, lint, and missing edge cases.

## Findings

Lead with bugs and regressions. Use file and line references. Do not request
changes for style preferences that already match the repository.

```markdown
## Android Code Review

### Calibration
- `<resolved-skill-path>`

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

- [code-review-checklist.md](../android-guides/references/code-review-checklist.md) when structuring the Android review.
- [architecture-rules-guide.md](../android-guides/references/architecture-rules-guide.md) and [clean-architecture](../clean-architecture/SKILL.md) when dependency direction or layer ownership changed.
- [compose-performance-guide.md](../android-guides/references/compose-performance-guide.md) when Compose stability, recomposition, lazy lists, or frame-time work changed.
- [kotlin-concurrency-guide.md](../android-guides/references/kotlin-concurrency-guide.md) when coroutines, Flow, dispatchers, or cancellation changed.
- [data-layer-guide.md](../android-guides/references/data-layer-guide.md) when repositories, DTO/domain mapping, caching, or source-of-truth behavior changed.
- [testing-guide.md](../android-guides/references/testing-guide.md) when evaluating Android test coverage and gates.
- [android-appshell-error-handling](../android-appshell-error-handling/SKILL.md) when app-wide common errors, session expiry, root navigation, dialogs, or snackbars changed.
