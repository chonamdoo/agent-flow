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
- Android UI layer docs separate screen UI state from UI element state and describe UDF/state holders as the UI state production pipeline.
- Android UI events docs distinguish durable state from transient events and recommend reducing critical one-off events to UI state when delivery matters.
- Compose state hoisting docs require immutable state down and events up from the lowest correct state owner.
- Compose stability docs allow `@Stable` on types whose mutation contract Compose cannot otherwise infer.

## Compose/Kotlin Local Skill Loading

For Android/Kotlin/Compose/KMP implementation or review:
- Load every matching local `compose-*`, `kotlin-*`, `navigation-3`, `edge-to-edge`, `adaptive`, and `testing-setup` `SKILL.md` named by the active Android profile.
- Prefer project-local skills under `.agent-flow/local-skills/<skill>/SKILL.md`; otherwise use `.agent-flow/skills/<skill>/SKILL.md` or the current host's configured local skill directory.
- Record loaded skills as `android-local-skills-used: <skill list>`.
- Use `android-local-skills-used: n/a` only when no matching Android/Compose/Kotlin skill exists.
- Do not say "Compose/Kotlin convention applied" or approve Compose/Kotlin code if matching local skill files were not explicitly loaded in the current work session.

## Architecture Rule

- `presentation` depends on domain use cases and platform/UI abstractions.
- `domain` owns repository interfaces, use cases, and domain models.
- `data` implements domain repositories and binds implementations to interfaces.
- `network` provides Retrofit/API infrastructure.
- ViewModels do not inject Retrofit APIs, data sources, or repository implementations directly when a domain use case exists.
- Keep public feature contracts, including route keys and exported entry contracts, in `feature:*:api` when the repo uses feature api/presentation split.
- Keep Compose screens, routes, ViewModels, UI contracts, UI models, and mappers in `feature:*:presentation`.
- DTOs, entities owned by data, Retrofit models, data sources, and data DI must not reach presentation.
- Do not add `BaseViewModel`, `BaseUiState`, or inherited error hooks for new presentation work. Use explicit helpers and mappers.

## Package Shape

Feature presentation packages should stay screen-oriented:
- `presentation/<flow>/<screen>/<Screen>.kt`
- `presentation/<flow>/<screen>/<Screen>ViewModel.kt`
- `presentation/<flow>/<screen>/model/<Screen>UiState.kt`
- `presentation/<flow>/<screen>/model/<Screen>UiAction.kt`
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

Route arguments and startup:
- Use `@HiltViewModel` with normal `@Inject` when no route argument is needed.
- Use `@HiltViewModel(assistedFactory = ...)` plus `@AssistedInject` when the ViewModel needs a NavKey or serializable route value.
- `@AssistedFactory.create(...)` should take only the NavKey or route value. Other dependencies stay normal Hilt injections.
- ViewModel creation belongs in the route/navigation entry wiring, not inside `Screen`.
- Use AndroidX Startup `Initializer` for one-shot SDK initialization. If initializer code needs Hilt dependencies, use Hilt `@EntryPoint` plus `EntryPointAccessors.fromApplication(...)`.

## ViewModel Rule

ViewModels are screen-level state holders:
- annotate with `@HiltViewModel`
- use `@Inject constructor`
- inject domain use cases and platform abstractions
- expose immutable `StateFlow<ScreenUiState>`
- keep mutable state private
- accept user input through named callbacks for simple screens or `fun onAction(action: ScreenUiAction)` for branchy screens
- expose one-shot UI behavior effects as `Flow<ScreenUiEvent>` only when they cannot be reduced to durable `ScreenUiState`
- convert non-suspending UI callbacks into `viewModelScope.launch`
- use `fun onAction(action: ScreenUiAction)` when the screen has multiple events or branchy behavior
- do not hold `Context`, `Activity`, `NavController`, `Navigator`, `Router`, launchers, `Intent`, `WebView`, or Compose state objects
- do not call navigation APIs directly. Emit state or event; route/navigation wiring executes navigation.

State patterns:
- For imperative screen state, use private `MutableStateFlow<ScreenUiState>` and public `asStateFlow()`.
- For repository/use-case streams, map domain data to `ScreenUiState` and terminate with `stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), initialState)`.
- Use `MutableStateFlow.update { ... }` for copy/update operations.
- Use `combine(...)` when UI state depends on multiple flows.
- Keep paging request ids, selected item ids, and active `Job` handles private inside the ViewModel.

Event patterns:
- Prefer modeling critical results as `ScreenUiState`; use `ScreenUiEvent` only for UI behavior such as navigation, snackbar, toast, permission launcher, browser intent, focus, or haptic feedback.
- Use `Channel<ScreenUiEvent>(Channel.BUFFERED)` plus `receiveAsFlow()` for single-consumer UI behavior events.
- Do not model fire-once effects as `StateFlow`.
- Prefer explicit `UiEvent` sealed interfaces over raw strings or lambdas from ViewModel to UI.

