---
name: react-native-app-shell-error-handling
description: Use when implementing or reviewing React Native app-wide error handling where screens notify common errors and App.tsx/AppShell owns NavigationContainer, root stack/tab navigation, global Modal, snackbar, toast hosts, auth flow switching, navigation reset, SessionExpired handling, and Maintenance handling.
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [app shell, appshell, global error, common error, session expired, navigation reset, modal host, toast host, 공통 에러, 전역 에러, 세션 만료]
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

## When To Use

- App-wide modal, snackbar, toast, banner, or maintenance UI work.
- `SessionExpired`, `Maintenance`, `Forbidden`, or server-wide business code
  handling.
- Root stack/tab auth flow switching.
- Navigation reset to login or maintenance roots.
- Review of screens/hooks/stores that handle global errors.

## AppShell Role

`App.tsx` or `AppShell` is the top-level React Native container:

- owns `NavigationContainer`;
- owns root stack, nested tabs, and auth-flow screen switching;
- owns app-wide `Modal`, snackbar, toast, and dialog hosts;
- observes `CommonErrorNotifier` or equivalent store;
- performs root navigation reset after common error confirmation;
- passes feature routes/screens only the callbacks or stores needed to notify
  errors.

Screens render screen UI only.

## Notifier Contract

Use a small common error notifier/store:

- `notify(error) == true`: AppShell handles the common error.
- `notify(error) == false`: screen handles local inline error state.
- expose pending common errors as state.
- consume errors by stable `id`.
- dedupe by stable key when repeated requests report the same common error.

Common error examples: `SessionExpired`, `Maintenance`, `Forbidden`, and server
codes such as `COMMON_*`.

## Development Checklist

- Keep global modal/snackbar/toast host at AppShell level.
- Show one current common error from the pending queue.
- Consume the common error before dispatching navigation reset.
- For `SessionExpired`, reset the root navigator to the login flow.
- For `Maintenance`, reset the root navigator to maintenance flow when present.
- Keep local validation and screen-only fetch failures in screen state when
  `notify(error)` returns `false`.
- Use React Navigation auth-flow state or root `CommonActions.reset` from
  AppShell-owned code.
- Preserve server `code`, `title`, `message`, and `requestId` through mappers into
  the common UI model when supplied.

## Review Checklist

Request changes when any of these are true:

- A screen directly shows global session/maintenance modal UI.
- A screen directly dispatches root reset for `SessionExpired`.
- A screen owns global toast/snackbar host instances.
- Multiple `NavigationContainer` trees are introduced without a clear isolated
  mini-app reason.
- `SessionExpired` can leave authenticated stack entries after confirm.
- Common and local error paths both render for the same failure.
- Error metadata is dropped before common UI can display it.

## Platform-Specific Forbidden Patterns

- Do not let screens use navigation reset for common errors directly.
- Do not scatter `Modal`/toast/snackbar host containers across screens for global
  errors.
- Do not nest independent `NavigationContainer` instances for ordinary feature
  screens.
- Do not use a render crash boundary as the main API/domain common error handler.
- Do not use an HTTP/interceptor layer to display React Native UI.

## Tests To Expect

- Notifier classifies common versus local errors.
- Notifier dedupes and consumes pending errors.
- AppShell renders the first pending common error in the global host.
- `SessionExpired` confirmation resets to login flow.
- Screen tests prove common errors call `notify` instead of rendering local error
  state.

## Completion Gate

When this skill applies, record:

- `project-local-skills: checked`
- `project-local-skills-used: react-native-app-shell-error-handling`
