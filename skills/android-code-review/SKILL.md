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

## Android Profile Skills

For Android/Kotlin/Compose/KMP diffs, read matching entries from the Android
profile's `android_skills` and `chrisbanes_skills` as plain text before
approving. Use these chrisbanes skill names as checklist labels:

- Compose state/effects: `compose-state-authoring`, `compose-state-hoisting`,
  `compose-state-holder-ui-split`, `compose-side-effects`
- Compose performance: `compose-recomposition-performance`,
  `compose-stability-diagnostics`, `compose-state-deferred-reads`
- Compose UI API/layout: `compose-modifier-and-layout-style`,
  `compose-slot-api-pattern`, `compose-animations`, `compose-focus-navigation`
- Compose tests: `compose-ui-testing-patterns`
- Kotlin: `kotlin-coroutines-structured-concurrency`,
  `kotlin-flow-state-event-modeling`, `kotlin-types-value-class`
- KMP: `kotlin-multiplatform-expect-actual`

Do not parse upstream frontmatter through the native skill loader. Resolve
skills through the current active host only:

- Codex: `~/.codex/skills/{skill}/SKILL.md`
- Claude: `~/.claude/skills/{skill}/SKILL.md`
- OMP: `~/.omp/agent/skills/{skill}/SKILL.md`

Do not install, copy, link, vendor, or fallback to another host path. If a
required local skill is missing, stop approval and report
`missing local android_skills: <skill>` or
`missing local chrisbanes_skills: <skill>` with the profile source URL.
Record `android-local-skills: checked`,
`android-local-skills-used: <skill list>`, `chrisbanes-skills: checked|n/a`,
and `chrisbanes-skills-used: <skill list or n/a>` in the review artifact's
`## Completion Gate`.

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
