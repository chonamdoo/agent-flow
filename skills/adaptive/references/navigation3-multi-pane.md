# Navigation 3 multi-pane scenes

Use Navigation 3 `SceneStrategy` for adaptive multi-pane navigation. Do not implement this workflow with `ListDetailPaneScaffold` or `SupportingPaneScaffold`.

## Choose a relationship

Use list-detail when selecting an item in a collection opens the item's primary detail, as in mail, notes, or messages. Avoid it by default when the detail is media-heavy and benefits from dedicated full-screen space.

Use a supporting pane when a main destination remains primary and a secondary destination provides complementary information or controls.

## List-detail

1. Add the project's compatible Material 3 Adaptive Navigation 3 dependency.
2. Create and remember a `ListDetailSceneStrategy`.
3. Pass it to `NavDisplay` through `sceneStrategies`.
4. Mark the list entry with `ListDetailSceneStrategy.listPane(...)` metadata.
5. Provide a useful `detailPlaceholder` for the no-selection state.
6. Mark detail entries with `ListDetailSceneStrategy.detailPane()` metadata.

Behavior constraints:

- Keep compact navigation behavior unchanged when only one pane fits.
- Do not render a detail back arrow while the detail is already visible beside the list.
- Turn off compact-only immersive presentation when the detail participates in a multi-pane scene.
- Preserve the selected item's stable identity across resize and state restoration.

## Supporting pane

1. Create and remember a `SupportingPaneSceneStrategy`.
2. Register it with `NavDisplay.sceneStrategies`.
3. Mark the primary entry with `SupportingPaneSceneStrategy.mainPane()` metadata.
4. Mark the complementary entry with `SupportingPaneSceneStrategy.supportingPane()` metadata.
5. Keep the main content usable when the supporting pane is absent on compact windows.

## State and navigation review

- Navigation state, not ad-hoc width branches, decides which entries are active.
- A window-size transition must not duplicate destinations or lose the current selection.
- Back removes the correct navigation entry in compact mode and does not fight the scene's pane composition in expanded mode.
- Pane-specific state must use stable keys and survive configuration changes.

## Verification matrix

Capture compact and expanded screenshots for no selection, active selection, back navigation, and restored state. Resize while a detail or supporting entry is active and verify that content moves between single- and multi-pane presentations without re-triggering domain work.
