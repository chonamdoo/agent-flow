---
name: react-development-guide
description: React Web and Next.js implementation and review checklist. Use only when writing, modifying, or reviewing React Web, Next.js, TSX components, hooks, state, effects, rendering, accessibility, server/client boundaries, hydration, or list rendering. Do not use for React Native-only issues, TypeScript language generalities, CSS taste, or broad rewrites.
---

# React Development Guide

Use this as a secondary checklist after user request, repo instructions, existing repo patterns, and `code-generation-discipline`. Do not score it. Do not use best-practice generalities to force broad rewrites.

## Scope

- Include React Web, Next.js, TSX components, hooks, state, effects, rendering, accessibility, and list rendering.
- Exclude React Native-only issues, TypeScript language generalities, and CSS taste.
- Keep Next.js checks limited to React rendering boundaries; do not turn this into a Next.js-only guide.

## Write

- Keep component responsibility narrow and aligned with existing repo patterns.
- Follow Rules of Hooks: call hooks only at the top level of components or custom hooks, never inside conditions, loops, callbacks, async functions, or after early returns. The installed React version's supported `use` API has separate rules: conditional/loop calls are allowed inside a component or hook, but reading a promise with `use` must not be wrapped in `try/catch`. This exception does not relax ordinary Hook rules.
- Minimize effects. Use effects for external systems, not for derivable render state.
- When the installed React and hooks-lint versions support `useEffectEvent`, use it only for genuinely non-reactive events inside Effects, including calls from another local Effect Event. Keep dependencies needed for resynchronization; exclude the Effect Event itself from the dependency array. It is not a general callback for render, click handlers, or passing to another component/hook.
- Avoid derived state when a value can be computed from props/state during render.
- Preserve loading, empty, error, and success states when touching async or user-visible flows.
- Keep server/client component boundaries explicit. Do not move browser-only logic into server components.
- Server HTML and the first client hydration render must use the same snapshot. Avoid nondeterministic output and inconsistent markup; a Client Component can participate in SSR, so a lazy initializer reading browser storage or locale is not automatically safe. Pass request-known initial values consistently from server to client. For browser-only differences, use a supported client-only boundary or update after that boundary hydrates, accounting for extra rendering and visible changes. Do not hide every screen until mount or move all derivation into Effects; a parent's Effect does not prove every streaming descendant has hydrated.
- Render the shell from data a plain request can obtain. A first-visit or crawler request that misses build-warmed state must still receive the shell.
- Use stable domain IDs for ordinary lists; do not use array indexes when reorder, insert, delete, or filtering can happen. Library-managed identity takes precedence for its own rows: RHF `useFieldArray` uses generated `field.id` (or the configured key property supported by the installed version), not the row's database ID. This does not change valid domain-list keys.
- Avoid rerender work only when there is a real changed path or measured risk. Do not add memoization by default. Before relying on React Compiler, inspect installed React/Compiler versions, the actual build configuration, compilation/skip coverage for the changed path, and third-party API compatibility. Avoid redundant manual memoization on compiled paths; compatible manual memoization remains valid for uncompiled/skipped paths or measured bottlenecks. Memoization is not a correctness or state-lifetime guarantee, and Compiler installation alone does not justify bulk deletion of existing memoization. For RHF compatibility, use the subscription reference in `react-hook-form-zod`.

## Conditional Capabilities

- RHF form draft, subscriptions, validation, initialization, field adapters, or submit changes: read `react-hook-form-zod`; generic TSX or an unrelated `watch`, `reset`, or `Controller` is not enough.
- TanStack Form draft, subscriptions, validation, initialization, field composition, arrays, wizards, submit, or SSR integration changes: read `react-tanstack-form` and check installed versions; generic TSX, TanStack Query alone, or RHF-only work is not enough.
- Public React Web URL indexing, metadata, canonical, JSON-LD, bot responses, or SEO-related cache changes: read `react-web-seo`; internal authenticated UI does not need SEO work by default.
- Existing React Web Storybook stories/configuration, meaningful state or interaction reproduction, or explicit adoption design: read `react-storybook`; ordinary component work does not require introducing Storybook.

## Test

- Run the repo's typecheck/lint/tests when component props, hooks, imports, or rendering branches change.
- For UI behavior, verify the changed flow plus loading, empty, error, and success states when relevant.
- For Next.js server/client changes, verify no hydration or boundary errors in the touched route.
- For accessibility-sensitive UI, verify labels, roles, keyboard/focus behavior, and semantic markup where relevant.

## Review

- Blocking only: hook rule violation, stale closure or effect loop, hydration/server-client boundary break, broken user flow, accessibility regression, clear performance regression, test/type/lint failure, or project-rule violation.

## Sources

- React [Rules of Hooks](https://react.dev/reference/rules/rules-of-hooks), [`use`](https://react.dev/reference/react/use), [`useEffectEvent`](https://react.dev/reference/react/useEffectEvent), and [`hydrateRoot`](https://react.dev/reference/react-dom/client/hydrateRoot).
- React [Compiler setup and coverage](https://react.dev/learn/react-compiler/installation), [incompatible libraries](https://react.dev/reference/eslint-plugin-react-hooks/lints/incompatible-library), and [`memo`](https://react.dev/reference/react/memo).
- Next.js docs: Server and Client Components.
- Repo configuration and existing component patterns override generic advice.
