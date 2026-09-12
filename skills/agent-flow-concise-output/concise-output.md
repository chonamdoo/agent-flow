# Concise Output

Keep agent-flow artifact, review, and commit output short while preserving parser contracts and technical meaning.

## Preserve

- Preserve code, commands, paths, URLs, API names, function names, environment variables, error strings, and version numbers verbatim.
- Do not translate or shorten YAML/JSON keys, CLI statuses, phase IDs, or completion markers.
- Preserve every exact `verdict:`, `status:`, and `next_command` line emitted by the active workflow.
- Preserve exact workflow verdict lines such as `verdict: approve`, `verdict: request-changes`, and `verdict: blocked`.

## Korean Adapter

- Write user-facing sentences in Korean.
- Keep code, commands, and identifiers in their original English form.
- Do not use emojis.
- Use concise technical Korean, not caveman-style speech.
- Omit grammatical particles only when doing so preserves the meaning.

## Review Findings

- Write one finding per line.
- Format: `path/to/file:L42: must-fix: Problem. Correction.` Write the problem and correction in Korean while preserving the literal path and severity.
- Use only `must-fix`, `should-fix`, and `note` for severity.
- Omit praise, background explanation, and generalities.

## Commit Messages

- Preserve the Conventional Commit format.
- Aim for a 50-character subject with a 72-character hard cap.
- Add a body only when the subject alone does not explain the reason for the change.
- Keep type and scope in English.

## Compression Safety

- Compress natural language only.
- Do not overwrite original memory or context documents.
- A summary fails if it changes a code block, inline code, URL, path, environment variable, or version number.
