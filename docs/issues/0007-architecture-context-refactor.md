# Architecture context refactor (deferred from the selection run)

Split out of run `default/20260912-042606` (프로젝트별 아키텍처 선택). That run
implements the selection itself (T1–T5) plus its regression tests and CI
registration. This document carries the remaining ticket so the work can be
resumed without re-deriving it.

## Why it was split

The context refactor touches shared workflow YAML, skill sources, and both
reviewer/author envelopes. Doing it inside the selection run mixes two failure
causes: a red test would not say whether the selection contract or the prompt
restructuring broke. It also needs before/after measurement that the selection
run has no reason to produce.

## Invariants this work must not change

1. Workflow phase ids, order, routes, and completion conditions are never
   dropped or weakened.
2. Author and reviewer both follow the structure, development method, and code
   patterns the selected required skills state. A read/applied marker alone
   never settles compliance.
3. `pending` is not an exemption. A step that cannot proceed without an
   undecided rule waits for the decision.
4. Only the project's architecture standard is replaceable. Agent Flow's
   workflow and required-skill compliance principles stay.

An optimization that reduces input by removing a required rule, summarizing a
normative document, cutting reviewers, or merging phases is a failure, not a
saving.

## Scope

- **Normative canonicalization.** Keep one authoritative location per rule with
  its exceptions and applicability beside it. Platform skills keep only their
  differences and link the canonical source as a required dependency. Produce an
  `old-doc#section -> new-doc#section` obligation map and check for omissions.
- **Clean alias cutover.** Move every kit-owned workflow/profile/skill/template
  consumer to `clean-architecture-core`, then delete the rule-free alias and the
  duplicated compatibility checklist. Custom workflows outside the kit are not
  rewritten silently: an old required name becomes an explicit migration error.
  A run already in flight keeps its pinned definition.
- **Role-scoped envelopes.** `Adapter.render_envelope()` shares task, scope,
  normative content, and evidence. The author leg keeps artifact-writing and the
  next-command instruction; the reviewer leg keeps its review obligation, angle,
  stdout contract, and read-only boundary. Remove the contradictory
  "write it / do not write it" pairs, not the review obligations.
- **Exact duplicate suppression per input unit.** When one document is selected
  through several routes, deliver its body once per input unit and keep the
  provenance of every route. Author and each independent reviewer are separate
  input units; delivering to one never suppresses delivery to another.
- **In-process derivation reuse.** Reuse an immutable snapshot and the same
  phase resolution within one process, keyed by phase, scope, profile,
  selection/document digest, host, and provider authority. No global read-once
  cache, and never assume another session or a compacted context still holds a
  document.

## Out of scope

- Changing the selection contract shipped by the parent run.
- Reducing the minimum number of independent reviewers.
- A runtime rule database or a generated per-request compliance table.

## Acceptance

- Obligation map shows zero lost required rules or exceptions across the move.
- Fixture cases covering normal, violating, and exception paths produce the same
  observable verdicts before and after.
- No duplicate normative body within a single input unit.
- Measurement records raw input bytes, delivered normative bytes, duplicated
  bytes, actual provider input tokens, cached/uncached tokens, call count, and
  wall time separately. UTF-8 bytes are never reported as token counts; when the
  provider reports no usage, record `unavailable`.
- No savings percentage is claimed without a measurement backing it.

## Starting points

- `src/agent_flow/core/skill_resolver.py` — `resolve_phase_skills()`,
  `skill_prompt_block()`
- `src/agent_flow/adapters/base.py` — `render_envelope()`
- `src/agent_flow/adapters/hosted.py` — `_reviewer_jobs()` base prompt and angle
  contract assembly
- `skills/clean-architecture/SKILL.md` — the alias and its duplicated markers
- `skills/code-generation-discipline/SKILL.md` — the presentation/app-shell
  routing list
- `src/agent_flow/workflows/default.yaml`, `full-feature.yaml` — repeated Clean
  prose and marker blocks

Design source: `agent-flow-architecture-selection-design.html`, section 09
(context refactor) and its T6 ticket.
