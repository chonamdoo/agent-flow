# Workflow Contract

The workflow runner is the source of truth for phase order. Agents may read skills and prompts, but must use `npx github:chonamdoo/agent-flow run next` and `npx github:chonamdoo/agent-flow run advance` to move through the workflow.

Phases with completion markers are not complete just because the artifact file exists. The artifact must include every required marker printed by `npx github:chonamdoo/agent-flow run next`.

Context rules:

- Artifacts and manifests must use repo-relative paths; local absolute paths are forbidden.
- Do not paste full docs or raw logs into artifacts. Summarize and link by relative path.
- `CONTEXT.md` is hot context only and must stay under 200 lines.
- Current and future vocabulary must stay separated.
- Follow `.Codex/rules/context/agent-flow-context-map.md` for phase-specific context loading.
