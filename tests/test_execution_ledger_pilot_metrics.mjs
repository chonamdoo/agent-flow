import assert from "node:assert/strict";
import { createHash, createPrivateKey, generateKeyPairSync, sign } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  compareExecutionLedgerPilot,
  PILOT_MODES,
  readExecutionLedgerRunDirs,
} from "../scripts/execution-ledger-pilot-metrics.mjs";
import {
  captureExecutionState,
  initializeExecutionStateLedger,
  observeExecutionStateInjection,
  recordExecutionStateUsage,
} from "../lib/execution-state-ledger.mjs";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const FIXTURE = path.join(ROOT, "tests", "fixtures", "execution-ledger", "pilot-comparison.json");
const SCRIPT = path.join(ROOT, "scripts", "execution-ledger-pilot-metrics.mjs");
const GENERATED_AT = "2026-07-11T00:00:00.000Z";
const PRICING = Object.freeze({
  currency: "USD",
  input_per_million: 2,
  output_per_million: 8,
  snapshot_id: "pilot-price-v1",
});
const PINNED_PRICING = Object.freeze({
  ...PRICING,
  input_per_million: "2",
  output_per_million: "8",
});
const ATTESTATION_KEY_PAIR = generateKeyPairSync("rsa", { modulusLength: 1024 });
const ATTESTATION_PUBLIC_KEY = ATTESTATION_KEY_PAIR.publicKey.export({ format: "jwk" });
const ATTESTATION_PRIVATE_KEY = ATTESTATION_KEY_PAIR.privateKey.export({ format: "jwk" });
const WORKFLOW_PHASES = Object.freeze([
  { id: "implement", routes: { default: "gates" } },
  { id: "fix-loop", routes: { default: "gates" } },
  { id: "gates", routes: { green: "commit", "request-changes": "fix-loop" } },
  { id: "commit", routes: { default: "done" } },
]);
const RUN_SNAPSHOT = Object.freeze({
  runtime_id: "test",
  profile_snapshot_sha256: "d".repeat(64),
  installed_skill_plan_sha256: "e".repeat(64),
  local_skill_plan_sha256: "f".repeat(64),
  lore_snapshot_sha256: "1".repeat(64),
  prompt_controls_sha256: "2".repeat(64),
});

function fixture({ replicates = 3 } = {}) {
  const template = JSON.parse(fs.readFileSync(FIXTURE, "utf8"));
  const runs = [];
  for (let trial = 1; trial <= replicates; trial += 1) {
    for (const source of template.runs) {
      const run = structuredClone(source);
      run.run_id = `${source.run_id}-${trial}`;
      run.controls.experiment_id = `experiment-${trial}`;
      Object.assign(run.controls, {
        task_sha256: "3".repeat(64),
        workflow_sha256: "4".repeat(64),
        tool_permissions_sha256: "a".repeat(64),
        system_prompt_sha256: "b".repeat(64),
        caps_sha256: "c".repeat(64),
        runner_control_snapshot_sha256: "5".repeat(64),
        execution_controls_sha256: "7".repeat(64),
        exposure_policy_sha256: "6".repeat(64),
        per_exposure_token_cap: 180,
        provider_retry_policy_sha256: "8".repeat(64),
        provider_max_retries: 2,
        pricing_snapshot_sha256: createHash("sha256")
          .update(JSON.stringify(Object.fromEntries(Object.entries(PINNED_PRICING).sort())))
          .digest("hex"),
        provider_attestation_key_id: "fixture-provider-key-1",
        provider_attestation_public_key_sha256: "9".repeat(64),
      });
      run.pricing_snapshot = PINNED_PRICING;
      run.injected_event_count = run.injected_bytes > 0 ? 1 : 0;
      run.exposure_schedule = run.injected_event_count > 0
        ? [{ phase: "fix-loop", round: 1 }]
        : [];
      run.actual_additional_token_scope = "condition-total";
      run.actual_control_coverage = true;
      run.actual_control_evidence_status = "verified-control-receipt";
      run.runner_control_snapshot_sha256 = run.controls.runner_control_snapshot_sha256;
      run.exposure_policy_sha256 = run.controls.exposure_policy_sha256;
      run.per_exposure_token_cap = run.controls.per_exposure_token_cap;
      run.exposure_budget_compliant = true;
      run.max_injected_token_proxy = Math.ceil(run.injected_bytes / 4);
      for (const event of run.usage_events) {
        event.run_id = run.run_id;
        event.event_id = `${event.event_id}-${trial}`;
        event.additional_token_scope = "condition-total";
        event.evidence_status = "verified-provider-receipt";
        event.control_evidence_status = "verified-control-receipt";
        event.control_receipt_id = `control-${event.event_id}`;
        event.runner_control_snapshot_sha256 = run.controls.runner_control_snapshot_sha256;
        event.exposure_policy_sha256 = run.controls.exposure_policy_sha256;
        event.per_exposure_token_cap = run.controls.per_exposure_token_cap;
        event.tool_permissions_sha256 = run.controls.tool_permissions_sha256;
        event.system_prompt_sha256 = run.controls.system_prompt_sha256;
        event.caps_sha256 = run.controls.caps_sha256;
        event.provider_retry_policy_sha256 = run.controls.provider_retry_policy_sha256;
        event.provider_max_retries = run.controls.provider_max_retries;
        event.pricing_snapshot_sha256 = run.controls.pricing_snapshot_sha256;
        event.provider_attestation_key_id = run.controls.provider_attestation_key_id;
        event.provider_attestation_public_key_sha256 = run.controls.provider_attestation_public_key_sha256;
        event.execution_controls_sha256 = run.controls.execution_controls_sha256;
        event.usage_provenance = {
          source_kind: "provider-usage-receipt",
          run_id: run.run_id,
          original_path: `artifacts/${event.event_id}.json`,
          archive_path: `artifacts/execution-ledger/sources/${"a".repeat(64)}.json`,
          sha256: "a".repeat(64),
          expected_sha256: "a".repeat(64),
          control_receipt_id: event.control_receipt_id,
          runner_control_snapshot_sha256: event.runner_control_snapshot_sha256,
          attestation_algorithm: "RS256",
          attestation_key_id: event.provider_attestation_key_id,
          attestation_public_key_sha256: event.provider_attestation_public_key_sha256,
          signed_payload_sha256: "b".repeat(64),
          provider: "fixture-provider",
          request_id: `request-${event.event_id}`,
          receipt_id: null,
          extractor_version: "execution-ledger-v1",
        };
        event.id = createHash("sha256")
          .update(JSON.stringify({ event_id: event.event_id, run_id: run.run_id }))
          .digest("hex");
      }
      runs.push(run);
    }
  }
  return { ...template, evidence_verified: true, runs };
}

