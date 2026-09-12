# AGENTS.md Templates

Use these sections only when they fit the repo. Do not force every section into every project.

## Key Principles

- Keep root `AGENTS.md` short and repo-specific.
- Put long details in `.Codex/rules/` when the project already imports rules from there.
- Prefer commands, paths, gotchas, and verification steps over prose.
- Avoid duplicating global preferences from `~/.codex/AGENTS.md`.

## Recommended Sections

### Commands

Discover exact commands from project manifests, scripts, and workflow configuration. Record the task each command performs, its working directory, required environment, and applicable execution permissions. Distinguish declared syntax from commands actually run with observed results. A documented install or deploy command is not authorization to execute it during an audit.

### Architecture

Record only repository-backed relationships: which modules consume which contracts, where state or policy is owned, and where dependency direction matters. Use the actual project paths rather than importing an example application tree.

### Key Files

Name actual entry points or configuration sources only when their role is not obvious and future agents need them to navigate. Explain the decision or behavior each governs; do not copy an unrelated app shell or client setup.

### Code Style

Capture non-obvious local conventions after inspecting the relevant code and instructions. If generated files must not be edited, identify their declared generator and the source that should change instead.

### Environment

Discover required services, configuration inputs, and setup sources from the repo. Record prerequisites and safe setup instructions without embedding credentials or assuming a particular database, cache, or environment-file naming convention.

### Testing

Record the project's actual verification commands, relevant test scope, prerequisites, and execution owner. Distinguish observed results from checks deferred to another phase or owner; do not invent passing evidence.

### Gotchas

Keep recurring constraints with their evidence and applicability. In an Agent Flow lifecycle, the active runner owns worktree creation and provides the actual worktree and next command; do not prescribe a separate worktree command or fixed location. Preserve the project's policy for keeping generated runtime artifacts private and ignored unless publication is authorized.

### Workflow

Record which instructions apply before work, who owns lifecycle actions, and which verification evidence is required before handoff. Follow the active workflow rather than creating a second lifecycle from a template. Apply instruction-file edits only after the approval required by the entrypoint.
