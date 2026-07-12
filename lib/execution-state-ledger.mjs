import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

export const LEDGER_MODES = Object.freeze([
  "artifacts-only",
  "ledger-always",
  "ledger-selective",
  "action-self-review",
]);

const LEDGER_MODE_SET = new Set(LEDGER_MODES);
const SCHEMA_VERSION = 1;
const EXTRACTOR_VERSION = "execution-ledger-v1";
const MAX_FIX_LOOP_ROUNDS = 3;
const MAX_BLOCK_LINES = 5;
const MAX_ITEM_BYTES = 160;
const MAX_BLOCK_BYTES = 720;
const REVIEW_SUMMARY_SCHEMA_VERSION = 1;
const USAGE_RECEIPT_SCHEMA_VERSION = 1;
const USAGE_RECEIPT_KIND = "provider-usage-receipt";
const CONTROL_RECEIPT_SCHEMA_VERSION = 1;
const CONTROL_RECEIPT_KIND = "execution-control-receipt";
const PROVIDER_ATTESTATION_SCHEMA_VERSION = 1;
const PROVIDER_ATTESTATION_KIND = "provider-usage-attestation";
const PROVIDER_ATTESTATION_ALGORITHM = "RS256";
const VERIFIED_USAGE_EVIDENCE = "verified-provider-receipt";
const SUMMARY_USAGE_EVIDENCE = "summary-only";
const VERIFIED_CONTROL_EVIDENCE = "verified-control-receipt";
const COMMITMENT_SHA256 = /^[a-f0-9]{64}$/;
const COMMITTED_RECORD_TYPES = Object.freeze(["capture", "injection", "usage"]);
const STRICT_RFC3339 = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$/;
const ELIGIBLE_PHASES = new Set([
  "implement",
  "implement-fix",
  "red",
  "green",
  "refactor",
  "fix-loop",
  "pr-ci-fix",
  "pr-comment-fix",
]);
const SELECTIVE_FIX_PHASES = new Set(["fix-loop", "implement-fix"]);
const FIX_ROUTE_TARGETS = new Set([
  "fix-loop",
  "implement-fix",
  "refactor",
  "pr-ci-fix",
  "pr-comment-fix",
]);
const REVIEW_PHASES = new Set([
  "review",
  "final-review",
  "multi-review",
  "architecture-review",
  "plan-review",
]);
const EXPOSURE_POLICY = Object.freeze({
  schema_version: 1,
  selective_trigger: "repeat-or-literal-fix-loop",
  selective_fix_phases: [...SELECTIVE_FIX_PHASES].sort(),
  eligible_phases: [...ELIGIBLE_PHASES].sort(),
  excluded_phases: ["multi-review"],
  independent_reviewer_exclusion: "phase.multi_review-or-multi-review-id",
  non_agent_excluded_phases: [
    "commit",
    "gates",
    "merge",
    "merge-approval",
    "pr-watch",
    "push-pr",
  ],
  target_prompt_kind: "action-agent",
  max_lines: MAX_BLOCK_LINES,
  max_bytes: MAX_BLOCK_BYTES,
  per_exposure_token_cap: Math.ceil(MAX_BLOCK_BYTES / 4),
});
const ANSI_PATTERN = /\x1B(?:[@-_][0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1B\\))/g;
const CONTROL_PATTERN = /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g;

export function resolveLedgerMode(value) {
  const normalized = String(value ?? "").trim().toLowerCase();
  if (!normalized) return "artifacts-only";
  if (!LEDGER_MODE_SET.has(normalized)) {
    throw new Error(`invalid AGENT_FLOW_LEDGER_MODE: ${value}`);
  }
  return normalized;
}

export function validateExecutionStateLedger({
  runDir,
  runId,
  mode,
  requireCompletion = false,
}) {
  try {
    const paths = ledgerPaths(runDir);
    recoverPendingTransaction(path.resolve(runDir), paths);
    const config = readJson(paths.config);
    const normalizedMode = resolveLedgerMode(mode ?? config.mode);
    assertRunDocument(config, runId ?? config.run_id, normalizedMode, "config.json");
    assertRunnerControlSnapshot(config);
    const workflow = readJson(paths.workflow);
    assertWorkflowSnapshot(config, workflow);
    if (normalizedMode !== "artifacts-only") {
      assertLedger(readJson(paths.ledger), config.run_id);
    }
    const validation = validateCommittedRecords({
      runDir: path.resolve(runDir),
      paths,
      config,
      workflow,
      requireCompletion,
    });
    return {
      ok: true,
      verified: true,
      completed: Boolean(validation.completion),
      event_count: validation.eventCount,
      event_head_sha256: validation.eventHeadSha256,
      config,
      workflow,
      captures: validation.recordsByType.capture,
      injections: validation.recordsByType.injection,
      usage: validation.recordsByType.usage,
      metrics: validation.metrics,
      reasons: [],
    };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return failOpen(error, {
      verified: false,
      completed: false,
      reasons: [message],
    });
  }
}

function canonicalWorkflowSnapshot(workflowId, workflowPhases) {
  const id = requireString(workflowId, "workflow_id");
  const phases = Array.isArray(workflowPhases) ? workflowPhases : [];
  if (phases.length === 0) throw new Error("missing execution ledger workflow phases");
  return {
    schema_version: SCHEMA_VERSION,
    workflow_id: id,
    phases: phases.map((phase, index) => {
      if (!isPlainObject(phase)) throw new Error(`invalid execution ledger workflow phase: ${index}`);
      const routes = phase.routes === undefined || phase.routes === null
        ? null
        : canonicalWorkflowRoutes(phase.routes, index);
      const requiredMarkers = phase.required_markers === undefined
        ? []
        : phase.required_markers;
      if (!Array.isArray(requiredMarkers) || requiredMarkers.some((item) => typeof item !== "string")) {
        throw new Error(`invalid execution ledger workflow required_markers: ${index}`);
      }
      return {
        index,
        id: requireString(phase.id, `workflow phase ${index} id`),
        artifact: optionalString(phase.artifact),
        description: typeof phase.description === "string" ? phase.description : "",
        instruction: typeof phase.instruction === "string"
          ? phase.instruction
          : (typeof phase.prompt === "string" ? phase.prompt : ""),
        prompt: optionalString(phase.prompt),
        required_markers: [...requiredMarkers],
        optional: phase.optional === true,
        pause_after: phase.pause_after === true,
        multi_review: phase.multi_review === true,
        cite_lore: phase.cite_lore === true,
        routes,
      };
    }),
  };
}

function canonicalWorkflowRoutes(value, index) {
  if (!isPlainObject(value)) throw new Error(`invalid execution ledger workflow routes: ${index}`);
  const routes = {};
  for (const key of Object.keys(value).sort(compareCodePoints)) {
    const target = value[key];
    if (!key || typeof target !== "string" || !target) {
      throw new Error(`invalid execution ledger workflow route: ${index}`);
    }
    routes[key] = target;
  }
  return routes;
}

function canonicalRunnerControlSnapshot({
  task,
  workflowId,
  workflowSnapshotSha256,
  baseCommit,
  runSnapshot,
  exposurePolicySha256,
  executionControlsSha256,
}) {
  if (!isPlainObject(runSnapshot)) throw new Error("invalid execution ledger run snapshot");
  return {
    schema_version: SCHEMA_VERSION,
    runtime_id: requireIdentifier(runSnapshot.runtime_id, "runtime_id"),
    task_sha256: sha256(String(task ?? "")),
    workflow_id: requireString(workflowId, "workflow_id"),
    workflow_sha256: requireCommitment(workflowSnapshotSha256, "workflow_sha256"),
    base_commit: optionalString(baseCommit),
    profile_snapshot_sha256: requireCommitment(
      runSnapshot.profile_snapshot_sha256,
      "profile_snapshot_sha256",
    ),
    installed_skill_plan_sha256: requireCommitment(
      runSnapshot.installed_skill_plan_sha256,
      "installed_skill_plan_sha256",
    ),
    local_skill_plan_sha256: requireCommitment(
      runSnapshot.local_skill_plan_sha256,
      "local_skill_plan_sha256",
    ),
    lore_snapshot_sha256: requireCommitment(
      runSnapshot.lore_snapshot_sha256,
      "lore_snapshot_sha256",
    ),
    prompt_controls_sha256: requireCommitment(
      runSnapshot.prompt_controls_sha256,
      "prompt_controls_sha256",
    ),
    fix_loop_max_rounds: MAX_FIX_LOOP_ROUNDS,
    exposure_policy_sha256: requireCommitment(
      exposurePolicySha256,
      "exposure_policy_sha256",
    ),
    execution_controls_sha256: requireCommitment(
      executionControlsSha256,
      "execution_controls_sha256",
    ),
  };
}

function canonicalExecutionControls(experiment) {
  if (!isPlainObject(experiment)) throw new Error("invalid execution ledger experiment controls");
  const pricingSnapshot = normalizePricingSnapshot(experiment.pricing_snapshot);
  const publicKey = normalizeProviderPublicKey(experiment.provider_attestation_public_key);
  const keyId = optionalControlIdentifier(
    experiment.provider_attestation_key_id,
    "provider_attestation_key_id",
  );
  if ((publicKey === null) !== (keyId === null)) {
    throw new Error("provider attestation key id and public key must be configured together");
  }
  return {
    model_id: optionalString(experiment.model_id),
    tool_permissions_sha256: optionalCommitment(
      experiment.tool_permissions_sha256,
      "tool_permissions_sha256",
    ),
    system_prompt_sha256: optionalCommitment(
      experiment.system_prompt_sha256,
      "system_prompt_sha256",
    ),
    caps_sha256: optionalCommitment(experiment.caps_sha256, "caps_sha256"),
    provider_retry_policy_sha256: optionalCommitment(
      experiment.provider_retry_policy_sha256,
      "provider_retry_policy_sha256",
    ),
    provider_max_retries: optionalNonnegativeInteger(
      experiment.provider_max_retries,
      "provider_max_retries",
    ),
    pricing_snapshot: pricingSnapshot,
    pricing_snapshot_sha256: pricingSnapshot === null
      ? null
      : sha256(canonicalJson(pricingSnapshot)),
    provider_attestation_key_id: keyId,
    provider_attestation_public_key: publicKey,
    provider_attestation_public_key_sha256: publicKey === null
      ? null
      : sha256(canonicalJson(publicKey)),
  };
}

function normalizePricingSnapshot(value) {
  if (value === undefined || value === null) return null;
  assertExactObjectKeys(value, [
    "currency",
    "input_per_million",
    "output_per_million",
    "snapshot_id",
  ], "pricing snapshot");
  const input = normalizePricingRate(value.input_per_million, "pricing input_per_million");
  const output = normalizePricingRate(value.output_per_million, "pricing output_per_million");
  return {
    currency: requireString(value.currency, "pricing currency"),
    input_per_million: input,
    output_per_million: output,
    snapshot_id: requireString(value.snapshot_id, "pricing snapshot_id"),
  };
}

function normalizeProviderPublicKey(value) {
  if (value === undefined || value === null) return null;
  assertExactObjectKeys(value, ["e", "kty", "n"], "provider attestation public key");
  if (value.kty !== "RSA") throw new Error("provider attestation public key must be RSA");
  const modulus = decodeBase64Url(value.n, "provider attestation modulus");
  const exponent = decodeBase64Url(value.e, "provider attestation exponent");
  if (
    modulus.length < 128
    || modulus[0] < 0x80
    || exponent.length === 0
    || exponent[0] === 0
    || exponent.length > 4
  ) {
    throw new Error("provider attestation RSA key is too weak or malformed");
  }
  const exponentValue = exponent.reduce((total, byte) => (total * 256) + byte, 0);
  if (exponentValue < 3 || exponentValue % 2 === 0) {
    throw new Error("provider attestation RSA exponent is invalid");
  }
  return { kty: "RSA", n: value.n, e: value.e };
}

export function initializeExecutionStateLedger({
  runDir,
  runId,
  mode,
  experimentEnabled = false,
  task,
  workflowId,
  workflowPhases,
  baseCommit = null,
  experiment = {},
  runSnapshot = {},
}) {
  try {
    if (experimentEnabled !== true) return { ok: true, enabled: false };
    const normalizedMode = resolveLedgerMode(mode);
    const paths = ledgerPaths(runDir);
    const workflow = canonicalWorkflowSnapshot(workflowId, workflowPhases);
    const workflowSnapshotSha256 = sha256(canonicalJson(workflow));
    const exposurePolicySha256 = sha256(canonicalJson(EXPOSURE_POLICY));
    const executionControls = canonicalExecutionControls(experiment);
    const executionControlsSha256 = sha256(canonicalJson(executionControls));
    const runnerControlSnapshot = canonicalRunnerControlSnapshot({
      task,
      workflowId,
      workflowSnapshotSha256,
      baseCommit,
      runSnapshot,
      exposurePolicySha256,
      executionControlsSha256,
    });
    const config = {
      schema_version: SCHEMA_VERSION,
      run_id: requireString(runId, "run_id"),
      mode: normalizedMode,
      task_sha256: sha256(String(task ?? "")),
      workflow_id: requireString(workflowId, "workflow_id"),
      workflow_sha256: workflowSnapshotSha256,
      workflow_snapshot_sha256: workflowSnapshotSha256,
      base_commit: optionalString(baseCommit),
      fix_loop_max_rounds: MAX_FIX_LOOP_ROUNDS,
      runner_control_snapshot: runnerControlSnapshot,
      runner_control_snapshot_sha256: sha256(canonicalJson(runnerControlSnapshot)),
      exposure_policy: EXPOSURE_POLICY,
      exposure_policy_sha256: exposurePolicySha256,
      per_exposure_token_cap: EXPOSURE_POLICY.per_exposure_token_cap,
      experiment_id: optionalString(experiment.experiment_id),
      ...executionControls,
      execution_controls_sha256: executionControlsSha256,
    };
    writeImmutableJson(paths.workflow, workflow);
    writeImmutableJson(paths.config, config);
    ensureJsonl(paths.captures);
    ensureJsonl(paths.injections);
    ensureJsonl(paths.usage);
    ensureJsonl(paths.events);
    if (normalizedMode !== "artifacts-only") {
      writeImmutableJson(paths.ledger, emptyLedger(config.run_id));
    }
    if (!fs.existsSync(paths.metrics)) {
      writeCanonicalJson(paths.metrics, emptyMetrics(config.run_id, normalizedMode, config));
    } else {
      assertRunDocument(readJson(paths.metrics), config.run_id, normalizedMode, "metrics.json");
    }
    return { ok: true, enabled: true, config };
  } catch (error) {
    return failOpen(error);
  }
}

export function captureExecutionState({
  runDir,
  runId,
  mode,
  experimentEnabled = false,
  phase,
  artifactPath,
  projectRoot,
  round,
  fixLoopRounds = 0,
  generatedAt = new Date().toISOString(),
  workflowId,
  routeKey,
  routedTo,
  transitionOccurrenceId = null,
}) {
  try {
    if (experimentEnabled !== true) return { ok: true, enabled: false, captured: false };
    const phaseId = String(phase?.id ?? "");
    const context = loadContext(runDir, runId, mode);
    const snapshotPhase = context.workflow.phases.find((candidate) => candidate.id === phaseId);
    const suppliedRoutes = phase?.routes ?? null;
    const routesMatch = snapshotPhase?.routes === null
      ? suppliedRoutes === null
      : isPlainObject(suppliedRoutes)
        && Object.entries(suppliedRoutes).every(([key, target]) => snapshotPhase.routes?.[key] === target);
    if (
      !snapshotPhase
      || snapshotPhase.multi_review !== (phase?.multi_review === true)
      || !routesMatch
    ) {
      throw new Error(`execution ledger phase does not match workflow snapshot: ${phaseId}`);
    }
    const sourceEligible = isLedgerSourcePhase(snapshotPhase);
    const normalizedRound = normalizeRound(round);
    const timestamp = normalizeTimestamp(generatedAt);
    const normalizedWorkflowId = String(workflowId ?? context.config.workflow_id);
    const normalizedRouteKey = String(routeKey ?? "");
    const normalizedRoutedTo = String(routedTo ?? "");
    const normalizedFixLoopRounds = Math.max(0, integerOrZero(fixLoopRounds));
    const normalizationContext = {
      extractor_version: EXTRACTOR_VERSION,
      project_root: canonicalProjectRoot(projectRoot),
    };
    const sourceIdentities = captureSourceIdentities(
      context.runDir,
      artifactPath,
      phaseId,
      normalizedRouteKey,
      sourceEligible,
    );
    const normalizedTransitionOccurrenceId = normalizeTransitionOccurrenceId(
      transitionOccurrenceId,
      {
        runId: context.config.run_id,
        workflowId: normalizedWorkflowId,
        phaseId,
        round: normalizedRound,
        routeKey: normalizedRouteKey,
        routedTo: normalizedRoutedTo,
        fixLoopRounds: normalizedFixLoopRounds,
        sourceIdentities,
      },
    );
    if (context.completion) {
      return completedCaptureReplay(context, {
        phaseId,
        artifactPath,
        projectRoot,
        round: normalizedRound,
        fixLoopRounds,
        workflowId: normalizedWorkflowId,
        routeKey: normalizedRouteKey,
        routedTo: normalizedRoutedTo,
        sourceEligible,
        sourceIdentities,
        transitionOccurrenceId: normalizedTransitionOccurrenceId,
      });
    }
    const priorCaptures = readJsonl(context.paths.captures);
    const equivalentCapture = findEquivalentCapture(priorCaptures, {
      runDir: context.runDir,
      artifactPath,
      phaseId,
      round: normalizedRound,
      workflowId: normalizedWorkflowId,
      routeKey: normalizedRouteKey,
      routedTo: normalizedRoutedTo,
      fixLoopRounds: normalizedFixLoopRounds,
      normalizationContext,
      sourceEligible,
      sourceIdentities,
      transitionOccurrenceId: normalizedTransitionOccurrenceId,
    });
    if (equivalentCapture) return captureReplayResult(equivalentCapture);
    const sourceRecords = [];
    const extractedEntries = [];
    const gateResolutions = [];
    let gateSnapshotResolution = null;
    let gateSourceProvenance = null;
    const gateCommandFingerprints = [];
    let unsupportedCount = 0;
    let gateObservation = null;
    let reviewObservation = null;
    if (phaseId === "gates") {
      const source = archiveSource(runDir, artifactPath, "/", timestamp, normalizedRound, context.config.run_id);
      gateSourceProvenance = source.provenance;
      sourceRecords.push(source.provenance);
      const extracted = extractGateEntries(source, projectRoot, timestamp, normalizedRound);
      extractedEntries.push(...extracted.entries);
      gateResolutions.push(...extracted.resolutions);
      gateCommandFingerprints.push(...extracted.command_fingerprints);
      unsupportedCount += extracted.unsupported_count;
      gateObservation = extracted.gate_observation;
    } else if (phase?.multi_review === true || REVIEW_PHASES.has(phaseId)) {
      const summary = createCanonicalReviewSummary({
        runDir,
        runId: context.config.run_id,
        phaseId,
        artifactPath,
        routeKey,
        projectRoot,
        generatedAt: timestamp,
        round: normalizedRound,
      });
      if (summary) {
        const source = archiveSource(
          runDir,
          summary.path,
          "/",
          timestamp,
          normalizedRound,
          context.config.run_id,
        );
        sourceRecords.push(source.provenance);
        const extracted = extractReviewEntries(source, projectRoot, timestamp, normalizedRound);
        extractedEntries.push(...extracted.entries);
        unsupportedCount += extracted.unsupported_count + summary.unsupported_count;
        sourceRecords.push(summary.artifact_provenance);
        reviewObservation = {
          complete: summary.complete,
          phase_id: phaseId,
          finding_fingerprints: extracted.entries.map((entry) => entry.payload.finding_fingerprint),
          provenance: { ...source.provenance, selector: "/review-result" },
        };
      } else {
        if (artifactPath && fs.existsSync(artifactPath)) {
          const source = archiveSource(
            runDir,
            artifactPath,
            "/unsupported-review-summary",
            timestamp,
            normalizedRound,
            context.config.run_id,
          );
          sourceRecords.push(source.provenance);
        }
        unsupportedCount += 1;
      }
    }

    if (gateObservation) {
      gateObservation.attempt = priorCaptures.filter((capture) => capture.phase === "gates").length + 1;
    }
    const routing = routingObservation(
      context.workflow,
      snapshotPhase,
      normalizedRouteKey,
      normalizedRoutedTo,
    );
    unsupportedCount += routing.unsupported_count;
    if (gateObservation) {
      gateObservation.passed = canonicalGatePassed(gateObservation, routing, normalizedRoutedTo);
      if (gateObservation.snapshot_complete === true && gateSourceProvenance) {
        gateSnapshotResolution = { ...gateSourceProvenance, selector: "/" };
      }
    }

    const entryIds = extractedEntries.map((entry) => entry.id).sort(compareCodePoints);
    const measurement = captureMeasurement(
      extractedEntries,
      gateObservation,
      fixLoopRounds,
      routing,
      gateCommandFingerprints,
    );
    const extractedProjection = {
      entries: extractedEntries,
      gate_resolutions: gateResolutions,
      gate_snapshot_resolution: gateSnapshotResolution,
      review_observation: reviewObservation,
      gate_observation: gateObservation,
      gate_command_fingerprints: gateCommandFingerprints,
      unsupported_count: unsupportedCount,
      fix_loop_rounds: normalizedFixLoopRounds,
      routing,
      normalization_context: normalizationContext,
    };
    const captureBase = {
      schema_version: SCHEMA_VERSION,
      run_id: context.config.run_id,
      round: normalizedRound,
      generated_at: timestamp,
      phase: phaseId,
      source_provenance: sourceRecords,
      entry_ids: entryIds,
      unsupported_count: unsupportedCount,
      measurement,
      workflow_id: normalizedWorkflowId,
      route_key: normalizedRouteKey,
      routed_to: normalizedRoutedTo,
      expected_target: routing.expected_target,
      transition_occurrence_id: normalizedTransitionOccurrenceId,
      extracted_projection: extractedProjection,
      extracted_projection_sha256: sha256(canonicalJson(extractedProjection)),
    };
    const capturePayload = {
      ...captureBase,
      id: sha256(canonicalJson(captureBase)),
    };
    const appended = appendCommittedRecord(context, "capture", capturePayload, { strict: true });
    return {
      ok: true,
      captured: appended,
      entry_ids: entryIds,
      unsupported_count: unsupportedCount,
      gate_attempt: gateObservation?.attempt ?? null,
      transition_occurrence_id: normalizedTransitionOccurrenceId,
    };
  } catch (error) {
    return failOpen(error);
  }
}