test("unverified programmatic comparisons remain summary-only", () => {
  const payload = fixture();
  delete payload.evidence_verified;

  const result = compareExecutionLedgerPilot(payload);

  assert.equal(result.evidence_verified, false);
  assert.equal(result.summary_only, true);
  assert.equal(result.evidence_status, "insufficient");
  assert.equal(result.comparison_valid, false);
  assert.equal(result.adoption_decision, "reject");
  assert.equal(result.best_mode, null);
  assert.match(result.reasons.join("\n"), /evidence is not verified/);
});

test("caller-provided evidence_verified cannot forge authoritative adoption", () => {
  const payload = fixture();
  payload.evidence_verified = true;

  const result = compareExecutionLedgerPilot(payload);

  assert.equal(result.evidence_verified, false);
  assert.equal(result.summary_only, true);
  assert.equal(result.comparison_valid, false);
  assert.equal(result.adoption_decision, "reject");
  assert.equal(result.candidate_adoption_decision, "accept");
});

test("an empty run-dir set is never authoritative evidence", () => {
  const loaded = readExecutionLedgerRunDirs([]);
  assert.equal(loaded.evidence_verified, false);
  assert.deepEqual(loaded.runs, []);
});

test("repeated matched four-arm pilot comparison reports every required metric", () => {
  const result = compareExecutionLedgerPilot(fixture());

  assert.equal(result.comparison_valid, false);
  assert.equal(result.evidence_status, "insufficient");
  assert.equal(result.adoption_decision, "reject");
  assert.equal(result.best_mode, null);
  assert.equal(result.candidate_adoption_decision, "accept");
  assert.equal(result.candidate_best_mode, "ledger-selective");
  assert.equal(result.action_self_review_budget_matched, true);
  assert.deepEqual(Object.keys(result.arms), PILOT_MODES);
  const selective = result.arms["ledger-selective"];
  assert.equal(selective.gate_green_le_3, true);
  assert.equal(selective.gate_green_le_3_rate, 1);
  assert.equal(selective.run_count, 3);
  assert.equal(selective.repeated_command_fingerprint_count, 0);
  assert.equal(selective.repeated_command_fingerprint_rate, 0);
  assert.equal(selective.repeated_failure_fingerprint_count, 0);
  assert.equal(selective.repeated_failure_fingerprint_rate, 0);
  assert.equal(selective.repeated_diagnosis_fingerprint_count, 0);
  assert.equal(selective.repeated_diagnosis_fingerprint_rate, 0);
  assert.equal(selective.repeated_finding_fingerprint_count, 0);
  assert.equal(selective.repeated_finding_fingerprint_rate, 0);
  assert.equal(selective.fix_loop_rounds, 1);
  assert.equal(selective.actual_input_tokens, 1025);
  assert.equal(selective.actual_output_tokens, 225);
  assert.equal(selective.actual_additional_tokens, 25);
  assert.equal(selective.actual_usage_event_ids.length, 3);
  assert.equal(selective.prompt_token_proxy, 1025);
  assert.equal(selective.injected_token_proxy, 25);
  assert.equal(selective.latency_ms, 8000);
  assert.equal(selective.estimated_cost, 0.00385);
  assert.equal(selective.stale_reminder_count, 0);
  assert.equal(selective.stale_reminder_rate, 0);
  assert.equal(selective.unsupported_reminder_count, 0);
  assert.equal(selective.unsupported_reminder_rate, 0);
  assert.equal(selective.canonical_routing_violations, 0);
});

test("missing actual provider usage is insufficient evidence", () => {
  const payload = fixture();
  const selective = payload.runs.find((run) => run.mode === "ledger-selective");
  selective.actual_usage_coverage = false;
  selective.actual_input_tokens = null;

  const result = compareExecutionLedgerPilot(payload);

  assert.equal(result.comparison_valid, false);
  assert.equal(result.evidence_status, "insufficient");
  assert.equal(result.adoption_decision, "reject");
  assert.match(result.reasons.join("\n"), /actual provider usage is incomplete/);
});

test("one matched quartet cannot establish reproducibility", () => {
  const result = compareExecutionLedgerPilot(fixture({ replicates: 1 }));

  assert.equal(result.comparison_valid, false);
  assert.equal(result.adoption_decision, "reject");
  assert.match(result.reasons.join("\n"), /at least 3 matched experiments/);
});

