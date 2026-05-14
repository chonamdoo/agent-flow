---
name: typescript-development-guide
description: TypeScript-specific implementation and review checklist. Use only when writing, modifying, or reviewing TypeScript or TSX files (`*.ts`, `*.tsx`) or TypeScript project configuration. Apply as a secondary guide after repo patterns and task scope; do not use it to demand broad rewrites.
---

# TypeScript Development Guide

Use this only for TypeScript/TSX code in the changed scope. Do not score it.

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
