# Adaptive app bars

Each top-level destination owns its app-bar state and scroll behavior. Do not share one mutable scroll state across independently navigable screens.

## Choose the behavior

- `exitUntilCollapsedScrollBehavior`: collapse or hide while scrolling down, and remain collapsed while scrolling upward until the content returns to the top.
- `enterAlwaysScrollBehavior`: hide while scrolling down and reveal immediately when scrolling upward.

Choose from the product interaction, not window width alone. Preserve existing title, actions, insets, accessibility semantics, and navigation behavior.

## Integration checklist

1. Create the scroll behavior in the destination that owns the app bar.
2. Connect its nested-scroll connection to the correct scrolling container.
3. Ensure list, grid, and pane migrations do not disconnect nested scrolling.
4. Keep app-bar and navigation-area visibility decisions separate unless the product explicitly couples them.
5. Restore app-bar state when returning to a destination.

## Multi-pane considerations

Decide whether the scene has one shared app bar or pane-specific bars. Avoid duplicate back actions and titles. A detail shown in a secondary pane normally should not retain compact-only full-screen chrome rules.

## Verification

- Scroll down, partially up, and fully to the top for each behavior.
- Resize while the bar is collapsed and verify a coherent state.
- Test keyboard, mouse wheel, touch, and D-pad scrolling supported by the target.
- Check edge-to-edge insets and content occlusion in every supported window size.
