# Review Angle — Compose Stability (Android)

Review Jetpack Compose code for recomposition correctness and stability.

## What to verify

1. **Stability annotations**
   - Are state-holder data classes `@Stable` or `@Immutable` where appropriate?
   - Are `List`/`Map` parameters using `ImmutableList` / `ImmutableMap` from `kotlinx.collections.immutable` (not raw `List<T>`)?

2. **Recomposition scope isolation**
   - Is the **read** of a state value as close to its consumer as possible, so one value change does not recompose the whole screen? Lambda parameters, `derivedStateOf`, and deferred reads inside `Modifier` are the tools; narrowing the read is the goal.
   - Does flow **collection** stay at the route or top-level composable? `.agent-flow/skills/android-clean-presentation-architecture/SKILL.md` Compose Screen Rule owns that boundary: pushing `collectAsStateWithLifecycle()` into a content composable is a UDF violation, not an optimization. Narrow the read, never the collection point.
   - Does a high-frequency state (input, scroll, animation) cause a parent composable to recompose unnecessarily?

3. **State hoisting decisions**
   - Are state owners chosen based on who needs to read AND modify, not just convention?
   - Where a child owns UI-local state, is the owner the lowest composable that both reads and writes it? Do not hand a state holder's flow to a child so it can collect there — hoist the value, or pass a lambda that defers the read.

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

Same structure as other angles (Must-fix / Should-fix / Notes / Calibration).

Keep under 200 lines.

Emit exactly one unfenced final verdict line: `verdict: approve` or `verdict: request-changes`.
