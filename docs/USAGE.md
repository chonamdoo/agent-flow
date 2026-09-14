# Install and usage

What this tool is and why it is shaped this way is in [README.md](../README.md). This page only
covers actually running it.

## Install the tool

### Homebrew

```bash
brew tap chonamdoo/agent-flow https://github.com/chonamdoo/agent-flow
brew install chonamdoo/agent-flow/agent-flow
```

The repository is not named `homebrew-*`, so the tap takes the two-argument form with the URL
spelled out. Install by the fully qualified name: since Homebrew 6.0 a third-party tap needs
explicit trust, and the qualified name grants it for this one formula.

The formula installs `node` as a runtime dependency because project install and every managed hook
run `bin/agent-flow-kit.mjs`.

```bash
brew upgrade chonamdoo/agent-flow/agent-flow
```

`brew install --HEAD chonamdoo/agent-flow/agent-flow` builds the current `main` instead of the
released tag; that form compiles from source, so it needs the Xcode command line tools. A HEAD keg
only sees a newer commit through `brew upgrade --fetch-HEAD`, which is why `agent-flow update`
prints that flag when it detects one.

### From a checkout

```bash
pip install -e <path-to-this-kit>
```

This is the same executable the formula installs; use it when you are changing the kit itself.

## Initialize a project

Once per project. Do not run it again just because a new session started.

```bash
agent-flow .
```

A bare path is the install command: `agent-flow <dir>` installs the project assets into that
directory, so there is no need to `cd` into it.

```bash
agent-flow <project-path>
```

Installer flags are passed straight through, and the installer — not this shorthand — validates
them.

```bash
agent-flow . --profile android --skills tdd,code-review
```

From a checkout the Node entry point does the same thing; it is what `agent-flow <dir>` calls.

```bash
npx <path-to-this-kit> install --root <project-path>
```

Install always happens in the leader checkout. Running it inside a linked worktree is blocked.

## Update check

`run`, `start`, `status`, and `continue` check for a newer release at most once a day and print
one line to stderr when there is one. The check reads GitHub Releases, is capped at 1.5 seconds,
and caches its result — including a failure — in `$XDG_STATE_HOME/update-check.json` (or
`~/.agent-flow/update-check.json`), so a blocked network costs one attempt per day rather than
one per command.

```bash
agent-flow update
```

That asks immediately, bypassing the cache, and prints the installed version, the latest
release, and the upgrade command for how this kit was installed. Inside a Homebrew Cellar it is
`brew upgrade chonamdoo/agent-flow/agent-flow`, with `--fetch-HEAD` added when the keg is a HEAD
build, because that is the only form that re-reads the upstream commit; from a checkout it is
`git -C <kit> pull`. It never upgrades anything itself.

`AGENT_FLOW_NO_UPDATE_CHECK=1` turns off the automatic check. `agent-flow update` ignores that
switch, because asking directly is not the same as being asked.

This is a different axis from the stale-install warning: that one says the assets copied into
the project no longer match the kit and is fixed by installing again, this one says the kit
itself is behind and is fixed by upgrading.

## Workflow definition migration

New runs store the exact workflow source and its digest binding in `meta.json`.
Updating kit YAML does not change an already pinned run's phases, routes, or approval contract.

| Existing run | Resume behavior |
|---|---|
| Complete, valid workflow pin | Uses the stored definition, even if kit YAML changes |
| Legacy run with `workflow_digest` but no stored definition | Resumes only if the available original YAML matches that digest; the runner captures the verified definition under its lifecycle lease |
| Missing identity, invalid pin, or unavailable original YAML | Blocks advancement without rewriting records or approvals |

Before upgrading a runtime or replacing installed assets, finish unpinned runs with their
original runtime and assets, or back up the full run directory together with its original
kit assets, including workflow YAML and required skills. Runs created by older releases,
including 0.2.11, are not guaranteed to
resume after their original YAML has been overwritten. The loader does not search historical
Git revisions or infer an old definition from the current kit.
A verified legacy workflow that still names `clean-architecture` also needs its original
skill installation; the resolver will not silently substitute a different norm.

`--accept-workflow-drift` is no longer supported. This is an intentional compatibility change:
the old command could rebase an existing run onto changed YAML and require fresh approval.
The pinned-definition policy instead preserves the old approval contract. Do not replace
`workflow_digest` with the current digest, remove binding fields, or overwrite new packaged
YAML to make an old run pass.