test("a selective arm that never reaches green within three attempts is not adopted", () => {
  const payload = fixture();
  for (const run of payload.runs) {
    run.gate_green_le_3 = false;
    run.gate_green_attempt = 3;
  }

  const result = compareExecutionLedgerPilot(payload);

  assert.equal(result.adoption_decision, "reject");
  assert.equal(result.arms["ledger-selective"].gate_green_le_3_rate, 0);
  assert.match(result.reasons.join("\n"), /gate_green_le_3 rate is insufficient/);
});

test("terminal runs with no gate attempt are insufficient evidence", () => {
  const payload = fixture();
  for (const run of payload.runs) {
    run.gate_green_le_3 = false;
    run.gate_green_attempt = null;
  }

  const result = compareExecutionLedgerPilot(payload);

  assert.equal(result.comparison_valid, false);
  assert.equal(result.evidence_status, "insufficient");
  assert.equal(result.adoption_decision, "reject");
  assert.equal(result.best_mode, null);
  assert.equal(result.arms["ledger-selective"].gate_green_le_3_rate, 0);
  assert.equal(result.arms["ledger-selective"].gate_green_attempt, null);
  assert.match(result.reasons.join("\n"), /conflicts with gate_green_attempt/);
});

test("run-total usage events are idempotent and arithmetic is enforced", () => {
  const replay = fixture();
  replay.runs[0].usage_events.push(structuredClone(replay.runs[0].usage_events[0]));
  assert.equal(compareExecutionLedgerPilot(replay).candidate_adoption_decision, "accept");

  const conflict = fixture();
  const conflictingEvent = structuredClone(conflict.runs[0].usage_events[0]);
  conflictingEvent.input_tokens += 1;
  conflict.runs[0].usage_events.push(conflictingEvent);
  const conflictResult = compareExecutionLedgerPilot(conflict);
  assert.equal(conflictResult.adoption_decision, "reject");
  assert.match(conflictResult.reasons.join("\n"), /conflicting usage event id/);

  const invalidSubset = fixture();
  invalidSubset.runs[1].usage_events[0].additional_tokens = 2000;
  const subsetResult = compareExecutionLedgerPilot(invalidSubset);
  assert.equal(subsetResult.adoption_decision, "reject");
  assert.match(subsetResult.reasons.join("\n"), /invalid usage event/);

  const invalidTotal = fixture();
  invalidTotal.runs[2].actual_total_tokens += 1;
  const totalResult = compareExecutionLedgerPilot(invalidTotal);
  assert.equal(totalResult.adoption_decision, "reject");
  assert.match(totalResult.reasons.join("\n"), /does not match aggregated actual metrics/);

  const numericCost = fixture();
  numericCost.runs[2].usage_events[0].estimated_cost = 0.00385;
  const costResult = compareExecutionLedgerPilot(numericCost);
  assert.equal(costResult.adoption_decision, "reject");
  assert.match(costResult.reasons.join("\n"), /invalid usage event/);
});

test("missing or mismatched frozen controls are insufficient evidence", () => {
  const missing = fixture();
  delete missing.runs[1].controls.system_prompt_sha256;
  const missingResult = compareExecutionLedgerPilot(missing);
  assert.equal(missingResult.adoption_decision, "reject");
  assert.match(missingResult.reasons.join("\n"), /missing control system_prompt_sha256/);

  const mismatched = fixture();
  mismatched.runs[2].controls.model_id = "different-model";
  const mismatchResult = compareExecutionLedgerPilot(mismatched);
  assert.equal(mismatchResult.adoption_decision, "reject");
  assert.match(mismatchResult.reasons.join("\n"), /control mismatch model_id/);

  const retryMismatch = fixture();
  retryMismatch.runs[2].controls.provider_max_retries += 1;
  const retryResult = compareExecutionLedgerPilot(retryMismatch);
  assert.equal(retryResult.adoption_decision, "reject");
  assert.match(retryResult.reasons.join("\n"), /control mismatch provider_max_retries/);
});

test("placeholder control hashes cannot qualify as frozen execution evidence", () => {
  const payload = fixture();
  const selective = payload.runs.find((run) => run.mode === "ledger-selective");
  selective.controls.tool_permissions_sha256 = "tool-policy-v1";
  selective.usage_events[0].tool_permissions_sha256 = "tool-policy-v1";

  const result = compareExecutionLedgerPilot(payload);

  assert.equal(result.comparison_valid, false);
  assert.equal(result.adoption_decision, "reject");
  assert.match(result.reasons.join("\n"), /missing control tool_permissions_sha256/);
});

test("any stale reminder, unsupported reminder, or canonical routing violation rejects adoption", () => {
  for (const field of [
    "stale_reminder_count",
    "stale_reminder_rate",
    "unsupported_reminder_count",
    "unsupported_reminder_rate",
    "canonical_routing_violations",
  ]) {
    const payload = fixture();
    payload.runs[1][field] = 1;

    const result = compareExecutionLedgerPilot(payload);

    assert.equal(result.evidence_status, "insufficient", field);
    assert.equal(result.adoption_decision, "reject", field);
    assert.equal(result.safety_violations, 1, field);
  }
});

test("malformed or excluded sources are quality metrics, not safety violations", () => {
  const payload = fixture();
  payload.runs[1].malformed_source_count = 2;
  payload.runs[1].excluded_source_count = 3;

  const result = compareExecutionLedgerPilot(payload);

  assert.equal(result.candidate_adoption_decision, "accept");
  assert.equal(result.safety_violations, 0);
  assert.equal(result.arms["ledger-always"].malformed_source_count, 2);
  assert.equal(result.arms["ledger-always"].excluded_source_count, 3);
});

