# Review Angle — Android Skills

Compatibility alias for older profiles. Use
`templates/_shared/review/android-skills.md` as the canonical review angle.

Before approving Android, Kotlin, Jetpack Compose, or KMP changes, read only
matching local entries from the active Android profile's `android_skills` and
`chrisbanes_skills` for the current host. Do not load duplicate skills from
other host paths. If a required local skill is missing, request installation from
the profile source URL. Current host paths are Codex
`~/.codex/skills/{skill}/SKILL.md`, Claude
`~/.claude/skills/{skill}/SKILL.md`, and Antigravity
`~/.agents/skills/{skill}/SKILL.md`. Cite the skill paths used in Calibration.
End review artifacts with `android-local-skills: checked|n/a` and
`android-local-skills-used: <skill list or n/a>` in `## Completion Gate`.