When a definition cannot be verified, `agent-flow status` still prints the recorded run
identity, phase, artifact inventory, and a blocked `status_json`, then exits with code 2.
The recorded phase is not verified; `required_artifact` is null and `next_command` is empty.
This output is diagnostic, not permission to advance or approve the run.

If the original definition cannot be recovered, ask the user to authorize ending the old
run and starting a successor. In the selected worktree, after that approval:

```bash
agent-flow abort --root <leader-path> --worktree <worktree-name> --yes
agent-flow run "<remaining work>" --workflow development --reuse-existing-worktree
```

`abort` preserves the old run's artifacts. Reference them from the successor's exploration
artifact; do not transfer old approvals to the new definition. Choose the successor workflow
for the remaining task rather than automatically restarting a larger lifecycle.

## Running

Use it inside a Claude or Codex session.

```text
/agent-flow add a user profile page       # start
/agent-flow                               # continue in the selected worktree
/agent-flow status                        # progress
/agent-flow abort                         # cancel
```

The slash form takes no workflow, so it starts the compatibility `default`. To run a smaller
workflow, start from the CLI with `--workflow`.

The same thing straight from the CLI:

```bash
agent-flow run "add a user profile page" --workflow default
```

```bash
agent-flow status --worktree "feat-user-profile"
```

```bash
agent-flow continue --worktree "feat-user-profile"
```

Add `--workflow` to pick the smallest workflow that fits. Omitted, the CLI falls back to `default`
for compatibility, but policy-compliant starts name the workflow explicitly.

```bash
agent-flow run "<task>" --workflow bugfix
```

Which workflow fits which task, the optional `Source-URL` / `Source-Revision`
task-text convention, and the subsystems that change only through a reproduced
bug are in [IDENTITY-AND-FREEZE-POLICY.md](IDENTITY-AND-FREEZE-POLICY.md).

### worktree

```bash
agent-flow worktree create --name feat-user-profile
```

```bash
agent-flow worktree list
```

The default location is `~/.agent-flow/worktrees/<repo-id>/<name>`. Put it inside the project
folder and an IDE left open on the leader reacts to worktree activity, touches the leader's
caches, and the leader tripwire reports that as contamination — which blocks the remaining
phases.

Do not run `git worktree add` by hand; it skips the creation lock, the base selection, and the
adoption record. Any other linked worktree has to be adopted before it is recognized.

```bash
agent-flow worktree adopt --path <checkout>
```

Short workflows (`review`, `development`, `bugfix`, `diagnosing-bugs`) declare
`completion_disposition: local-handoff`: completing them keeps the bound checkout.
An already-pending cleanup journal still resumes; this setting is not a way to hide
unfinished cleanup. Other workflows retain integrated cleanup.

At a `pause_after` boundary, review the artifact and use the exact `next_command`
reported by `status`. Its `--approve` token identifies the run, phase attempt, and
artifact bytes. Rewriting the artifact or re-entering the phase requires a new
approval. The token binds an approval to content; it is not user authentication.

### The SPEC ledger

The initial list automatically becomes the baseline. Only additions, changes, and deletions
after that are shown as a delta.

```bash
agent-flow spec changes --run-dir <run-dir>
```

```bash
agent-flow spec confirm --run-dir <run-dir>
```

Manually verified items follow the same flow. Once the user confirms in conversation, the agent
runs this on their behalf.

```bash
agent-flow spec approve <spec-id> --run-dir <run-dir>
```

### Gates

The runner executes the `gates` phase itself. It runs the profile gates with `--phase all` and
writes the result file too — the point is that the thing being verified does not write the
verification result. If the agent runs `agent-flow gates` in this phase or writes the result
file, that file is discarded.

The ceiling for a single gate comes from the profile's `gates[].timeout_s`. With no declaration
it is 600 seconds. A timeout is recorded as undecidable rather than as a failure, so gates that
take minutes — gradle, xcodebuild — should declare a higher ceiling in the profile.

An `execution: ci` gate is omitted from the local wave and must declare `ci_check`. That value
must match the pull-request check name exactly after case folding and whitespace trimming. If a
matrix adds a suffix such as `pytest (3.12)`, declare that full name. A local gate must not declare
`ci_check`.

