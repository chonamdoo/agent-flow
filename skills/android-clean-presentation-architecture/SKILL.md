---
name: android-clean-presentation-architecture
description: Use when creating, modifying, or reviewing an Android Clean Architecture presentation layer with Hilt DI, ViewModel, StateFlow uiState, one-shot UI events, and Compose screen wiring.
required_markers:
  - "presentation-skill: android"
  - "presentation-state-based-development: applied|n/a"
  - "presentation-state-review: pass|fail"
  - "ui-state-modeling: explicit"
  - "presentation-mapping-boundary: domain-to-uimodel|n/a"
  - "di-boundary: hilt"
---

# Android Clean Presentation Architecture

Use this skill for Android feature work where presentation code should follow a reusable Clean Architecture pattern.

## Evidence Basis

- Android Hilt docs define `@HiltAndroidApp`, `@AndroidEntryPoint`, `@HiltViewModel`, Hilt modules, and Jetpack integration.
- Android architecture docs position `ViewModel` as the screen-level state holder.
- Android coroutine/Flow docs support `StateFlow`, `stateIn`, and `SharingStarted` for observable UI state.
- Jetpack Compose state docs support state flowing down and events flowing up, with lifecycle-aware collection in Android UI.

## Architecture Rule

- `presentation` depends on domain use cases and platform/UI abstractions.
- `domain` owns repository interfaces, use cases, and domain models.
- `data` implements domain repositories and binds implementations to interfaces.
- `network` provides Retrofit/API infrastructure.
- ViewModels do not inject Retrofit APIs, data sources, or repository implementations directly when a domain use case exists.

## Package Shape

Feature presentation packages should stay screen-oriented:
- `presentation/<flow>/<screen>/<Screen>.kt`
- `presentation/<flow>/<screen>/<Screen>ViewModel.kt`
- `presentation/<flow>/<screen>/model/<Screen>UiState.kt`
- `presentation/<flow>/<screen>/model/<Screen>UiEvent.kt`
- `presentation/<flow>/<screen>/model/<Screen>UiModel.kt`
- `presentation/<flow>/<screen>/mapper/*Mapper.kt`
- `presentation/<flow>/<screen>/component/*`

Presentation mappers must convert domain data into presentation models before state reaches Compose UI.
Presentation model types must use the `UiModel` postfix, for example `<Screen>ItemUiModel`.

## DI Rule

Application and entry points:
- `@HiltAndroidApp` on the `Application`.
- `@AndroidEntryPoint` on Activities or Fragments that host injected ViewModels.
- Compose obtains ViewModels with `hiltViewModel()` only at the state-holder boundary.

Module placement:
- `app/di`: Android platform bindings such as `SharedPreferences`, `ResourceProvider`, `NetworkStatusChecker`.
- `core/network/di`: `Json`, `OkHttpClient`, `Retrofit`, API creation.
- `core/data/<feature>/di`: data API providers and repository bindings.

Binding rule:
- Use `@Provides` for constructing concrete objects that need factory logic or third-party builders.
- Use `@Binds` for interface-to-implementation mappings.
- Put shared app/data/network bindings in `SingletonComponent` only when the instance is app-wide.
- Do not create Hilt modules for use cases that can use `@Inject constructor`.

## ViewModel Rule

ViewModels are screen-level state holders:
- annotate with `@HiltViewModel`
- use `@Inject constructor`
- inject domain use cases and platform abstractions
- expose immutable `StateFlow<ScreenUiState>`
- keep mutable state private
- expose one-shot events as `Flow<ScreenUiEvent>`
- convert non-suspending UI callbacks into `viewModelScope.launch`

State patterns:
- For imperative screen state, use private `MutableStateFlow<ScreenUiState>` and public `asStateFlow()`.
- For repository/use-case streams, map domain data to `ScreenUiState` and terminate with `stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), initialState)`.
- Use `MutableStateFlow.update { ... }` for copy/update operations.
- Use `combine(...)` when UI state depends on multiple flows.
- Keep paging request ids, selected item ids, and active `Job` handles private inside the ViewModel.

