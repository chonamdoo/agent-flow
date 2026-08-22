# Review Angle: Rebuild and Layout (Flutter / Dart)

Check rebuild scope, constraint failures, disposal, and state read during
build.

## What to verify

1. **Rebuild scope**
   - `setState` sits on the smallest `StatefulWidget` that owns the changing
     value, not at the top of a large `build`.
   - The changing subtree is its own widget, so an unchanged sibling is not
     rebuilt with it.
   - A widget whose fields are all final has a `const` constructor, so an
     unchanged subtree can skip rebuilding.
   - Heavy work (parsing, sorting, formatting) is hoisted out of `build`.

2. **Constraints**
   - An unbounded `Column` / `Row` child uses `Expanded` or `Flexible`, or
     sits under a bounded parent — not a hardcoded size guessed from the
     overflow message.
   - A `ListView` / `GridView` nested in a scrollable declares `shrinkWrap`
     or a bounded height deliberately, not to silence a layout error.
   - Size adaptation goes through `LayoutBuilder` / `MediaQuery` or the
     repo's breakpoint helper, not a platform branch.

3. **List and image rendering**
   - `ListView.builder` / `SliverList` for data-driven lengths; `itemBuilder`
     stays cheap.
   - Items use stable domain-id keys, not the index, when they can be
     inserted, removed, or reordered.
   - Remote images and futures carry loading and error builders.
   - `RepaintBoundary` only where a measured repaint problem exists.

4. **BuildContext across async gaps**
   - `mounted` re-checked before `setState`, `Navigator`, `ScaffoldMessenger`,
     or `Theme.of` on the far side of an `await`.
   - A captured `BuildContext` is not stored in a field or a closure that
     outlives the element.

5. **Disposal**
   - Every `AnimationController`, `FocusNode`, `TextEditingController`, and
     `ScrollController` the `State` owns is disposed.
   - Every `StreamSubscription` and `Timer` is cancelled in `dispose`.

6. **State reads**
   - A provider that affects widget output is watched at the render boundary
     rather than read once and cached in a field. A state holder reads its
     dependencies through `ref`.
   - A provider is declared at library top level, not constructed inside
     `build`.

## Output format

```text
## Rebuild review findings

verdict: approve | request-changes

### Must-fix
- <severity:high> [path:line] <statement>. Symptom: <overflow / leak / stale context / jank>.

### Should-fix
- <severity:med> ...

### Notes
- <severity:low> ...
```

Cite paths as `path/to/file:line`. Keep total under 150 lines.

Emit exactly one unfenced final verdict line: `verdict: approve` or `verdict: request-changes`.
