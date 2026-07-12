#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { createHash, createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { fileURLToPath } from "node:url";

import { validateExecutionStateLedger } from "../lib/execution-state-ledger.mjs";

export const PILOT_MODES = Object.freeze([
  "artifacts-only",
  "ledger-always",
  "ledger-selective",
  "action-self-review",
]);

export const FROZEN_CONTROL_FIELDS = Object.freeze([
  "task_sha256",
  "workflow_id",
  "workflow_sha256",
  "base_commit",
  "fix_loop_max_rounds",
  "runner_control_snapshot_sha256",
  "execution_controls_sha256",
  "exposure_policy_sha256",
  "per_exposure_token_cap",
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
]);

export const MIN_MATCHED_TRIALS = 3;
const MIN_REPRODUCED_FRACTION = 2 / 3;
const MIN_SELECTIVE_GATE_GREEN_RATE = 2 / 3;
const VERIFIED_EVIDENCE_TAG = Symbol("verified-execution-ledger-run-dirs");
const VERIFIED_EVIDENCE_SECRET = randomBytes(32);
const MATCHED_CONTROL_FIELDS = Object.freeze(
  FROZEN_CONTROL_FIELDS.filter((field) => field !== "experiment_id"),
);

const REPEATED_METRIC_FIELDS = Object.freeze([
  "repeated_command_fingerprint_count",
  "repeated_failure_fingerprint_count",
  "repeated_diagnosis_fingerprint_count",
  "repeated_finding_fingerprint_count",
]);

const REPEATED_RATE_FIELDS = Object.freeze([
  "repeated_command_fingerprint_rate",
  "repeated_failure_fingerprint_rate",
  "repeated_diagnosis_fingerprint_rate",
  "repeated_finding_fingerprint_rate",
]);

const SAFETY_FIELDS = Object.freeze([
  "stale_reminder_count",
  "stale_reminder_rate",
  "unsupported_reminder_count",
  "unsupported_reminder_rate",
  "canonical_routing_violations",
]);

const SOURCE_QUALITY_FIELDS = Object.freeze([
  "malformed_source_count",
  "excluded_source_count",
]);

export function compareExecutionLedgerPilot(payload) {
  const reasons = [];
  const evidenceVerified = hasVerifiedRunDirEvidence(payload);
  if (!evidenceVerified) reasons.push("evidence is not verified; comparison is summary-only");
  if (Array.isArray(payload?.evidence_reasons)) {
    reasons.push(...payload.evidence_reasons.filter((value) => typeof value === "string" && value));
  }
  const runs = Array.isArray(payload?.runs) ? payload.runs : [];
  const unknownModes = [];
  const runIds = runs.map((run) => run?.run_id).filter((value) => typeof value === "string" && value);
  const duplicateRunIds = [...new Set(runIds.filter((value, index) => runIds.indexOf(value) !== index))];
  if (runIds.length !== runs.length) reasons.push("every run requires a non-empty run_id");
  if (duplicateRunIds.length > 0) reasons.push(`duplicate run_ids: ${duplicateRunIds.join(", ")}`);
  const trialsById = new Map();
  for (const [index, run] of runs.entries()) {
    const mode = run?.mode;
    if (!PILOT_MODES.includes(mode)) {
      unknownModes.push(String(mode));
      continue;
    }
    const experimentId = run?.controls?.experiment_id;
    if (!validControlValue("experiment_id", experimentId)) {
      reasons.push(`missing control experiment_id for run ${String(run?.run_id ?? index)}`);
      continue;
    }
    if (!trialsById.has(experimentId)) trialsById.set(experimentId, new Map());
    const trial = trialsById.get(experimentId);
    if (trial.has(mode)) {
      reasons.push(`duplicate mode ${mode} for experiment ${experimentId}`);
      continue;
    }
    trial.set(mode, run);
  }

  if (unknownModes.length > 0) reasons.push(`unknown modes: ${unknownModes.join(", ")}`);
  const trialEntries = [...trialsById.entries()]
    .sort(([left], [right]) => String(left).localeCompare(String(right), "en"));
  for (const [experimentId, trial] of trialEntries) {
    const missingModes = PILOT_MODES.filter((mode) => !trial.has(mode));
    if (missingModes.length > 0) {
      reasons.push(`experiment ${experimentId} missing modes: ${missingModes.join(", ")}`);
    }
  }
  if (trialEntries.length < MIN_MATCHED_TRIALS) {
    reasons.push(`expected at least ${MIN_MATCHED_TRIALS} matched experiments, got ${trialEntries.length}`);
  }

  const controlResult = compareFrozenControls(runs);
  reasons.push(...controlResult.reasons);
  const pricingResult = resolvePinnedPricing(runs, payload?.pricing);
  reasons.push(...pricingResult.reasons);

  const normalizedTrials = [];
  const normalizedByMode = Object.fromEntries(PILOT_MODES.map((mode) => [mode, []]));
  let actualUsageComplete = true;
  let experimentRecordsValid = true;
  let measurementsComplete = true;
  let safetyViolations = 0;

  for (const [experimentId, trial] of trialEntries) {
    const arms = {};
    for (const mode of PILOT_MODES) {
      const run = trial.get(mode);
      if (!run) continue;
      const normalized = normalizeRun(run, pricingResult.pricing);
      arms[mode] = normalized;
      normalizedByMode[mode].push(normalized);
      if (!normalized.actual_usage_complete) actualUsageComplete = false;
      if (!normalized.experiment_valid) experimentRecordsValid = false;
      if (!normalized.measurements_complete) measurementsComplete = false;
      safetyViolations += normalized.safety_violations;
      reasons.push(...normalized.reasons.map((reason) => `${experimentId}/${mode}: ${reason}`));
    }
    normalizedTrials.push({ experiment_id: experimentId, arms });
  }

  const normalizedArms = Object.fromEntries(
    PILOT_MODES.map((mode) => [mode, aggregateArm(normalizedByMode[mode])]),
  );
  const structureValid = (
    unknownModes.length === 0
    && trialEntries.length >= MIN_MATCHED_TRIALS
    && runs.length === trialEntries.length * PILOT_MODES.length
    && runIds.length === runs.length
    && duplicateRunIds.length === 0
    && trialEntries.every(([, trial]) => PILOT_MODES.every((mode) => trial.has(mode)))
    && controlResult.valid
    && pricingResult.valid
  );
  const budgetMatched = normalizedTrials.length >= MIN_MATCHED_TRIALS
    && normalizedTrials.every((trial) => actionSelfReviewBudgetMatched(trial.arms));

  let candidateAdoptionDecision = "reject";
  let candidateBestMode = null;
  if (safetyViolations > 0) {
    reasons.push(`safety violations: ${safetyViolations}`);
  } else if (
    !structureValid
    || !actualUsageComplete
    || !experimentRecordsValid
    || !measurementsComplete
    || !budgetMatched
  ) {
    if (!actualUsageComplete) reasons.push("actual provider usage is incomplete");
    if (!experimentRecordsValid) reasons.push("one or more experiment records are invalid");
    if (!measurementsComplete) reasons.push("one or more required measurements are incomplete");
    if (!budgetMatched) {
      reasons.push("ledger-selective and action-self-review precommitted exposure policies or per-exposure budgets do not match");
    }
  } else {
    const selectiveResult = selectiveMeetsAdoptionPolicy(normalizedArms, normalizedTrials);
    if (!selectiveResult.accept) {
      reasons.push(...selectiveResult.reasons);
    } else {
      candidateAdoptionDecision = "accept";
      candidateBestMode = "ledger-selective";
    }
  }
  const evidenceStatus = !evidenceVerified
    ? (Array.isArray(payload?.evidence_reasons) && payload.evidence_reasons.length > 0
      ? "invalid"
      : "insufficient")
    : (
      !structureValid
      || !actualUsageComplete
      || !measurementsComplete
      || !budgetMatched
        ? "insufficient"
        : (!experimentRecordsValid && safetyViolations === 0 ? "invalid" : "verified")
    );

  return {
    schema_version: 1,
    evidence_verified: evidenceVerified,
    summary_only: !evidenceVerified,
    evidence_status: evidenceStatus,
    comparison_valid: evidenceVerified
      && structureValid
      && actualUsageComplete
      && experimentRecordsValid
      && measurementsComplete
      && budgetMatched,
    adoption_decision: evidenceVerified ? candidateAdoptionDecision : "reject",
    best_mode: evidenceVerified ? candidateBestMode : null,
    candidate_adoption_decision: candidateAdoptionDecision,
    candidate_best_mode: candidateBestMode,
    frozen_controls: controlResult.controls,
    pricing: pricingResult.pricing,
    safety_violations: safetyViolations,
    action_self_review_budget_matched: budgetMatched,
    reasons: [...new Set(reasons)],
    arms: normalizedArms,
    trials: normalizedTrials,
  };
}

