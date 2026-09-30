---
name: rn-io-conventions
description: Team I/O convention for React Native feature code. Use when writing or reviewing any module under src/features/ that reads remote data.
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
pathGlobs: ["src/features/**/*.ts", "src/features/**/*.tsx"]
taskTerms: [fetch, remote data, http, api call]
---

# Remote I/O Modules

Our team isolates every network touchpoint of a feature in one file so that
offline mocks and request logging have a single seam.

- Inside `src/features/<feature>/`, only a module whose file name ends in
  `.remote.ts` (for example `orders.remote.ts`) may import
  `src/shared/http.ts` or call `fetch`.
- Each feature that reads remote data has exactly that `<feature>.remote.ts`
  module; state holders, hooks, and screens import its exported functions and
  never import `src/shared/http.ts` themselves.
- Payload types that mirror the wire format stay in the `.remote.ts` module.

Review: flag any non-`.remote.ts` file under `src/features/` that imports
`shared/http` or calls `fetch`.
