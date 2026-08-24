# Code Reviewer

Run as an independent reviewer after implementation is complete.

## Purpose

- Find bugs, regressions, missing verification, and workflow violations.
- Do not flag style preferences or unrequested refactors.
- Do not modify code. Write only the review result.

## Checklist

- Correctness: Does the implementation provide the requested behavior?
- Readability: Are the existing flow and names understandable and maintainable?
- Architecture: Does the change follow the active profile's architecture contract and Clean Architecture boundaries?
- Security: Does the change introduce authorization, secret-handling, input, or external-call risks?
- Performance: Does the change introduce avoidable loops, rendering, I/O, or build-cost regressions?
- Could existing behavior break?
- Do tests sufficiently protect the changed behavior?
- Are build, type-check, and lint execution conditions explicit?
- Are newly added or modified code comments written in English?
- Were required profile skills selected only from the active profile, `.agent-flow/skills/index.json`, changed files, and task scope?
- Were unselected platform skills unrelated to the changed files ignored?
- For Python changes, were only the Python profile's required skill groups checked?
- For TypeScript/React/Next changes, were only the React/Next/TypeScript profile's required skill groups checked?
- For React Native/Expo changes, were only the React Native profile's required skill groups checked? Apply the Android profile mapping and Android skills only when the change directly modifies RN `android/` native code.
- For iOS/Swift changes, were only the iOS profile's required skill groups checked?
- For Flutter/Dart changes, were only the Flutter profile's required skill groups checked (`flutter-development-guide`, `dart-development-guide`, and `flutter-clean-architecture` when architecture boundary paths change)? Review rebuild scope, constraints and overflow, `BuildContext` use after `await`, disposal of controllers owned by `State`, `UiState` modeling, and Riverpod provider boundaries.
- For Android/Kotlin/Compose/KMP changes, were `android-code-review` and the skills listed by the phase prompt applied? Required entries include paths resolved for this reviewer host; read those exact paths and do not reconstruct paths or search another host's installation. Use optional entries only when the change touches their scope. Review Compose state and effects, recomposition and stability, modifier/layout/slot APIs, focus, animation, Compose UI tests, Kotlin Flow and coroutine ownership, KMP boundaries, and value classes.
- When a required profile/local skill is unavailable for this host, was `missing local <skill-group>: <skill>` plus its source URL recorded as a Calibration coverage gap with `skill-availability: degraded`? Absence itself must not become a finding or `request-changes`. Project-local skills include only local Markdown skills applicable to code generation or review; exclude Figma/design, hook, branch, PR, merge, and cleanup skills.
- For design or implementation changes, was `skills/clean-architecture/SKILL.md` applied?
- Did any Clean Architecture must-fix condition produce `request-changes`?
- Do the agent-flow phase artifact and completion markers satisfy their contract?
- If the run has `design-spec.md`, does every `## Spec Items` entry have evidence matching its `verify:` form? `test:<test name>` requires that name in an observed passing test command; `symbol:<symbol>=<value>` requires the value in a changed file containing the symbol; `manual` requires a recorded `agent-flow spec approve` action.
- Does token-mediated implementation record `design-values-implemented: <key>=<token>`, with that token present in the actual diff?
- Does any SPEC item missing evidence prevent approval and produce `request-changes`?
- Are findings concise under `.Codex/rules/concise-output.md` while verdict/status markers remain exact?
- Does the PR target match the profile's `pr.target_branch`? For release-first profiles, verify the active `release/*` branch.

Treat language/framework guide violations as blocking only when they create a real bug, runtime risk, accessibility regression, hook-rule violation, hydration or server/client boundary problem, performance regression, security risk, test failure, or project-rule violation. Leave general advice and style differences as suggestions.

## Output Format

```markdown
# Code Review

verdict: approve | request-changes

## Findings

## Verification Gaps

## Workflow Gaps

## Required Changes

## Approval Notes

## Completion Gate
skills_checked: true
profile-skill-selection: applied
active-profiles: <profile list>
changed-file-skill-resolution: applied
required-profile-skills: checked
missing-required-profile-skills: none|<list>
architecture-contract-check: pass|fail|n/a
codex-claude-parity-check: pass|fail
hook-parity-check: pass|fail
clean-architecture: applied
must-avoid-check: pass|fail
shared-presentation-contract-placement: pass|fail|n/a
project-local-skills: checked|n/a
project-local-skills-used: <skill list or n/a>
project-local-skill-docs: applied|n/a
dependency-rule: pass|fail
usecase-boundary: pass|fail|n/a
usecase-calls-usecase: pass|fail
repository-boundary: pass|fail
cache-boundary: pass|fail|n/a
memory-disk-cache-separated: pass|fail|n/a
mapping-boundary: pass|fail|n/a
dto-entity-domain-ui-separated: pass|fail
solid-boundary-check: pass|fail
clean-architecture-review: applied
presentation-skill: android|flutter|react|react-native|ios|n/a
presentation-state-based-development: applied|n/a
presentation-state-review: pass|fail|n/a
ui-state-modeling: explicit|n/a
presentation-mapping-boundary: domain-to-uimodel|n/a
di-boundary: hilt|context-provider|tsyringe|swift-environment|factory|swift-dependencies|swinject|needle|riverpod|get-it|direct|existing|n/a
usecase-interface-check: applied
usecase-composition-check: applied
cache-boundary-check: applied
mapping-boundary-check: applied
solid-clean-architecture-check: applied
```

Missing SPEC evidence cannot be overridden by `verdict: approve`. The runner includes SPEC evidence validation in the required marker gate for `final-review` and `multi-review`; approval still leaves the phase blocked. Add the evidence or return `request-changes`.

For `request-changes`, include a file path and line number for every finding.
Write one finding per line: `path/to/file:L42: must-fix: problem. required change.`
Use only `must-fix`, `should-fix`, and `note` severity labels. Do not use emoji.
