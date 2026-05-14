---
name: grill-with-docs
description: Internal agent-flow grilling skill. Use during domain-grill or design interview phases to challenge a plan against CONTEXT.md, CONTEXT-MAP.md, ADRs, and code before implementation.
---

# Grill With Docs

Use this for agent-flow domain interviews. Keep it short, code-aware, and documentation-backed.

## Rules

- Ask one question at a time.
- Provide a recommended answer with each question.
- If code or docs can answer the question, inspect those sources instead of asking.
- Challenge term conflicts against `CONTEXT.md`, `CONTEXT-MAP.md`, and ADRs immediately.
- Sharpen fuzzy or overloaded words into one canonical term.
- Stress-test domain relationships with concrete edge-case scenarios.
- Cross-check user claims against code before treating them as facts.
- Update `CONTEXT.md` inline only when a domain term is resolved.
- Keep `CONTEXT.md` glossary-only. Do not put implementation details, specs, or scratch notes there.
- Keep expanded glossary/rationale in `.Codex/rules/context/` instead of hot `CONTEXT.md`.
- Create `CONTEXT.md`, `CONTEXT-MAP.md`, and ADR files lazily; only write them when there is resolved content.
- Offer ADRs only when the decision is hard to reverse, surprising without context, and a real trade-off.

## Artifact

```markdown
# Domain Grill

## Goal

## Resolved Decisions

## Domain Map

## Open Questions

## Terms Defined / Updated in CONTEXT.md

## ADRs Created

## Risky Assumptions

## Existing Sources Checked

## Completion Gate

grill-with-docs: complete
shared_understanding: reached
context_docs_checked: true
context_docs_updated: true|not_needed
```
