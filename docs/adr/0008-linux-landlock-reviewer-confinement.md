# 0008 Linux Landlock Confinement for Read-Only Provider Runs

## Status

Accepted

Supplements [ADR 0004](0004-exclude-sandboxed-ai-cli-execution.md). Tracks [#422](https://github.com/chonamdoo/agent-flow/issues/422).

## Context

Managed providers run under a write sandbox; until now the only verified backend was macOS `sandbox-exec`, so every Linux host failed closed at `multi-review` (field run stopped at phase 13 of 24). Reviewers run with `allow_workspace_writes=False`, so the rule actually applied to them is "reads allowed, writes only to the private scratch and `/dev/null`". Landlock expresses that rule without privileges and works inside a default Docker container; the Claude Code cloud environment reported ABI 7 (kernel 6.18, 2026-10-22).

## Decision

On Linux with Landlock ABI ≥ 3 (Linux 6.2; ABI 1 and 2 cannot handle truncation, so a reviewer could empty any file), `verify_provider_sandbox_backend(allow_workspace_writes=False)` returns `providers/landlock_launch.py`. The provider argv becomes `python -I landlock_launch.py --write <scratch> -- <argv>`; the launcher handles every write-class access the ABI knows (including `REFER` and `TRUNCATE`), allows only the scratch (all write rights) and `/dev/null` (file writes), sets `PR_SET_NO_NEW_PRIVS`, restricts itself, and `exec`s the provider. Any failure exits 126 before the provider starts.

Runs that write the workspace stay fail closed on Linux. Landlock cannot re-deny a path beneath an allowed directory, so it cannot express the macOS profile's protected paths inside the checkout (`.git` pointer, `.agent-flow`, host state folders).

## Consequences

- Linux hosts with Landlock run `multi-review` reviewers; the start banner and `available` line stop reporting them as blocked.
- The confinement is per process tree, not a container. Host authentication and the ADR 0004 configuration model are unchanged.
- Escape tests (`tests/test_landlock_launch.py`) run for real on Linux CI: writes, creates, truncation, symlink and rename escapes, `mkdir`, `.git` pointer and host-state writes, and child processes are denied; reads and `/dev/null` stay allowed.
- Kernels without Landlock (some sandboxed kernels such as gVisor) or below ABI 3 keep failing closed.

## Known Limits

- Landlock does not restrict metadata: a reviewer can still `chmod`, `utime` or `setxattr` paths outside the scratch. macOS `file-write*` denies these. File contents, creation, removal, renames and links stay denied. For the leader, the multi-review snapshot comparison still runs after reviewers finish; sibling checkouts have no after-the-fact check. Closing this needs a seccomp filter that also blocks `chmod` inside the scratch, which the provider CLIs may need. That decision is deferred.
- Descriptors inherited at `exec` keep their access. The only one passed on purpose is the provider-lease slot (an empty lock file, `O_RDWR`). Writing to it changes nothing the runner reads, but `flock(LOCK_UN)` on it releases the slot early. This is the same on macOS.

## Alternatives Considered

### bubblewrap or user namespaces

Needs namespace creation, which default Docker and most cloud sandboxes refuse (`unshare -U` EPERM; bubblewrap fails without uid map rights).

### `preexec_fn` in the runner

The parent is multi-threaded; running Landlock setup between `fork` and `exec` there is unsafe. A separate launcher keeps setup in a single-threaded process.
