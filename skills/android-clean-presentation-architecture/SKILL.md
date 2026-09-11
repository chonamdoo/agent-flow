---
name: android-clean-presentation-architecture
description: Defines Android Clean Architecture presentation-layer guidance for Hilt DI, ViewModel, StateFlow uiState, one-shot UI events, and Compose screen wiring. Use when creating, modifying, or reviewing Android feature presentation code for state-based UI and domain-to-UiModel boundaries.
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [viewmodel, uistate, uievent, uiaction, uimodel, state holder, compose screen, screen state, presentation layer]
pathGlobs: ["**/*ViewModel.kt", "**/*UiState.kt", "**/presentation/**/*Screen.kt", "**/presentation/src/main/**/*.kt", "**/presentation/src/commonMain/**/*.kt", "**/presentation/src/androidMain/**/*.kt"]
requires: [clean-architecture-core]
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

For Android/Compose or Android-targeted Kotlin/KMP implementation or review:
- Load every matching local `compose-*`, `kotlin-*`, `navigation-3`, `edge-to-edge`, `adaptive`, and `testing-setup` `SKILL.md` named by the active Android profile.
- Read the exact paths supplied by the active phase resolver and installed skill index; do not guess another host's installation paths.
- Apply the missing-skill procedure in `code-generation-discipline` when a required skill is unavailable. Record degraded availability and the paths actually read; absence is not a code defect or grounds for request-changes.

## Architecture Rule

- `presentation` depends on domain contracts and platform/UI abstractions.
- `domain` owns repository interfaces, use cases, and domain models.
- `data` implements domain repositories and binds implementations to interfaces.
- `network` provides Retrofit/API infrastructure.
- A ViewModel may inject a single context's repository interface directly.
  Require an application use case when it crosses contexts, orders meaningful
  multi-step side effects, or adds business failure semantics. Place it according
  to the adopted role mapping; do not add a pure forwarding wrapper.
- Repositories/data map transport failures to domain errors; a presentation mapper converts domain/application errors into screen-specific UI results.
- In every case presentation injects neither a repository implementation, nor a data source, nor an API service.
- When the project uses a feature API/presentation split, keep public route/entry
  contracts in its feature API role and screens/state holders/UI mapping in its
  presentation role. Map actual source roots rather than requiring fixed paths.
- DTOs, entities owned by data, Retrofit models, data sources, and data DI must not reach presentation.
- Do not add `BaseViewModel`, `BaseUiState`, or inherited error hooks for new presentation work. Use explicit helpers and mappers.

## Presentation Boundaries

Keep route/screen wiring, ViewModels, UI values/actions/events, mapping, and
components in the project's adopted presentation boundary. These responsibilities
do not require a fixed folder tree or one file per concept.

Project domain/application values to the UI contract. Identity projection is
valid for an already safe immutable shape without transport dependencies; do not
create redundant models or forwarding mappers. Follow adopted naming conventions:
`UiModel` is a semantic role, not a suffix whose absence alone fails review.

## DI Rule

Application and entry points:
- `@HiltAndroidApp` on the `Application`.
- `@AndroidEntryPoint` on Activities or Fragments that host injected ViewModels.
- Compose obtains ViewModels with `hiltViewModel()` only at the state-holder boundary.

Binding ownership:
- Android platform bindings belong at the app/platform composition boundary.
- Serialization, HTTP clients, and API construction belong at the network adapter boundary.
- Repository bindings and data providers belong at the data adapter/composition boundary.
- Locate those roles in the existing project rather than creating a prescribed DI tree.

Binding rule:
- Use `@Provides` for constructing concrete objects that need factory logic or third-party builders.
- Use `@Binds` for interface-to-implementation mappings.
- Put shared app/data/network bindings in `SingletonComponent` only when the instance is app-wide.
- Do not create Hilt modules for use cases that can use `@Inject constructor`.

