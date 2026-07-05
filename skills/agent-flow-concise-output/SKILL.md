---
name: agent-flow-concise-output
description: >
  Adapts agent-flow reviews, commits, phase artifacts, PR summaries, and
  compressed context summaries into concise Korean while preserving parser
  markers and technical tokens. Use when writing agent-flow review findings,
  commit messages, phase artifacts, PR summaries, or compressed context
  summaries.
---

# Agent Flow Concise Output

Use this skill to reduce prose while preserving agent-flow contracts.

## Quick start

1. Apply this as an output adapter after the main workflow or review skill has determined the substance.
2. Shorten only prose; preserve parser markers, paths, commands, identifiers, code, and required verdict/status lines byte-for-byte.
3. If a one-page rules file is needed for Codex/installer integration, read [concise-output.md](concise-output.md).

This is not a standalone workflow skill. It pairs with `agent-flow` and review/commit/artifact-writing phases, and it does not replace the underlying review, QA, commit, or phase contract.
The installer also writes `concise-output.md` to `.Codex/rules/concise-output.md`.

## Rules

- Write user-facing prose in Korean.
- Keep code, commands, paths, URLs, API names, function names, env vars, errors, version numbers, YAML keys, JSON keys, phase ids, and parser markers unchanged.
- Do not use emoji.
- Do not imitate caveman speech. Use short technical Korean.
- Preserve exact verdict/status lines, including `verdict: approve`, `verdict: request-changes`, `verdict: blocked`, and `next_command`.

## Review

- One finding per line.
- Format: `path/to/file:L42: must-fix: 문제. 수정.`
- Severity: `must-fix`, `should-fix`, `note`.
- No praise, throat-clearing, long background, or generic advice.

## Commit

- Use Conventional Commits.
- Subject target: 50 chars. Hard cap: 72 chars.
- Add body only when the reason is not obvious from the subject.
- Keep type and scope in English.

## Artifact

- Keep artifacts focused on action taken, file paths, verdict/status, blocker, and `next_command` when required.
- Do not translate required markers or completion gates.
- For context compression, write a summary artifact instead of overwriting the original. Preserve code blocks, inline code, URLs, paths, env vars, and version numbers exactly.

## Source Note

Inspired by `juliusbrussee/caveman`, adapted for Korean agent-flow usage without importing its full skill set.
