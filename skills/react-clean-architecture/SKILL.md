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
and configured architecture roots before placing code. Map existing locations to
responsibilities; these roles do not require separate folders or modules:

- `app-shell`: startup, dependency composition, root routing, and global UI hosts.
- Feature API: public entry/route/capability contracts without presentation or
  data implementation details.
- `application`: actions and stable ports, including repository, Clock, payment,
  transaction, and platform capabilities where the application needs them.
- `core-domain`: framework-free business language, models, and policy.
- `core-data`: outbound HTTP/storage adapters, persistence, and boundary mapping.
- `inbound-adapter`: HTTP/tool input and response schemas, authenticated request
  adaptation, and application action invocation.
- Feature presentation: client state holders, render contracts, and UI.
- `shared-presentation-contract`: framework-neutral notifier/queue contracts
  consumed by AppShell and feature presentation, not an AppShell implementation
  or a server-side UI queue.

Preserve the repository's names and package shape. Colocation is valid when the
dependency boundary remains clear; equal shapes need no forwarding mapper or
copied model. Connect actual managed source roots to the profile's semantic
roles; an unscanned location or empty lint result is not boundary evidence.
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
- Apply the core's single-context state-holder repository-interface exception
  without extending it to HTTP controller-to-ORM access.

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
