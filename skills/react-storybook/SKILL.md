---
name: react-storybook
description: React Web Storybook stories/configuration, important UI-state reproduction, play interactions, accessibility/visual evidence, or explicit adoption design. Not ordinary component work, generic user-story documents, React Native renderers, or proof of server auth, DB, SSR, RSC, SEO, or end-to-end behavior.
workflowPhases: [design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [storybook, CSF story, storybook play, Storybook interaction, Storybook a11y]
---

# React Storybook

Storybook is opt-in. Apply to an existing React Web Storybook surface or explicitly requested adoption work, not every component or TSX change. Discover the installed renderer, builder/framework, version, addons, and existing scripts first. Discussing adoption does not authorize installation, server startup, paid services, external writes, or changes to runtime approval/workflow gates.

## State and Interaction Contract

- Reproduce meaningful, actually possible component/screen states with typed args and existing render contracts. Include loading/error/empty/success only when they can occur; do not create impossible combinations to fill a checklist. Follow existing naming and fixture conventions rather than adding model copies or provider layers for their own sake.
- An `error` prop fixture proves the error presentation. To claim form validation behavior, mount the actual form instance, validators/resolver, and field adapter; exercise input, blur, invalid submit, correction, and resubmit. Read `react-hook-form-zod` for RHF or `react-tanstack-form` for TanStack Form only when this branch applies. A story that bypasses validation cannot stand in for it.
- Assert the observable contract: visible state, accessible errors, focus, and meaningful submission values/outcomes. A mocked function being called alone does not establish a correct user flow or server persistence.

## Isolation and Safety

- Control dependencies at existing provider, typed port, or network-adapter boundaries, using the project's established mocking approach where present. Keep production behavior unchanged: no `isStorybook` switches, fake success fallbacks, or app-wide fake clients to make a story pass.
- Scope form instances, stores, query caches, mock handlers, and changed clock/global state to each run and restore them through supported lifecycle cleanup. A previous story's error, cache, or edited values must not affect the next one.
- Use synthetic non-secret fixtures. Do not embed credentials, real personal data, production write access, or automatic real backend mutations in stories. Storybook published assets are not a safe place for secrets.
- Separate evidence: render success shows a state can render; `play` checks an interaction; visual comparison checks appearance; automated a11y checks its supported rules. None alone verifies complete keyboard/screen-reader accessibility. Storybook success does not prove actual server auth/DB behavior, SSR/hydration/RSC serialization, SEO, or end-to-end integration.

## Conditional Detail

- Installation/upgrades, framework/builder/addon selection, CSF or test API imports: read [Version integration](references/version-integration.md). A fixture-only change on the existing setup does not require a migration.
- `play`, visual/a11y evidence, form interaction, or story-state cleanup: read [Interaction and evidence](references/interaction-and-test-details.md). A static render fixture need not acquire a play function.

## Observable Acceptance

Use the existing authorized Storybook runtime and declared commands to open the changed story and exercise the relevant contract. For a form interaction, observe invalid submission/error announcement and focus, then corrected submission through the real form adapter. For isolation, edit or fail one story, switch to another, and revisit to ensure state matches its intended fixture. For visual or accessibility work, inspect the actual surface and label the evidence type and limits.

Report observed outcomes separately from source reasoning and scenarios not run. Missing server evidence cannot be filled by a passing story. A static Button story without app-wide providers or `play`, an error-display-only fixture, and a project that has not opted into Storybook are normal counterexamples. Broken focus/submission through the real adapter, impossible fixture states, production fallbacks, and cross-story leaks are concrete failures. Do not require every component to have stories or require a paid visual service.

## Primary Sources

- [Storybook stories and args](https://storybook.js.org/docs/writing-stories), [interaction tests](https://storybook.js.org/docs/writing-tests/interaction-testing).
- [Mocking providers](https://storybook.js.org/docs/writing-stories/mocking-data-and-modules/mocking-providers), [mocking network requests](https://storybook.js.org/docs/writing-stories/mocking-data-and-modules/mocking-network-requests).
- [Accessibility testing](https://storybook.js.org/docs/writing-tests/accessibility-testing), [visual testing](https://storybook.js.org/docs/writing-tests/visual-testing).
