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
agent-flow start development --root /path/to/project --task "add login"
agent-flow status --root /path/to/project
agent-flow detect-profile --root /path/to/project
```

