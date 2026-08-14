---
name: react-app-shell-error-handling
description: Use when implementing or reviewing React Web app-wide error handling where feature components notify common errors and an AppShell, root layout, root route, or client provider layer owns global dialogs, snackbars, toasts, auth flow switching, router resets, SessionExpired handling, Maintenance handling, React Router layout/error boundaries, or Next.js App Router layout/error boundaries.
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [app shell, appshell, global error, common error, session expired, error boundary, snackbar, toast host, router reset, 공통 에러, 전역 에러, 세션 만료]
pathGlobs: ["**/*AppShell*.tsx", "**/app/layout.tsx", "**/*CommonError*Provider.tsx", "**/*CommonError*Host.tsx"]
requires: [app-shell-error-contract]
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

## Do not use for

- Feature-local validation or fetch errors that render inline.
- Ordinary component/hook state or UI-model mapping; use `react-clean-presentation-architecture` instead.

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

## Shared Error Contract

Read [`app-shell-error-contract`](../app-shell-error-contract/SKILL.md) before the platform rules below. It is the source of truth for classification, queue identity, acknowledgement, retry, and metadata preservation.

## Development Checklist

- Keep global error state in AppShell/provider scope, not feature component local state.
- Run AppShell recovery effects through the shared queue and acknowledgement contract.
- For `SessionExpired`, replace the current browser history entry with the login route. An auth guard must block protected history entries reached through Back; browser APIs cannot erase the user's prior history.
- For `Maintenance`, replace the current entry with the maintenance flow and guard routes that cannot run during maintenance.
- In React Router, put global hosts in the root/layout route, not leaf routes.
- In Next.js App Router, put interactive stores/providers in a Client Component imported by `layout.tsx`; keep the root document server-rendered unless another requirement needs a client boundary.

## Review Checklist

Request changes when any of these are true:

- A feature directly shows global session/maintenance dialogs.
- A feature directly resets root navigation or redirects to login for common
  errors.
- A feature owns the global snackbar/toast host.
- Back navigation reaches a protected route without the auth guard rejecting or redirecting it.
- The shared classification, deduplication, acknowledgement, retry, or metadata contract is violated.
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

- AppShell renders common errors and applies the router/auth recovery behavior.
- Feature tests prove common errors call `notify` instead of rendering local
  `UiState.Error`.

## Completion Gate

Use only the markers supplied by the active phase.
