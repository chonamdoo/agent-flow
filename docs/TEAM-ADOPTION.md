[한국어](ko/TEAM-ADOPTION.md)

# Team adoption

This document describes the intended path from "one person's tool" to "a team's tool". It is
split into what the repository can do today and what does not exist yet. Nothing here is a
roadmap: there are no dates and no estimates. Anything unbuilt is labeled "not implemented".

---

## The goal

The target is that a team can add its own documents, conventions and rules, and that every
member's agent behaves by them without a person repeating the rule in chat. One member changes
a shared rule, the change is reviewable before it lands, and the next run on every machine
follows the changed rule.

That is a target, not a shipped feature. Today the pieces below carry parts of it, and the
parts they do not carry are listed under "What is missing for a team".

---

## What already carries team rules today

### The active profile

`src/agent_flow/profiles/_schema.yaml` defines the profile shape, and each
`src/agent_flow/profiles/<id>.yaml` fills it. A profile is the only place where stack rules are
declared without editing a workflow, because a workflow phase reads e.g. `profile.gates` and
stays stack-neutral.

A team can express:

- `gates` — the ordered verification commands, each with `id`, `command`, `required`, `phase`
  (`pre-commit | pre-push | post-merge`) and `timeout_s`. A gate that runs for minutes declares
  its own ceiling here, because a timeout is recorded as "not judged", not as a failure.
- `review_angles` — the specialist reviewers added at the final review phase, each an `id` plus
  a `prompt` path relative to the kit root. `src/agent_flow/profiles/android.yaml` declares six:
  `architecture-design`, `android-skills`, `compose-stability`, `test-edge`, `sdui` and `udf`;
  the base run always has one generalist reviewer and angles are extras.
- `branching` — `strategy`, `base`, `integration`, `worktree`, `leader_tripwire`, the
  `naming.prefix` and `naming.slug_style` for agent-generated branches, and `worktree_setup.copy`
  for the gitignored machine files a new worktree needs.
- `pr` — `target_branch` and `merge_strategy`.
- `commit_convention` — `style` (`conventional | tagged | freeform`) and `co_author`
  (`include | skip`). `android.yaml` uses `style: tagged`.
- `vocabulary` — stack-native renaming. Workflow YAML keeps canonical ids (`prd`, `adr`) and the
  agent substitutes the declared word when speaking to the user: `android.yaml` maps `prd: PRD`,
  `flutter.yaml` maps `prd: spec` and `adr: decision-log`.
- `execution.reviewers` — the model and effort a reviewer subprocess runs with, matched by
  `phase` and `angle`. It decorates an already assigned provider; it does not choose the
  provider.

A team cannot express, in a repository, the whole of that list. A repository lays its own values
on top through `.agent-flow/profiles/<profile-id>.local.yaml`, and
`PROJECT_OVERRIDE_KEYS` in `src/agent_flow/core/profiles.py` accepts exactly five keys:
`architecture`, `branching`, `execution`, `gates`, `pr`. Any other key is rejected with an
error rather than ignored, so a declaration that would not take effect is never swallowed
silently. `review_angles`, `commit_convention`, `vocabulary` and `skills` are therefore not
settable per repository: changing them means shipping a changed profile. Editing the installed
`.agent-flow/profiles/<id>.yaml` directly does not survive, because install overwrites the
shipped profile so new fields reach existing installs.

That override file is per working copy, not per repository. Install writes `.agent-flow/` into
the project's `.gitignore` (`upsertGitignore` in `lib/installer-shared.mjs`, called from
`bin/agent-flow-kit.mjs`), so a plain `git add` never picks the file up and another clone never
sees it. Sharing it takes `git add -f .agent-flow/profiles/<profile-id>.local.yaml` or a
negation rule in `.gitignore`, and the ignore entry is re-added on the next `agent-flow .`.

### Profile resolution and `AGENT_FLOW_PROFILE`

`resolve_profile` in `src/agent_flow/core/profile_resolution.py` fixes the order:

1. `AGENT_FLOW_PROFILE` in the environment — always wins.
2. `.agent-flow/kit.json:profiles` — the multi-profile union written by the filtered installer.
3. `.agent-flow/kit.json:profile` — the single profile written by the installer.
4. `generic`.

A profile named in `kit.json` that does not exist on disk is a hard error, not a degraded mode,
because a typo would otherwise run the whole workflow against the wrong stack — wrong
`branching`, wrong `gates`, wrong PR target. `AGENT_FLOW_FALLBACK_GENERIC=1` opts into silent
fallback instead.

What a team gets: the profile set is committed in `kit.json`, so every clone resolves the same
ids. What it does not get: the environment override sits above the committed value and is
deliberately lenient, so a member exporting `AGENT_FLOW_PROFILE` runs a different rule set and
nothing reports that to the team.

### The installed `AGENTS.md` contract block

