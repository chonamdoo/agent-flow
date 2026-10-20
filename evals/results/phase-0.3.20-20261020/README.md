# v0.3.20 release evaluation — blocked

This is an incomplete, failed measurement, not a release-gate export. The paired
run planned 168 units with author 3 / review 2, Codex `gpt-6.1-sol`, concurrency 8,
and the unchanged evaluator and thresholds against v0.3.19. Repeated provider
HTTP 429 responses prevented completion, including a separate tiny writable
sandbox diagnostic. The run was canceled after 19 retained units; failed calls
and partial results remain in private native evidence. No failed unit was
selectively retried and no pass record or tag was produced.

`blocked-measurement.json` records the raw results digest and verified native
receipt counts without publishing prompts, responses, credentials, or paths.
The mandatory comparator rejects this run for incomplete coverage and invalid
units. A complete passing release measurement is still required to publish.
