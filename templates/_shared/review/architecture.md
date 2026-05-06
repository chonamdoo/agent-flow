# Review Angle — Architecture (general)

Review for architectural soundness independent of stack-specific patterns: module boundaries, coupling, cohesion, public surface, abstraction levels.

## What to verify

1. **Module/package boundaries**
   - Does the change respect existing module ownership? New types placed in the right module?
   - Are circular dependencies introduced? Does a new edge create a back-reference cycle?

2. **Public API surface**
   - Are new public types/functions intentional? Anything that could be `private`/`internal`?
   - Are breaking changes to public API justified and documented?

3. **Coupling**
   - Are there new transitive dependencies pulled in (heavyweight library for a small need)?
   - Does a feature module import from another feature module directly (should it go through a shared layer)?

4. **Cohesion**
   - Are the changes within a module thematically related, or has a single PR mixed unrelated concerns?
   - Does a class/file gain responsibilities beyond its name?

5. **Abstraction level**
   - Are abstractions earning their keep (3+ usages or known reuse path), or speculative?
   - Are concrete details escaping into abstract interfaces (interface signatures referencing concrete types)?

6. **Error / edge propagation**
   - Are errors propagated as types meaningful at the call site, or swallowed/rethrown as opaque exceptions?
   - Are nullable/optional decisions consistent with the module's idioms?

## Output format

```
## Architecture review findings

### Must-fix
- <severity:high> [path:line] <statement>. Why: <one sentence>.

### Should-fix
- <severity:med> ...

### Notes
- <severity:low> ...

### One thing this change got right (calibration)
- ...
```

Keep total under 200 lines. Cite paths as `path/to/file:line`.
