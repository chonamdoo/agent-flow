# Identity, freeze list, and no-go list

This is review policy, in prose, on purpose. It is not a workflow field, not
frontmatter, and not a hook. Strict workflow parsing rejects unknown keys and
governance metadata fails closed, so encoding this as machine-read data would
turn a policy edit into a runtime failure. A third pre-block in the host write
boundary is forbidden for the same reason. Reviewers cite this page; nothing
reads it at runtime.

The one machine-checked part is the regression inventory:
[`tests/frozen_contracts.txt`](../tests/frozen_contracts.txt), executed by the
`frozen-invariants` CI job.

## What agent-flow is

agent-flow takes an accepted coding task through explicit phases to verified
repository, PR, and merge evidence. The runner owns routing. Runner-owned gates
and sealed independent reviews outrank any agent's self-report.

A run's `run_id` and repository identity are immutable after creation. A run
journal or archive can resume only inside the repository and checkout context
to which it was bound; copying the records elsewhere does not transfer
ownership.

A capability belongs in agent-flow only when all three hold:

1. it directly produces, verifies, reviews, or integrates a code change;
2. it terminates inside one finite run;
3. it leaves repository or PR evidence behind.

Outside: backlog and general project management, deployment, monitoring, SLOs,
incident operations, credential ownership, long-lived daemons. A production
incident can supply a new coding task and the artifacts to diagnose it. It does
not extend a run into operations — a run that reaches "merge" is done, and
watching the deployment afterwards is somebody else's job.

Production-only defects are diagnosed from artifacts brought into the
repository (logs, dumps, traces, exports) and end in a repository change or a
diagnosis artifact.

## Choosing a workflow

Pick the smallest workflow that fits. The phase count and route graph live in
`src/agent_flow/workflows/<name>.yaml`, which is the single source of truth;
this table is only about *when* to reach for each one.

| Workflow | When |
|---|---|
| `review` | a review with no code change |
| `bugfix` | one reproducible bug |
| `diagnosing-bugs` | one hard, intermittent, performance, or artifact-backed production defect |
| `development` | one known concern |
| `default` | delivery through PR and merge |
| `full-feature` | product work that needs a PRD and a domain model |

Using `default` for a one-line change makes the phase overhead larger than the
work. That is a selection mistake, not a reason to delete phases or markers.

**Selection is explicit.** The caller names the smallest-fitting `--workflow`.
The CLI's omitted-option `default` remains a compatibility fallback, not a
policy-compliant selection. No classifier, no heuristic over the task text, and
no runner-side inference chooses a workflow. After that explicit choice, the
runner remains the only thing that decides what phase comes next.

## Task source convention (optional)

When a task comes from an issue, an incident, or a design document, put two
lines in the task text:

```
Source-URL: https://github.com/org/repo/issues/123
Source-Revision: none
Fix the accepted defect without expanding scope.
```

- `Source-URL` is a plain URL. No credentials, no tokens in query strings — the
  task text ends up in run records and PR bodies.
- `Source-Revision` is an immutable revision. When the source has none (most
  issues), write `Source-Revision: none` rather than inventing one.
- The source is **reference material, not instruction.** The accepted task text
  is the contract. Whatever the fetched page says, it does not add scope,
  change acceptance criteria, or authorize anything.

