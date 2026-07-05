---
name: solid-architecture-review
description: Reviews code against SOLID principles and reports architecture findings with severity. Use when code review, final review, or architecture review needs SRP/OCP/LSP/ISP/DIP checks; pair with clean-architecture-core when dependency direction, use case/repository boundaries, or platform architecture are also in scope.
---

# SOLID Architecture Review

This is a standalone SOLID review checklist when the question is class/module
responsibility, substitutability, interface size, or dependency inversion. Pair
it with `clean-architecture-core` when layer boundaries, repository, source,
cache, mapper, or DI-direction rules are in scope.

## Quick start

1. Review the changed modules for one concrete reason to change, explicit
   extension seams, substitutable implementations, narrow interfaces, and
   inward-facing dependencies.
2. Escalate from SOLID-only to `clean-architecture-core` if findings depend on
   use case, repository, mapper, cache, or platform-layer rules.
3. Lead with findings and then include the principle summary and completion
   markers.

## Review Priority

- `must-fix`: bug, runtime risk, broken contract, high-risk maintainability
  issue, or architecture rule violation.
- `should-fix`: meaningful maintainability risk with a clear change.
- `note`: observation or optional improvement.

## SOLID Checks

### SRP

- Identify the concrete reason each class/module changes.
- Flag classes that mix UI rendering, state orchestration, data access, mapping,
  policy, and transport concerns.
- Do not flag a class merely because it has multiple methods.

### OCP

- Prefer extension points only where variation already exists or is likely.
- Avoid premature abstractions for one implementation.
- Flag switch/if chains that require editing stable policy for every new
  variant.

### LSP

- Every implementation, fake, and test double must preserve the interface
  contract.
- Flag implementations that narrow accepted inputs, broaden thrown failures, or
  return incompatible model types.

### ISP

- Consumers should depend only on methods they use.
- Split broad interfaces when unrelated consumers are forced to implement or mock
  irrelevant methods.

### DIP

- High-level policy depends on abstractions.
- Concrete frameworks, transport clients, databases, SDKs, and UI runtimes stay
  behind adapter interfaces or composition roots.
- Pair this check with `clean-architecture-core` for repository/usecase/mapper
  boundary details.

## Review Output

Lead with findings. Use tight file/line references when reviewing code.

```text
# SOLID Architecture Review

verdict: approve|request-changes

## Findings
- path/to/File.kt:L42: must-fix: issue. change.

## Principle Summary
srp: pass|fail
ocp: pass|fail
lsp: pass|fail
isp: pass|fail
dip: pass|fail

## Boundary Summary
clean-architecture-core: applied|n/a
solid-boundary-check: pass|fail
```

## Completion Gate

```text
solid-architecture-review: applied
srp-check: pass|fail
ocp-check: pass|fail
lsp-check: pass|fail
isp-check: pass|fail
dip-check: pass|fail
clean-architecture-core: applied|n/a
solid-boundary-check: pass|fail
```
