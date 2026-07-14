---
name: ios-clean-presentation-architecture
description: Defines iOS Clean Architecture presentation-layer guidance for SwiftUI/UIKit state holders, explicit UiState, UiModel mapping, dependency injection, and state-based presentation review. Use when creating, modifying, or reviewing iOS feature presentation code for durable screen state and domain-to-UiModel boundaries.
required_markers:
  - "presentation-skill: ios"
  - "presentation-state-based-development: applied|n/a"
  - "presentation-state-review: pass|fail"
  - "ui-state-modeling: explicit"
  - "presentation-mapping-boundary: domain-to-uimodel|n/a"
  - "di-boundary: swift-environment|factory|swift-dependencies|swinject|needle|direct|existing|n/a"
---

# iOS Clean Presentation Architecture

Use this skill for iOS feature work where SwiftUI or UIKit presentation code should follow a reusable Clean Architecture pattern.

## Quick start

1. Use this for feature or screen presentation state: SwiftUI/UIKit state holders, dependency injection, `UiState`/`UiAction`/`UiEvent`, `UiModel` mapping, and render-focused views.
2. Start by locating the screen state holder and its domain dependencies, then model durable `UiState` before wiring SwiftUI/UIKit views.
3. If the task is app-wide common error UI, session expiry, maintenance mode, root navigation reset, or global alert/sheet/toast ownership, use `ios-app-shell-error-handling` instead.

## Do not use for

- AppShell/root-coordinator common error hosts, `SessionExpired`/`Maintenance` flow switching, or root navigation resets; use `ios-app-shell-error-handling`.
- Data, infrastructure, or domain implementation design except to enforce presentation boundaries and mapper responsibilities.


## Evidence Basis

- Apple SwiftUI `EnvironmentValues`: SwiftUI views can read values from the environment, and custom environment values can be created with `@Entry`.
- Apple SwiftUI user-interface state docs define least-common-ancestor state ownership, read-only values, and `Binding` for two-way child access.
- Apple SwiftUI model-data docs define observable model data as separate from views and use Observation to keep UI updated.
- Apple `State` docs define `@State` as a private source of truth for value state in a view hierarchy.
- Apple `Binding` docs define a binding as a two-way connection to a source of truth stored elsewhere.
- Apple Observation migration guide: starting with iOS 17, SwiftUI supports `@Observable`; Apple recommends `State` and `Environment` for observable models instead of object-specific wrappers when fully adopting Observation.
- Apple `StateObject` / `EnvironmentObject`: still valid for `ObservableObject` code and incremental migration.
- Apple `URLSession` docs define asynchronous network transfers that return data
  and `URLResponse` or throw errors.
- Apple Swift `Result` docs model success and failure as typed associated values.
- GitHub API check on 2026-06-01: `Swinject/Swinject` had the highest stars among checked Swift DI containers; `Factory`, `swift-dependencies`, and `Needle` were active alternatives.
- Library docs: Factory targets Swift/SwiftUI container DI and previews/tests; swift-dependencies is inspired by SwiftUI environment; Needle is compile-time safe; Swinject is a mature Swift DI container.

## Architecture Rule

- `presentation` owns SwiftUI views, UIKit view controllers, state holders, UI events, presentation models, mappers, and navigation effects.
- `domain` owns entities, use cases, repository protocols, and pure business rules.
- `data` or `infrastructure` implements repository protocols and API/storage/native adapters.
- Presentation code depends on domain use cases or ports, not concrete API clients, storage clients, or repository implementations.
- Platform APIs such as Keychain, CoreLocation, Photos, notifications, and analytics should be wrapped behind ports/adapters before reaching presentation state holders.

## Data and Error Boundary

- URLSession, decoding, transport failures, Keychain, database, cache, and native
  SDK details stay in `data` or `infrastructure`.
- Network/storage failure types are raw diagnostics until mapped by repository
  implementations or data mappers into domain error/result types.
- Use cases return domain result/error types and add only business-rule errors.
- SwiftUI views and UIKit view controllers receive presentation state, events,
  and `UiModel`/error UI models, not DTOs, URLSession responses, or raw storage
  errors.
