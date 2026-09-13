---
name: ios-clean-presentation-architecture
description: Use when creating, modifying, or reviewing an iOS Clean Architecture presentation layer with SwiftUI/UIKit state holders, explicit UiState, UiModel mapping, dependency injection, and state-based presentation code review.
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [uistate, ui state, state holder, observableobject, swiftui view state, screen state, presentation layer]
pathGlobs: ["**/*ViewModel.swift", "**/*UiState.swift", "**/Presentation/**"]
requires: [clean-architecture-core, ios-clean-architecture]
---

# iOS Clean Presentation Architecture

Use this skill for iOS feature work where SwiftUI or UIKit presentation code should follow a reusable Clean Architecture pattern.

For AppShell-owned global error hosts, queue acknowledgement, or root navigation reset, use `ios-app-shell-error-handling` instead.

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
- Library docs: Factory targets Swift/SwiftUI container DI and previews/tests; swift-dependencies is inspired by SwiftUI environment; Needle is compile-time safe; Swinject is a mature Swift DI container.

## Architecture Rule

Apply the required `clean-architecture-core` **Semantic Layers**, **Dependency
Rule**, **Mapping Boundary**, and **Error Boundary**. Apply the required
`ios-clean-architecture` for platform composition and DI.

- Platform APIs such as Keychain, CoreLocation, Photos, notifications, and analytics should be wrapped behind ports/adapters before reaching presentation state holders.

## Data and Error Boundary

- URLSession, decoding, transport failures, Keychain, database, cache, and native
  SDK details stay in `data` or `infrastructure`.


Apply the required core's **Error Boundary** and **Mapping Boundary** for
normalization and the values supplied to rendering.

## DI Rule

iOS has no built-in Hilt equivalent. Use this priority:

1. Prefer initializer injection for local dependencies.
2. Use the adopted app, scene, or feature composition owner to construct concrete implementations with explicit shared or local lifetimes.
3. In SwiftUI, use `EnvironmentValues` / `@Environment` for app-level dependencies and feature dependencies that must flow through a view tree.
4. If using iOS 17+ Observation, prefer `@Observable` state holders with `@State` ownership and `@Environment` injection where it fits the tree.
5. Use `@StateObject`, `@ObservedObject`, and `@EnvironmentObject` only when the project still uses `ObservableObject` or needs incremental migration.
6. Use an external DI library only when direct composition and SwiftUI environment become too large or the repo already standardizes on a library.

Library selection:
- Existing repo standard wins.
- For justified SwiftUI container-based DI, consider `Factory` if its scope and test/preview substitution model fits the project.
- For controllable live/test/preview dependencies, especially clients like date, UUID, API, storage, or feature flags, consider `swift-dependencies`.
- For mature general-purpose container DI or existing UIKit-heavy codebases, `Swinject` is acceptable.
- For large modular apps that need generated compile-time-safe dependency graphs, consider `Needle`.
- Check the selected library's supported Swift/toolchain versions, lifetime semantics, and compile-time or runtime resolution guarantees; popularity alone does not establish suitability.
- Do not introduce a DI library for a small feature when initializer injection plus composition root is enough.

## Direct DI Shape

Prefer direct composition when a container adds no needed capability. The
composition owner constructs dependencies for their declared app, scene, or
feature lifetime and passes typed dependencies through initializers or SwiftUI
environment values. Shared collaborators are reused within that owner, not
automatically made app-wide singletons.

Use `@Entry` for custom environment values when the project toolchain supports
it; otherwise use `EnvironmentKey` and `EnvironmentValues`. Choose defaults
according to the dependency contract rather than assuming a live implementation
is safe. Tests and previews must be able to supply replacements without reaching
production services.

## Swinject Rule

Use Swinject only when the repo already standardizes on it, direct composition has become too large, or UIKit/modular service registration needs a mature runtime container.

Registration shape:
- Group related registrations in Swinject `Assembly` types by feature, layer, or integration boundary.
- Build the `Assembler` at the app, scene, test, or feature composition root.
- Keep `Container` mutation inside composition roots and assemblies. Presentation code should receive concrete dependencies, factories, or a narrow `Resolver`/dependency hook, not the mutable container.
- Use Swinject registration arguments for runtime screen inputs such as route ids. Do not store navigation state inside the container.
- Prefer initializer injection inside resolved types. Use property, method, or `initCompleted` injection only for UIKit/storyboard integration or unavoidable circular dependencies.