Install writes the contract into the project's root `AGENTS.md` between
`<!-- agent-flow:start -->` and `<!-- agent-flow:end -->`, from `bootstrap/AGENTS.md.template`.
`CLAUDE.md` receives a pointer block instead, because the Claude CLI auto-loads only root
`CLAUDE.md`; that `@AGENTS.md` import line is the only path by which project prose outside the
block reaches Claude and the Claude reviewer.

Two sub-blocks inside it are filled by the installer:

- `<!-- agent-flow:skills:start -->` / `<!-- agent-flow:skills:end -->` — the skill index.
- `<!-- agent-flow:docs:start -->` / `<!-- agent-flow:docs:end -->` — the docs index.
  `docsIndexBlock` in `lib/installer-shared.mjs` walks `DOCS_INDEX_ROOT`, which is `docs`, and
  emits paths only. It carries a size ceiling and reports how many entries it truncated,
  because a silently cut list turns a missing entry into a missing file.

So a team that drops its own markdown into `docs/` gets those paths named in the contract every
agent loads. What it does not get: the index carries paths, not content, and it is refreshed
when the installer runs — not when a file is added. Team prose has to live outside the managed
block: install rewrites what is inside it. Ownership is decided against a hash recorded in
`.agent-flow/bootstrap/blocks.json`, and a block the installer did not write is kept and
reported as user-edited rather than overwritten.

### Project-local skills

`_DEFAULT_PROJECT_TEMPLATES` in `src/agent_flow/core/skill_resolver.py` lists the
repository-side skill roots, in order:

- `.agent-flow/local-skills/<skill>/SKILL.md` — a private drop-box. A document placed here
  attaches to code-generation and review phases with no frontmatter declaration, because
  putting it there is the declaration.
- `skills/<skill>/SKILL.md` — skills the repository owns and may name.
- `.agent-flow/skills/<skill>/SKILL.md` — the bundled set.
- `.claude/skills/<skill>/SKILL.md` and `.agents/skills/<skill>/SKILL.md` — vendor installs,
  kept separate from `skills/` because those names belong to someone else.

A profile's `skills.required_review` turns repository-owned names into a blocking requirement. A
group declares `skills`, a human-readable `when`, its activation selectors (`task_terms`,
`path_globs`, `concerns`) and the `missing:` message the run prints. A group without selectors
has no activation evidence and stays inert.

What a team cannot do here: `required_review` may only name skills the repository owns —
installed external skills are never enumerated there, because upstream renames make a name list
stale. `skills` is rejected in the `.local.yaml` override, since the installer, not the Python
runtime, decides what gets installed; opening it would split "declared" from "actually
installed" until routing pointed at nothing. And `.agent-flow/local-skills/` is per working
copy: install gitignores `.agent-flow/`, so committing that directory does not share it. The
shared drop-box is `skills/` at the project root, which install never ignores.

### External `skill_sources`

A profile declares `skill_sources`; `parse_skill_sources` in
`src/agent_flow/core/skill_sync.py` reads it, and `agent-flow skills sync` runs it. Two kinds:

- `kind: host-managed` — a source that already has an install manager. It is never fetched. The
  paths are resolved and, if absent, the declared `install_hint` is printed once at install
  time. `src/agent_flow/profiles/android.yaml` declares `android-official` and `chrisbanes`
  this way.
- `kind: fetch` — a plain git repository with no install manager. It is cloned once at a pinned
  `ref` into a machine-shared cache under `~/.agent-flow/skill-sources/<id>/<ref>`
  (`XDG_STATE_HOME` or `AGENT_FLOW_SKILL_CACHE` relocate it), not per project and not per run.
  Nothing is written into the repository. `android.yaml` declares
  `skydoves-compose-performance` this way with `layout: "*/{skill}/SKILL.md"`.

`agent-flow skills sync --refresh` discards the cache and re-fetches, because a moving `ref`
such as `main` otherwise freezes at whatever commit a machine happened to get first — an
invisible, per-machine pin. `agent-flow skills sync` fetches only the external `skill_sources`;
profiles and workflows themselves are refreshed by running the installer again.

So a team can point every member at the same external document set from one declaration. What
it cannot do: the declaration lives in the shipped profile, so a team wanting its own source
list carries a patched profile; and with `ref: main` two members can sit on different commits
with no error and no report.

---

## What is missing for a team

Each item below is derived from the four open problems in `README.md`. None of them is
implemented.

**Who owns the marker list, and how it changes.** A phase's completion contract is
`required_markers` in `src/agent_flow/workflows/<name>.yaml`, and
`_missing_required_markers` in `src/agent_flow/runner.py` blocks the advance when the artifact
does not carry them. There is no per-team ownership of that list and no override file for
workflows: `PROJECT_OVERRIDE_KEYS` covers profiles only, and the installed
`.agent-flow/workflows/<name>.yaml` is refreshed by re-running the installer, so a hand edit
does not survive. A procedure for proposing and accepting a marker change is not implemented.

