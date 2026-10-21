# Run start
- Run: `full-feature/20261020-205937`
- Task: Implement the Store in docs/source-prd.md: all five screens and the header, every Spec Item (SPEC-1..SPEC-9) and the exact data-testid contract, reading fixtures/catalog.json
- Profile: `nextjs`; architecture mode `stack` (`react-fsd-architecture`). Note: the runner header prints `architecture: default`.
- Branch: `feat/implement-the-store-…-catalog.json` off `main@cfabf98`.
- Host: claude controller (hooks registered but not active in this session — `hook_unproven`); test/build runs use `agent-flow run-command -- <argv>`.
- Reviewer providers: `claude-session` available; `codex-session` binary present but `codex login status` = Not logged in.
- Constraints: `docs/source-prd.md` is final (constants, texts, test ids); only `fixtures/catalog.json` as data; no remote — stop at `commit`.
- Gates: architecture-lint (n/a in stack), test (`npm test`), lint (`npm run lint`, optional), build (`npm run build`, pre-push).
