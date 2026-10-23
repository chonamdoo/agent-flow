# Adversarial review: adaptive workflow selection

## What would this answer?

**Strongest case for adding it:** A/B/C/S estimate performance of fixed policies; none tests whether observable task characteristics justify different workflows. R1 asks whether request-time selection improves quality/cost over the best deployable fixed default. That directly informs what to ship: a workflow or a router. A globally winning slim workflow could conceal expensive overprocessing of bugs and underprocessing of complex features. pstack supplies a plausible design pattern, not evidence that routing works.

**Strongest case against:** Routing introduces another policy to tune before the evaluator is validated. With $150/cycle and n=3–5, it can consume the replication needed to distinguish existing arms. If one fixed flow dominates, or all tasks reward feature implementation, there is little routing value. Selection also adds prompt/context overhead and failure modes. Merely choosing different names proves nothing.

R1, R2, and R3 are different interventions. `default` already claims R2: variable depth with invariant phase order. B/C are not substitutes for testing that claim. R1 chooses a whole workflow once; R3 adds a sequential policy whose switches depend on execution history. Do not combine them into an “adaptive” arm. The current files establish descriptions, not verified runtime adaptation.

## Measurement attacks and controls

- **Selection versus execution:** A router can choose sensible flows that execute poorly, or exploit a strong flow without useful selection. Evaluate every menu flow on the same tasks, with identical snapshots and limits. Compare routing against the best fixed flow selected on development data. Add an offline selector replay over that matrix to diagnose selection separately from end-to-end execution.
- **Task mix:** A feature-only evaluator cannot establish cross-type routing value. Include bug fixes, refactors, features, and UI work; UI overlaps the other categories. Include difficulty variation within types and freeze deployment-relevant weights before testing. Report task-level results, not just an average that easy bugs can dominate.
- **Routing cost/context:** Charge selection tokens, latency, repository inspection, retries, and failed dispatches. Bound preselection inspection identically. Menu-reading context can itself help; a fixed-choice wrapper seeing the same menu/information is a diagnostic control if overhead matters. Offline replay omits context carryover, so it cannot replace live runs.
- **Cheap-flow gaming:** Keep evaluators private; freeze the quality-first decision rule and caps. A cheap choice is legitimate only when functional, follow-on, architecture, UI, and integrity requirements hold. No reward for flow labels, rationale eloquence, or skipped phases.
- **Stochasticity:** n=3 is a pilot, not evidence of noninferiority. Repeat both selection and execution; log choice frequencies and outcomes. Block/randomize by task and cycle. Many repetitions of one task do not establish generalization across tasks. Use uncertainty intervals and an explicit inconclusive outcome; deterministic decoding alone does not remove execution noise.
- **Oracle inflation/leakage:** A human’s preferred label is not outcome truth. Report expert-label agreement only as diagnostic. Estimate per-task oracle performance from independent repetitions of all menu flows; choosing each task’s luckiest observed run exaggerates oracle value. Keep held-out outcomes unavailable to router tuning.

## Minimal design, once prerequisites hold

Make this a separate Q4, not another Q2 dimension: Q2 searches a fixed workflow; Q4 evaluates a selection policy. Reuse Q2’s frozen candidates and evaluator infrastructure.

Start with R1 only. Menu: `bugfix`, `development`, and the frozen Q2 winner S*. If S* duplicates development, omit it. Add `diagnosing-bugs` only if development evidence motivates a distinct hard-diagnosis stratum. Exclude review-only work from implementation tasks. Do not import pstack’s 23-playbook menu or add costly C without evidence it wins somewhere.

Development must first show credible task-dependent crossovers. Then freeze router instructions, menu descriptions, fallback to development, inspection allowance, budgets, and task weights. Confirm on unseen mixed tasks, with multiple tasks per relevant stratum. Arms are R1 and every menu member forced as a fixed policy; designate the development-selected best fixed policy as primary comparator. Keep shipping/merge actions outside the sandbox and completion conditions identical.

Log input/snapshot and policy versions, selection seed, choice, concise pre-execution reason, inspection and routing cost, actual phases/depth, overrides/fallbacks, failures, execution cost, and all existing endpoints. Record intended versus executed flow. No switches in R1.

Adopt only if all primary quality endpoints meet preregistered noninferiority margins, integrity holds, and total cost improves over the frozen fixed comparator with adequate uncertainty bounds. Otherwise retain the fixed default. Report per-task quality loss and excess cost against the independently estimated best feasible flow, oracle headroom, selection frequencies, and expert agreement. Avoid inventing a scalar “regret” that silently trades failures for dollars.

## Missing from program-v2

The plan lacks a target task distribution; a tested definition of `default` adaptation; routing/phase telemetry; a frozen best-fixed comparator; and an operational shipping rule across endpoints. §5 checks functional success alone although Q2 promises noninferiority on all quality endpoints. Its noise-floor heuristic does not establish noninferiority. Budgeting must include menu coverage, selection repetitions, follow-on runs, and held-out task diversity.

**Verdict: add later with preconditions.** First validate measurement, repair Q2’s adoption rule, and establish task-dependent workflow advantages on development tasks. Then fund Q4/R1 separately with frozen policies and unseen mixed tasks. Current budget and n cannot support a credible routing claim alongside existing commitments. Test `default`’s R2 claim separately if that is the actual shipping candidate; defer R3 until simpler adaptation demonstrates value.
