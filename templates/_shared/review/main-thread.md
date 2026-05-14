# Review Angle: Main Thread (iOS / Swift)

Check UI updates, state mutations, and async callbacks for correct
main-thread boundaries.

## What to verify

1. **UIKit / SwiftUI mutations**
   - Any `UIView` property mutation on background queue (frame, text, image,
     subviews) is a bug — must be `DispatchQueue.main.async` or `@MainActor`.
   - SwiftUI `@State` / `@Published` / `@Observable` writes must occur on
     main actor.

2. **`@MainActor` annotations**
   - Methods that touch UIKit / SwiftUI state declared `@MainActor` so the
     compiler enforces the boundary.
   - Callbacks from `URLSession`, `Combine`, third-party SDKs hop to main
     before touching view state (`receive(on: DispatchQueue.main)` or
     `await MainActor.run`).

3. **Async / await**
   - `Task { ... }` inside a `@MainActor` context inherits main actor.
     `Task.detached` does NOT — explicit hop required for UI updates.
   - `withTaskGroup` / `async let` results consumed on the right actor.

4. **CoreData / Realm / Heavy work**
   - Database fetches and large parsing kept off the main thread.
   - Image decoding / file IO off main.
   - Long-running work signals progress via main-actor publisher, not by
     blocking the run loop.

5. **Combine**
   - Publishers that touch UI end with `.receive(on: DispatchQueue.main)`
     before `.assign(to: ...)` to a `@Published`/UI binding.

## Output format

```text
## Main-thread review findings

verdict: approve | request-changes

### Must-fix
- <severity:high> [path:line] <statement>. Risk: <UI corruption / crash>.

### Should-fix
- <severity:med> ...

### Notes
- <severity:low> ...
```

Cite paths as `path/to/file:line`. Keep total under 150 lines.