test("missing production source-quality counters make evidence incomplete", () => {
  for (const field of ["malformed_source_count", "excluded_source_count"]) {
    const payload = fixture();
    delete payload.runs[1][field];
    const result = compareExecutionLedgerPilot(payload);
    assert.equal(result.comparison_valid, false, field);
    assert.equal(result.arms["ledger-always"][field], null, field);
    assert.match(result.reasons.join("\n"), /required measurements are incomplete/, field);
  }
});

test("summary-only usage cannot become provider evidence", () => {
  const payload = fixture();
  payload.runs[0].usage_events[0].evidence_status = "summary-only";
  payload.runs[0].usage_events[0].usage_provenance = null;
  const result = compareExecutionLedgerPilot(payload);
  assert.equal(result.comparison_valid, false);
  assert.equal(result.adoption_decision, "reject");
  assert.match(result.reasons.join("\n"), /invalid usage event/);
});

test("manual-only committed run dirs are not marked evidence_verified", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "ledger-manual-summary-"));
  try {
    const runDir = writeCommittedRun(root, 1, "artifacts-only", {
      actionScheduleMismatch: false,
      manualUsage: true,
    });
    const loaded = readExecutionLedgerRunDirs([runDir]);
    assert.equal(loaded.evidence_verified, false);
    assert.match(loaded.evidence_reasons.join("\n"), /summary-only/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("only ledger-selective can satisfy the adoption policy", () => {
  const payload = fixture();
  for (const selective of payload.runs.filter((run) => run.mode === "ledger-selective")) {
    selective.repeated_failure_fingerprint_count = 2;
    selective.repeated_failure_fingerprint_rate = 0.5;
    selective.repeated_diagnosis_fingerprint_count = 2;
    selective.repeated_diagnosis_fingerprint_rate = 0.5;
    selective.fix_loop_rounds = 2;
  }

  const result = compareExecutionLedgerPilot(payload);

  assert.equal(result.adoption_decision, "reject");
  assert.equal(result.best_mode, null);
  assert.match(result.reasons.join("\n"), /ledger-selective did not reproducibly reduce baseline/);
});

test("counts cannot hide a repeated-command rate regression", () => {
  const payload = fixture();
  for (const selective of payload.runs.filter((run) => run.mode === "ledger-selective")) {
    selective.repeated_command_fingerprint_rate = 0.75;
  }

  const result = compareExecutionLedgerPilot(payload);

  assert.equal(result.adoption_decision, "reject");
  assert.match(result.reasons.join("\n"), /without command or diagnosis regression/);
});

test("zero-floor command and diagnosis metrics do not hide a real failure/fix-loop improvement", () => {
  const payload = fixture();
  for (const run of payload.runs.filter((candidate) => (
    ["artifacts-only", "ledger-selective"].includes(candidate.mode)
  ))) {
    run.repeated_command_fingerprint_count = 0;
    run.repeated_command_fingerprint_rate = 0;
    run.repeated_diagnosis_fingerprint_count = 0;
    run.repeated_diagnosis_fingerprint_rate = 0;
  }

  const result = compareExecutionLedgerPilot(payload);

  assert.equal(result.candidate_adoption_decision, "accept");
});

test("paired improvement must reproduce in at least two of three trials", () => {
  const twoOfThree = fixture();
  const firstSelective = twoOfThree.runs.find((run) => (
    run.mode === "ledger-selective" && run.controls.experiment_id === "experiment-1"
  ));
  firstSelective.repeated_failure_fingerprint_count = 2;
  firstSelective.repeated_failure_fingerprint_rate = 0.5;
  firstSelective.fix_loop_rounds = 2;
  assert.equal(compareExecutionLedgerPilot(twoOfThree).candidate_adoption_decision, "accept");

  const oneOfThree = fixture();
  for (const experimentId of ["experiment-1", "experiment-2"]) {
    const selective = oneOfThree.runs.find((run) => (
      run.mode === "ledger-selective" && run.controls.experiment_id === experimentId
    ));
    selective.repeated_failure_fingerprint_count = 2;
    selective.repeated_failure_fingerprint_rate = 0.5;
    selective.fix_loop_rounds = 2;
  }
  assert.equal(compareExecutionLedgerPilot(oneOfThree).adoption_decision, "reject");
});

test("two-of-three gate success is measurable when matched controls do not outperform it", () => {
  const payload = fixture();
  for (const mode of ["artifacts-only", "ledger-selective", "action-self-review"]) {
    const failed = payload.runs.find((run) => (
      run.mode === mode && run.controls.experiment_id === "experiment-1"
    ));
    failed.gate_green_le_3 = false;
    failed.gate_green_attempt = 3;
  }

  const result = compareExecutionLedgerPilot(payload);

  assert.equal(result.arms["ledger-selective"].gate_green_le_3_rate, 2 / 3);
  assert.equal(result.candidate_adoption_decision, "accept");
});

test("one efficiency regression is tolerated only when the mean and two trials still improve", () => {
  const twoOfThree = fixture();
  const first = twoOfThree.runs.find((run) => (
    run.mode === "ledger-selective" && run.controls.experiment_id === "experiment-1"
  ));
  first.latency_ms = 10000;
  first.usage_events[0].latency_ms = 10000;
  assert.equal(compareExecutionLedgerPilot(twoOfThree).candidate_adoption_decision, "accept");

  const oneOfThree = fixture();
  for (const experimentId of ["experiment-1", "experiment-2"]) {
    const selective = oneOfThree.runs.find((run) => (
      run.mode === "ledger-selective" && run.controls.experiment_id === experimentId
    ));
    selective.latency_ms = 10000;
    selective.usage_events[0].latency_ms = 10000;
  }
  assert.equal(compareExecutionLedgerPilot(oneOfThree).adoption_decision, "reject");
});

test("every matched experiment requires exactly one run per arm", () => {
  const missing = fixture();
  missing.runs = missing.runs.filter((run) => !(
    run.mode === "ledger-always" && run.controls.experiment_id === "experiment-2"
  ));
  const result = compareExecutionLedgerPilot(missing);

  assert.equal(result.comparison_valid, false);
  assert.match(result.reasons.join("\n"), /experiment experiment-2 missing modes/);
});

test("selective cannot claim a clear self-review win with a worse gate-green rate", () => {
  const payload = fixture();
  payload.runs.find((run) => (
    run.mode === "ledger-selective" && run.controls.experiment_id === "experiment-1"
  )).gate_green_le_3 = false;

  const result = compareExecutionLedgerPilot(payload);

  assert.equal(result.adoption_decision, "reject");
  assert.match(result.reasons.join("\n"), /did not reproducibly outperform action-self-review/);
});

test("run ids must be globally unique across matched trials", () => {
  const payload = fixture();
  payload.runs[1].run_id = payload.runs[0].run_id;
  payload.runs[1].usage_events[0].run_id = payload.runs[1].run_id;
  payload.runs[1].usage_events[0].id = createHash("sha256")
    .update(JSON.stringify({
      event_id: payload.runs[1].usage_events[0].event_id,
      run_id: payload.runs[1].run_id,
    }))
    .digest("hex");

  const result = compareExecutionLedgerPilot(payload);

  assert.equal(result.comparison_valid, false);
  assert.match(result.reasons.join("\n"), /duplicate run_ids/);
});

test("mismatched selective and self-review precommitted token caps are insufficient evidence", () => {
  const payload = fixture();
  const selfReview = payload.runs.find((run) => run.mode === "action-self-review");
  selfReview.controls.per_exposure_token_cap = 181;
  selfReview.per_exposure_token_cap = 181;
  selfReview.usage_events[0].per_exposure_token_cap = 181;

  const result = compareExecutionLedgerPilot(payload);

  assert.equal(result.comparison_valid, false);
  assert.equal(result.adoption_decision, "reject");
  assert.equal(result.action_self_review_budget_matched, false);
  assert.match(result.reasons.join("\n"), /precommitted exposure policies or per-exposure budgets do not match/);
});

test("actual condition totals remain outcomes when precommitted per-exposure budgets match", () => {
  const payload = fixture();
  for (const run of payload.runs) {
    if (run.mode === "ledger-selective") {
      run.actual_additional_tokens = 24;
      run.usage_events[0].additional_tokens = 24;
    } else if (run.mode === "action-self-review") {
      run.actual_additional_tokens = 40;
      run.usage_events[0].additional_tokens = 40;
    }
  }

  const result = compareExecutionLedgerPilot(payload);

  assert.equal(result.comparison_valid, false);
  assert.equal(result.adoption_decision, "reject");
  assert.equal(result.candidate_adoption_decision, "accept");
  assert.equal(result.arms["ledger-selective"].actual_additional_tokens, 24);
  assert.equal(result.arms["action-self-review"].actual_additional_tokens, 40);
  assert.equal(result.arms["ledger-selective"].injected_token_proxy, 25);
});

test("condition-total additional token coverage is mandatory", () => {
  const payload = fixture();
  const selective = payload.runs.find((run) => run.mode === "ledger-selective");
  delete selective.actual_additional_token_scope;
  delete selective.usage_events[0].additional_token_scope;

  const result = compareExecutionLedgerPilot(payload);

  assert.equal(result.comparison_valid, false);
  assert.equal(result.adoption_decision, "reject");
  assert.match(result.reasons.join("\n"), /condition-total coverage/);
});

test("repeat improvements cannot offset worse gate effort or fix-loop count", () => {
  const payload = fixture();
  for (const run of payload.runs) {
    if (run.mode === "artifacts-only") run.fix_loop_rounds = 3;
    if (run.mode === "ledger-selective") {
      run.gate_green_attempt = 3;
      run.fix_loop_rounds = 2;
    }
    if (run.mode === "action-self-review") {
      run.gate_green_attempt = 2;
      run.fix_loop_rounds = 1;
    }
  }

  const result = compareExecutionLedgerPilot(payload);

  assert.equal(result.adoption_decision, "reject");
  assert.match(result.reasons.join("\n"), /did not reproducibly outperform action-self-review/);
});

test("per-run cost claims cannot override the shared pinned pricing snapshot", () => {
  const payload = fixture();
  const selective = payload.runs.find((run) => run.mode === "ledger-selective");
  selective.estimated_cost = "0.000001";
  selective.usage_events[0].estimated_cost = "0.000001";

  const result = compareExecutionLedgerPilot(payload);

  assert.equal(result.comparison_valid, false);
  assert.equal(result.adoption_decision, "reject");
  assert.match(result.reasons.join("\n"), /does not match pinned pricing/);
});

test("post-run pricing input cannot replace the run-pinned pricing snapshot", () => {
  const payload = fixture();
  payload.pricing = { ...PRICING, input_per_million: 0 };

  const result = compareExecutionLedgerPilot(payload);

  assert.equal(result.comparison_valid, false);
  assert.equal(result.adoption_decision, "reject");
  assert.match(result.reasons.join("\n"), /supplied pricing does not match/);
});

test("the comparison JSON CLI is always summary-only", (t) => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-ledger-metrics-"));
  t.after(() => fs.rmSync(temp, { recursive: true, force: true }));
  const input = path.join(temp, "comparison.json");
  fs.writeFileSync(input, `${JSON.stringify(fixture())}\n`, "utf8");

  const result = spawnSync(process.execPath, [SCRIPT, input], { encoding: "utf8" });

  assert.equal(result.status, 0, result.stderr);
  const output = JSON.parse(result.stdout);
  assert.equal(output.evidence_verified, false);
  assert.equal(output.summary_only, true);
  assert.equal(output.evidence_status, "insufficient");
  assert.equal(output.comparison_valid, false);
  assert.equal(output.adoption_decision, "reject");
  assert.equal(output.best_mode, null);
});

test("the CLI compares repeated matched execution ledger run directories directly", (t) => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-ledger-run-dirs-"));
  t.after(() => fs.rmSync(temp, { recursive: true, force: true }));
  const runDirs = writeCommittedRunDirs(temp);
  const pricing = path.join(temp, "pricing.json");
  fs.writeFileSync(pricing, `${JSON.stringify(PRICING)}\n`, "utf8");

  const loaded = readExecutionLedgerRunDirs(runDirs);
  assert.equal(loaded.runs.length, PILOT_MODES.length * 3);
  assert.equal(loaded.evidence_verified, true, loaded.evidence_reasons.join("\n"));
  const result = spawnSync(
    process.execPath,
    [SCRIPT, "--pricing", pricing, ...runDirs.flatMap((runDir) => ["--run-dir", runDir])],
    { encoding: "utf8" },
  );

  assert.equal(result.status, 0, result.stderr);
  const output = JSON.parse(result.stdout);
  assert.equal(output.evidence_verified, true);
  assert.equal(output.summary_only, false);
  assert.equal(output.evidence_status, "verified");
  assert.equal(output.comparison_valid, true);
  assert.equal(output.adoption_decision, "accept");
  assert.equal(output.best_mode, "ledger-selective");

  const forged = { ...loaded, pricing: PRICING, runs: structuredClone(loaded.runs) };
  forged.runs[0].run_id = "forged-run";
  const forgedResult = compareExecutionLedgerPilot(forged);
  assert.equal(forgedResult.evidence_verified, false);
  assert.equal(forgedResult.comparison_valid, false);
  assert.equal(forgedResult.adoption_decision, "reject");
});

