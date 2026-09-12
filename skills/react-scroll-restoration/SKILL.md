---
name: react-scroll-restoration
description: Design, fix, or review React Web history-navigation scroll restoration, including virtualized or infinite lists, coordinate ownership, layout readiness, and obsolete restoration cancellation. Not ordinary scrolling, React Native lists, or a working browser/router restoration path that needs no change.
---

# React Scroll Restoration

Restore the intended reading position without fighting navigation, layout, or the user. Keep an existing browser or router solution when it meets the required behavior; custom restoration is a conditional choice, not the default.

## Establish the Restoration Contract

Before editing, inspect the installed router and virtualizer versions, current restoration code, actual scrolling element, and the failing navigation sequence. Distinguish history traversal from a new navigation, replacement, reload, and fragment navigation. Identify which of these the requested change covers.

Record the owner, saved-position identity, coordinate space, readiness signal, invalidation conditions, and expected behavior when restoration is impossible. Use existing route and content identities rather than prescribing a new storage schema. This step is complete when each scroll write in the affected flow has an accountable owner and a reason to occur.

## One Owner per Scroll Surface

- Let the browser, router, or application own restoration for a given surface and navigation. Coordinated ownership across separate scroll containers is valid; independent writers competing for the same surface are not.
- `history.scrollRestoration = 'auto'` permits browser restoration. Set `'manual'` only when the application or router deliberately takes responsibility for the relevant document history behavior. Do not disable native restoration merely because the application uses React.
- If taking ownership temporarily, preserve the prior setting and release it through the same owner. Component cleanup must not overwrite a setting that a newer owner now controls. Respect any existing router lifecycle instead of adding a competing mount effect.
- Preserve the distinction between revisiting a history entry and creating a new entry with the same URL. A pathname-only key is valid when intentional path-level persistence matches product behavior; it is insufficient when different queries, content, or visits need separate positions.
- Treat browser back-forward cache restoration separately from a newly mounted React tree. `pageshow` occurs on initial loads and returns, including back-forward cache returns; it is not proof that the page is visible, nor a signal to replay every saved position. Check the event's `persisted` state and whether the retained page already satisfies the target before applying custom restoration.

When using React Router's `ScrollRestoration`, consult the installed-version documentation: the current reference describes Framework and Data modes, one component per app, and `location.key` as the default restoration key. Its supported mode, imports, and placement are not a universal recipe for other routers or older releases.

## Position Identity and Coordinates

- Associate a saved position with the intended history entry or explicitly chosen reuse key, relevant content/query identity, and layout compatibility. Filtering, sorting, data replacement, changed row geometry, or a different scroll container can invalidate an old position even when the URL string is unchanged.
- Keep stable content identity separate from position. An item ID can identify the reading target after a reorder; a numeric row index only identifies that target while ordering remains compatible. Do not introduce domain IDs where a virtualizer requires its own supported index or snapshot representation: resolve between them at that boundary.
- Choose and name one coordinate reference for each saved representation: document offset, container offset, or stable item plus an offset relative to a defined edge. Capture and restore must agree on the reference and units.
- `getBoundingClientRect()` reports viewport-relative coordinates. For document coordinates, account for the current window scroll offset. A nested scroller needs its own origin and scroll offset; adding `window.scrollY` does not turn every rectangle into a container position. Account for borders, transforms, sticky content, and direction where the affected layout uses them.
- Convert only where the consuming scroll API requires another coordinate system. Avoid repeated conversion across storage, hooks, and virtualizer adapters; the same header or inset must not be subtracted twice.
- Save the outgoing surface before its position becomes unavailable. Do not capture the incoming page's reset position under the outgoing identity. Preserve framework-owned history state if the existing storage strategy uses it.

For virtualized or infinite lists, missing target data, or layout-dependent restoration, read [Virtualized and delayed layout](references/virtualized-and-delayed-layout.md).

## Readiness and Cancellation

Apply a saved target only while its navigation, content, and layout identity is current and the surface can represent it. A committed component or one animation frame alone does not prove that deferred data, fonts, images, or virtualizer measurements are ready.

Use existing data/layout readiness events rather than a blind timer or a retry loop. Pending work must stop when superseded by navigation, invalidation, unmount, or user interaction that takes control of the reading position. Distinguish user intent from scroll events caused by the restoration itself; do not cancel every programmatic scroll or repeatedly pull the user back after wheel, touch, scrollbar, or keyboard scrolling.

When the target no longer exists, apply the agreed fallback, such as a valid nearby anchor or the ordinary navigation position. Preserve a usable page instead of manufacturing success or repeatedly forcing an unreachable pixel coordinate. Do not add generic retry counts, pixel tolerances, expiration durations, or user-agent workarounds without evidence from this application's supported runtime.

## Observable Verification

Use the authorized browser/runtime and the actual affected surface. Capture the navigation sequence, content/layout identity, visible target, and final offset in the chosen reference; observing a scroll API call is not proof of restoration.

- Traverse away and back, then forward, with both ready and delayed content where applicable. The intended item or position should be visible without a second owner moving it afterward.
- Change the relevant filter, ordering, layout, or scroll container and traverse history. An incompatible saved position must not replay against unrelated content; a deliberately preserved compatible anchor remains valid.
- Start a pending restoration, then navigate elsewhere or scroll manually. A late data/layout completion must not move the new view or undo the user's chosen position.
- Exercise a reload, fragment link, or back-forward cache return when its ownership path changed. Record whether back-forward cache was actually used rather than assuming every Back action uses it.
- For nested scrollers, observe which element moved and whether unrelated window position was preserved. Check keyboard/focus behavior when restoration changes it; focus movement must not introduce a competing scroll.

Derived counterexamples: a stable document that restores correctly with browser `auto` needs no custom hook; an explicitly path-persistent reading view may reuse a pathname key; a virtual list with changing item heights may need an item anchor rather than a stale pixel offset. The defect is wrong identity, conflicting ownership, premature application, or late interference, not the absence of a favored library.

Report exercised browsers, router/virtualizer versions, observations, and untested navigation branches. If no suitable browser is available, provide the source-grounded change and precise missing runtime evidence without claiming visual verification. This skill does not authorize installation, deployment, external uploads, or unrelated routing changes.

## Sources and Applicability

Browser/API facts and router-specific statements are grounded in the following references, consulted on 2026-09-11. These are living documentation pages, not pinned package versions. Ownership, compatibility, and cancellation rules above are reusable engineering judgments; the derived examples are illustrative, not reported experiments.

- [MDN: History.scrollRestoration](https://developer.mozilla.org/en-US/docs/Web/API/History/scrollRestoration)
- [MDN: pageshow](https://developer.mozilla.org/en-US/docs/Web/API/Window/pageshow_event)
- [MDN: getBoundingClientRect](https://developer.mozilla.org/en-US/docs/Web/API/Element/getBoundingClientRect)
- [React Router: ScrollRestoration](https://reactrouter.com/api/components/ScrollRestoration)
