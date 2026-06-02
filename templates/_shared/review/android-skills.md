# Review Angle — Android Skills

Review Android, Kotlin, Jetpack Compose, and KMP changes against the active
Android profile's `android_skills` and `chrisbanes_skills`.

## Required routing

Before approving, choose matching entries from both profile lists when relevant.
Resolve them through the current active host only:

- Codex: `~/.codex/skills/{skill}/SKILL.md`
- Claude: `~/.claude/skills/{skill}/SKILL.md`
- Antigravity: `~/.agents/skills/{skill}/SKILL.md`

Read selected `SKILL.md` files as plain text. Do not install, copy, link, or
vendor Android skills. Do not load the same skill from multiple host paths. If a
required local skill is missing from the current host path, stop approval and
report `missing local android_skills: <skill>` or
`missing local chrisbanes_skills: <skill>` with the profile source URL.
If no Android/Kotlin/Compose/KMP files changed, mark the completion gate `n/a`.

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
7. Data/error boundaries: `api` for Retrofit services, `source.remote` and
   `source.local` for data source implementations, DTO/request/response models
   in `model`, conversions in `mapper`, `NetworkFailure -> AppError` in
   repository/data mappers, and `AppError -> ErrorUiModel` in presentation
   mappers.

## Output

Use the standard review angle output. Cite local skill paths used in Calibration.
If a required local skill is missing, request changes with the missing skill and
source URL.

Include this completion gate:

```text
## Completion Gate
android-local-skills: checked|n/a
android-local-skills-used: <skill list or n/a>
```
