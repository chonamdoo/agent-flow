# Adversarial review request: should the experiment add an "agent picks its own flow" arm?

You are an adversarial reviewer of an experiment design. Read only the files in this folder.

Context:
- `program-v2.md` is the plan (Korean). It compares, on the same private evaluators: A (bare model), A+P (bare + 8 principles in CLAUDE.md), B (`development` workflow), C (`full-feature` workflow), and S1..Sk (fixed slim workflows found by an autoresearch-style search over discrete dimensions on development tasks, then confirmed on a held-out task). Primary endpoints: functional success S, follow-on change success (maintainability), held-out architecture-rule violations, UI fidelity for UI tasks; integrity and cost are guardrails. Budget about $150 per cycle; run costs roughly A $3-4, B $7, C $20. n is small (3-5 per arm per task).
- `agent-flow-workflows.txt` and `agent-flow-default-workflow-description.txt`: agent-flow's workflows. Note `default` already says "AI infers depth per task — trivial fixes collapse phases".
- `pstack-summary.md`: pstack, where the agent reads the request and picks one of 23 playbooks itself.

The user asks: when we search agent-flow combinations with autoresearch, do we also need an experiment where, like pstack, the agent chooses which flow it needs right now? Possible shapes include: R1 = agent picks one workflow from a menu at the start; R2 = agent picks phases / depth per task inside one workflow (what `default` claims); R3 = agent may switch flow mid-task.

Your job, adversarially:
1. Argue both for and against adding such an arm, with the strongest case each way. Name what question it would answer that A/B/C/S do not, and whether that question matters for the user's decision (which flow to ship as the default).
2. Attack the measurement: what confounds appear (selection quality vs flow quality, task mix, the evaluator's tasks all being "feature" tasks so the router has nothing to choose, cost of the routing step, the router picking the cheapest flow and gaming, non-determinism making n=3 meaningless), and how each could be controlled.
3. Say whether it belongs in Q2 (slim variants), as its own question, or nowhere, given budget and n. If it belongs, specify the minimal design: arms, menu, task set (does it need a mixed task set — bug fix, refactor, feature, UI?), what is logged (the choice and its reason), the decision rule, and what to measure about the choice itself (agreement with an oracle choice, regret vs the best fixed flow per task).
4. List anything in program-v2.md this question exposes as missing.

Write your review to `review.md` in this folder (Korean or English), at most ~900 words, ending with a one-paragraph verdict: add now / add later with preconditions / do not add, and why.
