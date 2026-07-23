# Active-Worktree Capability Boundary Implementation Plan

> **For Hermes:** Implement this plan task-by-task with TDD; treat the run binding and sandbox as authorization boundaries, not convenience wrappers.

**Goal:** Replace the growing shell-command allowlist with an authenticated active-worktree write boundary, a trusted sandboxed command route, explicit artifact publication, and fast changed-file test routing.

**Architecture:** Structured editing tools continue to declare file paths and are authorized only after resolving those paths against the active execution’s authenticated workspace identity. Arbitrary build, Git, interpreter, and media commands move behind the existing `agent-flow gate -- <argv>` sandbox route; no raw host shell parser may certify those commands as safe. Final artifact export remains a separate, identity-bound capability and is generalized from APK-only semantics only after its source/destination contract is verified.

**Tech Stack:** Python 3.10+ (`src/agent_flow/core/workspace_boundary.py`, hook script, pytest/xdist); Node (`bin/agent-flow-kit.mjs`, Node test runner); Linux `bubblewrap` or macOS `sandbox-exec`.

---

## Current facts and non-negotiable constraints

- Worktree: `/opt/data/agent-flow-issue-87`, branch `fix/issue-87-contract-control-plane`; only this worktree may be changed.
- Existing uncommitted guard changes are not security-approved. Do not commit, push, reset, or silently discard them during this refactor.
- `resolve_mutation_path()` already provides the desired resolved-path containment primitive for structured writes (`src/agent_flow/core/workspace_boundary.py:1647`).
- `resolve_execution_workspace()` already authenticates active execution → workspace/run binding (`src/agent_flow/core/workspace_boundary.py:772`).
- `agent-flow gate -- <argv>` already has a sandbox implementation (`bin/agent-flow-kit.mjs:11855`), with worktree-local HOME/TMP/Gradle state. On Linux it requires a trusted `/usr/bin/bwrap`; current development host does not have it.
- `export-apk` is already a separate identity-bound, TOCTOU-hardened publication capability (`bin/agent-flow-kit.mjs:11578-11833`).
- Current parser hardening has demonstrated recurring unsafe surfaces (`git config`, pagers, fsmonitor, interpreter start hooks). No further raw-command allowlist expansion is acceptable.
- Raw Git/worktree lifecycle operations must be represented by authenticated lifecycle commands, not parser exceptions.

## Target policy

| Operation | Authorization |
|---|---|
| Structured write/edit/patch | Resolved target must be contained by the authenticated active worktree, or be a declared run artifact path. |
| Arbitrary shell/build/Git/Python/Node/media command | Must run via authenticated `agent-flow gate -- <argv>` sandbox; raw host command is denied. |
| Build cache/temp state | Worktree-local `.agent-flow/gate-runtime/**`; no inherited host HOME/TMP/cache write paths. |
| Artifact publish | Explicit identity-bound source and named destination capability; no generic `mv` outside the worktree. |
| Merge/worktree removal/branch deletion | Existing validated lifecycle API/launcher only; never an unrestricted shell exception. |
| No sandbox available | Deny arbitrary shell mutation and report the missing trusted sandbox prerequisite. Structured file writes still work. |

## Task 1: Lock the desired behavior in focused RED tests

**Objective:** Demonstrate that raw shell authorization is not based on an ever-growing command parser, while structured writes preserve current-worktree behavior.

**Files:**
- Modify: `tests/test_pinned_workspace_boundary.py`
- Modify: `tests/test_custom_skill_install.py`
- Test: same files

**Step 1: Add failing hook tests**

Add tests proving:

1. a direct structured write under the authenticated linked worktree remains accepted;
2. a structured write to leader, sibling worktree, `/tmp`, or a symlink escape remains denied;
3. an unsandboxed raw shell invocation of `git status`, `python -c`, Gradle, `cwebp`, or an unknown wrapper is denied with a stable `sandboxed gate`/`raw shell` reason, rather than conditionally accepted by parser grammar;
4. a trusted project launcher `agent-flow gate -- ...` remains recognized as the only generic shell route;
5. gate execution without trusted bwrap/sandbox fails closed and does not create an outside marker.

**Step 2: Run RED loop**

```bash
uv run --with pytest --with pytest-xdist python -m pytest -q -o addopts='' \
  tests/test_pinned_workspace_boundary.py -k 'raw_shell or sandboxed_gate or structured'
```

