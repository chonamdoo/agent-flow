# Workflow Contract

The workflow runner is the source of truth for phase order. Agents may read skills and prompts, but must use `npx github:chonamdoo/agent-flow run next` and `npx github:chonamdoo/agent-flow run advance` to move through the workflow.

Phases with completion markers are not complete just because the artifact file exists. The artifact must include every required marker printed by `npx github:chonamdoo/agent-flow run next`.

Implementation rules:

- Run every phase through the runner. Do not skip review, QA, PR watch, or fix-loop phases.
- Code comments are required when intent is not obvious.
- Every new or modified code comment must be written in Korean.
- If review or QA fails, return to the fix phase before continuing.

Document size rules:

- `CONTEXT.md`, grill-me docs, grill-with-docs outputs, and long planning docs must stay under 200 lines each.
- If a source doc grows past 200 lines, create or refresh a matching `*-summary.md` under `.Codex/rules/` and use that summary as agent context.
- Preserve the original long doc only as reference; do not load it as hot context unless the current phase needs a specific section.
- Artifacts must link to long docs by repo-relative path and summarize only the needed decision, not paste the full content.

Context rules:

- Artifacts and manifests must use repo-relative paths; local absolute paths are forbidden.
- Do not paste full docs or raw logs into artifacts. Summarize and link by relative path.
- `CONTEXT.md` is hot context only and must stay under 200 lines.
- Current and future vocabulary must stay separated.
- Follow the phase context map in `.Codex/rules/context/` for phase-specific context loading.
