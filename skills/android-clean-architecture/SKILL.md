---
name: android-clean-architecture
description: Android/Kotlin Clean Architecture adapter for the platform-neutral clean-architecture-core contract. Use for Gradle module shape, Kotlin package paths, Hilt DI, Retrofit remote data sources, ViewModel/Compose presentation, and Android architecture review.
---

# Android Clean Architecture

Load `clean-architecture-core` first. This skill adds Android/Kotlin module,
package, DI, and sample-code details only.

## Module Shape

```text
app/
core/{ui,designsystem,resources,platform,network,navigation/api,navigation/impl}
core/domain/<context>/
core/data/<context>/
feature/<name>/api/
feature/<name>/presentation/
```

Android paths include Gradle module path plus Kotlin source root/package path.
The semantic boundary is the Gradle module family, not the repeated package
string after `src/main/java`.

## Package Roles

- `app`: `Application`, root activity, root navigation, global error host,
  app-wide startup, and Hilt composition root.
- `core/domain/<context>`: domain models, repository interfaces, use cases,
  policies, domain services, domain errors.
- `core/data/<context>`: repository impls, API services, DTOs, mappers,
  remote/local sources, cache, Hilt data modules.
- `core/network`: Retrofit/OkHttp factories, response envelope, common network
  failure mapping, interceptors, qualifiers.
- `core/navigation/api`: route/nav-key contracts.
- `core/navigation/impl`: concrete navigation graph/entry composition.
- `feature/<name>/api`: public route keys and feature entry contracts.
- `feature/<name>/presentation`: routes, screens, ViewModels, UiState, UiAction,
  UiEvent, UiModel, domain-to-UI mappers, components.

## Hilt DI

- Use `@HiltAndroidApp` on the `Application`.
- Use `@AndroidEntryPoint` on activity/fragment hosts.
- Use `@HiltViewModel` for ViewModels.
- Put `@Provides` for third-party builders/factory logic.
- Put `@Binds` for interface-to-implementation mappings.
- Put Retrofit and OkHttp construction in `core/network/di`.
- Put API service providers and repository bindings in `core/data/<context>/di`.
- Use cases with `@Inject constructor` do not need Hilt modules.
- Assisted ViewModel factories pass only route values; other dependencies stay
  normal Hilt injections.

## Data Boundary

Default production flow:

```text
HomeRepositoryImpl -> HomeRemoteDataSource -> HomeApiService
```

- Keep Retrofit interfaces in `core/data/<context>/api`.
- Keep DTO/request/response models in `core/data/<context>/model`.
- Keep remote data sources in `core/data/<context>/source/remote`.
- Keep local sources in `core/data/<context>/source/local`.
- Keep data-to-domain mapping in `core/data/<context>/mapper`.
- Repository impls compose sources/cache/mappers and return domain models only.
- Do not create canonical samples where `RepositoryImpl` injects an API service
  directly.

## Presentation Boundary

- Route/top-level wiring obtains ViewModel, collects state/events with lifecycle,
  performs navigation/platform calls, and passes plain state/callbacks down.
- Screen/content composables are stateless and do not call `hiltViewModel()`,
  `viewModel()`, lifecycle collection, or navigation APIs.
- ViewModels inject use cases/platform abstractions, not repositories impls,
  data sources, API services, DTOs, `Context`, `Activity`, or `NavController`.
- UI state is immutable and explicit for loading/error/empty/offline/success
  states that can occur.
- Domain-to-UI mapping lives in presentation mappers.

## Generic Sample

```kotlin
package com.example.app.core.data.home.repository

internal class HomeRepositoryImpl @Inject constructor(
    private val remoteDataSource: HomeRemoteDataSource,
) : HomeRepository {
    override suspend fun getHomeFeed(): HomeFeed =
        remoteDataSource.getHomeFeed().toDomain()
}

internal interface HomeRemoteDataSource {
    suspend fun getHomeFeed(): HomeFeedResponse
}

internal class HomeRemoteDataSourceImpl @Inject constructor(
    private val apiService: HomeApiService,
) : HomeRemoteDataSource {
    override suspend fun getHomeFeed(): HomeFeedResponse =
        apiService.getHomeFeed()
}
```

```kotlin
@Module
@InstallIn(SingletonComponent::class)
internal abstract class HomeDataModule {
    @Binds abstract fun bindHomeRepository(
        impl: HomeRepositoryImpl,
    ): HomeRepository

    @Binds abstract fun bindHomeRemoteDataSource(
        impl: HomeRemoteDataSourceImpl,
    ): HomeRemoteDataSource
}
```

## Review Additions

```text
android-clean-architecture: applied
gradle-module-boundary: pass|fail
kotlin-package-boundary: pass|fail
hilt-composition-root: pass|fail
repository-impl-direct-api-service: pass|fail
remote-data-source-boundary: pass|fail
feature-api-public-contract-only: pass|fail
viewmodel-injects-usecases-only: pass|fail|n/a
compose-route-screen-split: pass|fail|n/a
```

## Evidence Basis

Hilt Android docs, Android ViewModel docs, Android UI layer docs, Compose state
docs, StateFlow and SharedFlow docs.
