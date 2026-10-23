# Adversarial review: per-role model split (pstack-style)

## 1. Case for and against

**For.** Measured C spend sits in build ($8.35) and verify ($10.29 of $21.5). If a cheaper model did the bulk work at equal quality, Q2's goal (same quality, lower cost) could be met on an axis Q2 doesn't search.

**Against.** (a) pstack offers no evidence. It gives no cost, quality or speed rationale and no measured effect, and it has no step where a strong model checks a cheap one. It shows a convention exists, nothing more. (b) **agent-flow cannot do the pstack split.** Explorer, planner, implementer and fixer all run in the controller on the session model. `roles/*.yaml` has no model field. The only knob is the per-reviewer `--model`/`-m`, so the only split available makes the *judgment* role cheaper: the opposite of pstack. A cheap implementer means swapping the session model, which also changes planning and orchestration. Doing it per role needs a runner change, and v2.1 forbids that. (c) The reviewers' share of spend is **unknown**. Reviewer subprocesses print no usage, and the $6.70 multi-review figure is controller cost only.

**Testing now:** nothing in favour. Against: A/A calibration and the noise floor aren't done, reviewer cost can't be seen, and the budget covers only 2-3 sets per cycle.

**Decision informed:** "what should reviewer jobs default to?" This matters only if reviewer spend is a large share of the total, which nobody knows yet. Per-role model fields are a kit-design decision, out of scope while the runner is frozen.

## 2. Measurement attacks and controls

| Confound | Control |
|---|---|
| Model family changes behaviour (what gets flagged, verbosity), not just price | First comparison stays within one family (Opus vs a smaller Claude slug). Cross-family is a separate, later factor |
| Controller/subprocess split: only reviewers can vary | Call it "reviewer tier". Never report it as a test of the pstack mapping |
| Reviewers unmeasured: cost superiority can't be judged. **This already breaks Q2's "reviewers 2/5" dimension** | Precondition: per-reviewer-job token capture (provider JSON output or billing delta; verify which works) |
| Provider availability: a missing slug falls back silently or fails late | Preflight check that each slug is served. Log requested vs served model per job. A mismatch makes the run invalid |
| n = 3-5: the effect is likely below the noise floor | Pair runs within a cycle. Use the v2.1 non-inferiority rule with intervals, allowing "no conclusion". Run only after the noise floor exists |
| Evaluator ceiling hides the loss | Use non-ceiling tasks only (OYG, devflow, mobile-tabs). Also measure reviewer recall offline on a fixed diff with injected defects |
| Fix-loop coupling: a weaker reviewer gives fewer rounds, lower cost and a worse product. A strong reviewer over a weak implementer gives more rounds and cap stops (C-2 hit cap 3) | Log rounds, cap stops, and findings per reviewer (count, severity, accepted). Report quality with and without cap-stopped runs |
| Cross-provider pricing (cache, reasoning tokens) | Log raw tokens by type. Price them from one dated table in the manifest. Report tokens and dollars separately |
| Judge family (§3d, later): a Codex reviewer muddies "different family" | Fix the judge as a family distinct from every model in the arm |

## 3. Placement and minimal design

It is not its own question and not a pstack test. Once the preconditions hold, add it as **one discrete Q2 dimension**: `reviewer_model ∈ {claude-opus-5-5, <smaller Claude slug confirmed in container>}`. It is config-only and needs no runner change.

- **Hold fixed:** controller model, workflow, reviewer count, effort, fix-loop cap 3, snapshot, follow-on agent (Opus), evaluators.
- **Comparison:** the Q2 incumbent (or B/D) with only the reviewer tier switched, paired within a cycle, ≥3 runs per task.
- **Decision rule:** ①②③ (④ for UI) all non-inferior with intervals; integrity violations = 0; **total** cost (controller plus reviewer) lower; cap-stop rate not higher. Otherwise keep Opus.
- **Log per job:** role, provider, requested/served model, effort, tokens by type, wall time, findings, fix-loop round.

A cheap implementer (the real pstack direction) would be a future **Q5**, gated on permission to change the runner. Its first step is a whole-session cheap-model baseline, to see whether the cheap model can do these tasks at all.

## 4. Interactions and gaps exposed

- **Phase trace** covers the controller only, so the v2.1 "orchestration vs work cost" split under-counts exactly where reviewers run. **Gap:** add per-reviewer usage and model now.
- **Manifest** says "same model" but doesn't record reviewer provider, model or effort. **Gap:** record them, or C/D could differ in reviewer config without anyone noticing.
- **Q2 "reviewers 2/5"** needs the same reviewer-cost measurement, so that work is required anyway.
- **Integrity (3a)** catches tampering a cheaper reviewer misses, independently of the reviewer. Report it per arm.
- **Q4:** keep the model axis out of the router menu, because it multiplies arms.

## Verdict

**Add later, with preconditions. Never as a pstack-style split.** pstack's mapping comes with no evidence. agent-flow can't express it without a forbidden runner change. The one split it can make (reviewer tier) runs the opposite way from pstack, and its cost can't currently be seen. **Now:** only log reviewer provider, model, effort and tokens per job in the manifest and phase trace. Q2's reviewer-count dimension needs this anyway. **Later:** add `reviewer_model` as one within-family Q2 dimension once (1) A/A calibration and the noise floor are done, (2) reviewer cost is measured and is a material share of run cost (say ≥20%), and (3) a cheaper slug is confirmed served in the eval container. Defer cheap-implementer splits to a Q5 gated on runner changes.
