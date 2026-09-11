# AGENTS.md Update Guidelines

## Core Principle

Every line in `AGENTS.md` enters future agent context. Keep only information that saves real rediscovery or prevents recurring mistakes.

## What To Add

### 1. Commands/Workflows Discovered

Add commands future sessions need and cannot infer safely. Resolve them from the repo's declared scripts and workflow, including working directory and prerequisites. Reading a command does not authorize executing it; distinguish declaration checks from actual execution evidence.

### 2. Gotchas and Non-Obvious Patterns

Add recurring repo-specific constraints supported by evidence. If tests share mutable state, record the required isolation or execution policy. For generated files, identify the actual source and generator rather than copying an example output directory.

### 3. Package Relationships

Add relationships not obvious from filenames: contract producers and consumers, dependency direction, and required initialization order. Record only relationships established by the repository, not an assumed application structure.

### 4. Testing Approaches That Worked

Add verification patterns with observed execution evidence. Locate helpers and setup through existing tests, and explain the observable contract or environmental constraint they support. Label unexecuted checks as unverified and retain the assigned verification owner.

### 5. Configuration Quirks

Add recurring environment behavior only when its cause, applicability, and supported configuration are established. Distinguish build-time and runtime inputs where that affects correctness. Do not copy an incident-specific connection workaround into permanent guidance without evidence that it remains necessary.

## What Not To Add

### 1. Obvious Code Info

Do not add statements that merely repeat a class or file name's meaning.

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

Ask for approval before applying instruction changes, as required by the entrypoint.
