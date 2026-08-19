---
name: react-clean-architecture
description: React Web and Next.js Clean Architecture adapter for the platform-neutral clean-architecture-core contract. Use for monorepo package layout, Context Provider composition root, optional TSyringe, state holder/component boundaries, repository/source/mapper boundaries, and React architecture review.
requires:
  - clean-architecture-core
---

# React Clean Architecture

Load `clean-architecture-core` first. This skill adds React/Next.js package and
composition-root details only.

## Package Shape

```text
apps/web/
packages/core-ui-web/
packages/core-design-system-web/
packages/core-resources/
packages/core-platform-web/
packages/core-network/
packages/core-navigation-api/
packages/core-navigation-react/
packages/core-domain-home/
packages/core-data-home/
packages/feature-home-api/
packages/feature-home-presentation-web/
```

Shared packages must not depend on React runtime unless their boundary is
explicitly React-specific.

## DI Shape

- Default to explicit factory functions plus React Context/Provider at the app
  shell.
- `createDependencies()` or `createContainer()` creates the same semantic shape
  used by other platforms.
- Optional TSyringe usage stays at app shell or adapter edge.
- If TSyringe is used, configure decorator metadata and import
  `reflect-metadata` once before DI use.
- Domain/usecase/data contracts must not import React, Next.js, TSyringe, fetch
  implementation, or app router details.

## Review Additions

```text
react-clean-architecture: applied
package-boundary: pass|fail
context-provider-composition-root: pass|fail
tsyringe-optional-edge-only: pass|fail|n/a
repository-impl-direct-api-client: pass|fail
remote-data-source-boundary: pass|fail
feature-api-public-contract-only: pass|fail
component-state-holder-split: pass|fail|n/a
```

## Evidence Basis

React createContext/useContext docs, React state structure docs, React reducer
docs, React effects docs, TSyringe README.
