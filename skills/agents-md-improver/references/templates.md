# AGENTS.md Templates

Use these sections only when they fit the repo. Do not force every section into every project.

## Key Principles

- Keep root `AGENTS.md` short and repo-specific.
- Put long details in `.Codex/rules/` when the project already imports rules from there.
- Prefer commands, paths, gotchas, and verification steps over prose.
- Avoid duplicating global preferences from `~/.codex/AGENTS.md`.

## Recommended Sections

### Commands

```markdown
## Commands
- `npm install` - install dependencies
- `npm run dev` - start local dev server
- `npm run build` - production build
- `npm run lint` - lint source files
- `npm test` - run tests
```

### Architecture

```markdown
## Architecture
- `apps/web` - Next.js App Router frontend
- `packages/api` - shared API contracts and server handlers
- `packages/ui` - shared UI primitives
```

### Key Files

```markdown
## Key Files
- `src/app/layout.tsx` - app shell
- `src/lib/supabase.ts` - Supabase client setup
```

### Code Style

```markdown
## Code Style
- Follow existing component and hook patterns before adding new abstractions.
- Do not edit generated files in `src/generated/`.
```

### Environment

```markdown
## Environment
- Copy `.env.example` to `.env.local` for local development.
- Required services: Postgres, Redis.
```

### Testing

```markdown
## Testing
- Unit tests: `npm test`
- Typecheck: `npm run typecheck`
- E2E: `npm run test:e2e`
```

### Gotchas

```markdown
## Gotchas
- Worktrees are created by `agent-flow worktree create`; the default location is `~/.agent-flow/worktrees/<repo-id>/`.
- Keep generated agent artifacts ignored unless the user asks to commit them.
```

### Workflow

```markdown
## Workflow
- Before implementation, read the nearest `AGENTS.md`.
- After implementation, run the project verification loop from this file.
```
