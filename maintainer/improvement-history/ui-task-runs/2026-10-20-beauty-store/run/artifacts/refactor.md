# Refactor — Store

Reviewed the green diff against code-generation-discipline, react-development-guide, typescript-development-guide and
react-fsd-architecture. No structural change made:
- Imports go down only (app → widgets → features → entities → shared); no feature imports another feature; `PriceView`
  is shared by the card and the product widget from `entities/catalog/ui`.
- No derived state stored: cart totals, price display and listing pages are computed during render.
- Effects are limited to external systems: `localStorage` hydration, the 200 ms suggestion timer, the 6 s carousel interval
  and the outside-pointer listener.
- Candidate refactors rejected as churn: splitting `CartView` into line/summary components (single consumer),
  extracting a shared stepper (two call sites with different semantics).

code-review smell baseline (applied directly, no sub-agents — the review phases own independent reviewers): possible
Duplicated Code in the two quantity steppers (product vs cart line: different bounds and actions — kept); no Repeated
Switches, Speculative Generality or Middle Man found.

`agent-flow run-command -- npm test` → exit 0, Tests 50 passed (50). Behaviour unchanged.

## Completion Gate
skills_checked: true
profile-skill-selection: applied
active-profiles: nextjs
changed-file-skill-resolution: applied
required-profile-skills: checked
missing-required-profile-skills: none
skill-availability: pass
skill-use-evidence: verified
architecture-contract: applied
project-local-skills: checked
project-local-skills-used: code-generation-discipline, tdd, code-review, codebase-design, react-app-shell-error-handling, react-development-guide, typescript-development-guide, react-fsd-architecture, app-shell-error-contract, write-for-work
project-local-skill-docs: applied
