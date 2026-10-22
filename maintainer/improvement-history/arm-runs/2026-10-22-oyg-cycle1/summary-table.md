| arm | assigned runs | success | one-sided Fisher p vs A | overclaim (upper 95%) | declared complete/blocked/undeclared/unmeasured | contaminated / infra error / timed out (counted as failures) | harness errors (excluded) | output tokens (mean) | input uncached+cached (mean) | seconds (mean) | reviewer jobs (mean) | rule violations | isolation | runs that ran gates / deferred a gate to CI (never run) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A | 5 | 1/5 | - | 0.800 (0.995) | 5/0/0/0 | 0 / 0 / 0 | 0 | 57,604 | 1,330,320 | 651 | - | 0 | instructed | - |
| C | 2 | 0/2 | 1.000 | - (-) | 0/2/0/0 | 0 / 0 / 0 | 5 | 237,788 | 51,548,078 | 4,001 | 40 | 0 | instructed | 0 / 0 |

Harness errors (re-run under the fixed harness, not in the denominator):

- C-1: controller inherited the operator session env (CLAUDE_CODE_SESSION_ID); stopped by the operator
- C-1: auto-mode classifier blocked phase approval; harness approver not yet in place
- C-1: kit installed with architecture pending; runner stopped on architecture_policy_drift
- C-1: Bash cwd reset outside the allowed dirs; runner worktree not added with --add-dir
- C-1: acceptEdits blocked writes to the protected .git path
