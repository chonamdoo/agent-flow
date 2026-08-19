---
name: ios-clean-architecture
description: iOS Clean Architecture adapter for the platform-neutral clean-architecture-core contract. Use for Swift source/package layout, SwiftUI/UIKit presentation, Swinject or local factory composition root, repository/source/mapper boundaries, and iOS architecture review.
requires:
  - clean-architecture-core
---

# iOS Clean Architecture

Load `clean-architecture-core` first. This skill adds iOS-specific layout and DI
details only.

## Source Shape

```text
App/
Sources/CoreUI/
Sources/CoreDesignSystem/
Sources/CoreResources/
Sources/CorePlatform/
Sources/CoreNetwork/
Sources/CoreNavigationAPI/
Sources/CoreNavigationImpl/
Sources/CoreDomain<Home>/
Sources/CoreData<Home>/
Sources/Feature<Home>API/
Sources/Feature<Home>Presentation/
```

Package names express the semantic boundary. Exact folder strings do not need to
match Android paths; roles and dependency direction must match.

## DI Shape

- App shell owns the composition root.
- Prefer local factories or a container assembled in app shell.
- The composition root builds each shared collaborator once and hands the same
  instance to every consumer; express that with stored lazy properties or a
  Swinject object scope.
- Swinject is allowed at the app shell or adapter edge.
- Domain types, use cases, and repository protocols must not import Swinject or
  UI frameworks.
- Presentation receives use cases, state holders, or factories through
  constructor/init injection.
- Data binds repository implementation to domain repository protocol in the app
  composition root or data assembly.

## Review Additions

```text
ios-clean-architecture: applied
swift-package-boundary: pass|fail
composition-root-location: pass|fail
swinject-confined-to-app-shell-or-edge: pass|fail|n/a
repository-impl-direct-api-client: pass|fail
remote-data-source-boundary: pass|fail
feature-api-public-contract-only: pass|fail
viewmodel-observable-state-boundary: pass|fail|n/a
```

## Evidence Basis

SwiftUI state docs, Swift model data docs, Swinject README, Swinject Assembler
docs, Swinject object scope docs.
