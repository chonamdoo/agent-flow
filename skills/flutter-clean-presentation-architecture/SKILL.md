---
name: flutter-clean-presentation-architecture
description: Use when creating, modifying, or reviewing a Flutter Clean Architecture presentation layer with Riverpod provider DI, state-holder notifiers, UiState modeling, UiModel mapping, navigation effects, and state-based presentation code review.
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [uistate, ui state, state holder, screen state, navigation effect, riverpod, asyncvalue, notifier, 상태 홀더, 화면 상태, 프레젠테이션 계층]
pathGlobs: ["**/*_ui_state.dart", "**/*_notifier.dart", "**/presentation/**"]
requires: [clean-architecture-core]
---

# Flutter Clean Presentation Architecture

Use this skill for Flutter feature work where presentation code should follow a reusable Clean Architecture pattern.

For widget-level layout, constraint, adaptive sizing, disposal, child-widget keys, `BuildContext` use across an async gap, and test mechanics, use `flutter-development-guide`.

## Evidence Basis

- Flutter official state management docs separate ephemeral widget state from app state that outlives a single widget.
- Flutter official app architecture guide places a view model between the view and the domain/data layers and keeps the view free of data-source detail.
- Riverpod resolves dependencies through `ref` rather than `BuildContext`, so a state holder reads its dependencies without a widget in the tree.
- Riverpod providers are top-level declarations, so referencing a provider that does not exist fails the analyzer instead of throwing at lookup time the way a `BuildContext`-scoped lookup does.
- Riverpod `AsyncValue` models loading, data, and error as one value, which removes the contradictory-boolean shape that separate `isLoading`/`error`/`data` fields allow.
- Flutter `ChangeNotifier`, `ValueNotifier`, and `Listenable` are the framework-native observable state primitives when the repo does not use Riverpod.

## Architecture Rule

- `presentation` owns screens, widgets, state-holder notifiers, UI events, presentation models, mappers, and navigation effects.
- `domain` owns entities, use cases, repository interfaces, and pure business rules.
- `data` implements repository interfaces and HTTP/storage/plugin adapters.
- Presentation code depends on domain use cases or ports, not on `http`/`dio` clients, plugin classes, or repository implementations.
- Flutter plugin and platform-channel APIs should be wrapped behind ports/adapters before reaching presentation state holders.
- Permissions, deep links, secure storage, sensors, and `MethodChannel` calls should be represented as application/domain ports before presentation uses them.

## Data and Error Boundary

- `http`/`dio` clients, `shared_preferences`, secure storage, `sqflite`/`drift`, plugins, permissions, and platform SDK detail stay in `data` or in platform adapters.
- `DioException`, `SocketException`, `PlatformException`, and storage failures are raw diagnostics until a repository implementation or data mapper turns them into domain error/result types.
- Use cases return domain result/error types and add only business-rule errors.
- Screens and widgets receive presentation state, callbacks, and `UiModel`/error UI models, not DTOs, `Response` objects, raw `PlatformException`, or database rows.
- Presentation mappers convert domain models and domain errors into UI models before state reaches widgets.
- Keep domain-to-UI derivation and error mapping in the mapper or notifier, not in `build`.

## DI Rule

Flutter has no framework-bundled DI container. Use this priority:

1. Prefer constructor parameters for a dependency a single widget or notifier owns.
2. Use Riverpod providers for app-level dependencies such as use cases, repositories, HTTP clients, storage, permissions, analytics, feature flags, and configuration.
3. Use `get_it` only when the project already registers services there.
4. Read a dependency through `ref`, and reserve `BuildContext` for `Theme`, `MediaQuery`, localization, navigation, and dialogs.

Provider rules — `flutter-clean-architecture` owns where `ProviderScope` sits and how an override replaces a dependency:
- declare providers as top-level `final` variables, not inside `build`; a provider constructed per rebuild leaks its state
- expose a use case or repository through its own provider so a test can override that one edge
- let a screen-scoped provider's state be destroyed when the screen stops listening, using whichever auto-dispose form the repo's Riverpod version provides
- keep screen-local state out of app-level providers

## Package Shape

Feature presentation directories should stay screen-oriented:
- `lib/features/<feature>/presentation/<screen>/<screen>_screen.dart`
- `lib/features/<feature>/presentation/<screen>/<screen>_notifier.dart`
- `lib/features/<feature>/presentation/<screen>/model/<screen>_ui_state.dart`
- `lib/features/<feature>/presentation/<screen>/model/<screen>_ui_action.dart`
- `lib/features/<feature>/presentation/<screen>/model/<screen>_ui_event.dart`
- `lib/features/<feature>/presentation/<screen>/model/<screen>_ui_model.dart`
- `lib/features/<feature>/presentation/<screen>/mapper/<screen>_mapper.dart`
- `lib/features/<feature>/presentation/<screen>/widgets/*`

Presentation mappers must convert domain data into presentation models before state reaches widgets.
Presentation model types must use the `UiModel` postfix, for example `<Screen>ItemUiModel`.

## State Holder Rule