export function observeExecutionStateInjection({
  runDir,
  runId,
  mode,
  experimentEnabled = false,
  phase,
  projectRoot,
  round,
  generatedAt = new Date().toISOString(),
  promptBytes = null,
  promptChars = 0,
}) {
  try {
    if (experimentEnabled !== true) return { ok: true, enabled: false, observed: false, block: "" };
    const context = loadContext(runDir, runId, mode);
    const timestamp = normalizeTimestamp(generatedAt);
    const actionProjectRoot = fs.realpathSync(path.resolve(projectRoot));
    const selected = selectPromptBlock(
      context,
      phase,
      projectRoot,
      normalizeRound(round),
      actionProjectRoot,
    );
    const eventBase = {
      schema_version: SCHEMA_VERSION,
      run_id: context.config.run_id,
      round: normalizeRound(round),
      generated_at: timestamp,
      phase: String(phase?.id ?? ""),
      mode: context.config.mode,
      exposure_policy_sha256: context.config.exposure_policy_sha256,
      per_exposure_token_cap: context.config.per_exposure_token_cap,
      injected: Boolean(selected.block),
      reason: selected.reason,
      entry_ids: selected.entry_ids,
      evidence_entry_ids: selected.evidence_entry_ids,
      evidence_entries: selected.evidence_entries,
      normalization_context: {
        extractor_version: EXTRACTOR_VERSION,
        project_root: canonicalProjectRoot(projectRoot),
        action_project_root_realpath: actionProjectRoot,
      },
      line_count: selected.block ? selected.block.split("\n").length : 0,
      byte_count: utf8ByteLength(selected.block),
      block_sha256: sha256(selected.block),
      block: selected.block,
      prompt_bytes: Math.max(0, integerOrZero(promptBytes ?? promptChars)),
      stale_candidate_count: selected.stale_count,
      unsupported_candidate_count: selected.unsupported_count,
    };
    const event = {
      ...eventBase,
      id: sha256(canonicalJson(eventBase)),
    };
    const appended = appendCommittedRecord(context, "injection", event, { strict: true });
    return { ok: true, observed: appended, block: selected.block };
  } catch (error) {
    return failOpen(error, { block: "" });
  }
}

export function executionStatePromptBlock({
  runDir,
  runId,
  mode,
  experimentEnabled = false,
  phase,
  projectRoot,
  round,
}) {
  try {
    if (experimentEnabled !== true) return "";
    const context = loadContext(runDir, runId, mode);
    return selectPromptBlock(context, phase, projectRoot, normalizeRound(round)).block;
  } catch (_error) {
    return "";
  }
}

export function recordExecutionStateUsage({
  runDir,
  runId,
  mode,
  experimentEnabled = false,
  eventId,
  generatedAt,
  scope,
  phaseId,
  round,
  inputTokens,
  outputTokens,
  additionalTokens,
  latencyMs,
  estimatedCostUsd,
  modelId,
  receiptPath = null,
  receiptSha256 = null,
}) {
  try {
    if (experimentEnabled !== true) return { ok: true, enabled: false, recorded: false };
    const context = loadContext(runDir, runId, mode);
    const receipt = receiptPath === null || receiptPath === undefined
      ? null
      : archiveUsageReceipt(
        context.runDir,
        receiptPath,
        receiptSha256,
        context.config,
      );
    const receiptFields = receipt
      ? usageFieldsFromReceipt(receipt.payload, context.config)
      : null;
    const rawEventId = requireString(receiptFields?.event_id ?? eventId, "event_id");
    if (!/^[A-Za-z0-9._:-]{1,128}$/.test(rawEventId)) {
      throw new Error("invalid execution ledger event_id");
    }
    const timestamp = normalizeTimestamp(receiptFields?.generated_at ?? generatedAt);
    const usageScope = requireString(receiptFields?.scope ?? scope, "scope");
    if (!new Set(["phase", "run-total"]).has(usageScope)) {
      throw new Error(`invalid execution ledger usage scope: ${usageScope}`);
    }
    let usagePhaseId = receiptFields?.phase_id ?? phaseId ?? null;
    let usageRound = receiptFields?.round ?? round ?? null;
    if (usageScope === "phase") {
      usagePhaseId = requireString(usagePhaseId, "phase_id");
      if (!Number.isInteger(usageRound) || usageRound < 0) {
        throw new Error("invalid execution ledger usage round");
      }
    } else if (usagePhaseId !== null || usageRound !== null) {
      throw new Error("run-total usage must have null phase_id and round");
    }
    const usageModelId = requireString(receiptFields?.model_id ?? modelId, "model_id");
    if (usageModelId !== context.config.model_id) {
      throw new Error("execution ledger usage model_id mismatch");
    }
    const input = requireNonnegativeInteger(receiptFields?.input_tokens ?? inputTokens, "input_tokens");
    const output = requireNonnegativeInteger(receiptFields?.output_tokens ?? outputTokens, "output_tokens");
    const additional = requireNonnegativeInteger(
      receiptFields?.additional_tokens ?? additionalTokens,
      "additional_tokens",
    );
    if (additional > input) {
      throw new Error("execution ledger additional_tokens exceeds input_tokens");
    }
    const latency = requireNonnegativeInteger(receiptFields?.latency_ms ?? latencyMs, "latency_ms");
    const cost = normalizeEstimatedCost(receiptFields?.estimated_cost_usd ?? estimatedCostUsd);
    if (receiptFields) {
      assertManualUsageMatchesReceipt({
        eventId,
        generatedAt,
        scope,
        phaseId,
        round,
        modelId,
        inputTokens,
        outputTokens,
        additionalTokens,
        latencyMs,
        estimatedCostUsd,
      }, receiptFields);
    } else if (receiptSha256 !== null && receiptSha256 !== undefined) {
      throw new Error("execution ledger receipt_sha256 requires receipt_path");
    }
    const event = {
      schema_version: SCHEMA_VERSION,
      id: sha256(canonicalJson({ run_id: runId, event_id: rawEventId })),
      event_id: rawEventId,
      run_id: runId,
      generated_at: timestamp,
      scope: usageScope,
      phase_id: usagePhaseId,
      round: usageRound,
      model_id: usageModelId,
      additional_token_scope: "condition-total",
      input_tokens: input,
      output_tokens: output,
      additional_tokens: additional,
      latency_ms: latency,
      estimated_cost: cost,
      evidence_status: receipt ? VERIFIED_USAGE_EVIDENCE : SUMMARY_USAGE_EVIDENCE,
      control_evidence_status: receipt ? VERIFIED_CONTROL_EVIDENCE : SUMMARY_USAGE_EVIDENCE,
      control_receipt_id: receiptFields?.control_receipt_id ?? null,
      runner_control_snapshot_sha256: receiptFields?.runner_control_snapshot_sha256 ?? null,
      exposure_policy_sha256: receiptFields?.exposure_policy_sha256 ?? null,
      per_exposure_token_cap: receiptFields?.per_exposure_token_cap ?? null,
      tool_permissions_sha256: receiptFields?.tool_permissions_sha256 ?? null,
      system_prompt_sha256: receiptFields?.system_prompt_sha256 ?? null,
      caps_sha256: receiptFields?.caps_sha256 ?? null,
      provider_retry_policy_sha256: receiptFields?.provider_retry_policy_sha256 ?? null,
      provider_max_retries: receiptFields?.provider_max_retries ?? null,
      pricing_snapshot_sha256: receiptFields?.pricing_snapshot_sha256 ?? null,
      provider_attestation_key_id: receiptFields?.provider_attestation_key_id ?? null,
      provider_attestation_public_key_sha256: receiptFields?.provider_attestation_public_key_sha256 ?? null,
      execution_controls_sha256: receiptFields?.execution_controls_sha256 ?? null,
      usage_provenance: receipt?.provenance ?? null,
    };
    const existingUsage = readJsonl(context.paths.usage);
    if (
      usageScope === "run-total"
      && existingUsage.some((item) => item.scope === "run-total" && item.id !== event.id)
    ) {
      throw new Error("execution ledger already has a distinct run-total usage event");
    }
    if (usageScope === "run-total") {
      canonicalTerminalState(context.runDir, context.config, context.workflow);
    }
    const recorded = appendCommittedRecord(context, "usage", event, {
      strict: true,
      completedAt: usageScope === "run-total" ? timestamp : null,
    });
    return {
      ok: true,
      recorded,
      phase_id: usagePhaseId,
      model_id: usageModelId,
      evidence_status: event.evidence_status,
      control_evidence_status: event.control_evidence_status,
    };
  } catch (error) {
    return failOpen(error);
  }
}

function loadContext(runDir, runId, mode) {
  const paths = ledgerPaths(runDir);
  recoverPendingTransaction(path.resolve(runDir), paths);
  const normalizedMode = resolveLedgerMode(mode);
  const config = readJson(paths.config);
  assertRunDocument(config, runId, normalizedMode, "config.json");
  assertRunnerControlSnapshot(config);
  const workflow = readJson(paths.workflow);
  assertWorkflowSnapshot(config, workflow);
  let ledger = null;
  if (normalizedMode !== "artifacts-only") {
    ledger = readJson(paths.ledger);
    assertLedger(ledger, runId);
  }
  const validation = validateCommittedRecords({
    runDir: path.resolve(runDir),
    paths,
    config,
    workflow,
    requireCompletion: false,
  });
  return {
    runDir: path.resolve(runDir),
    paths,
    config,
    workflow,
    ledger,
    completion: validation.completion,
  };
}

function ledgerPaths(runDir) {
  const root = path.join(fs.realpathSync(path.resolve(runDir)), "artifacts", "execution-ledger");
  return {
    root,
    config: path.join(root, "config.json"),
    workflow: path.join(root, "workflow.json"),
    captures: path.join(root, "captures.jsonl"),
    injections: path.join(root, "injections.jsonl"),
    events: path.join(root, "events.jsonl"),
    metrics: path.join(root, "metrics.json"),
    ledger: path.join(root, "ledger.json"),
    sources: path.join(root, "sources"),
    usage: path.join(root, "usage.jsonl"),
    completion: path.join(root, "completion.json"),
    journal: path.join(root, "transaction.json"),
    lock: path.join(root, "transaction.lock"),
  };
}

function assertWorkflowSnapshot(config, workflow) {
  if (
    !isPlainObject(workflow)
    || workflow.schema_version !== SCHEMA_VERSION
    || workflow.workflow_id !== config.workflow_id
    || !Array.isArray(workflow.phases)
  ) {
    throw new Error("execution ledger workflow snapshot identity mismatch");
  }
  const canonical = canonicalWorkflowSnapshot(workflow.workflow_id, workflow.phases);
  if (canonicalJson(canonical) !== canonicalJson(workflow)) {
    throw new Error("invalid execution ledger workflow snapshot");
  }
  const digest = sha256(canonicalJson(workflow));
  if (
    config.workflow_snapshot_sha256 !== digest
    || config.workflow_sha256 !== digest
  ) {
    throw new Error("execution ledger workflow snapshot hash mismatch");
  }
}

function committedRecordPath(paths, recordType) {
  if (recordType === "capture") return paths.captures;
  if (recordType === "injection") return paths.injections;
  if (recordType === "usage") return paths.usage;
  throw new Error(`invalid execution ledger record type: ${recordType}`);
}

function withoutContentCommitment(record, { includeChain = true } = {}) {
  if (!isPlainObject(record)) throw new Error("invalid execution ledger committed record");
  const copy = { ...record };
  delete copy.content_sha256;
  if (!includeChain) {
    delete copy.sequence;
    delete copy.previous_content_sha256;
  }
  return copy;
}

function appendCommittedRecord(context, recordType, payload, { strict, completedAt = null }) {
  if (!COMMITTED_RECORD_TYPES.includes(recordType)) {
    throw new Error(`invalid execution ledger record type: ${recordType}`);
  }
  const lock = acquireTransactionLock(context.paths);
  try {
    recoverPendingTransactionLocked(context.runDir, context.paths);
    const target = committedRecordPath(context.paths, recordType);
    const existing = readJsonl(target).find((record) => record.id === payload.id);
    if (existing) {
      if (
        strict
        && canonicalJson(withoutContentCommitment(existing, { includeChain: false })) !== canonicalJson(payload)
      ) {
        throw new Error(`execution ledger event id collision: ${payload.id}`);
      }
      return false;
    }
    if (sidecarPathExists(context.paths.completion)) {
      throw new Error("execution ledger is finalized");
    }
    validateProspectiveRecord(context, recordType, payload);
    const events = readJsonl(context.paths.events);
    const sequence = events.length + 1;
    const previousContentSha256 = events.length > 0
      ? events[events.length - 1].record_content_sha256
      : null;
    const committedBase = {
      ...payload,
      sequence,
      previous_content_sha256: previousContentSha256,
    };
    const committed = {
      ...committedBase,
      content_sha256: sha256(canonicalJson(committedBase)),
    };
    const indexBase = {
      schema_version: SCHEMA_VERSION,
      run_id: context.config.run_id,
      sequence,
      record_type: recordType,
      record_id: committed.id,
      record_content_sha256: committed.content_sha256,
      previous_content_sha256: previousContentSha256,
    };
    const index = {
      ...indexBase,
      id: sha256(canonicalJson(indexBase)),
    };
    const journalBase = {
      schema_version: SCHEMA_VERSION,
      run_id: context.config.run_id,
      record_type: recordType,
      record: committed,
      event: index,
      completed_at: completedAt,
    };
    const journal = {
      ...journalBase,
      content_sha256: sha256(canonicalJson(journalBase)),
    };
    writeImmutableJson(context.paths.journal, journal);
    applyPendingTransactionLocked(context.runDir, context.paths, journal, true);
    unlinkSafeSidecarFile(context.paths.journal);
    return true;
  } finally {
    releaseTransactionLock(context.paths, lock);
  }
}

function validateProspectiveRecord(context, recordType, payload) {
  const validation = validateCommittedRecords({
    runDir: context.runDir,
    paths: context.paths,
    config: context.config,
    workflow: context.workflow,
    requireCompletion: false,
  });
  if (recordType === "capture") {
    if (validation.recordsByType.capture.some(
      (record) => record.transition_occurrence_id === payload.transition_occurrence_id,
    )) {
      throw new Error("execution ledger transition occurrence id collision");
    }
    const gateAttempt = payload.phase === "gates"
      ? validation.recordsByType.capture.filter((record) => record.phase === "gates").length + 1
      : null;
    validateCaptureProjection(
      context.runDir,
      context.config,
      context.workflow,
      payload,
      gateAttempt,
    );
  } else if (recordType === "injection") {
    validateInjectionEvidence(
      payload,
      context.runDir,
      context.config,
      context.workflow,
      validation.replayedLedger,
    );
  } else {
    validateUsageEvidence(payload, context.runDir, context.config);
    if (
      payload.scope === "run-total"
      && validation.recordsByType.usage.some((record) => record.scope === "run-total")
    ) {
      throw new Error("execution ledger already has a distinct run-total usage event");
    }
  }
}

function recoverPendingTransaction(runDir, paths = ledgerPaths(runDir)) {
  if (!sidecarPathExists(paths.journal)) return;
  const lock = acquireTransactionLock(paths);
  try {
    recoverPendingTransactionLocked(path.resolve(runDir), paths);
  } finally {
    releaseTransactionLock(paths, lock);
  }
}

function recoverPendingTransactionLocked(runDir, paths) {
  if (!sidecarPathExists(paths.journal)) return;
  const journal = readJson(paths.journal);
  applyPendingTransactionLocked(runDir, paths, journal, false);
  unlinkSafeSidecarFile(paths.journal);
}

