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

7. **Workflow / process gates** (profile-driven)
   - Does the PR target match `profile.pr.target_branch`? For `release-first` profiles, verify the target is the active `release/*` branch, not `main` or a stale release line.
   - When `profile.branching.naming` is set, does the working branch follow `<prefix><slug>` per the configured `slug_style`?
   - Are required `profile.gates` represented in the verification evidence?

## Output format

```text
## Architecture review findings

### Must-fix
- <severity:high> [path:line] <statement>. Why: <one sentence>.

### Should-fix
- <severity:med> ...

### Notes
- <severity:low> ...

### One thing this change got right (calibration)
- ...

### Overall
verdict: approve | request-changes
```

Keep total under 200 lines. Cite paths as `path/to/file:line`.

Emit exactly one unfenced final verdict line: `verdict: approve` or `verdict: request-changes`.
