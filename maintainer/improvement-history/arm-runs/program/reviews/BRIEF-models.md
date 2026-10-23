# Adversarial review request: should the experiment split models by role (as pstack's setup-pstack does)?

You are an adversarial reviewer of an experiment design. Read only the files in this folder.

Context: `program-v2.md` and `program-v2.1.md` are the plan (Korean). It compares, on private evaluators, A (bare), A+P, B (`development`), C (`full-feature`), D (`default`) and searched slim variants S1..Sk, with primary endpoints functional success, follow-on change success, held-out architecture violations and UI fidelity; integrity and cost are guardrails; budget ~$150/cycle, n 3-5. `setup-pstack-summary.md` describes pstack's role-to-model mapping. `agent-flow-model-facts.md` gives what agent-flow already supports and measured spend.

The user asks: like pstack, is it right to split models by the kind of work (e.g. a cheaper/faster model for implementation and exploration, a stronger model for planning, review and judgment)? Should the experiment test it, and how?

Your job, adversarially (same standard as the earlier router review):
1. Strongest case for and against a per-role model split in agent-flow, and for and against testing it in this experiment now. Say what decision it would inform and whether that decision matters to the user.
2. Attack the measurement: confounds (a different model family changes behaviour, not just cost; the controller vs subprocess split; reviewers unmeasured; provider availability in the eval container; n too small; quality loss hidden by the evaluator ceiling; a stronger reviewer masking a weaker implementer and raising fix-loop rounds and cost; cost accounting across providers with different prices), and how to control each.
3. Placement: inside Q2's search space as one more discrete dimension, as its own question, or not at all. If it belongs, give the minimal design: which roles and which model pairs (only models actually available), what is held fixed, the comparison, the decision rule (non-inferiority on all primary endpoints and cost superiority?), and what must be logged per role.
4. Interaction with what is already planned (phase trace, follow-on changes, integrity, Q4 router deferral) and anything in program-v2/2.1 this exposes as missing.

Write `review.md` in this folder (Korean or English), at most ~900 words, ending with a one-paragraph verdict: add now / add later with preconditions / do not add, and why.
