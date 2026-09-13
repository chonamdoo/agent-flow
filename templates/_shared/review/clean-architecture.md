# Review Angle - Architecture Contract

Use the selected architecture contract supplied in this reviewer's own prompt.
Output strictly markdown findings. Do not propose code unless asked.

## Local mode

Read the complete normative root and every `requires_docs` document included in
the prompt. Assess every applicable requirement, permitted alternative, and
exception against actual changed code and the approved design. Cite both the
normative rule and implementation evidence for a violation; missing evidence is
not a pass.

Check the contract's dependency and ownership rules, action composition,
persistence and cache policy, representation and error boundaries, dependency
wiring, and testability wherever they apply. A feature-colocated or
framework-aware design is valid when the selected contract permits it. Preserve
shared security, data-integrity, cancellation, and interface-contract checks;
architecture selection is not an exemption from them.

An applicable required-rule violation produces `verdict: request-changes`.
Record `must-avoid-check: pass|fail` for the selected contract's prohibitions.
Retain the phase's completion markers and review-category coverage, explaining
the selected-contract evidence or a permitted `n/a` for each category. Legacy
Clean-named review categories record this assessment, not adoption of Clean
layers or permission to skip the review. A fixed legacy
`clean-architecture: applied` marker certifies selected-contract assessment
where the workflow requires it; document the actual mode and contract rather
than claiming an unselected Clean skill was read.

The sections from **What to verify** through **Required completion gate** below
are the Clean-mode rubric only. Local mode uses the full selected contract above
and the phase's completion gate instead. **Output format** applies to both modes.

## Clean mode

Apply the `clean-architecture-core` skill resolved by this reviewer prompt.

## What to verify

1. Dependency rule
   - Pure domain policy imports no UI, DB, HTTP, provider SDK, or framework.
     Application orchestration follows the adopted pure/adapter or explicitly
     framework-aware boundary; accepted application wiring metadata is not a
     blanket domain exception.
   - Presentation consumes use-case or domain ports, never repository
     implementations, data sources, caches, or raw clients. A UI state holder may
     use one context's repository interface without extra orchestration, as the
     core contract permits.
   - HTTP/tool handlers call application actions by default. Do not silently
     extend the UI exception to handler access to Spring Data or ORM details.
   - Use cases depend on stable domain/application ports and pure policies;
     Clock, payment, transaction, and platform ports need no Repository disguise.

2. UseCase boundary
   - One UseCase represents one user intent or application action.
   - Interface + Impl exists when module size, public contract, feature calls, or
     DI binding requires it.
   - UseCase does not inject or call another UseCase directly.
   - Shared flow logic is a Domain Service, Policy, pure function, or explicitly
     named Application Workflow/Orchestrator.
   - UseCase handles application/domain values, not inbound/outbound DTOs,
     persistence entities, raw transport failures, or UI models.

3. Repository boundary
   - Repository Interface is a domain/application port.
   - Repository Impl is a data/infrastructure adapter.
   - Repository owns the data policy its consumer needs. A DB-only or recorded
     simple adapter needs no invented remote/local/cache collaborators.
   - Repository returns its contract's domain/application values, not outbound
     DTOs, ORM entities, raw responses, or UI models.

4. Cache boundary
   - Cache policy stays behind a consumer contract; a distinct cache interface is
     needed only when substitution, lifetime, or ownership warrants it.
   - MemoryCache and DiskCache are split when lifetime or change reason differs.
   - Cache never exposes internal mutable storage.
   - Restart-required data is not stored only in MemoryCache.
   - Temporary data is not persisted to DiskCache without need.

5. Mapping boundary
   - Inbound HTTP/tool schemas belong to the driving adapter; outbound provider
     DTOs and DB entities belong to driven adapters; commands/results belong inside.
   - Mapping preserves meaning at each crossed boundary. Separate models when
     semantics or dependencies differ, not merely to change a suffix or file.
   - A mapper only converts; it does not perform I/O or business decisions.
   - Pure domain policy has no ORM, serialization, or framework annotations.
   - No giant mapper combines unrelated remote, persistence, and UI boundaries.

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
   - Shared presentation notifier/queue contracts import no AppShell or feature
     implementation, UI framework, raw transport, or persistence type. AppShell
     wires their implementation; UI-free servers do not need those contracts.

8. Full must-avoid sweep
   - Check every rule in the core skill's current `Must Avoid` section against
     the changed code; do not infer coverage from the seven categories above.
   - Record `must-avoid-check: pass|fail`.

## Must-fix policy

An applicable must-avoid violation or failed required criterion in
`clean-architecture-core` must produce `verdict: request-changes`. Apply its
recorded exceptions and adopted project conventions before judging a failure.
Naming, file counts, missing unnecessary collaborators, and an inactive/unmapped
lint boundary alone are not proof of a semantic violation or a successful check.
Use `clean-architecture` only for compatibility markers and skill loading order.

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
usecase-calls-usecase: pass|fail|n/a
repository-boundary: pass|fail
cache-boundary: pass|fail|n/a
memory-disk-cache-separated: pass|fail|n/a
mapping-boundary: pass|fail|n/a
dto-entity-domain-ui-separated: pass|fail
solid-boundary-check: pass|fail
```

For `usecase-boundary` and `usecase-calls-usecase`, use `n/a` only when no use-case
implementation/composition is in scope. An applicable path needs evidence for
`pass` or `fail`. Do not relax other marker enums or conditional required guards.

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

Emit exactly one unfenced final verdict line: `verdict: approve` or `verdict: request-changes`.
