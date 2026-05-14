---
name: code-generation-discipline
description: Common code generation and code modification discipline for agent-flow. Use before and during implementation, TDD red/green/refactor, fix-loop, bug fixes, feature work, and any task where Codex, Claude, Gemini, or another agent writes or changes code. Enforces start-before, during-implementation, and after-implementation checks without a scoring rubric.
---

# Code Generation Discipline

Use this as the common implementation discipline. Do not score it. Apply it as a checklist.

## Before Starting

- Restate the requested behavior in one or two lines.
- State assumptions and uncertainty. If the repo can answer a question, inspect it first.
- Check existing patterns, helpers, APIs, and project instructions before adding a new approach.
- Choose the smallest code path that can satisfy the request.
- Define the verification command or observable check before editing.
- Select language-specific support only for files you will touch:
  - Python files (`*.py`): apply `python-development-guide`.
  - TypeScript/TSX files (`*.ts`, `*.tsx`): apply `typescript-development-guide`.
  - React Web, Next.js, or TSX component/hook/rendering/accessibility changes: apply `react-development-guide`.
  - React Native, Expo, Metro, navigation, permissions, native bridge, or RN app UI changes: apply `react-native-development-guide`.
  - Android, Kotlin, Jetpack Compose, or KMP files: apply the relevant chrisbanes skill.

## During Implementation

- Stay inside the requested scope.
- Do not add unrelated refactors, formatting churn, docs, or error handling.
- Prefer existing local patterns and helpers over new abstractions.
- Add a new abstraction only when it removes real duplication or matches an existing pattern.
- Use the selected language-specific guide as a secondary checklist. Repo patterns and task scope stay first.
- Every new or modified code block must include Korean comments. Do not leave English comments in changed code.
- Remove unused imports, variables, functions, and files created by the change.

## After Implementation

- Confirm the requested behavior is actually implemented.
- Check language/framework guide violations only when they create real defects, runtime risk, accessibility regression, hook rule violation, hydration/server-client boundary risk, performance regression, security risk, test failure, or project-rule violation.
- Run the verification command chosen before editing, plus build/typecheck/lint/tests when relevant.
- If review or QA fails, return to the fix phase before continuing.
- Do not report completion if verification was not run or could not run.
- Summarize changed files, verification, and remaining risk briefly.
