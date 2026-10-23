# Adversarial review request

Read PLAN.md (Korean). It proposes an experiment program for an agentic coding workflow kit ("agent-flow"): which conditions to compare, an autoresearch search over workflow variants, a goal-only input layer with a simulated user for interview ("grilling") phases, and an extended quality evaluation (deterministic tools plus LLM judges for SRP, side effects, DRY, parameter count, fail-fast, guard clauses, SLAP, explicit receivers).

Be adversarial. Your job is to find what would make the conclusions wrong, not to be encouraging. For each finding give: id, severity (blocker/high/medium/low), the claim in PLAN.md it attacks, a concrete failure scenario, and a fix (or "no fix; drop it").

Cover at least:
1. Measurability: which quality axes in §3 can actually be measured reproducibly and validly on agent-written Kotlin/JS diffs, which are proxies that can mislead (e.g., function length vs SRP), and which should be dropped.
2. LLM-judge validity: blinding leaks (style, phase artifacts in the diff), position/verbosity bias, judge-model self-preference (Claude judging Claude output), calibration with injected quality mutants — is it enough?
3. Gaming and confounds: can the workflow arms learn the metrics; fix-loop cap confound; reviewer-count confound with cost; simulator leaking the hidden PRD.
4. Statistics: number of axes vs n, multiple comparisons, non-inferiority margins, Pareto acceptance in autoresearch (overfitting, noise), whether one holdout run per cycle can confirm anything.
5. Cost: is the program feasible at ~$150 per cycle; what to cut first.
6. Anything missing that the user's stated concerns need (false completion claims, architecture violations, bloated functions, scope creep, regression, security).

End with a ranked list of the 5 changes you would make before running anything. Write your review to REVIEW.md in this folder. Read only this folder.
