---
name: android-clean-presentation-architecture
description: Defines Android Clean Architecture presentation-layer guidance for Hilt DI, ViewModel, StateFlow uiState, one-shot UI events, and Compose screen wiring. Use when creating, modifying, or reviewing Android feature presentation code for state-based UI and domain-to-UiModel boundaries.
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [viewmodel, uistate, uievent, uiaction, uimodel, state holder, compose screen, 상태 홀더, 화면 상태, 프레젠테이션 계층]
pathGlobs: ["**/*ViewModel.kt", "**/*UiState.kt", "**/presentation/**/*Screen.kt", "**/presentation/src/main/**/*.kt", "**/presentation/src/commonMain/**/*.kt", "**/presentation/src/androidMain/**/*.kt"]
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

## Quick start

1. Use this for feature or screen presentation state: Hilt entry wiring, ViewModel state holders, `UiState`/`UiAction`/`UiEvent`, `UiModel` mapping, and stateless Compose rendering.
2. Start by locating the screen state holder and its domain dependencies, then model durable `UiState` before wiring Compose routes/screens.
3. If the task is app-wide common error UI, session expiry, maintenance mode, root navigation reset, or global dialog/snackbar/toast ownership, use `android-appshell-error-handling` instead.

## Do not use for

- AppShell-owned common error hosts, `SessionExpired`/`Maintenance` root flow switching, or Navi3 root back stack resets; use `android-appshell-error-handling`.
- Data, network, or domain implementation design except to enforce presentation boundaries and mapper responsibilities.


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
- Do not say "Compose/Kotlin convention applied" or approve Compose/Kotlin code if matching local skill files were not explicitly loaded in the current work session.

## Architecture Rule

- `presentation` depends on domain contracts and platform/UI abstractions.
- `domain` owns repository interfaces, use cases, and domain models.
- `data` implements domain repositories and binds implementations to interfaces.
- `network` provides Retrofit/API infrastructure.
- A ViewModel may inject a **single context's repository interface** directly. Put a use case in `core/domain/<context>` when one of these holds: (a) it combines repositories from two or more contexts, (b) it runs multi-step side effects whose order carries meaning (reservation, fence, polling), (c) it translates transport or domain error codes into the screen's result type. Do not write a use case that forwards one repository method without changing its arguments.
- In every case presentation injects neither a repository implementation, nor a data source, nor an API service.
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
- inject use cases, a single context's repository interface, and platform abstractions
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
- Do not start initial screen-state loading from `init`. Model it as a cold flow terminated by `stateIn` so the work starts when the route collects — including a one-shot initial API load, unless product behavior requires an explicit user action to begin.
- `MutableStateFlow` is for ViewModel-owned input and transient transition state. State produced from a repository or use-case stream terminates in `stateIn` instead of being pushed into a manually updated `MutableStateFlow`.

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

## UiModel Stability

- Leaf `*UiModel` data classes are not annotated with `@Stable` by default. When every field is immutable, let the Compose compiler infer stability.
- When a pure immutable value model needs an explicit contract, prefer `@Immutable` over `@Stable`.
- `@Stable` is allowed only when the equality contract is actually true, and only when a compiler stability report or a measured recomposition problem backs it.
- At presentation boundaries prefer `ImmutableList`, `ImmutableSet`, or `ImmutableMap`. Do not annotate a model to hide a raw mutable collection.

## List Item Modeling

Use this when one scroll surface renders several distinct section types.

- Model the mixed sections as one `@Stable sealed interface <Screen>ListItemUiModel` with one subtype per section.
- Subtypes are `@Immutable data class`es or `data object`s, and each exposes a stable unique `val key: String`.
- The ViewModel or mapper builds one immutable list; the lazy layout renders it with `items(items, key = { it.key }, contentType = { it::class })` and an exhaustive `when`.
- Do not model mixed sections as nullable payload buckets, `Any`, raw `Pair`/`Triple`, or a string type switch, and do not compute the list shape inside the composable.
- Keep callbacks at the call site. Do not store lambdas in an item model.

## Derived Display State

