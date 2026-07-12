# Review Angle — Android Skills

Review Android, Kotlin, Jetpack Compose, and KMP changes against the active
Android profile's `android_skills` and `chrisbanes_skills`.

## Required routing

Before approving, choose matching entries from both profile lists when relevant.
Resolve each entry through `.agent-flow/skills/index.json` in the leader checkout
and read only the indexed project snapshot. Codex, Claude, and OMP must use that
same path and tree hash. If a required snapshot is missing or its hash differs,
stop approval and report `missing local android_skills: <skill>` or
`missing local chrisbanes_skills: <skill>`. Never fall back to host-global paths.
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
   mappers. For app-wide common errors, verify that AppShell owns common
   dialog/snackbar/toast hosts and root navigation; feature ViewModels only
   notify common errors and do not hold `NavController`, `Context`, dialogs, or
   login-flow stack reset logic.

## Output

Use the standard review angle output. Cite indexed project skill paths used in Calibration.
If a required snapshot is missing, request changes with the missing skill.

Include this completion gate:

```text
## Completion Gate
android-local-skills: checked
android-local-skills-used: <skill list>
chrisbanes-skills: checked|n/a
chrisbanes-skills-used: <skill list or n/a>
```
