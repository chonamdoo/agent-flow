# Semantic Clean Architecture Skill Audit

## Inputs

- `local-skills.zip`: 3 local skills reviewed.
- `skills/`: 29 repo skills reviewed before this change.
- `.agent-flow/skills/`: not present in the isolated worktree; installer output
  is covered by tests.
- Android reference project export: inspected for module families, API/DI,
  repository/usecase/mapper boundaries, and guardrails.

## Consolidation Decisions

- `clean-architecture-core`: new canonical platform-neutral contract.
- `clean-architecture`: compatibility alias only. Existing workflows keep the
  old trigger and markers.
- `android-clean-architecture`, `ios-clean-architecture`,
  `react-clean-architecture`, `react-native-clean-architecture`,
  `python-api-clean-architecture`: new platform adapters depending on the core
  contract.
- Presentation skills stay presentation-focused and no longer need to duplicate
  full semantic Clean Architecture rules.
- Existing app-shell and development-guide skills remain separate because they
  cover error-host, framework, and implementation discipline concerns.

## Duplicate And Overlap Findings

- Local architecture guide overlap:
  - Module boundary rules moved to `clean-architecture-core` and
    `android-clean-architecture`.
  - ViewModel/UiState/route/screen rules remain in Android presentation skill.
  - App-shell error routing remains in app-shell error handling skills.
  - Network/DTO/error rules are represented as core guardrails plus Android data
    boundary details.
- `solid-architecture-review` duplicated Clean Architecture boundary rules and
  exceeded the line limit. It is compressed to SOLID review behavior and points
  to the canonical core skill for architecture boundaries.
- Platform presentation skills had source URLs. They are converted to source
  names only.

## Alias And Deprecation Choices

- Keep `clean-architecture` as alias. Deleting it would break existing workflow
  markers and installer assertions.
- Keep deprecated `clean-architecture/references/platform-standard.md` as a
  short compatibility stub.
- Do not import local zip skill names directly; their project-specific wording is
  not portable.

## Required Guardrails Reflected

- Repository implementation direct API/client injection is a failing review item.
- Remote data source is the canonical production boundary.
- Core data must have matching domain ownership or a documented adapter
  exception.
- Core UI must not own network policy details.
- DTO/entity/domain/UI model separation is explicit.
- Feature API exposes public contracts only.
- DI/composition root is explicit on every platform.
