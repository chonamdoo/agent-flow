# Review Angle — Android Skills

Compatibility alias for older profiles. Use
`templates/_shared/review/android-skills.md` as the canonical review angle.

Before reviewing Android, Kotlin, Jetpack Compose, or KMP changes, use the skill
facts the phase prompt already resolved against **your** host — required ones with
absolute paths, in-scope ones by name, and anything not installed here named as
not installed. Do not re-resolve host paths or load another host's copy. Cover
Compose state/effects, recomposition and stability, modifier/layout/slot APIs,
focus, animation, Compose UI testing, Kotlin coroutine and Flow ownership, KMP
boundaries, and value class fit. A missing skill is a Calibration coverage gap,
not a finding: record `skill-availability: degraded`, cite paths actually read,
and keep the verdict on the code.

Emit exactly one unfenced final verdict line: `verdict: approve` or `verdict: request-changes`.
