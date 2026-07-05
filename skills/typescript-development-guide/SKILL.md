---
name: typescript-development-guide
description: TypeScript implementation and review checklist for type-safe changes in `.ts`, `.tsx`, and TypeScript project configuration. Use when writing, modifying, or reviewing TypeScript source, TSX source, `tsconfig`, or typed tooling config; do not use for React/RN framework behavior, Python, Android native code, or broad architecture rewrites by itself.
---

# TypeScript Development Guide

Use this only for TypeScript/TSX code in the changed scope. Do not score it.

## Quick start

1. Confirm the changed scope includes TypeScript source or TypeScript-specific configuration.
2. Read `tsconfig`, lint rules, and nearby domain/framework patterns before changing types.
3. Preserve strictness: avoid `any`, unsafe casts, weakened null handling, and unawaited promises unless the boundary requires it.
4. Run only the targeted typecheck, test, or lint command that covers the edited path.

## Scope

- Include TypeScript types, async typing, imports/exports, TSX type safety, `tsconfig`, and typed tooling configuration.
- Exclude React Web behavior covered by `react-development-guide`, React Native behavior covered by `react-native-development-guide`, Python, Android native code, and broad architecture rewrites.

## Write

- Preserve the repo's existing framework, component, state, and API patterns first.
- Prefer `unknown`, discriminated unions, generics, or explicit domain types over `any`.
- Do not weaken strictness or silence type errors unless the boundary requires it and the reason is documented.
- Handle `null` and `undefined` explicitly when the type allows them.
- Keep async flows typed and awaited. Do not leave floating promises unless the repo has an explicit fire-and-forget pattern.
- For React/Next.js, preserve server/client component boundaries and existing data-fetching conventions.

## Test

- Add or update focused tests for changed logic, user-visible behavior, and bug regressions.
- Run the repo's typecheck and lint commands when TypeScript signatures or imports change.
- For UI changes, verify loading, empty, error, and success states when the changed path touches them.

## Review

- Treat these as blocking only for real runtime bugs, unsafe type holes, broken UI behavior, security issues, failing checks, or project-rule violations.
- Treat style-only differences as suggestions unless they conflict with configured lint/format tools.
- Check `any`, unsafe casts, missing null handling, unawaited promises, incorrect React hook dependencies, and server/client boundary mistakes.

## Sources

- TypeScript Handbook: `strict`, `noImplicitAny`, and `strictNullChecks` reduce common bug classes.
- typescript-eslint recommended type-checked rules: useful for unsafe `any` and promise issues when the repo uses ESLint.
- Existing repo configuration (`tsconfig.json`, ESLint, framework config) overrides generic advice.
