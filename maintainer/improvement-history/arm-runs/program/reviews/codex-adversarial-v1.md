# Adversarial review of PLAN.md

The proposed program can produce exploratory observations, but it cannot support Q1–Q3 as written. It combines adaptive workflow selection, underpowered comparisons, an unvalidated quality instrument, and a repeatedly reusable holdout. Keeping the evaluator hidden does not resolve those problems. The existing outcomes also do not establish that C is worse: task difficulty, tiny samples, and termination policy can explain them.

Severity: **blocker** means the proposed inference would be invalid; **high** means a plausible failure could reverse the result; **medium** means a material measurement or interpretation defect.

## Findings

### F01 — blocker — The budget cannot buy the proposed evidence

**Claim attacked:** §2's ~$150 cycle can explore variants and input layers, then “confirm” the best 1–2 on holdout; §4 supports comparative conclusions.

**Failure scenario:** One A/B/C set costs $30 before evaluation. On the four named development tasks that is $120 for only one run per arm per task. Four goal-layer simulations add $12–36 if all 12 runs interview, leaving $18 or exceeding the budget before variants, judges, retries, and holdout. Even one task with three repeats of each arm costs $90, leaving little for discovery and validation. Judge cost grows with eight axes, two judges, pair ordering, and comparisons; all-pairs comparisons of k outputs require k(k−1)/2 pairs. The plan budgets none of this explicitly.

**Fix:** Treat $150 as a feasibility pilot. Price a complete schedule before execution, including evaluation, failed runs, simulator, orchestration, context replay, and holdout. First cut adaptive variant search, then exhaustive pairwise judging and eight-axis scoring. Select one candidate and a small number of outcomes. Additional tasks/repeats require a separate budget; do not trade them away to retain every metric.

### F02 — blocker — The experimental unit and target population are undefined

**Claim attacked:** §4 Q1 uses S and Fisher's test; quality uses condition differences and confidence intervals.

**Failure scenario:** Twenty-eight checkout checks are counted as 28 independent observations although they share a single implementation and failure cause. Repeated seeds on one task are interpreted as evidence across projects. Cart v0/v1 are treated as independent tasks despite shared code and requirements. Pooling task-specific H denominators changes the result when one task merely has more checks.

**Fix:** Define H and S, the independent unit, target task distribution, weighting, and estimand before running. Pair arms on identical clean task snapshots and block by task; randomize execution order. Separate within-task stochastic repeatability from generalization across tasks. Do not use checks, functions, or judge calls as independent run samples. Fisher's test needs an independence justification; a paired design needs an appropriate paired analysis. With very few task clusters, report task-level outcomes and uncertainty without population claims.

### F03 — blocker — Five-percentage-point non-inferiority is not credible at this scale

**Claim attacked:** §4 Q2 declares H non-inferior at −0.05 and every quality axis non-inferior at preregistered margins.

**Failure scenario:** C and Sx both pass one checkout run; the study declares no meaningful loss. A real 10–20 point loss is entirely compatible with these observations. If H is a check fraction, correlated checks create deceptively tight intervals. On several quality axes, passing a margin test can also be bought by choosing a permissive margin after observing the baseline.

**Fix:** Define task/run-level estimands and justify margins by acceptable user harm, independently of observed results. Calculate sample needs under plausible baseline success and variance. Require the appropriate one-sided confidence bound to clear each margin; “not significantly different” is not non-inferiority. If infeasible, drop confirmatory non-inferiority and report exploratory differences with intervals. Prespecify the joint all-axes decision and error control rather than treating separate intervals as joint coverage.

### F04 — high — Many outcomes and adaptive looks defeat nominal error control

**Claim attacked:** §4's per-axis 95% intervals and multiple-comparison correction address chance differences.

**Failure scenario:** Researchers try several S variants, inspect maxima, p90s, eight ratings, two input layers and multiple tasks, then report the favorable subset. Correcting only the final A/B/C table ignores selection and repeated looks. Eight related principle scores are presented as eight independent confirmations.

**Fix:** Preregister one primary functional endpoint and a small safety/quality gate set, contrasts, aggregation, missing-data handling, and stopping rule. Label the remaining axes exploratory. Define the multiplicity family across contrasts and outcomes, and distinguish adjusted inference from descriptive intervals. Reserve fresh data for the selected workflow; final-table correction cannot repair adaptive selection on the same data.

