# Review Angle: Test Edges (Flutter / Dart)

Check missing edge-case tests, widget-test gaps, async cases, and
failure-path coverage.

## What to verify

1. **Empty / boundary inputs**
   - Empty list, empty string, `0`, and `null` handled by behavior tests, not
     only the happy path.
   - Min/max boundaries for paginated calls (first page, last page,
     beyond-last).

2. **Failure paths**
   - Repository failures (a `Future` that throws, a `Stream` that emits an
     error) are asserted, not swallowed.
   - The mapped domain error is asserted by type, not only by message.
   - Retry policy proven by a test, not by reading the code.

3. **Async and cancellation**
   - A superseded in-flight request is proven not to overwrite newer state.
   - `Stream` subscriptions are proven cancelled; a test fails if a listener
     survives teardown.
   - Tests await completion explicitly instead of sleeping with
     `Future.delayed`.
   - `fakeAsync` or a controllable clock is used for timer-driven behavior in a
     pure-Dart test; inside `testWidgets` the binding already supplies one, so
     the clock is advanced with `tester.pump(duration)` instead.

4. **Widget tests**
   - Every declared `UiState` branch has a rendered assertion — not-ready,
     loading, refreshing, placeholder, empty, error, success, offline, and
     permission-required, whichever the screen declares.
   - `pump` is called with an explicit duration where an animation never
     settles; a bare `pumpAndSettle` there fails with `pumpAndSettle timed out`.
   - Assertions use `find.bySemanticsLabel` or a stable key rather than
     matching user-visible copy that localization will change.
   - `tester.takeException()` is asserted where a layout or build error is
     the behavior under test.

5. **Test doubles**
   - Fakes preferred over generated mocks for a single-method dependency.
   - Riverpod dependencies replaced at one edge, not by rebuilding the graph:
     `ProviderScope(overrides: ...)` in a widget test, `ProviderContainer(
     overrides: ...)` with `addTearDown(container.dispose)` in a unit test.

6. **Goldens**
   - Golden tests only where the repo already keeps goldens for that surface.
   - A golden update is accompanied by the reason the pixels changed.

## Output format

```text
## Test-edge review findings

verdict: approve | request-changes

### Must-fix
- <severity:high> [path:line] <statement>. Missing case: <description>.

### Should-fix
- <severity:med> ...

### Notes
- <severity:low> ...
```

Cite paths as `path/to/file:line`. Keep total under 150 lines.

Emit exactly one unfenced final verdict line: `verdict: approve` or `verdict: request-changes`.