That check name is the **job** name, not the workflow name. A workflow holds several jobs, so
accepting the workflow name would let any green job in it satisfy the gate. Declare `pytest`, not
the `Tests` workflow that contains it; a workflow name never matches and stays pending forever.

The forms below are for reproducing a failure by hand. Their output is not the routing basis for
a run.

```bash
agent-flow gates --phase all
```

```bash
agent-flow gates
```

The second form is a local check that runs only the default `pre-commit`. It writes
`artifacts/gate-results-local-pre-commit.json` and leaves the canonical all-phase ledger
unchanged. Passing `--timeout` takes precedence over the profile declaration.

With several active profiles, branching, PR, and commit policy must agree;
conflicts fail before creating a worktree. Duplicate command gates retain
`required: true` if any profile requires them. Conflicting timeout or CI-check
declarations also need an explicit resolution.

Implementation evidence distinguishes `change-kind: bugfix|feature|behavior-preserving`.
Bugfixes and features need a real failing regression followed by GREEN.
Behavior-preserving work needs an unchanged-contract reason and the named
relevant regression GREEN, not an invented failure. A runner-issued
`red-reference` is reusable only for the same regression and phase-entry code
baseline; empty legacy baselines are not reusable. Command hooks observe a
post-command baseline, not proof that code stayed unchanged throughout a command.
CI-only gates remain remote unless the user grants a scoped local exception.

### Skills

`skills sync` fetches only the external `skill_sources` a profile declares. The profiles and
workflows themselves are refreshed by running the installer again.

```bash
agent-flow skills sync
```

Bundled skills are restored through the installer, not `skills sync`. External
source refresh publishes URL/ref-qualified immutable checkouts atomically; failed
refreshes leave existing published readers intact. Legacy mutable cache paths are
not accepted as a published snapshot.

Run installation from the leader only after active runs finish. Both installer
entrypoints check leader and private-worktree runtime state before writing project
assets. A delegated failure exits nonzero and skips the success metadata/banner;
this does not promise rollback of every earlier write. Explicit `--no-hooks`
cleanup remains independent. User-edited copied references survive reinstall and
retirement, and the installation digest is taken after reference synchronization.

Hook registration is not proof that a host executes hooks. Check the native
host's activation/trust requirements and observed command evidence. Managed
provider confinement currently has a verified macOS `sandbox-exec` backend only;
other operating systems fail closed instead of receiving the same isolation claim.

### PR watching

```bash
agent-flow pr-watch <number> --run-dir <run-dir>
```

It polls until the state needs action. Add `--once` to query a single time. Outside a workflow run,
pass `--allow-unbound` explicitly to watch without deferred CI gate evidence.

It calls the `gh` CLI directly and inherits the authentication the user already has. agent-flow
does not manage a token of its own. If `gh` is missing or unauthenticated, it says exactly that.

Resolved review threads and superseded reviews do not requeue old feedback.
To acknowledge observed feedback without treating discussion as a code change:

```bash
agent-flow pr-watch <number> --run-dir <run-dir> --repo <owner/repo> \
  --ack-head <observed-head> --ack-feedback <observed-feedback-id>
```

Repeat `--ack-feedback` for additional observed IDs. An acknowledgement is scoped
to that run, repository, PR, HEAD, and feedback revision; it does not acknowledge
new or edited feedback. Code changes during PR fixes return to review and
invalidate previous gate evidence before publication. Keep code-related feedback
unacknowledged and threads open until the fix is verified in the published PR
HEAD; then reply, resolve the thread, and acknowledge it. Discussion-only answers
can complete without a push. The canonical triage and completion rules are in
[`push-watch`](../skills/push-watch/SKILL.md).

