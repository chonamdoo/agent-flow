# Sources and applicability

Authored 2026-10-20 for Agent Flow. Rules below are labelled by where they come from, so a project can
weigh them against its own policy by authority, scope and currency.

| Rule | Source | Kind |
|---|---|---|
| A pass is an observed command and exit, not a written claim (rule 1) | Agent Flow verification policy: a result written by the party being verified does not prove green (regression ledger #102, #193); diagnosing-bugs `feedback-*` evidence contract | Project requirement |
| Judgment only adds failures; a finding needs a failing reproduction (rule 2) | tester-army/e2e bug-bash: a finding is confirmed when its repro fails with an assertion (Apache-2.0); chonamdoo/app-qa: a model may only add refusals | Practice in the cited tools, adopted as principle |
| Zero executed, skipped-only, flaky or unreadable is not a pass (rule 3) | chonamdoo/web-qa-skill result states; tester-army/e2e counts flaky as exit 0, which this skill deliberately does not follow | Practice, with a stricter choice |
| Missing environment is `blocked`, not a code failure (rule 4) | Agent Flow gate routing proposal (chonamdoo/agent-flow#382): an unavailable required gate blocks instead of entering the fix loop | Project requirement (proposed) |
| QA changes no code, tests or baselines (rule 5) | chonamdoo/web-qa-skill; the deliberate-visual-change exception is the skill author's inference | Practice; exception is author inference |
| Check the spec, not the implementation (rule 6) | Agent Flow PRD Spec Items as the definition of done | Project requirement |
| Prefer the project's own harness; narrowest command first | Skill author's inference from the references above | Author inference |

Unknown: no source here sets numeric thresholds (retries, time budgets, device matrix). Where a project
needs them, its own policy decides.
