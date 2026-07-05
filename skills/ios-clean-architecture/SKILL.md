---
name: ios-clean-architecture
description: iOS platform adapter for `clean-architecture-core` that maps the core contract to Swift packages/source layout, app-shell composition roots, Swinject or local factories, repository/source/mapper boundaries, and iOS platform boundaries. Use when applying Clean Architecture to iOS app/package architecture or reviewing data/DI/module boundaries; pair with ios-clean-presentation-architecture for SwiftUI/UIKit ViewModel, UiState, or presentation-only work.
---

# iOS Clean Architecture

This is not a standalone Clean Architecture guide. Load [`clean-architecture-core`](../clean-architecture-core/SKILL.md) first; this skill adds iOS-specific source/package, DI, and platform-boundary details only.

## Quick start

1. Apply the layer, dependency-direction, and review rules from `clean-architecture-core`.
2. Use this adapter only to translate those rules into Swift packages/source groups, app-shell composition roots, Swinject/local factory boundaries, and iOS platform adapters.
3. If the task is only SwiftUI/UIKit state, ViewModel wiring, navigation effects, or presentation mapping, pair with `ios-clean-presentation-architecture` before applying presentation details.

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
- Swinject is allowed at the app shell or adapter edge.
- Domain types, use cases, and repository protocols must not import Swinject or
  UI frameworks.
- Presentation receives use cases, state holders, or factories through
  constructor/init injection.
- Data binds repository implementation to domain repository protocol in the app
  composition root or data assembly.

## Generic Sample

```swift
protocol HomeRepository {
    func homeFeed() async throws -> HomeFeed
}

final class HomeRepositoryImpl: HomeRepository {
    private let remoteDataSource: HomeRemoteDataSource

    init(remoteDataSource: HomeRemoteDataSource) {
        self.remoteDataSource = remoteDataSource
    }

    func homeFeed() async throws -> HomeFeed {
        try await remoteDataSource.homeFeed().toDomain()
    }
}
```

```swift
final class AppContainer {
    lazy var homeRemoteDataSource =
        HomeRemoteDataSourceImpl(apiClient: apiClient)

    lazy var homeRepository: HomeRepository =
        HomeRepositoryImpl(remoteDataSource: homeRemoteDataSource)

    func makeHomeViewModel() -> HomeViewModel {
        HomeViewModel(getHomeFeed: GetHomeFeedUseCase(repository: homeRepository))
    }
}
```

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
