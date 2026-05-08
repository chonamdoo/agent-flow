---
name: graphify
description: |
  Use when the user asks to build, query, inspect, or explain a repository
  knowledge graph with Graphify. Graphify is optional and is installed by
  running agent-flow install with --with-graphify.
---

# Graphify

Graphify builds a queryable knowledge graph for code, docs, papers, and
diagrams. Use it for broad codebase orientation, architecture exploration,
cross-file relationship questions, and "why is this connected to that" style
analysis.

## Install

Agent-flow can install Graphify during project bootstrap:

```bash
npx github:chonamdoo/agent-flow install --with-graphify
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
- If `graphify` is missing, tell the user to reinstall with
  `--with-graphify` or run the manual install commands above.
