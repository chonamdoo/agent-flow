# qa skill — measured results

Evaluator-only record. Executors never see this file, `expectations.json` or `discovery.json`.

## Run 2026-10-20 (Claude CLI 2.1.304, default model, `--max-budget-usd 1.5` per executor)

### B — behavior, skill vs baseline

| Case | Split | skill | baseline | Baseline's own verdict (human reading) |
|---|---|---|---|---|
| B1-app-device-missing | dev | pass (`blocked`) | fail: no status | "NOT VERIFIED" — correct substance, does not separate blocked from request-changes |
| B2-web-flaky | dev | pass (`request-changes`) | fail: no status | "NOT SIGNED OFF" — correct |
| B3-web-zero-executed | heldout | pass (`request-changes`) | fail: no status | "NOT VERIFIED (inconclusive)" — correct |
| B4-api-auth-defect | dev | pass (`request-changes`, `/orders` 200 repro) | fail: no status | "FAIL" with curl repro — correct |
| B5-api-auth-correct | heldout | pass (`green`, SPEC-1/2 covered) | fail: no status | "PASS" — correct |
| B6-web-snapshot-baseline | dev | pass (`request-changes`, snapshot untouched) | fail: no status | "FAIL", did not update the snapshot — correct |

- Mechanical: skill 6/6 (all required criteria `pass`, verified), baseline 0/6.
- What the difference is: the baseline reached the right judgment in all six cases and never edited the
  fixture. It fails only `C-status` — it does not emit the Agent Flow status vocabulary the `qa` phase
  routes on (`green` / `request-changes` / `blocked`), and on B1 it cannot tell the runner that the
  failure is environmental (`blocked`) rather than a code defect. **These cases show contract conformance,
  not better QA judgment; they are saturated for this model.** Cases where an unaided model gets the
  judgment wrong are still needed before claiming judgment uplift.
- Cost: skill $0.73 (7–9 turns, ~200–244k input incl. cache), baseline $0.51 (5–6 turns, ~152–193k).
  The skill costs about +44% per QA run here, mostly from reading the skill and one reference.
- The first judge pass mis-scored skill B4/B5 (`Status: **green**` not parsed); fixed the status
  pattern and re-judged the saved artifacts with `--rejudge`. Workspace criteria (`C-unchanged`,
  `C-server-stopped`) keep the verdicts recorded during the run.

### Discovery — competitors `tdd`, `code-review`, `diagnosing-bugs` installed (plus host built-ins)

| Case | Split | Expected | Loaded | Result |
|---|---|---|---|---|
| D1 QA the cart change | dev | qa | qa | ok |
| D2 smoke-test GET /orders | dev | qa | qa | ok |
| D3 e2e test postcode validation | heldout | qa | qa | ok |
| D4 write unit tests | dev | not qa | none | ok |
| D5 review this diff | dev | not qa | none | ok |
| D6 why did the test fail | heldout | not qa | none | ok |

6/6. A first run that appended "planning only, do not run anything" to each request loaded no skill at
all (including competitors), so that suffix was removed; requests now go in as written with
`--max-turns 4`.

### Unverified

- App surface beyond the missing-device path (a real simulator run) — no device in this environment.
- Exploratory/bug-bash reference — no case exercises it.
- Inside Agent Flow, the skill is delivered by the `qa` phase (`skills.required: [qa]`), not by host
  discovery; the in-workflow run is covered by phase_eval only once a QA phase case exists.
