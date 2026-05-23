---
name: solid-architecture-review
description: Legacy detailed SOLID review reference for responsibility separation, interface contracts, substitutability, abstraction quality, and testability across languages and frameworks. Prefer clean-architecture for required Clean Architecture boundary markers; use this only when a deeper SOLID explanation is needed during final-review, multi-review, architecture-review, or code-review.
version: 1
trigger:
  - "SOLID"
  - "solid review"
  - "architecture review"
  - "final-review"
  - "multi-review"
  - "code-review"
  - "Clean Architecture"
  - "dependency direction"
  - "responsibility separation"
  - "interface contract"
  - "testability"
phases_invoked: [final-review, multi-review, architecture-review, code-review]
---

# solid-architecture-review skill

This is a legacy detailed SOLID reference. Required Clean Architecture boundary
markers and must-fix policy live in `skills/clean-architecture/SKILL.md`.
Apply `clean-architecture` first, then use this file only when deeper SOLID
reasoning is needed.

It is stack-agnostic by default. Apply the rules to the project's actual language, framework, and architecture style. When stack-specific skills exist, combine this skill with them instead of duplicating framework details here.

Examples:

- Android/Kotlin/Compose: combine with `android-code-review`, `compose-*`, `kotlin-flow-state-event-modeling`, or `kotlin-coroutines-structured-concurrency`.
- React/TypeScript: combine with `react-development-guide` or `typescript-development-guide`.
- React Native: combine with `react-native-development-guide`.
- Python: combine with `python-development-guide`.

## Role

Use this skill after implementation, during review phases.

This skill does not create a new design document. It checks whether the implemented code honors:

- SOLID principles
- Clean Architecture dependency direction
- stable interface contracts
- appropriate abstraction boundaries
- testability
- maintainability under expected change

Focus on actionable architecture findings. Avoid stylistic opinions, unnecessary abstraction requests, and framework-specific preferences unless they affect correctness, dependency direction, testability, or change safety.

## Review Priority

Classify findings by impact.

### Must Fix

Use for:

- actual bugs or broken requirements
- crashes or data corruption
- dependency direction violations across architectural layers
- domain/core logic depending on infrastructure/framework code
- implementation leaking transport/persistence models into higher layers
- interface contract violations that make substitution unsafe
- core behavior that cannot be tested without real infrastructure

### Should Fix

Use for:

- excessive responsibility in a class/module/function
- direct dependency on concrete implementations where a stable boundary is expected
- use cases/services that are missing, misplaced, or meaningless
- repository/service interfaces that are too large or too coupled
- duplicated policy logic across layers
- abstractions that hide side effects or make behavior unclear

### Note

Use for:

- acceptable tradeoffs for the current scope
- minor design debt
- future extension concerns
- places where the current structure is fine but should be watched if the feature grows

## SOLID Review Rules

### SRP: Single Responsibility Principle

Check whether each unit has one reason to change.

Review questions:

- Does this class/module/function mix UI, business policy, persistence, networking, formatting, and orchestration?
- Is there a clear owner for domain policy?
- Is mapping isolated from transport/persistence details?
- Are lifecycle/infrastructure concerns separated from business decisions?
- Would a small requirement change force unrelated code to change?

Common findings:

- UI/controller component performs business rules.
- Service/use case also parses transport models or writes storage.
- Repository also owns UI formatting.
- One module handles fetching, caching, mapping, validation, and rendering.

### OCP: Open/Closed Principle

Check whether expected extensions can be added without modifying unrelated stable code.

Review questions:

- If a new source/type/provider is added, how many existing files must change?
- Are conditionals centralized at a reasonable boundary?
- Are extension points explicit where variation is expected?
- Is the code over-abstracted for variation that is not expected?

Common findings:

- Type branching is duplicated across layers.
- Adding a new implementation requires changing consumers.
- A factory/strategy/interface would reduce repeated modification.
- Abstraction was introduced without real variation.

Guidance:

- Do not force abstraction for hypothetical changes.
- A small, localized branch can be acceptable.
- Repeated branching across layers is usually a risk.

### LSP: Liskov Substitution Principle

Check whether implementations can replace their abstractions without changing caller behavior.

Review questions:

- Do all implementations honor the same preconditions, postconditions, errors, nullability, ordering, paging, and side-effect contract?
- Do fake/test implementations behave like production implementations?
- Does a subtype narrow behavior promised by its parent?
- Does an implementation throw unexpected errors or return special values outside the contract?

Common findings:

- Fake repository returns impossible states.
- One implementation returns null where the contract implies empty collection.
- One implementation sorts data differently without documenting it.
- Subclass overrides behavior in a way that breaks caller expectations.

Guidance:

- Prefer explicit contracts over implicit behavior.
- If implementations cannot share the same contract, split the abstraction.

### ISP: Interface Segregation Principle

Check whether consumers depend only on capabilities they actually need.

Review questions:

- Is an interface too large for its consumers?
- Are read/write/admin/lifecycle operations forced into one abstraction?
- Are consumers depending on methods they never call?
- Would separate command/query interfaces make dependencies clearer?

Common findings:

- A broad repository/service interface used by many unrelated consumers.
- Test doubles must implement irrelevant methods.
- Interface shape mirrors an implementation instead of consumer needs.

