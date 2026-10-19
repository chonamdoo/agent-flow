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

The official paired release decision is recorded separately by `phase_eval_compare.py` under `evals/release-gates/v0.3.18/` only if the fresh complete measurement passes.

## Limits

Installation checks use synthetic projects, not real application approvals. Host registration files were checked, but native host trust/startup and a user Homebrew runtime upgrade were not exercised. Full-suite, minimum-Python, installed-parity, frozen-invariant, and phase-budget gates remain PR CI responsibilities. Raw model responses contain local paths and are retained privately; committed result files contain compact measurement/provenance data.