### F05 — blocker — Noisy Pareto selection and one holdout run cannot confirm a winner

**Claim attacked:** §2 accepts variants by Pareto dominance and confirms the top 1–2 once per cycle on mobile-checkout.

**Failure scenario:** A variant wins because its one run is cheap and its judge ratings are lucky. More dimensions make many variants mutually nondominated, so an unstated human preference decides acceptance. Checkout is inspected each cycle; subsequent variants encode its failures, turning the supposed holdout into development data. One successful run offers almost no estimate of reliability, and one holdout task cannot establish cross-task generalization.

**Fix:** Freeze an archive/selection rule, primary gates, uncertainty treatment, and tie-breaking before search. Repeat promising candidates against paired incumbents; require evidence of practically meaningful improvement, not point-estimate dominance. Use development data for search only. Lock a finalist before a genuinely untouched multi-task evaluation with repeats. Once used to guide changes, checkout is no longer a holdout. At $150, call the result a candidate-selection pilot and drop “confirmation.”

### F06 — high — Fix-loop caps change both treatment and opportunity to succeed

**Claim attacked:** §2 searches caps 2/3/5; §1–4 attribute differences to workflow or reduction.

**Failure scenario:** C stops at three while Sx gets five and completes. The result is reported as superior phase design although it bought extra repair attempts. Conversely, a common short cap disproportionately penalizes a review-heavy arm whose early work is deliberately slower. Excluding cap stops from analysis makes C look artificially reliable.

**Fix:** Separate the product-policy comparison (each workflow with its native cap, all stops counted) from a resource-matched comparison. Hold repair policy fixed for a phase ablation, or explicitly cross cap and workflow in a later experiment. Record termination reason, attempts, elapsed time, tokens, and intermediate correctness. Preserve the runner restriction; it limits attribution rather than authorizing unsupported causal claims.

### F07 — high — Reviewer count is a resource allocation treatment

**Claim attacked:** §2 varies 2/5/10 reviewers and their perspectives; Q2 seeks a cheaper workflow preserving quality.

**Failure scenario:** Ten reviewers improve results because they receive many more model calls. Or they lose because duplicate findings exhaust the fix budget. Changing count and perspectives together makes diversity, review volume, and cost indistinguishable. “Multi-review works” then overstates a product-level cost/quality observation.

**Fix:** Report native-cost product comparisons as such. For mechanistic claims, compare review policies at matched token/dollar or time budgets, fixing perspectives and repair opportunity where possible. Track unique actionable findings, false alarms, accepted fixes, and regressions introduced by review. Defer this ablation if budget cannot support it.

### F08 — high — Q3 confounds interviewing with information access and the rest of the workflow

**Claim attacked:** §4 compares goal conditions with and without grilling.

**Failure scenario:** The interview arm receives acceptance criteria from the hidden PRD, while the other arm has no means to know them. Higher H measures access to answers, not better planning. If grilling occurs only in C, its other phases further confound the comparison. Scoring unspecified requirements as failures punishes reasonable implementations.

**Fix:** Define the intended estimand: the total effect of allowing questions is valid but must be named that way. Toggle interview access within one frozen workflow with matched budgets. Include a fixed-facts transcript or standardized supplied-facts control to distinguish information acquisition from reasoning. Specify acceptable behavior for genuinely undecided facts and test goal-only tasks whose success criteria follow from available information.

### F09 — high — A PRD-backed simulator can act as an answer oracle

**Claim attacked:** §2's simulator answers only facts in the hidden PRD and reports a fact-recovery ratio.

**Failure scenario:** “Anything else I should know?” elicits the entire PRD, including hidden acceptance-test details. Leading questions receive helpful confirmations; repeated paraphrases obtain different facts. The simulator also invents decisions or reveals implementation hints embedded in the PRD. A workflow optimizing broad questions appears to conduct excellent interviews.

**Fix:** Separate user-visible facts, undisclosed but askable facts, undecided choices, and evaluator-only assertions. Use a versioned fact ledger with deterministic retrieval/answer rules where possible, fixed response budgets, and answer provenance. Audit transcripts for forbidden disclosures and invented facts. Score relevance and necessary fact coverage, not raw question count or an unweighted extraction ratio. Freeze the simulator and include its stochasticity and cost in the treatment.

