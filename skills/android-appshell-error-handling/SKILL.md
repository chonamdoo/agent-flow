---
name: android-appshell-error-handling
description: Defines Android app-shell common-error handling where feature ViewModels notify common errors and AppShell owns global UI, root navigation, Navigation3 back stack resets, SessionExpired handling, and Retrofit CallAdapter error mapping. Use when implementing or reviewing Android Kotlin Compose app-wide error handling.
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [app shell, appshell, global error, common error, error mapping, session expired, snackbar host, dialog host]
pathGlobs: ["**/*AppShell*.kt", "**/*CommonError*Host.kt"]
requires: [app-shell-error-contract]
---

# Android AppShell Error Handling

Use this with `code-generation-discipline` for Android app-wide error handling
implementation or review.

## Quick start

1. Use this for common errors whose UI or side effects must be owned above feature screens: `SessionExpired`, `Maintenance`, `Forbidden`, and server-wide business codes.
2. Apply the required shared contract below before the Android-specific rules.
3. If the task is ordinary screen `UiState`, ViewModel DI, `UiModel` mapping, or stateless Compose rendering without global hosts/root reset, use the selected contract's presentation guidance (`android-clean-presentation-architecture` in Clean mode) instead.


## When To Use

- App-wide common error dialog, snackbar, toast, or maintenance UI work.
- `SessionExpired`, `Maintenance`, `Forbidden`, or server-wide business code
  handling.
- Navi3 root back stack ownership, root flow reset, or login redirect work.
- Retrofit CallAdapter, mapper, ViewModel, or AppShell common error review.

## Do not use for

- Feature-local validation or fetch errors that should render inline as screen `UiState`.
- General ViewModel, Compose screen, DI, or presentation mapper design without AppShell-owned global error UI/root navigation; use the selected contract's presentation guidance (`android-clean-presentation-architecture` in Clean mode).


## Shared Error Contract

Read [`app-shell-error-contract`](../app-shell-error-contract/SKILL.md) before the platform rules below. It is the source of truth for classification, queue identity, acknowledgement, retry, and metadata preservation.

## Core Contract

Keep app-wide error UI and root navigation above feature screens:

- `Activity` applies the theme and calls `AppShell` or the app composable.
- `AppShell` owns the Navi3 root back stack, top-level navigation, and full-screen
  `NavDisplay`.
- `AppShell` hosts app-wide common error dialogs, snackbars, toasts, and other
  shared app dialogs.
- Feature routes and screens render local UI only.

Feature ViewModels notify common errors. They do not own `NavController`,
`Context`, dialogs, toasts, snackbar hosts, or login-flow stack resets.


## Development Checklist

- Observe pending common errors from AppShell with lifecycle-aware Compose state.
- For `SessionExpired`, clear the root back stack and add the login route after
  the user confirms.
- For maintenance mode, clear the root back stack and add the maintenance route
  after confirmation when that flow exists.
- Use Navi3 for post-confirm navigation only. Do not put common error dialogs in
  the Navi3 back stack.

## Boundary Checklist

- Retrofit CallAdapters may normalize API failures into the project's established
  failure representation.
- Remote data sources and repositories stay free of dialog, navigation, toast,
  snackbar, and Android UI concerns.
- Do not add a `BaseViewModel` solely to centralize common error display.
- Do not use an OkHttp Interceptor as a UI or navigation boundary.

## Platform-Specific Forbidden Patterns

- Do not let feature ViewModels own Navi3 root back stack mutation for common
  errors.
- Do not put global common error dialogs in feature screens.
- Do not put common error dialog entries in the Navi3 back stack.
- Do not display Android UI from Repository, RemoteDataSource, Retrofit
  CallAdapter, or OkHttp Interceptor code.

## Review Checklist

Request changes when any of these are true:

- A feature ViewModel stores or calls `NavController`, `Context`, Android dialog,
  toast, snackbar host, or root login navigation for common errors.
- Feature screens host session-expired or server-wide common dialogs instead of
  AppShell.
- Navi3 routes are used as common error dialog entries.
- `SessionExpired` can be confirmed without clearing the root back stack to the
  login flow.

## Tests To Expect

- The common-error UI displays the queued failure, accepts confirmation, and drives Android AppShell recovery.
- `SessionExpired` confirmation clears the root back stack and adds the login
  route.
- Apply the shared feature-classification test contract in ViewModel tests.
- Mapper and CallAdapter tests cover non-2xx responses, connectivity failures,
  serialization failures, canceled calls, and metadata preservation.

