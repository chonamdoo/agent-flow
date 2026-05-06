# Review Angle — Compose Stability (Android)

Review Jetpack Compose code for recomposition correctness and stability.

## What to verify

1. **Stability annotations**
   - Are state-holder data classes `@Stable` or `@Immutable` where appropriate?
   - Are `List`/`Map` parameters using `ImmutableList` / `ImmutableMap` from `kotlinx.collections.immutable` (not raw `List<T>`)?

2. **Recomposition scope isolation**
   - Are state collection points pushed as far down the tree as possible (`collectAsStateWithLifecycle()` placed near the consuming composable, not at the screen root)?
   - Does a high-frequency state (input, scroll, animation) cause a parent composable to recompose unnecessarily?

3. **State hoisting decisions**
   - Are state owners chosen based on who needs to read AND modify, not just convention?
   - Are `StateFlow` references passed when collection at child level avoids parent recomposition (vs collecting at parent and passing values)?

4. **Strong Skipping Mode (Kotlin 2.0.20+) compatibility**
   - Does the change rely on `===` reference equality? Lambdas captured in stable scopes?
   - Are unstable parameters passed where they could be `remember`-ed?

5. **`remember` and `derivedStateOf` usage**
   - Are heavy computations memoized?
   - Is `derivedStateOf` used when a small derivation should not retrigger downstream when its inputs change but the result does not?

6. **Side effects**
   - Are `LaunchedEffect` keys correct (re-run when they should, not on every recomposition)?
   - Are `DisposableEffect` cleanups present for subscriptions/listeners?

## Output format

Same structure as other angles (Must-fix / Should-fix / Notes / Calibration). Reference relevant lore entries on Compose stability if they exist in `.agent-flow/memory/lore/`.

Keep under 200 lines.
