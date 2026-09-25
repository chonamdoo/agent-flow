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

## Architecture selection

Choose the project's architecture before starting a run. The selection authority is
`.agent-flow/project.yaml`. It lives inside the gitignored `.agent-flow/`, so the mode is
this checkout's decision; new linked worktrees receive a copy at creation. What the team
shares is the contract document itself, under `skills/`. `run --architecture` is prompt
context, not this selection.

| Mode | Contract |
|---|---|
| `clean` | Bundled Clean Architecture core and applicable platform norms |
| `local` | The project contract at exactly `skills/architecture/SKILL.md` (team, tracked) or `.agent-flow/local-skills/architecture/SKILL.md` (private, this machine only) and its declared references |
| `pending` | No selected contract yet; existing-pattern local work is allowed, but work requiring a structural decision blocks until selection |

A project without the selection file keeps the compatibility default, `clean`; absence
does not mean `pending`. Human-readable `status` explains an absent selection or `pending`.
That guidance does not change JSON, exit codes, `next_command`, or the run's pinned selection.

Choose one of these commands, rather than running all three:

```bash
agent-flow architecture select --mode clean
agent-flow architecture select --mode local --skill skills/architecture/SKILL.md
agent-flow architecture select --mode local --skill .agent-flow/local-skills/architecture/SKILL.md
agent-flow architecture select --mode pending
```

`--skill` is required for `local` and rejected for the other modes. It is not an arbitrary
path: create the local contract at one of the two fixed paths before selecting it. The
team path `skills/architecture/SKILL.md` is tracked and reaches every clone, worktree, and
reviewer. The private path `.agent-flow/local-skills/architecture/SKILL.md` sits in the
gitignored drop-box, so it applies only on this machine; linked worktrees read the
leader's copy, and the tracking requirement below does not apply to it. Selection checks
the contract, references, and required skills before writing the file. Inspect the result:

```bash
agent-flow architecture export --format json
```

The local selection file has this shape:

```yaml
schema_version: 1
architecture:
  mode: local
  skill: skills/architecture/SKILL.md
```

For a team contract, track the contract root and **every** declared reference in Git
before starting the run. `select` can warn about untracked documents; that is not a
waiver of the run's tracking requirement.

```bash
git add skills/architecture/SKILL.md skills/architecture/references
```

