---
name: mixed-architecture
description: Apply the synthetic project's Main FSD, BO route, and BO form contracts to their changed paths; shared packages receive common rules only.
requires_docs:
  - references/common.md
  - path: references/main-fsd.md
    pathGlobs: ["apps/main/**"]
  - path: references/bo-routes.md
    pathGlobs: ["apps/bo/**"]
  - path: references/bo-forms.md
    pathGlobs: ["apps/bo/src/app/demo/orders/new/**"]
---

# Mixed local architecture

Use this root and common rules for every change. Add Main rules for Main paths, BO route rules for BO paths, and form rules only for the declared orders/new subtree. Mixed changes require the union, with judgments kept file-scoped. A packages-only change receives no app-specific layering obligations.

This is the selected local contract even when Clean Architecture skills are installed. Main uses FSD; BO uses route ownership. Do not introduce Clean ports, repositories, usecases, or domain/data/presentation folders to satisfy an unselected architecture.

## Evidence and fixture choices

The normative basis is the verified, individually re-read photo-contract confirmation, not earlier parallel-image attribution. It establishes written rules, not proof that the original lint configuration worked. The BO form/Zod fragment and the separate clipped common fragment were previously misattributed; the corrected confirmation is authoritative.

The fixture chooses neutral `app/demo` routes, synthetic orders/accounts/products, a shared parsing capability, scoped reference metadata, one ESLint configuration, and app-local `@/` aliases. These are executable modeling choices, not recovered original files or product policies. The original `mbo` description and `nbo` code/URL text do not establish an alias relationship.

The original form frontmatter and its first 26 lines, complete lint zones, Turbo configuration, and debt allowlist were not observed. Do not claim to restore them. This fixture has no synthetic legacy-debt allowlist. Its new edges must satisfy the current rules.

## Completion evidence

Use the literal `agent-flow architecture-lint` command for architecture-role checks; do not append `--complete`. This does not substitute for the actual `pnpm run lint` import-boundary gate. Follow common.md for lint evidence and app references for semantic review. Installed document delivery proves delivery, not model compliance or independent review approval.