function applyPendingTransactionLocked(runDir, paths, journal, allowFaultInjection) {
  const config = readJson(paths.config);
  const workflow = readJson(paths.workflow);
  assertWorkflowSnapshot(config, workflow);
  validateTransactionJournal(journal, config);
  const target = committedRecordPath(paths, journal.record_type);
  ensureTransactionRecord(target, journal.record);
  maybeInjectTransactionFault("target", allowFaultInjection);
  ensureTransactionRecord(paths.events, journal.event);
  maybeInjectTransactionFault("event", allowFaultInjection);
  const rawValidation = validateCommittedRecords({
    runDir: path.resolve(runDir),
    paths,
    config,
    workflow,
    requireCompletion: false,
    allowPendingCompletion: true,
    skipDerived: true,
  });
  const repairedContext = {
    runDir: path.resolve(runDir),
    paths,
    config,
    workflow,
    ledger: rawValidation.replayedLedger,
    completion: null,
  };
  if (config.mode !== "artifacts-only") {
    writeCanonicalJson(paths.ledger, rawValidation.replayedLedger);
  }
  maybeInjectTransactionFault("ledger", allowFaultInjection);
  recomputeMetrics(repairedContext);
  maybeInjectTransactionFault("metrics", allowFaultInjection);
  if (journal.completed_at !== null) {
    writeCompletionForTransaction(repairedContext, journal.completed_at, rawValidation);
    maybeInjectTransactionFault("completion", allowFaultInjection);
  }
  validateCommittedRecords({
    runDir: path.resolve(runDir),
    paths,
    config,
    workflow,
    requireCompletion: journal.completed_at !== null,
  });
}

function validateTransactionJournal(journal, config) {
  if (
    !isPlainObject(journal)
    || journal.schema_version !== SCHEMA_VERSION
    || journal.run_id !== config.run_id
    || !COMMITTED_RECORD_TYPES.includes(journal.record_type)
    || !isPlainObject(journal.record)
    || !isPlainObject(journal.event)
    || !COMMITMENT_SHA256.test(journal.content_sha256)
    || sha256(canonicalJson(withoutContentCommitment(journal))) !== journal.content_sha256
    || journal.record.run_id !== config.run_id
    || journal.event.run_id !== config.run_id
    || journal.event.record_type !== journal.record_type
    || journal.event.record_id !== journal.record.id
    || journal.event.record_content_sha256 !== journal.record.content_sha256
    || journal.event.sequence !== journal.record.sequence
    || journal.event.previous_content_sha256 !== journal.record.previous_content_sha256
  ) {
    throw new Error("invalid execution ledger transaction journal");
  }
  if (
    !COMMITMENT_SHA256.test(journal.record.content_sha256)
    || sha256(canonicalJson(withoutContentCommitment(journal.record))) !== journal.record.content_sha256
  ) {
    throw new Error("invalid execution ledger transaction record commitment");
  }
  const semanticPayload = withoutContentCommitment(journal.record, { includeChain: false });
  delete semanticPayload.id;
  const expectedRecordId = journal.record_type === "usage"
    ? sha256(canonicalJson({ run_id: journal.record.run_id, event_id: journal.record.event_id }))
    : sha256(canonicalJson(semanticPayload));
  if (journal.record.id !== expectedRecordId) {
    throw new Error("invalid execution ledger transaction record id");
  }
  const indexBase = { ...journal.event };
  delete indexBase.id;
  if (journal.event.id !== sha256(canonicalJson(indexBase))) {
    throw new Error("invalid execution ledger transaction event commitment");
  }
  if (journal.completed_at !== null) {
    if (journal.record_type !== "usage" || journal.record.scope !== "run-total") {
      throw new Error("invalid execution ledger transaction completion");
    }
    if (normalizeTimestamp(journal.completed_at) !== journal.completed_at) {
      throw new Error("invalid execution ledger transaction timestamp");
    }
  }
}

function ensureTransactionRecord(pathName, expected) {
  const records = readJsonl(pathName);
  const byId = records.find((record) => record.id === expected.id);
  if (byId) {
    if (canonicalJson(byId) !== canonicalJson(expected)) {
      throw new Error("execution ledger transaction record mismatch");
    }
    return;
  }
  if (records.some((record) => record.sequence === expected.sequence)) {
    throw new Error("execution ledger transaction sequence collision");
  }
  appendJsonlRecord(pathName, expected);
}

function writeCompletionForTransaction(context, completedAt, validation) {
  const completionBase = {
    schema_version: SCHEMA_VERSION,
    run_id: context.config.run_id,
    mode: context.config.mode,
    completed_at: completedAt,
    event_count: validation.eventCount,
    event_head_sha256: validation.eventHeadSha256,
    terminal_state: canonicalTerminalState(context.runDir, context.config, context.workflow),
    files: finalizedFileCommitments(context.paths, context.config.mode),
  };
  writeImmutableJson(context.paths.completion, {
    ...completionBase,
    content_sha256: sha256(canonicalJson(completionBase)),
  });
}

function maybeInjectTransactionFault(point, enabled) {
  if (enabled && process.env.AGENT_FLOW_LEDGER_FAULT_AFTER === point) {
    throw new Error(`injected execution ledger transaction fault after ${point}`);
  }
}

function captureSourceIdentities(runDir, artifactPath, phaseId, routeKey, sourceEligible) {
  if (!sourceEligible) return [];
  if (!artifactPath || !fs.existsSync(artifactPath)) return [];
  const resolvedRun = fs.realpathSync(path.resolve(runDir));
  const resolvedArtifact = fs.realpathSync(path.resolve(artifactPath));
  const originalPath = safeRelativePath(resolvedRun, resolvedArtifact, "source artifact");
  const supportedReview = phaseId !== "gates"
    && /^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(phaseId)
    && ["approve", "request-changes", "blocked"].includes(routeKey);
  return [{
    selector: phaseId === "gates" ? "/" : (supportedReview ? "/review-artifact" : "/unsupported-review-summary"),
    original_path: originalPath,
    sha256: sha256(fs.readFileSync(resolvedArtifact)),
  }];
}

function recordedCaptureSourceIdentities(capture) {
  if (!Array.isArray(capture?.source_provenance)) return [];
  return capture.source_provenance
    .filter((source) => (
      source?.selector === "/review-artifact"
      || source?.selector === "/unsupported-review-summary"
      || (capture.phase === "gates" && source?.selector === "/")
    ))
    .filter((source) => typeof source.original_path === "string" && typeof source.sha256 === "string")
    .map((source) => ({
      selector: source.selector,
      original_path: source.original_path,
      sha256: source.sha256,
    }))
    .sort((left, right) => compareCodePoints(canonicalJson(left), canonicalJson(right)));
}

function findEquivalentCapture(captures, observation) {
  const sourceIdentities = observation.sourceIdentities ?? captureSourceIdentities(
    observation.runDir,
    observation.artifactPath,
    observation.phaseId,
    observation.routeKey,
    observation.sourceEligible,
  );
  return captures.find((capture) => (
    capture.transition_occurrence_id === observation.transitionOccurrenceId
    && capture.phase === observation.phaseId
    && capture.round === observation.round
    && capture.workflow_id === observation.workflowId
    && capture.route_key === observation.routeKey
    && capture.routed_to === observation.routedTo
    && capture.measurement?.fix_loop_rounds === observation.fixLoopRounds
    && canonicalJson(capture.extracted_projection?.normalization_context)
      === canonicalJson(observation.normalizationContext)
    && canonicalJson(recordedCaptureSourceIdentities(capture)) === canonicalJson(sourceIdentities)
  ));
}

function captureReplayResult(capture) {
  return {
    ok: true,
    captured: false,
    entry_ids: capture.entry_ids,
    unsupported_count: capture.unsupported_count,
    gate_attempt: capture.measurement?.gate_attempt ?? null,
    transition_occurrence_id: capture.transition_occurrence_id,
  };
}

function completedCaptureReplay(context, observation) {
  const existing = findEquivalentCapture(readJsonl(context.paths.captures), {
    runDir: context.runDir,
    artifactPath: observation.artifactPath,
    phaseId: observation.phaseId,
    round: observation.round,
    workflowId: observation.workflowId,
    routeKey: observation.routeKey,
    routedTo: observation.routedTo,
    fixLoopRounds: Math.max(0, integerOrZero(observation.fixLoopRounds)),
    sourceEligible: observation.sourceEligible,
    sourceIdentities: observation.sourceIdentities,
    transitionOccurrenceId: observation.transitionOccurrenceId,
    normalizationContext: {
      extractor_version: EXTRACTOR_VERSION,
      project_root: canonicalProjectRoot(observation.projectRoot),
    },
  });
  if (!existing) throw new Error("execution ledger is finalized");
  return captureReplayResult(existing);
}

function normalizeTransitionOccurrenceId(value, legacyIdentity) {
  if (value === null || value === undefined || value === "") {
    return sha256(canonicalJson({
      schema_version: SCHEMA_VERSION,
      ...legacyIdentity,
    }));
  }
  if (typeof value !== "string" || !COMMITMENT_SHA256.test(value)) {
    throw new Error("invalid execution ledger transition occurrence id");
  }
  return value;
}

function validateCommittedRecords({
  runDir,
  paths,
  config,
  workflow,
  requireCompletion,
  allowPendingCompletion = false,
  skipDerived = false,
}) {
  const recordsBySequence = new Map();
  const seenRecordIds = new Set();
  const seenTransitionOccurrenceIds = new Set();
  const recordsByType = {};
  let gateCaptureCount = 0;
  for (const recordType of COMMITTED_RECORD_TYPES) {
    const records = readJsonl(committedRecordPath(paths, recordType));
    recordsByType[recordType] = records;
    let previousSequence = 0;
    for (const record of records) {
      if (
        record.schema_version !== SCHEMA_VERSION
        || record.run_id !== config.run_id
        || !Number.isInteger(record.sequence)
        || record.sequence < 1
        || record.sequence <= previousSequence
        || typeof record.id !== "string"
        || !COMMITMENT_SHA256.test(record.content_sha256)
        || (
          record.previous_content_sha256 !== null
          && !COMMITMENT_SHA256.test(record.previous_content_sha256)
        )
      ) {
        throw new Error(`invalid execution ledger ${recordType} commitment`);
      }
      if (sha256(canonicalJson(withoutContentCommitment(record))) !== record.content_sha256) {
        throw new Error(`execution ledger ${recordType} content commitment mismatch`);
      }
      const semanticPayload = withoutContentCommitment(record, { includeChain: false });
      delete semanticPayload.id;
      const expectedRecordId = recordType === "usage"
        ? sha256(canonicalJson({ run_id: record.run_id, event_id: record.event_id }))
        : sha256(canonicalJson(semanticPayload));
      if (record.id !== expectedRecordId) {
        throw new Error(`execution ledger ${recordType} id commitment mismatch`);
      }
      if (recordType === "usage" && record.additional_token_scope !== "condition-total") {
        throw new Error("execution ledger usage additional token scope mismatch");
      }
      if (recordType === "usage") {
        validateUsageEvidence(record, runDir, config);
      }
      if (recordsBySequence.has(record.sequence)) {
        throw new Error("duplicate execution ledger event sequence");
      }
      const recordKey = `${recordType}\0${record.id}`;
      if (seenRecordIds.has(recordKey)) {
        throw new Error("duplicate execution ledger committed record id");
      }
      seenRecordIds.add(recordKey);
      previousSequence = record.sequence;
      recordsBySequence.set(record.sequence, { recordType, record });
      if (recordType === "capture") {
        if (
          typeof record.transition_occurrence_id !== "string"
          || !COMMITMENT_SHA256.test(record.transition_occurrence_id)
          || seenTransitionOccurrenceIds.has(record.transition_occurrence_id)
        ) {
          throw new Error("duplicate or invalid execution ledger transition occurrence id");
        }
        seenTransitionOccurrenceIds.add(record.transition_occurrence_id);
        if (record.phase === "gates") gateCaptureCount += 1;
        validateCaptureProjection(
          runDir,
          config,
          workflow,
          record,
          record.phase === "gates" ? gateCaptureCount : null,
        );
      }
    }
  }

  const events = readJsonl(paths.events);
  if (events.length !== recordsBySequence.size) {
    throw new Error("execution ledger event index count mismatch");
  }
  let previousContentSha256 = null;
  let ledgerAtEvent = config.mode === "artifacts-only" ? null : emptyLedger(config.run_id);
  for (let index = 0; index < events.length; index += 1) {
    const sequence = index + 1;
    const event = events[index];
    const located = recordsBySequence.get(sequence);
    if (!located) throw new Error("execution ledger event sequence gap");
    const record = located.record;
    const expectedIndexBase = {
      schema_version: SCHEMA_VERSION,
      run_id: config.run_id,
      sequence,
      record_type: located.recordType,
      record_id: record.id,
      record_content_sha256: record.content_sha256,
      previous_content_sha256: previousContentSha256,
    };
    if (
      canonicalJson(event) !== canonicalJson({
        ...expectedIndexBase,
        id: sha256(canonicalJson(expectedIndexBase)),
      })
      || record.previous_content_sha256 !== previousContentSha256
    ) {
      throw new Error("execution ledger event chain mismatch");
    }
    previousContentSha256 = record.content_sha256;
    if (located.recordType === "capture" && ledgerAtEvent) {
      const projection = record.extracted_projection;
      ledgerAtEvent = mergeLedgerEntries(ledgerAtEvent, projection.entries, {
        gateResolutions: projection.gate_resolutions,
        gateSnapshotResolution: projection.gate_snapshot_resolution,
        gateCommandFingerprints: projection.gate_command_fingerprints,
        reviewObservation: projection.review_observation,
      });
    } else if (located.recordType === "injection") {
      validateInjectionEvidence(record, runDir, config, workflow, ledgerAtEvent);
    }
  }

  const usage = readJsonl(paths.usage);
  const runTotals = usage.filter((record) => record.scope === "run-total");
  if (runTotals.length > 1) throw new Error("execution ledger has multiple run-total usage events");
  if (skipDerived) {
    return {
      completion: null,
      eventCount: events.length,
      eventHeadSha256: previousContentSha256,
      recordsByType,
      metrics: null,
      replayedLedger: ledgerAtEvent,
    };
  }
  const completionPresent = sidecarPathExists(paths.completion);
  if (runTotals.length === 1 && !completionPresent && !allowPendingCompletion) {
    throw new Error("execution ledger run-total is not finalized");
  }
  if (completionPresent && runTotals.length !== 1) {
    throw new Error("execution ledger completion is missing run-total usage");
  }
  let completion = null;
  if (completionPresent) {
    completion = readJson(paths.completion);
    validateCompletion(paths, config, completion, events, runTotals[0]);
  } else if (requireCompletion) {
    throw new Error("execution ledger completion is required");
  }
  let replayedLedger = null;
  if (config.mode !== "artifacts-only") {
    replayedLedger = ledgerAtEvent;
    if (canonicalJson(replayedLedger) !== canonicalJson(readJson(paths.ledger))) {
      throw new Error("execution ledger semantic replay mismatch");
    }
  }
  const storedMetrics = readJson(paths.metrics);
  assertRunDocument(storedMetrics, config.run_id, config.mode, "metrics.json");
  const recomputedMetrics = recomputeMetrics({
    runDir,
    paths,
    config,
    workflow,
    ledger: replayedLedger,
  }, { write: false });
  if (canonicalJson(storedMetrics) !== canonicalJson(recomputedMetrics)) {
    const mismatched = Object.keys({ ...storedMetrics, ...recomputedMetrics })
      .filter((key) => canonicalJson(storedMetrics[key]) !== canonicalJson(recomputedMetrics[key]))
      .sort(compareCodePoints);
    throw new Error(`execution ledger metrics replay mismatch: ${mismatched.join(",")}`);
  }
  return {
    completion,
    eventCount: events.length,
    eventHeadSha256: previousContentSha256,
    recordsByType,
    metrics: recomputedMetrics,
    replayedLedger,
  };
}

function validateInjectionEvidence(injection, runDir, config, workflow, ledgerAtEvent) {
  const evidenceEntries = injection.evidence_entries;
  const evidenceEntryIds = injection.evidence_entry_ids;
  const normalizationContext = injection.normalization_context;
  if (
    !Array.isArray(evidenceEntries)
    || !Array.isArray(evidenceEntryIds)
    || canonicalJson(evidenceEntries.map((entry) => entry?.id)) !== canonicalJson(evidenceEntryIds)
    || !isPlainObject(normalizationContext)
    || normalizationContext.extractor_version !== EXTRACTOR_VERSION
    || typeof normalizationContext.project_root !== "string"
    || canonicalProjectRoot(normalizationContext.project_root) !== normalizationContext.project_root
    || typeof normalizationContext.action_project_root_realpath !== "string"
    || !path.isAbsolute(normalizationContext.action_project_root_realpath)
    || canonicalProjectRoot(normalizationContext.action_project_root_realpath)
      !== normalizationContext.action_project_root_realpath
    || injection.block_sha256 !== sha256(injection.block)
    || injection.byte_count !== utf8ByteLength(injection.block ?? "")
    || injection.exposure_policy_sha256 !== config.exposure_policy_sha256
    || injection.per_exposure_token_cap !== config.per_exposure_token_cap
    || tokenProxy(injection.byte_count) > config.per_exposure_token_cap
    || !Number.isInteger(injection.prompt_bytes)
    || injection.prompt_bytes < 0
    || injection.line_count !== (injection.block ? injection.block.split("\n").length : 0)
  ) {
    throw new Error("execution ledger injection evidence mismatch");
  }
  const phase = workflow.phases.find((candidate) => candidate.id === injection.phase);
  if (!phase) throw new Error("execution ledger injection workflow mismatch");
  if (phase.multi_review && (injection.injected || injection.block || evidenceEntries.length > 0)) {
    throw new Error("execution ledger multi-review injection violation");
  }
  const selected = selectPromptBlock(
    { runDir, config, workflow, ledger: ledgerAtEvent },
    phase,
    normalizationContext.project_root,
    injection.round,
    normalizationContext.action_project_root_realpath,
  );
  const expectedExposure = {
    block: selected.block,
    reason: selected.reason,
    entry_ids: selected.entry_ids,
    evidence_entry_ids: selected.evidence_entry_ids,
    evidence_entries: selected.evidence_entries,
    injected: Boolean(selected.block),
    line_count: selected.block ? selected.block.split("\n").length : 0,
    byte_count: utf8ByteLength(selected.block),
    block_sha256: sha256(selected.block),
    stale_candidate_count: selected.stale_count,
    unsupported_candidate_count: selected.unsupported_count,
  };
  const actualExposure = Object.fromEntries(
    Object.keys(expectedExposure).map((key) => [key, injection[key]]),
  );
  if (canonicalJson(actualExposure) !== canonicalJson(expectedExposure)) {
    throw new Error("execution ledger injection selection replay mismatch");
  }
  if (!ledgerAtEvent) {
    if (evidenceEntries.length > 0 || injection.injected || injection.block) {
      throw new Error("execution ledger artifacts-only injection violation");
    }
    return;
  }
  const available = new Map();
  for (const bucket of ["status", "knowledge", "procedural"]) {
    for (const entry of ledgerAtEvent.entries[bucket]) available.set(entry.id, entry);
  }
  for (const entry of evidenceEntries) {
    const expected = available.get(entry?.id);
    if (!expected || canonicalJson(expected) !== canonicalJson(entry)) {
      throw new Error("execution ledger injection semantic replay mismatch");
    }
  }
  const expectedPromptIds = injection.mode === "action-self-review" ? [] : evidenceEntryIds;
  if (canonicalJson(injection.entry_ids) !== canonicalJson(expectedPromptIds)) {
    throw new Error("execution ledger injection exposure mismatch");
  }
}