The state holder or mapper owns cross-item and cross-screen display derivation. Precompute these onto the item `UiModel`:
- neighbor and grouping flags (`showHeader`, `isLastIntroMessage`, block grouping, previous speaker)
- index and position flags (`isLast`, section index display, separator placement)
- selection and action flags derived from screen state (`selectable`, `isSelected`, `canDelete`)

The composable renders these fields. It must not derive them from `items[index ± 1]`, `index == lastIndex`, or unrelated screen state. This is a UDF and testability rule, not a recomposition shortcut.

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
- Preview and Compose UI tests target the stateless screen/content composable with a fake `UiState`.

Keep UI-local state local:
- scroll, focus, text field editing state, pager state, and animation state can stay in Compose unless they drive business or repository work
- if UI-local state coordinates multiple fields and operations, extract a plain state holder remembered in composition

## Server-Driven Screen Exception

Screens whose layout is authored by the server follow this contract with three
scoped exceptions. Everything not listed here still applies, including the
stateless-content rule — a node renderer is a content composable.

- **Names are reversed; judge by direction.** A server-driven codebase commonly
  calls the upward input type `ScreenEvent` and the downward one-shot type
  `UiEffect`. `ScreenEvent` plays the role this document calls `UiAction`, and
  `UiEffect` plays the role it calls `UiEvent`. Decide which role a type has from
  the direction it travels, never from its suffix.
- **A screen whose `UiState` carries the server node tree needs no per-screen
  `UiModel` or mapper.** `Success(screen: Screen)` is a complete state type when
  `Screen` is the parsed node model; the mapping boundary belongs to the node
  parser, which already converted the payload into client types. Per-screen
  presentation models return as soon as a screen owns its own layout.
- **One shared abstract state holder for server-driven screens is a documented
  exception to the `BaseViewModel` prohibition.** The shared holder owns the whole
  state pipeline — storage flow, refresh, effect channel — because every
  server-driven screen has the same pipeline. Subclasses supply only the screen id
  and screen-specific context values. A subclass that reaches into the state
  pipeline is a screen that should not be server-driven.

## Navigation Rule

- Feature api modules define serializable route keys or public route contracts.
- Feature presentation modules install concrete navigation entries or screen factories.
- App/core navigation composes feature installers; it should not know screen internals.
- Route keys carry serializable data only. Do not put lambdas, `NavController`, `Context`, or mutable objects in route keys.

## Review Checklist

- `ViewModel` constructor injects use cases, one context's repository interface, and platform abstractions — never a repository implementation, data source, or API service. A use case is required only for the cross-context, ordered-side-effect, or error-translation cases named in the Architecture Rule.
- `uiState` is public immutable `StateFlow`; mutable state is private.
- `UiState` is `@Stable sealed interface` unless the repo has a documented exception.
- `UiState` is explicit for not-ready/loading/refreshing/placeholder/empty/error/offline/permission/success states that can occur; no fake domain default.
- `UiAction`, `UiEvent`, and `UiState` roles are explicit; transient `UiEvent`s are not used for durable state.
- one-shot UI behavior events use `Channel(...).receiveAsFlow()` or another deliberate event model.
- flows converted to UI state use one shared `stateIn` value, not per-call `stateIn`.
- `SharingStarted.WhileSubscribed(5_000)` is acceptable only when stale cached `.value` is not used as a fresh source.
- initial screen-state loading is not started from `init`; a cold flow plus `stateIn` starts it when the route collects.
- leaf `*UiModel`s are not blanket-annotated `@Stable`; `@Immutable` or inferred stability is the default.
- mixed section lists use one sealed `*ListItemUiModel` with stable keys, `contentType`, and an exhaustive `when`.
- neighbor/index/selection display flags are precomputed in the state holder or mapper, not derived inside a composable.
- Preview and Compose UI tests render the stateless screen with a fake `UiState`.
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

- Android Hilt docs
- Hilt and Jetpack integration docs
- Android ViewModel docs
- Android UI layer docs
- Android state holder and UI state production docs
- Android UI events docs
- StateFlow and SharedFlow docs
- Compose state, state hoisting, and stability docs
- AndroidX Startup docs
