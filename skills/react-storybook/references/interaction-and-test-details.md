# Interaction, Isolation, and Evidence

Read when changing play functions, visual/a11y coverage, form stories, or lifecycle cleanup. Use installed-version APIs; consult [Version integration](version-integration.md) only if the integration itself changes or API support is uncertain.

## Exercise the Consumer's Contract

- Scope queries to the story's canvas through supported APIs. Prefer roles, accessible names, and labels. For portals outside the canvas, query the intentional overlay root without accidentally selecting Storybook's own controls.
- Await user interactions and asynchronous observations/assertions as required by the installed runner. Avoid timing sleeps and incidental render-count assertions. Inspect the resulting screen/values/focus rather than only recording mock calls.
- For actual form validation evidence, instantiate the real form with its schema and adapter. Input and blur must reach the installed form library; invalid submit must display/announce errors and focus the intended field; correction must permit a meaningful submission with the required transformed output. A prop-only error fixture remains useful for rendering, but does not exercise this path.
- For async form stories, control the application port or network boundary so a realistic accepted or rejected outcome reaches the actual form logic. Use relevant record-change, newer-edit, or delayed-response scenarios from `react-hook-form-zod` or `react-tanstack-form`, matching the installed library; do not add every race scenario to every story.
- Fixtures should satisfy the existing discriminated state contract. Do not cast impossible combinations through types simply to obtain a screenshot.

## Isolate Execution

Create per-run mutable state and use the installed Storybook lifecycle to clean up handlers, modified globals, clocks, storage, and subscriptions. Query caches/stores should be fresh or explicitly reset between stories; avoid a module singleton that retains the preceding story's mutation. Unmounting a view alone does not necessarily clear an external cache or mock server handler.

Use deterministic fixture data and control time/theme/viewport/font-loading or motion only when relevant to the intended visual comparison. Restore changed globals after the story; mocking time for one screenshot must not freeze later interactions. Network mocks and provider fixtures are normal dependency controls, not a reason to add production-only detection flags.

## Name the Evidence

| Evidence | What it establishes | What it does not establish |
|---|---|---|
| Render fixture | A supported UI state renders | Validation, persistence, or all transitions |
| `play` interaction | Exercised input and observable outcome | Unexercised server or browser paths |
| Visual comparison | Appearance against an intentional baseline | Semantic correctness or server behavior |
| Automated a11y | Supported rules on the rendered state | Complete keyboard, focus, or screen-reader behavior |

For accessibility changes, inspect the relevant keyboard order, label/error association, focus restoration, and announcements. Automated a11y reporting zero violations is not full accessibility approval. For visual changes, actually inspect the rendered surface rather than equating a story build with a visual pass.

A static story without `play`, a small provider-free primitive, or an intentional error-prop fixture is valid within its declared purpose. A story that claims successful saving solely because a fake callback returned, or succeeds only after another story seeded a cache, lacks the required evidence. Real auth, database writes, SSR/hydration/RSC and SEO remain the actual application's responsibility.

## Primary Sources

- [Storybook interaction tests](https://storybook.js.org/docs/writing-tests/interaction-testing), [play function](https://storybook.js.org/docs/writing-stories/play-function).
- [Mocking network requests](https://storybook.js.org/docs/writing-stories/mocking-data-and-modules/mocking-network-requests), [mocking providers](https://storybook.js.org/docs/writing-stories/mocking-data-and-modules/mocking-providers).
- [Accessibility testing](https://storybook.js.org/docs/writing-tests/accessibility-testing), [visual testing](https://storybook.js.org/docs/writing-tests/visual-testing).