export function readExecutionLedgerRunDirs(runDirs) {
  const validations = runDirs.map((runDir) => readExecutionLedgerRunDir(runDir));
  const result = {
    schema_version: 1,
    evidence_verified: validations.length > 0 && validations.every((item) => item.verified),
    evidence_reasons: validations.flatMap((item) => item.reasons),
    runs: validations.map((item) => item.run),
  };
  if (result.evidence_verified) {
    Object.defineProperty(result, VERIFIED_EVIDENCE_TAG, {
      value: evidenceTag(result),
      enumerable: true,
    });
  }
  return result;
}

function hasVerifiedRunDirEvidence(payload) {
  if (!payload || payload.evidence_verified !== true) return false;
  const actual = payload[VERIFIED_EVIDENCE_TAG];
  if (typeof actual !== "string" || !/^[a-f0-9]{64}$/.test(actual)) return false;
  const expected = evidenceTag(payload);
  return timingSafeEqual(Buffer.from(actual, "hex"), Buffer.from(expected, "hex"));
}

function evidenceTag(payload) {
  return createHmac("sha256", VERIFIED_EVIDENCE_SECRET)
    .update(canonicalValue({
      evidence_reasons: payload?.evidence_reasons ?? [],
      evidence_verified: payload?.evidence_verified === true,
      runs: payload?.runs ?? [],
      schema_version: payload?.schema_version ?? null,
    }))
    .digest("hex");
}

function readExecutionLedgerRunDir(runDir) {
  const resolved = path.resolve(runDir);
  const validation = validateExecutionStateLedger({
    runDir: resolved,
    requireCompletion: true,
  });
  if (
    validation.ok !== true
    || validation.verified !== true
    || validation.completed !== true
    || !validation.config
    || !validation.metrics
    || !Array.isArray(validation.captures)
    || !Array.isArray(validation.injections)
    || !Array.isArray(validation.usage)
  ) {
    const detail = Array.isArray(validation.reasons) && validation.reasons.length > 0
      ? validation.reasons.join("; ")
      : String(validation.error ?? "execution ledger evidence validation failed");
    return {
      verified: false,
      reasons: [`${resolved}: ${detail}`],
      run: {
        schema_version: 1,
        run_id: null,
        mode: null,
        controls: {},
        usage_events: [],
        exposure_schedule: [],
        experiment_valid: false,
      },
    };
  }
  const config = validation.config;
  const runTotal = validation.usage.find((event) => event.scope === "run-total") ?? null;
  const providerUsageVerified = Boolean(
    runTotal?.evidence_status === "verified-provider-receipt"
    && validation.metrics.actual_usage_coverage === true
    && runTotal?.control_evidence_status === "verified-control-receipt"
    && validation.metrics.actual_control_coverage === true
  );
  const controls = Object.fromEntries(
    FROZEN_CONTROL_FIELDS.map((field) => [field, config[field]]),
  );
  return {
    verified: providerUsageVerified,
    reasons: providerUsageVerified
      ? []
      : [`${resolved}: run-total usage is summary-only or lacks provider receipt provenance`],
    run: {
      ...validation.metrics,
      schema_version: 1,
      run_id: config.run_id,
      mode: config.mode,
      controls,
      usage_events: validation.usage,
      actual_additional_token_scope: runTotal?.additional_token_scope ?? null,
      exposure_schedule: validation.injections
        .filter((event) => event.injected === true)
        .map((event) => ({ phase: event.phase, round: event.round }))
        .sort(compareExposure),
      pricing_snapshot: config.pricing_snapshot ?? null,
    },
  };
}

