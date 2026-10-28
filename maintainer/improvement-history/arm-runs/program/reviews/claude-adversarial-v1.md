# Adversarial review: #409 experiment program v1 (epic409-program-v1.md)

I read only the file named. Section references (§) point to it.

## Bottom line

The plan measures many things, and almost all of them are things the treatment arms are told to optimize. The sample sizes cannot support the decision rules in §4. At ~$150 per cycle the program can afford roughly 10–20 agent runs, so it can compare one variant to one baseline on one or two tasks. It cannot run an autoresearch search, a 10-axis non-inferiority test, a goal layer and pairwise LLM judging. As written, the likely outcomes are (a) "no significant differences anywhere", or (b) a Pareto winner picked from noise and "confirmed" by a single holdout run. Neither answers Q1–Q4.

---

## 1. Measurability of the quality axes

**M1 — blocker — Claim attacked: the quality axes measure quality (§3a, §3b).**
The workflow arms are explicitly told to follow SRP, minimize parameters (bundle them into classes), use guard clauses, and so on. The evaluator then measures exactly those things: function length, `LongParameterList`, nesting depth, and judges asked about the same 8 principles. This is Goodhart by construction. Keeping the evaluator private (§5) does nothing, because the principles are the metric.
- Failure scenario: C splits a 40-line function into five 8-line wrappers and adds a `CheckoutParams` data class that just forwards fields. Lizard p90 length drops, `LongParameterList` hits drop, and the SRP and parameter judges score higher. The code has more indirection and is harder to change. You conclude "agent-flow improves quality" when it only improved compliance.
- Fix:
  - Add counter-metrics that penalize over-engineering: total added LOC, number of new types and files, single-use functions or classes, call depth / fan-out of new code, wrapper-only functions, parameter-object classes used once.
  - Add one validity anchor that is not a proxy: a **follow-on task**. A fixed agent (bare model, fixed seed policy) implements a second, pre-written change on top of each arm's output. Measure its success and cost. That is the operational definition of maintainability, and none of the arms is trained toward it.

**M2 — high — Claim attacked: function length, cyclomatic complexity and nesting are reproducible and meaningful on Kotlin/JS diffs.**
- Reproducible: yes. Rerunning lizard on the same tree is deterministic, which also makes the Q4 check vacuous (see S6).
- Valid: weak.
  - Mobile Kotlin with Compose has long composables by idiom, so length is dominated by UI code.
  - Lizard's Kotlin parsing of lambdas and DSLs is shaky.
  - "Changed functions" depends on how diff hunks are mapped to functions.
  - p90 over 3–8 changed functions is noise.
  - Comparisons across tasks mix languages.
- Failure scenario: one arm touches one more composable and its p90 doubles.
- Fix:
  - Compare only within task.
  - Report the change in max function length in touched files versus the base commit. Growth of existing functions is the actual "stuffed one function" failure the user saw, and the plan does not measure it.
  - Exclude `@Composable` functions and tests, or report them separately.
  - Drop p90 and keep max plus count over a threshold.

**M3 — high — Claim attacked: DRY via jscpd/CPD on the changed code (§3a).**
- Clone rate inside a small diff is usually 0 or one clone (degenerate), and tests are duplicated by design.
- The failure that matters is ignoring an existing utility and re-implementing it. Within-diff clone detection does not catch it.
- Fix: run clone detection of added code against the whole repo, with a token threshold, excluding tests. Report a count, not a rate.

**M4 — high — Claim attacked: architecture is measured by agent-flow `architecture-lint` applied to all conditions.**
- B, C and S likely run this lint, or have an architecture-review phase, during the run. A never sees it. "Arms that run the lint pass the lint" is a tautology, not a finding.
- Fix:
  - Use held-out architecture rules that no arm's gates or prompts contain, written per task repo before any runs (import-direction rules, forbidden layer calls).
  - Report the in-loop lint separately as a "process compliance" number.

