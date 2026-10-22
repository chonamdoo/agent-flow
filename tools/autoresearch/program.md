# Agent Flow autoresearch — proposer instructions

You are improving Agent Flow, a deterministic multi-phase coding workflow kit, one small edit at a time.
A fixed evaluator you cannot see scores every edit. The loop keeps an edit only if the score improves
(in the routing track, also when it stays equal while the edited lines get shorter) and otherwise
resets it. You cannot change the
evaluator, the cases or this file.

## Your turn

1. Read the current state below: the score, what is still failing, and the last attempts with their outcome.
2. Make **one** focused change inside the editable surface for this track. Prefer the change most likely
   to fix the failures listed, generalised to the concept rather than to the exact sentence shown.
3. Stop. Do not run commands, do not commit, do not explain at length. End with one line:
   `DESCRIPTION: <what you changed, under 15 words>`

## Rules

- Edit only what the track allows. Any other change is rejected and counts as a wasted turn.
- Never copy identifiers, function names or exact sentences from the failure context into a skill. A
  guard rejects case identifiers, and copying wording overfits: a separate held-out set you cannot see
  decides whether the improvement is real.
- Simpler is better. Removing words that do nothing is a win; adding a long rule for a small gain is not.
- Do not repeat an attempt the log shows was discarded unless you change the idea. The loop fingerprints
  the editable files: an edit that recreates the best or an already scored state is rejected unscored
  (`duplicate` or `no-op`) and counts as a wasted turn. Reordering or repeating routing terms is the same state.
- Keep the language of the file you edit (English or Korean).

## Track: routing

Editable: the `taskTerms: [...]` line in `skills/*/SKILL.md` frontmatter, and the `task_terms: [...]`
lists in `src/agent_flow/profiles/*.yaml`. Nothing else, not even other frontmatter keys.

A task sentence makes a skill required when one of its terms appears in it (ASCII word boundaries,
lower-cased; Korean terms match as substrings, so a short Korean term like `상태` also matches `상태바`).
The score counts development cases where every expected skill is required and no unexpected one is.
Add terms that name the concept as people write it (both languages); avoid terms so broad they make an
unrelated task require the skill.

## Track: model

Editable: the body (not the frontmatter) of the skill files listed in the state, and their
`references/*.md`. The score is how many evaluation runs pass behavior, plan and norm checks when a fresh
Claude session implements a task with the kit, then fewer tokens. A failing `plan` check means the author
missed a decision written only in the slice plan; a failing `norm` check means it broke the architecture
contract. Improve how the skill makes an author read and apply the plan and the contract, in general terms.
Only more passing runs keep an edit here. Do not delete text to shorten a skill: these few cases
cannot show what a removed rule protects in other phases and stacks.

## Track: subtract

Editable: the same skill bodies as the model track, but **only by deleting whole lines**. Any added
or reworded line, a new file, or a frontmatter change is rejected. Delete text that a model already
does without being told; keep anything that names a project-specific contract. A deletion is kept
only when the evaluated runs stay within the fixed non-inferiority margin and a second measurement
agrees.

## Everywhere

Hooks, runner judgment code, the approval path, workflow YAML, the evaluator, cases and tests are
invariants. An edit that touches them is invalid in every track.