function compareFrozenControls(runs) {
  const reasons = [];
  const baseline = runs[0];
  const controls = {};
  for (const field of MATCHED_CONTROL_FIELDS) {
    const baselineValue = baseline?.controls?.[field];
    if (!validControlValue(field, baselineValue)) {
      reasons.push(`missing control ${field} for artifacts-only`);
      continue;
    }
    controls[field] = baselineValue;
    for (const run of runs.slice(1)) {
      const value = run?.controls?.[field];
      if (!validControlValue(field, value)) {
        reasons.push(`missing control ${field} for ${String(run?.run_id ?? run?.mode)}`);
      } else if (canonicalValue(value) !== canonicalValue(baselineValue)) {
        reasons.push(`control mismatch ${field} for ${String(run?.run_id ?? run?.mode)}`);
      }
    }
  }
  return { controls, reasons, valid: reasons.length === 0 };
}

function validControlValue(field, value) {
  if (field === "fix_loop_max_rounds" || field === "per_exposure_token_cap") {
    return Number.isInteger(value) && value > 0;
  }
  if (field === "provider_max_retries") return Number.isInteger(value) && value >= 0;
  if (field.endsWith("_sha256")) return typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
  return typeof value === "string" && value.length > 0;
}

function resolvePinnedPricing(runs, suppliedPricing) {
  const reasons = [];
  const baseline = normalizePricingSnapshot(runs[0]?.pricing_snapshot);
  if (baseline === null) {
    return { pricing: null, reasons: ["missing pinned pricing snapshot"], valid: false };
  }
  const canonical = canonicalValue(baseline);
  const digest = createHash("sha256").update(canonical).digest("hex");
  for (const run of runs) {
    if (canonicalValue(normalizePricingSnapshot(run?.pricing_snapshot)) !== canonical) {
      reasons.push(`pricing snapshot mismatch for ${String(run?.run_id ?? run?.mode)}`);
    }
    if (run?.controls?.pricing_snapshot_sha256 !== digest) {
      reasons.push(`pricing snapshot digest mismatch for ${String(run?.run_id ?? run?.mode)}`);
    }
  }
  if (
    suppliedPricing !== undefined
    && canonicalValue(normalizePricingSnapshot(suppliedPricing)) !== canonical
  ) {
    reasons.push("supplied pricing does not match the run-pinned pricing snapshot");
  }
  return { pricing: baseline, reasons, valid: reasons.length === 0 };
}

function normalizePricingSnapshot(value) {
  if (
    !value
    || typeof value !== "object"
    || Array.isArray(value)
    || Object.keys(value).sort().join(",")
      !== "currency,input_per_million,output_per_million,snapshot_id"
    || typeof value.currency !== "string"
    || value.currency.length === 0
    || typeof value.snapshot_id !== "string"
    || value.snapshot_id.length === 0
  ) return null;
  const input = normalizePricingRate(value.input_per_million);
  const output = normalizePricingRate(value.output_per_million);
  if (input === null || output === null) return null;
  return {
    currency: value.currency,
    input_per_million: input,
    output_per_million: output,
    snapshot_id: value.snapshot_id,
  };
}

function normalizePricingRate(value) {
  let text;
  if (
    typeof value === "number"
    && Number.isSafeInteger(value)
    && value >= 0
    && value <= Number.MAX_SAFE_INTEGER
  ) text = String(value);
  else if (typeof value === "string") text = value;
  else return null;
  if (!/^(0|[1-9]\d*)(?:\.\d{1,12})?$/.test(text)) return null;
  if (!Number.isFinite(Number(text)) || Number(text) > Number.MAX_SAFE_INTEGER) return null;
  return text.includes(".") ? text.replace(/0+$/, "").replace(/\.$/, "") : text;
}

