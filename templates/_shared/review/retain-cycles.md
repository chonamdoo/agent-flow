# Review Angle: Retain Cycles (iOS / Swift)

Check closures, delegates, async tasks, and observers for ownership cycles
or leaks.

## What to verify

1. **Closures capturing `self`**
   - Escaping closures stored in properties capture `self` strongly — use
     `[weak self]` or `[unowned self]` deliberately.
   - `Task { [weak self] in ... }` for long-lived tasks owned by view models.
   - Combine `sink` and `assign(to:on:)` capture closures — apply `[weak self]`.

2. **Delegates**
   - Delegate properties declared `weak var delegate: ...?` for two-way
     references.
   - Notification observers removed in `deinit` (NotificationCenter does not
     hold weakly by default; the block-based API requires explicit removal).

3. **Combine / async**
   - `AnyCancellable` stored in a `Set<AnyCancellable>` owned by the object
     that should keep the subscription alive — not leaked into a global.
   - `Task.cancel` called on view-model `deinit` to halt outstanding work.

4. **Timers / DisplayLink**
   - `Timer.scheduledTimer` retains its target; either use the
     block-based variant with weak self or invalidate in `deinit`.
   - `CADisplayLink` requires explicit `invalidate()`.

5. **CoreData / NSCache**
   - Long-lived NSManagedObjectContext / cache observers torn down on screen
     dismissal.

## Output format

```text
## Retain-cycle review findings

verdict: approve | request-changes

### Must-fix
- <severity:high> [path:line] <statement>. Cycle path: A → B → A.

### Should-fix
- <severity:med> ...

### Notes
- <severity:low> ...
```

Cite paths as `path/to/file:line`. Keep total under 150 lines.
