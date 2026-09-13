# Review Angle — Android Skills

Compatibility alias for older profiles. Use
`templates/_shared/review/android-skills.md` as the canonical review angle.

For Android/Compose or Android-targeted Kotlin/KMP changes, use the skill
facts the phase prompt already resolved against **your** host — required ones with
absolute paths, in-scope ones by name, and anything not installed here named as
not installed. Do not re-resolve host paths or load another host's copy. Cover
Compose state/effects, recomposition and stability, modifier/layout/slot APIs,
focus, animation, Compose UI testing, Kotlin coroutine and Flow ownership, KMP
boundaries, and value class fit. Apply `code-generation-discipline`
**Missing Required Skills** for unavailable skills and Calibration evidence.

Emit exactly one unfenced final verdict line: `verdict: approve` or `verdict: request-changes`.