function normalizeRun(run, pricing) {
  const reasons = [];
  const usage = validateRunTotalUsage(run);
  reasons.push(...usage.reasons);
  const actualUsageComplete = usage.complete;
  if (!actualUsageComplete) reasons.push("missing actual token usage");
  const actualControlComplete = usage.control_complete;
  if (!actualControlComplete) reasons.push("missing verified execution control receipt");

  const repeated = Object.fromEntries(
    REPEATED_METRIC_FIELDS.map((field) => [field, nonNegativeNumber(run[field])]),
  );
  const repeatedRates = Object.fromEntries(
    REPEATED_RATE_FIELDS.map((field) => [field, unitRateOrZero(run[field])]),
  );
  const safety = Object.fromEntries(
    SAFETY_FIELDS.map((field) => [field, nonNegativeNumber(run[field])]),
  );
  const sourceQuality = Object.fromEntries(
    SOURCE_QUALITY_FIELDS.map((field) => [field, nullableNonNegativeNumber(run[field])]),
  );
  const safetyViolations = Object.values(safety).reduce((total, value) => total + value, 0);
  const gateGreenAttempt = isPositiveInteger(run.gate_green_attempt) ? run.gate_green_attempt : null;
  const gateMetricValid = (
    (run.gate_green_le_3 === true && gateGreenAttempt !== null && gateGreenAttempt <= 3)
    || (run.gate_green_le_3 === false && gateGreenAttempt !== null)
  );
  const gateGreenLe3 = gateMetricValid && run.gate_green_le_3 === true;
  const gateEffort = gateGreenLe3 ? gateGreenAttempt : 4;
  if (!gateMetricValid) {
    reasons.push("gate_green_le_3 conflicts with gate_green_attempt");
  }

  const promptBytes = nonNegativeNumber(run.prompt_bytes ?? run.prompt_chars);
  const injectedBytes = nonNegativeNumber(run.injected_bytes ?? run.injected_chars);
  const promptTokenProxy = isNonNegativeInteger(run.prompt_token_proxy)
    ? run.prompt_token_proxy
    : Math.ceil(promptBytes / 4);
  const injectedTokenProxy = isNonNegativeInteger(run.injected_token_proxy)
    ? run.injected_token_proxy
    : Math.ceil(injectedBytes / 4);
  const actualAdditionalTokens = integerOrNull(run.actual_additional_tokens);
  const additionalTokenScope = usage.event?.additional_token_scope ?? null;
  const additionalTokenCoverageVerified = actualAdditionalTokens !== null
    && additionalTokenScope === "condition-total";
  if (!additionalTokenCoverageVerified) {
    reasons.push("actual additional_tokens require condition-total coverage");
  }
  const estimatedCost = estimateCost(run, pricing, usage.event);
  if (estimatedCost === null) reasons.push("missing pinned pricing or estimated cost");
  const reportedCost = decimalCostNumber(usage.event?.estimated_cost);
  const costVerified = estimatedCost !== null
    && reportedCost !== null
    && Math.abs(estimatedCost - reportedCost) <= 1e-12;
  if (!costVerified) reasons.push("reported estimated cost does not match pinned pricing");
  const exposureScheduleValid = isExposureSchedule(run.exposure_schedule);
  const exposureSchedule = exposureScheduleValid
    ? run.exposure_schedule.map((item) => ({ phase: item.phase, round: item.round })).sort(compareExposure)
    : [];
  if (!exposureScheduleValid) reasons.push("missing or invalid injection exposure schedule");
  const runnerControlSnapshotSha256 = run.runner_control_snapshot_sha256;
  const exposurePolicySha256 = run.exposure_policy_sha256;
  const perExposureTokenCap = integerOrNull(run.per_exposure_token_cap);
  const exposureBudgetCompliant = run.exposure_budget_compliant === true
    && isPositiveInteger(perExposureTokenCap)
    && isNonNegativeInteger(run.max_injected_token_proxy)
    && run.max_injected_token_proxy <= perExposureTokenCap;
  if (!exposureBudgetCompliant) reasons.push("exposure token budget policy is not satisfied");
  const routeCoverageComplete = run.canonical_route_coverage_complete === true
    && run.canonical_route_coverage_gap_count === 0;
  if (!routeCoverageComplete) reasons.push("canonical transition route coverage is incomplete");
  const measurementsComplete = (
    REPEATED_METRIC_FIELDS.every((field) => isNonNegativeNumber(run[field]))
    && REPEATED_RATE_FIELDS.every((field) => isUnitRate(run[field]))
    && SAFETY_FIELDS.every((field) => (
      field.endsWith("_rate") ? isUnitRate(run[field]) : isNonNegativeNumber(run[field])
    ))
    && SOURCE_QUALITY_FIELDS.every((field) => isNonNegativeNumber(run[field]))
    && isNonNegativeNumber(run.fix_loop_rounds)
    && isNonNegativeInteger(run.injected_event_count)
    && gateMetricValid
    && isNonNegativeNumber(run.latency_ms)
    && estimatedCost !== null
    && additionalTokenCoverageVerified
    && costVerified
    && exposureScheduleValid
    && validControlValue("runner_control_snapshot_sha256", runnerControlSnapshotSha256)
    && validControlValue("exposure_policy_sha256", exposurePolicySha256)
    && exposureBudgetCompliant
    && routeCoverageComplete
  );
  if (!measurementsComplete) reasons.push("missing required count, rate, latency, or cost measurement");

  return {
    run_id: typeof run.run_id === "string" ? run.run_id : null,
    experiment_valid: run.experiment_valid === true
      && additionalTokenCoverageVerified
      && costVerified
      && gateMetricValid
      && actualControlComplete
      && exposureBudgetCompliant
      && routeCoverageComplete,
    measurements_complete: measurementsComplete,
    gate_green_le_3: gateGreenLe3,
    gate_green_attempt: gateGreenAttempt,
    gate_effort: gateEffort,
    ...repeated,
    ...repeatedRates,
    fix_loop_rounds: nonNegativeNumber(run.fix_loop_rounds),
    actual_usage_complete: actualUsageComplete,
    actual_control_complete: actualControlComplete,
    actual_usage_event_id: usage.event?.event_id ?? null,
    actual_input_tokens: integerOrNull(run.actual_input_tokens),
    actual_output_tokens: integerOrNull(run.actual_output_tokens),
    actual_total_tokens: integerOrNull(run.actual_total_tokens),
    actual_additional_tokens: actualAdditionalTokens,
    actual_additional_token_scope: additionalTokenScope,
    prompt_bytes: promptBytes,
    injected_bytes: injectedBytes,
    prompt_token_proxy: promptTokenProxy,
    injected_token_proxy: injectedTokenProxy,
    injected_event_count: integerOrNull(run.injected_event_count),
    exposure_schedule: exposureSchedule,
    runner_control_snapshot_sha256: runnerControlSnapshotSha256,
    exposure_policy_sha256: exposurePolicySha256,
    per_exposure_token_cap: perExposureTokenCap,
    exposure_budget_compliant: exposureBudgetCompliant,
    max_injected_token_proxy: integerOrNull(run.max_injected_token_proxy),
    canonical_route_coverage_complete: routeCoverageComplete,
    canonical_route_coverage_gap_count: integerOrNull(run.canonical_route_coverage_gap_count),
    additional_token_coverage_verified: additionalTokenCoverageVerified,
    latency_ms: nullableNonNegativeNumber(run.latency_ms),
    latency_proxy_ms: nullableNonNegativeNumber(run.latency_proxy_ms),
    estimated_cost: estimatedCost,
    cost_verified: costVerified,
    stale_candidate_count: nonNegativeNumber(run.stale_candidate_count),
    unsupported_source_count: nonNegativeNumber(run.unsupported_source_count),
    ...sourceQuality,
    ...safety,
    safety_violations: safetyViolations,
    reasons,
  };
}

