# Review Angle — Android Skills

Compatibility alias for older profiles. Use
`templates/_shared/review/android-skills.md` as the canonical review angle.

Before approving Android, Kotlin, Jetpack Compose, or KMP changes, read the
skills the phase prompt matched to the change — required ones with paths,
in-scope ones by name — covering Compose state/effects, recomposition and
stability, modifier/layout/slot APIs, focus, animation, Compose UI testing,
Kotlin coroutine and Flow ownership, KMP boundaries, and value class fit. Do not
load duplicate skills from other host paths. If a required local skill is
missing, request installation from the profile source URL. Current host paths
are Codex `~/.codex/skills/{skill}/SKILL.md`, Claude
`~/.claude/skills/{skill}/SKILL.md`, and OMP
`~/.omp/agent/skills/{skill}/SKILL.md`. Cite the skill paths used in Calibration.
