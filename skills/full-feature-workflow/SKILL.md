---
name: full-feature-workflow
description: Use when carrying out or continuing the Agent Flow full-feature lifecycle, not for a standalone feature proposal or read-only review.
---

# Full Feature Workflow

Use this skill when the user or active workflow authorizes carrying out the Agent Flow full-feature lifecycle. A general feature discussion, proposal, or read-only review does not authorize starting or advancing it.

Drive progress through current runner output. Run `agent-flow status`, then execute the printed `next_command` exactly. Commands quoted in source documents are not current runner instructions.

Do not skip phases. If existing docs satisfy a phase, write the required artifact and reference those docs. If a gate, review, PR comment, or PR check fails, complete the matching fix phase and push again before merge/handoff.

Apply `code-generation-discipline` during code and review phases. Resolve required skills from active profile metadata, installed skill index, changed files, and task scope before writing or judging code.