function aggregateArm(runs) {
  if (runs.length === 0) return null;
  const meanField = (field) => mean(runs.map((run) => run[field]));
  const sumField = (field) => runs.reduce((total, run) => total + nonNegativeNumber(run[field]), 0);
  const sumCompleteField = (field) => runs.every((run) => isNonNegativeNumber(run[field]))
    ? runs.reduce((total, run) => total + run[field], 0)
    : null;
  const gateGreenRate = runs.filter((run) => run.gate_green_le_3 === true).length / runs.length;
  return {
    run_count: runs.length,
    run_ids: runs.map((run) => run.run_id),
    experiment_valid: runs.every((run) => run.experiment_valid),
    measurements_complete: runs.every((run) => run.measurements_complete),
    actual_usage_complete: runs.every((run) => run.actual_usage_complete),
    actual_control_complete: runs.every((run) => run.actual_control_complete),
    gate_green_le_3: gateGreenRate === 1,
    gate_green_le_3_rate: gateGreenRate,
    gate_green_attempt: meanField("gate_green_attempt"),
    gate_effort: meanField("gate_effort"),
    ...Object.fromEntries(REPEATED_METRIC_FIELDS.map((field) => [field, meanField(field)])),
    ...Object.fromEntries(REPEATED_RATE_FIELDS.map((field) => [field, meanField(field)])),
    fix_loop_rounds: meanField("fix_loop_rounds"),
    actual_usage_event_ids: runs.map((run) => run.actual_usage_event_id),
    actual_input_tokens: meanField("actual_input_tokens"),
    actual_output_tokens: meanField("actual_output_tokens"),
    actual_total_tokens: meanField("actual_total_tokens"),
    actual_additional_tokens: meanField("actual_additional_tokens"),
    actual_additional_token_scope: runs.every((run) => run.actual_additional_token_scope === "condition-total")
      ? "condition-total"
      : null,
    prompt_bytes: meanField("prompt_bytes"),
    injected_bytes: meanField("injected_bytes"),
    prompt_token_proxy: meanField("prompt_token_proxy"),
    injected_token_proxy: meanField("injected_token_proxy"),
    injected_event_count: meanField("injected_event_count"),
    exposure_schedules: runs.map((run) => run.exposure_schedule),
    runner_control_snapshot_sha256: runs.every(
      (run) => run.runner_control_snapshot_sha256 === runs[0].runner_control_snapshot_sha256,
    ) ? runs[0].runner_control_snapshot_sha256 : null,
    exposure_policy_sha256: runs.every(
      (run) => run.exposure_policy_sha256 === runs[0].exposure_policy_sha256,
    ) ? runs[0].exposure_policy_sha256 : null,
    per_exposure_token_cap: runs.every(
      (run) => run.per_exposure_token_cap === runs[0].per_exposure_token_cap,
    ) ? runs[0].per_exposure_token_cap : null,
    exposure_budget_compliant: runs.every((run) => run.exposure_budget_compliant),
    canonical_route_coverage_complete: runs.every(
      (run) => run.canonical_route_coverage_complete,
    ),
    canonical_route_coverage_gap_count: sumField("canonical_route_coverage_gap_count"),
    max_injected_token_proxy: meanField("max_injected_token_proxy"),
    additional_token_coverage_verified: runs.every((run) => run.additional_token_coverage_verified),
    latency_ms: meanField("latency_ms"),
    latency_proxy_ms: meanNullable(runs.map((run) => run.latency_proxy_ms)),
    estimated_cost: meanField("estimated_cost"),
    cost_verified: runs.every((run) => run.cost_verified),
    stale_candidate_count: sumField("stale_candidate_count"),
    unsupported_source_count: sumField("unsupported_source_count"),
    malformed_source_count: sumCompleteField("malformed_source_count"),
    excluded_source_count: sumCompleteField("excluded_source_count"),
    ...Object.fromEntries(SAFETY_FIELDS.map((field) => [field, sumField(field)])),
    safety_violations: sumField("safety_violations"),
    reasons: runs.flatMap((run) => run.reasons),
  };
}

function mean(values) {
  return values.length > 0 && values.every(isNonNegativeNumber)
    ? Number((values.reduce((total, value) => total + value, 0) / values.length).toFixed(12))
    : null;
}

function meanNullable(values) {
  const present = values.filter(isNonNegativeNumber);
  return present.length === values.length && present.length > 0 ? mean(present) : null;
}

function estimateCost(run, pricing, usageEvent) {
  if (
    !pricing
    || typeof pricing.currency !== "string"
    || pricing.currency.length === 0
    || typeof pricing.snapshot_id !== "string"
    || pricing.snapshot_id.length === 0
    || normalizePricingRate(pricing.input_per_million) === null
    || normalizePricingRate(pricing.output_per_million) === null
  ) {
    return null;
  }
  const input = usageEvent?.input_tokens ?? run.actual_input_tokens;
  const output = usageEvent?.output_tokens ?? run.actual_output_tokens;
  if (!isNonNegativeInteger(input) || !isNonNegativeInteger(output)) {
    return null;
  }
  return (
    (input * Number(pricing.input_per_million))
    + (output * Number(pricing.output_per_million))
  ) / 1_000_000;
}

