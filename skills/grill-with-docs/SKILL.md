---
name: grill-with-docs
description: Runs the grilling interview while also maintaining domain-modeling outputs such as glossary updates and ADRs. Use when the user wants to grill or stress-test a plan and explicitly wants decisions documented as the conversation progresses; pair this with `grilling` and `domain-modeling`, not as a standalone workflow.
disable-model-invocation: true
---

# Grill With Docs

This is a paired workflow, not a standalone skill. Run the `/grilling` interview pattern and the `/domain-modeling` capture discipline together.

## Quick start

1. Start a `/grilling` session: one load-bearing question at a time, with a recommended answer.
2. When a domain term is clarified, update `CONTEXT.md` using `/domain-modeling`.
3. When a hard-to-reverse, surprising trade-off is settled, offer an ADR using `/domain-modeling`.
4. Do not implement the plan; stop when the shared understanding and docs are current.

Use plain `/grilling` when the user only wants an interview. Use `/domain-modeling` alone when the user only wants glossary or ADR work.
