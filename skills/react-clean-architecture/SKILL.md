---
name: react-clean-architecture
description: React Web and Next.js Clean Architecture adapter for the platform-neutral clean-architecture-core contract. Use for monorepo package layout, Context Provider composition root, optional TSyringe, state holder/component boundaries, repository/source/mapper boundaries, and React architecture review.
requires:
  - clean-architecture-core
---

# React Clean Architecture

Load `clean-architecture-core` first. This skill adds React/Next.js package and
composition-root details only.

## Semantic Roles and Discovery

Discover actual package exports, imports, route boundaries, dependency providers,
and configured architecture roots before placing code. Apply the required core's
**Semantic Layers**, **Dependency Rule**, and **Mapping Boundary** to those
locations, preserving the repository's names and package shape.

Framework-neutral shared contracts must not import React runtime; explicitly
React-specific UI/adapter packages may do so.

## DI Shape

- Default to explicit factory functions plus React Context/Provider at the app
  shell.
- Existing factories construct implementations at the composition root and
  expose typed application/domain ports to feature dependency hooks, not raw
  clients merely wrapped in Context.
- Optional TSyringe usage stays at app shell or adapter edge.
- If TSyringe is used, configure decorator metadata and import
  `reflect-metadata` once before DI use.
- Domain/usecase/data contracts must not import React, Next.js, TSyringe, fetch
  implementation, or app router details.
- Next Server pages retain server data fetching, metadata, and composition.
  Interactive Client wrappers own hooks and browser effects. Respect RSC
  serializability rather than passing server clients or repository instances to
  a Client provider; a server-only page needs no empty hook or client conversion.

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