### F10 — high — Hidden evaluation does not prevent metric gaming or harness drift

**Claim attacked:** §3 evaluates unseen copies; §5 says hiding the evaluator prevents gaming.

**Failure scenario:** Workflow authors know the stated metrics and extract trivial helpers to lower function length, wrap parameters in a bag, or write boilerplate to please judges. Across cycles, feedback reveals the private tests. An agent edits tests, lint configuration, architecture rules, or package scripts so the evaluator reports green. Different arm overrides silently fail to load, so the nominal treatment is not applied.

**Fix:** Freeze evaluator code/configuration and hidden tests outside the writable checkout; apply trusted rules to artifacts. Validate actual workflow loading, phase execution, tool permissions and gate outcomes from external logs. Version model, harness, repository and dependencies. Audit exclusions, suppression comments, test deletion, baseline changes and generated-code marking. Rotate genuinely fresh tasks after feedback. Hidden metrics remain a defense in depth, not proof against gaming.

### F11 — high — The “deterministic” table mixes reproducible counts with invalid constructs

**Claim attacked:** §3a labels its measures quality axes; Q4 checks repeated scores for equality.

**Failure scenario:** A tool reliably counts shorter functions after mechanical splitting, and the study concludes SRP improved. Parser failures in Kotlin extension functions or JS arrows silently omit the worst functions. A fresh evaluator run is stable because it repeats the same extraction bug.

**Fix:** Pin tool/parser versions, rules and exclusions; validate supported Kotlin/JS syntax on fixtures and representative diffs. Report extraction failures and coverage, never zeros for missing analysis. Use enclosing symbols and relevant unchanged context, with baseline-to-result deltas and final absolute values. Reproducibility is necessary but does not establish construct validity. Adopt the following disposition:

| Proposed axis | Reproducible observation | Valid interpretation and disposition |
|---|---|---|
| H/S, build, existing tests | Fixed environment and test outcomes | Keep as bounded evidence of tested behavior; test adequacy and isolation need validation. |
| File/line counts, reference diff ratio | Pinned diff conventions | Keep descriptive only. Legitimate alternative designs and baseline size make these invalid scope/quality scores. Drop reference-size ratio as an acceptance gate. |
| Function length, complexity, nesting | Validated AST measurements | Keep as smell screens and outlier locators. They do not measure SRP, SLAP, maintainability or correctness. |
| Parameter counts | Counts including an explicit receiver/constructor policy | Keep descriptive. Four is not a universal boundary; parameter objects can hide unrelated state. Drop “fewer means better” and automatic class-wrapping rewards. |
| Clone rate | Fixed detector, token threshold and denominator | Keep as duplication candidates. Tiny diffs, generated code and necessary repetition distort rates. Cannot establish semantic DRY; inspect candidates in context. |
| Architecture imports/dependencies | Violations of frozen explicit boundaries | Keep hard rules where valid. Imports miss reflection, dependency injection, dynamic JS and semantic cross-layer coupling; supplement focused review. |
| Security lint | Matches to pinned rules | Keep findings with triage. A clean scan cannot establish input safety, authorization or absence of injection. |
| Lint/naming deltas | Fixed trusted baseline and rules | Keep for repository convention compliance only. A zero delta can preserve severe pre-existing violations. |
| False completion | A claim and evidence ledger | Keep, but replace the S=0 shortcut as described in F16. |

### F12 — high — Eight principle scores impose unsupported universal preferences

**Claim attacked:** §3b scores SRP, side effects, DRY, parameters, fail-fast, guard clauses, SLAP and explicit receivers from 0–3.

**Failure scenario:** Kotlin idiomatic implicit receivers are punished, JS event handlers lose points for necessary side effects, transactional code loses points for delayed validation, and clear repeated branches are penalized in favor of a brittle abstraction. SRP and SLAP need design context absent from a diff. Several scores reward the same superficial function splitting, multiplying its apparent evidence.

**Fix:** Do not use eight universal ordinal totals as acceptance gates. Retain SRP/cohesion and abstraction consistency as contextual expert findings with concrete maintenance consequences. Evaluate side effects as explicit ownership, isolation and contracts; fail-fast/guards as correct error handling and path clarity. Treat DRY as unjustified knowledge duplication. Parameter counts remain descriptive. Drop explicit-receiver preference as a cross-language quality axis; enforce it only if a frozen local rule requires it. Permit “not applicable” and “insufficient context,” and distinguish those from a good score.

