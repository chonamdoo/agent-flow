# Vendored skill provenance

Some skills here are vendored from [mattpocock/skills](https://github.com/mattpocock/skills)
and adapted to this repository. This record identifies the upstream revision last
compared, so the next sync can use
`git -C <clone> diff <pinned>..HEAD -- skills/` instead of comparing the entire history.

- upstream: `https://github.com/mattpocock/skills`
- pinned commit: `4588b32ecab9ecc9fc8cc6b6c5e7d675b6004b0d` (2026-10-05)
- `diagnosing-bugs` adoption source: `0ab1b63a410a03d3627979a109c8695de27af954` (2026-08-20)
- `diagnosing-bugs` verbatim at upstream commit `4588b32ecab9ecc9fc8cc6b6c5e7d675b6004b0d` (directory tree `de4236cf34757c4e9ea7afe78d46c023213ff254`: `SKILL.md`, `agents/openai.yaml`, `scripts/hitl-loop.template.sh`)

When advancing the pin, record both adopted and intentionally omitted changes in
the table and notes below. A pin alone cannot distinguish reviewed changes from
unreviewed ones. Comparison evidence is in `docs/mattpocock-skills-upstream-audit.md`.

## Mapping

Upstream groups skills under `skills/engineering|productivity/<name>/`; this
repository uses `skills/<name>/`. The remaining relative paths are unchanged.

| Local | Upstream | Status |
| --- | --- | --- |
| `codebase-design` | `engineering/codebase-design` | Locally adapted boundaries, evidence preservation, and tool authority; see below |
| `agent-flow-diagnosing-bugs` + `workflows/diagnosing-bugs.yaml` | `engineering/diagnosing-bugs` | lifecycle wrapper + 9-phase workflow + marker-driven command evidence |
| `diagnosing-bugs` | `engineering/diagnosing-bugs` | Unmodified upstream tree; installed through the lifecycle wrapper's `requires` |
| `domain-modeling` | `engineering/domain-modeling` | Merged description and local evidence/edit-authority rules; see below |
| `tdd` | `engineering/tdd` | Local `requires`, judgment-focused examples, and host-neutral skill access; see below |
| `grill-with-docs` | `engineering/grill-with-docs` | Local `requires` and host-neutral skill access; see below |
| `grilling` | `productivity/grilling` | Local emoji policy, exploration dispatch wording, and em-dash choice; see below |
| `code-review` | `engineering/code-review` | Intentional divergence; see below |
| `resolving-merge-conflicts` | `engineering/resolving-merge-conflicts` (deleted upstream in `daa01d8`) | Local body rewrite; retained for the merge phases; see below |
| `to-prd` | `engineering/to-spec` | Adapted to local PRD terminology |

## Intentional divergences: preserve during sync

- **`requires:` / `delivery:`** are local frontmatter fields absent upstream.
  The installer's `validateSkillDependencies` checks `requires`; `delivery: passive`
  populates the AGENTS.md skill index's `always:` entry. Preserve both when
  replacing upstream content.
- **PRD terminology:** upstream changed PRD to spec. Here, `to-prd`, the
  full-feature `product-brief` phase, and profile `artifacts.prd`/`vocabulary.prd`
  share PRD terminology. Changing only `code-review` would split the vocabulary.
- **`code-review`'s `## Quick start`:** upstream removed it after
  `/setup-matt-pocock-skills` took over entry guidance. That skill is not installed
  here; profile gates and branching live outside the skill and do not replace
  its entry procedure.
- **`/setup-matt-pocock-skills` and `docs/agents/issue-tracker.md` instructions:**
  these commands and paths do not exist here. Do not import instructions that
  depend on them.
- **`grilling` question formatting:** upstream uses question and arrow emoji.
  This repository uses `**Q1** — ...` and a plain arrow instead. The round
  template's `---` separator between questions (`85f83d3`) is adopted in that
  notation. The design tree, frontier, round-based questions, and termination
  conditions remain unchanged.
- **`resolving-merge-conflicts` body and retention:** the local body (`16ba2e7`)
  stops and asks the user whether to abort or which intent wins when the merge
  goal does not decide a conflict, runs the active profile's checks, and stages
  only the paths resolved for the operation. Upstream said "Always resolve; never
  `--abort`" and "Stage everything", then deleted the skill in `daa01d8` ("No
  longer needed"). The `default` and `full-feature` merge phases still declare and
  read it, so it stays. The upstream description quoting is not adopted; the
  parsed value is the same.
- **`Skill` tool invocation wording:** upstream standardized references on
  `call the Skill tool with "<name>"` in `tdd` and `grill-with-docs`. These vendored
  content skills are outside `BUNDLED_HOST_SKILL_NAMES`, so the host picker does
  not expose them. Phase prompts instead resolve `.agent-flow/skills/<name>/SKILL.md`,
  as in the full-feature domain-grill phase. Preserve the accessible path-based
  instruction rather than requiring an unavailable invocation route.
- **`domain-modeling` description:** upstream switched to file-based triggers
  for GLOSSARY.md/ADR and removed `or when another skill needs to maintain the domain model`.
  That trigger is still used here by `grill-with-docs` through `requires` and by
  design/domain-grill phases in the default and full-feature workflows. Keep it
  alongside the file-based triggers.
- **TDD examples and seams:** implementation scaffolds are replaced with
  observable-behavior cases. Preserve valid external boundaries and public
  count/order contracts; do not treat every collaborator or interaction assertion
  as an implementation detail.
- **`grilling` exploration dispatch:** upstream says `dispatch a sub-agent to find it`.
  The local instruction permits direct repository/tool inspection or a read-only
  exploration subagent. A fact available from one tool call does not require delegation.
- **Em-dash choice:** upstream removed em dashes repo-wide (`86cba45` in
  `grilling`, `3216582` everywhere else). The vendored copies keep their em
  dashes; the meaning is unchanged. `diagnosing-bugs` is the exception because it
  is kept verbatim. The `5c89081` description quoting is not adopted: the local
  descriptions contain no unquoted `: ` and parse to the same values.
- **`GLOSSARY.md` convention (`d80fa0f`):** adopted. `domain-modeling`
  (`GLOSSARY-FORMAT.md`, `GLOSSARY-MAP.md`), `tdd`, `codebase-design`,
  `diagnosing-bugs`, and the default design prompt read and write `GLOSSARY.md`.
  Skills do not fall back to `CONTEXT.md`; installed projects rename an existing
  glossary as described in `docs/USAGE.md`.
- **Upstream skills not imported:** `implement-spec`, `pr`, `retro`, and
  `in-progress/chief-of-staff`, added after the previous pin. Reasons are in the
  audit document.
- **2026-09-11 local adaptations:** these changes do not advance the upstream pin.
  `codebase-design` uses semantic ownership and approved seams rather than fixed
  application scaffolds or candidate quotas; preserve behavior coverage before
  deleting tests. `domain-modeling` separates observed code, proposed intent,
  agreed terminology, and permission to edit GLOSSARY.md/ADRs. `code-review`
  distinguishes committed and WIP scopes, includes relevant untracked files, and
  keeps both review axes on the same snapshot without arbitrary finding caps.
  `to-prd` retains approval before publication and allows appropriately scoped
  prototypes rather than imposing document-length or repetition quotas.

## Locally synthesized capabilities

The 2026-09-11 additions below are local synthesis from the task requirements and
linked primary sources, not imports from `mattpocock/skills`:

- `react-scroll-restoration`
- `react-runtime-i18n`
- `nextjs-auth-session`
- `webview-json-rpc-bridge`
- `ga4-ecommerce-events`
- `datadog-rum-sourcemaps`

Each bundle records its source links and applicable version caveats. A retrieval
date for living documentation is not a required dependency version. Preserve the
JSON-RPC specification's copyright and permission notice included in the bridge
bundle.

The corpus-wide English adaptation preserves public APIs, workflow markers,
valid exceptions, and attribution. Korean task aliases remain in the existing
profile routing fields, while the concise-output and writing skills still
support Korean deliverables. Supplied SDUI PART labels and React Native
operational notes whose original source cannot be identified remain explicitly
unverified; they are not represented as verified external standards.

## `agents/openai.yaml`

Upstream includes this file with each skill for Codex picker
`interface.display_name`/`short_description` and, for user-invoked skills,
`policy.allow_implicit_invocation: false`. Do not add it to vendored content skills
that the host picker does not expose.

`agent-flow-diagnosing-bugs` is an exception: it is a user-invoked lifecycle wrapper
in `BUNDLED_HOST_SKILL_NAMES`, linked into `.claude/skills`, `.Codex/skills`, and
`.omp/skills`. It carries both Claude's `disable-model-invocation: true` and Codex's
`policy.allow_implicit_invocation: false` to preserve the invocation policy.

Other vendored content skills are exposed through `.agent-flow/skills/`,
the AGENTS.md index, and phase prompts. Adding an unconsumed `agents/openai.yaml`
would create a second metadata source that can drift from `SKILL.md`.

`diagnosing-bugs` preserves upstream `agents/openai.yaml` as part of the requested
verbatim tree import. It does not add a native host entry; the existing
`agent-flow-diagnosing-bugs` wrapper remains the lifecycle entry.

## Cloudflare security audit

- Upstream: [cloudflare/security-audit-skill](https://github.com/cloudflare/security-audit-skill).
- Pinned revision: `c1c8a8c1471069fb0e188eeaff69b8e8db6564a8`.
- Mapping: upstream `skills/security-audit/` → local `skills/security-audit/`.
- The 21 upstream files are imported verbatim, including domain companions,
  validators, their tests, and the [MIT license](security-audit/LICENSE).
- [Local provenance manifest](security-audit/upstream.json) records this pin
  and each imported file's SHA-256. The manifest is local metadata, not an
  upstream file or an install-time integrity verifier.
- Conditional guidance-mode integration belongs to
  `templates/_shared/review/io-safety.md`; it does not modify the imported
  audit workflow or replace the runner's review verdict contract.
