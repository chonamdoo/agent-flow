---
name: react-tailwind-v4
description: React Web Tailwind CSS v4 styling implementation and review for className utilities, CSS @theme tokens, responsive/state variants, existing cn/CVA helpers, and class formatting. Use when the touched React Web project actually uses Tailwind v4; not for CSS-only alternatives, Tailwind v3 migrations, or React Native styles.
workflowPhases: [design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [Tailwind v4, Tailwind CSS 4, tailwindcss, '@theme', tailwind className, Tailwind variant]
---

# React Web Tailwind CSS v4

Use for styling a React Web application that actually uses Tailwind CSS v4. First inspect the installed major version, global stylesheet/imports, `@theme`, any legacy `@config`, component variants, existing `cn`/class-merging helper, responsive breakpoints, and repository formatter. A React component without Tailwind usage does not trigger adoption; do not convert CSS modules or another styling system as a side effect. This skill covers styling, not feature placement: apply the selected local architecture independently (for an FSD project, `react-fsd-architecture`).

## Decide where each style lives

- Put reusable design values that must generate utilities/variants in the project's established CSS `@theme` surface. Use meaningful role names for semantic colors and surfaces, preserving its established palette and naming. Ordinary runtime-only CSS variables can live in normal CSS; `@theme` is for tokens that Tailwind exposes as classes or breakpoint variants. Check whether an existing `@config`/JS compatibility configuration intentionally owns some values (for example, class-merge ambiguity) before moving or redefining them. Do not add a second source of truth for a token or blindly delete a working compatibility config.
- Keep a one-off precisely measured value as an arbitrary value when it materially serves the design; reuse an existing utility/token where possible, and consider `@theme` for repeated design values. Do not invent a universal spacing grid or a mandated breakpoint from another project's example. Use the configured breakpoints; express mobile-first base styling with conditional wider-screen variants where the product layout calls for it. Handle hover/focus/disabled, dark, and other contextual variants according to the actual interaction and accessibility contract.
- Use complete literal utility names in source. Tailwind scans text, not JavaScript interpolation: map a finite prop to whole class strings (or defined typed variants), e.g. `{ success: 'bg-green-600', warning: 'bg-amber-500' }`, rather than `` `bg-${tone}-500` ``. If classes live in an ignored/external package, inspect the app's source-detection configuration before concluding they will be generated.
- Use the project's **existing** `cn`/equivalent helper for conditional and conflicting classes where it actually solves composition; inspect its configured merge groups before assuming a custom token will merge correctly. For a reusable component with real variant/size axes, follow the installed component variant convention (CVA if already chosen). A simple conditional class need not acquire CVA, a new merge helper, or a separate component. Reuse UI components for repeated visual patterns; reserve `@apply` for an existing justified CSS integration rather than reflexively duplicating whole utility sets in CSS.
- Let the project's formatter/plugin and configuration determine ordering and line breaks. If `prettier-plugin-tailwindcss` is installed and configured, use it rather than hand-enforcing another project's ordering or print width. Do not install tools or rewrite unrelated classes to satisfy a generic style preference.

The styling decision is complete when each changed class resolves under the installed Tailwind setup, conditional alternatives survive scanning, tokens have one owner, and the intended responsive/state behavior is observable. For UI changes, inspect the rendered surface at relevant viewport widths and states; source inspection or formatting alone is not visual proof.

## Cases for implementation or review

- **Success:** A shared component uses an established `cn` helper to combine literal base classes with a typed literal variant map, and uses an existing `@theme` semantic color for the active state; verify both active and inactive rendering.
- **Valid alternative:** A unique, measured one-off width uses an arbitrary value; or a project keeps its documented `@config` plus `@theme` split for distinct token responsibilities. Neither requires a new token or a wholesale config migration.
- **Failure:** A prop interpolates a partial utility such as `` `text-${tone}-600` `` that the scanner cannot discover, a new class references a missing token, or a new token is defined independently in both CSS theme and JS config with conflicting values.
- **Non-target:** Tailwind v3 upgrade planning, plain CSS-in-JS/CSS modules in a non-Tailwind app, React Native `StyleSheet`, or a Query/Form state-only change without styling.

## Sources and uncertainty

- [Tailwind v4 theme variables](https://tailwindcss.com/docs/theme/) defines `@theme`-generated utilities, CSS-only variables, and namespace behavior; [source detection](https://tailwindcss.com/docs/detecting-classes-in-source-files) documents literal class scanning and `@source`; [v4 upgrade guide](https://tailwindcss.com/docs/upgrade-guide) documents v4 compatibility concerns. Official pages are rolling references: compare with the **installed release** before using version-sensitive behavior.
- Another project's tool versions, paths, spacing values, breakpoint names, library presence, and formatting preferences are **not** evidence for the target app. The one-off arbitrary-value exception, conditional use of CVA, and priority of existing project config are judgments reconciled with Tailwind's official behavior, not universal rules.
