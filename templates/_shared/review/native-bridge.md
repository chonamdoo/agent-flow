# Review Angle: Native Bridge (React Native)

Check native module boundaries, serialization, lifecycle handling, and
platform-specific failures.

## What to verify

1. **Bridge payload shape**
   - Only JSON-serializable values cross the bridge (no functions, no
     Date, no class instances).
   - Large payloads avoided; binary data passed as `ArrayBuffer` /
     base64 with explicit size bounds.

2. **Threading**
   - Native module methods invoked from the JS thread; long native work
     runs on a separate dispatcher (iOS `dispatch_queue_create`, Android
     executor), not blocking the UI thread.
   - Callbacks/promises resolved on the right thread back to JS.

3. **Lifecycle**
   - Listeners added in `componentDidMount` / `useEffect` cleaned up on
     unmount; otherwise leak across screen transitions.
   - Background/foreground transitions handled (`AppState` events) for
     resources that must pause.

4. **Platform parity**
   - Both iOS and Android implementations present; if one diverges, the
     JS-side calls fall back gracefully (`Platform.select`).
   - Permissions requested per platform (iOS Info.plist, Android manifest
     + runtime request).

5. **Error propagation**
   - Native errors surface as rejected promises with stable error codes,
     not raw exception strings.
   - JS-side handles permission-denied / not-supported distinctly from
     unknown failure.

6. **Turbo / Fabric / New Architecture**
   - Codegen specs match runtime method signatures.
   - JSI hostobjects don't capture JS refs beyond their lifetime.

## Output format

```text
## Native-bridge review findings

verdict: approve | request-changes

### Must-fix
- <severity:high> [path:line] <statement>. Risk: <crash / leak / parity break>.

### Should-fix
- <severity:med> ...

### Notes
- <severity:low> ...
```

Cite paths as `path/to/file:line`. Keep total under 150 lines.
