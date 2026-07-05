---
name: clean-architecture
description: Compatibility alias that routes Clean Architecture work to the canonical platform-neutral and platform-specific skills. Use when an existing workflow asks for clean-architecture; otherwise load clean-architecture-core directly and pair it with the matching platform clean architecture skill when framework details matter.
---

# Clean Architecture Compatibility Alias

This is a compatibility alias, not a standalone architecture source. It exists
for existing workflows and completion markers; route substantive Clean
Architecture decisions through `clean-architecture-core`.

## Quick start

1. If the user explicitly requested `clean-architecture`, load this alias and
   immediately continue with `clean-architecture-core`.
2. For implementation or review in a concrete stack, also load exactly one
   platform clean architecture skill for framework, DI, folder, and sample-code
   details.
3. Use `ddd-architecture` first when domain language, bounded contexts, or
   aggregates are still being modeled; use `solid-architecture-review` alongside
   reviews that need SOLID findings.

## Use Order

1. Load `clean-architecture-core`.
2. Load exactly the platform skill needed by the current code:
   - `android-clean-architecture`
   - `ios-clean-architecture`
   - `react-clean-architecture`
   - `react-native-clean-architecture`
   - `python-api-clean-architecture`
3. Load presentation-specific skills only when changing or reviewing UI state,
   ViewModels/controllers, screens/components, or route wiring.

## Compatibility Markers

Existing workflows may still require these markers:

```text
clean-architecture: applied
clean-architecture-review: applied
dependency-rule: pass|fail
usecase-boundary: pass|fail|n/a
repository-boundary: pass|fail
mapping-boundary: pass|fail|n/a
solid-boundary-check: pass|fail
```

When both old and new markers are required, include the old marker plus the
`clean-architecture-core` checklist from the canonical skill.

## Maintenance Rule

Do not add new architecture rules here. Add shared semantic rules to
`clean-architecture-core` and platform-specific rules to the matching platform
skill.
