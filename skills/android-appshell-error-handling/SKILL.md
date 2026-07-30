---
name: android-appshell-error-handling
description: Defines Android app-shell common-error handling where feature ViewModels notify common errors and AppShell owns global UI, root navigation, Navigation3 back stack resets, SessionExpired handling, and Retrofit CallAdapter error mapping. Use when implementing or reviewing Android Kotlin Compose app-wide error handling.
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [app shell, appshell, global error, common error, error mapping, session expired, snackbar host, dialog host, 공통 에러, 전역 에러, 세션 만료]
---

# Android AppShell Error Handling

Use this with `code-generation-discipline` for Android app-wide error handling
implementation or review.

## Quick start

1. Use this for common errors whose UI or side effects must be owned above feature screens: `SessionExpired`, `Maintenance`, `Forbidden`, and server-wide business codes.
2. Start by deciding whether `notify(error)` returns `true`; if it does, route the error to AppShell and keep feature `UiState` limited to local errors.
3. If the task is ordinary screen `UiState`, ViewModel DI, `UiModel` mapping, or stateless Compose rendering without global hosts/root reset, use `android-clean-presentation-architecture` instead.


## When To Use

- App-wide common error dialog, snackbar, toast, or maintenance UI work.
- `SessionExpired`, `Maintenance`, `Forbidden`, or server-wide business code
  handling.
- Navi3 root back stack ownership, root flow reset, or login redirect work.
- Retrofit CallAdapter, mapper, ViewModel, or AppShell common error review.

## Do not use for

- Feature-local validation or fetch errors that should render inline as screen `UiState`.
- General ViewModel, Compose screen, DI, or presentation mapper design without AppShell-owned global error UI/root navigation; use `android-clean-presentation-architecture`.


## Core Contract

Keep app-wide error UI and root navigation above feature screens:

- `Activity` applies the theme and calls `AppShell` or the app composable.
- `AppShell` owns the Navi3 root back stack, top-level navigation, and full-screen
  `NavDisplay`.
- `AppShell` hosts app-wide `CommonErrorDialogHost`, `SnackbarHost`, `ToastHost`,
  and common app dialogs.
- Feature routes and screens render local UI only.

Feature ViewModels notify common errors. They do not own `NavController`,
`Context`, dialogs, toasts, snackbar hosts, or login-flow stack resets.

## Development Checklist

- Use a notifier contract where `notify(error) == true` means AppShell handles a
  common error, and `notify(error) == false` means the feature emits local
  `UiState.Error`.
- Treat `SessionExpired`, `Maintenance`, `Forbidden`, and server-wide business
  codes such as `COMMON_*` as common errors unless the product flow says
  otherwise.
- Observe pending common errors from AppShell with lifecycle-aware Compose state.
- Show the first pending common error, consume it on confirmation, then run the
  app-level side effect.
- For `SessionExpired`, clear the root back stack and add the login route after
  the user confirms.
- For maintenance mode, clear the root back stack and add the maintenance route
  after confirmation when that flow exists.
- Use Navi3 for post-confirm navigation only. Do not put common error dialogs in
  the Navi3 back stack.
- Preserve server `code`, `title`, `message`, and `requestId` through the mapper
  chain into the common UI model when the server supplies them.

## Boundary Checklist

- Retrofit CallAdapters may convert API failures into `AppFailure(AppError)`.
- Data mappers convert network/server failures into `AppError`; they do not create
  Compose UI models.
- Presentation mappers convert `AppError` into feature-local `ErrorUiModel` or
  common `CommonErrorUiModel`.
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
- `notify(error)` can double-show the same error, lose an error, or leave consumed
  errors in the pending queue.
- Common and local errors are mixed so the same failure can become both a common
  dialog and a feature `UiState.Error`.
- Server error metadata is dropped before the UI can display it.

## Tests To Expect

- `CommonErrorNotifier` classifies common versus local errors, deduplicates by a
  stable key, and removes errors after `consume`.
- `CommonErrorDialogHost` renders the first pending error and consumes it on
  confirm.
- `SessionExpired` confirmation clears the root back stack and adds the login
  route.
- ViewModel tests prove common errors notify AppShell instead of becoming feature
  `UiState.Error`.
- Mapper and CallAdapter tests cover non-2xx responses, connectivity failures,
  serialization failures, canceled calls, and metadata preservation.

## Completion Gate

When this skill applies, record it as:

- `project-local-skills: checked`
- `project-local-skills-used: android-appshell-error-handling`