Expected: the new raw-shell tests initially fail because existing `READ_ONLY_SHELL_COMMANDS`/`_shell_mutation_paths()` still grants parser-derived approvals.

## Task 2: Make the hook capability-first

**Objective:** Remove parser-derived authorization of generic shell commands while retaining direct structured target validation and trusted launcher recognition.

**Files:**
- Modify: `scripts/hooks/guard-worktree-write.py:20-170,3698-3789`
- Test: `tests/test_pinned_workspace_boundary.py`

**Step 1: Preserve authenticated structured paths**

Keep the existing `main()` path for write/edit/patch payloads:

```python
for requested in _requested_paths(tool_input):
    target = _resolve_within_pinned(...)
    _ensure_leader_private_run_binding(...)
```

It must remain the only direct host-write authorization route.

**Step 2: Replace raw shell parser authorization**

After startup-environment checks and trusted `agent-flow` launcher verification:

```python
raise ValueError(
    "write boundary rejected: run arbitrary commands through the trusted sandboxed agent-flow gate"
)
```

Do not retain `READ_ONLY_SHELL_COMMANDS`, `_read_only_command_arguments_are_safe`, or `_shell_mutation_paths()` as an authorization mechanism. They may remain temporarily only if referenced by diagnostics/tests, but must not grant an allow decision.

**Step 3: Keep fail-closed behavior**

- `agent-flow gate` must be validated as the authenticated project launcher before bypassing the raw-shell denial.
- No PATH-relative, source, interpreter, Git, or wrapper exception may bypass this rule.
- A trusted launcher remains responsible for validating its execution binding and sandbox prerequisites.

**Step 4: Verify GREEN**

Run the focused tests from Task 1 plus:

```bash
python3 -m py_compile scripts/hooks/guard-worktree-write.py
```

## Task 3: Harden the sandbox capability contract

**Objective:** Ensure the existing sandbox route is the mandatory execution surface and has no inherited host write/cache escape.

**Files:**
- Modify: `bin/agent-flow-kit.mjs:11855-11935`
- Modify: `tests/test_custom_skill_install.py:8009+` and/or narrowly scoped Node tests

**Step 1: Add RED tests**

Test that a sandboxed command attempting to create an external marker is denied or leaves no external marker. Test inherited `HOME`, `TMPDIR`, Gradle, Node, and Python cache variables are overridden/sanitized. Keep platform-unavailable test behavior explicit: no trusted sandbox means a fail-closed error, not a skipped policy path.

**Step 2: Define the sandbox contract**

- Linux: use only `trustedLinuxBubblewrap()` and bind the active worktree RW over a host root mounted read-only.
- macOS: use `sandbox-exec` write deny plus active worktree write allow.
- Set worktree-local HOME/TMP and language cache roots.
- Do not pass raw command strings through a shell; preserve argv execution.
- Document/encode that device/network access is out of scope for this first filesystem boundary and must become separate capabilities.

**Step 3: Implement only the missing sanitizer/validation**

Reuse `runSandboxedGate()` rather than create a second executor. Do not attempt a fallback unsandboxed executor.

**Step 4: Verify**

Run the exact sandbox contract tests. If trusted bwrap is unavailable on this host, verify the fail-closed error path locally and run the positive sandbox proof only in an environment with bwrap.

## Task 4: Generalize explicit artifact publication without broad host writes

**Objective:** Support APK, WebP, archives, reports, and similar outputs via explicit publication rather than `mv`/`cp` exceptions.

**Files:**
- Modify: `bin/agent-flow-kit.mjs` near `parseExportApkArgs()` / `runExportApk()`
- Modify: `tests/test_custom_skill_install.py` export tests
- Optional create: `src/agent_flow/core/artifact_publish.py` only if Python callers need shared validation

**Step 1: Add RED tests**

Test source requirements:

- source is a regular, owned file contained by the execution-bound active worktree;
- leader/sibling/unbound sources are denied;
- extension/name policy is explicit rather than accepting arbitrary path syntax;
- destination is an approved root (initial default: user Downloads) and preserves TOCTOU checks;
- symlink-swap source/destination attacks remain denied.

**Step 2: Implement an explicit generic command**

Introduce `agent-flow publish-artifact <workspace-relative-source> [--name FILE]` using the existing descriptor/chain validation machinery. Keep `export-apk` as a compatible specialized alias or wrapper. Do not accept an arbitrary external destination path in this phase.

