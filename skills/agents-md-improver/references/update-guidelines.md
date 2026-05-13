# AGENTS.md Update Guidelines

## Core Principle

Every line in `AGENTS.md` enters future agent context. Keep only information that saves real rediscovery or prevents recurring mistakes.

## What To Add

### 1. Commands/Workflows Discovered

Add commands that future sessions need and cannot infer safely:

```markdown
## Commands
- `npm run build` - production build
- `npm run lint` - lint changed TypeScript and TSX files
```

### 2. Gotchas and Non-Obvious Patterns

Add repo-specific traps:

```markdown
## Gotchas
- Tests touching shared DB state must run sequentially.
- Generated files under `src/generated/` are not edited manually.
```

### 3. Package Relationships

Add relationships that are not obvious from filenames:

```markdown
## Architecture
- `apps/web` consumes API types generated from `packages/api`.
- Auth middleware must load before feature route handlers.
```

### 4. Testing Approaches That Worked

Add verification patterns that future agents should reuse:

```markdown
## Testing
- API tests use `supertest` with helpers from `tests/setup.ts`.
- UI tests mock image loading through `tests/mocks/image.ts`.
```

### 5. Configuration Quirks

Add environment behavior that commonly breaks work:

```markdown
## Config
- `NEXT_PUBLIC_*` values must exist at build time.
- Local Redis needs the IPv6-compatible connection suffix used in `.env.example`.
```

## What Not To Add

### 1. Obvious Code Info

Do not add statements that names already express, such as "`UserService` handles users."

### 2. Generic Best Practices

Do not add universal advice such as "write tests" or "use clear variable names."

### 3. One-Off Fixes

Do not add resolved incident details unless the pattern is likely to recur.

### 4. Verbose Explanations

Prefer one actionable line over background prose. Link or import longer docs from `.Codex/rules/` when the project uses that structure.

## Diff Format

For each proposed update, show:

````markdown
### Update: ./AGENTS.md

**Why:** one-line reason

```diff
+ concise addition
```
````

Ask for approval before applying non-trivial instruction changes.