Coroutine rule:
- `viewModelScope.launch` is valid at the UI-to-state-holder boundary.
- Repositories, data sources, and use cases should expose suspend functions or flows instead of storing arbitrary `CoroutineScope`.
- Cancellation-sensitive work should keep `CancellationException` semantics intact.

## UiState Rule

Use `@Stable sealed interface <Screen>UiState` for screen state by default:
- `data object NotReady` or a domain-specific not-ready state when input is missing
- `data object Loading`
- `data object Refreshing` when refresh is visually distinct from first load
- `data object Placeholder` or `data class Placeholder(...)` when skeleton rows/cards need stable placeholder `UiModel`s
- `data object Empty` or `SearchNotReady` when absence is a real UI state
- `data class Success(...)`
- `data class Error(...)`
- `data object Offline` when network absence is a distinct UI state
- `data object PermissionRequired` when permission is required before content can load

`UiState`, `UiAction`, and `UiEvent` roles:
- `UiState` is durable render data. It must be replayable and enough to redraw the screen after recreation.
- `UiAction` is user or UI input flowing upward to the state holder.
- `UiEvent` is a transient UI behavior command consumed by route/top-level wiring.

Avoid fake domain sentinel values. If not-ready, loading, refreshing, placeholder, empty, error, offline, permission-required, or success can happen, model it explicitly in the `UiState` type.

Keep `UiState` immutable:
- expose presentation `UiModel` types, not mutable domain/data entities
- prefer immutable collections when the project already uses them
- include only data needed by the UI surface

## Compose Screen Rule

Split state-holder wiring from rendering:
- route/top-level wiring obtains the ViewModel with `hiltViewModel()`
- route/top-level wiring collects `uiState` with `collectAsStateWithLifecycle()`
- route/top-level wiring collects `uiEventFlow` inside `LaunchedEffect` and lifecycle-aware collection when the screen needs navigation or snackbars
- pass plain `uiState` and callbacks to child composables
- screen/content composables are stateless: receive `uiState` and callbacks, render, and emit actions upward
- screen/content composables should not call `hiltViewModel()`, `viewModel()`, `collectAsStateWithLifecycle()`, or navigation APIs
- child composables should not know about Hilt, repositories, use cases, or ViewModels
- collect navigation and one-shot commands with `collect`, not `collectLatest`

Keep UI-local state local:
- scroll, focus, text field editing state, pager state, and animation state can stay in Compose unless they drive business or repository work
- if UI-local state coordinates multiple fields and operations, extract a plain state holder remembered in composition

## Navigation Rule

- Feature api modules define serializable route keys or public route contracts.
- Feature presentation modules install concrete navigation entries or screen factories.
- App/core navigation composes feature installers; it should not know screen internals.
- Route keys carry serializable data only. Do not put lambdas, `NavController`, `Context`, or mutable objects in route keys.

## Review Checklist

- `ViewModel` constructor injects use cases/platform abstractions, not data implementation classes.
- `uiState` is public immutable `StateFlow`; mutable state is private.
- `UiState` is `@Stable sealed interface` unless the repo has a documented exception.
- `UiState` is explicit for not-ready/loading/refreshing/placeholder/empty/error/offline/permission/success states that can occur; no fake domain default.
- `UiAction`, `UiEvent`, and `UiState` roles are explicit; transient `UiEvent`s are not used for durable state.
- one-shot UI behavior events use `Channel(...).receiveAsFlow()` or another deliberate event model.
- flows converted to UI state use one shared `stateIn` value, not per-call `stateIn`.
- `SharingStarted.WhileSubscribed(5_000)` is acceptable only when stale cached `.value` is not used as a fresh source.
- Route/top-level Compose wiring collects with lifecycle APIs and passes state/callbacks downward.
- Route/top-level wiring owns ViewModel collection, one-shot event collection, and navigation/platform calls.
- Screen/content composables are stateless and do not obtain ViewModels, lifecycle flows, Hilt dependencies, or navigation APIs.
- `@Provides` and `@Binds` are placed in the layer that owns the constructed dependency.
- assisted ViewModel factories pass only route values through assisted parameters.
- use cases with `@Inject constructor` are not manually bound without need.
- repositories/data sources do not store ad-hoc or app-wide `CoroutineScope` for UI-triggered work.
- feature api exposes only public route/contracts; data-layer DTOs and implementations do not leak into presentation.

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
- Android UI layer: https://developer.android.com/topic/architecture/ui-layer
- Android state holders and UI state: https://developer.android.com/topic/architecture/ui-layer/stateholders
- Android UI state production: https://developer.android.com/topic/architecture/ui-layer/state-production
- Android UI events: https://developer.android.com/topic/architecture/ui-layer/events
- StateFlow and SharedFlow: https://developer.android.com/kotlin/flow/stateflow-and-sharedflow
- Compose state: https://developer.android.com/develop/ui/compose/state
- Compose state hoisting: https://developer.android.com/develop/ui/compose/state-hoisting
- Compose stability: https://developer.android.com/develop/ui/compose/performance/stability
- AndroidX Startup: https://developer.android.com/topic/libraries/app-startup