function validateRunTotalUsage(run) {
  const reasons = [];
  const events = Array.isArray(run.usage_events) ? run.usage_events : [];
  const byId = new Map();
  for (const event of events) {
    if (!event || typeof event !== "object" || Array.isArray(event)) {
      reasons.push("invalid usage event object");
      continue;
    }
    const eventId = event.event_id;
    const expectedId = typeof eventId === "string" && typeof event.run_id === "string"
      ? usageEventHash(event.run_id, eventId)
      : null;
    const structurallyValid = (
      event.schema_version === 1
      && typeof event.id === "string"
      && event.id === expectedId
      && typeof eventId === "string"
      && /^[A-Za-z0-9._:-]{1,128}$/.test(eventId)
      && event.run_id === run.run_id
      && typeof event.generated_at === "string"
      && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(event.generated_at)
      && ["phase", "run-total"].includes(event.scope)
      && typeof event.model_id === "string"
      && event.model_id === run.controls?.model_id
      && isNonNegativeInteger(event.input_tokens)
      && isNonNegativeInteger(event.output_tokens)
      && isNonNegativeInteger(event.additional_tokens)
      && event.additional_tokens <= event.input_tokens
      && event.additional_token_scope === "condition-total"
      && event.evidence_status === "verified-provider-receipt"
      && event.control_evidence_status === "verified-control-receipt"
      && typeof event.control_receipt_id === "string"
      && /^[A-Za-z0-9._:-]{1,128}$/.test(event.control_receipt_id)
      && event.runner_control_snapshot_sha256 === run.controls?.runner_control_snapshot_sha256
      && event.exposure_policy_sha256 === run.controls?.exposure_policy_sha256
      && event.per_exposure_token_cap === run.controls?.per_exposure_token_cap
      && event.tool_permissions_sha256 === run.controls?.tool_permissions_sha256
      && event.system_prompt_sha256 === run.controls?.system_prompt_sha256
      && event.caps_sha256 === run.controls?.caps_sha256
      && event.provider_retry_policy_sha256 === run.controls?.provider_retry_policy_sha256
      && event.provider_max_retries === run.controls?.provider_max_retries
      && event.pricing_snapshot_sha256 === run.controls?.pricing_snapshot_sha256
      && event.provider_attestation_key_id === run.controls?.provider_attestation_key_id
      && event.provider_attestation_public_key_sha256 === run.controls?.provider_attestation_public_key_sha256
      && event.execution_controls_sha256 === run.controls?.execution_controls_sha256
      && isVerifiedUsageProvenance(event.usage_provenance, event)
      && isNonNegativeNumber(event.latency_ms)
      && (event.estimated_cost === null || decimalCostNumber(event.estimated_cost) !== null)
      && (
        event.scope === "run-total"
          ? event.phase_id === null && event.round === null
          : typeof event.phase_id === "string" && event.phase_id.length > 0 && isNonNegativeInteger(event.round)
      )
    );
    if (!structurallyValid) {
      reasons.push(`invalid usage event ${String(eventId)}`);
      continue;
    }
    const canonical = canonicalValue(event);
    if (byId.has(event.id) && byId.get(event.id).canonical !== canonical) {
      reasons.push(`conflicting usage event id ${event.id}`);
      continue;
    }
    if (!byId.has(event.id)) byId.set(event.id, { event, canonical });
  }
  const totals = [...byId.values()].map((item) => item.event).filter((event) => event.scope === "run-total");
  if (totals.length !== 1) reasons.push(`expected one distinct run-total usage event, got ${totals.length}`);
  const event = totals.length === 1 ? totals[0] : null;
  if (event) {
    const expectedTotal = event.input_tokens + event.output_tokens;
    if (
      run.actual_usage_coverage !== true
      || run.actual_control_coverage !== true
      || run.actual_control_evidence_status !== event.control_evidence_status
      || run.actual_input_tokens !== event.input_tokens
      || run.actual_output_tokens !== event.output_tokens
      || run.actual_total_tokens !== expectedTotal
      || run.actual_additional_tokens !== event.additional_tokens
      || run.actual_additional_token_scope !== event.additional_token_scope
      || run.latency_ms !== event.latency_ms
      || decimalCostNumber(event.estimated_cost) === null
    ) {
      reasons.push("run-total usage event does not match aggregated actual metrics");
    }
  }
  const controlComplete = Boolean(
    event
    && event.control_evidence_status === "verified-control-receipt"
    && validControlValue("runner_control_snapshot_sha256", event.runner_control_snapshot_sha256)
    && validControlValue("exposure_policy_sha256", event.exposure_policy_sha256)
    && validControlValue("per_exposure_token_cap", event.per_exposure_token_cap)
    && validControlValue("tool_permissions_sha256", event.tool_permissions_sha256)
    && validControlValue("system_prompt_sha256", event.system_prompt_sha256)
    && validControlValue("caps_sha256", event.caps_sha256)
    && validControlValue("provider_retry_policy_sha256", event.provider_retry_policy_sha256)
    && validControlValue("provider_max_retries", event.provider_max_retries)
    && validControlValue("pricing_snapshot_sha256", event.pricing_snapshot_sha256)
    && validControlValue("provider_attestation_key_id", event.provider_attestation_key_id)
    && validControlValue("provider_attestation_public_key_sha256", event.provider_attestation_public_key_sha256)
    && validControlValue("execution_controls_sha256", event.execution_controls_sha256)
  );
  return {
    complete: reasons.length === 0 && event !== null && controlComplete,
    control_complete: controlComplete,
    event,
    reasons,
  };
}

