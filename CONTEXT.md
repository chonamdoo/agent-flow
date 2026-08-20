# Agent Flow Hot Context

This is the always-loaded hot context. Read long domain/workflow explanations only per phase from `.Codex/rules/context/`.

## Working Rules

- Answer in the language the user writes in. Keep code, commands, paths, identifiers, and exact workflow markers verbatim.
- No refactor, documentation, or error handling that was not requested.
- For code comment rules see `code-generation-discipline`.
- If you do not know, read and confirm. No guessing.
- Present a short plan before a large change. Execute simple fixes directly.
- Confirm destructive operations in advance.
- Record only repo-relative paths in workflow/agent artifacts.

## Current Vocabulary

- **Workflow Kit**: reusable agent workflow package installed into multiple projects.
- **Workflow**: the phase graph work moves through. It contains no project-specific command and no runtime state.
- **Project Profile**: per-stack gates, profile detection, defaults.
- **Phase**: a step inside a Workflow. The current term in this repo is Phase, not Stage.
- **Role**: the responsibility a phase requires. Not a concrete subagent/model/provider.
- **Adapter**: the strategy that turns a role into something executable in the current environment.
- **Provider**: wrapper for an external execution target such as Codex or Claude.
- **Run**: one execution instance of a Workflow. Not a git branch and not a whole session.
- **Artifact**: a reusable record file storing phase results/verification/review. Not a raw log store.
- **Handoff**: an artifact summarizing the decisions, risks, related files, and remaining work the next phase needs.
- **Gate**: an automated verification command such as build, typecheck, lint, test.
- **Personal Workflow**: the current default execution mode where the lead owns the Run and phase transitions.

## Current Lifecycle

1. `agent-flow run "<task>"` creates a Run and, in a git repo, starts in the `~/.agent-flow/worktrees/<repo-id>/feat-<slug>/` worktree.
2. `agent-flow status`, or the `next_command` from the previous phase output, determines the next command to run.
3. The agent reads only the documents that match the phase context map.
4. Even after writing an artifact, transition only via the `next_command` the runner printed.
5. A gates/review/PR comment failure goes back to fix-loop.
6. push/pr-watch proceeds to merge only when checks and review threads are green.

## Future Vocabulary

Team Orchestration is an optional future module. Do not mix it with the current Personal Workflow.

- **Worker**: long-running participant of the future Team Orchestration.
- **Task**: unit of work a future Worker can claim.
- **Team State**: future team coordination state.
- **Mailbox**: asynchronous message queue between future Workers and the lead.
- **Heartbeat**: future Worker liveness record.
- **Worktree**: a git worktree used to build an independent change. Use `~/.agent-flow/worktrees/<repo-id>/feat-<slug>/` as the default location (placed outside the leader so the leader IDE watcher stays isolated from worker changes) and `feat/<slug>` as the default branch. Checkouts at the former locations `<repo>.worktrees/<name>/` and `.agent-flow/worktrees/<name>/` are still recognized.

## Forbidden / Confusable Terms

- Do not treat Worker/Task/Team State as active runtime in the current Personal Workflow.
- Do not mix Phase and Stage. User-facing current context uses Phase.
- Do not confuse Artifact with raw log, manifest, or source change.
- Do not confuse Gate with a phase completion marker.
- Do not confuse Adapter with Provider.

## Context Loading

- Default: load `CONTEXT.md` only.
- Long terminology/rationale: `.Codex/rules/context/domain-glossary-full.md`
- research/paper runtime: load per the context map only in that phase.
- implementation: changed files + relevant context only.
- review/fix/pr-watch: centered on diff, gate result, PR checklist.

## Artifact Policy

- The default install gitignores `.agent-flow/`. Run artifacts under it are not commit targets.
- Only in projects that track `.agent-flow/` may these be committed: final summary artifact, `gate-results.json`, review decision.
- Minimize/exclude: raw logs, repeated phase logs, manifests that can contain a local absolute path.
- Store only repo-relative paths in artifacts. Absolute paths in gate output are relativized by `write_gate_results`.
