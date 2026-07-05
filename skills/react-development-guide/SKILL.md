---
name: react-development-guide
description: React Web and Next.js implementation and review checklist for TSX components, hooks, state, effects, rendering, accessibility, server/client boundaries, hydration, and list rendering. Use when writing, modifying, or reviewing React Web or Next.js code; do not use for React Native-only code, TypeScript language generalities, CSS taste, or broad rewrites.
---

# React Development Guide

Use this as a secondary checklist after user request, repo instructions, existing repo patterns, and `code-generation-discipline`. Do not score it. Do not use best-practice generalities to force broad rewrites.

## Quick start

1. Confirm the changed scope is React Web or Next.js, not React Native-only or generic TypeScript.
2. Read the nearest existing component, hook, route, and framework boundary pattern before applying checklist advice.
3. Preserve the user-visible loading, empty, error, and success branches touched by the change.
4. Run only the targeted typecheck, test, lint, or route smoke check that covers the edited path.

## Scope

- Include React Web, Next.js, TSX components, hooks, state, effects, rendering, accessibility, and list rendering.
- Exclude React Native-only issues, TypeScript language generalities, and CSS taste.
- Keep Next.js checks limited to React rendering boundaries; do not turn this into a Next.js-only guide.

## Write

- Keep component responsibility narrow and aligned with existing repo patterns.
- Follow Rules of Hooks: call hooks only at the top level of components or custom hooks, never inside conditions, loops, callbacks, async functions, or after early returns.
- Minimize effects. Use effects for external systems, not for derivable render state.
- Avoid derived state when a value can be computed from props/state during render.
- Preserve loading, empty, error, and success states when touching async or user-visible flows.
- Keep server/client component boundaries explicit. Do not move browser-only logic into server components.
- Avoid hydration mismatch sources such as nondeterministic render output, browser-only values during server render, and inconsistent server/client markup.
- Use stable list keys from domain IDs. Do not use array indexes when reorder, insert, delete, or filtering can happen.
- Avoid rerender work only when there is a real changed path or measured risk. Do not add memoization by default.

## Test

- Run the repo's typecheck/lint/tests when component props, hooks, imports, or rendering branches change.
- For UI behavior, verify the changed flow plus loading, empty, error, and success states when relevant.
- For Next.js server/client changes, verify no hydration or boundary errors in the touched route.
- For accessibility-sensitive UI, verify labels, roles, keyboard/focus behavior, and semantic markup where relevant.

## Review

- Blocking only: hook rule violation, stale closure or effect loop, hydration/server-client boundary break, broken user flow, accessibility regression, clear performance regression, test/type/lint failure, or project-rule violation.
- Best practice 일반론으로 blocking하지 말 것.
- Do not block on theoretical cleaner architecture, preferred folder layout, CSS taste, or optional memoization.
- Treat generic React best practice comments as suggestions unless they prevent a concrete failure.

## Sources

- React docs: Rules of Hooks, `useEffect`, memoization.
- Next.js docs: Server and Client Components.
- Repo configuration and existing component patterns override generic advice.