Event patterns:
- Use `Channel<ScreenUiEvent>(Channel.BUFFERED)` plus `receiveAsFlow()` for single-consumer navigation/snackbar/toast events.
- Do not model fire-once effects as `StateFlow`.
- Prefer explicit `UiEvent` sealed interfaces over raw strings or lambdas from ViewModel to UI.

Coroutine rule:
- `viewModelScope.launch` is valid at the UI-to-state-holder boundary.
- Repositories, data sources, and use cases should expose suspend functions or flows instead of storing arbitrary `CoroutineScope`.
- Cancellation-sensitive work should keep `CancellationException` semantics intact.

## UiState Rule

Use `sealed interface <Screen>UiState` for screen state:
- `data object NotReady` or a domain-specific not-ready state when input is missing
- `data object Loading`
- `data object Refreshing` when refresh is visually distinct from first load
- `data object Empty` or `SearchNotReady` when absence is a real UI state
- `data class Success(...)`
- `data class Error(...)`
- `data object Offline` when network absence is a distinct UI state
- `data object PermissionRequired` when permission is required before content can load

Avoid fake domain sentinel values. If not-ready, loading, refreshing, empty, error, offline, permission-required, or success can happen, model it explicitly in the `UiState` type.

Keep `UiState` immutable:
- expose presentation `UiModel` types, not mutable domain/data entities
- prefer immutable collections when the project already uses them
- include only data needed by the UI surface

## Compose Screen Rule

Split state-holder wiring from rendering:
- top-level screen/route obtains the ViewModel with `hiltViewModel()`
- collect `uiState` with `collectAsStateWithLifecycle()`
- collect `uiEventFlow` inside `LaunchedEffect` and lifecycle-aware collection when the screen needs navigation or snackbars
- pass plain `uiState` and callbacks to child composables
- child composables should not know about Hilt, repositories, use cases, or ViewModels

Keep UI-local state local:
- scroll, focus, text field editing state, pager state, and animation state can stay in Compose unless they drive business or repository work
- if UI-local state coordinates multiple fields and operations, extract a plain state holder remembered in composition

## Review Checklist

- `ViewModel` constructor injects use cases/platform abstractions, not data implementation classes.
- `uiState` is public immutable `StateFlow`; mutable state is private.
- `UiState` is explicit for not-ready/loading/refreshing/empty/error/offline/permission/success states that can occur; no fake domain default.
- one-shot events use `Channel(...).receiveAsFlow()` or another deliberate event model.
- flows converted to UI state use one shared `stateIn` value, not per-call `stateIn`.
- `SharingStarted.WhileSubscribed(5_000)` is acceptable only when stale cached `.value` is not used as a fresh source.
- Compose screens collect with lifecycle APIs and pass state/callbacks downward.
- `@Provides` and `@Binds` are placed in the layer that owns the constructed dependency.
- use cases with `@Inject constructor` are not manually bound without need.
- repositories/data sources do not store ad-hoc or app-wide `CoroutineScope` for UI-triggered work.

## Required Markers

When this skill is used for presentation development or code review, include these markers in the completion artifact or review output:

- `presentation-skill: android`
- `presentation-state-based-development: applied|n/a`
- `presentation-state-review: pass|fail`
- `ui-state-modeling: explicit`
- `presentation-mapping-boundary: domain-to-uimodel|n/a`
- `di-boundary: hilt`

## Sources

- Android Hilt: https://developer.android.com/training/dependency-injection/hilt-android
- Hilt and Jetpack integrations: https://developer.android.com/training/dependency-injection/hilt-jetpack
- Android ViewModel: https://developer.android.com/topic/libraries/architecture/viewmodel
- StateFlow and SharedFlow: https://developer.android.com/kotlin/flow/stateflow-and-sharedflow
- Compose state: https://developer.android.com/develop/ui/compose/state
