# Install and usage

What this tool is and why it is shaped this way is in [README.md](../README.md). This page only
covers actually running it.

## Install the tool

### Homebrew

```bash
brew tap chonamdoo/agent-flow https://github.com/chonamdoo/agent-flow
brew install --HEAD chonamdoo/agent-flow/agent-flow
```

The repository is not named `homebrew-*`, so the tap takes the two-argument form with the URL
spelled out. Install by the fully qualified name: since Homebrew 6.0 a third-party tap needs
explicit trust, and the qualified name grants it for this one formula.

`--HEAD` is required while no release is tagged — the formula has no stable download yet, and
Homebrew refuses a head-only formula without the flag. A HEAD build compiles from source, so the
Xcode command line tools have to be present. The formula installs `node` as a runtime dependency
because project install and every managed hook run `bin/agent-flow-kit.mjs`.

```bash
brew upgrade --fetch-HEAD chonamdoo/agent-flow/agent-flow
```

`brew upgrade` on its own never looks at the upstream commit of a HEAD install; `--fetch-HEAD`
is what asks. Once a release is tagged, `brew install chonamdoo/agent-flow/agent-flow` and a
plain `brew upgrade` are enough.

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

## Running

Use it inside a Claude or Codex session.

```text
/agent-flow add a user profile page       # start
/agent-flow                               # continue in the selected worktree
/agent-flow status                        # progress
/agent-flow abort                         # cancel
```

The same thing straight from the CLI:

```bash
agent-flow run "add a user profile page"
```

```bash
agent-flow status --worktree "feat-user-profile"
```

```bash
agent-flow continue --worktree "feat-user-profile"
```

Add `--workflow` to pick a workflow. Omitted, it is `default`.

```bash
agent-flow run "<task>" --workflow bugfix
```

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

The forms below are for reproducing a failure by hand. Their output is not the routing basis for
a run.

```bash
agent-flow gates --phase all
```

```bash
agent-flow gates
```

The second form is a local check that runs only the default `pre-commit`. Passing `--timeout`
takes precedence over the profile declaration.

### Skills

`skills sync` fetches only the external `skill_sources` a profile declares. The profiles and
workflows themselves are refreshed by running the installer again.

```bash
agent-flow skills sync
```

### PR watching

```bash
agent-flow pr-watch <number>
```

It polls until the state needs action. Add `--once` to query a single time.

It calls the `gh` CLI directly and inherits the authentication the user already has. agent-flow
does not manage a token of its own. If `gh` is missing or unauthenticated, it says exactly that.

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

Phases marked `multi_review: true` run review angles only on the installed **Claude and Codex**
CLIs. OMP can be the host or controller but is not used as a reviewer provider.

`final-review` distributes every angle to both providers. Other `multi_review` phases run every
angle on one primary provider, plus any additional provider that was selected.

- **Both installed** — `final-review` runs every angle on both sides, and a provider whose probe
  failed is excluded from the remaining angles
- **Only one installed** — every angle runs on that provider, still as independent subprocesses
- **Neither** — the phase closes as a failure. The controller session cannot record the review
  verdict in its place

`AGENT_FLOW_REVIEWERS="codex"` narrows it. Names other than Claude and Codex are ignored.
Per-angle artifacts (`final-review-<angle>-<provider>.md`) survive even when some time out. One
slow CLI does not block the rest.

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
