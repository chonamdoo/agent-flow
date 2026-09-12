---
name: domain-modeling
description: Build and sharpen a project's domain model. Use when discussing codebase terminology or a ubiquitous language, writing or editing a CONTEXT.md, recording or editing an ADR, or when another skill needs to maintain the domain model.
---

# Domain Modeling

Actively build and sharpen the project's domain model as you design. This is the *active* discipline — challenging terms, inventing edge-case scenarios, and writing the glossary and decisions down the moment they crystallise. (Merely *reading* `CONTEXT.md` for vocabulary is not this skill — that's a one-line habit any skill can do. This skill is for when you're changing the model, not just consuming it.)

## File structure

Discover the repository's existing documentation conventions before choosing a
location. Most repos have a single context, with a root `CONTEXT.md` and
system-wide decisions in `docs/adr/`.

If a root `CONTEXT-MAP.md` exists, read it to locate each context and its glossary.
Keep system-wide ADRs separate from context-specific ADRs at the locations
established by that map or the repository's conventions. A source directory alone
does not establish a business context; use the agreed meaning and ownership
boundaries.

Create files lazily — only when you have something to write. If no `CONTEXT.md` exists, create one when the first term is resolved. If no `docs/adr/` exists, create it when the first ADR is needed.

## During the session

### Challenge against the glossary

When the user uses a term that conflicts with the existing language in `CONTEXT.md`, call it out immediately. State the documented meaning and the meaning implied by the current discussion, then ask which meaning is intended.

### Sharpen fuzzy language

When the user uses vague or overloaded terms, identify the distinct concepts the term might refer to and propose a precise canonical term grounded in the context's agreed language.

### Discuss concrete scenarios

When domain relationships are being discussed, stress-test them with specific scenarios that probe edge cases and clarify boundaries between concepts. Label invented scenarios as hypotheses for discussion, not agreed business rules.

### Cross-reference with code

When the user states how something works, check whether the code agrees. Surface any contradiction between observed implementation and stated intent, and resolve which behavior is current versus intended. Code is evidence of implementation, not authority to override the user's business meaning.

### Update CONTEXT.md inline

When a term's meaning is confirmed and editing is authorized, update `CONTEXT.md` right there rather than batching resolved terms. For review- or discussion-only requests, propose the update without editing; agreement on a definition is not itself permission to change files. Use the format in [CONTEXT-FORMAT.md](./CONTEXT-FORMAT.md).

`CONTEXT.md` should be totally devoid of implementation details. Do not treat `CONTEXT.md` as a spec, a scratch pad, or a repository for implementation decisions. It is a glossary and nothing else.

### Offer ADRs sparingly

Only offer to create an ADR when all three are true:

1. **Hard to reverse** — the cost of changing your mind later is meaningful
2. **Surprising without context** — a future reader will wonder "why did they do it this way?"
3. **The result of a real trade-off** — there were genuine alternatives and you picked one for specific reasons

If any of the three is missing, skip the ADR. Use the format in [ADR-FORMAT.md](./ADR-FORMAT.md).
