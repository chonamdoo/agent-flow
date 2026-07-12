---
name: adaptive
description: Makes Jetpack Compose interfaces adapt across phones, tablets, foldables, desktop, TV, Auto, and XR using Navigation 3 scenes, adaptive navigation, responsive lists, Grid, FlexBox, and MediaQuery. Use when implementing or reviewing Android layouts for multiple window sizes, input devices, capabilities, or multi-pane navigation.
---

# Adaptive Compose UI

Apply only the sections relevant to the requested screen. Preserve existing behavior before changing layout structure.

## Prerequisites

- The affected screens use Jetpack Compose. For Views or Fragments, propose migration before applying Compose-only APIs.
- Multi-pane work uses Jetpack Navigation 3. Do not substitute legacy adaptive pane scaffolds for Navigation 3 `SceneStrategy`.
- Check the project's Compose and Material 3 Adaptive versions before selecting APIs. Ask before introducing an experimental API.

## Quick start

1. Establish screenshot or preview coverage for phone, foldable, tablet, and desktop-sized windows.
2. Adapt top-level navigation while preserving visibility and full-screen rules.
3. Model related destinations as Navigation 3 list-detail or supporting-pane scenes when appropriate.
4. Adapt repeated content using lazy adaptive columns or non-lazy Grid/FlexBox as appropriate.
5. Query actual window and input capabilities with MediaQuery when size alone is insufficient.
6. Keep each top-level destination's app-bar scroll state independent.
7. Build and run local tests. Run screenshot tests, but do not update golden images without user review.

## Progressive references

- Read [workflow-and-navigation.md](references/workflow-and-navigation.md) for baseline verification, adaptive navigation areas, visibility, and full-screen behavior.
- Read [navigation3-multi-pane.md](references/navigation3-multi-pane.md) for list-detail and supporting-pane `SceneStrategy` guidance.
- Read [layouts-and-capabilities.md](references/layouts-and-capabilities.md) for adaptive lists, Grid, FlexBox, MediaQuery, and target sizing.
- Read [app-bars.md](references/app-bars.md) when changing collapsing or hide-on-scroll app bars.

## Review constraints

- Do not select a layout from device labels alone when window size or capabilities can change at runtime.
- Do not show a detail back arrow while that detail is visible in a list-detail pane.
- Do not keep mobile-only full-screen suppression when the same content is shown beside another pane.
- Preserve navigation-area visibility rules such as camera, media, and scroll-driven hiding.
- Verify keyboard, pointer, focus, and touch target behavior when the target device supports them.