function validateCaptureProjection(runDir, config, workflow, capture, gateAttempt) {
  const projection = capture.extracted_projection;
  if (
    !isPlainObject(projection)
    || !Array.isArray(projection.entries)
    || !Array.isArray(projection.gate_resolutions)
    || !Array.isArray(projection.gate_command_fingerprints)
    || !isPlainObject(projection.routing)
    || !isPlainObject(projection.normalization_context)
    || typeof capture.transition_occurrence_id !== "string"
    || !COMMITMENT_SHA256.test(capture.transition_occurrence_id)
    || capture.extracted_projection_sha256 !== sha256(canonicalJson(projection))
  ) {
    throw new Error("execution ledger capture projection mismatch");
  }
  if (!Array.isArray(capture.source_provenance)) {
    throw new Error("invalid execution ledger capture source provenance");
  }
  const sources = capture.source_provenance.map((provenance) => {
    if (!isPlainObject(provenance)) {
      throw new Error("execution ledger capture archive commitment mismatch");
    }
    const source = readVerifiedArchive(runDir, config.run_id, provenance);
    if (!source) {
      throw new Error("execution ledger capture archive commitment mismatch");
    }
    return source;
  });
  const normalizationContext = projection.normalization_context;
  if (
    normalizationContext.extractor_version !== EXTRACTOR_VERSION
    || typeof normalizationContext.project_root !== "string"
    || canonicalProjectRoot(normalizationContext.project_root) !== normalizationContext.project_root
  ) {
    throw new Error("execution ledger normalization context mismatch");
  }
  const phase = workflow.phases.find((candidate) => candidate.id === capture.phase);
  if (!phase || capture.workflow_id !== config.workflow_id) {
    throw new Error("execution ledger capture workflow mismatch");
  }
  const routing = routingObservation(workflow, phase, capture.route_key, capture.routed_to);
  let expectedProjection;
  if (capture.phase === "gates") {
    if (sources.length !== 1) throw new Error("invalid execution ledger gate source count");
    const extracted = extractGateEntries(
      sources[0],
      normalizationContext.project_root,
      capture.generated_at,
      capture.round,
    );
    if (extracted.gate_observation) {
      extracted.gate_observation.attempt = gateAttempt;
      extracted.gate_observation.passed = canonicalGatePassed(
        extracted.gate_observation,
        routing,
        capture.routed_to,
      );
    }
    const snapshotResolution = extracted.gate_observation?.snapshot_complete === true
      ? { ...capture.source_provenance[0], selector: "/" }
      : null;
    expectedProjection = {
      entries: extracted.entries,
      gate_resolutions: extracted.resolutions,
      gate_snapshot_resolution: snapshotResolution,
      review_observation: null,
      gate_observation: extracted.gate_observation,
      gate_command_fingerprints: extracted.command_fingerprints,
      unsupported_count: extracted.unsupported_count + routing.unsupported_count,
      fix_loop_rounds: projection.fix_loop_rounds,
      routing,
      normalization_context: normalizationContext,
    };
  } else if (!isLedgerSourcePhase(phase)) {
    if (sources.length !== 0) throw new Error("invalid execution ledger routing-only source count");
    expectedProjection = {
      entries: [],
      gate_resolutions: [],
      gate_snapshot_resolution: null,
      review_observation: null,
      gate_observation: null,
      gate_command_fingerprints: [],
      unsupported_count: routing.unsupported_count,
      fix_loop_rounds: projection.fix_loop_rounds,
      routing,
      normalization_context: normalizationContext,
    };
  } else {
    if (sources.length === 2) {
      const summary = sources[0].payload;
      if (!isPlainObject(summary)) throw new Error("invalid execution ledger review summary");
      const verdict = ["approve", "request-changes", "blocked"].includes(capture.route_key)
        ? capture.route_key
        : null;
      if (!verdict || capture.source_provenance[1]?.selector !== "/review-artifact") {
        throw new Error("invalid execution ledger review artifact binding");
      }
      const expectedSummary = {
        schema_version: REVIEW_SUMMARY_SCHEMA_VERSION,
        run_id: config.run_id,
        phase_id: capture.phase,
        round: capture.round,
        generated_at: capture.generated_at,
        artifact_path: capture.source_provenance[1].original_path,
        artifact_sha256: capture.source_provenance[1].sha256,
        artifact_archive_path: capture.source_provenance[1].archive_path,
        verdict,
        findings: verdict === "approve"
          ? []
          : parseReviewFindings(sources[1].bytes, normalizationContext.project_root),
      };
      if (canonicalJson(summary) !== canonicalJson(expectedSummary)) {
        throw new Error("execution ledger review summary replay mismatch");
      }
      const extracted = extractReviewEntries(
        sources[0],
        normalizationContext.project_root,
        capture.generated_at,
        capture.round,
      );
      const summaryComplete = summary.verdict === "approve"
        || (Array.isArray(summary.findings) && summary.findings.length > 0);
      const summaryUnsupported = summaryComplete ? 0 : 1;
      expectedProjection = {
        entries: extracted.entries,
        gate_resolutions: [],
        gate_snapshot_resolution: null,
        review_observation: {
          complete: summaryComplete,
          phase_id: capture.phase,
          finding_fingerprints: extracted.entries.map((entry) => entry.payload.finding_fingerprint),
          provenance: { ...capture.source_provenance[0], selector: "/review-result" },
        },
        gate_observation: null,
        gate_command_fingerprints: [],
        unsupported_count: extracted.unsupported_count + summaryUnsupported + routing.unsupported_count,
        fix_loop_rounds: projection.fix_loop_rounds,
        routing,
        normalization_context: normalizationContext,
      };
    } else if (
      sources.length === 0
      || (
        sources.length === 1
        && capture.source_provenance[0]?.selector === "/unsupported-review-summary"
      )
    ) {
      expectedProjection = {
        entries: [],
        gate_resolutions: [],
        gate_snapshot_resolution: null,
        review_observation: null,
        gate_observation: null,
        gate_command_fingerprints: [],
        unsupported_count: 1 + routing.unsupported_count,
        fix_loop_rounds: projection.fix_loop_rounds,
        routing,
        normalization_context: normalizationContext,
      };
    } else {
      throw new Error("invalid execution ledger review source count");
    }
  }
  if (canonicalJson(expectedProjection) !== canonicalJson(projection)) {
    throw new Error("execution ledger archive extraction projection mismatch");
  }
  const entryIds = projection.entries.map((entry) => entry?.id).sort(compareCodePoints);
  const expectedMeasurement = captureMeasurement(
    projection.entries,
    projection.gate_observation,
    projection.fix_loop_rounds,
    projection.routing,
    projection.gate_command_fingerprints,
  );
  if (
    canonicalJson(entryIds) !== canonicalJson(capture.entry_ids)
    || canonicalJson(expectedMeasurement) !== canonicalJson(capture.measurement)
    || capture.unsupported_count !== projection.unsupported_count
    || capture.expected_target !== routing.expected_target
  ) {
    throw new Error("execution ledger capture measurement projection mismatch");
  }
}

function finalizedFileCommitments(paths, mode) {
  const names = [
    "config",
    "workflow",
    "captures",
    "injections",
    "usage",
    "events",
    "metrics",
  ];
  if (mode !== "artifacts-only") names.push("ledger");
  return names
    .map((name) => {
      const bytes = readSafeSidecarBytes(paths[name]);
      return {
        path: path.basename(paths[name]),
        byte_length: bytes.length,
        sha256: sha256(bytes),
      };
    })
    .sort((left, right) => compareCodePoints(left.path, right.path));
}

function validateCompletion(paths, config, completion, events, runTotal) {
  if (
    completion.schema_version !== SCHEMA_VERSION
    || completion.run_id !== config.run_id
    || completion.mode !== config.mode
    || normalizeTimestamp(completion.completed_at) !== completion.completed_at
    || !COMMITMENT_SHA256.test(completion.content_sha256)
    || sha256(canonicalJson(withoutContentCommitment(completion))) !== completion.content_sha256
  ) {
    throw new Error("invalid execution ledger completion commitment");
  }
  const eventHeadSha256 = events.length > 0
    ? events[events.length - 1].record_content_sha256
    : null;
  if (
    completion.event_count !== events.length
    || completion.event_head_sha256 !== eventHeadSha256
    || events.at(-1)?.record_type !== "usage"
    || events.at(-1)?.record_id !== runTotal.id
  ) {
    throw new Error("execution ledger completion event head mismatch");
  }
  const expectedFiles = finalizedFileCommitments(paths, config.mode);
  if (canonicalJson(completion.files) !== canonicalJson(expectedFiles)) {
    throw new Error("execution ledger completion file commitment mismatch");
  }
  const runDir = path.dirname(path.dirname(paths.root));
  const terminalState = canonicalTerminalState(runDir, config, readJson(paths.workflow));
  if (canonicalJson(completion.terminal_state) !== canonicalJson(terminalState)) {
    throw new Error("execution ledger completion terminal state mismatch");
  }
}

function sidecarPathExists(pathName) {
  try {
    fs.lstatSync(pathName);
    return true;
  } catch (error) {
    if (error?.code === "ENOENT") return false;
    throw error;
  }
}

function canonicalTerminalState(runDir, config, workflow) {
  const resolvedRun = fs.realpathSync(path.resolve(runDir));
  const manifestPath = path.join(resolvedRun, "manifest.json");
  const metaPath = path.join(resolvedRun, "meta.json");
  const hasManifest = sidecarPathExists(manifestPath);
  const hasMeta = sidecarPathExists(metaPath);
  if (hasManifest === hasMeta) {
    throw new Error("execution ledger terminal state evidence is missing or ambiguous");
  }
  const evidencePath = hasManifest ? manifestPath : metaPath;
  const bytes = readDirectRunFile(evidencePath);
  const payload = JSON.parse(bytes.toString("utf8"));
  if (!isPlainObject(payload)) throw new Error("invalid execution ledger terminal state evidence");
  const phaseIndex = payload.phase_index;
  if (
    payload.run_id !== config.run_id
    || payload.workflow !== config.workflow_id
    || !Number.isInteger(phaseIndex)
    || phaseIndex < workflow.phases.length
  ) {
    throw new Error("execution ledger terminal state identity mismatch");
  }
  if (hasManifest) {
    if (payload.status !== "complete" || payload.phase !== "complete") {
      throw new Error("execution ledger Node run is not complete");
    }
    return {
      runtime: "node",
      path: "manifest.json",
      sha256: sha256(bytes),
      run_id: payload.run_id,
      workflow_id: payload.workflow,
      status: "complete",
      current_phase: "complete",
      phase_index: phaseIndex,
    };
  }
  if (payload.current_phase !== null || sidecarPathExists(path.join(resolvedRun, "active"))) {
    throw new Error("execution ledger Python run is not complete");
  }
  return {
    runtime: "python",
    path: "meta.json",
    sha256: sha256(bytes),
    run_id: payload.run_id,
    workflow_id: payload.workflow,
    status: "complete",
    current_phase: null,
    phase_index: phaseIndex,
  };
}

function readDirectRunFile(pathName) {
  const stat = fs.lstatSync(pathName);
  if (stat.isSymbolicLink() || !stat.isFile()) {
    throw new Error(`unsafe execution ledger terminal state file: ${pathName}`);
  }
  const flags = fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0);
  const descriptor = fs.openSync(pathName, flags);
  try {
    if (!fs.fstatSync(descriptor).isFile()) {
      throw new Error(`unsafe execution ledger terminal state file: ${pathName}`);
    }
    return fs.readFileSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
}

function emptyLedger(runId) {
  return {
    schema_version: SCHEMA_VERSION,
    run_id: runId,
    entries: { status: [], knowledge: [], procedural: [] },
  };
}

function emptyMetrics(runId, mode, config = null) {
  return {
    schema_version: SCHEMA_VERSION,
    run_id: runId,
    mode,
    gate_green_le_3: null,
    gate_green_attempt: null,
    repeated_command_fingerprint_count: 0,
    repeated_command_fingerprint_rate: 0,
    repeated_failure_fingerprint_count: 0,
    repeated_failure_fingerprint_rate: 0,
    repeated_diagnosis_fingerprint_count: 0,
    repeated_diagnosis_fingerprint_rate: 0,
    repeated_finding_fingerprint_count: 0,
    repeated_finding_fingerprint_rate: 0,
    fix_loop_rounds: 0,
    prompt_bytes: 0,
    injected_bytes: 0,
    prompt_token_proxy: 0,
    injected_token_proxy: 0,
    runner_control_snapshot_sha256: config?.runner_control_snapshot_sha256 ?? null,
    exposure_policy_sha256: config?.exposure_policy_sha256 ?? null,
    per_exposure_token_cap: config?.per_exposure_token_cap ?? 0,
    exposure_budget_compliant: config !== null,
    max_injected_token_proxy: 0,
    actual_usage_coverage: false,
    actual_control_coverage: false,
    actual_control_evidence_status: null,
    actual_input_tokens: null,
    actual_output_tokens: null,
    actual_total_tokens: null,
    actual_additional_tokens: null,
    actual_additional_token_coverage: false,
    actual_usage_budget_matched: false,
    actual_usage_evidence_status: null,
    summary_usage_coverage: false,
    summary_input_tokens: null,
    summary_output_tokens: null,
    summary_additional_tokens: null,
    summary_latency_ms: null,
    summary_estimated_cost: null,
    injected_event_count: 0,
    latency_proxy_ms: null,
    latency_ms: null,
    estimated_cost: null,
    stale_candidate_count: 0,
    unsupported_source_count: 0,
    malformed_source_count: 0,
    excluded_source_count: 0,
    stale_reminder_count: 0,
    stale_reminder_rate: 0,
    unsupported_reminder_count: 0,
    unsupported_reminder_rate: 0,
    stale_count: 0,
    unsupported_count: 0,
    canonical_routing_violations: 0,
    canonical_transition_count: 0,
    canonical_route_coverage_complete: false,
    canonical_route_coverage_gap_count: 1,
    routing_provenance: [],
    experiment_valid: false,
  };
}

function archiveSource(runDir, sourcePath, selector, generatedAt, round, runId) {
  const resolvedRun = fs.realpathSync(path.resolve(runDir));
  const resolvedSource = fs.realpathSync(path.resolve(sourcePath));
  const originalPath = safeRelativePath(resolvedRun, resolvedSource, "source artifact");
  const bytes = fs.readFileSync(resolvedSource);
  const digest = sha256(bytes);
  const extension = safeExtension(resolvedSource);
  const archive = path.join(resolvedRun, "artifacts", "execution-ledger", "sources", `${digest}${extension}`);
  preserveAtomicBytes(archive, bytes);
  return {
    bytes,
    payload: parseJsonBytes(bytes),
    provenance: {
      run_id: runId,
      round,
      generated_at: generatedAt,
      original_path: originalPath,
      archive_path: safeRelativePath(resolvedRun, archive, "archive artifact"),
      sha256: digest,
      selector,
      extractor_version: EXTRACTOR_VERSION,
    },
  };
}

function archiveUsageReceipt(runDir, sourcePath, expectedSha256, config) {
  const runId = config.run_id;
  if (typeof expectedSha256 !== "string" || !COMMITMENT_SHA256.test(expectedSha256)) {
    throw new Error("verified usage requires --receipt-sha256");
  }
  const resolvedRun = fs.realpathSync(path.resolve(runDir));
  const lexicalSource = path.resolve(sourcePath);
  const sourceStat = fs.lstatSync(lexicalSource);
  if (sourceStat.isSymbolicLink() || !sourceStat.isFile()) {
    throw new Error("provider usage receipt must be a regular non-symlink file");
  }
  const resolvedSource = fs.realpathSync(lexicalSource);
  const originalPath = safeRelativePath(resolvedRun, resolvedSource, "provider usage receipt");
  const flags = fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0);
  const descriptor = fs.openSync(lexicalSource, flags);
  let bytes;
  try {
    if (!fs.fstatSync(descriptor).isFile()) {
      throw new Error("provider usage receipt must be a regular file");
    }
    bytes = fs.readFileSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
  if (fs.realpathSync(lexicalSource) !== resolvedSource) {
    throw new Error("provider usage receipt changed during archival");
  }
  const digest = sha256(bytes);
  if (digest !== expectedSha256) {
    throw new Error("provider usage receipt sha256 mismatch");
  }
  const payload = parseJsonBytes(bytes);
  const fields = usageFieldsFromReceipt(payload, config);
  const archive = path.join(
    resolvedRun,
    "artifacts",
    "execution-ledger",
    "sources",
    `${digest}.json`,
  );
  preserveAtomicBytes(archive, bytes);
  return {
    payload,
    fields,
    provenance: {
      source_kind: USAGE_RECEIPT_KIND,
      run_id: runId,
      original_path: originalPath,
      archive_path: safeRelativePath(resolvedRun, archive, "provider usage receipt archive"),
      sha256: digest,
      expected_sha256: expectedSha256,
      provider: payload.provider,
      request_id: payload.request_id,
      receipt_id: payload.receipt_id,
      attestation_algorithm: fields.attestation_algorithm,
      attestation_key_id: fields.provider_attestation_key_id,
      attestation_public_key_sha256: fields.provider_attestation_public_key_sha256,
      signed_payload_sha256: fields.signed_payload_sha256,
      control_receipt_id: fields.control_receipt_id,
      runner_control_snapshot_sha256: fields.runner_control_snapshot_sha256,
      extractor_version: EXTRACTOR_VERSION,
    },
  };
}