test("committed run-dir evidence rejects tamper, deletion, reordering, and missing completion", (t) => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-ledger-tamper-"));
  t.after(() => fs.rmSync(temp, { recursive: true, force: true }));
  const runDirs = writeCommittedRunDirs(path.join(temp, "base"));
  const original = runDirs.find((runDir) => runDir.endsWith(path.join("experiment-1", "ledger-always")));
  assert.ok(original);
  const cases = [
    ["metrics-rewrite", (runDir) => {
      const file = ledgerFile(runDir, "metrics.json");
      const metrics = readJson(file);
      metrics.fix_loop_rounds += 1;
      writeJson(file, metrics);
    }],
    ["capture-tamper", (runDir) => {
      const file = ledgerFile(runDir, "captures.jsonl");
      const captures = readJsonlRecords(file);
      captures[0].measurement.fix_loop_rounds += 1;
      writeJsonlRecords(file, captures);
    }],
    ["injection-delete", (runDir) => {
      const file = ledgerFile(runDir, "injections.jsonl");
      writeJsonlRecords(file, readJsonlRecords(file).slice(1));
    }],
    ["injection-reorder", (runDir) => {
      const file = ledgerFile(runDir, "injections.jsonl");
      writeJsonlRecords(file, readJsonlRecords(file).reverse());
    }],
    ["ledger-tamper", (runDir) => {
      const file = ledgerFile(runDir, "ledger.json");
      const ledger = readJson(file);
      ledger.entries.status[0].payload.diagnosis = "tampered";
      writeJson(file, ledger);
    }],
    ["source-tamper", (runDir) => {
      const capture = readJsonlRecords(ledgerFile(runDir, "captures.jsonl"))[0];
      fs.appendFileSync(path.join(runDir, capture.source_provenance[0].archive_path), "tampered", "utf8");
    }],
    ["workflow-tamper", (runDir) => {
      const file = ledgerFile(runDir, "workflow.json");
      const workflow = readJson(file);
      workflow.phases[0].instruction = "tampered";
      writeJson(file, workflow);
    }],
    ["runner-snapshot-tamper", (runDir) => {
      const file = ledgerFile(runDir, "config.json");
      const config = readJson(file);
      config.runner_control_snapshot.prompt_controls_sha256 = "9".repeat(64);
      writeJson(file, config);
    }],
    ["completion-missing", (runDir) => {
      fs.unlinkSync(ledgerFile(runDir, "completion.json"));
    }],
  ];

  for (const [label, mutate] of cases) {
    const clone = path.join(temp, "cases", label);
    fs.mkdirSync(path.dirname(clone), { recursive: true });
    fs.cpSync(original, clone, { recursive: true });
    mutate(clone);
    const candidateDirs = runDirs.map((runDir) => (runDir === original ? clone : runDir));
    const loaded = readExecutionLedgerRunDirs(candidateDirs);
    assert.equal(loaded.evidence_verified, false, label);
    assert.equal(loaded.evidence_reasons.length > 0, true, label);
    const comparison = compareExecutionLedgerPilot({ ...loaded, pricing: PRICING });
    assert.equal(comparison.summary_only, true, label);
    assert.equal(comparison.evidence_status, "invalid", label);
    assert.equal(comparison.comparison_valid, false, label);
    assert.equal(comparison.adoption_decision, "reject", label);
  }
});

