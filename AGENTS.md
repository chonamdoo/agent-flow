# Project Instructions

- Do not add refactors or documentation that was not requested.

## Architecture

```text
bin/agent-flow-kit.mjs  ← JS entry point (install, artifact validation, push-watch). It does no phase routing
src/agent_flow/         ← Python CLI (runner, adapters, gates, multi-review, worktrees)
src/agent_flow/workflows/  ← Source YAML (full-feature, bugfix, review, and so on). The source of truth is this one copy
src/agent_flow/profiles/   ← Per-stack profiles (android, nextjs, python, and so on)
skills/                 ← Source skills (install copies them into the target .agent-flow/skills/)
templates/              ← Review angle templates (_shared/review/)
bootstrap/              ← AGENTS/CLAUDE.md template
scripts/hooks/          ← PreToolUse/PostToolUse/Stop hooks (guard-protected-branch, comment-checker, record-skill-read)
.Codex/agents/          ← code-reviewer.md (review criteria)
```

## Key Files

- `src/agent_flow/workflows/*.yaml`: the single source of truth for phase order and routes. Do not keep a copy at the repo root — two copies mean a separate check to keep them equal. JS consumes the Python `workflow export` JSON (`bin/agent-flow-kit.mjs` `exportWorkflowDefinition()`).
- `bin/agent-flow-kit.mjs`: the JS entry point for install and installed-asset sync. It hands `start`/`status`/`next`/`advance` to the Python CLI and never advances the run lifecycle. The only place it writes its own state is `push-watch`.
- `src/agent_flow/core/phase_workflow.py`: the workflow YAML loader. `routes` parsing and route target validation live here.
- `src/agent_flow/runner.py`: the Python runner. **It is the routing authority** — the decision of which route to take and the fix-loop round cap live here.
- `src/agent_flow/profiles/_schema.yaml`: the profile field schema (gates, branching, worktree).
- `skills/code-generation-discipline/SKILL.md`: the canonical source for code generation criteria.

## Gotchas