function usageFieldsFromReceipt(payload, config) {
  const topKeys = [
    "attestation",
    "event_id",
    "generated_at",
    "kind",
    "control_receipt",
    "model_id",
    "phase_id",
    "provider",
    "receipt_id",
    "request_id",
    "round",
    "run_id",
    "schema_version",
    "scope",
    "usage",
  ];
  const usageKeys = [
    "additional_token_scope",
    "additional_tokens",
    "estimated_cost_usd",
    "input_tokens",
    "latency_ms",
    "output_tokens",
  ];
  assertExactObjectKeys(payload, topKeys, "provider usage receipt");
  assertExactObjectKeys(payload.usage, usageKeys, "provider usage receipt usage");
  if (payload.schema_version !== USAGE_RECEIPT_SCHEMA_VERSION || payload.kind !== USAGE_RECEIPT_KIND) {
    throw new Error("invalid provider usage receipt identity");
  }
  if (payload.run_id !== config.run_id) throw new Error("provider usage receipt run_id mismatch");
  const attestation = verifyProviderUsageAttestation(payload, config);
  const controlReceipt = validateControlReceipt(payload.control_receipt, config);
  const provider = requireIdentifier(payload.provider, "provider");
  const requestId = optionalIdentifier(payload.request_id, "request_id");
  const receiptId = optionalIdentifier(payload.receipt_id, "receipt_id");
  if (!requestId && !receiptId) {
    throw new Error("provider usage receipt requires request_id or receipt_id");
  }
  const eventId = requireString(payload.event_id, "receipt event_id");
  if (!/^[A-Za-z0-9._:-]{1,128}$/.test(eventId)) {
    throw new Error("invalid provider usage receipt event_id");
  }
  const scope = requireString(payload.scope, "receipt scope");
  if (!new Set(["phase", "run-total"]).has(scope)) {
    throw new Error("invalid provider usage receipt scope");
  }
  let phaseId = payload.phase_id;
  let round = payload.round;
  if (scope === "phase") {
    phaseId = requireString(phaseId, "receipt phase_id");
    if (!Number.isInteger(round) || round < 0) {
      throw new Error("invalid provider usage receipt round");
    }
  } else if (phaseId !== null || round !== null) {
    throw new Error("run-total provider usage receipt must have null phase_id and round");
  }
  const modelId = requireString(payload.model_id, "receipt model_id");
  if (config.model_id !== null && config.model_id !== undefined && modelId !== config.model_id) {
    throw new Error("provider usage receipt model_id mismatch");
  }
  if (payload.usage.additional_token_scope !== "condition-total") {
    throw new Error("provider usage receipt additional token scope mismatch");
  }
  const input = requireNonnegativeInteger(payload.usage.input_tokens, "receipt input_tokens");
  const output = requireNonnegativeInteger(payload.usage.output_tokens, "receipt output_tokens");
  const additional = requireNonnegativeInteger(
    payload.usage.additional_tokens,
    "receipt additional_tokens",
  );
  if (additional > input) throw new Error("provider usage receipt additional_tokens exceeds input_tokens");
  return {
    provider,
    request_id: requestId,
    receipt_id: receiptId,
    event_id: eventId,
    generated_at: normalizeTimestamp(payload.generated_at),
    scope,
    phase_id: phaseId,
    round,
    model_id: modelId,
    input_tokens: input,
    output_tokens: output,
    additional_tokens: additional,
    latency_ms: requireNonnegativeInteger(payload.usage.latency_ms, "receipt latency_ms"),
    estimated_cost_usd: normalizeEstimatedCost(payload.usage.estimated_cost_usd),
    ...attestation,
    ...controlReceipt,
  };
}

function verifyProviderUsageAttestation(payload, config) {
  const attestation = payload.attestation;
  assertExactObjectKeys(attestation, [
    "algorithm",
    "key_id",
    "kind",
    "schema_version",
    "signature_base64url",
    "signed_payload_sha256",
  ], "provider usage attestation");
  if (
    attestation.schema_version !== PROVIDER_ATTESTATION_SCHEMA_VERSION
    || attestation.kind !== PROVIDER_ATTESTATION_KIND
    || attestation.algorithm !== PROVIDER_ATTESTATION_ALGORITHM
    || attestation.key_id !== config.provider_attestation_key_id
    || !isPlainObject(config.provider_attestation_public_key)
    || config.provider_attestation_public_key_sha256
      !== sha256(canonicalJson(config.provider_attestation_public_key))
  ) {
    throw new Error("provider usage receipt lacks a trusted independent attestation");
  }
  const unsignedPayload = Object.fromEntries(
    Object.entries(payload).filter(([key]) => key !== "attestation"),
  );
  const signedBytes = Buffer.from(canonicalJson(unsignedPayload), "utf8");
  const signedPayloadSha256 = sha256(signedBytes);
  if (attestation.signed_payload_sha256 !== signedPayloadSha256) {
    throw new Error("provider usage attestation payload commitment mismatch");
  }
  const signature = decodeBase64Url(
    attestation.signature_base64url,
    "provider usage attestation signature",
  );
  const modulusLength = decodeBase64Url(
    config.provider_attestation_public_key.n,
    "provider attestation modulus",
  ).length;
  if (signature.length !== modulusLength) {
    throw new Error("provider usage attestation signature verification failed");
  }
  let verified = false;
  try {
    const publicKey = crypto.createPublicKey({
      key: config.provider_attestation_public_key,
      format: "jwk",
    });
    verified = crypto.verify(
      "RSA-SHA256",
      signedBytes,
      {
        key: publicKey,
        padding: crypto.constants.RSA_PKCS1_PADDING,
      },
      signature,
    );
  } catch {
    verified = false;
  }
  if (!verified) throw new Error("provider usage attestation signature verification failed");
  return {
    attestation_algorithm: PROVIDER_ATTESTATION_ALGORITHM,
    provider_attestation_key_id: attestation.key_id,
    provider_attestation_public_key_sha256: config.provider_attestation_public_key_sha256,
    signed_payload_sha256: signedPayloadSha256,
  };
}

function validateControlReceipt(receipt, config) {
  assertExactObjectKeys(receipt, [
    "caps_sha256",
    "control_receipt_id",
    "execution_controls_sha256",
    "experiment_id",
    "exposure_policy_sha256",
    "kind",
    "model_id",
    "per_exposure_token_cap",
    "pricing_snapshot_sha256",
    "provider_attestation_key_id",
    "provider_attestation_public_key_sha256",
    "provider_max_retries",
    "provider_retry_policy_sha256",
    "run_id",
    "runner_control_snapshot_sha256",
    "schema_version",
    "system_prompt_sha256",
    "tool_permissions_sha256",
  ], "execution control receipt");
  if (
    receipt.schema_version !== CONTROL_RECEIPT_SCHEMA_VERSION
    || receipt.kind !== CONTROL_RECEIPT_KIND
    || receipt.run_id !== config.run_id
    || receipt.experiment_id !== config.experiment_id
    || receipt.model_id !== config.model_id
    || receipt.runner_control_snapshot_sha256 !== config.runner_control_snapshot_sha256
    || receipt.exposure_policy_sha256 !== config.exposure_policy_sha256
    || receipt.per_exposure_token_cap !== config.per_exposure_token_cap
    || receipt.tool_permissions_sha256 !== config.tool_permissions_sha256
    || receipt.system_prompt_sha256 !== config.system_prompt_sha256
    || receipt.caps_sha256 !== config.caps_sha256
    || receipt.provider_retry_policy_sha256 !== config.provider_retry_policy_sha256
    || receipt.provider_max_retries !== config.provider_max_retries
    || receipt.pricing_snapshot_sha256 !== config.pricing_snapshot_sha256
    || receipt.provider_attestation_key_id !== config.provider_attestation_key_id
    || receipt.provider_attestation_public_key_sha256 !== config.provider_attestation_public_key_sha256
    || receipt.execution_controls_sha256 !== config.execution_controls_sha256
  ) {
    throw new Error("execution control receipt does not match the pinned run");
  }
  for (const field of [
    "runner_control_snapshot_sha256",
    "exposure_policy_sha256",
    "tool_permissions_sha256",
    "system_prompt_sha256",
    "caps_sha256",
    "provider_retry_policy_sha256",
    "pricing_snapshot_sha256",
    "provider_attestation_public_key_sha256",
    "execution_controls_sha256",
  ]) {
    requireCommitment(receipt[field], `control receipt ${field}`);
  }
  requireNonnegativeInteger(receipt.provider_max_retries, "control receipt provider_max_retries");
  requireIdentifier(receipt.provider_attestation_key_id, "provider_attestation_key_id");
  return {
    control_receipt_id: requireIdentifier(receipt.control_receipt_id, "control_receipt_id"),
    runner_control_snapshot_sha256: receipt.runner_control_snapshot_sha256,
    exposure_policy_sha256: receipt.exposure_policy_sha256,
    per_exposure_token_cap: receipt.per_exposure_token_cap,
    tool_permissions_sha256: receipt.tool_permissions_sha256,
    system_prompt_sha256: receipt.system_prompt_sha256,
    caps_sha256: receipt.caps_sha256,
    provider_retry_policy_sha256: receipt.provider_retry_policy_sha256,
    provider_max_retries: receipt.provider_max_retries,
    pricing_snapshot_sha256: receipt.pricing_snapshot_sha256,
    provider_attestation_key_id: receipt.provider_attestation_key_id,
    provider_attestation_public_key_sha256: receipt.provider_attestation_public_key_sha256,
    execution_controls_sha256: receipt.execution_controls_sha256,
  };
}

function validateUsageEvidence(record, runDir, config) {
  if (record.evidence_status === SUMMARY_USAGE_EVIDENCE) {
    if (
      record.usage_provenance !== null
      || record.control_evidence_status !== SUMMARY_USAGE_EVIDENCE
      || [
        "control_receipt_id",
        "runner_control_snapshot_sha256",
        "exposure_policy_sha256",
        "per_exposure_token_cap",
        "tool_permissions_sha256",
        "system_prompt_sha256",
        "caps_sha256",
        "provider_retry_policy_sha256",
        "provider_max_retries",
        "pricing_snapshot_sha256",
        "provider_attestation_key_id",
        "provider_attestation_public_key_sha256",
        "execution_controls_sha256",
      ].some((field) => record[field] !== null)
    ) {
      throw new Error("summary-only usage may not claim provider provenance");
    }
    return;
  }
  if (record.evidence_status !== VERIFIED_USAGE_EVIDENCE || !isPlainObject(record.usage_provenance)) {
    throw new Error("invalid execution ledger usage evidence status");
  }
  const provenance = record.usage_provenance;
  assertExactObjectKeys(provenance, [
    "attestation_algorithm",
    "attestation_key_id",
    "attestation_public_key_sha256",
    "archive_path",
    "control_receipt_id",
    "expected_sha256",
    "extractor_version",
    "original_path",
    "provider",
    "receipt_id",
    "request_id",
    "run_id",
    "runner_control_snapshot_sha256",
    "sha256",
    "signed_payload_sha256",
    "source_kind",
  ], "provider usage provenance");
  if (
    provenance.source_kind !== USAGE_RECEIPT_KIND
    || provenance.run_id !== config.run_id
    || provenance.extractor_version !== EXTRACTOR_VERSION
    || !COMMITMENT_SHA256.test(provenance.sha256)
    || provenance.expected_sha256 !== provenance.sha256
  ) {
    throw new Error("invalid provider usage provenance");
  }
  const archive = resolveRelativeChild(runDir, provenance.archive_path, "provider usage receipt archive");
  const bytes = readSafeSidecarBytes(archive);
  if (sha256(bytes) !== provenance.sha256) {
    throw new Error("provider usage receipt archive hash mismatch");
  }
  const payload = parseJsonBytes(bytes);
  const fields = usageFieldsFromReceipt(payload, config);
  const expected = {
    event_id: fields.event_id,
    run_id: config.run_id,
    generated_at: fields.generated_at,
    scope: fields.scope,
    phase_id: fields.phase_id,
    round: fields.round,
    model_id: fields.model_id,
    additional_token_scope: "condition-total",
    input_tokens: fields.input_tokens,
    output_tokens: fields.output_tokens,
    additional_tokens: fields.additional_tokens,
    latency_ms: fields.latency_ms,
    estimated_cost: fields.estimated_cost_usd,
    control_evidence_status: VERIFIED_CONTROL_EVIDENCE,
    control_receipt_id: fields.control_receipt_id,
    runner_control_snapshot_sha256: fields.runner_control_snapshot_sha256,
    exposure_policy_sha256: fields.exposure_policy_sha256,
    per_exposure_token_cap: fields.per_exposure_token_cap,
    tool_permissions_sha256: fields.tool_permissions_sha256,
    system_prompt_sha256: fields.system_prompt_sha256,
    caps_sha256: fields.caps_sha256,
    provider_retry_policy_sha256: fields.provider_retry_policy_sha256,
    provider_max_retries: fields.provider_max_retries,
    pricing_snapshot_sha256: fields.pricing_snapshot_sha256,
    provider_attestation_key_id: fields.provider_attestation_key_id,
    provider_attestation_public_key_sha256: fields.provider_attestation_public_key_sha256,
    execution_controls_sha256: fields.execution_controls_sha256,
  };
  for (const [key, value] of Object.entries(expected)) {
    if (canonicalJson(record[key]) !== canonicalJson(value)) {
      throw new Error(`provider usage receipt projection mismatch: ${key}`);
    }
  }
  if (
    provenance.provider !== fields.provider
    || provenance.request_id !== fields.request_id
    || provenance.receipt_id !== fields.receipt_id
    || provenance.control_receipt_id !== fields.control_receipt_id
    || provenance.runner_control_snapshot_sha256 !== fields.runner_control_snapshot_sha256
    || provenance.attestation_algorithm !== fields.attestation_algorithm
    || provenance.attestation_key_id !== fields.provider_attestation_key_id
    || provenance.attestation_public_key_sha256 !== fields.provider_attestation_public_key_sha256
    || provenance.signed_payload_sha256 !== fields.signed_payload_sha256
  ) {
    throw new Error("provider usage receipt identity mismatch");
  }
}

function assertManualUsageMatchesReceipt(manual, receipt) {
  const mappings = [
    ["eventId", "event_id"],
    ["generatedAt", "generated_at"],
    ["scope", "scope"],
    ["phaseId", "phase_id"],
    ["round", "round"],
    ["modelId", "model_id"],
    ["inputTokens", "input_tokens"],
    ["outputTokens", "output_tokens"],
    ["additionalTokens", "additional_tokens"],
    ["latencyMs", "latency_ms"],
  ];
  for (const [manualKey, receiptKey] of mappings) {
    const value = manual[manualKey];
    if (value !== undefined && value !== null && canonicalJson(value) !== canonicalJson(receipt[receiptKey])) {
      throw new Error(`manual usage ${manualKey} conflicts with provider receipt`);
    }
  }
  if (
    manual.estimatedCostUsd !== undefined
    && manual.estimatedCostUsd !== null
    && normalizeEstimatedCost(manual.estimatedCostUsd) !== receipt.estimated_cost_usd
  ) {
    throw new Error("manual usage estimatedCostUsd conflicts with provider receipt");
  }
}

function assertExactObjectKeys(value, expected, label) {
  if (!isPlainObject(value) || canonicalJson(Object.keys(value).sort(compareCodePoints)) !== canonicalJson([...expected].sort(compareCodePoints))) {
    throw new Error(`invalid ${label} fields`);
  }
}

function requireIdentifier(value, label) {
  if (typeof value !== "string" || !/^[A-Za-z0-9._:-]{1,128}$/.test(value)) {
    throw new Error(`invalid provider usage receipt ${label}`);
  }
  return value;
}

function optionalIdentifier(value, label) {
  if (value === null) return null;
  return requireIdentifier(value, label);
}

function extractGateEntries(source, projectRoot, generatedAt, round) {
  const payload = source.payload;
  if (!isPlainObject(payload) || !Array.isArray(payload.results) || typeof payload.passed !== "boolean") {
    return {
      entries: [],
      resolutions: [],
      command_fingerprints: [],
      unsupported_count: 1,
      gate_observation: null,
    };
  }
  const entries = [];
  const resolutions = [];
  const commandFingerprints = [];
  let unsupported = 0;
  let snapshotComplete = payload.results.length > 0;
  let requiredRowCount = 0;
  let requiredRowsPassed = true;
  for (let index = 0; index < payload.results.length; index += 1) {
    const result = payload.results[index];
    if (!isPlainObject(result)) {
      unsupported += 1;
      snapshotComplete = false;
      continue;
    }
    const gateId = typeof result.gate_id === "string" ? normalizeOneLine(result.gate_id, 80) : "";
    const argv = Array.isArray(result.argv) && result.argv.every((value) => typeof value === "string")
      ? result.argv.map((value) => normalizeArgument(value, projectRoot))
      : null;
    if (!gateId || !argv || typeof result.required !== "boolean") {
      unsupported += 1;
      snapshotComplete = false;
      continue;
    }
    const commandFingerprint = sha256(canonicalJson({ argv }));
    commandFingerprints.push(commandFingerprint);
    if (result.required === false) continue;
    requiredRowCount += 1;
    if (typeof result.passed !== "boolean") {
      unsupported += 1;
      snapshotComplete = false;
      requiredRowsPassed = false;
      continue;
    }
    if (result.passed) {
      resolutions.push({
        command_fingerprint: commandFingerprint,
        provenance: { ...source.provenance, selector: `/results/${index}` },
      });
      continue;
    }
    requiredRowsPassed = false;
    const diagnosis = gateDiagnosis(result.stderr, result.stdout, projectRoot);
    const exitCode = Number.isInteger(result.exit_code) ? result.exit_code : null;
    const diagnosisFingerprint = sha256(diagnosis);
    const failureFingerprint = sha256(canonicalJson({
      command_fingerprint: commandFingerprint,
      exit_code: exitCode,
      diagnosis_fingerprint: diagnosisFingerprint,
    }));
    const entryPayload = {
      type: "gate-failure",
      gate_id: gateId,
      required: result.required,
      exit_code: exitCode,
      diagnosis,
      command_fingerprint: commandFingerprint,
      diagnosis_fingerprint: diagnosisFingerprint,
      failure_fingerprint: failureFingerprint,
    };
    entries.push(makeEntry("status", round, generatedAt, entryPayload, {
      ...source.provenance,
      selector: `/results/${index}`,
    }));
  }
  snapshotComplete = snapshotComplete && requiredRowCount > 0;
  requiredRowsPassed = snapshotComplete && requiredRowsPassed;
  return {
    entries,
    resolutions,
    command_fingerprints: [...commandFingerprints].sort(compareCodePoints),
    unsupported_count: unsupported,
    gate_observation: {
      reported_passed: payload.passed,
      required_rows_passed: requiredRowsPassed,
      snapshot_complete: snapshotComplete,
      passed: false,
      attempt: null,
    },
  };
}

function extractReviewEntries(source, projectRoot, generatedAt, round) {
  const payload = source.payload;
  if (
    !isPlainObject(payload)
    || payload.schema_version !== REVIEW_SUMMARY_SCHEMA_VERSION
    || typeof payload.phase_id !== "string"
    || typeof payload.artifact_sha256 !== "string"
    || typeof payload.artifact_archive_path !== "string"
    || !Array.isArray(payload.findings)
  ) {
    return { entries: [], unsupported_count: 1 };
  }
  if (payload.findings.length === 0) return { entries: [], unsupported_count: 0 };
  const entries = [];
  let unsupported = 0;
  for (let index = 0; index < payload.findings.length; index += 1) {
    const normalized = normalizeFinding(payload.findings[index], projectRoot);
    if (!normalized) {
      unsupported += 1;
      continue;
    }
    normalized.review_phase_id = payload.phase_id;
    normalized.review_artifact_sha256 = payload.artifact_sha256;
    entries.push(makeEntry("knowledge", round, generatedAt, normalized, {
      ...source.provenance,
      selector: `/findings/${index}`,
    }));
  }
  return { entries, unsupported_count: unsupported };
}

function isLedgerSourcePhase(phase) {
  return phase?.id === "gates" || phase?.multi_review === true || REVIEW_PHASES.has(phase?.id);
}