Route arguments and startup:
- Use normal `@Inject` with `SavedStateHandle` when the adopted navigation and restoration contract supplies route values there.
- Use Hilt assisted injection when route values need explicit construction-time delivery. Assisted values are not persisted after process death; define restoration separately when required.
- Assisted factories accept only route values. Other dependencies stay normal Hilt injections. Choose by argument lifetime and the actual navigation API, not by the mere presence of a route argument; see [Hilt View Models](https://dagger.dev/hilt/view-model.html).
- ViewModel creation belongs in the route/navigation entry wiring, not inside `Screen`.
- Use AndroidX Startup `Initializer` when the SDK/project adopts that initialization mechanism. If it needs Hilt dependencies, use Hilt `@EntryPoint` and application entry-point access; otherwise preserve the SDK's supported initialization contract.

## ViewModel Rule

ViewModels are screen-level state holders:
- annotate with `@HiltViewModel`
- use normal or assisted constructor injection according to the route-argument contract above
- inject use cases, a single context's repository interface, and platform abstractions
- expose immutable screen state through `StateFlow`
- keep mutable state private
- accept user input through named callbacks for simple screens or a typed action handler for branchy screens
- expose transient UI behavior through an event `Flow` only when it cannot be reduced to durable screen state
- convert non-suspending UI callbacks into `viewModelScope.launch`
- do not hold `Context`, `Activity`, `NavController`, `Navigator`, `Router`, launchers, `Intent`, `WebView`, or Compose state objects
- do not call navigation APIs directly. Emit state or event; route/navigation wiring executes navigation.

State patterns:
- For imperative screen state, use private `MutableStateFlow` and public `asStateFlow()`.
- For repository/use-case streams, map domain data into UI state and share one `stateIn` value in the state-holder scope. Choose `SharingStarted` and any stop timeout from the actual collector lifetime, upstream cost, and freshness requirements.
- Use `MutableStateFlow.update { ... }` for copy/update operations.
- Use `combine(...)` when UI state depends on multiple flows.
- Keep paging request ids, selected item ids, and active `Job` handles private inside the ViewModel.
- This portfolio's default initial-loading policy is subscription-driven: use a cold flow shared by `stateIn`, rather than starting screen-state loading from `init`. Preserve an explicitly adopted lifecycle-driven loading contract or a product requirement for user-triggered loading; record the owner, restart/re-subscription behavior, and freshness rule. This is an adopted policy, not the only Android-supported lifecycle.
- `MutableStateFlow` is for ViewModel-owned input and transient transition state. State produced from a repository or use-case stream terminates in `stateIn` instead of being pushed into a manually updated `MutableStateFlow`.

Event patterns:
- Prefer modeling critical results as durable screen state. Reserve transient UI events for navigation, snackbar, toast, permission launcher, browser intent, focus, or haptic behavior.
- A buffered `Channel` with `receiveAsFlow()` is one single-consumer, no-replay option, not a no-loss guarantee: cancellation after receipt can lose an element before processing. Define owner/collector lifetimes, buffering, cancellation, and consumption explicitly; use durable state when loss is unacceptable. See [receiveAsFlow cancellation semantics](https://kotlinlang.org/api/kotlinx.coroutines/kotlinx-coroutines-core/kotlinx.coroutines.flow/receive-as-flow.html).
- Do not model fire-once effects as `StateFlow`.
- Prefer explicit `UiEvent` sealed interfaces over raw strings or lambdas from ViewModel to UI.

Coroutine rule:
- `viewModelScope.launch` is valid at the UI-to-state-holder boundary.
- Repositories, data sources, and use cases should expose suspend functions or flows instead of storing arbitrary `CoroutineScope`.
- Cancellation-sensitive work should keep `CancellationException` semantics intact.

## UiState Rule

Use a sealed interface for screen state. Add a stability annotation only under
the verified rules below. Represent every reachable condition explicitly:
not-ready when input is missing; loading; refreshing when distinct from first
load; placeholders with stable identity when skeleton content needs it; empty;
success; error; offline when distinct; and permission-required when access
blocks content. Use the project's names and payloads, not a fixed type scaffold.

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

- Model mixed sections as one sealed UI item contract with one subtype per section kind.
- Subtypes are immutable values and expose stable unique identity. Add `@Immutable` only under the `UiModel Stability` rule above.
- The ViewModel or mapper builds one immutable list; the lazy layout uses stable keys, meaningful `contentType` grouping, and exhaustive rendering of its item variants.
- Do not model mixed sections as nullable payload buckets, `Any`, raw `Pair`/`Triple`, or a string type switch, and do not compute the list shape inside the composable.
- Keep callbacks at the call site. Do not store lambdas in an item model.

## Derived Display State

The state holder or mapper owns cross-item and cross-screen display derivation. Precompute these onto the item `UiModel`:
- neighbor and grouping relationships
- position-dependent labels and separator placement
- selection state and available actions derived from screen state

The composable renders these fields. It must not derive them from `items[index ± 1]`, `index == lastIndex`, or unrelated screen state. This is a UDF and testability rule, not a recomposition shortcut.

## Compose Screen Rule

Split state-holder wiring from rendering. The screen entry composable — a `*Route`, or the stateful overload of a same-named `*Screen` — owns wiring; the stateless composable renders. The constraint is **where acquisition happens, not the call shape**: a screen with several state holders may receive holders the navigation entry created as parameters.
- the screen entry composable obtains the ViewModel with `hiltViewModel()`, or receives it from the navigation entry that created it
- route/top-level wiring collects `uiState` with `collectAsStateWithLifecycle()`
- route/top-level wiring collects `uiEventFlow` inside `LaunchedEffect` and lifecycle-aware collection when the screen needs navigation or snackbars
- pass plain `uiState` and callbacks to child composables
- stateless rendering composables receive `uiState` and callbacks, render, and emit actions upward
- stateless rendering composables do not call `hiltViewModel()`, `viewModel()`, `collectAsStateWithLifecycle()`, or navigation APIs
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
  `UiModel` or mapper.** A success state containing the parsed client node model
  already supplies the rendering contract. The mapping boundary belongs to the
  node parser, which converted payloads into client types. When the client
  owns layout, apply the Presentation Boundaries rule; a safe identity projection
  still does not require a separate per-screen type.
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
- `UiState` is an immutable sealed interface; any stability annotation follows the verified `UiModel Stability` rule above.
- `UiState` is explicit for not-ready/loading/refreshing/placeholder/empty/error/offline/permission/success states that can occur; no fake domain default.
- `UiAction`, `UiEvent`, and `UiState` roles are explicit; transient `UiEvent`s are not used for durable state.
- one-shot UI behavior events use `Channel(...).receiveAsFlow()` or another deliberate event model.
- flows converted to UI state use one shared `stateIn` value, not per-call `stateIn`.
- sharing lifetime and timeout follow actual requirements; stale cached `.value` is not treated as a fresh source.
- initial loading follows the subscription-driven default or an explicit existing lifecycle/user-triggered exception, including restart and freshness semantics.
- leaf `*UiModel`s are not blanket-annotated `@Stable`; `@Immutable` or inferred stability is the default.
- mixed section lists use one sealed item contract with stable keys, meaningful `contentType`, and exhaustive rendering.
- neighbor/index/selection display flags are precomputed in the state holder or mapper, not derived inside a composable.
- Preview and Compose UI tests render the stateless screen with a fake `UiState`.
- the screen entry composable (a route, or the stateful overload of a same-named screen) collects with lifecycle APIs and passes state/callbacks downward.
- that same entry composable owns state-holder acquisition, state collection, one-shot event collection, and navigation/platform calls; a multi-holder screen may receive its holders as parameters.
- Stateless rendering composables do not obtain ViewModels, lifecycle flows, Hilt dependencies, or navigation APIs; the explicitly stateful screen entry owns that wiring.
- `@Provides` and `@Binds` are placed in the layer that owns the constructed dependency.
- assisted ViewModel factories pass only route values through assisted parameters.
- use cases with `@Inject constructor` are not manually bound without need.
- repositories/data sources do not store ad-hoc or app-wide `CoroutineScope` for UI-triggered work.
- feature api exposes only public route/contracts; data-layer DTOs and implementations do not leak into presentation.

## Required Markers

When this skill is used for presentation development or code review, write every marker below in the phase artifact or review output. The active workflow `required_markers` is the allowed-value source of truth:

- `presentation-skill: android|flutter|react|react-native|ios|n/a`
- `presentation-state-based-development: applied|n/a`
- `presentation-state-review: pass|fail|n/a`
- `ui-state-modeling: explicit|n/a`
- `presentation-mapping-boundary: domain-to-uimodel|n/a`
- `di-boundary: hilt|context-provider|tsyringe|swift-environment|factory|swift-dependencies|swinject|needle|riverpod|get-it|direct|existing|n/a`

Apply these Android-specific decisions:

- `presentation-skill`: use `android` when Android presentation code is in scope. Use `n/a` only when the phase has no presentation work.
- `presentation-state-based-development`: use `applied` when presentation code was created or changed under this contract. Use `n/a` for review-only work or when no presentation code changed.
- `presentation-state-review`: use `pass` when every applicable checklist item passes, `fail` when any applicable item fails, and `n/a` only when no Android presentation code is in scope.
- `ui-state-modeling`: use `explicit` when the screen's durable states are modeled explicitly. Use `n/a` only when no screen state is in scope.
- `presentation-mapping-boundary`: use `domain-to-uimodel` for domain/application
  values projected to the UI contract, including safe identity projection;
  no separate mapper file is required. Use `n/a` only when no such boundary exists.
- `di-boundary`: use `hilt`, `direct`, or `existing` for the verified Android composition path. Use `n/a` only when the change neither creates nor reviews dependency wiring.

A `fail` result is actionable: record the failed criterion and return to the workflow's fix path before approval.

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
