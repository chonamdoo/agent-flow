---
name: react-clean-architecture
description: React Web and Next.js platform adapter for `clean-architecture-core` that maps the core contract to monorepo packages, Context Provider composition roots, optional TSyringe edge use, repository/source/mapper boundaries, and web platform adapters. Use when applying Clean Architecture across React packages or reviewing package/data/DI boundaries; pair with react-clean-presentation-architecture for hooks, UiState, component-state-holder, or presentation-only work.
---

# React Clean Architecture

This is not a standalone Clean Architecture guide. Load [`clean-architecture-core`](../clean-architecture-core/SKILL.md) first; this skill adds React/Next.js package, composition-root, DI-edge, and platform-boundary details only.

## Quick start

1. Apply the layer, dependency-direction, and review rules from `clean-architecture-core`.
2. Use this adapter only to translate those rules into React/Next.js packages, Context Provider composition roots, optional TSyringe edge use, and web platform adapters.
3. If the task is only hooks, UiState, component/state-holder split, effects, or presentation mapping, pair with `react-clean-presentation-architecture` before applying presentation details.

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

## Generic Sample

```ts
export interface HomeRepository {
  getHomeFeed(): Promise<HomeFeed>
}

export class HomeRepositoryImpl implements HomeRepository {
  constructor(private readonly remoteDataSource: HomeRemoteDataSource) {}

  async getHomeFeed(): Promise<HomeFeed> {
    return toHomeFeed(await this.remoteDataSource.getHomeFeed())
  }
}

export class HomeRemoteDataSourceImpl implements HomeRemoteDataSource {
  constructor(private readonly apiClient: HomeApiClient) {}

  getHomeFeed(): Promise<HomeFeedResponse> {
    return this.apiClient.getHomeFeed()
  }
}
```

```tsx
export function createDependencies() {
  const homeApiClient = new HomeApiClient(fetchClient)
  const homeRemoteDataSource = new HomeRemoteDataSourceImpl(homeApiClient)
  const homeRepository = new HomeRepositoryImpl(homeRemoteDataSource)

  return {
    getHomeFeed: new GetHomeFeedUseCase(homeRepository),
  }
}
```

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