**A shared convention pack that is versioned and reviewable rather than copied per repository.**
Today a convention document is either committed into each repository under `skills/`
(`.agent-flow/local-skills/` sits under the gitignored `.agent-flow/` and stays per working
copy), or reached through a `skill_sources` entry declared in a shipped profile. A pack that a
team owns, versions, reviews and pins independently of both the kit and the consuming
repository is not implemented.

**How a team agrees a review criterion.** A criterion is a `review_angles` entry pointing at a
prompt path relative to the kit root, such as `templates/_shared/review/architecture-design.md`.
`review_angles` is not one of the five keys a `.local.yaml` override accepts, so a team cannot
add or retire an angle without shipping a changed profile, and there is no record of who agreed
to it. An agreement procedure is not implemented.

**Per-member skill differences.** `agent-flow skills doctor` and `agent-flow skills scan` report
what resolves on the machine they run on. Nothing compares two machines. The candidate set is,
as `_schema.yaml` states, whatever happens to be installed locally, and the same change
described in two languages produced different required sets (measured: 6 required / 26,241 B in
English, 4 in Korean) — which is why task wording never promotes a skill to required. A
team-level check that two members resolve the same required set is not implemented.

**The token cost of parallel reviewers multiplied by team size.** `execution.reviewers` picks the
model and effort per phase and angle, and every extra `review_angles` entry is another reviewer
subprocess on the same change. Nothing accounts for that cost, per run or per team, and there is
no budget or ceiling expressed in team terms. Cost accounting is not implemented.

The first three of the four README problems are not technical. Adding automation is not the same
as earning trust, and none of the stages below changes that.

---

## Staged plan

Three stages. Each names the existing mechanism it would extend, the smallest observable
outcome that would prove it, the decision that belongs to people rather than code, and the
condition under which the stage should be dropped rather than built.

### Stage A — a team profile that lives outside a single repository

Would extend: `resolve_profile` in `src/agent_flow/core/profile_resolution.py` (which already
has a fixed order and a hard error for a missing declared profile), the
`.agent-flow/profiles/<id>.local.yaml` override, and the pinned-ref fetch cache in
`src/agent_flow/core/skill_sync.py` (which already clones a `ref` into a machine-shared cache
and records the commit sha it froze at).

Smallest observable outcome: two clones on two machines resolve one externally hosted profile at
the same pin and print the same `gates` and `pr` values, and moving the pin shows as a diff
before any run uses it.

People decide: who may move the pin, and what happens to a run already in flight when it moves.

Drop it when: the team concludes each working copy should keep its own values. The `.local.yaml`
override already carries `architecture`, `branching`, `execution`, `gates` and `pr`, but it is
gitignored, so dropping this stage leaves every member restating those values by hand — or
force-adding the file — rather than resolving one shared declaration.

### Stage B — team convention packs as versioned skill sources with a review owner

Would extend: `skill_sources` with `kind: fetch`, `ref` and `layout`
(`src/agent_flow/core/skill_sync.py`), and the `skills.required_review` groups that already
block a phase with a declared `missing:` message.

Smallest observable outcome: a review phase blocks with the group's `missing:` message when the
pack is absent, and the run record names the pack's commit sha, so two runs can be compared on
which pack version they saw.

People decide: who reviews a change to the pack, and whether a pack may make a criterion
required rather than offered — the schema deliberately keeps promotion out of task wording, so
promotion is a human decision either way.

Drop it when: the packs turn out to be one per repository. `skills/` at the project root already
carries those, and committing that directory is cheaper than versioning a pack nobody else
consumes.

### Stage C — an agreed verification contract

Would extend: `required_markers` in `src/agent_flow/workflows/<name>.yaml` and the runner block
in `src/agent_flow/runner.py`.

Smallest observable outcome: a run refuses to start when the effective marker list differs from
the agreed one without a recorded acceptance, and an accepted change is readable as a diff with
a name attached to it.

People decide: who may change a `required_markers` list and by what quorum, and whether a member
may loosen a marker for a single run — and if so, where that shows up afterwards.

Drop it when: the team is small enough that the list changes by conversation. A refusal-to-start
mechanism costs more friction than it removes if one person already owns the list.

---

## Open questions

No answer is claimed for either.

**Where is the break-even point between the friction the process creates and the trust it
buys?** The gate count is bearable alone. In a team, how much waiting is worth how much
confidence, and what observation would tell a team it has passed the point where the process
costs more than it returns? What signal would say the answer has changed — for a new member, or
for the same team six months later?

**What are the economics of parallel reviewer sub-agents at team size?** Running reviewers in
parallel multiplies token cost by the number of angles, and again by the number of people
running them. Which of the two independent reviews is actually paying for itself? Is the right
unit a run, a person, or a change, and who sees the number when the answer is "too expensive"?
