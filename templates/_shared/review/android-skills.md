# Review Angle — Android Skills

Review Android/Compose and Android-targeted Kotlin/KMP changes against the Android
profile's required skills and the phase prompt's matched scope. Kotlin/JVM server
or Ktor client-only code does not become Android work from its file extension.

## Required routing

The phase prompt already resolved this run's skills against **your** host: required
ones are listed with the absolute path you can open, in-scope ones by name, and
anything not installed here is named as not installed. Treat that list as the fact —
do not re-resolve it, do not construct host home-directory paths, and do not search
another host's installation. Use an in-scope skill only when the diff actually touches
it.

Read selected `SKILL.md` files as plain text. Do not install, copy, link, or vendor
Android skills. Apply `code-generation-discipline` **Missing Required Skills**.
If no Android-targeted platform/presentation work is in scope, use the active
phase's applicable `n/a` markers rather than reviewing unrelated Kotlin as Android.

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
7. Data/error boundaries: apply the selected architecture contract and its
   required Android skills. In Clean mode, the required core owns Repository And
   Source Boundary, Mapping Boundary, and Error Boundary, including the recorded
   simple-adapter and existing `Result`/exception alternatives.
   For app-wide common errors, apply the required
   `android-appshell-error-handling` and its shared error contract for host,
   notification, and root-navigation ownership. Android presentation owns
   ViewModel platform-object restrictions.

## Output

Use the standard review angle output and `code-generation-discipline`
**Missing Required Skills** for Calibration evidence.

The run's own `skill-availability` and `skill-use-evidence` markers already
record which required skills were resolved and opened, so this angle adds no
marker of its own.

Emit exactly one unfenced final verdict line: `verdict: approve` or `verdict: request-changes`.
