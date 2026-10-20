# Worktree

- Path: `/root/.agent-flow/worktrees/store-2ebbfa61be15/feat-implement-the-store-in-docs-source-prd.md-all-five-screens-and-the-header-every-spec-item-spec-1-spec-9-and-the-exact-data-testid-contract-reading-fixtures-catalog.json`
- Branch: `feat/implement-the-store-in-docs-source-prd.md-all-five-screens-and-the-header-every-spec-item-spec-1-spec-9-and-the-exact-data-testid-contract-reading-fixtures-catalog.json`
- Base: `main` at `cfabf98` (leader `/root/store`, trunk strategy, base/integration `main` per profile).
- Not on main/master/develop: yes.
- Naming check: prefix `feat/` and kebab-case match `profile.branching.naming`; **the slug is ~170 characters, over
  `max_slug_length: 60`**, and keeps `.md`/`.json` dots from the task text. Recorded, not changed (renaming the bound
  branch would break the CLI binding; the phase forbids re-creating the worktree).
- Setup: `.agent-flow/project.yaml` copied by the CLI. `node_modules` was absent (profile `link_node_modules` is off),
  so `npm ci` was run in the worktree through `agent-flow run-command -- npm ci` (exit 0). Working tree clean.
