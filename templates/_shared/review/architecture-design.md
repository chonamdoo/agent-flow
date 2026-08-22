# Review Angle - Architecture Design

You are reviewing whether implementation matches the design artifact.
Apply `.agent-flow/skills/ddd-architecture/SKILL.md` first for domain model fit, then apply
`.agent-flow/skills/clean-architecture-core/SKILL.md` for layer boundaries, dependency rules,
and post-implementation SOLID validation. Use platform adapters only for
framework-specific evidence.

## Scope

- Compare the implementation diff against `design.md` or `ddd-design.md`.
- Keep DDD findings focused on domain terms, contexts, aggregates, events, and
  invariants.
- Keep Clean Architecture findings focused on layers, ports, adapters, caches,
  mappers, composition root, testability, and SOLID boundary validation.

## Checks

1. DDD alignment
   - Bounded contexts and ubiquitous language match the design.
   - Entities, Value Objects, Aggregates, Domain Events, and invariants are placed
     where the design says they belong.
   - Domain flow is still expressed in domain terms.

2. Clean Architecture alignment
   - Dependency direction points inward.
   - UseCase, Repository, Cache, and Mapper boundaries match the design.
   - Domain/Application imports no UI, DB, HTTP, SDK, or framework implementation.
   - DTO, DB Entity, Domain Model, and UI Model remain separate.
   - Composition root owns concrete wiring.

3. SOLID boundary validation
   - SRP change reasons are not mixed across layers.
   - OCP extension points exist only where the design identified variation.
   - LSP contracts hold for interfaces, fakes, and production implementations.
   - ISP ports are consumer-focused.
   - DIP direction is inward.

## Output format

```markdown
## Architecture design review findings

### Must-fix
- <severity:high> [path:line] <design or boundary violation>. Why: <one sentence>.

### Should-fix
- <severity:med> ...

### Notes
- <severity:low> ...

### Overall
verdict: approve | request-changes
```

Cite paths as `path/to/file:line`. If a category is empty, write `none`.

Emit exactly one unfenced final verdict line: `verdict: approve` or `verdict: request-changes`.
