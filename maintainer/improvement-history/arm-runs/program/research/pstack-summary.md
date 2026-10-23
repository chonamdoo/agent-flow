# pstack (cursor/plugins, pstack/) — summary of the public README and skills/poteto-mode/SKILL.md, read 2026-10-23

Source: https://github.com/cursor/plugins/tree/main/pstack and .../pstack/skills/poteto-mode/SKILL.md (summarised by the operator; treat as a secondary source).

- A plugin of skills for Cursor and Claude Code by "poteto". Goal: "write less, but higher quality code".
- `/poteto-mode`: "it reads your request, picks from a set of playbooks, and runs the other skills as the steps need them."
- Routing rule in SKILL.md: "Match the task to a playbook below and open its full steps at playbooks/<title>.md." Each playbook has a one-line description. Overrides: PR-status -> Babysit; large/cross-cutting or later-reviewed work -> figure-it-out even if a narrower playbook fits; multi-day -> Orchestrate; empirical questions -> Prototype.
- Fallback: figure-it-out when no playbook fits. No rule to ask the user which playbook; "never block on the human" for reversible work.
- The matched playbook's steps are copied verbatim into the todo list; skipped steps stay with "skip: <reason>". Every playbook ends with "Opening a PR".
- 23 playbooks (investigation, bug fix, perf, hillclimb, feature, refactoring, prototype, visual parity, eval, babysit, shipping, autonomous run, orchestrate, ...), ~35 standalone skills, 24 principles. Role->model mapping configurable.
- No published quantitative evaluation; an /eval playbook exists for blinded skill-change tests. No numbers reported.