test("outcome-dependent selective and self-review schedules share the precommitted budget policy", (t) => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-ledger-schedule-"));
  t.after(() => fs.rmSync(temp, { recursive: true, force: true }));
  const runDirs = writeCommittedRunDirs(temp, { actionScheduleMismatch: true });
  const loaded = readExecutionLedgerRunDirs(runDirs);

  assert.equal(loaded.evidence_verified, true, loaded.evidence_reasons.join("\n"));
  const result = compareExecutionLedgerPilot({ ...loaded, pricing: PRICING });
  assert.equal(result.action_self_review_budget_matched, true, result.reasons.join("\n"));
  assert.equal(result.comparison_valid, true, result.reasons.join("\n"));
});

function writeCommittedRunDirs(root, { actionScheduleMismatch = false } = {}) {
  const runDirs = [];
  for (let trial = 1; trial <= 3; trial += 1) {
    for (const mode of PILOT_MODES) {
      runDirs.push(writeCommittedRun(root, trial, mode, { actionScheduleMismatch }));
    }
  }
  return runDirs;
}

function writeCommittedRun(root, trial, mode, { actionScheduleMismatch, manualUsage = false }) {
  const runId = `run-${trial}-${mode}`;
  const runDir = path.join(root, `experiment-${trial}`, mode);
  fs.mkdirSync(path.join(runDir, "artifacts"), { recursive: true });
  const initialized = initializeExecutionStateLedger({
    runDir,
    runId,
    mode,
    experimentEnabled: true,
    task: "repair deterministic checkout",
    workflowId: "full-feature",
    workflowPhases: WORKFLOW_PHASES,
    baseCommit: "1".repeat(40),
    experiment: {
      experiment_id: `experiment-${trial}`,
      model_id: "model-1",
      tool_permissions_sha256: "a".repeat(64),
      system_prompt_sha256: "b".repeat(64),
      caps_sha256: "c".repeat(64),
      provider_retry_policy_sha256: "4".repeat(64),
      provider_max_retries: 2,
      pricing_snapshot: PRICING,
      provider_attestation_key_id: "test-provider-key-1",
      provider_attestation_public_key: ATTESTATION_PUBLIC_KEY,
    },
    runSnapshot: RUN_SNAPSHOT,
  });
  assert.equal(initialized.ok, true, initialized.error);

  const failureCount = mode === "artifacts-only" ? 3 : 2;
  for (let attempt = 1; attempt <= failureCount; attempt += 1) {
    const diagnosis = mode === "ledger-selective"
      ? `checkout failure ${attempt}`
      : "checkout failure";
    const command = mode === "ledger-selective" && attempt === 2
      ? ["npm", "run", "lint"]
      : ["npm", "test"];
    const artifact = writeCommittedGateArtifact(runDir, {
      passed: false,
      command,
      diagnosis,
    });
    const captured = captureExecutionState({
      runDir,
      runId,
      mode,
      experimentEnabled: true,
      phase: WORKFLOW_PHASES[2],
      artifactPath: artifact,
      projectRoot: runDir,
      round: Math.min(attempt, 3),
      fixLoopRounds: attempt,
      generatedAt: timestamp(attempt),
      workflowId: "full-feature",
      routeKey: "request-changes",
      routedTo: "fix-loop",
    });
    assert.equal(captured.ok, true, captured.error);

    const shouldObserve = mode === "ledger-always"
      || ["ledger-selective", "action-self-review"].includes(mode)
      || (mode === "artifacts-only" && attempt === 1);
    if (shouldObserve) {
      const injectionPhase = actionScheduleMismatch
        && mode === "action-self-review"
        && attempt === 2
        ? WORKFLOW_PHASES[0]
        : WORKFLOW_PHASES[1];
      const observed = observeExecutionStateInjection({
        runDir,
        runId,
        mode,
        experimentEnabled: true,
        phase: injectionPhase,
        projectRoot: runDir,
        round: Math.min(attempt, 3),
        generatedAt: timestamp(10 + attempt),
        promptBytes: 400,
      });
      assert.equal(observed.ok, true, observed.error);
    }
  }

  const success = writeCommittedGateArtifact(runDir, {
    passed: true,
    command: mode === "ledger-selective" ? ["npm", "run", "lint"] : ["npm", "test"],
    diagnosis: "",
  });
  const succeeded = captureExecutionState({
    runDir,
    runId,
    mode,
    experimentEnabled: true,
    phase: WORKFLOW_PHASES[2],
    artifactPath: success,
    projectRoot: runDir,
    round: Math.min(failureCount, 3),
    fixLoopRounds: failureCount,
    generatedAt: timestamp(20),
    workflowId: "full-feature",
    routeKey: "green",
    routedTo: "commit",
  });
  assert.equal(succeeded.ok, true, succeeded.error);

  const actualAdditionalTokens = {
    "artifacts-only": 0,
    "ledger-always": 150,
    "ledger-selective": 100,
    "action-self-review": 100,
  }[mode];
  const outputTokens = {
    "artifacts-only": 300,
    "ledger-always": 260,
    "ledger-selective": 220,
    "action-self-review": 230,
  }[mode];
  const inputTokens = 1000 + actualAdditionalTokens;
  const cost = decimalCost(inputTokens, outputTokens);
  fs.writeFileSync(path.join(runDir, "manifest.json"), `${JSON.stringify({
    run_id: runId,
    workflow: "full-feature",
    phase_index: WORKFLOW_PHASES.length,
    phase: "complete",
    status: "complete",
  })}\n`, "utf8");
  const unsignedReceipt = {
    schema_version: 1,
    kind: "provider-usage-receipt",
    provider: "test-provider",
    request_id: `request-${runId}`,
    receipt_id: `receipt-${runId}`,
    event_id: "run-total",
    run_id: runId,
    generated_at: timestamp(30),
    scope: "run-total",
    phase_id: null,
    round: null,
    model_id: "model-1",
    control_receipt: {
      schema_version: 1,
      kind: "execution-control-receipt",
      control_receipt_id: `control-${runId}`,
      run_id: runId,
      experiment_id: `experiment-${trial}`,
      model_id: "model-1",
      runner_control_snapshot_sha256: initialized.config.runner_control_snapshot_sha256,
      tool_permissions_sha256: "a".repeat(64),
      system_prompt_sha256: "b".repeat(64),
      caps_sha256: "c".repeat(64),
      exposure_policy_sha256: initialized.config.exposure_policy_sha256,
      per_exposure_token_cap: initialized.config.per_exposure_token_cap,
      provider_retry_policy_sha256: initialized.config.provider_retry_policy_sha256,
      provider_max_retries: initialized.config.provider_max_retries,
      pricing_snapshot_sha256: initialized.config.pricing_snapshot_sha256,
      provider_attestation_key_id: initialized.config.provider_attestation_key_id,
      provider_attestation_public_key_sha256: initialized.config.provider_attestation_public_key_sha256,
      execution_controls_sha256: initialized.config.execution_controls_sha256,
    },
    usage: {
      input_tokens: inputTokens,
      output_tokens: outputTokens,
      additional_tokens: actualAdditionalTokens,
      additional_token_scope: "condition-total",
      latency_ms: {
        "artifacts-only": 10000,
        "ledger-always": 9000,
        "ledger-selective": 8000,
        "action-self-review": 8500,
      }[mode],
      estimated_cost_usd: cost,
    },
  };
  const signedBytes = Buffer.from(stableJson(unsignedReceipt), "utf8");
  const receipt = {
    ...unsignedReceipt,
    attestation: {
      schema_version: 1,
      kind: "provider-usage-attestation",
      algorithm: "RS256",
      key_id: initialized.config.provider_attestation_key_id,
      signed_payload_sha256: createHash("sha256").update(signedBytes).digest("hex"),
      signature_base64url: sign(
        "RSA-SHA256",
        signedBytes,
        createPrivateKey({ key: ATTESTATION_PRIVATE_KEY, format: "jwk" }),
      ).toString("base64url"),
    },
  };
  const receiptPath = path.join(runDir, "artifacts", "provider-usage-receipt.json");
  const receiptBytes = Buffer.from(`${JSON.stringify(receipt)}\n`, "utf8");
  fs.writeFileSync(receiptPath, receiptBytes);
  const recorded = recordExecutionStateUsage(manualUsage ? {
    runDir,
    runId,
    mode,
    experimentEnabled: true,
    eventId: "run-total",
    generatedAt: timestamp(30),
    scope: "run-total",
    phaseId: null,
    round: null,
    inputTokens,
    outputTokens,
    additionalTokens: actualAdditionalTokens,
    latencyMs: receipt.usage.latency_ms,
    estimatedCostUsd: cost,
    modelId: "model-1",
  } : {
    runDir,
    runId,
    mode,
    experimentEnabled: true,
    receiptPath,
    receiptSha256: createHash("sha256").update(receiptBytes).digest("hex"),
  });
  assert.equal(recorded.ok, true, recorded.error);
  return runDir;
}

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function writeCommittedGateArtifact(runDir, { passed, command, diagnosis }) {
  const artifact = path.join(runDir, "artifacts", "gate-results.json");
  fs.writeFileSync(artifact, `${JSON.stringify({
    passed,
    results: [{
      gate_id: command.join("-"),
      argv: command,
      required: true,
      passed,
      exit_code: passed ? 0 : 1,
      stdout: passed ? "ok" : "",
      stderr: passed ? "" : diagnosis,
    }],
  })}\n`, "utf8");
  return artifact;
}

function timestamp(offset) {
  return new Date(Date.parse(GENERATED_AT) + (offset * 1000)).toISOString();
}

function decimalCost(inputTokens, outputTokens) {
  return (((inputTokens * PRICING.input_per_million) + (outputTokens * PRICING.output_per_million)) / 1_000_000)
    .toFixed(12)
    .replace(/0+$/, "")
    .replace(/\.$/, "");
}

function ledgerFile(runDir, name) {
  return path.join(runDir, "artifacts", "execution-ledger", name);
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

function writeJson(file, value) {
  fs.writeFileSync(file, `${JSON.stringify(value)}\n`, "utf8");
}

function readJsonlRecords(file) {
  return fs.readFileSync(file, "utf8")
    .split(/\r?\n/)
    .filter(Boolean)
    .map((line) => JSON.parse(line));
}

function writeJsonlRecords(file, records) {
  const content = records.length > 0
    ? `${records.map((record) => JSON.stringify(record)).join("\n")}\n`
    : "";
  fs.writeFileSync(file, content, "utf8");
}
