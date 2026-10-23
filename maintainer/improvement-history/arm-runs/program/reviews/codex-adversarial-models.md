# Adversarial review: per-role model allocation

**The decision is whether to ship a fixed role-to-model allocation that lowers the user's total cost without harming delivered quality.** It matters: implementation is recurring spend, and savings could buy more experimental repetitions. It is distinct from choosing workflows. The supplied pstack summary establishes a configuration convention, not evidence that its allocation is cheaper, faster, or better; its budget presets change effort, not model family.

## Case for and against

**For splitting:** routine exploration and implementation may tolerate a cheaper model while planning and review benefit from stronger judgment. Agent-flow already supports reviewer subprocess model selection, so some allocations need no runner change. Keeping judgment strong might preserve quality while reducing work costs.

**Against splitting:** “implementation is routine” is an untested assumption. Repository reasoning, tool reliability, and architecture decisions occur during implementation too. Handoffs can lose context; reviewers can miss defects or demand expensive repairs. A stronger reviewer can rescue weaker work while increasing latency and fix rounds. The resulting system might cost more than uniform execution. Pstack's role labels do not establish equivalent boundaries in agent-flow.

**For testing now:** controller-only C costs about $21.5 on OYG versus about $2 for A. Cost allocation could matter more than deleting phases. A small feasibility audit can reveal whether a future comparison is executable.

**Against an outcome experiment now:** the instrument is not yet calibrated, reviewer spend is invisible, and $150/cycle already cannot comfortably support the existing arms and follow-ons. Adding allocations dilutes n and multiplies selection opportunities. One capped C run does not establish the savings opportunity: build was $8.35, while verify was $10.29, excluding subprocess spend. A cheap implementer might enlarge the largest component.

## Measurement attacks and controls

- **Model behavior versus price:** changing family changes reasoning, tools, context handling, and coding style. Treat the intervention as a complete allocation policy; do not attribute its effect to price or role specialization alone. Freeze prompts, tools, workflow, context limits, effort settings, and versions; record unavoidable provider differences. A later cheap-uniform arm would distinguish allocation benefit from simply choosing another model.
- **Controller versus subprocess:** a phase name is not a model boundary. Today the session model controls implementation and orchestration; reviewer subprocesses have separate selectors. Trace the actual call graph and effective model of every call. Do not describe changing reviewer flags as changing the implementer, or claim independent planner selection without a demonstrated supported handoff.
- **Unmeasured reviewers and cross-provider pricing:** reviewer_jobs is not a dollar estimate. Collect provider usage or attributable billing, input/output/cache tokens, retries and failed calls. Apply a dated common price schedule; report actual charges separately, including subscription ambiguity. Include controller review overhead, subprocesses, repairs and follow-ons. Unknown material spend blocks cost superiority.
- **Availability:** preflight authentication, exact model IDs, effort support and a real invocation inside the evaluation container. Claude/Codex provider support does not prove any particular model is usable. Fail closed on unavailable models; log fallback as a protocol deviation, never silently substitute.
- **Small n:** pair runs by task/snapshot and cycle, randomize order, retain failures, and report paired uncertainty. Three to five runs cannot reliably establish narrow non-inferiority, particularly on binary outcomes. Set acceptable margins before results; wide intervals mean inconclusive, not equivalent. Do not widen margins merely to fit n.
- **Ceilings:** keep mobile-cart as regression surveillance. Use calibrated, discriminating development tasks and follow-ons, then untouched holdouts. Validate architecture and UI mutants too. Passing saturated checks cannot demonstrate preserved quality.
- **Reviewer rescue:** retain first-pass artifacts and review findings; log repair rounds, cap hits, elapsed time and repair costs. Score final artifacts for adoption, with first-pass outcomes as diagnostics. Rescue is legitimate system behavior only when fully charged.

## Placement and minimal later design

Use a **separate fixed-allocation question after Q2**, not another Q2 dimension. Joint workflow/model search would obscure attribution and consume repetitions. Unlike Q4, a fixed allocation needs no heterogeneous-task oracle gap or adaptive router.

Only **claude-opus-5-5** is explicitly identified as an experimental model in these files. No cheaper pair is confirmed available; pstack's slugs are not evidence of container availability. Therefore no honest named cheaper-model comparison can start yet.

After preflight confirms an exact cheaper model L, freeze Q2's chosen workflow. Compare uniform Opus with L as controller/implementer and Opus reviewer subprocesses; acknowledge that orchestration/planning also change. If supported boundaries later permit it, test L explorer/implementer/fixer with Opus planner/reviewer/QA separately. Do not promise that allocation under the current runner constraint.

Hold evaluator, snapshots, review count, gates, cap, budgets and the bare follow-on agent fixed. Require every primary endpoint, including UI where applicable, to meet preregistered non-inferiority bounds, zero integrity violations, and total-cost superiority with uncertainty. Log role, effective provider/model/effort, phase, tokens, cost, latency, retries, fallback, artifacts and repair lineage.

**Verdict — add later with preconditions.** Add availability and complete cost instrumentation now, alongside v2.1's phase trace. Amend “same model” to distinguish workflow comparisons from allocation comparisons; specify margins, cost scope and budget for D and follow-ons. Start the separate allocation test only after calibration, confirmed models, supported execution boundaries and adequate repetitions. Preserve Q4's deferral and the independent deterministic evaluator; production reviewers are not evaluation judges.