Use a Riverpod notifier as the screen state holder:
- name it `<Screen>Notifier` with a `<screen>NotifierProvider`
- read use cases and dependencies through `ref`, not through `BuildContext`
- expose one `UiState` value
- expose user actions as named methods
- keep async orchestration, pagination, refresh, retry, and navigation-effect decisions inside the notifier
- keep rendering inside widgets
- use `ChangeNotifier` or `ValueNotifier` with the repo's existing wiring when the project does not use Riverpod

State patterns:
- model `not-ready`, `loading`, `refreshing`, `placeholder`, `empty`, `error`, `success`, `offline`, and `permission-required` states explicitly when they can occur
- define `UiState` as a sealed class hierarchy or an `AsyncValue`, and switch over it exhaustively instead of combining booleans that can contradict each other
- keep initial UI state out of fake domain sentinel values
- keep request ids, `CancelToken`s, pagination cursors, selected ids, and optimistic update state private in the notifier
- cancel in-flight work with the repo's existing cancellation pattern when a newer request supersedes it
- keep keyboard, scroll, focus, and animation state in the widget unless it drives business work

`UiState`, `UiAction`, and `UiEvent` roles:
- `UiState` is durable render data. It must be enough to redraw the screen after the widget is rebuilt or the route is restored.
- `UiAction` is user or UI input. Use a sealed class when the screen is branchy; otherwise named notifier methods are acceptable but must map to explicit actions conceptually.
- `UiEvent` is a transient screen effect such as navigation, snackbar, haptic feedback, permission prompt, deep link, focus, or analytics trigger.

Event patterns:
- treat navigation, snackbar, dialog, bottom sheet, haptic feedback, permission prompts, and imperative focus as one-shot effects
- perform them on the widget side, where a `BuildContext` is legitimately available, after listening to the notifier
- keep fire-once effects out of durable `UiState`

## Widget Rule

Split state-holder wiring from rendering:
- a `ConsumerWidget` or `ConsumerStatefulWidget` screen watches the notifier provider and passes plain values down
- child widgets receive only the data and callbacks they need
- presentational widgets should not read providers, use cases, repositories, HTTP clients, or plugins
- `switch` over the sealed `UiState` or `AsyncValue` exhaustively so every declared state has a rendered branch
- platform-specific UI branches stay in presentation, while platform API calls stay behind adapters

## Review Checklist

- dependencies reach the notifier through `ref` or constructor parameters, and `get_it` is used only where the repo already registers services
- plugin and platform-channel APIs are wrapped before reaching the state holder
- permissions, deep links, storage, sensors, and custom channels are accessed through ports/adapters
- `UiState` is a sealed hierarchy or `AsyncValue` and covers not-ready, loading, refreshing, placeholder, empty, error, success, offline, and permission states that can occur
- `UiState` has no contradictory booleans or duplicated derived fields
- `UiAction`, `UiEvent`, and `UiState` roles are explicit for branchy screens
- domain data is mapped to `UiModel` before rendering
- `UiModel` postfix is used for presentation models
- the notifier owns async orchestration and exposes named action methods
- widgets stay render-focused and receive plain values
- providers are declared as top-level `final` variables rather than constructed inside `build`
- a screen-scoped provider's state is destroyed when the screen stops listening
- screen-local state is absent from app-level providers
- a superseded in-flight request is cancelled through the repo's existing cancellation pattern
- `BuildContext` is reserved for framework lookups and effects
- one-shot effects are not modeled as durable UI state
- `ProviderScope` stays the single composition root and test overrides replace one edge
- review output includes the required markers below

## Required Markers

When this skill is used for presentation development or code review, write every marker below in the phase artifact or review output. The active workflow `required_markers` is the allowed-value source of truth:

- `presentation-skill: android|flutter|react|react-native|ios|n/a`
- `presentation-state-based-development: applied|n/a`
- `presentation-state-review: pass|fail|n/a`
- `ui-state-modeling: explicit|n/a`
- `presentation-mapping-boundary: domain-to-uimodel|n/a`
- `di-boundary: hilt|context-provider|tsyringe|swift-environment|factory|swift-dependencies|swinject|needle|riverpod|get-it|direct|existing|n/a`

Apply these Flutter-specific decisions:

- `presentation-skill`: use `flutter` when Flutter presentation code is in scope. Use `n/a` only when the phase has no presentation work.
- `presentation-state-based-development`: use `applied` when presentation code was created or changed under this contract. Use `n/a` for review-only work or when no presentation code changed.
- `presentation-state-review`: use `pass` when every applicable checklist item passes, `fail` when any applicable item fails, and `n/a` only when no Flutter presentation code is in scope.
- `ui-state-modeling`: use `explicit` when the screen's durable states are modeled explicitly. Use `n/a` only when no screen state is in scope.
- `presentation-mapping-boundary`: use `domain-to-uimodel` when domain data crosses into presentation through a mapper. Use `n/a` only when no such data crosses the boundary.
- `di-boundary`: use `riverpod`, `get-it`, `direct`, or `existing` for the verified Flutter composition path. Use `n/a` only when the change neither creates nor reviews dependency wiring.

A `fail` result is actionable: record the failed criterion and return to the workflow's fix path before approval.

## Sources

- Flutter state management docs
- Flutter app architecture guide and architecture case study
- Flutter accessibility and internationalization docs
- Riverpod provider, `AsyncValue`, `ProviderScope`, and testing docs
- get_it README
- Dart language docs: sealed classes and exhaustive switch