- Presentation mappers convert domain models and domain errors into UI models
  before state reaches SwiftUI/UIKit.

## DI Rule

iOS has no built-in Hilt equivalent. Use this priority:

1. Prefer initializer injection for local dependencies.
2. Use a composition root such as `AppDependencies`, `SceneDependencies`, or feature builder/factory to wire concrete implementations once.
3. In SwiftUI, use `EnvironmentValues` / `@Environment` for app-level dependencies and feature dependencies that must flow through a view tree.
4. If using iOS 17+ Observation, prefer `@Observable` state holders with `@State` ownership and `@Environment` injection where it fits the tree.
5. Use `@StateObject`, `@ObservedObject`, and `@EnvironmentObject` only when the project still uses `ObservableObject` or needs incremental migration.
6. Use an external DI library only when direct composition and SwiftUI environment become too large or the repo already standardizes on a library.

Library selection:
- Existing repo standard wins.
- For new SwiftUI container-based DI, prefer `Factory` when a library is justified.
- For controllable live/test/preview dependencies, especially clients like date, UUID, API, storage, or feature flags, consider `swift-dependencies`.
- For mature general-purpose container DI or existing UIKit-heavy codebases, `Swinject` is acceptable.
- For large modular apps that need generated compile-time-safe dependency graphs, consider `Needle`.
- Do not introduce a DI library for a small feature when initializer injection plus composition root is enough.

## Direct DI Shape

Use direct DI before a container:

```swift
struct AppDependencies {
    var searchUseCase: SearchUseCase
}

extension EnvironmentValues {
    @Entry var appDependencies: AppDependencies = .live
}
```

If the project toolchain cannot use `@Entry`, use the older `EnvironmentKey` shape instead:

```swift
private struct AppDependenciesKey: EnvironmentKey {
    static let defaultValue: AppDependencies = .live
}

extension EnvironmentValues {
    var appDependencies: AppDependencies {
        get { self[AppDependenciesKey.self] }
        set { self[AppDependenciesKey.self] = newValue }
    }
}
```

At the composition root, create concrete dependencies once and inject them through initializers or environment values. Tests and previews should replace `AppDependencies` with test doubles.

## Swinject Rule

Use Swinject only when the repo already standardizes on it, direct composition has become too large, or UIKit/modular service registration needs a mature runtime container.

Registration shape:
- Group related registrations in Swinject `Assembly` types by feature, layer, or integration boundary.
- Build the `Assembler` at the app, scene, test, or feature composition root.
- Keep `Container` mutation inside composition roots and assemblies. Presentation code should receive concrete dependencies, factories, or a narrow `Resolver`/dependency hook, not the mutable container.
- Use Swinject registration arguments for runtime screen inputs such as route ids. Do not store navigation state inside the container.
- Prefer initializer injection inside resolved types. Use property, method, or `initCompleted` injection only for UIKit/storyboard integration or unavoidable circular dependencies.

Lifetime and tests:
- Choose object scopes deliberately: `.transient` for new instances, `.graph` for one resolution graph, `.container` for app-wide shared instances, `.hierarchy` for parent/child container sharing, and `.weak` only for weakly shared instances.
- Use child containers or alternate assemblies for tests, previews, and mock implementations.
- If resolutions can cross threads, resolve through `container.synchronize()` as a `Resolver`; direct `Container.resolve` is not thread safe.
- Treat circular dependencies as a design smell. If unavoidable, make one side property-based and wire it with `initCompleted`.

## Package Shape

Feature presentation packages should stay screen-oriented:
- `Features/<Feature>/Presentation/<Screen>/<Screen>View.swift`
- `Features/<Feature>/Presentation/<Screen>/<Screen>ViewModel.swift`
- `Features/<Feature>/Presentation/<Screen>/Model/<Screen>UiState.swift`
- `Features/<Feature>/Presentation/<Screen>/Model/<Screen>UiAction.swift`
- `Features/<Feature>/Presentation/<Screen>/Model/<Screen>UiEvent.swift`
- `Features/<Feature>/Presentation/<Screen>/Model/<Screen>UiModel.swift`
- `Features/<Feature>/Presentation/<Screen>/Mapper/*Mapper.swift`
- `Features/<Feature>/Presentation/<Screen>/Components/*`

