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
- Antigravity: `~/.agents/skills/{skill}/SKILL.md`

Do not install, copy, link, vendor, or fallback to another host path. If a
required local skill is missing, stop approval and report
`missing local android_skills: <skill>` or
`missing local chrisbanes_skills: <skill>` with the profile source URL.
Record `android-local-skills: checked|n/a` and
`android-local-skills-used: <skill list or n/a>` in the review artifact's
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

- `../android-guides/references/code-review-checklist.md`
- `../android-guides/references/architecture-rules-guide.md`
- `../clean-architecture/SKILL.md`
- `../android-guides/references/compose-performance-guide.md`
- `../android-guides/references/kotlin-concurrency-guide.md`
- `../android-guides/references/data-layer-guide.md`
- `../android-guides/references/testing-guide.md`
- `../android-appshell-error-handling/SKILL.md`