### F13 — high — Removing arm names does not blind judges

**Claim attacked:** §3b calls condition-name removal “blind.”

**Failure scenario:** C produces characteristic tests, plan comments, helper names and review artifacts. A judge infers the arm or recognizes its own model's style, then rewards polished explanations. A longer diff appears more comprehensive; the same long diff may also be penalized simply because there are more places to find faults. Pair reversal does not remove these effects.

**Fix:** Hide provenance, costs and agent narratives; supply uniform task requirements, base-code context and artifact packaging. Exclude non-product phase logs from quality judgment while evaluating required documentation separately. Test whether independent raters can guess arm/model above chance and report leakage. Preserve semantically meaningful code rather than rewriting it to fake blinding. Call this provenance masking and qualify residual bias.

### F14 — high — Two judge brands and order reversal do not establish validity

**Claim attacked:** §3b uses Claude and Codex, two pair orders and Cohen's κ as validation.

**Failure scenario:** Both judges share the same superficial preference and agree on the wrong answer. Claude favors Claude-generated code; averaging it with a second judge does not cancel the interaction. Deterministic decoding repeats a biased judgment, while stochastic repeats disagree. Cohen's κ on ordinal 0–3 scores hides distance between disagreements and becomes unstable when nearly everything gets one rating.

**Fix:** Freeze exact judge models, prompts, context limits and decoding settings. Balance order, allow ties, collect repeated judgments on a planned subset, and retain disagreement. Cross-tabulate author-model × judge-model preferences where author provenance varies; if it does not, state that self-preference cannot be identified. Use ordinal weighted agreement with uncertainty for scores and separate statistics for pairwise choices. Validate against independent expert adjudication on a small stratified sample. Agreement and reproducibility are reliability evidence, not truth.

### F15 — high — Obvious injected mutants are an inadequate calibration set

**Claim attacked:** §3b uses deliberate bloat, copy-paste and nesting mutants to calibrate judges.

**Failure scenario:** Judges detect conspicuous defects but miss realistic hidden coupling. A mutant breaks behavior, so its rejection says nothing about structural quality. The suite has only bad mutants, permitting a judge that labels every change bad to “pass.” Calibration examples reused to tune prompts become another training set.

**Fix:** Build held-out, expert-labeled positive and negative controls: subtle realistic defects, harmless refactors, necessary side effects, appropriate duplication, idiomatic receiver use, and function splitting that worsens cohesion. Preserve behavior for structural mutants and verify that preservation. Measure false-positive rate, sensitivity by defect type, severity discrimination, and pairwise ranking. Separate rubric development from calibration validation. If calibration is weak, use judges only to surface findings for adjudication, not decide winners.

### F16 — high — The false-completion definition misses the user's actual complaint

**Claim attacked:** §3a measures completion declaration ∧ S=0 and claim-versus-evidence auditing.

**Failure scenario:** An agent says “all implemented and all tests pass” while 27 of 28 checks pass and one required payment path is absent. S is nonzero, so the shortcut misses the lie. Another agent honestly reports a blocked build and is lumped together with one fabricating test results. A required workflow template nudges C to make stronger final claims than A.

**Fix:** Operationalize claim auditing independently of aggregate S: extract atomic implementation, test-execution, test-result and scope claims; verify them against external tool logs, final artifacts and requirement evidence. Score unsupported claims, contradicted claims and omitted required work separately. Include partial completion, fabricated test runs, stale test results and unverified “done” claims. Preserve the final message and full external execution record for every arm, including timeouts. Measure accuracy as well as completion rate so an always-cautious answer cannot win by avoiding claims.

### F17 — high — Architecture and scope need requirement-based oracles

**Claim attacked:** §3a measures architecture through import rules and scope through unrelated modules and diff size.

**Failure scenario:** Code obeys dependency direction but puts checkout business rules in UI callbacks. Conversely, a legitimate cross-module change is branded scope creep. A reference solution's architecture becomes the only accepted architecture, favoring the arm that resembles it.

**Fix:** Freeze task-specific allowed boundaries and behavioral responsibilities before seeing outputs. Check semantic placement of business logic, state ownership and public API contracts alongside dependency rules. Map changed behavior to requirements and justified supporting work. Require evidence for unrequested behavior or needless dependency/configuration changes; use diff size as a review trigger only. Human-adjudicate ambiguous scope/architecture findings while masked to arm.