Presentation mappers must convert domain data into presentation models before state reaches SwiftUI views or UIKit view controllers.
Presentation model types must use the `UiModel` postfix, for example `<Screen>ItemUiModel`.

## State Holder Rule

Use a screen-level state holder:
- SwiftUI iOS 17+: `@MainActor @Observable final class <Screen>ViewModel`
- SwiftUI incremental/older code: `@MainActor final class <Screen>ViewModel: ObservableObject`
- UIKit: `@MainActor final class <Screen>ViewModel` with explicit observation/binding used by the project

State patterns:
- expose explicit `UiState`, preferably an enum with associated data
- model `notReady`, `loading`, `refreshing`, `placeholder`, `empty`, `error`, `success`, `offline`, and `permissionRequired` cases when they can occur
- do not use fake domain sentinel values as initial UI state
- keep pagination cursors, selected ids, cancellation handles, optimistic updates, and retry state private in the state holder
- keep UI-only focus, scroll, animation, sheet, and text editing state local unless it drives domain work
- all UI state mutation should happen on `MainActor`
- use SwiftUI `@State` for view-local transient state, `Binding` for child write access to an existing source of truth, and `Environment` for shared observable dependencies when that is the project pattern

`UiState`, `UiAction`, and `UiEvent` roles:
- `UiState` is durable render data. It must be enough to redraw the screen from the state holder.
- `UiAction` is user or UI input flowing to the state holder. Use an enum when the screen has branchy behavior; direct methods are acceptable for simple screens but must map to explicit actions conceptually.
- `UiEvent` is a transient view/container effect such as navigation, toast/banner, haptic feedback, permission prompt, share sheet, focus, or analytics trigger.

Event patterns:
- treat navigation, toast/banner, haptic feedback, permission prompts, share sheets, and imperative focus as one-shot effects
- do not store fire-once effects as durable `UiState`
- prefer callback outputs, `AsyncStream<UiEvent>`, Combine publisher, or the repo's existing narrow event stream only when the screen truly needs one-shot effects

## View Rule

Split state-holder wiring from rendering:
- composition root or screen container creates/injects dependencies
- view owns or receives the state holder according to the project pattern
- rendering view receives plain `uiState` and callbacks where possible
- child views receive only the data/callbacks they need
- views should not construct repositories, API clients, storage clients, or DI containers
- SwiftUI previews must be able to inject fake dependencies or fixed `UiState`

## Review Checklist

- dependency flow uses initializer injection, composition root, or SwiftUI environment; external DI is justified or already present
- if Swinject is used, registrations are grouped in assemblies and the assembler/container is built at a composition root
- if Swinject is used, object scopes are explicit and container mutation does not leak into presentation
- native/platform APIs are wrapped before reaching the state holder
- `UiState` is an enum or equivalent explicit type and covers not-ready/loading/refreshing/placeholder/empty/error/success/offline/permission states that can occur
- `UiAction`, `UiEvent`, and `UiState` roles are explicit for branchy screens
- domain data is mapped to `UiModel` before rendering
- `UiModel` postfix is used for presentation models
- state holder owns async orchestration and exposes callbacks/events
- UI state mutation is `MainActor` safe
- views stay render-focused and receive plain state/callbacks
- one-shot effects are not modeled as durable UI state
- review output includes the required markers below

## Required Markers

When this skill is used for presentation development or code review, include these markers in the completion artifact or review output:

- `presentation-skill: ios`
- `presentation-state-based-development: applied|n/a`
- `presentation-state-review: pass|fail`
- `ui-state-modeling: explicit`
- `presentation-mapping-boundary: domain-to-uimodel|n/a`
- `di-boundary: swift-environment|factory|swift-dependencies|swinject|needle|direct|existing|n/a`

## Sources

- Apple SwiftUI user interface state docs
- Apple SwiftUI model data docs
- Apple SwiftUI State, Binding, Observation, and Environment docs
- Factory README
- swift-dependencies README
- Swinject README
- Swinject Assembler, object scope, container hierarchy, thread safety,
  injection pattern, and circular dependency docs
- Needle README
