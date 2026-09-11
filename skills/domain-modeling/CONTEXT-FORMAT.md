# CONTEXT.md Format

## Structure

```md
# {Context Name}

{One or two sentence description of what this context is and why it exists.}

## Language

**{Agreed canonical term}**:
{One or two sentences defining its agreed meaning in this context.}
_Avoid_: {Confirmed ambiguous or competing terms for the same concept}
```

## Rules

- **Be opinionated.** When multiple words exist for the same concept, pick the best one and list the others under `_Avoid_`.
- **Keep definitions tight.** One or two sentences max. Define what it IS, not what it does.
- **Only include terms specific to this project's context.** General programming concepts (timeouts, error types, utility patterns) don't belong even if the project uses them extensively. Before adding a term, ask: is this a concept unique to this context, or a general programming concept? Only the former belongs.
- **Group terms under subheadings** when natural clusters emerge. If all terms belong to a single cohesive area, a flat list is fine.

## Single vs multi-context repos

**Single context (most repos):** One `CONTEXT.md` at the repo root.

**Multiple contexts:** A `CONTEXT-MAP.md` at the repo root lists the contexts, where they live, and how they relate to each other:

Use `# Context Map`, `## Contexts`, and `## Relationships` to organize the map:

- Under **Contexts**, link each confirmed context to its actual `CONTEXT.md`
  location and describe its responsibility.
- Under **Relationships**, record verified direction, ownership, and translation
  between contexts. Record published or consumed events and shared contracts only
  when supported by the agreed model or observed integration.
- Separate observed integration from intended relationships; leave unresolved
  policy explicit rather than inventing event payloads, shared types, or ownership
  rules to complete the map.

The skill infers which structure applies:

- If `CONTEXT-MAP.md` exists, read it to find contexts
- If only a root `CONTEXT.md` exists, single context
- If neither exists, create a root `CONTEXT.md` lazily when the first term is resolved

When multiple contexts exist, infer which one the current topic relates to. If unclear, ask.
