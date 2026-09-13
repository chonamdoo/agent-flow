---
name: solid-architecture-review
description: SOLID architecture review checklist for code review, final review, and architecture review. Use with the selected architecture contract when dependency direction, use case/repository boundaries, or platform architecture are also in scope.
---

# SOLID Architecture Review

Use this skill for SOLID-specific review. Apply the selected architecture
contract for structural boundaries and dependency direction. In Clean mode,
load `clean-architecture-core`; local mode uses its full root and required references.

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
- In Clean mode, concrete frameworks, transport clients, databases, SDKs, and UI
  runtimes stay behind adapter interfaces or composition roots. Local mode
  follows its selected contract's framework and dependency boundaries.
- Pair this check with the selected architecture contract for structural
  boundary details (`clean-architecture-core` in Clean mode).

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
