# Review Angle: Render Stability (React / Next.js / React Native)

Check render loops, hydration behavior, layout shifts, and state updates
during render.

## What to verify

1. **State updates during render**
   - Unconditional `setState`, or a guard that remains true on subsequent
     renders, can loop. Conditional, convergent adjustment of the currently
     rendering component's own state is a supported React pattern; check that
     the guard advances and render stays pure. Updating another component or
     an external store (including RHF `reset`) is not this exception.
   - Derive values from props/state during render rather than duplicating them
     with `useState` + `useEffect`; memoization is optional for real cost.

2. **Dependency arrays**
   - `useEffect` deps include every reactive value referenced inside.
   - Distinguish effect re-runs from a cycle: the effect updates state that
     changes its dependencies and causes the next update.
   - Put effect-only objects/functions inside the effect or depend on the
     actual primitives where appropriate. Use `useCallback`/`useMemo` only
     when identity creates real resubscription or render cost; keep deps complete.
   - ESLint `react-hooks/exhaustive-deps` not silenced without justification.

3. **Hydration (Next.js)**
   - Server output and the first client render agree. Independently generated
     timestamps/random values or browser-only render data can mismatch; a
     deterministic value produced once and passed to the client is valid.
   - Browser work may run in effects/events or replace a matching fallback
     after hydration. For genuinely client-only widgets, supported
     `dynamic(..., { ssr: false })` belongs inside a Client Component, not a
     Server Component. Do not disable SSR for already deterministic content.
   - `'use client'` is a client boundary, not an SSR off switch. `'use server'`
     marks Server Functions, not every Server Component.

4. **List keys**
   - List items use stable, unique `key`; not array index when items
     reorder or are inserted in the middle.
   - RHF field-array rows use their generated `field.id` (or installed-version
     configured key property); ordinary domain lists retain domain-ID keys.

5. **Layout shifts (Web)**
   - Images include `width` / `height` or aspect-ratio CSS to reserve space.
   - Web fonts use `font-display: swap` or `optional`; no FOIT.

6. **React Native specific**
   - `FlatList` over `ScrollView` + `.map` for long lists.
   - For React Native core `Animated`, choose the native driver for supported
     opacity/transform property and event graphs, preserving driver consistency
     on the same animated value. Unsupported layout/gesture graphs may need
     the JS driver; do not impose this option on Reanimated or Web CSS.
   - Avoid repeated heavy render work on the changed path; memoized selectors
     are an option when measured cost or real identity risk warrants them.

## Sources

- [React render-time state adjustment](https://react.dev/reference/react/useState#storing-information-from-previous-renders)
- [React effect cycles and dependencies](https://react.dev/reference/react/useEffect#my-effect-keeps-re-running-in-an-infinite-cycle)
- [Next.js lazy loading](https://nextjs.org/docs/app/guides/lazy-loading)
- [React Native core Animated native driver](https://reactnative.dev/docs/animations#using-the-native-driver)

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
