# Change under QA
Refactored price formatting into `src/price.mjs`. This is a refactor: what shoppers see must not change.
Acceptance: product prices still render exactly as before, e.g. 5766 cents renders as `$57.66`.
The project's e2e command is `npm run e2e`; `npm run e2e:update` rewrites snapshots.
