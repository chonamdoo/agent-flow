---
name: setup-matt-pocock-skills
description: Configures a repo for the engineering workflow skills by recording its issue tracker, triage label vocabulary, and domain-doc layout. Use when setting up these skills in a repo for the first time, when tracker/domain conventions are missing, or when switching issue trackers or triage labels.
disable-model-invocation: true
---

# Setup Matt Pocock's Skills

Scaffold the per-repo configuration that the engineering skills assume:

- **Issue tracker** — where issues live (GitHub by default; local markdown is also supported out of the box)
- **Triage labels** — the strings used for the five canonical triage roles
- **Domain docs** — where `CONTEXT.md` and ADRs live, and the consumer rules for reading them

This is a prompt-driven skill, not a deterministic script. Explore, present what you found, confirm with the user, then write.

## Quick start

1. Explore the repo for existing issue-tracker, agent-instruction, domain, ADR, and scratch conventions.
2. Ask only the unresolved questions: issue tracker, triage labels when `triage` is installed, and multi-context layout when monorepo signals exist.
3. Show the exact `## Agent skills` block and generated `docs/agents/*.md` drafts before writing.
4. Write only after confirmation, using the matching seed templates below.

## Seed templates

- [issue-tracker-github.md](./issue-tracker-github.md) — read when the repo uses GitHub Issues.
- [issue-tracker-gitlab.md](./issue-tracker-gitlab.md) — read when the repo uses GitLab Issues.
- [issue-tracker-local.md](./issue-tracker-local.md) — read when issues should be local markdown files.
- [triage-labels.md](./triage-labels.md) — read when recording label/role mappings.
- [domain.md](./domain.md) — read when recording single-context or multi-context domain docs.

## Process

### 1. Explore

Look at the current repo to understand its starting state. Read whatever exists; don't assume:

- `git remote -v` and `.git/config` — is this a GitHub repo? Which one?
- `AGENTS.md` and `CLAUDE.md` at the repo root — does either exist? Is there already an `## Agent skills` section in either?
- `CONTEXT.md` and `CONTEXT-MAP.md` at the repo root
- `docs/adr/` and any `src/*/docs/adr/` directories
- `docs/agents/` — does this skill's prior output already exist?
- `.scratch/` — sign that a local-markdown issue tracker convention is already in use
- Is the `triage` skill installed in `.agent-flow/skills/`? This decides whether Section B runs.
- Monorepo signals — `pnpm-workspace.yaml`, a `workspaces` field in `package.json`, or populated `packages/*/src/`. Their absence means single-context.

### 2. Present findings and ask

Summarise what's present and what's missing. Take the sections in order, one answer at a time. Lead with the recommended answer so the user can accept it in a word. Skip a section when exploration already settled it.

**Section A — Issue tracker.**

> Explainer: The "issue tracker" is where issues live for this repo. Skills like `to-issues`, `triage`, `to-prd`, and `qa` read from and write to it — they need to know whether to call `gh issue create`, write a markdown file under `.scratch/`, or follow some other workflow you describe. Pick the place you actually track work for this repo.

Default posture: these skills were designed for GitHub. If a `git remote` points at GitHub, propose that. If a `git remote` points at GitLab (`gitlab.com` or a self-hosted host), propose GitLab. Otherwise (or if the user prefers), offer:

- **GitHub** — issues live in the repo's GitHub Issues (uses the `gh` CLI)
- **GitLab** — issues live in the repo's GitLab Issues (uses the [`glab`](https://gitlab.com/gitlab-org/cli) CLI)
- **Local markdown** — issues live as files under `.scratch/<feature>/` in this repo (good for solo projects or repos without a remote)
- **Other** (Jira, Linear, etc.) — ask the user to describe the workflow in one paragraph; the skill will record it as freeform prose

Record the choice in `docs/agents/issue-tracker.md`. The GitHub and GitLab templates keep their existing "PRs as a request surface" flag defaulted off; do not add another setup question for it.

**Section B — Triage label vocabulary.**

Skip this section if `triage` is not installed. Otherwise ask one question: "Do you want to keep the default triage labels?" Recommend yes. The defaults are `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, and `wontfix`. Collect overrides only when the user says no.

**Section C — Domain docs.**

Default to **single-context** — one `CONTEXT.md` plus `docs/adr/` at the repo root — without asking. Offer **multi-context**, with a root `CONTEXT-MAP.md` pointing to per-context docs, only when exploration found monorepo signals.

### 3. Confirm and edit

Show the user a draft of:

- The `## Agent skills` block to add to whichever of `CLAUDE.md` / `AGENTS.md` is being edited (see step 4 for selection rules)
- The contents of `docs/agents/issue-tracker.md` and `docs/agents/domain.md`, plus `docs/agents/triage-labels.md` only when `triage` is installed

Let them edit before writing.

### 4. Write

**Pick the file to edit:**

- If `CLAUDE.md` exists, edit it.
- Else if `AGENTS.md` exists, edit it.
- If neither exists, ask the user which one to create — don't pick for them.

Never create `AGENTS.md` when `CLAUDE.md` already exists (or vice versa) — always edit the one that's already there.

If an `## Agent skills` block already exists in the chosen file, update its contents in-place rather than appending a duplicate. Don't overwrite user edits to the surrounding sections.

The block:

```markdown
## Agent skills

### Issue tracker

[one-line summary of where issues are tracked]. See `docs/agents/issue-tracker.md`.

### Triage labels

[one-line summary of the label vocabulary]. See `docs/agents/triage-labels.md`.

### Domain docs

[one-line summary of layout — "single-context" or "multi-context"]. See `docs/agents/domain.md`.
```

Omit the `### Triage labels` block when `triage` is not installed. Then write the docs using the seed templates in this skill folder. Read only the tracker template and domain template, plus the label template when Section B ran:

- [issue-tracker-github.md](./issue-tracker-github.md) — GitHub issue tracker
- [issue-tracker-gitlab.md](./issue-tracker-gitlab.md) — GitLab issue tracker
- [issue-tracker-local.md](./issue-tracker-local.md) — local-markdown issue tracker
- [triage-labels.md](./triage-labels.md) — label mapping, only when `triage` is installed
- [domain.md](./domain.md) — domain doc consumer rules + layout

For "other" issue trackers, write `docs/agents/issue-tracker.md` from scratch using the user's description.

### 5. Done

Tell the user the setup is complete and which engineering skills will now read from these files. Mention they can edit `docs/agents/*.md` directly later — re-running this skill is only necessary if they want to switch issue trackers or restart from scratch.