### F18 — high — Regression and security coverage is too shallow for safety claims

**Claim attacked:** §3a's existing tests, reused mutants and static rules extend evaluation to regressions/security.

**Failure scenario:** The agent weakens an existing test, introduces cross-user data exposure, or breaks an untested legacy flow while all named checks pass. Existing-behavior mutants test whether the tests can detect selected defects, not whether the submitted implementation preserved every old behavior. A scan misses business-logic authorization and dynamic injection.

**Fix:** Execute trusted original regression tests and hidden behavioral checks against the submission; track test changes separately. Use mutation testing to audit oracle sensitivity, not substitute for regression outcomes. Include task-relevant negative and boundary tests, authorization isolation, error paths and web injection checks in controlled local fixtures. Review added dependencies and insecure configuration. Separate confirmed vulnerabilities from static warnings, and say “no detected issue” rather than “secure.”

### F19 — high — Harness and environment effects can masquerade as workflow benefits

**Claim attacked:** §3 applies the same evaluator to all outputs, implicitly making the comparison fair.

**Failure scenario:** A benefits from cached dependencies, C runs later after a model update, or a reused checkout contains files created by another arm. C receives a larger context window, different tool access, or a different model routing policy. Evaluating only successful builds drops precisely the runs likely to have the worst quality.

**Fix:** Use clean isolated snapshots, pinned dependencies and model versions where available, equivalent tools and budgets appropriate to the stated estimand, randomized/block execution order, and a captured run manifest. Record external failures separately using a predetermined rerun rule. Analyze every assigned run; build failures and cap stops remain outcomes. Mark unscorable quality explicitly and report quality conditional on success alongside overall success, without selecting survivors as the whole population.

### F20 — medium — Diff-only evaluation rewards moving defects outside the denominator

**Claim attacked:** §3a examines changed functions/code and §3b supplies diffs.

**Failure scenario:** An agent delegates to an already bloated function without changing it, introduces a one-line call with a dangerous side effect elsewhere, or moves code to a generated/excluded file. A large rename inflates apparent scope. Tiny changes yield unstable p90 and clone-rate statistics, while a corrected long function still looks worse than a short broken function.

**Fix:** Analyze changed symbols with their callers, callees and relevant whole-file context; report both final-state risks and changes from baseline. Freeze rename and generated-code policies. Show sample sizes and raw counts for ratios and quantiles. Do not compare structural scores without functional status and review of critical paths.

## What this budget can reasonably establish

Use the first cycle to test the apparatus, not rank a workflow family. Freeze one baseline and one candidate, one input layer, and a small set of representative task snapshots. Run a pre-budgeted number of paired repeats. Keep trusted functionality/regression/build checks, atomic completion auditing, and explicit architecture/security gates; record structural counts as diagnostics. Calibrate a small judge sample independently before paying for large-scale scoring. Publish all run outcomes and costs, including failures, and describe uncertainty without significance or non-inferiority claims that the data cannot support.

A later experiment must separately buy task diversity, repeated runs, simulator validation, and untouched evaluation. A cheap pilot can expose large failures and instrumentation defects; it cannot certify a five-point reliability margin or general superiority.

## Ranked changes before running anything

1. **Replace confirmatory claims with a costed pilot protocol.** Define H/S, units, task weighting, primary endpoints, all-run handling and a concrete run/evaluator budget. Remove infeasible non-inferiority and one-run confirmation.
2. **Freeze trusted evaluation and add evidence-based failure checks.** Protect tests/configuration, validate actual treatment execution, audit atomic completion claims, and define task-specific architecture, scope, regression and security gates.
3. **Reduce and validate quality measurement.** Keep deterministic counts as diagnostics, drop universal explicit-receiver/parameter-minimization rewards, and calibrate contextual judges on held-out positive and negative controls with expert adjudication.
4. **Separate causal questions.** Choose either native product cost/quality or resource-matched effects; hold fix policy and reviewer resources fixed for phase claims. For Q3, isolate interview access and constrain simulator disclosures with a fact ledger.
5. **Separate discovery from evaluation.** Freeze a candidate-selection rule, account for noisy Pareto decisions, and reserve genuinely fresh tasks with repeats for later validation. Retire any holdout as soon as its feedback influences development.