- `full-feature` cycles `gates` fail → `fix-loop` → `gates`. In `default`, gates are forced by the `implement` completion marker.
- `multi-review` requires at least two installed Claude/Codex CLI reviewer subprocesses. OMP is host/controller only and is excluded from the reviewer provider pool; approve is impossible without 2+ independent reviewers.
- An `architecture-review` verdict of `request-changes`/`blocked` routes to `refactor`.
- Create worktrees with `agent-flow worktree create --name feat-<slug>`. Their default location is `~/.agent-flow/worktrees/<repo-id>/<name>` — put them inside the project folder and an IDE left open on the leader reacts to worktree work, touches leader caches such as `.gradle/`, and the leader tripwire reports that as contamination, blocking every remaining phase with exit 2. Checkouts at the former locations (`<repo>.worktrees/<name>`, `.agent-flow/worktrees/<name>`) are still recognized. Do not switch the leader worktree's branch with `git checkout`/`git switch`.
- Any other linked worktree (Orca's `~/orca/workspaces/<repo>/<slug>`, for example) is recognized only after `agent-flow worktree adopt --path <checkout>`. Running `run`/`start` inside one before adoption is a blocker — authorizing on git registration alone lets a worker mint its own permission with `git worktree add`. Always install from the leader checkout (running it in a linked worktree is blocked).
- The host write boundary (`scripts/hooks/guard-host-worktree.sh` → `core/host_write_boundary.py`) has **exactly two pre-blocks**: refuse when a protected path (the leader, a registered sibling checkout, runtime state) appears literally in the command text, and refuse when an irreversible command (`rm`/`rmdir`/`shred`/`mv`, `git checkout|restore|clean|reset`) can reach that path. Do not rebuild a per-command "write target" table — shell syntax is infinite and a list is finite, so that direction only grows exceptions and makes it impossible to say what is blocked (it breaks if `tests/test_host_write_boundary.py::test_pre_block_surface_stays_two_rules` comes back to life).
- **Reversible** writes through dynamic paths (`$(...)`, variables) and through symlinks inside a worktree are not pre-blocked. On the leader side the PostToolUse tripwire (`scripts/hooks/worktree-tripwire.py`) catches them by comparing content after every command. Sibling checkouts and unbound sessions have no after-the-fact detection (`worktree-tripwire.py:65,68`) — that is why the literal block for those two is never relaxed.
- Undecidable is not blocked. Let a failed shell parse or an undeclared cwd through (destructive commands are the only exception), and instead let lifecycle commands (`status`/`continue`/`run`/`start`) through **in every state**. When the command that clears a deadlock the boundary created sits behind that boundary, the number of ways out is zero. Do not widen that exemption list — an exempted path skips the literal block, the destructive list, and the tripwire all at once, so `eval --judge-command` becomes a channel for arbitrary argv execution and `worktree remove --name` deletes a sibling checkout (`tests/test_host_write_boundary.py::test_lifecycle_exemption_stays_narrow` guards this).
- A leader that fast-forwards normally is not drift. The HEAD relaxation is granted only when **all three** hold: the same branch, an ancestor relationship, and the leader's own reflog record (`core/worktree_isolation.py:_head_drift_kind`). That is why there is no command to clear a stale baseline by hand — instead `reset --hard` (not an ancestor), a branch switch, and a ref pushed in from outside all still trip. A record whose snapshot **format** changed is recaptured instead of compared (`LeaderSnapshot.version`) — reporting a format difference as contamination would block every run in flight with no basis.

The agent-flow block below is the canonical source for the Workflow Contract and Context Economy. Do not duplicate them here.

<!-- agent-flow:start -->
## Agent Flow

- Start every new session with `agent-flow status`. If a run is active, run that output's `next_command` verbatim; if none is active, start with `agent-flow run "<task>"`. Never guess at `agent-flow continue` or `agent-flow run advance`.
- Install runs once per project. Do not run it again just because a new session started.
- `/agent-flow` is a skill trigger, not a shell path. The entry procedure, SPEC review and approval, and run artifact locations are in `.agent-flow/skills/agent-flow/SKILL.md`.

### Workflow Contract

- Pick the workflow by task size: `agent-flow run "<task>" --workflow <name>`. The number in parentheses is the phase count and the source of truth is `.agent-flow/workflows/<name>.yaml`. Omit it and you get `default`; using `default` for a small change makes the phase overhead larger than the work itself.
  - `review`(3) review with no code change · `bugfix`(5) one reproducible bug · `diagnosing-bugs`(9) one hard or intermittent bug · `development`(6) one concern · `default`(15) through PR and merge · `full-feature`(24) from PRD and DDD
- The status output is the source of truth for the current phase and the next command. Do not keep a copy of the phase list in this file.
- `multi-review` requires at least two installed Claude/Codex CLI reviewer subprocesses. Leave `reviewer-source: sub-agent` in each result, and at the end write only `## Overall` plus `verdict: approve` or `verdict: request-changes`. Use OMP as host/controller only, never as a reviewer provider.
- The active profile's `branching`/`pr` is the source of truth for branching and PR target. Follow the profile even when a skill document dictates a different base, PR target, or branch deletion. Express release-first through the profile's `branching.strategy`/`base`/`integration`/`pr.target_branch`, and leave topic branches to the cleanup phase and the protected-branch hook — do not replace that with the `git branch -D` a skill dictates.
- Do not run an IDE, Gradle, or a build in the leader checkout. Build output in the leader makes the phase-boundary tripwire report it as drift and the run stops. Open builds, tests, and IDEs only in a bound worktree.
- Take build/test/lint commands only from the active profile's `gates`. Do not repeat verification commands that no gate declares.
- Create worktrees only with `agent-flow worktree create --name feat-<slug>`. Do not run `git worktree add` by hand, and do not run install or regenerate skill links inside a worktree.
- The bans on protected-branch commit/push and on leader checkout/switch hold identically on every host. The hook blocks them automatically.

### Context Economy

- Keep answers short and in the language the user writes in; keep code, commands, paths, identifiers, and exact workflow markers verbatim.
<!-- agent-flow:skills:start -->
```text
[agent-flow skill index]|root: .agent-flow/skills
|IMPORTANT: The files below outrank memory. Skim what you are about to change, and read only what your scope touches.
|always:{code-generation-discipline,comment-authoring-discipline}
|on-demand:{agent-flow,agent-flow-concise-output,agent-flow-diagnosing-bugs,architecture-reviewer,clean-architecture,clean-architecture-core,code-review,codebase-design,comment-checker,ddd-architecture,domain-modeling,full-feature-workflow,grill-with-docs,grilling,plan-reviewer,product-brief,push-watch,python-api-clean-architecture,python-development-guide,resolving-merge-conflicts,tdd,to-prd,write-for-work}
```
<!-- agent-flow:skills:end -->
<!-- agent-flow:docs:start -->
```text
[agent-flow docs index]|root: docs
|IMPORTANT: Paths only. Read a document when you need it; do not move its body here.
|docs:{GETTING-STARTED.md,mattpocock-skills-upstream-audit.md,PLAN.md,semantic-clean-architecture-code-review.md,semantic-clean-architecture-skill-audit.md,TEAM-ADOPTION.md,USAGE.md}
|docs/adr:{0001-separate-core-from-environment-adapters.md,0002-use-stage-artifacts-for-subagent-first-workflows.md,0003-keep-team-orchestration-optional.md,0004-exclude-sandboxed-ai-cli-execution.md,0005-prefer-team-state-archive-before-delete.md,0006-add-domain-and-architecture-gates-to-full-feature.md,0006-use-push-watch-as-pr-automation-entrypoint.md,0007-hosted-remote-sandbox-queue-session-infra.md}
|docs/issues:{0001-host-provider-discovery.md,0002-worktree-backed-run-start.md,0003-team-runtime-tracer.md,0004-installable-workflow-kit.md,0005-domain-ddd-full-feature-phases.md,0005-push-watch-workflow.md,0006-enforce-ddd-architecture-intent.md}
|docs/ko:{GETTING-STARTED.md,TEAM-ADOPTION.md}
```
<!-- agent-flow:docs:end -->
<!-- agent-flow:end -->