Lifetime and tests:
- Choose [Swinject object scopes](https://github.com/Swinject/Swinject/blob/master/Documentation/ObjectScopes.md) deliberately: `.transient` creates new instances; `.graph` shares within one resolution graph; `.container` shares within the registering container and its children; `.weak` shares only while strong references remain. A container need not be app-wide. Use a custom scope only when the project actually defines its storage and reset contract; `.hierarchy` is not a built-in scope.
- Use child containers or alternate assemblies for tests, previews, and mock implementations.
- If resolutions can cross threads, resolve through `container.synchronize()` as a `Resolver`; direct `Container.resolve` is not thread safe.
- Treat circular dependencies as a design smell. If unavoidable, make one side property-based and wire it with `initCompleted`.

## Presentation Boundaries

Discover the project's adopted screen/state-holder, UI-value, mapping, and
component boundaries and actual architecture role mappings. Preserve existing
packages and names rather than generating a fixed folder tree.

Apply the required core's **Mapping Boundary** to presentation projections.
`UiModel` describes a responsibility, not a suffix whose absence fails review.

## State Holder Rule

Use a screen-level state holder with the project's adopted naming:
- SwiftUI iOS 17+: use `@Observable` when adopting Observation, with UI state isolated to `MainActor`
- SwiftUI incremental/older code: preserve `ObservableObject` and the corresponding ownership wrappers, with UI state isolated to `MainActor`
- UIKit: use the project's explicit observation/binding mechanism and `MainActor`-safe UI state

State patterns:
- expose explicit `UiState`, preferably an enum with associated data
- model `notReady`, `loading`, `refreshing`, `placeholder`, `empty`, `error`, `success`, `offline`, and `permissionRequired` cases when they can occur
- do not use fake domain sentinel values as initial UI state
- keep cancellation handles and rollback bookkeeping private; expose rendered
  selection, optimistic results, and retry status through observable state
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
- presentation projections satisfy the required core's **Mapping Boundary**.
- presentation values follow adopted naming conventions; suffix or file count
  alone does not fail the state or architecture contract
- state holder owns async orchestration and exposes callbacks/events
- UI state mutation is `MainActor` safe
- views stay render-focused and receive plain state/callbacks
- one-shot effects are not modeled as durable UI state
- review output includes the required markers below

## Required Markers

When this skill is used for presentation development or code review, write every marker below in the phase artifact or review output. The active workflow `required_markers` is the allowed-value source of truth:

- `presentation-skill: android|flutter|react|react-native|ios|n/a`
- `presentation-state-based-development: applied|n/a`
- `presentation-state-review: pass|fail|n/a`
- `ui-state-modeling: explicit|n/a`
- `presentation-mapping-boundary: domain-to-uimodel|n/a`
- `di-boundary: hilt|context-provider|tsyringe|swift-environment|factory|swift-dependencies|swinject|needle|riverpod|get-it|direct|existing|n/a`

Apply these iOS-specific decisions:

- `presentation-skill`: use `ios` when iOS presentation code is in scope. Use `n/a` only when the phase has no presentation work.
- `presentation-state-based-development`: use `applied` when presentation code was created or changed under this contract. Use `n/a` for review-only work or when no presentation code changed.
- `presentation-state-review`: use `pass` when every applicable checklist item passes, `fail` when any applicable item fails, and `n/a` only when no iOS presentation code is in scope.
- `ui-state-modeling`: use `explicit` when the screen's durable states are modeled explicitly. Use `n/a` only when no screen state is in scope.
- `presentation-mapping-boundary`: use `domain-to-uimodel` for domain/application
  values projected to the UI contract, including safe identity projection;
  no separate mapper file is required. Use `n/a` only when no such boundary exists.
- `di-boundary`: use `swift-environment`, `factory`, `swift-dependencies`, `swinject`, `needle`, `direct`, or `existing` for the verified iOS composition path. Use `n/a` only when the change neither creates nor reviews dependency wiring.

A `fail` result is actionable: record the failed criterion and return to the workflow's fix path before approval.

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