function routingObservation(workflow, phase, routeKey, routedTo) {
  if (!isPlainObject(phase?.routes)) {
    const key = String(routeKey ?? "");
    const phaseIndex = workflow?.phases?.findIndex((candidate) => candidate.id === phase?.id) ?? -1;
    if (key !== "sequential" || phaseIndex < 0) {
      return { expected_target: null, violation: false, unsupported_count: 1 };
    }
    const expected = workflow.phases[phaseIndex + 1]?.id ?? "complete";
    return {
      expected_target: expected,
      violation: expected !== String(routedTo ?? ""),
      unsupported_count: 0,
    };
  }
  const key = String(routeKey ?? "");
  const expected = phase.routes[key] ?? phase.routes.default;
  if (typeof expected !== "string" || !expected || expected === "block") {
    return { expected_target: null, violation: false, unsupported_count: 1 };
  }
  return {
    expected_target: expected,
    violation: expected !== String(routedTo ?? ""),
    unsupported_count: 0,
  };
}

function canonicalGatePassed(gateObservation, routing, routedTo) {
  const target = String(routedTo ?? "");
  return gateObservation?.reported_passed === true
    && gateObservation.required_rows_passed === true
    && gateObservation.snapshot_complete === true
    && routing?.violation === false
    && typeof routing.expected_target === "string"
    && routing.expected_target === target
    && !FIX_ROUTE_TARGETS.has(target);
}

function captureMeasurement(
  entries,
  gateObservation,
  fixLoopRounds,
  routing,
  commandFingerprints = [],
) {
  const status = entries.filter((entry) => entry.payload.type === "gate-failure");
  const knowledge = entries.filter((entry) => entry.payload.type === "review-finding");
  return {
    command_fingerprints: [...commandFingerprints].sort(compareCodePoints),
    failure_fingerprints: [...new Set(status.map((entry) => entry.payload.failure_fingerprint))].sort(compareCodePoints),
    diagnosis_fingerprints: [...new Set(status.map((entry) => entry.payload.diagnosis_fingerprint))].sort(compareCodePoints),
    finding_fingerprints: [...new Set(knowledge.map((entry) => entry.payload.finding_fingerprint))].sort(compareCodePoints),
    gate_passed: gateObservation ? gateObservation.passed : null,
    gate_attempt: gateObservation ? gateObservation.attempt : null,
    fix_loop_rounds: Math.max(0, integerOrZero(fixLoopRounds)),
    canonical_routing_violation: routing.violation,
  };
}

function normalizeFinding(value, projectRoot) {
  if (typeof value === "string") {
    const finding = normalizeDiagnostic(value, projectRoot, MAX_ITEM_BYTES);
    if (!finding) return null;
    return {
      type: "review-finding",
      finding,
      finding_fingerprint: sha256(finding),
    };
  }
  if (!isPlainObject(value)) return null;
  const expectedKeys = ["line", "must_fix", "path", "severity", "statement"];
  if (Object.keys(value).sort(compareCodePoints).join("\0") !== expectedKeys.join("\0")) return null;
  if (value.must_fix !== true || !Number.isInteger(value.line) || value.line < 1) return null;
  if (typeof value.severity !== "string" || typeof value.path !== "string" || typeof value.statement !== "string") {
    return null;
  }
  const severity = normalizeOneLine(value.severity, 24).toLowerCase();
  const findingPath = normalizeFindingPath(value.path);
  const statement = normalizeDiagnostic(value.statement, projectRoot, MAX_ITEM_BYTES);
  if (!severity || !findingPath || !statement) return null;
  const exact = `${severity}|${findingPath}:${value.line}|${statement}`;
  return {
    type: "review-finding",
    must_fix: true,
    severity,
    path: findingPath,
    line: value.line,
    statement,
    finding: exact,
    finding_fingerprint: sha256(exact),
  };
}

function makeEntry(kind, round, generatedAt, payload, provenance) {
  const id = sha256(canonicalJson({
    kind,
    round,
    generated_at: generatedAt,
    payload,
    source_sha256: provenance.sha256,
    selector: provenance.selector,
  }));
  return { id, round, generated_at: generatedAt, payload, provenance };
}

function mergeLedgerEntries(
  ledger,
  additions,
  {
    gateResolutions = [],
    gateSnapshotResolution = null,
    gateCommandFingerprints = [],
    reviewObservation = null,
  } = {},
) {
  const next = structuredClone(ledger);
  for (const entry of additions) {
    const bucket = entry.payload.type === "gate-failure" ? "status" : "knowledge";
    if (!next.entries[bucket].some((existing) => existing.id === entry.id)) {
      next.entries[bucket].push(entry);
    }
  }
  for (const bucket of ["status", "knowledge"]) {
    next.entries[bucket].sort(compareEntries);
  }
  const resolvedCommands = new Map(
    gateResolutions.map((item) => [item.command_fingerprint, item.provenance]),
  );
  const snapshotCommands = new Set(gateCommandFingerprints);
  for (const entry of next.entries.status) {
    const provenance = resolvedCommands.get(entry.payload.command_fingerprint);
    if (!entry.resolution && provenance) {
      entry.resolution = resolutionRecord("gate-success", provenance);
    } else if (
      !entry.resolution
      && gateSnapshotResolution
      && !snapshotCommands.has(entry.payload.command_fingerprint)
    ) {
      entry.resolution = resolutionRecord("gate-snapshot-absent", gateSnapshotResolution);
    }
  }
  if (reviewObservation?.complete === true) {
    const current = new Set(reviewObservation.finding_fingerprints);
    for (const entry of next.entries.knowledge) {
      if (
        !entry.resolution
        && entry.payload.review_phase_id === reviewObservation.phase_id
        && !current.has(entry.payload.finding_fingerprint)
      ) {
        entry.resolution = resolutionRecord(
          "review-result",
          reviewObservation.provenance,
        );
      }
    }
  }
  next.entries.procedural = buildProceduralEntries(next);
  return next;
}

function resolutionRecord(kind, provenance) {
  return {
    kind,
    round: provenance.round,
    generated_at: provenance.generated_at,
    provenance,
  };
}

function buildProceduralEntries(ledger) {
  const groups = new Map();
  const collect = (entry, fingerprint, repeatKind, template) => {
    if (!fingerprint) return;
    if (!groups.has(fingerprint)) groups.set(fingerprint, { entries: [], repeatKind, template });
    groups.get(fingerprint).entries.push(entry);
  };
  for (const entry of ledger.entries.status) {
    if (entry.resolution) continue;
    collect(
      entry,
      entry.payload.failure_fingerprint,
      "gate-failure-repeat",
      "Before retrying, change the action that produced this repeated gate failure.",
    );
  }
  for (const entry of ledger.entries.knowledge) {
    if (entry.resolution) continue;
    collect(
      entry,
      entry.payload.finding_fingerprint,
      "review-finding-repeat",
      "Before editing, address this repeated verified review finding.",
    );
  }
  const procedural = [];
  for (const [fingerprint, group] of groups.entries()) {
    const occurrences = new Set(group.entries.map((entry) => canonicalJson({
      round: entry.round,
      generated_at: entry.generated_at,
      source_sha256: entry.provenance.sha256,
    })));
    if (occurrences.size < 2) continue;
    const latest = [...group.entries].sort(compareEntries).at(-1);
    const payload = {
      type: group.repeatKind,
      fingerprint,
      occurrences: occurrences.size,
      template: group.template,
    };
    procedural.push(makeEntry("procedural", latest.round, latest.generated_at, payload, {
      ...latest.provenance,
      selector: `/repeat/${fingerprint}`,
    }));
  }
  return procedural.sort(compareEntries);
}

function selectPromptBlock(
  context,
  phase,
  projectRoot,
  round,
  actionProjectRoot = fs.realpathSync(path.resolve(projectRoot)),
) {
  const phaseId = String(phase?.id ?? "");
  const pinnedPhase = context.workflow?.phases?.find((candidate) => candidate.id === phaseId);
  if (
    phaseId === "multi-review"
    || phase?.multi_review === true
    || pinnedPhase?.multi_review === true
  ) {
    return emptySelection("multi-review");
  }
  if (context.workflow && !pinnedPhase) return emptySelection("ineligible-phase");
  if (context.config.mode === "artifacts-only" || !context.ledger) {
    return emptySelection("artifacts-only");
  }
  if (!ELIGIBLE_PHASES.has(phaseId)) {
    return emptySelection("ineligible-phase");
  }
  const verified = { status: [], knowledge: [] };
  let staleCount = 0;
  let unsupportedCount = 0;
  for (const bucket of Object.keys(verified)) {
    for (const entry of context.ledger.entries[bucket]) {
      const verification = verifyEntry(context.runDir, context.config.run_id, entry, projectRoot);
      if (verification === "verified") verified[bucket].push(entry);
      else if (verification === "stale") staleCount += 1;
      else unsupportedCount += 1;
    }
  }
  const activeStatus = verified.status.filter((entry) => !entry.resolution);
  const activeKnowledge = verified.knowledge.filter((entry) => !entry.resolution);
  const procedural = buildProceduralEntries({
    entries: { status: activeStatus, knowledge: activeKnowledge },
  });
  const latest = {
    status: latestSemanticEntries(activeStatus, (entry) => entry.payload.command_fingerprint),
    knowledge: latestSemanticEntries(
      activeKnowledge,
      (entry) => `${entry.payload.review_phase_id}:${entry.payload.finding_fingerprint}`,
    ),
    procedural: [...procedural].sort(compareEntries).reverse(),
  };
  const totalVerified = Object.values(latest).reduce((total, entries) => total + entries.length, 0);
  if (totalVerified === 0) {
    return { ...emptySelection("no-verified-entries"), stale_count: staleCount, unsupported_count: unsupportedCount };
  }
  const selectiveCandidates = SELECTIVE_FIX_PHASES.has(phaseId)
    ? [...latest.procedural, ...latest.status, ...latest.knowledge]
    : [...latest.procedural];
  if (["ledger-selective", "action-self-review"].includes(context.config.mode)) {
    const modeCandidates = (
      context.config.mode === "action-self-review" && SELECTIVE_FIX_PHASES.has(phaseId)
    )
      ? [
          ...[...latest.status, ...latest.knowledge].sort(compareEntries).reverse(),
          ...latest.procedural,
        ]
      : selectiveCandidates;
    if (modeCandidates.length === 0) {
      return {
        ...emptySelection("selective-no-repeat"),
        stale_count: staleCount,
        unsupported_count: unsupportedCount,
      };
    }
    const selected = modeCandidates.slice(0, MAX_BLOCK_LINES - 1);
    const selectiveHeader = `## Execution ledger (advisory; run ${context.config.run_id}, round ${round}/${MAX_FIX_LOOP_ROUNDS})`;
    const selectiveBlock = boundedBlock(selectiveHeader, selected.map(promptLineForEntry));
    const actionEvidence = actionReviewEvidenceEntry(
      selected[0],
      activeStatus,
      activeKnowledge,
    );
    const actionBlock = actionReviewBlock(
      actionEvidence,
      context.runDir,
      actionProjectRoot,
    );
    const targetProxy = Math.max(
      tokenProxy(utf8ByteLength(selectiveBlock)),
      tokenProxy(utf8ByteLength(actionBlock)),
    );
    const block = context.config.mode === "action-self-review"
      ? padBlockToTokenProxy(actionBlock, targetProxy)
      : padBlockToTokenProxy(selectiveBlock, targetProxy);
    return {
      block,
      entry_ids: context.config.mode === "action-self-review" ? [] : selected.map((entry) => entry.id),
      evidence_entry_ids: context.config.mode === "action-self-review"
        ? (actionEvidence ? [actionEvidence.id] : [])
        : selected.map((entry) => entry.id),
      evidence_entries: context.config.mode === "action-self-review"
        ? (actionEvidence ? [actionEvidence] : [])
        : selected,
      stale_count: staleCount,
      unsupported_count: unsupportedCount,
      reason: block
        ? (context.config.mode === "action-self-review" ? "action-self-review" : "verified-entries")
        : "char-cap",
    };
  }
  const header = `## Execution ledger (advisory; run ${context.config.run_id}, round ${round}/${MAX_FIX_LOOP_ROUNDS})`;
  const candidates = [...latest.procedural, ...latest.status, ...latest.knowledge];
  const selected = candidates.slice(0, MAX_BLOCK_LINES - 1);
  const lines = selected.map(promptLineForEntry);
  const block = boundedBlock(header, lines);
  return {
    block,
    entry_ids: selected.map((entry) => entry.id),
    evidence_entry_ids: selected.map((entry) => entry.id),
    evidence_entries: selected,
    stale_count: staleCount,
    unsupported_count: unsupportedCount,
    reason: block ? "verified-entries" : "char-cap",
  };
}

function latestSemanticEntries(entries, semanticKey) {
  const latest = new Map();
  for (const entry of [...entries].sort(compareEntries).reverse()) {
    const key = semanticKey(entry);
    if (!latest.has(key)) latest.set(key, entry);
  }
  return [...latest.values()].sort(compareEntries).reverse();
}

function actionReviewEvidenceEntry(entry, statusEntries, knowledgeEntries) {
  const repeatType = entry?.payload?.type;
  const fingerprint = entry?.payload?.fingerprint;
  if (typeof fingerprint !== "string") return entry ?? null;
  const fields = {
    "gate-command-repeat": "command_fingerprint",
    "gate-failure-repeat": "failure_fingerprint",
    "gate-diagnosis-repeat": "diagnosis_fingerprint",
    "review-finding-repeat": "finding_fingerprint",
  };
  const field = fields[repeatType];
  if (!field) return null;
  const candidates = repeatType === "review-finding-repeat" ? knowledgeEntries : statusEntries;
  return [...candidates]
    .filter((candidate) => candidate?.payload?.[field] === fingerprint)
    .sort(compareEntries)
    .at(-1) ?? null;
}

function actionReviewBlock(entry, runDir, actionProjectRoot) {
  if (!entry?.provenance?.archive_path) return "";
  const source = readVerifiedArchive(runDir, entry.provenance.run_id, entry.provenance);
  if (!source) return "";
  const rawSlice = boundedRawSelfReviewSlice(entry, source, runDir);
  if (rawSlice === null) return "";
  const lines = [`- Source sha256: ${entry.provenance.sha256}`];
  let remaining = canonicalJson(rawSlice).replace(ANSI_PATTERN, "").replace(CONTROL_PATTERN, " ");
  const projectRoot = canonicalProjectRoot(actionProjectRoot);
  if (projectRoot) remaining = remaining.split(projectRoot).join("");
  while (remaining && lines.length < MAX_BLOCK_LINES - 1) {
    const chunk = truncateUtf8(remaining, MAX_ITEM_BYTES - 8);
    if (!chunk) break;
    lines.push(`- Raw: ${chunk}`);
    remaining = remaining.slice(chunk.length);
  }
  return boundedBlock("## Action self-review (bounded verified artifact)", lines);
}

function boundedRawSelfReviewSlice(entry, source, runDir) {
  const payload = source.payload;
  if (isPlainObject(payload) && Array.isArray(payload.results)) {
    const selectedIndex = /^\/results\/(\d+)$/.exec(String(entry.provenance.selector ?? ""));
    const selected = selectedIndex ? payload.results[Number(selectedIndex[1])] : null;
    return isPlainObject(selected) ? selected : null;
  }
  if (
    isPlainObject(payload)
    && payload.schema_version === REVIEW_SUMMARY_SCHEMA_VERSION
    && Array.isArray(payload.findings)
    && verifyReviewArtifactBinding(runDir, payload)
  ) {
    const selectedIndex = /^\/findings\/(\d+)$/.exec(String(entry.provenance.selector ?? ""));
    const finding = selectedIndex ? payload.findings[Number(selectedIndex[1])] : undefined;
    return finding === undefined
      ? null
      : { artifact_sha256: payload.artifact_sha256, finding };
  }
  return null;
}

function padBlockToTokenProxy(block, targetProxy) {
  if (!block || tokenProxy(utf8ByteLength(block)) >= targetProxy) return block;
  const targetBytes = Math.min(((targetProxy - 1) * 4) + 1, MAX_BLOCK_BYTES);
  const lines = block.split("\n").map((line) => truncateUtf8(line, MAX_ITEM_BYTES));
  while (lines.length < MAX_BLOCK_LINES && utf8ByteLength(lines.join("\n")) < targetBytes) {
    const remaining = targetBytes - utf8ByteLength(lines.join("\n")) - 1;
    if (remaining <= 0) break;
    lines.push(".".repeat(Math.min(MAX_ITEM_BYTES, remaining)));
  }
  for (
    let index = lines.length - 1;
    index >= 0 && utf8ByteLength(lines.join("\n")) < targetBytes;
    index -= 1
  ) {
    const remaining = targetBytes - utf8ByteLength(lines.join("\n"));
    const capacity = MAX_ITEM_BYTES - utf8ByteLength(lines[index]);
    lines[index] += ".".repeat(Math.min(capacity, remaining));
  }
  return truncateUtf8(lines.join("\n"), MAX_BLOCK_BYTES);
}

function promptLineForEntry(entry) {
  const payload = entry.payload;
  if (payload.type === "gate-failure") {
    const exit = payload.exit_code === null ? "unknown" : payload.exit_code;
    return truncateUtf8(`- Gate ${payload.gate_id} exit ${exit}: ${payload.diagnosis}`, MAX_ITEM_BYTES);
  }
  if (payload.type === "review-finding") {
    if (payload.path) {
      return truncateUtf8(
        `- Review ${payload.severity} ${payload.path}:${payload.line}: ${payload.statement}`,
        MAX_ITEM_BYTES,
      );
    }
    return truncateUtf8(`- Review: ${payload.finding}`, MAX_ITEM_BYTES);
  }
  return truncateUtf8(`- ${payload.template}`, MAX_ITEM_BYTES);
}

function boundedBlock(header, itemLines) {
  const lines = [header];
  for (const rawLine of itemLines) {
    if (lines.length >= MAX_BLOCK_LINES) break;
    const line = truncateUtf8(normalizeOneLine(rawLine, MAX_ITEM_BYTES), MAX_ITEM_BYTES);
    if (!line) continue;
    const candidate = [...lines, line].join("\n");
    if (utf8ByteLength(candidate) > MAX_BLOCK_BYTES) break;
    lines.push(line);
  }
  return lines.length > 1 ? lines.join("\n") : "";
}

function emptySelection(reason) {
  return {
    block: "",
    entry_ids: [],
    evidence_entry_ids: [],
    evidence_entries: [],
    stale_count: 0,
    unsupported_count: 0,
    reason,
  };
}

function verifyEntry(runDir, runId, entry, projectRoot) {
  try {
    if (
      !isPlainObject(entry)
      || typeof entry.id !== "string"
      || !Number.isInteger(entry.round)
      || typeof entry.generated_at !== "string"
      || !isPlainObject(entry.payload)
      || !isPlainObject(entry.provenance)
    ) return "unsupported";
    const source = readVerifiedArchive(runDir, runId, entry.provenance);
    if (!source) return "stale";
    let candidates;
    if (entry.payload.type === "gate-failure") {
      candidates = extractGateEntries(
        source,
        projectRoot,
        entry.generated_at,
        entry.round,
      ).entries;
    } else if (entry.payload.type === "review-finding") {
      if (!verifyReviewArtifactBinding(runDir, source.payload)) return "stale";
      candidates = extractReviewEntries(
        source,
        projectRoot,
        entry.generated_at,
        entry.round,
      ).entries;
    } else {
      return "unsupported";
    }
    const expected = candidates.find(
      (candidate) => candidate.provenance.selector === entry.provenance.selector,
    );
    if (!expected) return "unsupported";
    const baseEntry = { ...entry };
    delete baseEntry.resolution;
    if (canonicalJson(baseEntry) !== canonicalJson(expected)) return "unsupported";
    if (entry.resolution && !verifyResolution(runDir, runId, entry, projectRoot)) {
      return "unsupported";
    }
    return "verified";
  } catch (_error) {
    return "unsupported";
  }
}

