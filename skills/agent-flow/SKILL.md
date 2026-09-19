---
name: agent-flow
description: Start or resume an explicitly requested Agent Flow project workflow.
disable-model-invocation: true
---

# Agent Flow

Apply this lifecycle only after explicit invocation or an explicit request to resume a run. Installation, plugin enablement, opening a project, and reading this skill do not start a workflow or authorize status guidance. Ordinary project rules and runtime protection remain in force outside this lifecycle.

When the confirmed project has an installed runtime, prepend its absolute `.agent-flow/bin` directory to the current shell's `PATH` before status, lifecycle commands, or their printed `next_command`. Do not let an older global CLI or a plugin-cache launcher select a different runtime.

## Slash Trigger

Dispatch exact inputs in this order:

1. For `/agent-flow status`, run `agent-flow status` from the project root and report its output.
2. For an explicit task or resume request, run `agent-flow status` from the requested checkout before choosing a lifecycle command.
3. When status reports an active run, identify its task, run ID, and checkout. Execute its printed `next_command` only when the current request explicitly resumes that run. Discovery alone is not permission to adopt another run. For a new task or an unclear/mismatched resume target, report the active run and ask which task/run the user intends; do not advance it or start a second run before resolving the target.
4. When status exits 0 and reports no active run:
   - `/agent-flow` with no task asks the user for `/agent-flow <task>`.
   - For `/agent-flow <task>`, choose an existing workflow by purpose and requested completion scope, then run `agent-flow run "<task>" --workflow <name>`.

| Purpose and completion scope | Workflow |
| --- | --- |
| Read-only review, local handoff | `review` |
| One reproducible bug, local handoff | `bugfix` |
| Hard or intermittent bug, local handoff | `diagnosing-bugs` |
| One implementation concern, local handoff | `development` |
| Implementation through PR and merge, when authorized | `default` |
| PRD and domain design through delivery, when authorized | `full-feature` |

Use the installed workflow YAML as the source of truth; do not maintain phase counts here. The four local-handoff workflows do not authorize publication or merge. If requested completion scope is unclear and materially changes the choice, ask before starting. Never replace an active run's workflow or pins.

Treat the status command output as the only source of truth. Use the `run` output for the actual worktree, branch, phase, and next command instead of predicting them.

Do not run install just because a new session started. Install is project setup, not the normal task entry. Do not infer npm, npx, or install failure unless the command exits non-zero with that error.

Existing legacy installations retain their root operating instructions on ordinary updates. Only an explicit root-context conversion request authorizes `install --migrate-root-context`, passed to the same verified installer used for project setup. It requires an unchanged receipt-owned block and preserves project rules, imports, indexes, and backups. Missing or ambiguous ownership requires resolution; `--force-managed` is not permission to bypass it. Never edit a parent directory's shared rules.

## SPEC Change Confirmation

The initial SPEC list is baselined without a separate approval step. When status reports later additions, modifications, or deletions:

- Show only that delta and ask the user for confirmation in the current chat.
- After a clear affirmative reply, run the printed `agent-flow spec confirm --run-dir <run-dir>`.
- For a `manual` verifier, ask in chat and then run `agent-flow spec approve <spec-id> --run-dir <run-dir>`.

Never require an exact phrase or ask the user to enter a terminal command.

## Checkout, Verification, and Completion

- Install once as explicit project setup, not on session startup. Native discovery, enablement, project installation, hook trust, invocation, and run participation are separate states; none proves the others succeeded.
- The CLI attaches or creates and prepares the working checkout before the first phase. Use its reported checkout. A later worktree phase verifies and records that binding; it does not create another checkout.
- When a new checkout is actually required, use `agent-flow worktree create --name feat-<slug>`, never raw `git worktree add`. Do not install or regenerate skill links in a linked worktree.
- The active profile controls branching, base, PR target, and gates, even when another skill suggests a different base, target or branch deletion. Express release-first through the profile, and leave topic-branch cleanup to its workflow phase and protected-branch guard. Do not switch the leader branch or run IDEs, builds, tests, or lint there. Keep verification in the bound checkout and respect CI-only gates. Explicit project installation belongs in the leader checkout, never a linked worktree.
- Apply the architecture contract selected by the project. Preserve Clean obligations, local required references and scoped norms, pending-mode structural restrictions, and the separate DDD review axis. Plugin discovery is not norm activation.
- `multi-review` requires at least two actual independent Claude/Codex CLI reviewer subprocesses. OMP is controller only. A failed model request or controller investigation is not a review. Keep `reviewer-source: sub-agent` and the required `## Overall` verdict markers.
- Complete the active workflow's evidence, review and gate requirements before claiming its completion disposition. Local handoff is not PR/merge completion; publication, merge, release and cleanup still require their applicable authorization.
- Do not weaken protected-branch or leader/sibling/runtime guards. On drift, stop and follow the reported recovery contract; never infer permission to reset a baseline or reuse a prior acknowledgement.
- Preserve active run state, workflow pins and evidence in project-owned runtime storage when a plugin is disabled, removed or its cache disappears. Plugin removal does not authorize removing project protection.

## Behavior

- Treat `/agent-flow` as a project-local workflow trigger, not as a shell path.
- Keep git-project runtime state private under the repository git dir, such as `.git/agent-flow/worktrees/feat-<slug>/`; expose it only for status, debugging, or artifact inspection.
- After a phase writes its artifact, run the `next_command` printed by status or the current phase output.
- If the workflow pauses for design or slice review, summarize the relevant artifact and wait for user approval before continuing.
- During code generation, modification, and code review phases, apply `code-generation-discipline`. Skill resolution and missing-skill handling are defined there; do not restate them here.
- Keep user-facing replies short and in the language the user writes in. Keep code, commands, paths, and identifiers in English.
- Do not paste long logs or whole files. Summarize only current phase, action, `next_command`, and blocker when useful.
