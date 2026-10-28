# pstack `setup-pstack` — summary of skills/setup-pstack/SKILL.md, read 2026-10-23 (operator's summary; secondary source)

Source: https://github.com/cursor/plugins/blob/main/pstack/skills/setup-pstack/SKILL.md

- Writes one rule file (`~/.cursor/rules/pstack-models.mdc`, alwaysApply) mapping roles to model slugs; every pstack skill reads it, and a deleted line falls back to the skill default.
- Default mapping (budget `large`): coding roles (feature, refactoring, bug-fix, perf-issue, hillclimb), "how explorer", "why investigators", "reflect tooling", "swarm workers" -> grok-4.7-xhigh-fast; "judgment and prose", "hardest tasks", "how explainer", "why synthesizer", reflect judgment/synthesizer -> claude-opus-5-5-xhigh; panels (arena runners, architect runners, interrogate reviewers, arena cross-judge pool) list both families. The cross-judge picks a family different from the parent when possible.
- `inherit-parent`/`auto` = run on the parent chat model. Budgets unlimited/large/medium/small only change the effort token (max/xhigh/high/medium) on the same slugs.
- Validation: never write a slug not confirmed available in the session.
- The document gives no cost, quality or speed rationale, claims no measured effect, and describes no step where a stronger model checks a cheaper model's work (apart from the cross-family judge pool).
