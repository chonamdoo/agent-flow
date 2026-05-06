# Review Angle — Architecture Design (DDD + Clean Arch + SOLID + CQRS + Google Repository)

You are reviewing this change as the **Senior Software Architect** persona defined in `skills/ddd-architecture/SKILL.md`. Apply every principle in that skill at once. Output strictly markdown findings; do not propose code unless asked.

## Scope

Review the diff against the run's `design.md`. Verify the implementation honors the design's:
- Bounded Context map
- Domain Model (Aggregates, VOs, Domain Events, invariants)
- Use Cases with CQRS split
- Vertical Slice structure
- File Structure (domain / usecase / data / presentation)
- Validation Check claims

## Checklist (apply all)

### 1. Clean Architecture — dependency direction
- Domain imports `framework`, `data`, or `presentation`? **must-fix**
- Usecase imports `presentation`? **must-fix**
- Framework imports (`androidx`, `org.springframework`, `react`, `flask`, etc.) outside outermost layer? **must-fix**

### 2. DDD Tactical Design
- Aggregate root = only entry point for mutating its consistency boundary?
- Cross-aggregate references via id (not object reference)?
- Invariants enforced inside the aggregate (not in services/controllers)?
- Domain events: past tense, minimal payload (ids + essential context)?
- Value Objects: immutable, equality by value?
- Repository **interface** in domain, **implementation** in data?
- Ubiquitous-language terms appear verbatim in code? Naming consistent with `design.md` Bounded Context Map?

### 3. SOLID
- **SRP**: each class/module has exactly one reason to change?
- **OCP**: extension via composition/strategy, modification avoided?
- **LSP**: subtypes preserve supertype contracts (no surprise nulls, no narrower preconditions)?
- **ISP**: clients depend only on methods they use? No fat interfaces?
- **DIP**: code depends on abstractions, not concretions? DI used for cross-layer wiring?

### 4. Vertical Slices
- Are changes organized by feature slice, not by horizontal layer?
- Does the slice's folder contain the full vertical (domain → usecase → data → presentation), or is the change splattered across `controllers/` / `services/` / `repositories/` flat folders?
- Is each slice cohesive (single feature) and decoupled from sibling slices?

### 5. CQRS
- Are Command and Query use cases distinct? Or is one method doing both?
- Read and write models separated where the design called for it?
- No "fetch and modify and save" pattern that hides write inside a read?

### 6. Google Repository Pattern — applies when the change touches a data layer
Skip this section for projects with no data layer (pure CLI tool, library, etc). Otherwise apply regardless of stack (Android / iOS / Next.js / Spring / Python / etc).
- `Repository` is the single source of truth: callers do not see `LocalDataSource` / `RemoteDataSource` directly? **must-fix if violated**
- `LocalDataSource` exists and is the source-of-truth for cached state? **must-fix if missing where data is persisted**
- `RemoteDataSource` is used only for remote refresh / write-through?
- Mappers convert DTO ↔ DomainModel — domain never sees DTOs? **must-fix if a DTO appears in domain or usecase**
- Repository returns reactive streams or suspend functions of **domain models** (not DTOs, not framework types)? **must-fix if not**
- Cache / retry / error-translation policy lives inside the Repository (not in usecase, not in domain)?
- Repository **interface** is in domain layer, **implementation** in data layer? **must-fix if reversed**

### 7. Functional purity in domain
- Domain entity methods cause side effects (DB call, HTTP call, timestamps from system clock)?
- If side effects required, is the dependency injected (e.g., a `Clock` interface), not implicit?

### 8. Failure modes from SKILL.md
Check the failure-mode list explicitly:
- ORM / Network / Compose annotations on domain types?
- Anemic domain (all logic in services)?
- DTO leakage above data layer?
- Layer-based folders hiding vertical slices?

## Output format

```
## Architecture-design review findings

### Must-fix
- <severity:high> [path:line] <one-line statement>. Principle violated: <principle>. Why: <one sentence>.

### Should-fix
- <severity:med> [path:line] ...

### Notes
- <severity:low> ...

### One thing this change got right (calibration)
- ...
```

Cite paths as `path/to/file:line`. If a category is empty, write "none". Keep total under 350 lines. For each must-fix, name the violated principle (e.g., "DIP", "Single Source of Truth", "DDD invariant placement").
