# Review Angle - Clean Architecture

You are reviewing this change through `.agent-flow/skills/clean-architecture-core/SKILL.md`.
Output strictly markdown findings. Do not propose code unless asked.

## What to verify

1. Dependency rule
   - Domain/Application imports no UI, DB, HTTP, SDK, or framework implementation.
   - Presentation/UI/Controller/ViewModel/Handler depends on UseCase ports, not
     Repository Impl, DataSource, or Cache implementation.
   - UseCase Impl depends on Repository Interface, not Repository Impl.

2. UseCase boundary
   - One UseCase represents one user intent or application action.
   - Interface + Impl exists when module size, public contract, feature calls, or
     DI binding requires it.
   - UseCase does not inject or call another UseCase directly.
   - Shared flow logic is a Domain Service, Policy, pure function, or explicitly
     named Application Workflow/Orchestrator.
   - UseCase does not handle DTO, DB Entity, or UI Model.

3. Repository boundary
   - Repository Interface is a domain/application port.
   - Repository Impl is a data/infrastructure adapter.
   - Repository is the single source of truth, not just an API wrapper.
   - Repository Interface returns domain models only.

4. Cache boundary
   - Cache is a data-layer detail behind an interface.
   - MemoryCache and DiskCache are split when lifetime or change reason differs.
   - Cache never exposes internal mutable storage.
   - Restart-required data is not stored only in MemoryCache.
   - Temporary data is not persisted to DiskCache without need.

5. Mapping boundary
   - Remote DTO, DB Entity, Domain Model, and UI Model are separate.
   - Mapper sits at the boundary it crosses and only converts.
   - No DTO/DB Entity is exposed as domain/application/presentation state.
   - Domain Model has no ORM, serialization, or framework annotations.
   - No giant mapper handles Remote DTO, DB Entity, and UI Model conversions together.

6. Error boundary
   - Raw transport/storage/native failures stay in data/infrastructure adapters.
   - Repository Impl or data mapper translates raw failures to domain app errors.
   - UseCase returns domain result/error types and adds only business-rule errors.
   - Presentation maps domain errors to UI error models before UI rendering.
   - UI components/views/screens never receive DTOs, HTTP `Response`, URLSession
     responses, native exception strings, or storage failure types.

7. SOLID architecture validation
   - SRP: separated by reason to change.
   - OCP: extension points exist only at real variation points.
   - LSP: implementations and fakes preserve interface contracts.
   - ISP: consumers do not depend on unused methods.
   - DIP: high-level policy depends on abstractions.

8. Full must-avoid sweep
   - Check every rule in the core skill's current `Must Avoid` section against
     the changed code; do not infer coverage from the seven categories above.
   - Record `must-avoid-check: pass|fail`.

## Must-fix policy

Any violation listed in `.agent-flow/skills/clean-architecture-core/SKILL.md` as a
must-avoid rule, or any failing required checklist item from that core skill,
must produce `verdict: request-changes`. Use
`.agent-flow/skills/clean-architecture/SKILL.md` only for compatibility markers and skill
loading order.

Record every applicable marker from the core skill's current `Review Checklist`;
the block below is only the workflow compatibility subset.

## Required completion gate

The review artifact must include:

```text
## Completion Gate
clean-architecture: applied
must-avoid-check: pass|fail
dependency-rule: pass|fail
usecase-boundary: pass|fail|n/a
usecase-calls-usecase: pass|fail
repository-boundary: pass|fail
cache-boundary: pass|fail|n/a
memory-disk-cache-separated: pass|fail|n/a
mapping-boundary: pass|fail|n/a
dto-entity-domain-ui-separated: pass|fail
solid-boundary-check: pass|fail
```

For code-review or multi-review artifacts, include:

```text
## Completion Gate
clean-architecture-review: applied
usecase-interface-check: applied
usecase-composition-check: applied
cache-boundary-check: applied
mapping-boundary-check: applied
solid-clean-architecture-check: applied
```

## Output format

```markdown
## Clean Architecture review findings

### Must-fix
- <severity:high> [path:line] <boundary violation>. Why: <one sentence>.

### Should-fix
- <severity:med> ...

### Notes
- <severity:low> ...

### Overall
verdict: approve | request-changes
```

Cite paths as `path/to/file:line`. If a category is empty, write `none`.
