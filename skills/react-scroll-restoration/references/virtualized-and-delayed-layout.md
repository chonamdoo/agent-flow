# Virtualized and Delayed Layout

Use this branch when the target is outside the rendered range, earlier pages are absent, item dimensions change after rendering, or the scrolling surface is not the document. These are library-neutral design judgments, not an API contract for a particular virtualizer. Read the installed library's official restoration and measurement documentation before choosing an API; a ref method copied from another library or version is not evidence of compatibility.

## Select the Saved Representation

| Existing capability and layout | Suitable approach | Compatibility question |
| --- | --- | --- |
| A virtualizer provides a documented restoration snapshot | Reuse that API rather than reconstructing its internal measurements | Does its contract require the same data, item count, dimensions, or viewport? |
| Stable items but changing order or row heights | Preserve an item identity and a defined relative offset; resolve to the current library coordinate at consumption | Does the item still exist, and is its relative offset meaningful in this layout? |
| Stable fixed geometry with a compatible data window | A container offset or supported index-based target can be sufficient | Is the data order and coordinate origin unchanged? |
| Target data cannot be loaded or no longer exists | Use the agreed fallback and terminate the obsolete attempt | What usable position does the navigation contract permit? |

Do not replace a working library snapshot with an item-anchor implementation merely because the latter is described here. Conversely, an index-only record cannot identify a particular item after an incompatible reorder.

## Make Readiness Explicit

1. Resolve the saved identity against the incoming content. A prior filter's page cursor or offset must not populate a new query's list.
2. Determine whether the target's data is available. Use the existing pagination/cache contract to obtain the required window when authorized and supported; do not invent unbounded page fetching to recover an old position.
3. Wait for the current scroll container and the virtualizer's supported measurement or restoration readiness. A target's data existing in memory does not mean its DOM row exists; DOM queries cannot locate an unrendered item.
4. Apply the target through the chosen owner and documented API. If rendering changes measurements, use the library's reconciliation mechanism or a layout-ready correction with the same identity and cancellation checks. Do not run a competing raw window scroll alongside virtualizer control.
5. Complete when the intended anchor and relative position are observed, or when the agreed fallback is applied. Terminate when the attempt becomes obsolete or the required target cannot be represented; a pending flag must not keep the user trapped in restoration.

These stages describe required ordering, not a mandated state-machine class, hook, number of passes, or scheduler. Reuse the application's existing lifecycle and cancel its observers, pending callbacks, and subscriptions when ownership ends.

## Layout Changes That Matter

- Late images, fonts, translated text, expanded content, and responsive widths can change heights above the target. Reserve known geometry where the application already has it; otherwise reconcile using the actual supported layout signal.
- A sticky header changes what the user can see, not necessarily the scroll API's coordinate origin. Define whether the anchor offset is relative to the scroller edge or its unobscured reading area.
- Preserve distinct window and nested-container positions. If the application switches from one scrolling surface to another at a breakpoint, explicitly establish compatibility or invalidate the old representation.
- Let real user scrolling take control during data or measurement delays. A delayed callback must check current ownership and identity again before writing, even if its initial request was valid.

## Evidence for This Branch

Observe restoration into an initially unrendered item, then repeat with changed row dimensions or ordering relevant to the task. Record the final visible item and relative position rather than only a virtualizer index or a successful promise. Interrupt the delayed path with a new navigation and with manual scrolling; neither should be reversed by late completion. If the library/version documentation or target data is unavailable, state which API or compatibility claim remains unresolved instead of inventing a helper or a successful reproduction.
