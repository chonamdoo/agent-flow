---
name: qa
description: Verify a finished change the way its user or API client meets it — web UI, mobile app or backend API — against the PRD acceptance criteria and Spec Items, with observed commands and evidence instead of claims. Use in a workflow `qa` phase, or when asked to QA, smoke-test, e2e-test or bug-bash a change. Not for writing unit tests, reviewing code, or debugging a known failure.
workflowPhases: [qa]
---

# QA

QA answers one question: does the changed behavior work for the person or client who uses it, as the
spec says? Tests that passed while the code was written are not that answer; the user-facing path is.

## Rules

1. **A pass is observed, not written.** It is the exact command, its exit code, and the evidence it
   produced. When the phase gives a `run-command` prefix (`agent-flow run-command -- <argv>`), run checks
   through it so the exit is recorded; without it, record the command and the exit you saw and mark the
   run evidence `unavailable`. `verified` in prose is not a pass.
2. **Judgment can only add failures.** An exploratory finding counts only when a reproduction command
   fails against this change. A finding you could not reproduce is listed as unverified, never as a pass
   or a failure.
3. **Not a pass:** zero tests executed, only skipped tests, a test that passed only on retry (flaky), or a
   run whose report you could not read.
4. **Missing environment is `blocked`.** A missing browser, simulator, device, service or tool is
   `status: blocked` naming exactly what is missing. Code cannot fix it, so it is not `request-changes`,
   and it is not green. Do not substitute unit tests and call the user path verified.
5. **QA changes nothing.** Do not edit code, tests or visual baselines to make QA pass, and do not run a
   baseline-update command. A missing test or a wrong baseline goes back as `request-changes`.
   Exception: when the task itself is a deliberate visual change, report the baseline difference as a
   review item rather than a failure — still without updating it.
6. **Check the spec, not the implementation.** Every acceptance criterion and Spec Item maps to a check,
   or is listed as not covered with the reason. If the change has no written criteria, check the changed
   behavior, say that no criteria were given, and do not invent requirements.

A project's own written QA policy overrides these rules where it is stricter or more specific.

## Procedure

1. From the changed files and the plan, list the surfaces this change reaches: web, app, API.
2. Read only the reference for each surface (table below).
3. Prefer the project's own e2e, integration or smoke command for that surface. Run the narrowest one that
   exercises the changed user path; add a focused check only when none exists.
4. Write the QA artifact with these sections, then any Completion Gate the phase requires:
   - `## Coverage` — acceptance criterion or Spec Item → check → pass | fail | not covered (why)
   - `## Commands` — exact argv and observed exit code
   - `## Evidence` — report, screenshot or trace paths the commands produced
   - `## Findings` — confirmed (with failing repro) and unverified
5. Status: `green` only when every applicable check passed under rule 1 and nothing confirmed failed;
   `request-changes` for any confirmed failure or uncovered required behavior; `blocked` per rule 4.

## References

| Read | When |
|---|---|
| [references/web.md](references/web.md) | The change reaches pages, components, routes or client state in a browser. |
| [references/app.md](references/app.md) | The change reaches React Native, Flutter, Android or iOS screens. |
| [references/backend.md](references/backend.md) | The change reaches an HTTP/RPC endpoint, a contract or a job other code calls. |
| [references/evidence.md](references/evidence.md) | You record reports, screenshots or traces, or a run was flaky, empty or unreadable. |
| [references/exploratory.md](references/exploratory.md) | You are asked to bug-bash, or no automated check covers a changed user flow. |
| [references/sources.md](references/sources.md) | A rule here conflicts with project policy, or you need to know where a rule comes from. |
