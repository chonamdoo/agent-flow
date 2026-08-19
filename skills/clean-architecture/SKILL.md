---
name: clean-architecture
description: Compatibility alias for Clean Architecture review and design. Use clean-architecture-core as the canonical source, then load the matching platform clean architecture skill for Android, iOS, Flutter, React, React Native, or Python API details.
requires:
  - clean-architecture-core
---

# Clean Architecture Compatibility Alias

This skill is kept for existing workflows and completion markers. Treat
`clean-architecture-core` as canonical.

## Use Order

1. Load `clean-architecture-core`.
2. Load exactly the platform skill needed by the current code:
   - `android-clean-architecture`
   - `ios-clean-architecture`
   - `flutter-clean-architecture`
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
