# Wear Material 3 verification

## Static review

- [ ] No unintended phone Material or Wear Material 2.5 imports remain.
- [ ] Horologist UI/layout/theme usage is removed from migrated screens.
- [ ] Exactly one `AppScaffold` surrounds navigation.
- [ ] Every screen uses its own `ScreenScaffold` where appropriate.
- [ ] Scrollable content uses `TransformingLazyColumn`, not
  `ScalingLazyColumn`.
- [ ] `ScreenScaffold` padding reaches the list.
- [ ] Morphing items use `transformedHeight`, `SurfaceTransformation`, and the
  component default padding token.
- [ ] `EdgeButton` is in the scaffold slot.
- [ ] Snap fling and rotary behavior are configured together.
- [ ] Theme and `*Defaults` tokens replace hard-coded style values.
- [ ] New navigation uses Navigation 3's Wear swipe-dismiss scene strategy.

## Build and behavior

1. Run Gradle dependency resolution and confirm a single compatible Wear
   Compose family.
2. Compile affected modules and run unit, lint, and Compose UI tests.
3. Run on round and square Wear targets where supported.
4. Exercise touch, rotary input, focus, swipe-to-dismiss, Back, and scrolling.
5. Test normal, largest supported, and at least one intermediate font scale.
6. Verify ambient transitions and time/scroll-indicator coordination.
7. Verify pager indicators, edge buttons, overscroll, and snap behavior.
8. Run screenshot tests and inspect expected Material 3 differences; do not
   distort component defaults solely to match Material 2.5 goldens.

## Migration evidence

Record the stable dependency version, successful sync, exact extracted sample
file, API opt-ins, affected screenshots, and test commands. An editor showing
no errors is not sufficient evidence; use Gradle and target-device behavior.

If an unresolved reference remains after sync, compare imports and arguments
against the version-matched local sample and inspect dependency resolution.
Changing to an older version without that evidence is not an acceptable fix.
