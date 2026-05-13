# AGENTS.md Quality Criteria

Use this rubric when auditing Codex project memory.

## Scoring Rubric

### 1. Commands/Workflows (20 points)

20: Build, test, lint, deploy, setup, and common local workflows are documented with enough context to run them correctly.

15: Most important commands are present, but some context or common operations are missing.

10: Basic commands are listed without workflow context.

5: Few commands are present.

0: No useful commands are documented.

### 2. Architecture Clarity (20 points)

20: Key directories, entry points, module relationships, and relevant data flow are clear.

15: Good structure overview with minor gaps.

10: Mostly a directory listing.

5: Vague or incomplete.

0: No architecture context.

### 3. Non-Obvious Patterns (15 points)

15: Gotchas, quirks, workarounds, edge cases, and unusual "why" decisions are captured.

10: Some non-obvious patterns are documented.

5: Minimal pattern documentation.

0: No gotchas or project-specific patterns.

### 4. Conciseness (15 points)

15: Dense, valuable content with no filler, duplicated instructions, or obvious code restatements.

10: Mostly concise with minor padding.

5: Verbose in several places.

0: Mostly filler or redundant content.

### 5. Currency (15 points)

15: Commands work, paths exist, tech stack is current, and instructions match the repo.

10: Mostly current with minor stale references.

5: Several outdated references.

0: Severely outdated.

### 6. Actionability (15 points)

15: Instructions are concrete, commands are copy-paste ready, and paths are real.

10: Mostly actionable.

5: Some vague or theoretical instructions.

0: Not executable or too abstract.

## Assessment Process

1. Read the relevant `AGENTS.md` file completely.
2. Cross-check documented commands, paths, architecture claims, and imports against the repo.
3. Score each criterion.
4. Assign a grade: A 90-100, B 70-89, C 50-69, D 30-49, F 0-29.
5. List concrete issues and proposed improvements.

## Red Flags

- commands that would fail
- references to deleted paths
- outdated framework or package manager notes
- copied templates with no repo-specific information
- generic advice not specific to this repo
- stale TODO items
- duplicated content across global and project instruction files
- long documentation that belongs in `.Codex/rules/`
