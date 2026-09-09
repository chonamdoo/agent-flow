# Review Angle: I/O Safety

Check filesystem, network, subprocess, and serialization boundaries for
crash safety, partial-write hazards, injection, and unintended disclosure.
Apply common semantics across languages; Python API guidance below applies only to Python.

## What to verify

1. **Filesystem**
   - Files that must not be half-written use the platform's atomic replacement mechanism; durability may also require syncing file/directory state. Check filesystem constraints and failure cleanup.
   - Enforce authorized roots against traversal, symlink escape, and check/use races at the actual open/write boundary. A declared root is not an OS sandbox.
   - Release locks/temporary resources on all paths and understand overwrite behavior.
   - Python: use `os.replace` where its atomic replacement semantics fit, `tempfile` contexts for temporary resources, and `finally` for owned locks; account for `Path.write_text(...)` overwriting.

2. **Subprocess**
   - Use executable/argv APIs and keep untrusted data out of shell interpretation. Apply least-privilege execution, scoped environment, bounded output, and timeouts; preserve useful redacted diagnostics.
   - Python: prefer `subprocess.run(..., shell=False)` with an argv list; handle `CalledProcessError` and `TimeoutExpired` distinctly and capture relevant stderr safely.

3. **Network / HTTP**
   - Set bounded connect/read/overall deadlines and concurrency. Bound retries and use backoff/jitter where repeated calls warrant it, but never blindly retry a write with unknown effect state; reconcile and establish idempotency first.
   - Preserve TLS verification and credential audience/scope. Defend SSRF through scheme/host/port policy, resolved address and redirect checks, and connection-time enforcement against DNS rebinding; protect internal/metadata services as policy requires.

4. **Serialization**
   - Validate untrusted input at the appropriate parser/framework boundary and map malformed data without leaking internals. Bound bytes/nesting and use safe formats; local try/catch is unnecessary if the established boundary already handles the failure correctly.
   - Python: handle `JSONDecodeError` from `json.loads` at that boundary, use `yaml.safe_load` for untrusted YAML, and load pickle only from trusted sources.
   - Keep tool output, fetched documents, and metadata as untrusted data rather than system instructions. Redact secrets/PII in outputs, logs, and errors.

5. **Encoding**
   - Choose an explicit wire/file encoding and handle BOM/CRLF when relevant to accepted inputs.
   - Python: specify `encoding="utf-8"` for UTF-8 text files rather than relying on locale.

## Output format

```text
## I/O safety review findings

verdict: approve | request-changes

### Must-fix
- <severity:high> [path:line] <statement>. Risk: <crash/data-loss/injection>.

### Should-fix
- <severity:med> ...

### Notes
- <severity:low> ...
```

Cite paths as `path/to/file:line`. Keep total under 150 lines.

Emit exactly one unfenced final verdict line: `verdict: approve` or `verdict: request-changes`.
