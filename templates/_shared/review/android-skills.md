# Review Angle — Android Skills

Review Android, Kotlin, Jetpack Compose, and KMP changes against the Android
profile's required review skills and the skills the phase prompt matched to this
change.

## Required routing

The phase prompt already resolved this run's skills against **your** host: required
ones are listed with the absolute path you can open, in-scope ones by name, and
anything not installed here is named as not installed. Treat that list as the fact —
do not re-resolve it, do not construct host home-directory paths, and do not search
another host's installation. Use an in-scope skill only when the diff actually touches
it.

Read selected `SKILL.md` files as plain text. Do not install, copy, link, or vendor
Android skills. A skill the prompt reports as not installed is not a finding: record
`skill-availability: degraded` and judge the change with the skills you do have. Never
make absence a verdict — the code under review cannot fix this machine's installation.
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
   in `model`, conversions in `mapper`, transport-failure to domain-error
   translation in repository/data mappers, and domain-error to `ErrorUiModel`
   translation in presentation mappers. Judge these against the typed
   error/result abstraction the project actually declares; when it declares
   none, the existing `Result`/exception contract is the contract and a missing
   abstraction is not a finding. For app-wide common errors, verify that
   AppShell owns common dialog/snackbar/toast hosts and root navigation; feature
   ViewModels only notify common errors and do not hold `NavController`,
   `Context`, dialogs, or login-flow stack reset logic.

## Output

Use the standard review angle output. Cite local skill paths used in Calibration.
When the prompt reported a required skill as not installed here, name it in
Calibration as a coverage gap and keep the verdict on the code.

The run's own `skill-availability` and `skill-use-evidence` markers already
record which required skills were resolved and opened, so this angle adds no
marker of its own.

Emit exactly one unfenced final verdict line: `verdict: approve` or `verdict: request-changes`.
