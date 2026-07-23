---
name: code-generation-discipline
description: Common code generation and code modification discipline for agent-flow. Use before and during implementation, TDD red/green/refactor, fix-loop, bug fixes, feature work, and any task where Codex, Claude, OMP, or another agent writes or changes code. Enforces start-before, during-implementation, and after-implementation checks without a scoring rubric.
---

# Code Generation Discipline

Use this as the common implementation discipline. Do not score it. Apply it as a checklist.

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
  - Android, Kotlin, Jetpack Compose, or KMP files: apply Android profile `required_review`, including `android-code-review`, matching `android_skills`, and matching `chrisbanes_skills`. Local means the project/host-installed skill path such as `.agent-flow/local-skills/<skill>/SKILL.md`, `.agent-flow/skills/<skill>/SKILL.md`, or the current host's configured local skill directory.
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
- If the prompt surfaces project-local code/review skill docs, read only those applicable docs, record `project-local-skills: checked`, `project-local-skills-used: <skill list>`, and `project-local-skill-docs: applied`. Design/Figma, hook, branch, PR, merge, and cleanup local skills do not satisfy or trigger this code/review marker. If no project-local code/review skill applies, record `project-local-skills: n/a` and `project-local-skills-used: n/a`.
- If Android/Kotlin/Compose/KMP files are changed, additionally record `android-local-skills: checked`, `android-local-skills-used: <explicit loaded skill list>`, `chrisbanes-skills: checked|n/a`, and `chrisbanes-skills-used: <skill list or n/a>`. Do not write these Android-only markers as global requirements for unrelated profiles.
- If a required language/framework skill is not installed at the active host path, report `missing local <group>: <skill>` with the configured source URL and do not proceed until the user installs it or explicitly overrides.

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
- Resolve build, run, test, and lint commands from the active profile gates (`.agent-flow/profiles/<profile>.yaml`) or the installed skill/CLI docs, not from a web search. The profile already defines them (for example, Android builds with `./gradlew assembleDevDebug`, tests with `./gradlew test`). Only web-search when no profile gate, project script, or skill documents the command.
- If review or QA fails, return to the fix phase before continuing.
- Do not report completion if verification was not run or could not run.
- Summarize changed files, verification, and remaining risk briefly. Do not paste long logs or whole files.
