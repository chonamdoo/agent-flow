# Common ownership and verification

## Select before judging

Main files receive FSD rules; BO files receive route rules and, within the declared form subtree, form rules. Shared packages are neutral capabilities, not an app layer. Do not require Main slices in BO or BO underscore segments in Main. Mixed reviews apply both contracts to their own files rather than merging their vocabularies.

Dependency direction, sibling isolation, and UI/data separation are shared intentions with different implementations: Main isolates slices; BO isolates route-private folders. UI primitives receive data and actions through props rather than fetching data or importing business behavior.

## Ownership and promotion

Keep app semantics in the owning app. Promote a capability to packages only when an actual second consumer needs the same responsibility; coincidentally sharing one value is insufficient. Here both apps consume `@fixture/http`: it owns JSON parsing, while each caller validates its own response meaning. It must not import app types, swallow parse errors, or manufacture fallback success. BO form draft validation remains separate from response decoding.

Discuss Main shared changes before editing them. Record new dependencies and moves across section ownership in the plan; do not treat CODEOWNERS presence as proof of review enforcement. Route folder names are URL contracts, not formatting opportunities. This fixture's neutral route prefix is intentional and does not authorize renaming a product route.

## Executed lint and review

`pnpm run lint` is the required pre-commit boundary gate. Run the changed apps and root against the same effective configuration and app-local TypeScript resolution. Resolve paths from an explicit stable base so root/app working directories do not change the result. A missing tool, unexecuted command, or zero inspected sources is not a pass.

After editing a rule, create a deliberate violation file, confirm the actual error, delete it, and confirm the valid tree passes again. Keep the diagnostic/rule identity and nonzero exit as evidence; a source-text assertion or fabricated result is insufficient. A required lint failure must block success rather than being aggregated as green.

For source changes, report `lint-boundaries: pass|fail` with actual error count and execution evidence. Never substitute `lint-boundaries: n/a`. If execution is deferred to the integration owner, state that it was not run; do not manufacture a marker value. Record the routed documents under `project-local-skills-used`. App selection, cross-app rule mixing, and neutral package vocabulary are review obligations.

Lint decides detectable edges. Review still decides whether a public surface is coherent, UI knows business policy, shared/entity promotion has a real reason, and an exception matches its documented root cause. Filename counts and successful document delivery do not settle those questions.

## Observed policy versus new implementation

The original written rules described new violations as errors and a fixed, shrinking legacy allowlist as warnings. No allowlist contents were observed, so this fixture restores none and treats its new violations as errors. The original text also described root Turbo/app lint agreement and restricted-path exception constraints; the synthetic single-config implementation must prove working-directory agreement without claiming that unseen Turbo or zone settings were copied.
