T6 context measurement (manual, non-shipped)
============================================

Run from the bound worktree with its Python environment (PyYAML and the normal
agent-flow runtime dependencies must already be available). No installation or
workflow lifecycle command is performed. Actual provider runs require installed,
authenticated claude/codex executables and explicit model names. They may incur
provider charges. Model evaluation is manual/non-blocking, never independent
runner review approval. The harness does not reduce runner reviewer counts.

Scoped render/accounting pair (no model calls):

  python tools/t6-context/run.py \
    --baseline-archive /tmp/agent-flow-t6-20260913-160840-baseline.tar \
    --after . --output /tmp/t6-render-pair-NEW \
    --provider claude --provider codex --render-only \
    --case ui-single-repository --case ui-cross-context \
    --case local-framework-allowed --case pending-structural

Real scoped before/after pair, using the SAME provider/model for both sides:

  python tools/t6-context/run.py \
    --baseline-archive /tmp/agent-flow-t6-20260913-160840-baseline.tar \
    --after . --output /tmp/t6-live-pair-NEW \
    --provider claude --provider codex \
    --claude-model YOUR_INSTALLED_CLAUDE_MODEL \
    --codex-model YOUR_INSTALLED_CODEX_MODEL \
    --case ui-single-repository --case ui-cross-context --timeout 300

Repeat --case for the desired fixed cases. Omit --case to execute all 48 cases
(96 invocations per provider: one before and one after per fixture). This is a
manual fixture evaluation, not a project-wide local test suite. Start with a
scoped pair. Every --output must be new; existing records are never overwritten.

Focused deterministic checks (integration owner only, after concurrent edits):

  pytest -q tests/test_t6_context_measurement.py

What is measured
----------------

The repository-root baseline git archive and current source assets are copied
into separate evidence snapshots. No old run, approval, pin, stash, installation,
or provider configuration is changed. The subprocess renderer imports each
snapshot's actual runtime, loads its bundled default final-review phase, and
adds the fixture's explicit platform skills through the existing PhaseSkills
and requires resolver. HOME is isolated for resolution so installed personal
skills do not contaminate the fixed corpus. Complete author and EVERY applicable
final angle/provider prompt are saved, not merely reviewer base envelopes.
These envelope records are render-only; call_count is zero. They do not claim
that a provider has read an envelope or any linked document.
Ordinary required skills remain path-linked; only the selected local contract
and its declared references are delivered inline. Capturing an ordinary body's
bytes and provenance does not deliver it. The accounting counts actual body
occurrences in each prompt, so reference-only documents contribute zero.
The required-read plan names one representative path per exact reference body.
If an identical local body is already inline, that body satisfies both selections
without another read. Other selected paths remain non-actionable provenance.

Semantic evaluation deliberately has a different, explicit surface. Each side
gets the same fixture task, code and oracle, plus that side's full resolved
required skill bodies and selected local required references. Current-runtime
bytes come from immutable normative_documents metadata. The historical baseline
has no such metadata: ordinary required files are explicitly read from its fixed
snapshot, while local documents use the baseline's captured contract contents.
No alternative dependency loader, semantic summary, generated compliance table,
or string-based required-skill discovery is used. Full-byte duplicate bodies
are emitted once in each semantic input; all document path/hash identities remain
in render.json. This controlled semantic corpus is NOT evidence of runtime
transport savings: baseline runtime normally only referenced ordinary skills,
and the harness explicitly expands them to give both sides full rules.

Fixture required_references is an optional list of skills-relative paths asserting
that the resolver already selected those documents. It never selects extra rules
or opens arbitrary files. Missing selected references fail explicitly. The obsolete
references field is rejected; absolute, parent and symlink escapes are rejected
before reading reference content. All 48 current cases have empty assertions.

The exact final semantic user prompt sent on stdin is saved as call/stdin.raw.
command.json preserves argv, cwd and stdin hash. Raw stdout and stderr stream to
files without truncation. Codex's raw last response is also preserved; Claude's
raw response remains in its JSON event stream. No oracle answer is included in
the prompt. The unchanged fixture and source-snapshot checks are separate from
provider judgment. Host/model/executable, fixture, scorer, source-file, document
and prompt hashes bind the observations. A fresh versioned fixture set is
required for intentional revisions; the CLI cannot re-freeze or repair fixtures.

Accounting semantics
--------------------

* raw_input_bytes: UTF-8 bytes of the exact harness-supplied prompt, not tokens.
* delivered_normative_bytes: bytes of complete bodies physically included in
  that prompt. Referenced/pinned manifest bytes are never treated as delivery.
* duplicated_bytes: extra exact full-body occurrences in one input. There is no
  fuzzy, sentence-level or cross-call deduplication. suppressed_exact_duplicate_bytes
  reports duplicate source selections removed by semantic assembly, separately.
* render_seconds: resolver and composition wall time inside the isolated worker.
  render_execution.wall_seconds additionally includes interpreter/setup overhead.
* execution.call_count: actual provider CLI subprocess launches (0 if unlaunched,
  1 if launched, including timeout/failure). Renderer subprocesses are separately
  named and never included in provider-call counts. There are no automatic retries.
* execution.wall_seconds: actual provider process duration, including its startup.
* usage input/cached/uncached tokens: only actual provider event values. Codex
  turn.completed input_tokens includes cached_input_tokens; their measured
  difference is uncached. Claude result.usage input_tokens excludes cache reads
  and writes: total adds input, cache_read_input and cache_creation_input tokens;
  uncached includes cache creation. Creation is also retained as its own field.
  No bytes-to-tokens conversion is performed. Missing components are unavailable,
  not zero. An unknown provider format remains unavailable with all raw data kept.