Guidance:

- Split interfaces when consumers have meaningfully different needs.
- Do not split tiny cohesive interfaces just to satisfy the principle mechanically.

### DIP: Dependency Inversion Principle

Check whether high-level policy depends on stable abstractions rather than low-level details.

Review questions:

- Does domain/core policy import framework, database, HTTP, filesystem, or UI code?
- Do high-level modules instantiate low-level implementations directly?
- Are concrete implementations injected from composition root / DI boundary?
- Are transport/persistence DTOs leaking into domain or presentation where domain models are expected?
- Can core behavior be tested with in-memory/fake implementations?

Common findings:

- Use case depends directly on HTTP client/database/client SDK.
- UI/controller depends directly on repository implementation instead of boundary.
- Domain model imports framework annotations/types.
- Infrastructure error types leak into domain contract.

Guidance:

- Repository/service interfaces should live near the policy that owns the contract.
- Implementations should live in infrastructure/data layers.
- Manual DI/composition root is acceptable. A DI framework is not required.

## Clean Architecture Boundary Rules

Apply these rules when the project uses layers or ports/adapters.

Dependency direction:

- Outer layers may depend inward.
- Inner layers must not depend outward.
- Domain/core must be framework-agnostic unless the project intentionally has no domain layer.
- Infrastructure implements interfaces defined by core/application layers.
- UI/API handlers should coordinate, not own domain policy.

Typical boundaries:

- Presentation/controller/view -> application/use case
- Application/use case -> domain model + repository/service interface
- Domain/core -> no framework/infrastructure dependency
- Infrastructure/data -> implements interfaces, handles external systems
- Mapper/adapter -> converts external models to internal models

Check for:

- DTO/entity leakage across boundaries
- business rules in controllers/views
- persistence/network concerns in domain
- use cases that are bypassed by presentation
- repositories/services returning infrastructure-specific types
- unclear composition root

## Stack-Specific Integration

This skill should not duplicate stack-specific rules. When reviewing a concrete project, combine it with relevant skills.

Use examples:

- Android: Check ViewModel, UseCase, Repository, DataSource, Mapper, coroutine/flow boundaries using Android/Kotlin-specific skills.
- React: Check component responsibility, hooks, state ownership, server/client boundaries, and TypeScript contracts using React/TypeScript skills.
- React Native: Check native bridge boundaries, UI state, async side effects, and platform separation using React Native skills.
- Python: Check module boundaries, service/repository shape, dependency injection, and testability using Python-specific skills.

If no stack-specific skill exists, still apply the SOLID and Clean Architecture rules directly.

## Review Output Format

Always use this format.

```markdown
## SOLID Architecture Review

### Overall
verdict: approve | request-changes

### Findings

#### Must Fix
- [Principle] Finding title
  - File: `path:line`
  - Problem:
  - Impact:
  - Suggested fix:

#### Should Fix
- [Principle] Finding title
  - File: `path:line`
  - Problem:
  - Impact:
  - Suggested fix:

#### Notes
- [Principle] Note title
  - Context:
  - Recommendation:

### Principle Summary
- SRP: pass | risk | fail — reason
- OCP: pass | risk | fail — reason
- LSP: pass | risk | fail — reason
- ISP: pass | risk | fail — reason
- DIP: pass | risk | fail — reason

### Boundary Summary
- Dependency direction: pass | risk | fail — reason
- Interface contracts: pass | risk | fail — reason
- Framework isolation: pass | risk | fail — reason
- Model mapping: pass | risk | fail — reason
- Testability: pass | risk | fail — reason
```

## Review Policy

- Findings must be grounded in actual code, diff, or tests.
- Must Fix and Should Fix should include file and line references whenever possible.
- If evidence is incomplete, use Notes or Open Questions instead of overstating.
- Do not require Clean Architecture ceremony for trivial scripts, one-off tools, or small isolated changes.
- Do not require a DI framework. Manual dependency wiring is acceptable.
- Do not require repositories/use cases where they add no practical value.
- Do not introduce framework-specific requirements unless a stack-specific skill or project convention requires them.
- Prefer the existing architecture style of the repository when it is coherent.
- Challenge architecture only when it affects correctness, substitution safety, maintainability, or testability.

## Final Review Behavior

During final-review or architecture-review:

1. Inspect the implemented diff and relevant surrounding code.
2. Identify the intended architectural boundaries from the project structure.
3. Apply SOLID principles against those boundaries.
4. Check whether tests cover core policy and boundary contracts.
5. Produce the review output in the required format.
6. Use `verdict: request-changes` only when Must Fix findings exist.
7. Use `verdict: approve` when there are no Must Fix findings, even if Should Fix or Notes remain.

## Relationship With ddd-architecture

`ddd-architecture` is for design-phase modeling and architecture planning.
`solid-architecture-review` is for post-implementation review.
Do not duplicate long SOLID rules inside `ddd-architecture`.
`ddd-architecture` may reference this skill for final-review validation.

## Skills Checked

- `code-generation-discipline`
- Language/framework-specific skills: none. This artifact changes Markdown skill documentation only.

## Completion Gate

skills_checked: true
