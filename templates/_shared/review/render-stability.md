# Review Angle: Render Stability (React / Next.js / React Native)

Check render loops, hydration behavior, layout shifts, and state updates
during render.

## What to verify

1. **State updates during render**
   - `setState` called directly in the function body (not in an effect or
     event handler) → infinite render loop.
   - Derived state computed via `useMemo` or render-time, not duplicated in
     `useState` + `useEffect`.

2. **Dependency arrays**
   - `useEffect` deps include every reactive value referenced inside.
   - Functions / objects in deps are stable (`useCallback` / `useMemo`)
     to avoid spurious re-runs.
   - ESLint `react-hooks/exhaustive-deps` not silenced without justification.

3. **Hydration (Next.js)**
   - Server and client render the same HTML on first paint; no `Date.now`
     / `Math.random` / `window` access during SSR.
   - `dynamic(() => ..., { ssr: false })` used for client-only widgets
     instead of `if (typeof window !== 'undefined')` gates.
   - `'use client'` / `'use server'` boundaries explicit.

4. **List keys**
   - List items use stable, unique `key`; not array index when items
     reorder or are inserted in the middle.

5. **Layout shifts (Web)**
   - Images include `width` / `height` or aspect-ratio CSS to reserve space.
   - Web fonts use `font-display: swap` or `optional`; no FOIT.

6. **React Native specific**
   - `FlatList` over `ScrollView` + `.map` for long lists.
   - `useNativeDriver: true` for opacity / transform animations.
   - Heavy work hoisted out of render via memoized selectors.

## Output format

```text
## Render-stability review findings

verdict: approve | request-changes

### Must-fix
- <severity:high> [path:line] <statement>. Symptom: <loop / hydration mismatch / jank>.

### Should-fix
- <severity:med> ...

### Notes
- <severity:low> ...
```

Cite paths as `path/to/file:line`. Keep total under 150 lines.

Emit exactly one unfenced final verdict line: `verdict: approve` or `verdict: request-changes`.
