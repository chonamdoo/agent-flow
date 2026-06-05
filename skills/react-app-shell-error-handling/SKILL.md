---
name: react-app-shell-error-handling
description: Use when implementing or reviewing React Web app-wide error handling where feature components notify common errors and an AppShell, root layout, root route, or client provider layer owns global dialogs, snackbars, toasts, auth flow switching, router resets, SessionExpired handling, Maintenance handling, React Router layout/error boundaries, or Next.js App Router layout/error boundaries.
---

# React App Shell Error Handling

Use this with `code-generation-discipline` for React Web common error handling
implementation or review.

## Official Basis

- React Error Boundaries catch render crashes and show fallback UI.
- React Router root/layout routes can own shared UI and nested route rendering.
- React Router route `errorElement`/`ErrorBoundary` handles route render, loader,
  and action exceptions.
- Next.js App Router root layouts own shared UI; Client Components own client
  context providers.
- Next.js `error.tsx` and `global-error.tsx` handle uncaught render/runtime
  failures, not expected API/domain common errors.

## When To Use

- App-wide error dialog, snackbar, toast, banner, or maintenance modal work.
- `SessionExpired`, `Maintenance`, `Forbidden`, or server-wide business code
  handling.
- Auth flow switching between anonymous, login, onboarding, and authenticated
  roots.
- React Router root/layout route navigation resets.
- Next.js App Router root layout/provider boundary decisions.
- Review of feature components, hooks, or stores that handle global errors.

## AppShell Role

`AppShell` is the top-level React container above feature routes:

- owns global providers and common UI hosts;
- observes `CommonErrorNotifier` or equivalent store;
- renders common dialogs, snackbars, toasts, and app banners;
- owns auth/session root flow switching;
- performs root navigation replacement/reset after common error confirmation;
- contains React Router `RouterProvider`/root route layout, or Next.js root
  layout plus a client provider boundary for interactive state.

Feature routes render feature UI only.

## Notifier Contract

Use a small common error notifier/store:

- `notify(error) == true`: AppShell handles the common error.
- `notify(error) == false`: feature handles local inline error state.
- expose pending common errors as state.
- consume errors by stable `id`.
- dedupe by stable key when the same server/common error can repeat.

Common error examples: `SessionExpired`, `Maintenance`, `Forbidden`, and server
codes such as `COMMON_*`.

## Development Checklist

- Keep global error state in AppShell/provider scope, not feature component
  local state.
- Show one current common error from the pending queue.
- Consume the common error before running confirmation side effects.
- For `SessionExpired`, replace/reset to the login route after confirmation.
- For `Maintenance`, replace/reset to maintenance flow after confirmation.
- Keep local validation and local fetch failures inside the feature when
  `notify(error)` returns `false`.
- In React Router, put global hosts in the root/layout route, not leaf routes.
- In Next.js App Router, put interactive stores/providers in a Client Component
  imported by `layout.tsx`; avoid turning the whole root document into a client
  component.
- Preserve server `code`, `title`, `message`, and `requestId` through mappers into
  the common UI model when supplied.

## Review Checklist

Request changes when any of these are true:

- A feature directly shows global session/maintenance dialogs.
- A feature directly resets root navigation or redirects to login for common
  errors.
- A feature owns the global snackbar/toast host.
- `SessionExpired` can leave authenticated route state in history after confirm.
- Common and local error paths both render for the same failure.
- Error metadata is dropped before common UI can display it.
- React Router root/layout responsibilities are duplicated in leaf routes.
- Next.js `error.tsx` or `global-error.tsx` is used as the primary API/domain
  common error channel.

## Platform-Specific Forbidden Patterns

- Do not use React Error Boundaries as the main API/domain common error handler.
- Do not throw expected API/domain errors just to reach `error.tsx`,
  `global-error.tsx`, or a React Router error boundary.
- Do not put global common error stores inside feature-only providers.
- Do not call router reset/replace for session expiry from leaf components except
  through an AppShell-owned callback/store effect.
- Do not duplicate toast/snackbar containers in feature routes.

## Tests To Expect

- Notifier classifies common versus local errors.
- Notifier dedupes and consumes pending errors.
- AppShell renders the first pending common error.
- `SessionExpired` confirmation resets/replaces to login.
- Feature tests prove common errors call `notify` instead of rendering local
  `UiState.Error`.

## Completion Gate

When this skill applies, record:

- `project-local-skills: checked`
- `project-local-skills-used: react-app-shell-error-handling`