Omit the references directory from that command if the contract declares none. A private
contract is never tracked; its content is still pinned by digest, so editing it during a
run triggers the same drift block.
The installer honors the selection: `local` and `pending` exclude bundled Clean-specific
skills, while `clean` retains them. Project-owned norms are not installed copies to edit
under `.agent-flow/`. Install or refresh assets only in the leader with no active runs.
For an existing run, follow [Workflow definition migration](#workflow-definition-migration);
changing selection, contract bytes, or a declared reference triggers the existing drift
block rather than changing its approved contract.

### Conditional local references

Declare normative references in the frontmatter of the contract root (`SKILL.md`).
Links in its body alone do not make a document required.

```yaml
---
name: architecture
description: Project architecture contract
requires_docs:
  - references/common.md
  - path: references/app-a.md
    pathGlobs:
      - apps/a/**
      - packages/shared/**
  - path: references/app-b.md
    pathGlobs:
      - apps/b/**
      - packages/shared/**
---
```

Paths resolve beneath the contract's `architecture/` directory. Strings are always required; an object with
only `path` is also unconditional. Objects accept only `path` and optional `pathGlobs`.
Duplicate reference paths, including duplicates across string/object forms, are rejected.
Reference paths must be canonical `references/*.md` paths; nested reference directories
are allowed, but empty, `.`/`..`, backslash, or non-Markdown paths are not.

When present, `pathGlobs` must be a nonempty list of nonempty checkout-relative POSIX
patterns. Absolute paths, backslashes, empty or `.`/`..` segments, control characters,
negation, and brace expansion are rejected. Matching reuses the existing skill matcher:
case folding and Python `fnmatch`, with leading `**/` also matching root-level paths.
Python `*` can cross `/`; these are **not** gitignore/minimatch patterns or architecture
role patterns. Python metadata validation is authoritative; the installer consumes its
validated projection rather than maintaining a second YAML parser.

| Proven change scope | Required delivery |
|---|---|
| App A only | Root, unconditional references, and references matching A |
| Apps A and B | Union of both selections |
| No changes, proved by a complete snapshot | Root and unconditional references |
| Unknown, unresolved, or truncated scope | All documents; observation failures remain failures |
| Rename or deletion | Match both old/new rename paths and deleted paths |
| Selection file, contract root, or declared reference changes | Select all documents; existing selection/norm drift checks still apply first |

The author receives all documents in early or unresolved phases. Where a phase has a
proven code scope, it uses the existing review baseline's committed branch changes plus
staged, working-tree, and untracked changes. Each independent reviewer uses the scope of
the snapshot actually delivered to its job. An empty Git status does not remove documents
needed by already committed work. Existing review-base and publication-scope rules remain.

Scope growth adds required document identities to the phase input and requires re-entry
before an artifact lacking those documents can complete. Scope shrinkage does not remove
documents already required in that phase. Put shared paths in every affected reference's
conditions, as above, or keep that reference unconditional: task text and dependency
inference do not establish shared ownership.

Conditional delivery reduces prompt content, **not** capture or pinning. The root and all
declared references are still read, checked for safety/tracking, and digest-pinned,
including conditions in the root bytes. A change to an unselected reference still blocks
on drift. The common root is always delivered; do not claim an omitted reference was reviewed.

### Architecture lint in a monorepo

Architecture role lint remains a Clean-mode check. A non-Clean `architecture` profile
override is rejected by both direct lint and the CLI; `local`/`pending` without that
override remain `n/a`. Local contract review does not activate the built-in Clean role rules.

For Clean projects, use concrete app prefixes in
`.agent-flow/profiles/<profile>.local.yaml`. For example, adapt Next.js role paths from
`src/features/<feature>/api` to `apps/store/src/features/<feature>/api`, and the paired
presentation path to `apps/store/src/features/<feature>/presentation`. Likewise prefix
core paths such as `src/core/domain/<context>` with `apps/store/`. Keep each app's
paired roles, managed roots, and any activation/module declarations consistent.

`architecture.roles` is a whole-list override, not an appended path patch. Preserve the
other roles and constraints you still need, and restate required shipped declarations
for reused role IDs. `apps/*` is not a newly added role engine: existing wildcard,
placeholder, specificity, pair, and activation semantics are unchanged. A path such as
`productList` does not acquire an `api` or `presentation` role just by adding an app prefix.

```bash
agent-flow architecture-lint --profile nextjs --files apps/store/src/features/catalog/api/model.ts
```

The report separates source candidates, role-matched files, and findings per profile.
Absent contracts, inactive roots, no source candidates, and zero role matches are `n/a`,
not a checked pass. Matched valid input can pass; violations fail. Exit policy is unchanged:
`n/a` and valid input return 0; the lint consumer returns 1 for findings or configuration/
discovery errors. Invalid CLI arguments can return 2. Unmatched paths are not proof of
structural compliance.

### Architecture-specific completion markers

Fresh workflow definitions use neutral common requirements, including
`## Architecture Boundary Map` and the phase's `architecture-contract` markers.
`required_markers_by_architecture` adds the selected mode's requirements to
`required_markers`. Its only mode keys are `clean`, `local`, and `pending`, each containing
a marker list. Prompt generation, marker checking, and completion use the same effective
requirements from the validated selection.

Clean-only dependency, UseCase, repository, mapping, cache, and platform obligations retain
their enums and exceptions. Local review must assess the selected root and required
references; a single `applied` line is not proof of that review. `pending` still blocks
structural decisions. This is not a marker DSL in `requires_docs` or blanket permission
to answer `n/a`.

Old pinned definitions without the conditional field retain their old marker names,
requirements, and legacy validation. They are not aliased or automatically migrated to
new names. Use the existing [migration procedure](#workflow-definition-migration) when
the old definition cannot be verified; do not rewrite approvals or accept workflow drift.

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

#### Bound run or fresh inspection

Run these commands in the selected checkout (or pass its path with `--root`):

```bash
agent-flow skills resolve --phase green
agent-flow skills prompt --phase green
agent-flow skills markers --phase green --artifact <artifact-path>
agent-flow skills resolve --fresh --workflow full-feature --phase green
```

The first three examples assume an active `full-feature` run. `resolve`, `prompt`, and
`markers` use that checkout's active run's validated workflow pin when `--workflow` is
omitted or names the same workflow. Another phase in that pinned definition can be
inspected; it need not be the current cursor phase. An explicit different workflow without
`--fresh`, or a phase absent from the selected definition, is rejected with exit 2.

`--fresh` inspects current kit YAML using the explicit workflow or `default` and does not
borrow the active run's task, time, or concerns. With no active run, the same current-kit
selection applies. Bound inspection carries the run context; explicit `--task` and the
`markers` command's `--since` override remain available. Inspection does not rewrite the
pin, cursor, or approvals. Malformed pins do not fall back to live YAML; see
[Workflow definition migration](#workflow-definition-migration).

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

### Durable review angles

Put project overrides in `.agent-flow/profiles/<profile>.local.yaml`, not the installed
`<profile>.yaml` that install/update replaces. `review_angles` replaces the complete
capability-expanded profile list; entries are not merged by ID. For example, this
`android.local.yaml` keeps two profile angles:

```yaml
review_angles:
  - id: android-skills
    prompt: templates/_shared/review/android-skills.md
  - id: test-edge
    prompt: templates/_shared/review/test-edge.md
```

Omitting `review_angles` preserves the distributed list. `review_angles: []` removes
profile angles only: mandatory system baseline angles `generalist` and `types` still run
and cannot be gated or removed through this override. Multi-profile composition happens
after each profile's list replacement. `skills` and baseline overrides remain rejected.

Each entry requires a nonempty `id` and `prompt`. IDs use lowercase letters, digits, and
hyphens, begin with a letter or digit, and are at most 64 characters. Prompts use the
existing `templates/_shared/review/<name>.md` path. Optional `requires` is a nonempty
string; `task_terms` and `path_globs` are lists of nonempty strings. Existing requirement
and selector precedence, built-in prompt priority, and prompt path restrictions remain.
An invalid list/entry shape is rejected rather than silently ignored. Keep the local file
when updating installed assets.

The build, test, and lint commands come only from the active profile's `gates`. Verification
commands that are not in a gate are not repeated at will.

## Reviewer distribution

Every phase marked `multi_review: true` uses the same availability-based dispatch
over installed **Claude and Codex** CLIs. OMP can be the host or controller but is
not a reviewer provider. When both are candidates, each must also complete a valid probe.

- **Both available** — both can review every angle. A provider with a failed probe
  is excluded from remaining angles; valid rejections already obtained are kept.
- **Only one available** — that provider starts every angle in one dispatch, without a
  probe, up to `AGENT_FLOW_MAX_WORKERS` at a time, still using at least two independent
  subprocesses. After a provider-level failure (rate limit, timeout, or a failed exit
  such as an authentication or quota error), angles that have not started yet are
  skipped: approval needs that provider to complete every angle, so the review is
  already blocked. Two subprocesses do not require two vendor names.
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
