# Review Angle — Android Skills

Compatibility alias for older profiles. Use
`templates/_shared/review/android-skills.md` as the canonical review angle.

Before approving Android, Kotlin, Jetpack Compose, or KMP changes, read only
matching entries through the leader checkout's `.agent-flow/skills/index.json`.
Codex, Claude, and OMP must use the same indexed project snapshot and tree hash.
Never fall back to host-global paths. If a required snapshot is missing or
changed, request changes and cite the indexed project paths used in Calibration.
End review artifacts with `android-local-skills: checked`,
`android-local-skills-used: <skill list>`, `chrisbanes-skills: checked|n/a`,
and `chrisbanes-skills-used: <skill list or n/a>` in `## Completion Gate`.
