# Review Angle — Android chrisbanes Skills

Review Android, Kotlin, Jetpack Compose, and KMP changes against locally
installed chrisbanes skills from https://github.com/chrisbanes/skills/tree/main.

## Required routing

Before approving, choose the matching entries from the active Android profile's
`chrisbanes_skills.review` list. Resolve each `skill` through
`chrisbanes_skills.search_paths` and read the local `SKILL.md` file as plain
text.

Use at least one matching skill for Android/Kotlin/Compose/KMP diffs when local
content exists. Do not parse upstream frontmatter through the native skill
loader. If the local file is missing, unreadable, or produces a YAML parse
error, record `no content: <skill>` under Verification Gaps and continue with
`android-code-review` and `android-guides`.

## Review focus

1. Compose state and effects: state ownership, hoisting, state-holder/UI split,
   effect keys, event collection, and one-shot event handling.
2. Compose performance: recomposition scope, stability, deferred frame-rate
   reads, strong skipping compatibility, and jank risk.
3. Compose UI APIs: modifier placement, slot APIs, animation correctness,
   focus navigation, semantics, and UI test coverage.
4. Kotlin concurrency and Flow: structured concurrency, cancellation, blocking
   boundaries, `StateFlow`, `SharedFlow`, `Channel`, `stateIn`, and event loss.
5. KMP and domain types: expect/actual boundaries, platform interop shape,
   `@JvmInline value class` suitability, and Compose stability implications.

## Output

Use the standard review angle output. Cite local skill paths used in Calibration.
If none were readable, cite `no content: <skill>`.
