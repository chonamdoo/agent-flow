# agent-flow

A CLI workflow tool that runs AI coding agents on top of a development process you can verify.

The premise of this tool is that an agent's self-report is not trusted. "I wrote the tests" is
not evidence, so the runner reads the command-execution log recorded by the hooks and judges
for itself.

This is still a personal tool. I have not worked out how to get from here to something a team
agrees on and trusts together — that is written down in [Open problems](#open-problems).

```bash
brew tap chonamdoo/agent-flow https://github.com/chonamdoo/agent-flow
brew install --HEAD chonamdoo/agent-flow/agent-flow
agent-flow .          # install the workflow into the project in this directory
```

`--HEAD` is required while no release is tagged, and the tap takes an explicit URL because this
repository is not named `homebrew-*`. The rest — a checkout install, upgrades, the daily update
check — is in [docs/USAGE.md](docs/USAGE.md).

---

## Why I built it

Three problems kept coming back while putting AI coding tools to real work.

**① Output arrives, but nothing verifies it.** The agent reports "I wrote the tests," and there
is no way to tell whether they ever ran. It says it reviewed the change, but nothing records
what it reviewed against.

**② Requirements evaporate once the conversation gets long.** The values given early on
(spacing, colors, thresholds) disappear while the context is compacted. By the time
implementation starts, something other than the original request has already been built.

**③ The agent quietly widens the scope.** "This looked necessary too" — and unrequested
refactoring, module splits, and performance work come mixed in.

None of the three was solved by improving prompts. They were problems that needed the process
itself pinned down, not a knack one person happens to have.

---

## So what is verified, and how

### Take the routing decision away from the agent

Workflow YAML is only a **definition** of phases; the runner is what decides the next phase.
The agent must follow the `next_command` the runner printed and cannot skip a phase on its own.

### An artifact file existing is not completion

A phase that declares `required_markers` advances only when every one of those markers is
present in the `## Completion Gate` block at the end of its artifact document. A phase that
declares no markers is judged on the artifact file existing.

```
## Completion Gate
usecase-interface: required|optional|n/a
usecase-composition: none|domain-service|application-service|orchestrator|justified
cache-required: yes|no
cache-invalidation-policy: <policy or n/a>
solid-dip-dependency-direction: <summary>
```

The design phase alone demands more than 20 markers. Layer boundaries, dependency direction,
UseCase ports, Repository adapters, cache policy, mapping boundaries, the composition root, and
every SOLID item have to be answered explicitly, or the phase does not pass.

Marker values are cross-checked against the body as well. Writing only a count in `spec-items:`
is rejected; it has to match the actual list of item IDs. Write `design-values: none` while the
body records values, and the runner catches that contradiction.

### Judge on observation, not on claims

Whether a test actually ran in the red phase of TDD (test-driven development) is judged from
the command-execution log recorded by the hook, not from the agent's report.

```
The run itself is observed by the record-command-run.py PostToolUse hook,
so "I wrote a test" without a test command in this phase is rejected
regardless of what you record.

A test that passes on the first run is not a red phase.
```

Whatever a marker says, the runner reads the log itself.

### Attach one verification method to each requirement

In workflows that have a `design` or `prd` phase (`default`, `full-feature`), every instruction
from the user is recorded as a numbered item, and each item carries one verification method.

```
SPEC-<n>: <requirement>
verify: test:<name> | symbol:<symbol>=<value> | manual
```

Concrete values the user supplied (spacing, size, color, duration, threshold) are recorded
separately as `Design Values`. A value read out of an image or a design link is recorded on the
premise "this is what I read, and it is not the original," then read back as a table for the
user to confirm.

This ledger is the only carrier that survives a compacted conversation, so an instruction or a
value missing here never reaches implementation. The runner re-verifies the ledger in the
`final-review` and `multi-review` phases, and **the phase does not complete while any item is
unmet, even if the reviewers approve.** The smaller workflows with no ledger (`review`,
`bugfix`, `development`) do not carry this guarantee.

### Keep the session that wrote the code from reviewing it

Both review phases — implementation review and architecture review — split the work across two
or more independent sub-agents.

- `reviewer-source: sub-agent` is required in each reviewer section
- A single `request-changes` makes the overall verdict `request-changes`
- If a reviewer process fails, the controller session cannot review in its place (blocked)
- More reviewers are added when the change spans several areas

### Keep the scope from growing

When a SPEC is added, changed, or deleted mid-task, only the delta is reported to the user and
work continues after confirmation. The initial SPEC list requires no separate approval, so the
flow is not interrupted, while the path by which an agent grows the requirements is closed. The
comment cleanup phase carries the same constraint.

```
comment-scope: final-pass-only
refactor-scope: none
performance-optimization: none
module-split: none
```

### Keep automation from damaging the main branch

- All work happens inside an isolated git worktree. A branch alone is not enough
- The `guard-protected-branch.sh` hook blocks commits and pushes on protected branches
- `worktree-tripwire.py` detects drift in the leader checkout
- A separate phase has the user approve directly, just before the merge

### Treat the approval path itself as attack surface

The launcher is the one execution path the approval hooks ride on, so it is defended.

- Every loader-injection environment variable — `LD_PRELOAD`, `LD_AUDIT`,
  `DYLD_INSERT_LIBRARIES` — is cleared
- The current directory is removed from `sys.path` on Python runs, closing the path by which a
  single `argparse.py` dropped into the project would execute inside the approval process
- The run-start hook compares the launcher file digests

### The numbers in this README are checked too

The workflow phase counts, profile names, and skill count written below are compared against
the source files by `npm run parity:check`. If the docs go stale, the check breaks. It is the
same "do not trust self-reports" applied to the documentation.

---

## Workflows

Pick by the size of the work. There is exactly one source of truth,
`src/agent_flow/workflows/<name>.yaml`. In the table, `PRD` means product requirements document
and `DDD` means domain-driven design.

| Workflow | When to use | phases |
|---|---|---|
| `review` | a review with no code change | 3 |
| `bugfix` | one reproducible bug | 5 |
| `development` | one concern | 6 |
| `default` | through PR and merge | 15 |
| `full-feature` | from PRD and DDD | 24 |

Use `default` for a small change and waiting on phases costs more than the work itself.

### The `full-feature` flow

```
domain-grill         domain interview (one question at a time, until shared understanding)
product-brief        verify it is worth building
prd                  requirements doc + SPEC ledger + Design Values   [pause]
slice-plan           split into independently shippable units
plan-review          plan review        → approve: next / request-changes: slice-plan
ddd-design           DDD domain modeling → Clean Architecture boundaries
worktree             create the isolated workspace
run-start            record the run configuration
red                  write and run a failing test (the hook observes the run)
green                make it pass with the minimum implementation
refactor             restructure while behavior holds
comment-authoring    final comment pass (no scope growth)
multi-review         2+ sub-agents review the implementation in parallel
architecture-review  implementation checked against the design (2+ sub-agents)
gates                run the profile-declared checks (build, test, lint)
fix-loop             fix the review/gate failures → back to review
commit               commit the verified change
push-pr              push the branch and open the PR
pr-watch             watch PR checks and comments
pr-comment-fix       respond to review comments → pr-watch
pr-ci-fix            respond to CI failures → pr-watch
merge-approval       explicit user approval
merge                merge
handoff              write the handoff document
```

Review and verification are a loop, not a one-way street. A failure goes back to the fix phase,
and after the fix, the comment pass and the reviews run again before verification is re-run.

---

## Skills and profiles

52 skills ship with the kit, and only the ones the changed files and the active profile call
for are loaded. Reading all of them costs more context than the work can carry.

They fall into architecture (`clean-architecture-core`, `ddd-architecture`,
`domain-modeling`), platform (Android, iOS, React, React Native, Flutter, Python), development
discipline (`tdd`, `code-generation-discipline`, `comment-authoring-discipline`), review
(`code-review`, `architecture-reviewer`, `plan-reviewer`), requirement refinement (`grilling`,
`to-prd`, `product-brief`), and operations (`agent-flow`, `push-watch`).

10 profiles — `android` `flutter` `generic` `ios` `nextjs` `node` `python` `react-native` `spring` `typescript`

A profile declares the per-platform verification commands and review angles. The exact format
is in [docs/USAGE.md](docs/USAGE.md).

---

## Approaches dropped along the way

**Prompt optimization.** It started as tidying context and sharpening instructions. Output
quality went up, but it did not reproduce. With the same prompt giving a different result on a
different day, there was nothing to hand to a team.

**Self-reported checklists.** Markers asked "did you check X in this phase," and the agent
filled in the marker without checking. That is why the hooks now observe the actual run and the
runner reads the log itself.

**A single reviewer.** One review phase was not enough. When the session that wrote the code
reviews it, it passes. Independent sub-agents now run as separate processes, and the controller
session is blocked from standing in for a failed one.

**Requiring approval of the initial SPEC list.** Early on the whole SPEC list went to the user
for approval; it broke the flow and repeated the same confirmation every time. Confirming only
the delta cut that friction.

**A per-shell-command "write target" table.** The host write boundary tried to guess what a
command was about to write. Shell syntax is infinite and a list is finite, so only the
exceptions grew, and it stopped being possible to explain what was blocked. Pre-blocking is now
down to two rules (a protected path appearing literally, an irreversible command) and the rest
is left to after-the-fact detection.

---

## Open problems

It is stuck as a personal tool. It is my own criteria applied to my own projects, and I still
do not know how to get to something a team agrees on and trusts together.

Four things are unsolved.

- **A procedure for a team to agree on the verification criteria** — who decides the marker
  list, and how does it change. I decided it, so right now it fits only me.
- **Differing skill levels across a team** — I have not confirmed that the same gate means the
  same thing to someone who understands the process and to someone who looks for a way around
  wherever it blocks them.
- **The break-even point between the friction the process creates and the trust it buys** —
  there are many gates, bearable alone, but dropped into a team the first response will be "why
  does this take so long." I have no criterion for how much friction is worth paying.
- **The economics of reviewer sub-agent cost** — running reviewers in parallel doubles the
  token cost. I have never worked out the balance once that is multiplied by a team.

The first three are not technical problems; they are problems about people and organizations.
That adding automation is not the same as earning trust is what has become clearest from using
this tool.
