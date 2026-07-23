---
name: agent-flow-concise-output
description: >
  Korean concise output adapter for agent-flow review, commit, and artifact
  output. Keeps technical accuracy and parser markers intact while reducing
  prose. Use when writing review findings, commit messages, phase artifacts,
  PR summaries, or compressed context summaries.
---

# Agent Flow Concise Output

Use this skill to make agent-flow output concise and clear while preserving its
contracts. For anything beyond trivial shortening, apply `write-for-work`
(reader, purpose, plain language) rather than mechanical translation.
The installer also writes `concise-output.md` to `.Codex/rules/concise-output.md`.

## Rules

- Write user-facing prose in clear, natural Korean per `write-for-work`; favor plain, specific wording over mechanical shortening.
- Keep established English technical terms and identifiers as-is; do not force awkward Korean translations. Explain a term instead of renaming it.
- Keep code, commands, paths, URLs, API names, function names, env vars, errors, version numbers, YAML keys, JSON keys, phase ids, and parser markers unchanged.
- Do not use emoji.
- Do not imitate caveman speech. Use short, natural technical Korean.
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
