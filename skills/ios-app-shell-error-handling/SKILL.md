---
name: ios-app-shell-error-handling
description: Use when implementing or reviewing iOS app-wide error handling where SwiftUI views/ViewModels notify common errors and an @main App, WindowGroup AppShell, NavigationStack coordinator, or UIKit AppCoordinator owns root navigation, tab/session flow switching, alerts, sheets, toast hosts, SessionExpired handling, Maintenance handling, and root reset behavior.
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [app shell, appshell, global error, common error, session expired, alert host, toast host, root reset, 공통 에러, 전역 에러, 세션 만료]
---

# iOS App Shell Error Handling

Use this with `code-generation-discipline` for iOS common error handling
implementation or review. Default to SwiftUI. Use UIKit Coordinator guidance only
when the app already uses UIKit navigation.

## Official Basis

- SwiftUI apps define the entry point with `App` and declare scenes.
- `WindowGroup` contains the view hierarchy presented by the app window.
- `NavigationStack(path:)` exposes navigation state that the app can control.
- UIKit `UINavigationController` manages stack-based hierarchical navigation.
- UIKit `UIAlertController` presents alert-style UI from a presenter.

## When To Use

- App-wide alert, sheet, toast, banner, or maintenance UI work.
- `SessionExpired`, `Maintenance`, `Forbidden`, or server-wide business code
  handling.
- SwiftUI `NavigationStack(path:)`, `TabView`, or root session flow switching.
- UIKit root coordinator, tab coordinator, or root reset work.
- Review of Views/ViewModels that handle global errors.

## AppShell Role

SwiftUI structure:

- `@main App` creates app-level services and opens `WindowGroup`.
- `WindowGroup` hosts `AppShell`.
- `AppShell` owns root session state, `NavigationStack(path:)`, `TabView`, and
  global alert/sheet/snackbar/toast hosts.
- `AppShell` observes `CommonErrorNotifier` or equivalent store.
- `AppShell` performs path clearing and root flow switching after common error
  confirmation.

UIKit structure:

- `AppCoordinator` or root coordinator owns `UIWindow`, root
  `UINavigationController`, tab controller, alert presenter, and session reset.
- Feature coordinators may request intents, but root coordinator performs common
  session flow changes.

Feature views and ViewModels render feature UI and notify error intents only.

## Notifier Contract

Use a small common error notifier/store:

- `notify(error) == true`: AppShell or root coordinator handles the common error.
- `notify(error) == false`: feature handles local inline error state.
- expose pending common errors as observable state.
- consume errors by stable `id`.
- dedupe by stable key when repeated requests report the same common error.

Common error examples: `SessionExpired`, `Maintenance`, `Forbidden`, and server
codes such as `COMMON_*`.

## Development Checklist

- Keep global alert/sheet/snackbar/toast host in AppShell or root coordinator.
- Show one current common error from the pending queue.
- Consume the common error before changing root flow.
- For `SessionExpired`, clear `NavigationStack` path and switch to login flow
  after confirmation.
- For `Maintenance`, clear path and switch to maintenance flow when present.
- Keep local validation and feature-only fetch failures in feature state when
  `notify(error)` returns `false`.
- Preserve server `code`, `title`, `message`, and `requestId` through mappers into
  the common UI model when supplied.
- Keep root flow mutation on the main actor.

## Review Checklist

Request changes when any of these are true:

- A feature ViewModel mutates `NavigationPath`, `AppRoute` path, root session
  flow, or coordinator root reset directly.
- A feature ViewModel owns SwiftUI `Alert`, `Sheet`, UIKit `UIAlertController`,
  snackbar host, or toast host for common errors.
- A feature view presents session-expired or maintenance UI instead of AppShell.
- `SessionExpired` can leave authenticated routes in `NavigationStack` path or
  UIKit navigation stack after confirmation.
- Common and local error paths both render for the same failure.
- Error metadata is dropped before common UI can display it.
- UIKit feature coordinators present global session alerts instead of delegating
  to the root coordinator.

## Platform-Specific Forbidden Patterns

- Do not let feature ViewModels own root `NavigationPath` mutation for common
  errors.
- Do not put global common error alerts in leaf SwiftUI views.
- Do not present global `UIAlertController` from arbitrary feature view
  controllers.
- Do not use crash handling or render fallback as the main API/domain common
  error handler.
- Do not let networking/interceptor code present UI or reset root navigation.

## Tests To Expect

- Notifier classifies common versus local errors.
- Notifier dedupes and consumes pending errors.
- AppShell/root coordinator presents the first pending common error.
- `SessionExpired` confirmation clears navigation state and switches to login.
- ViewModel tests prove common errors notify instead of mutating root navigation
  or presenting alerts.

## Completion Gate

When this skill applies, record:

- `project-local-skills: checked`
- `project-local-skills-used: ios-app-shell-error-handling`
