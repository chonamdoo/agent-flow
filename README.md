# agent-flow

Personal agent workflow kit for project-agnostic development loops.

## Goals

- Keep the lead session focused on orchestration.
- Run work through stage-scoped subagent prompts and artifacts.
- Keep workflow logic independent from Codex, Claude, Gemini, or manual execution.
- Let project profiles define stack-specific gates.

## MVP Scope

- Sequential personal workflow runner.
- Review stage can declare parallel replicas.
- Adapter interface for `manual`, `codex-session`, `claude-session`, and future CLI providers.
- Project-local run artifacts under `.agent-flow/runs/`.

## Layout

```text
workflows/        reusable stage graphs
profiles/         project stack gate definitions
roles/            default role responsibilities
src/agent_flow/   Python runner package
templates/        adapter-specific prompt templates
```

## Example

```bash
agent-flow init --root /path/to/project
agent-flow start development --root /path/to/project --task "add login" --adapter auto
agent-flow record-stage --root /path/to/project --run-dir .agent-flow/runs/development/<run-id> --stage explore --content "..."
agent-flow handoff --root /path/to/project --run-dir .agent-flow/runs/development/<run-id> --from-stage explore --to-stage implement --decided "..." --remaining "..."
agent-flow review-summary --root /path/to/project --run-dir .agent-flow/runs/development/<run-id> --reviews .agent-flow/runs/development/<run-id>/artifacts/review-1.md
agent-flow worktree create --root /path/to/project --name implement-login
agent-flow worktree status --root /path/to/project --name implement-login
agent-flow team list --root /path/to/project
agent-flow team init --root /path/to/project --name feature-team --description "login work"
agent-flow team archive --root /path/to/project --team feature-team --reason "work complete"
agent-flow team archive-list --root /path/to/project
agent-flow team archive-export --archive-path /path/to/project/.agent-flow/archive/team/<archive-id>
agent-flow team archive-restore --root /path/to/project --archive-path /path/to/project/.agent-flow/archive/team/<archive-id>
agent-flow team archive-restore --root /path/to/project --archive-path /path/to/project/.agent-flow/archive/team/<archive-id> --report /path/to/restore-report.json
agent-flow team task --root /path/to/project --team feature-team --id task-1 --subject "Implement login"
agent-flow team worker --root /path/to/project --team feature-team --name worker-1 --role implementer
agent-flow team claim --root /path/to/project --team feature-team --task task-1 --worker worker-1
agent-flow team complete --root /path/to/project --team feature-team --task task-1 --claim-token <token> --result "done"
agent-flow team message --root /path/to/project --team feature-team --from-actor lead --to-worker worker-1 --body "Please check auth tests"
agent-flow team messages --root /path/to/project --team feature-team --worker worker-1 --unread-only
agent-flow team mark-read --root /path/to/project --team feature-team --worker worker-1 --message <message-id>
agent-flow team heartbeat --root /path/to/project --team feature-team --worker worker-1 --status reviewing
agent-flow team shutdown --root /path/to/project --team feature-team --worker worker-1 --reason "slice complete"
agent-flow team ack-shutdown --root /path/to/project --team feature-team --worker worker-1 --signal <signal-id>
agent-flow team status --root /path/to/project --team feature-team
agent-flow team status --root /path/to/project --team feature-team --detail
agent-flow team export --root /path/to/project --team feature-team
agent-flow team import-validate --file /path/to/team-state.json
agent-flow team import-dry-run --file /path/to/team-state.json
agent-flow team import-dry-run --file /path/to/team-state.json --report /path/to/import-report.json
agent-flow team import-apply --root /path/to/project --file /path/to/team-state.json
agent-flow team import-apply --root /path/to/project --file /path/to/team-state.json --report /path/to/import-report.json
agent-flow gates --root /path/to/project --profile auto
agent-flow status --root /path/to/project
agent-flow detect-profile --root /path/to/project
```

## Team State Import

Team State import is intentionally non-destructive.

- `import-validate` checks snapshot shape and references.
- `import-dry-run` reports the import summary without writing Team State.
- `import-apply` creates a new Team State only when the target team does not already exist.
- `--report` writes the same success or failure summary as deterministic JSON.
- Failed apply attempts remove partially created Team State and report cleanup diagnostics.