**Step 3: Verify**

Run the existing APK tests and new WebP/archive-like tests. Confirm no external file appears after rejected attempts.

## Task 5: Make lifecycle operations transaction-only

**Objective:** Ensure normal workflow cleanup stays usable without shell parser exceptions.

**Files:**
- Inspect/modify only if a regression proves a gap: `src/agent_flow/core/worktrees.py`, `src/agent_flow/core/workspace_boundary.py`, `src/agent_flow/cli.py`
- Test: `tests/test_runner_smoke.py`, `tests/test_pinned_workspace_boundary.py`

**Step 1: Add a narrow RED regression if needed**

Exercise merge/cleanup only through existing launcher/CLI lifecycle commands. Assert raw `git worktree remove`, raw branch deletion, and raw cleanup shell forms are denied by the hook while the authenticated lifecycle method remains allowed.

**Step 2: Reuse existing binding/finalizer APIs**

Do not create a new broad write exception. Require active/finalizer ownership, workspace identity, run ID, and single-writer checks already provided in `workspace_boundary.py`.

## Task 6: Fix developer test routing and test-time budgets

**Objective:** Make changed-file tests the default development feedback loop, not `full-final`.

**Files:**
- Modify: `scripts/run-test-shards.py:69-91,268-365`
- Modify: `tests/test_test_shards.py`
- Optional modify: `tests/shard_policy.py`

**Step 1: Add RED routing tests**

Assert a change to:

- `scripts/hooks/guard-worktree-write.py` selects an exact boundary test nodeid set;
- `bin/agent-flow-kit.mjs` selects its related Node/installer/export/sandbox tests, not `fast` fallback;
- `workspace_boundary.py` selects the focused binding/lifecycle tests;
- explicit `--test-nodeid` overrides inference;
- `full-final` remains the only command that schedules all final shards.

**Step 2: Add a curated impact map**

Extend `RELATED_TESTS_BY_PRODUCTION` with exact high-signal nodeids. Keep it small, reviewed, and tested: an impact map must never silently select zero tests for a production boundary file.

**Step 3: Preserve release coverage**

Do not remove `full-final`. Use it once after frozen boundary changes and in CI/nightly; development flow is `targeted` then `related`.

**Step 4: Verify plans, not only test output**

```bash
python3 scripts/run-test-shards.py targeted \
  --changed-file scripts/hooks/guard-worktree-write.py --plan
python3 scripts/run-test-shards.py related \
  --changed-file bin/agent-flow-kit.mjs --plan
```

Assert plans contain focused nodeids rather than `targeted-fast-fallback`.

## Task 7: Layered verification and exact-diff security review

**Objective:** Verify function, security, and compatibility without paying `full-final` after every small edit.

**Files:** no production changes required.

1. Run each new test red before the code that makes it pass.
2. Run focused tests after each task.
3. Run related target plans/tests after Tasks 2–6.
4. Run:

```bash
git diff --check
python3 -m py_compile scripts/hooks/guard-worktree-write.py
uv run --with pytest --with pytest-xdist python -m pytest -q -o addopts='' \
  tests/test_pinned_workspace_boundary.py tests/test_test_shards.py
```

5. Run `full-final` once only when the code is frozen. Record an environment-blocked trusted-bwrap positive test separately from a product regression.
6. Dispatch a read-only independent security review of the exact final diff. Do not commit or report completion if the review finds an unresolved bypass.

## Risks and decisions

- **No trusted bwrap on Linux:** This is an explicit operational prerequisite, not a reason to permit raw shell fallback. The first implementation must prove denial locally; sandbox-positive integration requires a trusted sandbox-capable host/CI worker.
- **Raw shell usability:** Developers/agents must use `agent-flow gate -- <argv>` for shell-based build/test/Git/media work. This is an intentional interface change that eliminates parser ambiguity.
- **Sandbox is filesystem-focused:** device-side ADB and network permissions require later explicit capabilities; they must not be smuggled in through a generic shell exception.
- **Artifact destination:** First phase keeps a fixed approved destination root (Downloads) rather than accepting arbitrary destination paths. Destination profiles/approval UX can be added after core containment is stable.
- **Current parser WIP:** Do not reset or conceal it. Replace authorization call sites incrementally and delete dead parser code only after positive and negative capability regressions are green.
