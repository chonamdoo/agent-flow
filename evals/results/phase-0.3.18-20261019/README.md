# v0.3.18 release verification

## Current release candidate: gate passed

The final merged kit `02164aa` (#370–#373) passed a new paired measurement against `v0.3.17`. The user transferred release preparation back from the cloud to this local session. This run used fresh tracked-file archives, the unchanged final evaluator and thresholds, Codex CLI `0.163.0`, requested model `gpt-6.1-sol`, author 3 trials, review 2 trials, and concurrency 8. It did not reuse or rescore either failed attempt.

The complete 168-unit record is in [final-kit-02164aa/](final-kit-02164aa/). Author uncached-input and output ratios are `0.983` and `0.982`; review ratios are `1.006` and `0.995`. Three review units were invalid because 13 native reviewer calls timed out. These units remain recorded and were not retried. `expected=null` reviews remain outside accuracy denominators, and architecture-lint `n/a` is not a pass.

Twelve of the 13 timeout calls belong to the candidate kit's web/clean and web/local defect units (all six reviewers in each first trial); the remaining timeout belongs to before web/clean/clean. Candidate defect detection therefore covers 22 of 24 defect units. Invalid units and their counterparts do not contribute to the paired review token ratio, so the passing ratio is not evidence of lower total cost including timeouts. Unobserved timeout usage is not estimated.

The [release record](../../release-gates/v0.3.18/) contains the compact rows used by the release workflow. The integrity audit matched all 616 original call receipts, prompt/stdout/stderr hashes, and observed usage against the exported rows. The public files contain no local path prefixes; original native evidence is preserved in the private Git runtime archive. Package verification rebuilt the npm tarball, wheel, and sdist from a separate unused archive, exercised both installers in clean/local/pending mode with fresh/reinstall checks, and ran the installed launcher and wheel CLI. It does not prove native host trust/startup or Homebrew upgrade behavior.

This is release preparation evidence, not tag or deployment completion. The earlier failures and their original conclusions are preserved below.

## Earlier source scope (#370)

PR #370 preserves case-sensitive regression selectors and confines installed SPEC recorders to the bound checkout/run. Release preparation changes the version declarations to `0.3.18` and repairs review-fixture execution evidence in `phase_eval.py`.

The evaluator now discovers the actual backend unittest inventory before executing those exact test IDs. Import/discovery errors and empty suites retain the original discovery command and its failure result. Flutter records the actual fixture test paths. Both clean and defect variants use the same preparation algorithm; case files, expected verdicts, oracles, and thresholds are unchanged.

## Failed measurement retained

`attempt-1-failed/` preserves all 168 compact rows from the first complete paired measurement, including invalid units and `expected=null` units. Its gate failed: review accuracy was `35/38 → 34/38`, and the author uncached-input token ratio was `1.110`. Capacity failures invalidated six review units per kit and one after-kit author unit. This is not a passing release record.

Review responses exposed missing named-test evidence in the prepared execution record. A proposed static selector list was rejected during independent Claude review because defect fixtures intentionally omit tests. The final fix derives selectors from the actual suite and preserves its full inventory. The fresh full measurement uses the corrected evaluator; old units are not rescored or selectively retried. The token increase has no established production-code cause and is not claimed fixed by the evidence correction.

## Executed checks

- Production changes and evaluator invariants: 55 focused cases passed before the final inventory correction. After that correction, 22 explicitly selected cases passed, including six real backend preparations covering every mode and clean/defect variant. These runs do not replace full PR CI.
- `review-evidence-all-variants-smoke.json`: 12 actual backend/Flutter preparations passed. Backend discovery and named execution retained identical inventories; Flutter named every fixture test file.
- `review-evidence-edge-smoke.json`: actual import-error and empty-suite forks retained exit codes 1 and 5, respectively. Preparation itself records these outcomes rather than turning them into successful tests.
- `package-verification.json`: built npm tarball, wheel, and sdist; both installer entry points passed fresh/reinstall checks in clean/local/pending projects. User-owned files, host registration files, launcher receipts, installed SPEC commands, drift/run rejection, and exact-case pytest evidence were exercised.
- Actual Claude and Codex CLI reviews independently approved the final source. Claude reported `claude-opus-5-5`; Codex was requested as `gpt-6.1-sol` but did not report its observed model identity. The earlier static-selector finding was explicitly resolved in fresh reviews.

## Second measurement: release blocked

`attempt-2-failed/` preserves the fresh complete 168-row measurement with the final reviewed evaluator (`d26712dacdb315eedec88cb900c8a1d5c7a7434bd25a5b438d497278c0eef3ac`). Review accuracy is `40/40` for both kits; defect detection, author pass rate, and required reading are all 1.000. The gate still fails: 32 valid paired author units consumed `1,937,356 → 2,059,003` uncached input tokens, ratio `1.0627902151`, exceeding `1.05`. Output-token ratio is `1.033801`; four after-kit author units reported provider capacity errors and remain recorded.

The comparator correctly uses only units valid in both kits. The declared author documents have identical content hashes, and after-kit author prompts are 12–20 bytes smaller. No kit/evaluator defect causing the token increase has been established; these facts do not justify dismissing the regression. No unchanged rerun, threshold relaxation, selective rescoring, or passing release export was performed.

Published source commit `7cbc3c4` passed the full PR CI, including all four pytest shards, minimum Python, installed parity, frozen invariants, and phase-budget comparison ([Tests](https://github.com/chonamdoo/agent-flow/actions/runs/39182007930), [Parity](https://github.com/chonamdoo/agent-flow/actions/runs/39182007931)). Source review/CI approval was not a passing release measurement. At that attempt, merge/tag/deployment and deployment-conditional cleanup were unperformed.

After that measurement the kit gained #371 (bound own `abort` recovery), #372 (launcher/bare equivalence and CLI checkout-target checks), and #373 (literal shell keyword and pattern-position fixes). Both failed attempts describe an earlier kit. The new final-kit measurement and passing export are recorded above; the failure of the earlier kit has not been relabeled as a pass.

## Limits

Installation checks use synthetic projects, not real application approvals. Host registration files were checked, but native host trust/startup and a user Homebrew runtime upgrade were not exercised. Full-suite, minimum-Python, installed-parity, frozen-invariant, and phase-budget gates remain PR CI responsibilities. For attempt-1 and attempt-2 only, raw model responses and local scratch trees were deleted at the user's request after compact extraction; only their committed compact measurement/provenance files remain. The final-kit-02164aa run's original native evidence is separately preserved in the private Git runtime archive.
