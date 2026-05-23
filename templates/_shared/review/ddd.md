# Review Angle — Domain-Driven Design

You are reviewing this change through a **DDD lens**. The code under review is provided as the diff for the run's slices. Output strictly markdown findings; do not propose code unless asked.

## What to verify

1. **Ubiquitous language**
   - Do new types, methods, and events use domain terms used by stakeholders, not technical/UI vocabulary?
   - Are persistence/transport names (DTO, Entity, Row) leaking into domain naming?

2. **Aggregates and invariants**
   - Is each aggregate root the only entry point for mutating its consistency boundary?
   - Are invariants enforced inside the aggregate, not in services or controllers?
   - Are aggregates kept small? Cross-aggregate references via id, not object reference?

3. **Bounded context boundaries**
   - Does the change cross a bounded context? If yes, is the crossing explicit (translation layer / context map)?
   - Are concepts duplicated across contexts intentionally (different meaning) rather than shared accidentally?

4. **Domain events**
   - Are state transitions that other subdomains care about emitted as events?
   - Do events name a fact in past tense ("OrderPlaced", not "PlaceOrder")?
   - Are event payloads minimal (ids + essential context, not full aggregate dumps)?

5. **Anemic vs rich models**
   - Is behavior on the entity/aggregate, or scattered into utility services?
   - Are domain rules expressed as methods, not branches in callers?

6. **Clean Architecture handoff**
   - If layer boundaries, repository ports/adapters, use cases, caches, or mappers
     are involved, did the artifact apply `clean-architecture` after this DDD pass?
   - Do not duplicate Clean Architecture findings here; record only the domain
     model issue and let the Clean Architecture angle judge dependency boundaries.

## Output format

```
## DDD review findings

### Must-fix
- <severity:high> [path:line] <one-line statement>. Why: <one sentence>.

### Should-fix
- <severity:med> ...

### Notes
- <severity:low> ...

### One thing this change got right (calibration)
- ...
```

Cite file paths as `path/to/file:line`. If no findings in a category, write "none". Keep total under 250 lines.