**M5 — medium — Claim attacked: security via semgrep/detekt.**
- On small feature diffs the base rate of findings is near zero, so every arm scores 0 and the axis has no power. If one finding appears, it is anecdote.
- Fix: either drop security as a comparative axis (keep it as a guardrail count), or build security-relevant tasks with hidden exploit tests (injection or XSS payloads against the new endpoint or form). Also add gitleaks (secrets) and OSV / `npm audit` on new dependencies.

**M6 — medium — Claim attacked: scope measured by diff size relative to a reference implementation.**
- The reference is one solution, so a legitimate alternative design is penalized.
- Workflow arms also write artifacts (plan, PRD, slice docs, comments from comment-authoring) that inflate files and lines.
- Fix:
  - Write a per-task allowed-paths manifest in advance and count touched paths outside it.
  - Count new dependencies (`package.json`, `build.gradle`) and whitespace-only or reformat churn.
  - Exclude `.agent-flow/` and docs from every code metric.
  - Drop the reference-ratio metric.

**M7 — medium — Claim attacked: side effects, SLAP, explicit receivers and fail-fast are measurable by judges.**
- "Avoid side effects" and SLAP are judgment calls; expect low inter-rater agreement even among humans.
- Explicit receivers is Kotlin-specific (scope functions, `this@`) and is better measured deterministically with an AST or a custom detekt rule. In JS it is ill-defined.
- Fail-fast has good deterministic proxies: detekt `SwallowedException`, `TooGenericExceptionCaught`, empty catch blocks, and `?: return` / `!!` counts. These also relate to "said it worked but didn't".
- Fix:
  - Move explicit receivers and fail-fast to deterministic metrics.
  - Make side effects and SLAP exploratory only, or drop them.
  - Keep at most 2–3 judge axes, and only those that pass validation (J4).

**M8 — high — Claim attacked: quality comparisons across conditions are like-for-like.**
- C stops at the fix-loop cap. An incomplete run has a smaller or partial diff and will look better on bloat, scope and duplication.
- Fix: pre-register the handling.
  - Report quality conditional on completion and intent-to-treat (incomplete run = worst observed value, or a separate "no usable artifact" category).
  - Always report the stop reason.

---

## 2. LLM-judge validity

**J1 — blocker — Claim attacked: the diff is blind once condition names are removed (§3b).**
Workflow arms leave fingerprints:
- KDoc and comments from comment-authoring
- TDD-shaped tests (many small red/green tests)
- plan files, commit-message style
- reviewer-driven micro-refactors
- comments that literally name the principle ("// guard clause", "single responsibility")

The judge will infer the arm, and its priors about "structured" code will drive the scores.
- Fix:
  - Judge only production source files.
  - Run ktlint/prettier, strip comments, squash commits, and drop non-source files and tests (judge tests in a separate pass, if at all).
  - Run a **leak probe**: ask a model to guess the arm (A vs agent-flow) from the sanitized diff. If accuracy is meaningfully above 50%, blinding failed, and the judge results cannot be used for between-arm claims.