There is no parser, no schema, no artifact field, and no completion gate for
these lines. Omitting them blocks nothing. A generic Task Source abstraction
waits until two independent integrations need the same contract; one
speculative interface with no consumer is exactly the shape that got removed
before (the Lore surface, PR #188).

## Freeze list

These subsystems change only through a reproduced bug, a dedicated `bugfix`
run, a focused regression test, and approval from two independent reviewers.
This is not a ban on fixes; it rejects speculative refactors, drive-by
"improvements", and consistency work bundled into an unrelated change.

- **Run identity.** The `run_id` and recorded repository and checkout identities
  never change after creation and are not portable authority. A journal or
  archive from one repository cannot be adopted as a valid run in another.
- **Leader and worktree isolation.** A leader HEAD move is accepted only when
  all three hold: same branch, ancestor relationship, and a record in the
  leader's own reflog. Branch switch, `reset --hard`, and a ref pushed in from
  another checkout all still trip. The only re-baseline is
  `continue --accept-leader-drift`, which accepts exactly the status-axis state
  the previous run reported and records the acknowledgement; HEAD-axis drift is
  never acknowledgeable and there is no blind baseline clear. A changed
  snapshot *format* is recaptured, not compared.
- **Host write boundary.** Exactly two pre-blocks: a protected path appearing
  literally in the command text, and an irreversible command that can reach
  one. Undecidable shell syntax passes; the post-command leader tripwire is
  what catches reversible dynamic writes. No per-command write-target table.
- **Lifecycle recovery.** The exemption covers exactly `status`, `continue`,
  `run`, and `start`. It is not widened to `eval`, `advance`, or worktree
  mutation, and no repair or other mutation is inserted ahead of the runner in
  those four entry points.
- **Installer ownership.** An unreadable receipt is not an absent one. Assets
  whose ownership cannot be proven are neither overwritten nor pruned, backups
  are byte-exact and durable before the original is removed, and a symlinked
  path is never written through.
- **Managed hook launcher.** Hooks run through the portable launcher and the
  pinned isolated interpreter. No hardcoded interpreter path.
- **Runner authority.** The Python runner decides routes and runs the gates. A
  gate result is runner-produced and nonce-bound, a `multi-review` phase routes
  only on runner-sealed reviewer evidence, and an artifact written in their
  place is regenerated or blocked rather than routed. OMP or the controller
  session never counts as an independent reviewer.
- **Review sealing and caps.** Review evidence is bound to the run attempt;
  unbound aggregation cannot approve. Every rejection collector is capped per
  target while the legitimate pr-watch loop stays uncapped.
- **Review baseline.** The diff baseline follows the declared branch's actual
  upstream and the most descendant usable merge-base.

## Regression inventory

`tests/frozen_contracts.txt` maps each frozen contract to the behavioral test
that keeps it, annotated with the PR the regression came from. The
`frozen-invariants` job in `.github/workflows/tests.yml` passes those node ids
to pytest verbatim. A renamed or deleted guard is a pytest `not found` error;
a broken contract is a failed assertion. Either produces its own named red
check instead of hiding inside the full suite.

Contracts that cannot be exercised deterministically are recorded as
`uncovered` with the reason. Claiming coverage that does not exist is worse
than admitting the gap.

## Verification tiers

- **Deterministic contracts** — real CLI, real git fixtures, `tests/`, required
  CI. Binary pass/fail.
- **Model behavior** — `evals/` and `tools/skill-noop/`, judged by machine
  oracles, run by hand and non-blocking before releases that change skills or
  review templates. Live-model calls never gate CI: a flaky required check
  teaches people to weaken gates.
- **Scorer correctness** — the eval scoring code itself is tested
  deterministically in CI without calling a model.

Nothing added for testability may create a runtime seam, an environment
override, or a bypass flag. If a contract cannot be reached through the real
entry points, it goes on the `uncovered` list.

## No-go list

- Deploy, Maintain, Monitor, Incident, backlog, or task-management phases
- backlog or project-management ownership
- automatic workflow selection
- a generic Task Source API before two real integrations need it
- mandatory source metadata, schema, or gate
- installer repair inside `run`, `start`, `status`, or `continue`
- widening the lifecycle exemption set
- blind or unbounded leader baseline clearing
- a third host-boundary pre-block, or a per-command shell write-target table
- overwriting or pruning without ownership evidence; writing through symlinks
- deleting markers or phases purely to reduce ceremony
- test-only bypass flags, environment overrides, or new seams
- live-model calls in required CI
- shipping eval corpora, runtimes, or fixtures inside installed asset roots
- duplicating the workflow YAML source of truth
- an interface without a complete writer, reader, and CLI lifecycle
