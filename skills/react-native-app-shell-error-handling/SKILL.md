---
name: react-native-app-shell-error-handling
description: Defines React Native app-shell common-error handling where screens notify common errors and App.tsx/AppShell owns NavigationContainer, root stack/tab navigation, global Modal/snackbar/toast hosts, auth flow switching, navigation reset, SessionExpired handling, and Maintenance handling. Use when implementing or reviewing React Native app-wide error handling.
---

# React Native App Shell Error Handling

Use this with `code-generation-discipline` for React Native common error handling
implementation or review.

## Quick start

1. Use this for common errors whose UI or side effects must be owned above feature screens: `SessionExpired`, `Maintenance`, `Forbidden`, and server-wide business codes.
2. Start by deciding whether `notify(error)` returns `true`; if it does, route the error to AppShell and keep screen `uiState` limited to local errors.
3. If the task is ordinary screen `uiState`, state-holder hook DI, `UiModel` mapping, or render-focused screens without global hosts/navigation reset, use `react-native-clean-presentation-architecture` instead.


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

## Do not use for

- Screen-local validation or fetch errors that should render inline as screen `uiState`.
- General state-holder hook, screen, DI, native adapter, or presentation mapper design without AppShell-owned global error UI/root navigation; use `react-native-clean-presentation-architecture`.


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