function verifyRecordedEntryIntegrity(runDir, runId, entry) {
  try {
    if (!isPlainObject(entry) || !isPlainObject(entry.provenance)) return "unsupported";
    if (!readVerifiedArchive(runDir, runId, entry.provenance)) return "stale";
    if (entry.resolution) {
      if (!isPlainObject(entry.resolution.provenance)) return "unsupported";
      if (!readVerifiedArchive(runDir, runId, entry.resolution.provenance)) return "stale";
    }
    return "verified";
  } catch (_error) {
    return "unsupported";
  }
}

function readVerifiedArchive(runDir, runId, provenance) {
  const expectedKeys = [
    "archive_path",
    "extractor_version",
    "generated_at",
    "original_path",
    "round",
    "run_id",
    "selector",
    "sha256",
  ];
  if (
    Object.keys(provenance).sort(compareCodePoints).join("\0") !== expectedKeys.join("\0")
    || provenance.run_id !== runId
    || provenance.extractor_version !== EXTRACTOR_VERSION
    || !Number.isInteger(provenance.round)
    || typeof provenance.generated_at !== "string"
    || typeof provenance.original_path !== "string"
    || typeof provenance.archive_path !== "string"
    || typeof provenance.selector !== "string"
    || !/^[a-f0-9]{64}$/.test(provenance.sha256)
  ) return null;
  const resolved = resolveRelativeChild(runDir, provenance.archive_path, "ledger archive");
  if (!fs.existsSync(resolved)) return null;
  const bytes = readSafeSidecarBytes(resolved);
  if (sha256(bytes) !== provenance.sha256) return null;
  return {
    bytes,
    payload: parseJsonBytes(bytes),
    provenance: { ...provenance, selector: "/" },
  };
}

function verifyReviewArtifactBinding(runDir, payload) {
  if (
    !isPlainObject(payload)
    || typeof payload.artifact_archive_path !== "string"
    || !/^[a-f0-9]{64}$/.test(payload.artifact_sha256)
  ) return false;
  const artifact = resolveRelativeChild(
    runDir,
    payload.artifact_archive_path,
    "review artifact archive",
  );
  if (!fs.existsSync(artifact)) return false;
  return sha256(readSafeSidecarBytes(artifact)) === payload.artifact_sha256;
}

function verifyResolution(runDir, runId, entry, projectRoot) {
  const resolution = entry.resolution;
  if (
    !isPlainObject(resolution)
    || !["gate-success", "gate-snapshot-absent", "review-result"].includes(resolution.kind)
    || !Number.isInteger(resolution.round)
    || typeof resolution.generated_at !== "string"
    || !isPlainObject(resolution.provenance)
    || resolution.round !== resolution.provenance.round
    || resolution.generated_at !== resolution.provenance.generated_at
  ) return false;
  const source = readVerifiedArchive(runDir, runId, resolution.provenance);
  if (!source) return false;
  if (resolution.kind === "gate-snapshot-absent") {
    const extracted = extractGateEntries(
      source,
      projectRoot,
      resolution.generated_at,
      resolution.round,
    );
    return resolution.provenance.selector === "/"
      && extracted.gate_observation?.snapshot_complete === true
      && !extracted.command_fingerprints.includes(entry.payload.command_fingerprint);
  }
  if (resolution.kind === "gate-success") {
    const resolutions = extractGateEntries(
      source,
      projectRoot,
      resolution.generated_at,
      resolution.round,
    ).resolutions;
    return resolutions.some((candidate) => (
      candidate.command_fingerprint === entry.payload.command_fingerprint
      && candidate.provenance.selector === resolution.provenance.selector
    ));
  }
  if (!verifyReviewArtifactBinding(runDir, source.payload)) return false;
  return source.payload.phase_id === entry.payload.review_phase_id
    && !source.payload.findings.some((finding) => (
      normalizeFinding(finding, projectRoot)?.finding_fingerprint
      === entry.payload.finding_fingerprint
    ));
}

function recomputeMetrics(context, { write = true } = {}) {
  const metrics = emptyMetrics(context.config.run_id, context.config.mode);
  const captures = readJsonl(context.paths.captures);
  const injections = fs.existsSync(context.paths.injections) ? readJsonl(context.paths.injections) : [];
  const usage = fs.existsSync(context.paths.usage) ? readJsonl(context.paths.usage) : [];
  const runTotals = usage.filter((event) => event.scope === "run-total");
  const summaryRunTotal = runTotals.at(-1) ?? null;
  const runTotal = runTotals.find((event) => event.evidence_status === VERIFIED_USAGE_EVIDENCE) ?? null;
  const measurements = captures.map((capture) => capture.measurement).filter(isPlainObject);
  applyRepeatMetric(metrics, "command", measurements.flatMap((item) => item.command_fingerprints || []));
  applyRepeatMetric(metrics, "failure", measurements.flatMap((item) => item.failure_fingerprints || []));
  applyRepeatMetric(metrics, "diagnosis", measurements.flatMap((item) => item.diagnosis_fingerprints || []));
  applyRepeatMetric(metrics, "finding", measurements.flatMap((item) => item.finding_fingerprints || []));
  const firstGreen = measurements.find((item) => item.gate_passed === true && Number.isInteger(item.gate_attempt));
  const lastGateAttempt = measurements.reduce(
    (maximum, item) => Math.max(maximum, Number.isInteger(item.gate_attempt) ? item.gate_attempt : 0),
    0,
  );
  metrics.gate_green_attempt = firstGreen?.gate_attempt ?? (summaryRunTotal && lastGateAttempt > 0 ? lastGateAttempt : null);
  metrics.gate_green_le_3 = firstGreen
    ? firstGreen.gate_attempt <= MAX_FIX_LOOP_ROUNDS
    : (summaryRunTotal ? false : null);
  metrics.fix_loop_rounds = measurements.reduce(
    (maximum, item) => Math.max(maximum, Math.max(0, integerOrZero(item.fix_loop_rounds))),
    0,
  );
  metrics.prompt_bytes = injections.reduce((total, item) => total + Math.max(0, integerOrZero(item.prompt_bytes)), 0);
  metrics.injected_bytes = injections.reduce((total, item) => total + Math.max(0, integerOrZero(item.byte_count)), 0);
  metrics.injected_event_count = injections.filter((item) => item.injected === true).length;
  metrics.prompt_token_proxy = injections.reduce(
    (total, item) => total + tokenProxy(Math.max(0, integerOrZero(item.prompt_bytes))),
    0,
  );
  metrics.injected_token_proxy = injections.reduce(
    (total, item) => total + tokenProxy(Math.max(0, integerOrZero(item.byte_count))),
    0,
  );
  metrics.runner_control_snapshot_sha256 = context.config.runner_control_snapshot_sha256;
  metrics.exposure_policy_sha256 = context.config.exposure_policy_sha256;
  metrics.per_exposure_token_cap = context.config.per_exposure_token_cap;
  metrics.max_injected_token_proxy = injections.reduce(
    (maximum, item) => Math.max(maximum, tokenProxy(Math.max(0, integerOrZero(item.byte_count)))),
    0,
  );
  metrics.exposure_budget_compliant = injections.every((item) => (
    item.exposure_policy_sha256 === context.config.exposure_policy_sha256
    && item.per_exposure_token_cap === context.config.per_exposure_token_cap
    && tokenProxy(Math.max(0, integerOrZero(item.byte_count))) <= context.config.per_exposure_token_cap
  ));
  metrics.stale_candidate_count = injections.reduce(
    (total, item) => total + Math.max(0, integerOrZero(item.stale_candidate_count)),
    0,
  );
  metrics.malformed_source_count = captures.reduce(
    (total, item) => total + Math.max(0, integerOrZero(item.unsupported_count)),
    0,
  );
  metrics.excluded_source_count = injections.reduce(
    (total, item) => total + Math.max(0, integerOrZero(item.unsupported_candidate_count)),
    0,
  );
  metrics.unsupported_source_count = metrics.malformed_source_count + metrics.excluded_source_count;
  const ledgerEntries = new Map();
  if (context.ledger) {
    for (const bucket of ["status", "knowledge", "procedural"]) {
      for (const entry of context.ledger.entries[bucket]) ledgerEntries.set(entry.id, entry);
    }
  }
  let staleReminders = 0;
  let unsupportedReminders = 0;
  let injectedEntryCount = 0;
  for (const injection of injections) {
    const evidenceEntryIds = Array.isArray(injection.evidence_entry_ids)
      ? injection.evidence_entry_ids
      : (Array.isArray(injection.entry_ids) ? injection.entry_ids : []);
    const evidenceEntries = Array.isArray(injection.evidence_entries)
      ? injection.evidence_entries
      : evidenceEntryIds.map((entryId) => ledgerEntries.get(entryId)).filter(Boolean);
    if (
      canonicalJson(evidenceEntries.map((entry) => entry?.id)) !== canonicalJson(evidenceEntryIds)
    ) {
      unsupportedReminders += Math.max(evidenceEntryIds.length, evidenceEntries.length);
      injectedEntryCount += Math.max(evidenceEntryIds.length, evidenceEntries.length);
      continue;
    }
    for (const entry of evidenceEntries) {
      injectedEntryCount += 1;
      if (!isPlainObject(entry)) {
        unsupportedReminders += 1;
        continue;
      }
      const verification = verifyRecordedEntryIntegrity(context.runDir, context.config.run_id, entry);
      if (verification === "stale") staleReminders += 1;
      else if (verification !== "verified") unsupportedReminders += 1;
    }
  }
  metrics.stale_reminder_count = staleReminders;
  metrics.stale_reminder_rate = injectedEntryCount > 0 ? staleReminders / injectedEntryCount : 0;
  metrics.unsupported_reminder_count = unsupportedReminders;
  metrics.unsupported_reminder_rate = injectedEntryCount > 0 ? unsupportedReminders / injectedEntryCount : 0;
  metrics.stale_count = staleReminders;
  metrics.unsupported_count = unsupportedReminders;
  metrics.canonical_routing_violations = measurements.filter(
    (item) => item.canonical_routing_violation === true,
  ).length;
  metrics.canonical_transition_count = captures.length;
  const routeCoverage = canonicalRouteCoverage(context.workflow, captures);
  metrics.canonical_route_coverage_complete = routeCoverage.complete;
  metrics.canonical_route_coverage_gap_count = routeCoverage.gap_count;
  metrics.routing_provenance = captures.map((capture) => ({
    transition_occurrence_id: capture.transition_occurrence_id,
    workflow_id: capture.workflow_id,
    phase: capture.phase,
    round: capture.round,
    route_key: capture.route_key,
    routed_to: capture.routed_to,
    expected_target: routingExpectedTarget(capture),
  })).sort((left, right) => compareCodePoints(canonicalJson(left), canonicalJson(right)));
  metrics.actual_usage_coverage = runTotal !== null;
  metrics.actual_control_coverage = Boolean(
    runTotal && runTotal.control_evidence_status === VERIFIED_CONTROL_EVIDENCE
  );
  metrics.actual_control_evidence_status = runTotal?.control_evidence_status ?? null;
  metrics.actual_usage_evidence_status = runTotal?.evidence_status ?? null;
  metrics.actual_input_tokens = runTotal?.input_tokens ?? null;
  metrics.actual_output_tokens = runTotal?.output_tokens ?? null;
  metrics.actual_total_tokens = runTotal
    ? runTotal.input_tokens + runTotal.output_tokens
    : null;
  metrics.actual_additional_tokens = runTotal?.additional_tokens ?? null;
  metrics.actual_additional_token_coverage = Boolean(
    runTotal && runTotal.additional_token_scope === "condition-total"
  );
  metrics.actual_usage_budget_matched = Boolean(
    runTotal && runTotal.additional_tokens === metrics.injected_token_proxy
  );
  metrics.latency_ms = runTotal?.latency_ms ?? null;
  metrics.estimated_cost = runTotal?.estimated_cost ?? null;
  metrics.summary_usage_coverage = summaryRunTotal !== null;
  metrics.summary_input_tokens = summaryRunTotal?.input_tokens ?? null;
  metrics.summary_output_tokens = summaryRunTotal?.output_tokens ?? null;
  metrics.summary_additional_tokens = summaryRunTotal?.additional_tokens ?? null;
  metrics.summary_latency_ms = summaryRunTotal?.latency_ms ?? null;
  metrics.summary_estimated_cost = summaryRunTotal?.estimated_cost ?? null;
  const controlsComplete = [
    "base_commit",
    "experiment_id",
    "model_id",
    "tool_permissions_sha256",
    "system_prompt_sha256",
    "caps_sha256",
    "provider_retry_policy_sha256",
    "provider_max_retries",
    "pricing_snapshot_sha256",
    "provider_attestation_key_id",
    "provider_attestation_public_key_sha256",
    "execution_controls_sha256",
  ].every((key) => context.config[key] !== null && context.config[key] !== undefined);
  metrics.experiment_valid = Boolean(
    metrics.actual_usage_coverage
    && metrics.actual_control_coverage
    && metrics.actual_additional_token_coverage
    && lastGateAttempt > 0
    && controlsComplete
    && metrics.stale_reminder_count === 0
    && metrics.unsupported_reminder_count === 0
    && metrics.canonical_routing_violations === 0
    && metrics.canonical_route_coverage_complete
    && metrics.exposure_budget_compliant
  );
  if (write) writeCanonicalJson(context.paths.metrics, metrics);
  return metrics;
}

function canonicalRouteCoverage(workflow, captures) {
  const phaseIds = new Set(workflow.phases.map((phase) => phase.id));
  let expectedPhase = workflow.phases[0]?.id ?? "complete";
  let gapCount = 0;
  for (const capture of captures) {
    if (expectedPhase === "complete" || capture.phase !== expectedPhase) {
      gapCount += 1;
    }
    const target = capture.routed_to;
    if (target !== "complete" && !phaseIds.has(target)) {
      gapCount += 1;
      expectedPhase = null;
      continue;
    }
    expectedPhase = target;
  }
  if (expectedPhase !== "complete") gapCount += 1;
  return { complete: gapCount === 0, gap_count: gapCount };
}

function routingExpectedTarget(capture) {
  return capture.expected_target ?? null;
}

function applyRepeatMetric(metrics, label, values) {
  const filtered = values.filter((value) => typeof value === "string" && value);
  const counts = new Map();
  for (const value of filtered) counts.set(value, (counts.get(value) ?? 0) + 1);
  const repeated = [...counts.values()].reduce((total, count) => total + Math.max(0, count - 1), 0);
  metrics[`repeated_${label}_fingerprint_count`] = repeated;
  metrics[`repeated_${label}_fingerprint_rate`] = filtered.length > 0 ? repeated / filtered.length : 0;
}

function createCanonicalReviewSummary({
  runDir,
  runId,
  phaseId,
  artifactPath,
  routeKey,
  projectRoot,
  generatedAt,
  round,
}) {
  if (!artifactPath || !fs.existsSync(artifactPath)) return null;
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(phaseId)) return null;
  const verdict = ["approve", "request-changes", "blocked"].includes(routeKey)
    ? routeKey
    : null;
  if (!verdict) return null;
  const artifact = archiveSource(
    runDir,
    artifactPath,
    "/review-artifact",
    generatedAt,
    round,
    runId,
  );
  const findings = verdict === "approve"
    ? []
    : parseReviewFindings(artifact.bytes, projectRoot);
  const payload = {
    schema_version: REVIEW_SUMMARY_SCHEMA_VERSION,
    run_id: runId,
    phase_id: phaseId,
    round,
    generated_at: generatedAt,
    artifact_path: artifact.provenance.original_path,
    artifact_sha256: artifact.provenance.sha256,
    artifact_archive_path: artifact.provenance.archive_path,
    verdict,
    findings,
  };
  const summaryId = sha256(canonicalJson(payload));
  const summaryPath = path.join(
    ledgerPaths(runDir).root,
    "review-summaries",
    `${summaryId}.json`,
  );
  writeImmutableJson(summaryPath, payload);
  return {
    path: summaryPath,
    artifact_provenance: artifact.provenance,
    complete: verdict === "approve" || findings.length > 0,
    unsupported_count: verdict === "approve" || findings.length > 0 ? 0 : 1,
  };
}

