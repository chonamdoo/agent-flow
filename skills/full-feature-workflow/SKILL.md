---
name: full-feature-workflow
description: Use this skill for feature work in this project.
---

# Full Feature Workflow

Use this skill for feature work in this project.

Always drive progress through the runner output. Run `agent-flow status`, then execute the printed `next_command` exactly.

Do not skip phases. If existing docs satisfy a phase, write the required artifact and reference those docs. If a gate, review, PR comment, or PR check fails, complete the matching fix phase and push again before merge/handoff.

Apply `code-generation-discipline` during code and review phases. Resolve required skills from active profile metadata, installed skill index, changed files, and task scope before writing or judging code.
