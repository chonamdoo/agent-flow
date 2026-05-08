---
name: graphify
description: |
  Use when the user asks to build, query, inspect, or explain a repository
  knowledge graph with Graphify. Graphify is installed by default during
  agent-flow install unless --without-graphify is passed.
---

# Graphify

Graphify builds a queryable knowledge graph for code, docs, papers, and
diagrams. Use it for broad codebase orientation, architecture exploration,
cross-file relationship questions, and "why is this connected to that" style
analysis.

## Install

Agent-flow installs Graphify during project bootstrap:

```bash
npx github:chonamdoo/agent-flow install
```

If the `graphify` CLI is already installed, bootstrap reuses it instead of
reinstalling the package for every project.

The bootstrap also runs `graphify .` once from the project root to create the
initial repository graph.

The bootstrap keeps a single shared global skill at
`~/.agents/skills/graphify` and removes duplicate host-specific copies from
`~/.gemini/skills/graphify` and `~/.claude/skills/graphify`.

If Graphify cannot be installed or the initial graph cannot be generated, the
default bootstrap fails instead of continuing with a partial setup.

Skip Graphify when needed:

```bash
npx github:chonamdoo/agent-flow install --without-graphify
```

Manual equivalent:

```bash
uv tool install graphifyy
graphify install
```

The PyPI package is `graphifyy`; the CLI command is `graphify`.

## Use

In Claude/Gemini chat from a project rooted workspace:

```bash
/graphify .
/graphify query "where is authentication handled?"
/graphify path "ViewModel" "Repository"
/graphify explain "core billing flow"
```

Graphify writes outputs under `graphify-out/`, including `graph.html`,
`GRAPH_REPORT.md`, and `graph.json`.

Codex uses `$graphify` instead of `/graphify`:

```text
$graphify .
$graphify query "where is authentication handled?"
```

The terminal CLI form also works:

```bash
graphify .
graphify query "where is authentication handled?"
```

## Agent Guidance

- Prefer Graphify for repository-wide relationship questions.
- Prefer normal code search for exact symbol or file lookup.
- `graphify-out/graph.html`, `graphify-out/GRAPH_REPORT.md`, and
  `graphify-out/graph.json` may be committed when the team wants a shared
  codebase map.
- Do not commit local bookkeeping files such as `graphify-out/manifest.json`
  or `graphify-out/cost.json`.
- Refresh the graph with `graphify .` after meaningful code changes.
- If `graphify` is missing, tell the user to reinstall with
  `npx github:chonamdoo/agent-flow install` or run the manual install commands
  above.