**J2 — high — Claim attacked: two judges (Claude and Codex) plus Cohen's κ establish reliability.**
- Agreement between two LLMs measures shared bias as easily as validity.
- If the agents run on Claude Code, a Claude judge is scoring Claude code that Claude reviewers (C's 10 reviewers, same principles) already polished. That stacks self-preference with "the in-loop reviewer is the judge" (see G2). If arms also run on Codex, the Codex judge has the mirror bias.
- Unweighted Cohen's κ is the wrong statistic for 0–3 ordinal scores.
- Fix:
  - Use a human anchor set: the user labels about 30–50 pairs or items, stratified by task.
  - Report judge-vs-human agreement (weighted κ or Krippendorff's α) per axis.
  - Report the judge-by-agent-family interaction (does Claude rate Claude-produced code higher than Codex does?).
  - Drop any axis where judge-vs-human agreement is poor.

**J3 — high — Claim attacked: injected quality mutants calibrate the judge (§3b).**
- Hand-injected single defects on the reference implementation are blatant and out of distribution. Detecting them shows sensitivity to gross defects, not the ability to tell two plausible agent diffs apart.
- Mutants test sensitivity only; there is no specificity test.
- Fix: add three mutant families.
  - **Neutral mutants:** semantics- and quality-preserving rewrites (renames, reorderings, an acceptable extract-function). The judge must not change its score.
  - **Over-engineering mutants:** needless abstraction, a single-use parameter object, over-splitting. The judge must penalize these, and the SRP/parameter rubric will probably reward them, which is exactly the bias to expose.
  - **Length-matched pairs**, to measure verbosity bias directly.
  - Also measure discrimination on real agent-vs-agent pairs against human labels, not only on mutants.

**J4 — medium — Claim attacked: 8 separate principle scores are 8 measurements.**
- Halo effect: judge scores across axes will be highly correlated, so the effective dimension is probably 1–2. That inflates the multiple-comparison problem with no gain in information.
- Fix: report the inter-axis correlation on pilot data. If it is above about 0.7, collapse to one "judge quality" score plus at most two specific axes.

**J5 — medium — Claim attacked: position swap handles position bias, and pairwise works on large diffs.**
- Swapping is fine, but the plan must also say how inconsistent verdicts are scored. Pre-register "inconsistent = tie" and report the inconsistency rate.
- mobile-checkout diffs may be large enough that judges skim.
- Line citations can be hallucinated.
- Fix: verify mechanically that each cited line exists in the diff, and discard verdicts with invalid citations. Chunk per file with a fixed budget.

**J6 — medium — Claim attacked: pairwise judging is affordable.**
- With k arms there are k(k−1)/2 pairs per task-replicate, times 2 positions, times 2 judges, times 8 axes. At 5 arms that is 40 judge calls per task-replicate before axes, on large diffs.
- Fix: score absolutely, one item at a time, with a rubric. Use pairwise only on a subsample for validation, and use the second judge family only on the calibration and human-anchor set.

---

## 3. Gaming and confounds

**G1 — blocker — Claim attacked: Q1/Q2 attribute effects to workflow phases.**
- B, C and every S variant carry the agent-flow principles in kit prompts; A does not. "agent-flow beats A on quality" therefore confounds principles-in-prompt with phases, reviewers and gates.
- Fix: add **A+P**, the bare model plus the 8 principles in CLAUDE.md or AGENTS.md. It costs about the same as A, is the cheapest credible treatment, and is the natural Pareto competitor for Q2. If A+P matches S on quality, the phase machinery is not what produces the gain.

**G2 — high — Claim attacked: the evaluator is private, so arms cannot game it (§5).**
- C's 10 reviewers are LLM judges with essentially the same rubric as §3b. C optimizes the judge metric in the loop.
- The autoresearch loop itself sees evaluator outputs every cycle. If whoever edits the YAML (human or LLM) can change prompt text, it will drift toward "keep functions under 20 lines" instructions that game lizard. The evaluator is private from the run, not from the search.
- Fix:
  - Restrict the search to the pre-declared discrete dimensions in §2, with no free-text prompt edits.
  - Keep the follow-on task, held-out architecture rules and human anchor completely out of the search loop's feedback.

**G3 — high — Claim attacked: the fix-loop cap confound is handled by listing it as a risk (§5).**
- C's 0/2 on OYG may be pure cap artifact. Any S variant with cap 5 will "beat" C for that reason alone.
- Reviewer count and cap also interact: more reviewers produce more findings, which means more fix iterations and more cap hits. One-factor-at-a-time search cannot see that interaction.
- Fix:
  - Hold the cap constant across all S variants, or add a fixed arm C-cap5 to estimate the cap effect separately.
  - If reviewer count and cap are both searched, run the 2×2 (reviewers 2/10 × cap 3/5) on one task.
  - Log iterations used and findings per iteration.

**G4 — medium — Claim attacked: reviewer count is a quality knob.**
- Cost scales roughly linearly with reviewers. With 10 reviewers the marginal ones probably duplicate findings, and false-positive findings trigger churn edits that worsen scope and regression.
- Fix: log unique findings per reviewer and the share of fixes that address a hidden-check failure. Drop the 10-reviewer level from the search (it is the most expensive and the least plausible winner) and keep it only inside fixed arm C.

**G5 — blocker for Q3 — Claim attacked: grilling vs no-grilling isolates interview value (§2, §4 Q3).**
- In the goal layer, an arm with grilling has an oracle channel to the hidden PRD, and the no-grilling arm has none. That comparison measures access to information, not interview skill. Grilling wins trivially.
- Fix: add **A+ask**, the bare model with an ask-user tool wired to the same simulator. Q3 then compares structured grilling against ad-hoc asking, at matched simulator budget.

**G6 — high — Claim attacked: the simulator answers only facts in the hidden PRD.**
How it leaks:
- Over-answering, volunteering adjacent facts.
- Answering "what should I build?" or "tell me all requirements" with the whole PRD.
- Generous reading of vague questions. A same-family simulator is good at guessing what the agent "meant".

There is also an unrealistic floor: real users answer preference questions instead of "undecided", and they are sometimes inconsistent. The "PRD-fact extraction rate" will be scored by an LLM unless specified otherwise.
- Fix:
  - Enumerate the PRD as atomic facts with ids.
  - The simulator returns only fact ids that directly answer the question; text is rendered from a template. Cap answer length and facts per answer (≤2), and refuse meta questions ("list requirements").
  - Log every exchange and audit a sample.
  - Split hidden checks into goal-derivable and PRD-only, so H in the goal layer is interpretable.
  - Use a simulator model from a different family than the agent.

---

## 4. Statistics

**S1 — blocker — Claim attacked: non-inferiority on H with a −0.05 margin (§4 Q2).**
- Assume run-level H has SD around 0.2 (plausible given A 1/5 and C 0/2). One-sided α=0.05 at 80% power needs about 2·(1.645+0.84)²·0.04/0.05² ≈ **200 runs per arm**. At $20 per C run, that is about $4,000 for C alone.
- At n=5 the 95% CI on a difference is roughly ±0.3–0.4. The pre-registered rule therefore cannot pass. If it is read as "no significant difference", it always passes, which is the classic NI error.
- Fix: either widen the margin to something defensible at the affordable n (for example −0.15 to −0.20, with a stated justification), or downgrade Q2 to descriptive estimation (effect plus CI, no decision claim).

**S2 — blocker — Claim attacked: non-inferiority on every quality axis (§4 Q2).**
- This is an intersection-union test: all axes must pass. With about 10 deterministic axes (several metrics each) plus up to 8 judge axes, even a truly equivalent variant fails at least one.
- Fix: pre-register at most 3 primary quality guardrails, for example:
  1. integrity / false completion (S7)
  2. held-out architecture violations
  3. follow-on task success, or max-function growth

  Everything else is exploratory, with no decision weight.

**S3 — high — Claim attacked: multiple-comparison correction makes Q1 per-axis reporting safe (§4 Q1).**
- Roughly 20+ deterministic metrics plus 16 judge scores, times many condition pairs, gives hundreds of tests. Bonferroni at n≈5 leaves nothing significant; FDR leaves very little; without correction, false positives are guaranteed.
- Runs are nested in tasks, and with 4–5 tasks the task is the real unit of generalization.
- Fix:
  - Use primary endpoints only (above).
  - Analyze within task, paired by task (a mixed model with task as a random effect, or a stratified test).
  - State plainly that conclusions generalize to these 5 tasks, not to "workflows".

**S4 — blocker — Claim attacked: Pareto dominance on the cost-quality vector is a safe acceptance rule (§2).**
- With 10+ noisy axes, strict dominance almost never happens and the search stalls. If acceptance is instead "non-dominated", nearly everything is accepted, including A, which is always on the front because it is cheapest.
- With one or a few runs per variant, the "winner" is the luckiest draw (winner's curse), and greedy one-factor-at-a-time search compounds that.
- Fix:
  - Accept on one primary metric (H, or S on a hard task), conditional on the guardrails not degrading beyond their margin and on lower cost.
  - Require ≥3 replicates per task per variant, and paired comparison against a baseline rerun in the same cycle.
  - **Run an A/A calibration first**: the same configuration twice as if it were two variants, through the full pipeline including the Pareto rule. If it "finds a winner" more than about 5% of the time, the rule is noise-driven.

**S5 — high — Claim attacked: one holdout confirmation per cycle confirms something (§2, §5).**
- One task, one or two variants, likely one run each: that is an anecdote.
- Using the holdout "once per cycle" across many cycles is adaptive reuse, so it stops being a holdout.
- mobile-checkout is also already being piloted with A, so it is being used to tune the hidden checks and the evaluator.
- Fix:
  - Run ≥3–5 runs per arm on the holdout, against the cycle's baseline.
  - Freeze the holdout evaluator before the search starts.
  - Retire and replace the holdout every N cycles, or keep a second never-viewed task for the final claim.
  - Pre-register the confirmation threshold.

**S6 — medium — Claim attacked: Q4 reproducibility = scoring the same artifact twice gives the same deterministic metric (§4 Q4).**
- That is trivially true and says nothing. The reproducibility that matters is test-retest across runs: within-condition variance compared with between-condition difference (ICC per metric).
- Fix: redefine Q4 as per-metric ICC from replicated runs, plus judge test-retest (same item, new session).

**S7 — medium — Claim attacked: false completion = "declared done and S=0".**
- S=0 is too strict; partial lies (claims 5 features, delivers 3) slip through.
- Workflow arms emit more structured claims, so claim-based rates have different denominators across arms.
- Some claims are emitted by the runner or its gates, not by the model.
- Fix:
  - Primary measure: declared done and H below a threshold, or any specific claim contradicted by evidence.
  - Attribute each claim to the model or the runner.
  - Add the integrity checks in §6.

**S8 — medium — Claim attacked: results are comparable across cycles.**
- Claude Code / Codex and model versions drift between cycles.
- Fix: pin and record versions, and rerun the baseline arm in every cycle. Never compare a variant against a previous cycle's baseline.

---

## 5. Cost feasibility (~$150 per cycle)

**C1 — blocker — Claim attacked: $150 per cycle supports the search plus the evaluation (§2).**
- C at $20 × 5 runs is $100 on its own. A large task like mobile-checkout probably costs more per run than OYG.
- One-factor-at-a-time over the 7 §2 dimensions is about 10 variants. Times 4 dev tasks, ≥3 replicates and about $8 per run, that is roughly **$1,000 per cycle** before judges, simulator, goal layer and holdout.
- Realistic capacity per cycle: one or two variants against a baseline rerun, on two tasks, with three replicates.

Cut, in this order:
1. **Goal layer / Q3.** It doubles every cell. Run it as a separate later study with A+ask vs grill only.
2. **Mobile v0/v1 in the search.** They are at ceiling (A 3/3) and give no functional signal, so they push the search toward "cheapest".
3. **C as a recurring arm.** Reuse existing C data plus a few C-cap5 runs as a fixed reference; do not rerun it every cycle.
4. **The 10-reviewer level and the 2/3/5 cap sweep** (fix the cap).
5. **Pairwise, dual-judge, 8-axis judging.** Use one judge family with absolute scores on 2–3 validated axes, and the second family only on the calibration set.
6. **Low-power deterministic axes** (security as a comparison, DRY rate, p90).

Spend the savings on replicates, the A/A calibration, A+P, and the follow-on task.

---

## 6. Missing coverage for the user's stated concerns

**X1 — high — False completion: no integrity checks.** Add:
- Run the **pristine original test suite** (from the base commit) against the final code, so the agent cannot have edited the tests that R uses.
- Detect tampered, deleted or skipped tests (`@Ignore`, `@Disabled`, `.skip`, `xit`, `assumeTrue(false)`) and weakened assertions (diff the test files).
- Detect stubs: `TODO()`, `NotImplementedError`, empty bodies, hard-coded return values matching test fixtures, and mocks of the unit under test.
- Check "tests pass" or "build ok" claims against actual runner logs.

**X2 — high — Architecture.** Add held-out rules (M4), plus "reuse versus re-implement existing abstractions" (repo-wide clone detection, M3).

**X3 — high — Bloated functions.** Add growth of existing functions in touched files compared with the base (M2). The current metric only covers changed and new functions.

**X4 — medium — Scope creep.** Add the allowed-paths manifest, new dependencies, reformat churn, and touched public APIs (M6).

**X5 — medium — Regression.** Behavior mutants are good. Also:
- Freeze the mutant set before runs.
- Verify that each mutant is killed by the hidden regression checks on the base commit.
- Report regression separately from H.

**X6 — medium — Security.** Use task-embedded exploit tests, gitleaks and OSV on new dependencies; semgrep alone on small diffs gives no signal (M5).

**X7 — high — No ground truth for "quality".** Neither the human anchor (J2) nor the follow-on maintainability task (M1) is in the plan. Without them, every quality conclusion rests on proxies the treatment optimizes.

**X8 — medium — No noise floor.** There is no A/A run and no within-condition replicate variance. Every "difference" is uninterpretable without them (S4, S6).

---

## Ranked: the 5 changes to make before running anything

1. **Make the scope fit the power.**
   - Pre-register one primary functional endpoint plus at most 3 quality guardrails; everything else is exploratory.
   - Replace NI −0.05 with a margin that is achievable at the affordable n, or make Q2 descriptive.
   - Run an **A/A calibration** of the full pipeline, including the acceptance rule, before any variant comparison.
   - Defer Q3 and drop mobile v0/v1 from the search.
2. **Add the deconfounding arms.**
   - **A+P** (bare model plus principles in CLAUDE.md), to separate principles from phases.
   - **C-cap5**, or a fixed cap across all variants, to separate the cap effect.
   - Later, for Q3, **A+ask** (bare model plus the simulator tool), to separate information access from interviewing.
   - Harden the simulator: atomic fact ids, an answer cap, refusal of meta questions, and a different model family.
3. **Add integrity and ground-truth checks targeting the user's real complaints.**
   - Pristine-test rerun, test-tampering and stub detection, runner-log claim verification.
   - Allowed-paths manifest and new-dependency count.
   - Growth of existing functions.
   - Held-out architecture rules that no arm sees.
   - Intent-to-treat handling of incomplete runs.
   - A **follow-on change task** as the maintainability anchor that no arm can optimize toward.
4. **Rebuild the judge before trusting it.**
   - Sanitize inputs: production source only, formatter, comments stripped, workflow artifacts removed.
   - Run the arm-guessing leak probe.
   - Calibrate with neutral, over-engineering and length-matched mutants in addition to defect mutants.
   - Build a human-labeled anchor set and report weighted κ or α against it.
   - Check the judge-by-agent-family interaction.
   - Keep only 2–3 axes that validate, use absolute scoring, and verify citations.
   - Drop SLAP and side effects unless they validate; move explicit receivers and fail-fast to deterministic rules.
5. **Replace the autoresearch acceptance and confirmation procedure.**
   - Search a discrete, pre-declared variant space with no free-text prompt edits.
   - Use ≥3 replicates per task per variant, paired against a baseline rerun in the same cycle.
   - Accept on the primary metric plus guardrails plus lower cost, not on Pareto dominance over 10+ noisy axes.
   - Confirm on the holdout with ≥3–5 runs per arm, using an evaluator frozen beforehand, and rotate the holdout across cycles.
   - Pin and record model and harness versions in every cycle.
