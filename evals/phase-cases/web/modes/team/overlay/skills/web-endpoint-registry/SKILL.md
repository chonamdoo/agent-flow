---
name: web-endpoint-registry
description: Team convention for backend endpoint paths in this Next.js app. Apply whenever TypeScript code calls, wraps, or references a backend HTTP endpoint.
pathGlobs: ["src/**/*.ts", "src/**/*.tsx", "app/**/*.ts", "app/**/*.tsx"]
---

# Endpoint registry

The platform team rotates API prefixes during migrations, so endpoint paths are owned in one place.

- Every backend endpoint path lives in `src/shared/api/endpoints.ts` as an exported `const` whose
  name starts with `EP_` followed by UPPER_SNAKE_CASE, for example
  `export const EP_ORDERS = "/api/orders";`.
- No other module may contain a string literal (quoted or template) that begins with `/api/`.
  Import the `EP_` constant instead, including in route handlers and feature `api` segments.
- Tests may use the literal path when asserting what was requested.

A change that writes an `/api/...` literal outside `src/shared/api/endpoints.ts`, or names an endpoint
constant without the `EP_` prefix, does not follow this convention.
