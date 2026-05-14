---
name: agents-md-improver
description: Audit and improve AGENTS.md files for Codex project memory. Use when the user asks to check, audit, update, optimize, shrink, deduplicate, or fix AGENTS.md, project instructions, repo memory, context bloat, context optimization, or agent rule quality. Finds AGENTS.md files, scores them for commands/workflows, architecture clarity, non-obvious patterns, conciseness, currency, and actionability, reports issues first, then makes targeted updates only after the user approves.
---

# AGENTS.md Improver

Audit and improve Codex `AGENTS.md` files so future sessions get concise, current, actionable project context.

## Workflow

### Phase 1: Discover

Find relevant instruction files without scanning generated folders:

```bash
rg --files -g 'AGENTS.md' -g '!node_modules' -g '!build' -g '!dist' -g '!coverage' -g '!vendor' -g '!.git'
```

Include `~/.codex/AGENTS.md` only when the task explicitly concerns global Codex behavior.

Common locations:

| Location | Purpose |
| --- | --- |
| `./AGENTS.md` | Primary project context checked into the repo |
| nested `AGENTS.md` | Package, app, or module-specific rules |
| `~/.codex/AGENTS.md` | User-wide Codex defaults |
| `.Codex/rules/` | Larger imported rules or compressed docs when the project uses that pattern |

### Phase 2: Assess Quality

Read `references/quality-criteria.md` when scoring or producing an audit report.

Score each file on:

| Criterion | Weight |
| --- | --- |
| Commands and workflows | 20 |
| Architecture clarity | 20 |
| Non-obvious patterns | 15 |
| Conciseness | 15 |
| Currency | 15 |
| Actionability | 15 |

Flag stale commands, broken paths, duplicated instructions, generic advice, outdated architecture, TODOs that are not useful to future agents, and long content that should move to `.Codex/rules/`.

### Phase 3: Report Before Editing

Always show the quality report before making edits.

Use this shape:

```markdown
## AGENTS.md Quality Report

### Summary
- Files found: X
- Average score: X/100
- Files needing update: X

### File-by-File Assessment

#### 1. ./AGENTS.md
**Score: XX/100 (Grade: X)**

| Criterion | Score | Notes |
| --- | --- | --- |
| Commands/workflows | X/20 | ... |
| Architecture clarity | X/20 | ... |
| Non-obvious patterns | X/15 | ... |
| Conciseness | X/15 | ... |
| Currency | X/15 | ... |
| Actionability | X/15 | ... |

**Issues:**
- ...

**Recommended updates:**
- ...
```

### Phase 4: Propose Targeted Updates

Read `references/update-guidelines.md` before proposing changes.

Only add information that will help future Codex sessions:

- commands or workflows discovered during analysis
- gotchas or non-obvious patterns found in the repo
- package relationships not obvious from filenames
- testing or verification approaches that actually work
- configuration quirks that would otherwise be rediscovered

Avoid generic best practices, one-off fixes, verbose explanations, and statements that merely restate file or class names.

Show a small diff for each proposed change and ask for approval before editing.

### Phase 5: Apply

After approval, edit the smallest relevant `AGENTS.md` file. Preserve existing structure, keep project files under 200 lines when feasible, and move longer material into `.Codex/rules/` with an import if the project already uses that pattern.

Use `references/templates.md` only when a file is missing major sections or needs a clean structure.
