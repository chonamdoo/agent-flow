# v0.3.19 release verification

The paired release gate passed for candidate `4da1c7c012dd37465850dbeb28c99e64682f2243`
against v0.3.18 (`8d94016442e9456e5875d0c558d26b13ccf21ebe`). The candidate contains
#375's RED evidence-window preservation during norm scope reentry and the v0.3.19
metadata. The evaluator, cases, thresholds and runtime were unchanged during measurement.

Codex CLI 0.159.2 requested `gpt-6.1-sol` for both roles, with author 3 trials,
review 2 trials and concurrency 8. All 168 unique units are retained: author 72 and
review 96 across four stacks, three modes and two independent kit snapshots.
This directory uses the client date in Asia/Seoul; original raw timestamps remain UTC.

All thresholds passed. Author required reading and behavior/plan/norm pass rates
are 1.000 on both kits. Scored review accuracy is 42/44 before and 43/43 after;
valid defect detection is 1.000. The candidate's first app/clean/defect review
could not install within 600 seconds, so it is retained as invalid without native
model calls. It was not removed or selectively retried. The normal second trial
completed. Candidate defect coverage is 23 valid units of 24 scheduled defect units.
The before kit's two false positives occurred on web clean/local clean diffs in
trial 2. Four expected=null units per kit remain outside accuracy denominators.
Architecture-lint has 12 pass and 24 n/a author results per kit; n/a is not a pass.

Author uncached-input/output ratios are 0.975/0.940; review ratios are 1.009/1.014.
Only pairs valid on both kits contribute. Missing model usage was not estimated;
all 610 executed calls exited successfully and supplied usage. The invalid
preparation explains the six unexecuted reviewer calls out of 616 planned calls.

[integrity-audit.json](integrity-audit.json) verifies every native command receipt,
prompt/stdout/stderr SHA-256 and observed usage against the original rows, and
checks that exported compact rows preserve the original measurements. The model
request flag is verified; observed model identity is not inferred. Native evidence
is retained in a private Git runtime archive whose SHA-256 is recorded in the audit.
Public records omit native text and local path prefixes. Ignored dependencies and
Next build caches were removed only from completed successful scratch projects to
recover disk space; project source, Git history and native evidence were preserved.

[package-verification.json](package-verification.json) retains the candidate
preparation's package and six-combination fresh/reinstall checks. Candidate and
evaluator fingerprints and all three artifact hashes were rechecked. Installed
parity passed again from a clean fixture. The installation matrix was not rerun
during this measurement. Native host trust/startup and Homebrew upgrades remain
unverified. Full-suite and phase-budget checks remain PR CI responsibilities.

The initial CLI 401 was resolved through user-completed device login before this
measurement. A successful actual model probe preceded the run. No authentication
failure is included as a scored trial. This record establishes release readiness;
tag creation and publication are separate operations.
