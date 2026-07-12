---
name: code-generation-discipline
description: Provides common implementation discipline for safe code generation and code modification across agent-flow phases. Use when starting or continuing implementation, TDD red/green/refactor, fix-loop, bug fixes, feature work, or any task where Codex, Claude, OMP, or another agent writes or changes code; pair it with the required language/framework skills instead of using it as a replacement.
---

# Code Generation Discipline

Use this as the common implementation discipline. Do not score it. Apply it as a checklist.

## Quick start

1. Restate the requested behavior and choose the smallest existing code path that can satisfy it.
2. Resolve and read only the required project, language, framework, presentation, and platform skills for the files in scope.
3. Define the verification command or observable check before editing, then apply the before/during/after checklist below.

This is a discipline skill, not a standalone implementation workflow. It does not replace language/framework skills, architecture skills, project instructions, review, QA, or user approval gates.

## Companion files

- [agents/openai.yaml](agents/openai.yaml): agent/profile metadata for installers or profile registries. Read it only when wiring this skill into OpenAI/Codex profile metadata, not during normal implementation.

## Before Starting

- Restate the requested behavior in one or two lines.
- State assumptions and uncertainty. If the repo can answer a question, inspect it first.
- Check existing patterns, helpers, APIs, and project instructions before adding a new approach.
- Choose the smallest code path that can satisfy the request.
- Define the verification command or observable check before editing.
- For agent-flow user-facing updates, default to short Korean and keep code, commands, paths, and identifiers in English.
- For code generation, modification, and code review, resolve required skills from active profile metadata, `.agent-flow/skills/index.json`, changed files, and task scope before writing or judging code.
- Load only the required skill union for touched profiles. Do not require unrelated platform skills.
  - Python files (`*.py`): apply the Python profile required skill group.
  - TypeScript/TSX files (`*.ts`, `*.tsx`): apply the TypeScript/React/Next profile required skill group.
  - React Web, Next.js, or TSX component/hook/rendering/accessibility changes: apply the React/Next profile required skill group.
  - React Native, Expo, Metro, navigation, permissions, native bridge, or RN app UI changes: apply the React Native profile required skill group.
  - Android, Kotlin, Jetpack Compose, or KMP files: apply Android profile `required_review`, including `android-code-review`, matching `android_skills`, and matching `chrisbanes_skills`. Resolve every required skill through the leader checkout's `.agent-flow/skills/index.json` and read its project path only.
  - React Native `android/` native code changes: also apply the Android profile mapping and matching Android skill groups.
- For app-wide error handling, common dialog/snackbar/toast hosts, SessionExpired navigation, root navigation resets, or API/domain common error mapping, read the matching app-shell skill: `android-appshell-error-handling`, `react-app-shell-error-handling`, `react-native-app-shell-error-handling`, or `ios-app-shell-error-handling`.
- For presentation-layer code generation, modification, and code review, also read the matching presentation architecture skill before writing or judging code:
  - Android/Kotlin/Compose presentation: read `android-clean-presentation-architecture`.
  - React Web/Next.js/TSX presentation: read `react-clean-presentation-architecture`.
  - React Native/Expo presentation: read `react-native-clean-presentation-architecture`.
  - iOS/SwiftUI/UIKit presentation: read `ios-clean-presentation-architecture`.
- Presentation work must be state-based. Record `presentation-skill: android|react|react-native|ios|n/a`, `presentation-state-based-development: applied|n/a`, `presentation-state-review: pass|fail|n/a`, `ui-state-modeling: explicit|n/a`, `presentation-mapping-boundary: domain-to-uimodel|n/a`, and `di-boundary: <hilt|context-provider|tsyringe|swift-environment|factory|swift-dependencies|swinject|needle|direct|existing|n/a>` in the completion gate when relevant.
- Completion Gate markers must use concrete values that the marker parser accepts. Do not leave angle-bracket placeholders; use `n/a` only when the marker is genuinely not relevant.
- Record the generic profile-driven markers in the phase artifact's `## Completion Gate`: `profile-skill-selection: applied`, `active-profiles: <profile list>`, `changed-file-skill-resolution: applied`, `required-profile-skills: checked`, and `missing-required-profile-skills: none|<list>`.
- In code generation and review phases, every deterministically applicable project-local skill is mandatory: explicit `always` skills plus `conditional` skills whose phase and task/path selectors match. Skills without activation metadata are `on-demand`: keep them installed and host-discoverable, but do not force them into unrelated prompts. Read each applicable local `SKILL.md`, follow references progressively, and record `project-local-skills: checked`, every applicable name in `project-local-skills-used`, and `project-local-skill-docs: applied`. If no project-local skill applies, record `project-local-skills: n/a` and `project-local-skills-used: n/a`.
- If Android/Kotlin/Compose/KMP files are changed, additionally record `android-local-skills: checked`, `android-local-skills-used: <explicit loaded skill list>`, `chrisbanes-skills: checked|n/a`, and `chrisbanes-skills-used: <skill list or n/a>`. Do not write these Android-only markers as global requirements for unrelated profiles.
- If a required language/framework skill is absent from the project skill index or its tree hash does not match, report `missing local <group>: <skill>` and stop. Repair the project snapshot outside the active run; do not fall back to a host-global directory.

## During Implementation

- Stay inside the requested scope.
- Do not add unrelated refactors, formatting churn, docs, or error handling.
- Prefer existing local patterns and helpers over new abstractions.
- Add a new abstraction only when it removes real duplication or matches an existing pattern.
- Use the selected language-specific guides as secondary checklists. Repo patterns and task scope stay first.
- Write comments only when code alone cannot carry the reason or contract.
- Do not add WHAT/HOW comments that restate the code. Avoid generic comments such as "Initialize", "Set value", or "Loop through".
- Keep comments for WHY, external API/platform constraints, workarounds, security, performance, concurrency, public API contracts, complex domain rules, and complex algorithms or regular expressions.
- Prefer no new comment when the reason is not explicit. If a final pass is required, apply `comment-authoring-discipline` before review.
- Remove unused imports, variables, functions, and files created by the change.

## After Implementation

- Confirm the requested behavior is actually implemented.
- Check language/framework guide violations only when they create real defects, runtime risk, accessibility regression, hook rule violation, hydration/server-client boundary risk, performance regression, security risk, test failure, or project-rule violation.
- Run the verification command chosen before editing, plus build/typecheck/lint/tests when relevant.
- If review or QA fails, return to the fix phase before continuing.
- Do not report completion if verification was not run or could not run.
- Summarize changed files, verification, and remaining risk briefly. Do not paste long logs or whole files.
