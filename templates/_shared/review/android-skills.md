# Review Angle — Android Skills

Review Android, Kotlin, Jetpack Compose, and KMP changes against the active
Android profile's `android_skills` and `chrisbanes_skills`.

## Required routing

Before approving, choose matching entries from both profile lists when relevant:

- `android_skills.review`: upstream Android skills from
  `.agent-flow/vendor/android-skills/`
- `chrisbanes_skills.review`: Compose/Kotlin skills from the first readable
  configured path, falling back to `.agent-flow/vendor/chrisbanes-skills/skills/`

Read selected `SKILL.md` files as plain text. Do not copy upstream Android
skills into `.codex/skills`, `.claude/skills`, `.gemini/skills`, or
`.gemini/antigravity/skills`, and do not load the same skill from multiple host
paths.

## Review focus

1. Android platform guidance: edge-to-edge, adaptive Compose, Navigation 3,
   testing setup, R8, Perfetto, Play integrations, Android CLI, and XR when
   relevant to the diff.
2. Compose state and effects: state ownership, hoisting, state-holder/UI split,
   effect keys, event collection, and one-shot event handling.
3. Compose performance: recomposition scope, stability, deferred frame-rate
   reads, strong skipping compatibility, and jank risk.
4. Compose UI APIs: modifier placement, slot APIs, animation correctness,
   focus navigation, semantics, and UI test coverage.
5. Kotlin concurrency and Flow: structured concurrency, cancellation, blocking
   boundaries, `StateFlow`, `SharedFlow`, `Channel`, `stateIn`, and event loss.
6. KMP and domain types: expect/actual boundaries, platform interop shape,
   `@JvmInline value class` suitability, and Compose stability implications.

## Output

Use the standard review angle output. Cite local skill paths used in Calibration.
If none were readable, cite `no matching android_skills` or
`no matching chrisbanes_skills`.
