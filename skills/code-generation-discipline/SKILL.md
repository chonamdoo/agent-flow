---
name: code-generation-discipline
description: Common code generation and code modification discipline for agent-flow. Use before and during implementation, TDD red/green/refactor, fix-loop, bug fixes, feature work, and any task where Codex, Claude, OMP, or another agent writes or changes code. Enforces start-before, during-implementation, and after-implementation checks without a scoring rubric.
delivery: passive
requires: [write-for-work]
---

# Code Generation Discipline

Use this as the common implementation discipline. Do not score it. Apply it as a checklist.

## Before Starting

- Restate the requested behavior in one or two lines.
- State assumptions and uncertainty. If the repo can answer a question, inspect it first.
- Check existing patterns, helpers, APIs, and project instructions before adding a new approach.
- Choose the smallest code path that can satisfy the request.
- Define the authorized verification command or observable check before editing;
  account for the active phase's execution permissions and assigned owner.
- For agent-flow user-facing updates, default to short prose in the language the user writes in, and keep code, commands, paths, and identifiers in English.
- For code generation, modification, and code review, resolve required skills from active profile metadata, `.agent-flow/skills/index.json`, changed files, and task scope before writing or judging code.
- Load only the required skill union for touched profiles. Do not require unrelated platform skills.
  - Python files (`*.py`): apply the Python profile required skill group.
  - TypeScript files: apply the TypeScript profile group. TSX alone does not
    distinguish React Web from React Native; inspect dependencies and source scope.
  - React Web/Next.js components, hooks, rendering, or accessibility: apply the
    dependency-qualified React capability on the active profile. Forms, public SEO,
    and Storybook select their own scoped skills, not every TSX change.
  - React Native/Expo UI, navigation, permissions, Metro, or native bridges: apply
    the React Native group; add native platform guidance for the touched source set.
  - Android/Compose or Android-targeted KMP work: apply Android `required_review`,
    including `android-code-review`. Plain Kotlin or Gradle files are not Android
    evidence.
  - Kotlin/JVM server work: apply `kotlin-backend-development-guide`; add
    `spring-boot-development-guide` for actual Spring or `ktor-development-guide`
    for Ktor server. Ktor client-only is not a server trigger; Java-only Spring
    work does not require the Kotlin guide.
  - LLM tool schema, dispatch, authority/approval, provider or MCP adapter
    development: apply `llm-tool-development` with the implementation language.
    Merely using an available tool or MCP server does not trigger development rules.
  - Read required skills at the paths resolved for the active host. Use offered
    skills only for matching work; do not invent host paths or install mid-run.
- For app-wide error handling, common dialog/snackbar/toast hosts, SessionExpired navigation, root navigation resets, or API/domain common error mapping, read the matching app-shell skill: `android-appshell-error-handling`, `react-app-shell-error-handling`, `react-native-app-shell-error-handling`, or `ios-app-shell-error-handling`.
- For presentation-layer code generation, modification, and code review, also read the matching presentation architecture skill before writing or judging code:
  - Android/Compose or Android-targeted KMP presentation: read `android-clean-presentation-architecture`.
  - React Web/Next.js/TSX presentation: read `react-clean-presentation-architecture`.
  - React Native/Expo presentation: read `react-native-clean-presentation-architecture`.
  - iOS/SwiftUI/UIKit presentation: read `ios-clean-presentation-architecture`.
  - Flutter/Dart presentation: read `flutter-clean-presentation-architecture`.
- Presentation work must be state-based. Record `presentation-skill: android|flutter|react|react-native|ios|n/a`, `presentation-state-based-development: applied|n/a`, `presentation-state-review: pass|fail|n/a`, `ui-state-modeling: explicit|n/a`, `presentation-mapping-boundary: domain-to-uimodel|n/a`, and `di-boundary: <hilt|context-provider|tsyringe|swift-environment|factory|swift-dependencies|swinject|needle|riverpod|get-it|direct|existing|n/a>` in the completion gate when relevant.
- Completion Gate markers must use concrete values that the marker parser accepts. Do not leave angle-bracket placeholders; use `n/a` only when the marker is genuinely not relevant.
- For multiple UI platforms, retain platform-scoped evidence using the workflow's
  supported artifact structure. Do not put comma-separated platforms into a scalar
  marker or collapse unreviewed platforms into a pass. UI-free server work uses
  the existing presentation `n/a`, not a new `spring` or `ktor` UI value.
- Record `change-kind` and `completion_disposition` only as the active workflow
  requires. A marker does not grant approval, authorize execution, prove integration,
  or permit checkout deletion; those remain runtime/controller decisions.
- Record the generic profile-driven markers in the phase artifact's `## Completion Gate`: `profile-skill-selection: applied`, `active-profiles: <profile list>`, `changed-file-skill-resolution: applied`, `required-profile-skills: checked`, and `missing-required-profile-skills: none|<list>`.
- If the prompt surfaces project-local code/review skill docs, read only those applicable docs, record `project-local-skills: checked`, `project-local-skills-used: <skill list>`, and `project-local-skill-docs: applied`. Design/Figma, hook, branch, PR, merge, and cleanup local skills do not satisfy or trigger this code/review marker. If no project-local code/review skill applies, record `project-local-skills: n/a` and `project-local-skills-used: n/a`.
- Missing-skill handling lives here only; other docs point at this bullet instead of restating it. The phase prompt resolves required skills against the host you are running on and names the ones that are not installed there. A skill named as not installed is not a violation and not a finding: record `skill-availability: degraded`, put its **bare skill name** in the comma-separated `missing-required-profile-skills:` marker, and write the declared `missing local <group>: <skill>` sentence only in prose/Calibration. Continue with the skills you do have. Do not stop work, do not ask the user to install anything mid-run, and never turn absence into `verdict: request-changes` — installation is not something the code under review can change. Installation is owned by project setup and `agent-flow skills sync`.