function isVerifiedUsageProvenance(provenance, event) {
  return Boolean(
    provenance
    && typeof provenance === "object"
    && !Array.isArray(provenance)
    && provenance.source_kind === "provider-usage-receipt"
    && provenance.run_id === event.run_id
    && typeof provenance.original_path === "string"
    && provenance.original_path.length > 0
    && typeof provenance.archive_path === "string"
    && provenance.archive_path.length > 0
    && typeof provenance.sha256 === "string"
    && /^[a-f0-9]{64}$/.test(provenance.sha256)
    && provenance.expected_sha256 === provenance.sha256
    && provenance.control_receipt_id === event.control_receipt_id
    && provenance.runner_control_snapshot_sha256 === event.runner_control_snapshot_sha256
    && provenance.attestation_algorithm === "RS256"
    && provenance.attestation_key_id === event.provider_attestation_key_id
    && provenance.attestation_public_key_sha256 === event.provider_attestation_public_key_sha256
    && typeof provenance.signed_payload_sha256 === "string"
    && /^[a-f0-9]{64}$/.test(provenance.signed_payload_sha256)
    && typeof provenance.provider === "string"
    && provenance.provider.length > 0
    && (
      (typeof provenance.request_id === "string" && provenance.request_id.length > 0)
      || (typeof provenance.receipt_id === "string" && provenance.receipt_id.length > 0)
    )
  );
}

function usageEventHash(runId, eventId) {
  return createHash("sha256")
    .update(JSON.stringify({ event_id: eventId, run_id: runId }))
    .digest("hex");
}

function selectiveMeetsAdoptionPolicy(arms, trials) {
  const baseline = arms["artifacts-only"];
  const always = arms["ledger-always"];
  const selective = arms["ledger-selective"];
  const selfReview = arms["action-self-review"];
  if (!baseline || !always || !selective || !selfReview) {
    return { accept: false, reasons: ["all four normalized arms are required"] };
  }
  const reasons = [];
  if (
    selective.gate_green_le_3_rate < MIN_SELECTIVE_GATE_GREEN_RATE
    || selective.gate_green_le_3_rate < baseline.gate_green_le_3_rate
  ) {
    reasons.push("ledger-selective gate_green_le_3 rate is insufficient or regressed");
  }
  if (!(
    selective.repeated_command_fingerprint_count <= baseline.repeated_command_fingerprint_count
    && selective.repeated_command_fingerprint_rate <= baseline.repeated_command_fingerprint_rate
    && metricPairImproved(selective, baseline, "failure")
    && selective.repeated_diagnosis_fingerprint_count <= baseline.repeated_diagnosis_fingerprint_count
    && selective.repeated_diagnosis_fingerprint_rate <= baseline.repeated_diagnosis_fingerprint_rate
    && selective.fix_loop_rounds < baseline.fix_loop_rounds
    && reproducedNonRegression(trials, "ledger-selective", "artifacts-only", "repeated_command_fingerprint_count")
    && reproducedNonRegression(trials, "ledger-selective", "artifacts-only", "repeated_command_fingerprint_rate")
    && reproducedPairImprovement(trials, "ledger-selective", "artifacts-only", "failure")
    && reproducedNonRegression(trials, "ledger-selective", "artifacts-only", "repeated_diagnosis_fingerprint_count")
    && reproducedNonRegression(trials, "ledger-selective", "artifacts-only", "repeated_diagnosis_fingerprint_rate")
    && reproducedReduction(trials, "ledger-selective", "artifacts-only", "fix_loop_rounds")
  )) {
    reasons.push("ledger-selective did not reproducibly reduce baseline failure/fix-loop without command or diagnosis regression");
  }
  if (!(
    selfReviewParetoImproved(selective, selfReview)
    && reproducedSelfReviewParetoImprovement(trials)
  )) {
    reasons.push("ledger-selective did not reproducibly outperform action-self-review");
  }
  if (!(
    selective.actual_additional_tokens < always.actual_additional_tokens
    && selective.latency_ms < always.latency_ms
    && selective.estimated_cost < always.estimated_cost
    && reproducedReduction(trials, "ledger-selective", "ledger-always", "actual_additional_tokens")
    && reproducedReduction(trials, "ledger-selective", "ledger-always", "latency_ms")
    && reproducedReduction(trials, "ledger-selective", "ledger-always", "estimated_cost")
  )) {
    reasons.push("ledger-selective was not reproducibly more token, latency, and cost efficient than ledger-always");
  }
  return { accept: reasons.length === 0, reasons };
}

function actionSelfReviewBudgetMatched(arms) {
  const selective = arms["ledger-selective"];
  const selfReview = arms["action-self-review"];
  if (!selective || !selfReview) return false;
  if (!validControlValue("exposure_policy_sha256", selective.exposure_policy_sha256)
    || !validControlValue("exposure_policy_sha256", selfReview.exposure_policy_sha256)
    || !isPositiveInteger(selective.per_exposure_token_cap)
    || !isPositiveInteger(selfReview.per_exposure_token_cap)
    || selective.exposure_budget_compliant !== true
    || selfReview.exposure_budget_compliant !== true) {
    return false;
  }
  return selective.exposure_policy_sha256 === selfReview.exposure_policy_sha256
    && selective.per_exposure_token_cap === selfReview.per_exposure_token_cap;
}

function reproducedReduction(trials, leftMode, rightMode, field) {
  return reproducedComparison(
    trials,
    (trial) => trial.arms[leftMode]?.[field],
    (trial) => trial.arms[rightMode]?.[field],
  );
}

function metricPairImproved(left, right, label) {
  const count = `repeated_${label}_fingerprint_count`;
  const rate = `repeated_${label}_fingerprint_rate`;
  return left[count] <= right[count]
    && left[rate] <= right[rate]
    && (left[count] < right[count] || left[rate] < right[rate]);
}

function reproducedPairImprovement(trials, leftMode, rightMode, label) {
  const complete = trials.filter((trial) => (
    trial.arms[leftMode] && trial.arms[rightMode]
  ));
  if (complete.length < MIN_MATCHED_TRIALS) return false;
  const improved = complete.filter((trial) => (
    metricPairImproved(trial.arms[leftMode], trial.arms[rightMode], label)
  )).length;
  return improved / complete.length >= MIN_REPRODUCED_FRACTION;
}

