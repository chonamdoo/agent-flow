# ADR Format

ADRs live in `docs/adr/` and use sequential numbering: `0001-slug.md`, `0002-slug.md`, etc.

Create the `docs/adr/` directory lazily — only when the first ADR is needed.

## Template

```md
# {Short title of the decision}

{1-3 sentences: what's the context, what did we decide, and why.}
```

That's it. An ADR can be a single paragraph. The value is in recording *that* a decision was made and *why* — not in filling out sections.

## Optional sections

Only include these when they add genuine value. Most ADRs won't need them.

- **Status** frontmatter (`proposed | accepted | deprecated | superseded by ADR-NNNN`) — useful when decisions are revisited
- **Considered Options** — only when the rejected alternatives are worth remembering
- **Consequences** — only when non-obvious downstream effects need to be called out

## Numbering

Scan `docs/adr/` for the highest existing number and increment by one.

## When to offer an ADR

Apply the three conditions in [SKILL.md](./SKILL.md#offer-adrs-sparingly):
hard to reverse, surprising without context, and the result of a real trade-off.
The categories below do not qualify automatically; record only supported
decisions and reasons, without inventing policy or constraints.

### What qualifies

- **Architectural shape.** Record the chosen organization of the system and why
  its alternatives were rejected, when changing that shape has a meaningful cost.
- **Integration patterns between contexts.** Record the agreed interaction
  mechanism, responsibility boundaries, and reasons for choosing it.
- **Technology choices that carry lock-in.** Assess the actual replacement cost
  and constraints rather than assuming that every library choice merits an ADR.
- **Boundary and scope decisions.** Record confirmed ownership and intentional
  exclusions, including how other contexts may access or refer to owned data.
- **Deliberate deviations from the obvious path.** Preserve the reason an
  otherwise reasonable alternative was not chosen so future changes retain the
  underlying constraint.
- **Constraints not visible in the code.** Cite the actual regulatory, operational,
  or external contract behind the decision. Include quantitative limits only
  when supplied by that evidence.
- **Rejected alternatives when the rejection is non-obvious.** Record the real
  options considered and the reasons for rejection rather than inventing a
  comparison or a date for reconsideration.
