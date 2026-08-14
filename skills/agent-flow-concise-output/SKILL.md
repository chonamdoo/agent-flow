---
name: agent-flow-concise-output
description: >
  Korean concise output adapter for agent-flow review, commit, and artifact
  output. Keeps technical accuracy and parser markers intact while reducing
  prose. Use when writing review findings, commit messages, phase artifacts,
  PR summaries, or compressed context summaries.
requires: [write-for-work]
---

# Agent Flow Concise Output

Use this skill to make agent-flow output concise and clear while preserving its
contracts. For anything beyond trivial shortening, apply `write-for-work`
(reader, purpose, plain language) rather than mechanical translation.
The installer also writes `concise-output.md` to `.Codex/rules/concise-output.md`.

## Rules

Read [`concise-output.md`](concise-output.md) and apply every section relevant to the current review, commit, artifact, or compression output. It is the detailed rule source; this file owns invocation only.

Apply `write-for-work` when shortening requires restructuring or rewriting rather than a trivial cut. Preserve every parser-owned marker, verdict, status, and `next_command` value exactly as emitted by the active workflow.

## Source Note

Inspired by `juliusbrussee/caveman`, adapted for Korean agent-flow usage without importing its full skill set.
