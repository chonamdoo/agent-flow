# Review Angle: Types and Data Contracts

Check language-appropriate type contracts, schema drift, nullability, and unsafe
casts. Apply only checks relevant to the language or languages in the diff; do
not impose Python, TypeScript, Kotlin, Swift, Dart, or Java conventions on a
different stack.

## What to verify

1. **Public contracts**
   - Public functions, methods, models, and exported values expose enough type
     information for callers to use them safely.
   - Generic/container types preserve their element types instead of falling
     back to raw or broadly dynamic values.
   - Broad escape hatches such as `Any`, `any`, `dynamic`, raw Java types, or
     untyped maps do not leak through public boundaries without justification.

2. **Nullability and absence**
   - Nullable/optional declarations match caller assumptions and runtime
     behavior (`None`, `null`, `nil`, optional values, or platform equivalents).
   - Missing, explicit null, and defaulted values are not silently conflated
     when the distinction affects behavior.
   - Defaults do not share mutable state between calls or instances.

3. **Structured boundaries**
   - External JSON, database rows, configuration, and IPC payloads become a
     typed or validated representation at the boundary instead of remaining
     unstructured deep inside the application.
   - Validation and decoding failures surface through the project's established
     error contract rather than leaking framework-specific errors unexpectedly.

4. **Casts and suppressions**
   - Forced casts and assertions are justified by a checked invariant, not used
     to silence the compiler or analyzer.
   - Suppressions are narrow and specific: examples include Python
     `typing.cast`/`# type: ignore`, TypeScript `as`/`@ts-ignore`,
     Kotlin/Swift forced casts, Dart `dynamic`, and Java unchecked casts.

5. **Schema drift**
   - API, database, event, and configuration shape changes propagate through
     models, decoders, adapters, and callers.
   - Compatibility handling is explicit and intentional; reflective or
     fallback access does not merely defer a predictable failure.

6. **Tool alignment**
   - Findings use the active stack's compiler, analyzer, or schema validator as
     evidence when available.
   - Do not request a language-specific annotation style when the repository
     uses a different established convention.

## Output format

```text
## Types review findings

verdict: approve | request-changes

### Must-fix
- <severity:high> [path:line] <statement>. Compiler/analyzer or runtime risk.

### Should-fix
- <severity:med> ...

### Notes
- <severity:low> ...
```

Cite paths as `path/to/file:line`. Keep total under 150 lines.

Emit exactly one unfenced final verdict line: `verdict: approve` or `verdict: request-changes`.