* provider_call_count: distinct Claude assistant message IDs when exposed;
  Codex internal model-request count is unavailable because turn.completed is
  aggregate usage, not a request log. Do not relabel CLI launches as API calls.

Hidden system prompts, global provider instructions, server-side retries,
provider-internal calls, and unobserved tool-read context are not reconstructed.
Codex runs read-only with ignored user configuration, disabled project document
injection and explicit no-tool instructions; Claude disables tools/MCP/settings
sources and session persistence. These are not a claim that CLI internals or
provider context are fully observable. Usage may cover more than stdin bytes.
Compare only like surfaces. Provider/configuration failures stay failures, not
an invitation to switch commands silently or manufacture usage.

Verdicts and evidence
---------------------

The existing evals/skill_tasks.py score_review is used unchanged: verdict,
finding location, defect coverage, false positives and host success stay distinct.
The inline corpus does not pretend to be an executed document tool-read trace.
Status distinguishes not-executed, timeout, provider-failure, fixture-mutated,
malformed-response, oracle-mismatch, matched-oracle, and render-failure.

comparison.json reports observed verdict parity only when both calls produced
valid successful responses, and reports both_match_oracle separately. Two wrong
verdicts can agree; agreement is NOT correctness or approval. Missing, malformed,
failed and unexecuted pairs cannot acquire parity or byte-savings claims.
summary.json intentionally makes no savings-percentage claim. Exit 1 preserves
all collected rows if a render, provider, fixture or oracle fails. Render-only
success means rendering worked, not that semantic parity passed.

The original 21 fixtures cover the named T6 normal/violation/exception cases:
direct UI repository versus cross-context orchestration, DB-only/simple adapters,
identical safe representation, use-case versus pure policy/Protocol composition,
Android application Inject versus pure-domain DI, three SDUI exceptions and
stateful renderer violation, adopted/unadopted DI, local and pending decisions.
Before any execution, an explicitly authorized extension added 27 AppShell cases
without changing those original cases. The six family mappings in
docs/issues/0007-t6-appshell-obligations.json name proposed coverage, not passes:

  appshell-classification: global-only ownership, duplicate display, local 403.
  appshell-queue: success acknowledgement, premature consume, new occurrence/retry.
  appshell-lifetime: collector pause, false restart guarantee, server outbox exception.
  appshell-boundaries: metadata/port ownership, interceptor UI, local value shape.
  appshell-platform-recovery: Android, iOS, React and React Native each have a
    normal, violation and exception case; exceptions include absent maintenance
    flow, existing UIKit, route-local 404 and an isolated mini-app.
  appshell-scoped-delivery: required shared dependency, forced Clean in local
    mode, and feature-local/conditional trusted-server non-target applicability.

For a scoped AppShell pair, replace the --case arguments with, for example:
  --case appshell-queue-ack-after-success
  --case appshell-queue-premature-consumption
  --case appshell-queue-new-occurrence-retry
Each argument belongs on the same invocation (use shell line continuations).
These cases awaited execution when introduced; completed v2 observations are below.
These cases are not a complete semantic oracle for every platform, SOLID,
workflow or gate rule. Remaining obligations require their own scoped evidence.
Neither row counts nor markers prove zero lost obligations. The existing
layer_boundary behavioral oracle remains separate and is not altered or
automatically run by this tool.

Fixture version 2 records an explicitly authorized manual input repair after
the original ui-single-repository smoke produced before=request-changes and
after=approve on both providers. Baseline findings exposed real unrelated
presentation defects: an empty-string initial sentinel, absent async render
states/error handling, and an unobservable mutable field. The first fixture now
contains an actual immutable React state hook, cancellation/error handling and
a view for each reachable state; its direct-repository question is unchanged.
An audit of all 48 inputs repaired 15 cases and retained 33, preserving every
target verdict/defect and scorer. Existing finding ranges still cover the same
defects; precise location mappings and per-case rationale are in
fixtures/repair-history-v2.json. Version 2 retains its own manifest and identity.
Original complete cases and manifest were preserved in the active run as
t6-fixtures-v1-cases.json and t6-fixtures-v1-manifest.json before any repair.
The original smoke outputs remain mismatch evidence, not superseded passes.
Main subsequently evaluated all 48 v2 cases (192 CLI calls across both sides and
providers), reporting six failed rows across three cases and stable source
snapshots. These full v2 observations remain v2 evidence, including failures.

The current manifest identifies version 3, which changes only those three cases:
ui-single-repository derives UiState through pure render-time mapping instead of
inside the effect; sdui-event-direction uses sealed PageState.Content with actual
zero initialization; ui-cross-context calibrates the same defect's bounded
finding region from lines 5-7 to its complete hook at lines 3-8. No target verdict,
expected defect count, or scorer changes. Complete v2 cases and manifest were
preserved as t6-fixtures-v2-cases.json and t6-fixtures-v2-manifest.json in the run.
fixtures/repair-history-v3.json records the exact 48-case v2/v3 identity crosswalk
and the original result paths. Run fresh v3 pairs only for the three changed
identities; retain the other 45 exact-identity observations explicitly as v2.
Do not describe this mixed-version evidence as a homogeneous v3 rerun, or turn
an earlier mismatch into a pass without a new measurement.
The retained three-case v3 execution contains 12 valid oracle-matching responses
and six equal before/after verdict pairs. Combined with the 45 unchanged v2
cases, the current fixture evidence has 192 oracle matches and 96 equal pairs.
This combines identified historical executions, not a new homogeneous run or
approval of a later runtime whose exact semantic inputs have not been compared.

No provider parity, token savings, wall-time improvement, complete delivery
savings, or independent reviewer approval has been measured merely by adding
this harness. Run the scoped commands above and retain their actual failures.
