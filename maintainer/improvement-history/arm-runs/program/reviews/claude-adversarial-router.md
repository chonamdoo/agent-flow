# Review: should program-v2 add an "agent picks its own flow" (router) arm?

## 1. For and against

**For.** The user's decision is which flow to ship as the *default*, and a default serves every request type: bug fixes, refactors, small features, UI. A/B/C/S each answer "how good is this fixed flow on these feature tasks". None answers "does the right flow depend on the request?". If the winners differ by request type, every single fixed default leaves quality or cost behind, and only a router gets it back. Also, `default` already *claims* R2 ("AI infers depth per task"). Shipping it untested means shipping an unverified router anyway.

**Against.** (a) The evaluator has only feature tasks. An ideal router picks `development` or `full-feature` every time, so R1 collapses to B or C plus routing noise and cost. (b) A router's value is capped by the *oracle gap*: mean over tasks of (best fixed flow for that task) minus (best single fixed flow). That gap can be computed from fixed-arm data you collect anyway, and until it beats the noise floor a router arm is estimating something bounded near zero. (c) pstack publishes no numbers, so it is no evidence. (d) At $150 per cycle, one more arm pushes n=5 back by about one cycle per task.

## 2. Measurement attacks and controls

| Confound | Control |
|---|---|
| Selection vs flow quality (wrong pick, or right pick run worse because of the routing preamble) | Log the pick. Compare with the existing fixed arm of the same flow on the same task. This splits regret into selection regret and execution delta, with no extra runs. |
| Feature-only tasks: nothing to choose | Mixed task set, stratified by type, labels pre-registered |
| Routing cost | Count routing tokens and time in R1 cost, and also report them separately |
| Cheapest-flow bias or gaming | No cost or length wording in the menu. Report the choice distribution. If one flow takes ≥90% of picks, treat R1 as that fixed arm and stop. Autoresearch must not tune the router prompt (§5 rule). |
| Non-determinism at n=3 | Outcome deltas will sit inside the noise floor. Make the choice metrics (agreement, consistency across reps) the main readout. |
| Noisy oracle | Use two oracles: a blinded human label per task written before any run, and the empirical best fixed arm. Report both. |
| R3 (switching mid-task) | Needs runner changes, like the on-hold C-cap5. Out of scope. |

## 3. Placement and minimal design

**Not Q2.** Q2 asks whether a cheaper fixed variant is non-inferior on feature tasks. "Router vs fixed" is not one of its discrete search dimensions, and adding it would mix the two up. If it is done, it is its own question, **Q4: does per-request selection beat the best single fixed default?**, run after §4 calibration and §6 confirmation.

- **Arms:** R1 (pick one from a menu at the start) and R2 = `default` as written. Compare both against the best single fixed flow, reusing that flow's per-task runs.
- **Menu:** `development`, `bugfix`, confirmed S\*, `full-feature`, and "no workflow" (A). Real one-line descriptions, cost wording removed.
- **Tasks:** at least 4, one per type: a bug fix (seeded defect plus a hidden regression test), a behaviour-preserving refactor (hidden tests, architecture rules, a follow-on change), a feature, and a UI task (§3e). Each needs the full private evaluator, and that is the real cost.
- **Log:** the choice, a one-sentence reason, routing cost, the phases run versus skipped, and both oracle labels.
- **Choice metrics:** agreement with each oracle, consistency across reps, per-task regret against the best fixed flow for that task, and overall regret against the best single fixed flow.
- **Decision rule (pre-registered):** ship R1 or R2 only if it is non-inferior to the best single fixed flow on ①②③(④) at the Q2 margin, costs no more, has choice consistency ≥2/3 on every task, and has zero integrity violations. Otherwise ship the fixed flow.
- **Gate:** first compute the oracle gap from fixed arms on the mixed set. If it is below the noise floor, skip Q4.

## 4. What this exposes as missing in program-v2

1. **`default` is never measured,** although it is the canonical workflow "applied to every project". Its adaptive-depth claim is untested.
2. **No ship decision rule** that turns the Q1 and Q2 results into the default.
3. **Feature-only tasks.** `bugfix` and `diagnosing-bugs` exist but are never evaluated, yet a default must handle them.
4. **No analysis by task type** and no oracle-gap computation.
5. **No phase trace** of which phases ran or were skipped. It is needed to explain cost and to check any "collapses phases" claim.
6. **Orchestration overhead** is not separated from work cost.

## Verdict

**Add later, with preconditions.** On today's feature-only evaluator, a router arm cannot answer anything A/B/C/S do not already answer. At n=3 and $150 cycles it would only add noise and delay calibration. The question still matters for the ship decision, because a default must handle mixed requests and `default` already claims to route. So do two cheap things now: make `default` (R2) a measured arm, or at least name it as the ship candidate, and add phase-trace logging to every run. Run Q4 (R1 plus R2 against the best single fixed flow) only after three things hold: (1) A/A calibration and the noise floor are done, (2) a mixed bug fix / refactor / feature / UI task set has the full private evaluator, and (3) the oracle gap measured from fixed arms on that set is above the noise floor. If the gap is not there, no router can beat the best fixed default: ship that flow and drop the arm.