function reproducedNonRegression(trials, leftMode, rightMode, field) {
  const complete = trials.filter((trial) => (
    isNonNegativeNumber(trial.arms[leftMode]?.[field])
    && isNonNegativeNumber(trial.arms[rightMode]?.[field])
  ));
  if (complete.length < MIN_MATCHED_TRIALS) return false;
  const notWorse = complete.filter((trial) => (
    trial.arms[leftMode][field] <= trial.arms[rightMode][field]
  )).length;
  return notWorse / complete.length >= MIN_REPRODUCED_FRACTION;
}

function selfReviewParetoImproved(selective, selfReview) {
  if (!selective || !selfReview) return false;
  const selectiveGreen = isNonNegativeNumber(selective.gate_green_le_3_rate)
    ? selective.gate_green_le_3_rate
    : Number(selective.gate_green_le_3 === true);
  const selfReviewGreen = isNonNegativeNumber(selfReview.gate_green_le_3_rate)
    ? selfReview.gate_green_le_3_rate
    : Number(selfReview.gate_green_le_3 === true);
  const lowerIsBetter = [
    "gate_effort",
    "fix_loop_rounds",
    "repeated_failure_fingerprint_count",
    "repeated_failure_fingerprint_rate",
    "repeated_diagnosis_fingerprint_count",
    "repeated_diagnosis_fingerprint_rate",
  ];
  if (!lowerIsBetter.every((field) => (
    isNonNegativeNumber(selective[field])
    && isNonNegativeNumber(selfReview[field])
    && selective[field] <= selfReview[field]
  ))) return false;
  if (selectiveGreen < selfReviewGreen) return false;
  return selectiveGreen > selfReviewGreen
    || lowerIsBetter.some((field) => selective[field] < selfReview[field]);
}

function reproducedSelfReviewParetoImprovement(trials) {
  const complete = trials.filter((trial) => (
    trial.arms["ledger-selective"] && trial.arms["action-self-review"]
  ));
  if (complete.length < MIN_MATCHED_TRIALS) return false;
  const improved = complete.filter((trial) => selfReviewParetoImproved(
    trial.arms["ledger-selective"],
    trial.arms["action-self-review"],
  )).length;
  return improved / complete.length >= MIN_REPRODUCED_FRACTION;
}

function reproducedComparison(trials, leftValue, rightValue) {
  const complete = trials.filter((trial) => (
    isNonNegativeNumber(leftValue(trial)) && isNonNegativeNumber(rightValue(trial))
  ));
  if (complete.length < MIN_MATCHED_TRIALS) return false;
  const improved = complete.filter((trial) => leftValue(trial) < rightValue(trial)).length;
  return improved / complete.length >= MIN_REPRODUCED_FRACTION;
}

function canonicalValue(value) {
  return JSON.stringify(canonicalize(value));
}

function canonicalize(value) {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value).sort().map((key) => [key, canonicalize(value[key])]),
    );
  }
  return value;
}

function isExposureSchedule(value) {
  return Array.isArray(value) && value.every((item) => (
    item
    && typeof item === "object"
    && !Array.isArray(item)
    && Object.keys(item).sort().join("\0") === "phase\0round"
    && typeof item.phase === "string"
    && item.phase.length > 0
    && Number.isInteger(item.round)
    && item.round >= 1
    && item.round <= 3
  ));
}

function compareExposure(left, right) {
  if (left.round !== right.round) return left.round - right.round;
  if (left.phase < right.phase) return -1;
  if (left.phase > right.phase) return 1;
  return 0;
}

function nonNegativeNumber(value) {
  return isNonNegativeNumber(value) ? value : 0;
}

function unitRateOrZero(value) {
  return isUnitRate(value) ? value : 0;
}

function nullableNonNegativeNumber(value) {
  return isNonNegativeNumber(value) ? value : null;
}

function integerOrNull(value) {
  return isNonNegativeInteger(value) ? value : null;
}

function isPositiveInteger(value) {
  return Number.isInteger(value) && value > 0;
}

function isNonNegativeInteger(value) {
  return Number.isInteger(value) && value >= 0;
}

function isNonNegativeNumber(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function isUnitRate(value) {
  return isNonNegativeNumber(value) && value <= 1;
}

function decimalCostNumber(value) {
  if (typeof value !== "string" || !/^(0|[1-9]\d*)(?:\.\d{1,12})?$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

function readJsonFile(file) {
  const stat = fs.lstatSync(file);
  if (stat.isSymbolicLink() || !stat.isFile()) throw new Error(`unsafe execution ledger file: ${file}`);
  const value = JSON.parse(fs.readFileSync(file, "utf8"));
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`invalid execution ledger JSON: ${file}`);
  }
  return value;
}

function main(argv) {
  try {
    let input;
    if (argv.length === 1 && argv[0] !== "--run-dir") {
      input = {
        ...JSON.parse(fs.readFileSync(path.resolve(argv[0]), "utf8")),
        evidence_verified: false,
      };
    } else {
      const runDirs = [];
      let pricingPath = null;
      for (let index = 0; index < argv.length; index += 2) {
        const option = argv[index];
        const value = argv[index + 1];
        if (!value || !["--pricing", "--run-dir"].includes(option)) {
          console.error("usage: execution-ledger-pilot-metrics <comparison.json> | --pricing <pricing.json> --run-dir <path> [... at least 12 matched run dirs]");
          return 2;
        }
        if (option === "--pricing") {
          if (pricingPath !== null) throw new Error("pricing may be specified only once");
          pricingPath = value;
        } else {
          runDirs.push(value);
        }
      }
      if (pricingPath === null || runDirs.length === 0) {
        throw new Error("run-dir comparison requires one pinned pricing file");
      }
      input = {
        ...readExecutionLedgerRunDirs(runDirs),
        pricing: readJsonFile(path.resolve(pricingPath)),
      };
    }
    console.log(JSON.stringify(compareExecutionLedgerPilot(input), null, 2));
    return 0;
  } catch (error) {
    console.error(`execution-ledger-pilot-metrics: ${error.message}`);
    return 2;
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  process.exitCode = main(process.argv.slice(2));
}