## Agent-Facing Documents

For skill/reference/template authoring, use only the applicable rules from
[skill-creator](https://github.com/openai/skills/blob/main/skills/.system/skill-creator/SKILL.md)
(understand concrete needs, write, check actual use) and
[writing-for-agents](https://github.com/mattpocock/skills/blob/main/skills/productivity/writing-for-agents/SKILL.md)
with [SKILL-MECHANICS](https://github.com/mattpocock/skills/blob/main/skills/productivity/writing-for-agents/SKILL-MECHANICS.md)
(observable completion, related rules together, conditional references).
Do not import either skill wholesale, install its scaffolder/validator, or create
a second workflow. The repository's actual metadata and routing contract governs.

- Establish the intended task, trigger/non-trigger examples, expected observable
  result, and harmful failure before writing. Resolve these from the request and
  project evidence; ask only for a material gap.

- Give independently selected capabilities precise trigger and non-trigger branches.
  Descriptions help discovery; supported profile/frontmatter selectors govern
  routing. Do not claim all hosts/resolvers enforce an invocation flag identically.
- Keep shared invariants and observable completion criteria inline, with related
  rules and their exceptions together. Put branch-specific API/version detail
  behind explicit conditional pointers; read only targets whose branch applies.
- Keep each policy in one canonical source. Examples distinguish plausible
  defects from valid alternatives, not incidental names, file counts, or a
  required application scaffold.
- Discover actual versions, commands, roots, and architecture mappings from
  project settings and runtime metadata. Reusable prose/examples contain no
  personal/organization/repository-specific paths, package prefixes, or fixed
  folder trees. Reference links locate documentation, not required app structure.
- Preserve authorization, tenant isolation, approval binding, side-effect states,
  failure recovery, output meaning, and evidence requirements when pruning prose.
  A skill is guidance, not an enforced permission or sandbox boundary.
- Distinguish source facts, design choices, executed results, and deferred checks.
  Shorter text alone is not evidence of improved correctness or performance.
- For each new or materially changed skill, hand off an observable acceptance,
  valid-code counterexample, critical-failure case, non-target activation case,
  and conditional-reference case. Evaluate actual behavior through the existing
  evaluation path; a readable document or resolved skill name is not behavioral proof.
- Keep proof layers separate: deterministic structure, selection/distribution,
  and scorer contracts belong in declared CI; live-model behavior runs manually
  and non-blocking. Preserve the existing verification-tier policy. Do not turn
  model variability into a required CI gate or introduce a runtime test bypass.
- The assigned evaluation owner runs checks after concurrent writing finishes.
  Record observed outcomes and revise defects; report unevaluated cases as
  unevaluated rather than manufacturing a pass from the skill's own checklist.


## During Implementation

- Stay inside the requested scope.
- Do not add unrelated refactors, formatting churn, docs, or error handling.
- Prefer existing local patterns and helpers over new abstractions.
- Add a new abstraction only when it removes real duplication or matches an existing pattern.
- Single Responsibility — keep one concrete reason to change in each function, class, or module.
- Side Effects — isolate necessary effects at named boundaries; keep computation pure where practical.
- Do Not Repeat Yourself — share repeated policy or logic, not coincidental syntax.
- Parameter Grouping — group values that travel and change together; do not create a type for unrelated arguments.
- Fail Fast — reject invalid state at the earliest boundary that has enough context to explain it.
- Guard Clauses — use early exits when they remove nesting without hiding the main path.
- Single Level of Abstraction — keep one function's steps at one conceptual level; delegate lower-level detail behind named operations.
- Explicit Receiver — make the owner of state or collaborator behavior clear at the call site without requiring language-specific receiver syntax.
- Treat these as blocking only when the code creates a concrete correctness, data-loss, contract, testability, or high-risk maintainability defect. Style differences alone are non-blocking.
- Use the selected language-specific guides as secondary checklists. Repo patterns and task scope stay first.
- Default to no new comments during implementation. Apply `comment-authoring-discipline` as the semantic source for warranted comments and the final comment-quality pass.
- When authoring or revising warranted comments or docstrings, read `write-for-work` and apply its Code comments and docstrings mode before writing the prose. This does not require adding comments to otherwise clear code.
- Remove unused imports, variables, functions, and files created by the change.

## After Implementation

- Confirm the requested behavior is actually implemented.
- Check language/framework guide violations only when they create real defects, runtime risk, accessibility regression, hook rule violation, hydration/server-client boundary risk, performance regression, security risk, test failure, or project-rule violation.
- Run the chosen checks only within the active phase's execution permissions and
  ownership. Resolve build/test/lint commands from declared active profile gates
  and project configuration; do not invent commands or change a CI-only policy.
  A delegated writer hands deferred checks to the integration owner rather than
  running them concurrently or declaring them successful.
- If review or QA fails, return to the fix phase before continuing.
- Do not claim verified completion when verification did not run, failed, or
  could not run. Report implementation state, missing proof, and the assigned
  verification owner separately; preserve the workflow's evidence requirements.
- Summarize changed files, verification, and remaining risk briefly. Do not paste long logs or whole files.
