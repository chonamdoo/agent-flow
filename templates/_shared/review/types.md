# Review Angle: Types (Python)

Check type contracts, schema drift, nullable handling, and unsafe casts.

## What to verify

1. **Annotations**
   - Public functions / dataclasses fully annotated.
   - No bare `Any` in public surface; if used, justified inline.
   - Generic types parameterized (`list[str]`, not bare `list`).

2. **Optional / None**
   - Optional return types match callers' assumptions (no silent KeyError).
   - `Optional[X]` differs from `X | None`; consistent style chosen.
   - Defaults not mutable (`def f(items=[])` is a bug).

3. **Dataclass / TypedDict / Pydantic boundaries**
   - External JSON parsed into a typed container at the boundary, not
     accessed as `dict[str, Any]` deep inside the code.
   - Schema validation errors surface as the project's own exception type,
     not raw `ValidationError`.

4. **Casts / `typing.cast`**
   - Unavoidable casts are commented with why; no `cast` to silence mypy
     when the actual code can be fixed.
   - `# type: ignore` carries the specific rule and a justification.

5. **Schema drift**
   - DB / API response shape changes propagated through the dataclass /
     Pydantic model — no `getattr(resp, "new_field", None)` shims that
     defer the failure.

## Output format

```text
## Types review findings

verdict: approve | request-changes

### Must-fix
- <severity:high> [path:line] <statement>. Mypy or runtime risk.

### Should-fix
- <severity:med> ...

### Notes
- <severity:low> ...
```

Cite paths as `path/to/file:line`. Keep total under 150 lines.

Emit exactly one unfenced final verdict line: `verdict: approve` or `verdict: request-changes`.
