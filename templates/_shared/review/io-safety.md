# Review Angle: I/O Safety (Python)

Check filesystem, network, subprocess, and serialization touch points for
crash safety, partial-write hazards, and injection.

## What to verify

1. **Filesystem**
   - Writes use `os.replace` (atomic) for files that must not be half-written.
   - Temporary files cleaned up on exception (use `tempfile` context).
   - `Path.write_text(...)` consequences understood (overwrites silently).
   - Lock files / mkdir-locks released in `finally`.

2. **Subprocess**
   - `subprocess.run(..., shell=False)` and argv list passed (no `shell=True`
     with user input).
   - Timeouts set; `CalledProcessError` and `TimeoutExpired` handled distinctly.
   - stderr captured before raising so the error message is preserved.

3. **Network / HTTP**
   - Connect/read timeouts set explicitly (no infinite hangs).
   - Retries bounded; backoff with jitter where called repeatedly.
   - TLS certificate verification not disabled silently.

4. **Serialization**
   - `json.loads` over user input wrapped in try/except for `JSONDecodeError`.
   - `yaml.safe_load`, never `yaml.load`.
   - `pickle.load` only over trusted sources.

5. **Encoding**
   - File reads/writes specify `encoding="utf-8"` (don't rely on locale).
   - BOM / CRLF handled if file may come from Windows tools.

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
