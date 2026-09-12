---
name: react-native-app-shell-error-handling
description: Use when implementing or reviewing React Native app-wide error handling where screens notify common errors and the app or framework root owns global Modal, snackbar, toast hosts, auth-flow switching, navigation recovery, SessionExpired handling, and Maintenance handling. Covers plain React Navigation and framework-managed roots such as Expo Router.
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [app shell, appshell, global error, common error, session expired, navigation reset, modal host, toast host]
pathGlobs: ["**/App.tsx", "**/*AppShell*.tsx", "**/*CommonError*Host.tsx"]
requires: [app-shell-error-contract]
---

# React Native App Shell Error Handling

Use this with `code-generation-discipline` for React Native common error handling
implementation or review.

## Official Basis

- React Native `Modal` presents content above an enclosing view.
- React Navigation `NavigationContainer` owns navigation state and links the
  top-level navigator to the app environment.
- React Navigation authentication flow examples switch available screens from
  top-level auth state.
- React Navigation `CommonActions.reset` replaces navigation state.
- [Expo Router manages the root `NavigationContainer`](https://docs.expo.dev/router/migrate/from-react-navigation/#replace-the-navigationcontainer); use the installed framework version's public navigation APIs rather than adding another container.

## When To Use

- App-wide modal, snackbar, toast, banner, or maintenance UI work.
- `SessionExpired`, `Maintenance`, `Forbidden`, or server-wide business code
  handling.
- Root stack/tab auth flow switching.
- Navigation reset to login or maintenance roots.
- Review of screens/hooks/stores that handle global errors.

## Do not use for

- Feature-local validation or fetch errors that render inline.
- Ordinary screen/hook state or UI-model mapping; use `react-native-clean-presentation-architecture` instead.

## AppShell Role

The app's adopted top-level composition or framework root owns AppShell responsibilities:

- owns `NavigationContainer` in plain React Navigation, or integrates with the framework-owned root in Expo Router and other managed routers;
- owns root stack, nested tabs, and auth-flow screen switching;
- owns app-wide `Modal`, snackbar, toast, and dialog hosts;
- observes `CommonErrorNotifier` or equivalent store;
- performs root navigation reset after common error confirmation;
- passes feature routes/screens only the callbacks or stores needed to notify
  errors.

For common errors, screens emit intents rather than owning global UI or root recovery. Feature-local errors remain in screen state.

## Shared Error Contract

Read [`app-shell-error-contract`](../app-shell-error-contract/SKILL.md) before the platform rules below. It is the source of truth for classification, queue identity, acknowledgement, retry, and metadata preservation.

## Development Checklist

- Keep the global modal/snackbar/toast host at AppShell level.
- Run root navigation recovery through the shared queue and acknowledgement contract.
- For `SessionExpired`, reset the root navigator to the login flow.
- For `Maintenance`, reset the root navigator to the maintenance flow when present.
- Identify the installed router and version. In plain React Navigation, use auth-flow state or root `CommonActions.reset` from AppShell-owned code. For Expo or another framework-managed root, use its supported auth/recovery mechanism and import boundary without creating a second `NavigationContainer`.

## Review Checklist

Request changes when any of these are true:

- A screen directly shows global session/maintenance modal UI.
- A screen directly dispatches root reset for `SessionExpired`.
- A screen owns global toast/snackbar host instances.
- Multiple `NavigationContainer` trees are introduced without a clear isolated
  mini-app reason.
- `SessionExpired` can leave authenticated stack entries after successful confirmation.
- The shared classification, deduplication, acknowledgement, retry, or metadata contract is violated.

## Platform-Specific Forbidden Patterns

- Do not let screens use navigation reset for common errors directly.
- Do not scatter `Modal`/toast/snackbar host containers across screens for global
  errors.
- Do not nest independent `NavigationContainer` instances for ordinary feature
  screens.
- Do not use a render crash boundary as the main API/domain common error handler.
- Do not use an HTTP/interceptor layer to display React Native UI.

## Tests To Expect

- A common error appears once in the global host without duplicate local rendering; a feature-local error remains local.
- Successful session recovery removes authenticated routes and switches to login, including when back navigation is attempted.
- Maintenance recovery reaches the configured maintenance root. Failed or cancelled recovery remains retryable according to the shared contract.
- Exercise the adopted router's observable recovery behavior rather than asserting a particular notifier method name.

## Completion Gate

Use only the markers supplied by the active phase.