function parseReviewFindings(bytes, projectRoot) {
  const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  const findings = [];
  let inFindings = false;
  let inReviewer = false;
  let inCodeFence = false;
  for (const line of text.split(/\r?\n/)) {
    const stripped = line.trim();
    if (stripped.startsWith("```")) {
      inCodeFence = !inCodeFence;
      continue;
    }
    if (inCodeFence) continue;
    const heading = /^(#{1,6})\s+(.+)$/.exec(stripped);
    if (heading) {
      const title = heading[2].trim();
      inFindings = /\bfinding(?:s)?\b/i.test(title);
      inReviewer = /^reviewer(?:\s|$)/i.test(title);
      continue;
    }
    if ((!inFindings && !inReviewer) || !stripped.startsWith("- ")) continue;
    const finding = normalizeDiagnostic(stripped.slice(2), projectRoot, MAX_ITEM_BYTES);
    if (!finding || ["none", "n/a", "없음"].includes(finding.toLowerCase())) continue;
    findings.push(finding);
  }
  return [...new Set(findings)].sort(compareCodePoints);
}

function gateDiagnosis(stderr, stdout, projectRoot) {
  const fromStderr = firstNonblankLine(stderr, projectRoot);
  return truncateUtf8(fromStderr || firstNonblankLine(stdout, projectRoot), 120);
}

function firstNonblankLine(value, projectRoot) {
  if (typeof value !== "string") return "";
  for (const line of value.split(/\r?\n/)) {
    const normalized = normalizeDiagnostic(line, projectRoot, 120);
    if (normalized) return normalized;
  }
  return "";
}

function normalizeArgument(value, projectRoot) {
  return normalizeDiagnostic(String(value), projectRoot, 512);
}

function normalizeDiagnostic(value, projectRoot, limit) {
  let normalized = String(value ?? "")
    .replace(ANSI_PATTERN, "")
    .replace(CONTROL_PATTERN, " ");
  const root = canonicalProjectRoot(projectRoot);
  if (root) {
    normalized = normalized.split(root).join("");
  }
  normalized = normalized.replace(/\s+/g, " ").trim();
  return truncateUtf8(normalized, limit);
}

function canonicalProjectRoot(value) {
  return value === null || value === undefined || String(value) === ""
    ? ""
    : path.resolve(String(value));
}

function normalizeOneLine(value, limit) {
  return truncateUtf8(
    String(value ?? "")
      .replace(ANSI_PATTERN, "")
      .replace(CONTROL_PATTERN, " ")
      .replace(/[\r\n]+/g, " ")
      .replace(/\s+/g, " ")
      .trim(),
    limit,
  );
}

function normalizeFindingPath(value) {
  const normalized = String(value).replaceAll("\\", "/").replace(/^\.\//, "");
  if (!normalized || normalized.startsWith("/") || normalized.split("/").includes("..")) return "";
  return normalizeOneLine(normalized, 240);
}

function readJsonl(pathName) {
  if (!fs.existsSync(pathName)) return [];
  const bytes = readSafeSidecarBytes(pathName);
  const records = [];
  for (const line of bytes.toString("utf8").split(/\r?\n/).filter(Boolean)) {
    const parsed = JSON.parse(line);
    if (!isPlainObject(parsed) || typeof parsed.id !== "string") {
      throw new Error(`invalid JSONL record: ${pathName}`);
    }
    records.push(parsed);
  }
  return records;
}

function writeImmutableJson(pathName, payload) {
  if (fs.existsSync(pathName)) {
    if (canonicalJson(readJson(pathName)) !== canonicalJson(payload)) {
      throw new Error(`immutable execution ledger config mismatch: ${pathName}`);
    }
    return;
  }
  writeCanonicalJson(pathName, payload);
}

function ensureJsonl(pathName) {
  ensureSafeSidecarParent(pathName, true);
  if (fs.existsSync(pathName)) assertSafeSidecarFile(pathName);
  const flags = fs.constants.O_CREAT | fs.constants.O_APPEND | fs.constants.O_WRONLY | (fs.constants.O_NOFOLLOW ?? 0);
  const descriptor = fs.openSync(pathName, flags, 0o600);
  try {
    if (!fs.fstatSync(descriptor).isFile()) throw new Error(`unsafe execution ledger file: ${pathName}`);
  } finally {
    fs.closeSync(descriptor);
  }
}

function writeCanonicalJson(pathName, payload) {
  writeAtomic(pathName, `${canonicalJson(payload)}\n`);
}

function writeAtomic(pathName, content) {
  ensureSafeSidecarParent(pathName, true);
  if (fs.existsSync(pathName)) assertSafeSidecarFile(pathName);
  const temp = path.join(
    path.dirname(pathName),
    `.${path.basename(pathName)}.${process.pid}.${crypto.randomBytes(6).toString("hex")}.tmp`,
  );
  let descriptor = null;
  try {
    descriptor = fs.openSync(temp, fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_WRONLY, 0o600);
    fs.writeFileSync(descriptor, content, typeof content === "string" ? "utf8" : undefined);
    fs.fsyncSync(descriptor);
    fs.closeSync(descriptor);
    descriptor = null;
    fs.renameSync(temp, pathName);
    fsyncDirectory(path.dirname(pathName));
  } finally {
    if (descriptor !== null) fs.closeSync(descriptor);
    fs.rmSync(temp, { force: true });
  }
}

function preserveAtomicBytes(pathName, bytes) {
  ensureSafeSidecarParent(pathName, true);
  if (fs.existsSync(pathName)) {
    const stat = fs.lstatSync(pathName);
    if (stat.isSymbolicLink() || !stat.isFile() || !fs.readFileSync(pathName).equals(bytes)) {
      throw new Error(`execution ledger source hash collision: ${pathName}`);
    }
    return;
  }
  const temp = path.join(
    path.dirname(pathName),
    `.${path.basename(pathName)}.${process.pid}.${crypto.randomBytes(6).toString("hex")}.tmp`,
  );
  let descriptor = null;
  try {
    descriptor = fs.openSync(temp, fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_WRONLY, 0o600);
    fs.writeFileSync(descriptor, bytes);
    fs.fsyncSync(descriptor);
    fs.closeSync(descriptor);
    descriptor = null;
    if (fs.existsSync(pathName)) {
      const stat = fs.lstatSync(pathName);
      if (stat.isSymbolicLink() || !stat.isFile() || !fs.readFileSync(pathName).equals(bytes)) {
        throw new Error(`execution ledger source hash collision: ${pathName}`);
      }
    } else {
      fs.renameSync(temp, pathName);
      fsyncDirectory(path.dirname(pathName));
    }
  } finally {
    if (descriptor !== null) fs.closeSync(descriptor);
    fs.rmSync(temp, { force: true });
  }
}

function fsyncDirectory(directory) {
  let descriptor = null;
  try {
    descriptor = fs.openSync(directory, fs.constants.O_RDONLY);
    fs.fsyncSync(descriptor);
  } catch (error) {
    if (!new Set(["EINVAL", "ENOTSUP", "EISDIR"]).has(error?.code)) throw error;
  } finally {
    if (descriptor !== null) fs.closeSync(descriptor);
  }
}

function readJson(pathName) {
  const payload = JSON.parse(readSafeSidecarBytes(pathName).toString("utf8"));
  if (!isPlainObject(payload)) throw new Error(`invalid execution ledger JSON: ${pathName}`);
  return payload;
}

function readSafeSidecarBytes(pathName) {
  assertSafeSidecarFile(pathName);
  const flags = fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0);
  const descriptor = fs.openSync(pathName, flags);
  try {
    if (!fs.fstatSync(descriptor).isFile()) throw new Error(`unsafe execution ledger file: ${pathName}`);
    return fs.readFileSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
}

function appendJsonlRecord(pathName, payload) {
  const current = readSafeSidecarBytes(pathName);
  const separator = current.length === 0 || current.at(-1) === 0x0a ? "" : "\n";
  writeAtomic(pathName, Buffer.concat([
    current,
    Buffer.from(`${separator}${canonicalJson(payload)}\n`, "utf8"),
  ]));
}

function acquireTransactionLock(paths) {
  ensureSafeSidecarParent(paths.lock, true);
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const token = crypto.randomBytes(16).toString("hex");
    const owner = { pid: process.pid, token };
    try {
      const flags = fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_WRONLY | (fs.constants.O_NOFOLLOW ?? 0);
      const descriptor = fs.openSync(paths.lock, flags, 0o600);
      try {
        fs.writeFileSync(descriptor, `${canonicalJson(owner)}\n`, "utf8");
        fs.fsyncSync(descriptor);
      } finally {
        fs.closeSync(descriptor);
      }
      fsyncDirectory(path.dirname(paths.lock));
      return owner;
    } catch (error) {
      if (error?.code !== "EEXIST") throw error;
      const existing = readJson(paths.lock);
      if (!Number.isInteger(existing.pid) || existing.pid < 1 || typeof existing.token !== "string") {
        throw new Error("invalid execution ledger transaction lock");
      }
      if (processIsAlive(existing.pid)) {
        throw new Error("execution ledger transaction is locked");
      }
      unlinkSafeSidecarFile(paths.lock);
    }
  }
  throw new Error("execution ledger transaction lock recovery failed");
}

function releaseTransactionLock(paths, owner) {
  if (!owner || !sidecarPathExists(paths.lock)) return;
  const current = readJson(paths.lock);
  if (current.pid !== owner.pid || current.token !== owner.token) {
    throw new Error("execution ledger transaction lock ownership changed");
  }
  unlinkSafeSidecarFile(paths.lock);
}

function unlinkSafeSidecarFile(pathName) {
  assertSafeSidecarFile(pathName);
  fs.unlinkSync(pathName);
  fsyncDirectory(path.dirname(pathName));
}

function processIsAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error?.code !== "ESRCH";
  }
}

function assertSafeSidecarFile(pathName) {
  ensureSafeSidecarParent(pathName, false);
  const stat = fs.lstatSync(pathName);
  if (stat.isSymbolicLink() || !stat.isFile()) {
    throw new Error(`unsafe execution ledger file: ${pathName}`);
  }
}

function ensureSafeSidecarParent(pathName, create) {
  const resolved = path.resolve(pathName);
  const boundary = sidecarBoundary(resolved);
  const runRoot = fs.realpathSync(boundary.runRoot);
  if (runRoot !== boundary.runRoot) {
    throw new Error(`unsafe execution ledger parent: ${pathName}`);
  }
  const relativeParent = path.relative(runRoot, path.dirname(resolved));
  if (!relativeParent || relativeParent.startsWith("..") || path.isAbsolute(relativeParent)) {
    throw new Error(`execution ledger path escapes run: ${pathName}`);
  }
  let current = runRoot;
  for (const part of relativeParent.split(path.sep)) {
    current = path.join(current, part);
    if (!fs.existsSync(current)) {
      if (!create) throw new Error(`missing execution ledger parent: ${current}`);
      fs.mkdirSync(current);
      continue;
    }
    const stat = fs.lstatSync(current);
    if (stat.isSymbolicLink() || !stat.isDirectory()) {
      throw new Error(`unsafe execution ledger parent: ${current}`);
    }
  }
}

function sidecarBoundary(pathName) {
  let current = path.dirname(pathName);
  while (path.dirname(current) !== current) {
    if (path.basename(current) === "execution-ledger" && path.basename(path.dirname(current)) === "artifacts") {
      return { runRoot: path.dirname(path.dirname(current)), sidecarRoot: current };
    }
    current = path.dirname(current);
  }
  throw new Error(`execution ledger path escapes sidecar: ${pathName}`);
}

function parseJsonBytes(bytes) {
  try {
    return JSON.parse(bytes.toString("utf8"));
  } catch (_error) {
    return null;
  }
}

function assertRunDocument(payload, runId, mode, label) {
  if (payload.schema_version !== SCHEMA_VERSION || payload.run_id !== runId || payload.mode !== mode) {
    throw new Error(`execution ledger ${label} identity mismatch`);
  }
}

function assertRunnerControlSnapshot(config) {
  const snapshot = config.runner_control_snapshot;
  assertExactObjectKeys(snapshot, [
    "base_commit",
    "execution_controls_sha256",
    "exposure_policy_sha256",
    "fix_loop_max_rounds",
    "installed_skill_plan_sha256",
    "local_skill_plan_sha256",
    "lore_snapshot_sha256",
    "profile_snapshot_sha256",
    "prompt_controls_sha256",
    "runtime_id",
    "schema_version",
    "task_sha256",
    "workflow_id",
    "workflow_sha256",
  ], "runner control snapshot");
  for (const field of [
    "execution_controls_sha256",
    "exposure_policy_sha256",
    "installed_skill_plan_sha256",
    "local_skill_plan_sha256",
    "lore_snapshot_sha256",
    "profile_snapshot_sha256",
    "prompt_controls_sha256",
    "task_sha256",
    "workflow_sha256",
  ]) {
    requireCommitment(snapshot[field], `runner control snapshot ${field}`);
  }
  requireIdentifier(snapshot.runtime_id, "runner control snapshot runtime_id");
  if (
    snapshot.schema_version !== SCHEMA_VERSION
    || snapshot.task_sha256 !== config.task_sha256
    || snapshot.workflow_id !== config.workflow_id
    || snapshot.workflow_sha256 !== config.workflow_sha256
    || snapshot.base_commit !== config.base_commit
    || snapshot.fix_loop_max_rounds !== config.fix_loop_max_rounds
    || canonicalJson(config.exposure_policy) !== canonicalJson(EXPOSURE_POLICY)
    || config.exposure_policy_sha256 !== sha256(canonicalJson(EXPOSURE_POLICY))
    || snapshot.exposure_policy_sha256 !== config.exposure_policy_sha256
    || snapshot.execution_controls_sha256 !== config.execution_controls_sha256
    || config.per_exposure_token_cap !== EXPOSURE_POLICY.per_exposure_token_cap
    || config.runner_control_snapshot_sha256 !== sha256(canonicalJson(snapshot))
  ) {
    throw new Error("execution ledger runner control snapshot mismatch");
  }
  const controls = canonicalExecutionControls(config);
  const recordedControls = Object.fromEntries(
    Object.keys(controls).map((key) => [key, config[key]]),
  );
  if (
    canonicalJson(controls) !== canonicalJson(recordedControls)
    || config.execution_controls_sha256 !== sha256(canonicalJson(controls))
  ) {
    throw new Error("execution ledger execution controls mismatch");
  }
}

function assertLedger(payload, runId) {
  if (payload.schema_version !== SCHEMA_VERSION || payload.run_id !== runId || !isPlainObject(payload.entries)) {
    throw new Error("execution ledger identity mismatch");
  }
  for (const bucket of ["status", "knowledge", "procedural"]) {
    if (!Array.isArray(payload.entries[bucket])) throw new Error(`invalid execution ledger bucket: ${bucket}`);
  }
}

function safeRelativePath(root, target, label) {
  const relative = path.relative(path.resolve(root), path.resolve(target));
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`${label} is outside the run: ${target}`);
  }
  return relative.split(path.sep).join("/");
}

function resolveRelativeChild(root, relative, label) {
  if (typeof relative !== "string" || !relative || path.isAbsolute(relative)) {
    throw new Error(`invalid ${label} path`);
  }
  const resolvedRoot = fs.realpathSync(path.resolve(root));
  const lexical = path.resolve(resolvedRoot, relative);
  safeRelativePath(resolvedRoot, lexical, label);
  if (!fs.existsSync(lexical)) return lexical;
  const resolved = fs.realpathSync(lexical);
  safeRelativePath(resolvedRoot, resolved, label);
  return resolved;
}

function safeExtension(pathName) {
  const extension = path.extname(pathName).toLowerCase();
  return /^\.[a-z0-9]{1,8}$/.test(extension) ? extension : ".bin";
}

function canonicalJson(value) {
  return JSON.stringify(canonicalValue(value));
}

function canonicalValue(value) {
  if (Array.isArray(value)) return value.map(canonicalValue);
  if (isPlainObject(value)) {
    return Object.fromEntries(
      Object.keys(value)
        .sort(compareCodePoints)
        .map((key) => [key, canonicalValue(value[key])]),
    );
  }
  if (typeof value === "number" && !Number.isFinite(value)) return null;
  return value;
}

function sha256(value) {
  const bytes = Buffer.isBuffer(value) ? value : Buffer.from(String(value), "utf8");
  return crypto.createHash("sha256").update(bytes).digest("hex");
}

function compareEntries(left, right) {
  if (left.round !== right.round) return left.round - right.round;
  const timestamp = compareCodePoints(left.generated_at, right.generated_at);
  if (timestamp !== 0) return timestamp;
  return compareCodePoints(left.id, right.id);
}

function compareCodePoints(left, right) {
  const a = Array.from(String(left), (char) => char.codePointAt(0));
  const b = Array.from(String(right), (char) => char.codePointAt(0));
  for (let index = 0; index < Math.min(a.length, b.length); index += 1) {
    if (a[index] !== b[index]) return a[index] - b[index];
  }
  return a.length - b.length;
}

function normalizeRound(value) {
  const round = Number(value);
  if (!Number.isInteger(round) || round < 1) return 1;
  return Math.min(round, MAX_FIX_LOOP_ROUNDS);
}

function normalizeTimestamp(value) {
  const match = typeof value === "string" ? STRICT_RFC3339.exec(value) : null;
  if (!match) {
    throw new Error("invalid execution ledger timestamp");
  }
  const [, year, month, day, hour, minute, second, , zone] = match;
  const numeric = [year, month, day, hour, minute, second].map(Number);
  const [yyyy, mm, dd, hh, min, sec] = numeric;
  const daysInMonth = new Date(Date.UTC(yyyy, mm, 0)).getUTCDate();
  const zoneValid = zone === "Z" || (() => {
    const [, zoneHour, zoneMinute] = /^([+-]\d{2}):(\d{2})$/.exec(zone) ?? [];
    return Number(zoneHour?.slice(1)) <= 23 && Number(zoneMinute) <= 59;
  })();
  if (
    yyyy < 1 || mm < 1 || mm > 12 || dd < 1 || dd > daysInMonth
    || hh > 23 || min > 59 || sec > 59 || !zoneValid
    || !Number.isFinite(Date.parse(value))
  ) {
    throw new Error("invalid execution ledger timestamp");
  }
  const normalized = new Date(value).toISOString();
  if (!/^(?!0000)\d{4}-/.test(normalized)) {
    throw new Error("invalid execution ledger timestamp");
  }
  return normalized;
}

function tokenProxy(bytes) {
  return bytes > 0 ? Math.ceil(bytes / 4) : 0;
}

function utf8ByteLength(value) {
  return Buffer.byteLength(String(value), "utf8");
}

function truncateUtf8(value, maxBytes) {
  let result = "";
  let size = 0;
  for (const character of String(value)) {
    const scalar = /^[\uD800-\uDFFF]$/.test(character) ? "\uFFFD" : character;
    const bytes = utf8ByteLength(scalar);
    if (size + bytes > maxBytes) break;
    result += scalar;
    size += bytes;
  }
  return result;
}

function integerOrZero(value) {
  return Number.isInteger(Number(value)) ? Number(value) : 0;
}

function requireString(value, label) {
  if (typeof value !== "string" || !value) throw new Error(`missing execution ledger ${label}`);
  return value;
}

function requireNonnegativeInteger(value, label) {
  if (!Number.isInteger(value) || value < 0) {
    throw new Error(`invalid execution ledger ${label}`);
  }
  return value;
}

function optionalNonnegativeInteger(value, label) {
  if (value === null || value === undefined || value === "") return null;
  return requireNonnegativeInteger(value, label);
}

function normalizePricingRate(value, label) {
  let text;
  if (
    typeof value === "number"
    && Number.isSafeInteger(value)
    && value >= 0
    && value <= Number.MAX_SAFE_INTEGER
  ) {
    text = String(value);
  } else if (typeof value === "string") {
    text = value;
  } else {
    throw new Error(`invalid execution ledger ${label}`);
  }
  if (!/^(0|[1-9]\d*)(?:\.\d{1,12})?$/.test(text)) {
    throw new Error(`invalid execution ledger ${label}`);
  }
  if (!Number.isFinite(Number(text)) || Number(text) > Number.MAX_SAFE_INTEGER) {
    throw new Error(`invalid execution ledger ${label}`);
  }
  return text.includes(".") ? text.replace(/0+$/, "").replace(/\.$/, "") : text;
}

function requireCommitment(value, label) {
  if (typeof value !== "string" || !COMMITMENT_SHA256.test(value)) {
    throw new Error(`invalid execution ledger ${label}`);
  }
  return value;
}

function optionalCommitment(value, label) {
  if (value === null || value === undefined || value === "") return null;
  return requireCommitment(value, label);
}

function normalizeEstimatedCost(value) {
  if (value === null || value === undefined) return null;
  if (typeof value !== "string" || !/^(0|[1-9]\d*)(?:\.\d{1,12})?$/.test(value)) {
    throw new Error("invalid execution ledger estimated_cost_usd");
  }
  if (!value.includes(".")) return value;
  return value.replace(/0+$/, "").replace(/\.$/, "");
}

function optionalString(value) {
  return typeof value === "string" && value ? value : null;
}

function optionalControlIdentifier(value, label) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value !== "string" || !/^[A-Za-z0-9._:-]{1,128}$/.test(value)) {
    throw new Error(`invalid execution ledger ${label}`);
  }
  return value;
}

function decodeBase64Url(value, label) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/.test(value)) {
    throw new Error(`invalid execution ledger ${label}`);
  }
  const bytes = Buffer.from(value, "base64url");
  if (bytes.length === 0 || bytes.toString("base64url") !== value) {
    throw new Error(`invalid execution ledger ${label}`);
  }
  return bytes;
}

function isPlainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function failOpen(error, extra = {}) {
  return {
    ok: false,
    error: error instanceof Error ? error.message : String(error),
    ...extra,
  };
}