The runner tracks unsuccessful completed CI repairs independently per check and
blocks after the third recurrence. Initial failures, repeated observations,
journal replay, unrelated HEAD changes, and ordinary review comments do not spend
this budget. Same-HEAD retries require a distinct completed CI execution/result,
not another read of the old failure. An accepted terminal result resets only the
resolved check, including results observed between polls or before a HEAD change.
Optional `NEUTRAL`, `SKIPPED`, and `STALE` results are accepted; declared required
checks still require success. Pending or missing results do not reset the budget.
A capped run stays blocked on `continue`; the
[`push-watch` recovery procedure](../skills/push-watch/SKILL.md#ci-repair--pr-ci-fix)
requires explicit user approval to preserve and end it before a focused new run.

The legacy Node `agent-flow-kit run push-watch-tick` command delegates bound PR
observation to the Python watcher, so both entry points use the same check
classification and recorded feedback evidence. It passes `--require-ready` to
preserve its stricter readiness contract: at least one check must be registered
and `reviewDecision` must be `APPROVED` before the result can be green. The Python
watcher also accepts this flag; without it, no-CI repositories remain supported.

## Repository layout

```text
agent-workflow/
├── bin/
│   ├── agent-flow-kit.mjs        # main entry point: install and installed-asset sync
│   └── agent-flow-install.mjs    # install-only entry point
├── lib/                          # JS modules shared by the installer
├── src/agent_flow/               # Python orchestrator
│   ├── cli.py                    # run / continue / status / abort
│   ├── runner.py                 # phase loop; routing authority lives here
│   ├── artifact.py               # phase artifact recording
│   ├── multi_review.py           # distributes review angles across the CLIs
│   ├── subprocess_pool.py        # parallel subprocesses with timeout and drain
│   ├── core/                     # boundaries, isolation, ledger, gate judgment
│   ├── adapters/                 # base / auto / hosted / generic
│   ├── workflows/                # workflow YAML source of truth (exactly one copy)
│   └── profiles/                 # per-stack profiles
├── skills/                       # copied to .agent-flow/skills/ on install
├── templates/_shared/review/     # review angle prompts
├── bootstrap/                    # AGENTS.md / CLAUDE.md templates
├── scripts/hooks/                # PreToolUse / PostToolUse / Stop hooks
├── Formula/agent-flow.rb         # the Homebrew tap formula lives in this repo
└── tests/
```

`bin/agent-flow-kit.mjs` hands `start`/`status`/`next`/`advance` to the Python CLI and does not
advance the run lifecycle itself. The one place it writes its own state is `push-watch`.

## Profiles

`src/agent_flow/profiles/<stack>.yaml` declares `branching`, `gates`, `review_angles`,
`artifacts`, `vocabulary`, `commit_convention`, and `pr`. The field schema is in
`src/agent_flow/profiles/_schema.yaml`.

The runner parses the active profile and injects it into every phase prompt. The host AI gets
the actual values, not "go look it up somewhere." The active profile is set by `profile` in
`.agent-flow/kit.json` or by the `AGENT_FLOW_PROFILE` environment variable.

The Android profile, as an example:

```yaml
branching:
  strategy: trunk
  worktree: required        # a branch alone will not do
  naming: { prefix: "feat/", slug_style: kebab-case }

gates:
  - architecture-lint  (pre-commit, required)
  - build              (pre-push,   required, timeout_s 1800)
  - test               (pre-push,   required, timeout_s 1800)

review_angles:
  - architecture-design
  - android-skills
  - compose-stability
  - test-edge
  - sdui
  - udf
```

The build, test, and lint commands come only from the active profile's `gates`. Verification
commands that are not in a gate are not repeated at will.

## Reviewer distribution

Every phase marked `multi_review: true` uses the same availability-based dispatch
over installed **Claude and Codex** CLIs. OMP can be the host or controller but is
not a reviewer provider. An installed binary must also complete a valid probe.

- **Both available** — both can review every angle. A provider with a failed probe
  is excluded from remaining angles; valid rejections already obtained are kept.
- **Only one available** — that provider covers every angle, still using at least
  two independent subprocesses. Two subprocesses do not require two vendor names.
- **Neither available** — review is blocked. Controller-session work cannot replace it.

Approval requires a complete valid set from at least one provider and no valid
request-changes result. Authentication, quota, timeout, and malformed-output failures
are execution failures, not code approval.

`AGENT_FLOW_REVIEWERS="codex"` narrows the candidate pool. Names other than Claude
and Codex are ignored. Per-angle artifacts survive partial failure so the cause
and valid findings remain available.

## Verification

```bash
npm run parity:check
```

Checks whether the installed assets drifted from the source, and whether the workflow phase
counts, profile names, and skill count declared in [README.md](../README.md) match the source
files.

```bash
npm test
```

Runs the Python tests together with the check above.

## A known trait

This kit does not itself follow the architecture it prescribes. The `ddd-architecture` skill
demands DDD and Clean Architecture of user code, while the kit's Python source is procedural
code with a single abstraction (`Adapter`). That is the right call for a small CLI tool, but the
fact that the prescription and the artifact differ is written down here.
