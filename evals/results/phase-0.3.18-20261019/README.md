# v0.3.18 release verification

## Scope

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

## Final measurement: release blocked

`attempt-2-failed/` preserves the fresh complete 168-row measurement with the final reviewed evaluator (`d26712dacdb315eedec88cb900c8a1d5c7a7434bd25a5b438d497278c0eef3ac`). Review accuracy is `40/40` for both kits; defect detection, author pass rate, and required reading are all 1.000. The gate still fails: 32 valid paired author units consumed `1,937,356 → 2,059,003` uncached input tokens, ratio `1.0627902151`, exceeding `1.05`. Output-token ratio is `1.033801`; four after-kit author units reported provider capacity errors and remain recorded.

The comparator correctly uses only units valid in both kits. The declared author documents have identical content hashes, and after-kit author prompts are 12–20 bytes smaller. No kit/evaluator defect causing the token increase has been established; these facts do not justify dismissing the regression. No unchanged rerun, threshold relaxation, selective rescoring, or passing release export was performed.

Published source commit `7cbc3c4` passed the full PR CI, including all four pytest shards, minimum Python, installed parity, frozen invariants, and phase-budget comparison ([Tests](https://github.com/chonamdoo/agent-flow/actions/runs/39182007930), [Parity](https://github.com/chonamdoo/agent-flow/actions/runs/39182007931)). Source review/CI approval is not a passing release measurement. Merge/tag/deployment and deployment-conditional cleanup remain unperformed.

After this measurement the kit gained #371 (bound own `abort` recovery) and the shared launcher check that now also guards `spec approve`/`spec confirm`. Both failed attempts therefore describe an earlier kit. The release decision needs a new paired measurement of the final kit, run in the cloud, and a passing `phase_eval_compare.py --export evals/release-gates/v0.3.18` record before tagging.

## Limits

Installation checks use synthetic projects, not real application approvals. Host registration files were checked, but native host trust/startup and a user Homebrew runtime upgrade were not exercised. Full-suite, minimum-Python, installed-parity, frozen-invariant, and phase-budget gates remain PR CI responsibilities. Raw model responses contained local paths; at the user's request they and the local scratch trees were deleted after compact extraction. Only the committed compact measurement/provenance files remain.
