import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { createHash, createPrivateKey, sign } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  captureExecutionState,
  executionStatePromptBlock,
  initializeExecutionStateLedger,
  observeExecutionStateInjection,
  recordExecutionStateUsage,
  resolveLedgerMode,
  validateExecutionStateLedger,
} from "../lib/execution-state-ledger.mjs";

const KIT_ROOT = fileURLToPath(new URL("..", import.meta.url));
const CLI = path.join(KIT_ROOT, "bin", "agent-flow-kit.mjs");
const FIXED_TIME = "2026-07-11T00:00:00.000Z";
const ATTESTATION_PUBLIC_KEY = {
  kty: "RSA",
  n: "vqho9mBYkUbz5uC4JATIe1pZpDm7SynIfIVWzMRc1CZzHlWOkOa7bhYSMYIP1jo7oAWu9d2wX5Fa4wrgMtPT3afMCmkvfmYfnyRC3897pF5a0n09JxNaJBpxtaBjtExtXGzw50w4ZTeHfHts5hnt5TqQ0xtZ2POpRzhPTdRPatc",
  e: "AQAB",
};
const ATTESTATION_PRIVATE_KEY = {
  ...ATTESTATION_PUBLIC_KEY,
  d: "dqW3LBupAj91aShPb5rKaHlBb8G9nHjUGymfaq6IVj3XRflYTzRHT6rMh6K42EhE8sCWsMrVB6QdO015WCgan7H7TS3Dz5jrnSZUKmMuZhz_nRqkgMdoru3uq5ODmwuUMzOCQgtg9mCyJ-d-0MgmluJyZtikeALudROuwPxiCgE",
  p: "76DkhGBt8h-Uh8kP8edn8cfmt7vajgfhO4bY73CrVTBq_-kUDYOsMTBinWB83UwWfUit39vAWBdmv55WZ-ZZsQ",
  q: "y68FGKAUMIpMLktOw9A8WkJpcondRK84ldF-X0271udiD8u7dR734m6_uD55dojoF4LwoyrVksqru0DbCCknBw",
  dp: "aBnWjKezu-b6SM8RTT8BiikU0ycZ-G_16j1XyxWAaT7ijRB9tK1KRghGHyaGuEDQ2FaVqtW1xs9LxN0Nno-U0Q",
  dq: "EAyebi5O6PQ8xHkSn8NMvh_1hxzt3negEc4MEx5g6rIYu_3lq3jhN2pamP3zPC_VeeTLaU_6vDJUDdEycRYtCQ",
  qi: "qC7c5LcGB-zDh5kIvyY0e-_cDHr2ExsgFvcDm_5WtLeJE-TKTpUd4QW1xf7OikbVw9jXfID1z919iiZAta8ThQ",
};
const PRICING = {
  currency: "USD",
  input_per_million: 2,
  output_per_million: 8,
  snapshot_id: "pilot-price-v1",
};
const EXPERIMENT = {
  experiment_id: "ledger-pilot",
  model_id: "model-v1",
  tool_permissions_sha256: "a".repeat(64),
  system_prompt_sha256: "b".repeat(64),
  caps_sha256: "c".repeat(64),
  provider_retry_policy_sha256: "4".repeat(64),
  provider_max_retries: 2,
  pricing_snapshot: PRICING,
  provider_attestation_key_id: "test-provider-key-1",
  provider_attestation_public_key: ATTESTATION_PUBLIC_KEY,
};
const RUN_SNAPSHOT = {
  runtime_id: "test",
  profile_snapshot_sha256: "d".repeat(64),
  installed_skill_plan_sha256: "e".repeat(64),
  local_skill_plan_sha256: "f".repeat(64),
  lore_snapshot_sha256: "1".repeat(64),
  prompt_controls_sha256: "2".repeat(64),
};
const WORKFLOW_PHASES = [
  { id: "implement" },
  { id: "gates", routes: { green: "commit", "request-changes": "fix-loop" } },
  { id: "final-review", multi_review: true, routes: { approve: "gates", "request-changes": "fix-loop" } },
  { id: "fix-loop", routes: { default: "comment-authoring" } },
  { id: "comment-authoring" },
  { id: "commit" },
];

function fixture(t, mode, {
  enabled = true,
  workflowPhases = WORKFLOW_PHASES,
  experiment = EXPERIMENT,
  runSnapshot = RUN_SNAPSHOT,
} = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-ledger-node-"));
  const runDir = path.join(root, ".agent-flow", "runs", "default", "r1");
  fs.mkdirSync(path.join(runDir, "artifacts"), { recursive: true });
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const initialized = initializeExecutionStateLedger({
    runDir,
    runId: "r1",
    mode,
    experimentEnabled: enabled,
    task: "fix checkout",
    workflowId: "default",
    workflowPhases,
    baseCommit: "1".repeat(40),
    experiment,
    runSnapshot,
  });
  assert.equal(initialized.ok, true, initialized.error);
  return { root, runDir, mode, enabled, workflowPhases, config: initialized.config };
}

function markNodeComplete(f) {
  fs.writeFileSync(path.join(f.runDir, "manifest.json"), `${JSON.stringify({
    run_id: "r1",
    workflow: "default",
    status: "complete",
    phase: "complete",
    phase_index: WORKFLOW_PHASES.length,
  })}\n`, "utf8");
}

function ledgerRoot(runDir) {
  return path.join(runDir, "artifacts", "execution-ledger");
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

function readJsonl(file) {
  if (!fs.existsSync(file)) return [];
  return fs.readFileSync(file, "utf8").split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
}

function usageReceipt(f, values, suffix = "usage") {
  const unsignedPayload = {
    schema_version: 1,
    kind: "provider-usage-receipt",
    provider: "test-provider",
    request_id: `request-${suffix}`,
    receipt_id: `receipt-${suffix}`,
    event_id: values.eventId,
    run_id: "r1",
    generated_at: values.generatedAt,
    scope: values.scope,
    phase_id: values.phaseId,
    round: values.round,
    model_id: values.modelId,
    control_receipt: {
      schema_version: 1,
      kind: "execution-control-receipt",
      control_receipt_id: `control-${suffix}`,
      run_id: "r1",
      experiment_id: EXPERIMENT.experiment_id,
      model_id: values.modelId,
      runner_control_snapshot_sha256: f.config.runner_control_snapshot_sha256,
      tool_permissions_sha256: EXPERIMENT.tool_permissions_sha256,
      system_prompt_sha256: EXPERIMENT.system_prompt_sha256,
      caps_sha256: EXPERIMENT.caps_sha256,
      provider_retry_policy_sha256: EXPERIMENT.provider_retry_policy_sha256,
      provider_max_retries: EXPERIMENT.provider_max_retries,
      pricing_snapshot_sha256: f.config.pricing_snapshot_sha256,
      provider_attestation_key_id: EXPERIMENT.provider_attestation_key_id,
      provider_attestation_public_key_sha256: f.config.provider_attestation_public_key_sha256,
      execution_controls_sha256: f.config.execution_controls_sha256,
      exposure_policy_sha256: f.config.exposure_policy_sha256,
      per_exposure_token_cap: f.config.per_exposure_token_cap,
    },
    usage: {
      input_tokens: values.inputTokens,
      output_tokens: values.outputTokens,
      additional_tokens: values.additionalTokens,
      additional_token_scope: "condition-total",
      latency_ms: values.latencyMs,
      estimated_cost_usd: values.estimatedCostUsd,
    },
  };
  const payload = attestUsagePayload(unsignedPayload);
  const receiptPath = path.join(f.runDir, "artifacts", `${suffix}-usage-receipt.json`);
  const bytes = Buffer.from(`${JSON.stringify(payload)}\n`, "utf8");
  fs.writeFileSync(receiptPath, bytes);
  return {
    receiptPath,
    receiptSha256: createHash("sha256").update(bytes).digest("hex"),
  };
}

function attestUsagePayload(unsignedPayload) {
  const signedBytes = Buffer.from(stableJson(unsignedPayload), "utf8");
  return {
    ...unsignedPayload,
    attestation: {
      schema_version: 1,
      kind: "provider-usage-attestation",
      algorithm: "RS256",
      key_id: EXPERIMENT.provider_attestation_key_id,
      signed_payload_sha256: createHash("sha256").update(signedBytes).digest("hex"),
      signature_base64url: sign(
        "RSA-SHA256",
        signedBytes,
        createPrivateKey({ key: ATTESTATION_PRIVATE_KEY, format: "jwk" }),
      ).toString("base64url"),
    },
  };
}

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function recordVerifiedUsage(f, values, suffix = "usage") {
  return recordExecutionStateUsage({
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
    experimentEnabled: true,
    ...usageReceipt(f, values, suffix),
  });
}

function git(cwd, args) {
  const result = spawnSync("git", args, { cwd, encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  return result;
}

function runCli(args, cwd, home, env = {}) {
  return spawnSync(process.execPath, [CLI, ...args], {
    cwd,
    encoding: "utf8",
    env: {
      ...process.env,
      HOME: home,
      AGENT_FLOW_SKIP_CODEX_TRUST: "1",
      PYTHONPATH: [path.join(KIT_ROOT, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
      ...env,
    },
  });
}

function cliProject(t) {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-ledger-cli-"));
  const root = path.join(sandbox, "project");
  const home = path.join(sandbox, "home");
  fs.mkdirSync(root, { recursive: true });
  fs.mkdirSync(home, { recursive: true });
  t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));
  git(root, ["init", "-b", "main"]);
  git(root, ["config", "user.email", "test@example.com"]);
  git(root, ["config", "user.name", "Test User"]);
  fs.writeFileSync(path.join(root, "README.md"), "fixture\n", "utf8");
  git(root, ["add", "README.md"]);
  git(root, ["commit", "-m", "init"]);
  const install = runCli(["install", "--profile", "node"], root, home);
  assert.equal(install.status, 0, install.stderr || install.stdout);
  return { root, home };
}

function activeNodeStateRecord(root) {
  const commonDir = path.resolve(root, git(root, ["rev-parse", "--git-common-dir"]).stdout.trim());
  const pointerPaths = [path.join(root, ".agent-flow", "state", "current-run.json")];
  const runtimeRoot = path.join(commonDir, "agent-flow", "worktrees");
  if (fs.existsSync(runtimeRoot)) {
    for (const entry of fs.readdirSync(runtimeRoot, { withFileTypes: true })) {
      if (entry.isDirectory() && !entry.isSymbolicLink()) {
        pointerPaths.push(path.join(runtimeRoot, entry.name, ".agent-flow", "state", "current-run.json"));
      }
    }
  }
  const active = pointerPaths.flatMap((statePath) => {
    if (!fs.existsSync(statePath)) return [];
    const state = readJson(statePath);
    if (["complete", "aborted"].includes(state.status) || state.phase === "complete") return [];
    return [{ statePath, state }];
  });
  assert.equal(active.length, 1, `expected one active Node state, found ${active.length}`);
  return active[0];
}

function experimentEnv(mode) {
  return {
    AGENT_FLOW_LEDGER_MODE: mode,
    AGENT_FLOW_EXPERIMENT_ID: EXPERIMENT.experiment_id,
    AGENT_FLOW_EXPERIMENT_MODEL_ID: EXPERIMENT.model_id,
    AGENT_FLOW_EXPERIMENT_TOOL_PERMISSIONS_SHA256: EXPERIMENT.tool_permissions_sha256,
    AGENT_FLOW_EXPERIMENT_SYSTEM_PROMPT_SHA256: EXPERIMENT.system_prompt_sha256,
    AGENT_FLOW_EXPERIMENT_CAPS_SHA256: EXPERIMENT.caps_sha256,
    AGENT_FLOW_EXPERIMENT_PROVIDER_RETRY_POLICY_SHA256: EXPERIMENT.provider_retry_policy_sha256,
    AGENT_FLOW_EXPERIMENT_PROVIDER_MAX_RETRIES: String(EXPERIMENT.provider_max_retries),
    AGENT_FLOW_EXPERIMENT_PRICING_JSON: JSON.stringify(PRICING),
    AGENT_FLOW_EXPERIMENT_PROVIDER_ATTESTATION_KEY_ID: EXPERIMENT.provider_attestation_key_id,
    AGENT_FLOW_EXPERIMENT_PROVIDER_ATTESTATION_PUBLIC_KEY_JWK: JSON.stringify(ATTESTATION_PUBLIC_KEY),
  };
}

function startCliRun(project, mode, workflow = "default", runId = "ledger-cli") {
  const env = mode ? experimentEnv(mode) : {};
  const start = runCli(
    ["run", "start", "--task", "ledger checkout", "--workflow", workflow, "--run-id", runId],
    project.root,
    project.home,
    env,
  );
  assert.equal(start.status, 0, start.stderr || start.stdout);
  const { statePath, state } = activeNodeStateRecord(project.root);
  const runDir = path.isAbsolute(state.run_dir)
    ? state.run_dir
    : path.resolve(path.dirname(path.dirname(path.dirname(statePath))), state.run_dir);
  const config = mode
    ? readJson(path.join(runDir, "artifacts", "execution-ledger", "config.json"))
    : null;
  if (config) {
    assert.equal(config.provider_max_retries, EXPERIMENT.provider_max_retries);
    assert.equal(config.provider_retry_policy_sha256, EXPERIMENT.provider_retry_policy_sha256);
    assert.deepEqual(config.pricing_snapshot, {
      ...PRICING,
      input_per_million: "2",
      output_per_million: "8",
    });
    assert.equal(config.provider_attestation_key_id, EXPERIMENT.provider_attestation_key_id);
    assert.deepEqual(config.provider_attestation_public_key, ATTESTATION_PUBLIC_KEY);
  }
  return {
    ...project,
    env,
    startResult: start,
    state,
    statePath,
    config,
    runDir,
  };
}

function setCliPhase(run, phaseId) {
  const ids = [...fs.readFileSync(path.join(KIT_ROOT, "workflows", `${run.state.workflow}.yaml`), "utf8")
    .matchAll(/^  - id:\s*([^\s]+)\s*$/gm)]
    .map((match) => match[1]);
  const index = ids.indexOf(phaseId);
  assert.notEqual(index, -1);
  const enteredAt = new Date(Date.now() - 5_000).toISOString();
  const state = {
    ...run.state,
    phase: phaseId,
    phase_index: index,
    status: "running",
    phase_entered_at: enteredAt,
    updated_at: enteredAt,
  };
  fs.writeFileSync(run.statePath, `${JSON.stringify(state, null, 2)}\n`, "utf8");
  fs.writeFileSync(path.join(run.runDir, "manifest.json"), `${JSON.stringify(state, null, 2)}\n`, "utf8");
  run.state = state;
}

function writeGate(runDir, { passed = false, diagnosis = "checkout failed", suffix = "" } = {}) {
  const file = path.join(runDir, "artifacts", "gate-results.json");
  fs.writeFileSync(file, `${JSON.stringify({
    passed,
    status: passed ? "green" : "request-changes",
    results: [{
      gate_id: "tests",
      argv: ["npm", "test"],
      command: "npm test",
      passed,
      required: true,
      exit_code: passed ? 0 : 1,
      stdout: passed ? "ok" : "",
      stderr: passed ? "" : `\u001b[31m${diagnosis}${suffix}\u001b[0m`,
    }],
  })}\n`, "utf8");
  return file;
}

function captureGate(f, artifactPath, {
  round = 1,
  fixLoopRounds = 0,
  routeKey = "request-changes",
  routedTo = "fix-loop",
  generatedAt = FIXED_TIME,
} = {}) {
  return captureExecutionState({
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
    experimentEnabled: f.enabled,
    phase: { id: "gates", routes: { green: "commit", "request-changes": "fix-loop" } },
    artifactPath,
    projectRoot: f.root,
    round,
    fixLoopRounds,
    generatedAt,
    workflowId: "default",
    routeKey,
    routedTo,
  });
}

function runnerTransitionOccurrenceId(state, phaseId) {
  const payload = {
    phase_id: phaseId,
    phase_index: state.phase_index,
    phase_revision: state.phase_revision ?? 0,
    run_id: state.run_id,
    schema_version: 1,
    workflow_id: state.workflow,
  };
  return createHash("sha256").update(JSON.stringify(payload)).digest("hex");
}

test("ledger mode is strict while an unset experiment creates no sidecar", (t) => {
  assert.equal(resolveLedgerMode(undefined), "artifacts-only");
  assert.throws(() => resolveLedgerMode("unknown"), /invalid AGENT_FLOW_LEDGER_MODE/);
  const disabled = fixture(t, "artifacts-only", { enabled: false });
  assert.equal(fs.existsSync(ledgerRoot(disabled.runDir)), false);
});

test("explicit artifacts-only creates measurement files without a ledger or sources", (t) => {
  const f = fixture(t, "artifacts-only");
  const root = ledgerRoot(f.runDir);
  for (const file of [
    "config.json",
    "workflow.json",
    "captures.jsonl",
    "injections.jsonl",
    "events.jsonl",
    "metrics.json",
    "usage.jsonl",
  ]) {
    assert.equal(fs.existsSync(path.join(root, file)), true, file);
  }
  assert.equal(fs.existsSync(path.join(root, "ledger.json")), false);
  assert.equal(fs.existsSync(path.join(root, "sources")), false);
  const config = readJson(path.join(root, "config.json"));
  assert.equal(config.mode, "artifacts-only");
  assert.equal(config.caps_sha256, EXPERIMENT.caps_sha256);
  assert.equal(config.fix_loop_max_rounds, 3);
  assert.match(config.task_sha256, /^[a-f0-9]{64}$/);
  assert.match(config.workflow_sha256, /^[a-f0-9]{64}$/);
});

test("manual usage is summary-only while immutable provider receipts enable actual usage", (t) => {
  const f = fixture(t, "ledger-always");
  const base = {
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
    experimentEnabled: true,
    generatedAt: FIXED_TIME,
    modelId: "model-v1",
    inputTokens: 100,
    outputTokens: 20,
    additionalTokens: 0,
    latencyMs: 50,
    estimatedCostUsd: "0.0000001",
  };
  const phase = recordExecutionStateUsage({
    ...base,
    eventId: "usage.phase.fix-loop.1",
    scope: "phase",
    phaseId: "fix-loop",
    round: 1,
    estimatedCostUsd: "1.2300",
  });
  assert.equal(readJsonl(path.join(ledgerRoot(f.runDir), "usage.jsonl"))[0].estimated_cost, "1.23");
  assert.deepEqual(phase, {
    ok: true,
    recorded: true,
    phase_id: "fix-loop",
    model_id: "model-v1",
    evidence_status: "summary-only",
    control_evidence_status: "summary-only",
  });
  assert.equal(readJson(path.join(ledgerRoot(f.runDir), "metrics.json")).actual_usage_coverage, false);

  markNodeComplete(f);
  const totalValues = {
    ...base,
    eventId: "usage.run-total",
    scope: "run-total",
    phaseId: null,
    round: null,
  };
  const recorded = recordVerifiedUsage(f, totalValues, "run-total");
  assert.deepEqual(recorded, {
    ok: true,
    recorded: true,
    phase_id: null,
    model_id: "model-v1",
    evidence_status: "verified-provider-receipt",
    control_evidence_status: "verified-control-receipt",
  });
  const metrics = readJson(path.join(ledgerRoot(f.runDir), "metrics.json"));
  assert.equal(metrics.actual_usage_coverage, true);
  assert.equal(metrics.actual_input_tokens, 100);
  assert.equal(metrics.actual_output_tokens, 20);
  assert.equal(metrics.actual_additional_tokens, 0);
  assert.equal(metrics.actual_usage_budget_matched, true);
  assert.equal(metrics.actual_total_tokens, 120);
  assert.equal(metrics.latency_ms, 50);
  assert.equal(metrics.estimated_cost, "0.0000001");
  assert.equal(metrics.experiment_valid, false);
  assert.equal(recordVerifiedUsage(f, totalValues, "run-total").recorded, false);

  const bad = recordExecutionStateUsage({
    ...totalValues,
    eventId: "usage.run-total",
    inputTokens: 101,
    ...usageReceipt(f, totalValues, "run-total"),
  });
  assert.equal(bad.ok, false);
  assert.match(bad.error, /conflicts with provider receipt/);
  assert.equal(readJsonl(path.join(ledgerRoot(f.runDir), "usage.jsonl")).length, 2);

  const numericCost = recordExecutionStateUsage({
    ...base,
    eventId: "usage.numeric-cost",
    scope: "phase",
    phaseId: "fix-loop",
    round: 2,
    estimatedCostUsd: 0.0000001,
  });
  assert.equal(numericCost.ok, false);
  assert.match(numericCost.error, /invalid execution ledger estimated_cost_usd/);
});

test("provider usage receipt hash and archived projection are mandatory", (t) => {
  const f = fixture(t, "ledger-always");
  markNodeComplete(f);
  const values = {
    eventId: "usage.receipt-proof",
    generatedAt: FIXED_TIME,
    scope: "run-total",
    phaseId: null,
    round: null,
    modelId: "model-v1",
    inputTokens: 10,
    outputTokens: 2,
    additionalTokens: 0,
    latencyMs: 5,
    estimatedCostUsd: "0.1",
  };
  const receipt = usageReceipt(f, values, "receipt-proof");
  const missingHash = recordExecutionStateUsage({
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
    experimentEnabled: true,
    receiptPath: receipt.receiptPath,
  });
  assert.equal(missingHash.ok, false);
  assert.match(missingHash.error, /receipt-sha256/);

  const unsignedPayload = readJson(receipt.receiptPath);
  delete unsignedPayload.attestation;
  const unsignedPath = path.join(f.runDir, "artifacts", "caller-made-receipt.json");
  const unsignedBytes = Buffer.from(`${JSON.stringify(unsignedPayload)}\n`, "utf8");
  fs.writeFileSync(unsignedPath, unsignedBytes);
  const unsigned = recordExecutionStateUsage({
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
    experimentEnabled: true,
    receiptPath: unsignedPath,
    receiptSha256: createHash("sha256").update(unsignedBytes).digest("hex"),
  });
  assert.equal(unsigned.ok, false);
  assert.match(unsigned.error, /attestation|receipt fields/);
  assert.equal(readJsonl(path.join(ledgerRoot(f.runDir), "usage.jsonl")).length, 0);

  const forgedPayload = readJson(receipt.receiptPath);
  const signature = forgedPayload.attestation.signature_base64url;
  forgedPayload.attestation.signature_base64url = `${signature[0] === "A" ? "B" : "A"}${signature.slice(1)}`;
  const forgedPath = path.join(f.runDir, "artifacts", "forged-receipt.json");
  const forgedBytes = Buffer.from(`${JSON.stringify(forgedPayload)}\n`, "utf8");
  fs.writeFileSync(forgedPath, forgedBytes);
  const forged = recordExecutionStateUsage({
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
    experimentEnabled: true,
    receiptPath: forgedPath,
    receiptSha256: createHash("sha256").update(forgedBytes).digest("hex"),
  });
  assert.equal(forged.ok, false);
  assert.match(forged.error, /signature verification failed/);
  assert.equal(readJsonl(path.join(ledgerRoot(f.runDir), "usage.jsonl")).length, 0);

  const recorded = recordExecutionStateUsage({
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
    experimentEnabled: true,
    ...receipt,
  });
  assert.equal(recorded.ok, true, recorded.error);
  const event = readJsonl(path.join(ledgerRoot(f.runDir), "usage.jsonl"))[0];
  assert.equal(event.evidence_status, "verified-provider-receipt");
  assert.equal(event.usage_provenance.sha256, receipt.receiptSha256);
  const archive = path.join(f.runDir, event.usage_provenance.archive_path);
  fs.writeFileSync(archive, "{}\n", "utf8");
  assert.equal(validateExecutionStateLedger({
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
    requireCompletion: true,
  }).verified, false);
});

test("external controls require commitments and verified receipts stay bound to the pinned snapshot", (t) => {
  const invalid = fixture(t, "ledger-always", { enabled: false });
  const invalidControl = initializeExecutionStateLedger({
    runDir: invalid.runDir,
    runId: "r1",
    mode: "ledger-always",
    experimentEnabled: true,
    task: "fix checkout",
    workflowId: "default",
    workflowPhases: WORKFLOW_PHASES,
    baseCommit: "1".repeat(40),
    experiment: { ...EXPERIMENT, tool_permissions_sha256: "tool-policy-v1" },
    runSnapshot: RUN_SNAPSHOT,
  });
  assert.equal(invalidControl.ok, false);
  assert.match(invalidControl.error, /tool_permissions_sha256/);
  assert.equal(fs.existsSync(ledgerRoot(invalid.runDir)), false);

  const unsafePricing = initializeExecutionStateLedger({
    runDir: invalid.runDir,
    runId: "r1",
    mode: "ledger-always",
    experimentEnabled: true,
    task: "fix checkout",
    workflowId: "default",
    workflowPhases: WORKFLOW_PHASES,
    baseCommit: "1".repeat(40),
    experiment: {
      ...EXPERIMENT,
      pricing_snapshot: { ...PRICING, input_per_million: 9_007_199_254_740_993 },
    },
    runSnapshot: RUN_SNAPSHOT,
  });
  assert.equal(unsafePricing.ok, false);
  assert.match(unsafePricing.error, /pricing input_per_million/);
  const noncanonicalPricing = initializeExecutionStateLedger({
    runDir: invalid.runDir,
    runId: "r1",
    mode: "ledger-always",
    experimentEnabled: true,
    task: "fix checkout",
    workflowId: "default",
    workflowPhases: WORKFLOW_PHASES,
    baseCommit: "1".repeat(40),
    experiment: {
      ...EXPERIMENT,
      pricing_snapshot: { ...PRICING, input_per_million: 0.000001 },
    },
    runSnapshot: RUN_SNAPSHOT,
  });
  assert.equal(noncanonicalPricing.ok, false);
  assert.match(noncanonicalPricing.error, /pricing input_per_million/);

  const missing = fixture(t, "ledger-always", {
    experiment: { experiment_id: EXPERIMENT.experiment_id, model_id: EXPERIMENT.model_id },
  });
  markNodeComplete(missing);
  const values = {
    eventId: "usage.missing-controls",
    generatedAt: FIXED_TIME,
    scope: "run-total",
    phaseId: null,
    round: null,
    modelId: EXPERIMENT.model_id,
    inputTokens: 10,
    outputTokens: 2,
    additionalTokens: 0,
    latencyMs: 5,
    estimatedCostUsd: "0.1",
  };
  const missingReceipt = usageReceipt(missing, values, "missing-controls");
  const missingResult = recordExecutionStateUsage({
    runDir: missing.runDir,
    runId: "r1",
    mode: missing.mode,
    experimentEnabled: true,
    ...missingReceipt,
  });
  assert.equal(missingResult.ok, false);
  assert.match(missingResult.error, /trusted independent attestation/);
  assert.equal(readJsonl(path.join(ledgerRoot(missing.runDir), "usage.jsonl")).length, 0);

  const pinned = fixture(t, "ledger-always");
  markNodeComplete(pinned);
  const retryReceipt = usageReceipt(pinned, values, "retry-mismatch");
  const retryUnsigned = readJson(retryReceipt.receiptPath);
  delete retryUnsigned.attestation;
  retryUnsigned.control_receipt.provider_max_retries += 1;
  const retryPayload = attestUsagePayload(retryUnsigned);
  const retryBytes = Buffer.from(`${JSON.stringify(retryPayload)}\n`, "utf8");
  fs.writeFileSync(retryReceipt.receiptPath, retryBytes);
  const retryMismatch = recordExecutionStateUsage({
    runDir: pinned.runDir,
    runId: "r1",
    mode: pinned.mode,
    experimentEnabled: true,
    receiptPath: retryReceipt.receiptPath,
    receiptSha256: createHash("sha256").update(retryBytes).digest("hex"),
  });
  assert.equal(retryMismatch.ok, false);
  assert.match(retryMismatch.error, /does not match the pinned run/);

  const receipt = usageReceipt(pinned, values, "snapshot-mismatch");
  const payload = readJson(receipt.receiptPath);
  payload.control_receipt.runner_control_snapshot_sha256 = "9".repeat(64);
  const bytes = Buffer.from(`${JSON.stringify(payload)}\n`, "utf8");
  fs.writeFileSync(receipt.receiptPath, bytes);
  const mismatch = recordExecutionStateUsage({
    runDir: pinned.runDir,
    runId: "r1",
    mode: pinned.mode,
    experimentEnabled: true,
    receiptPath: receipt.receiptPath,
    receiptSha256: createHash("sha256").update(bytes).digest("hex"),
  });
  assert.equal(mismatch.ok, false);
  assert.match(mismatch.error, /payload commitment mismatch/);
  assert.equal(readJsonl(path.join(ledgerRoot(pinned.runDir), "usage.jsonl")).length, 0);
});

test("journal recovers every derived append boundary idempotently", (t) => {
  const previous = process.env.AGENT_FLOW_LEDGER_FAULT_AFTER;
  t.after(() => {
    if (previous === undefined) delete process.env.AGENT_FLOW_LEDGER_FAULT_AFTER;
    else process.env.AGENT_FLOW_LEDGER_FAULT_AFTER = previous;
  });
  for (const point of ["target", "event", "ledger", "metrics"]) {
    const f = fixture(t, "ledger-always");
    process.env.AGENT_FLOW_LEDGER_FAULT_AFTER = point;
    const failed = captureGate(f, writeGate(f.runDir));
    assert.equal(failed.ok, false, point);
    assert.equal(fs.existsSync(path.join(ledgerRoot(f.runDir), "transaction.json")), true, point);
    delete process.env.AGENT_FLOW_LEDGER_FAULT_AFTER;
    if (point === "target") {
      const deadOwner = spawnSync(process.execPath, ["-e", ""], { encoding: "utf8" });
      fs.writeFileSync(
        path.join(ledgerRoot(f.runDir), "transaction.lock"),
        `${JSON.stringify({ pid: deadOwner.pid, token: "stale-owner" })}\n`,
        "utf8",
      );
    }
    const recovered = validateExecutionStateLedger({
      runDir: f.runDir,
      runId: "r1",
      mode: f.mode,
    });
    assert.equal(recovered.ok, true, `${point}: ${recovered.error}`);
    assert.equal(recovered.captures.length, 1, point);
    assert.equal(recovered.event_count, 1, point);
    assert.equal(fs.existsSync(path.join(ledgerRoot(f.runDir), "transaction.json")), false, point);
  }

  const completed = fixture(t, "ledger-always");
  markNodeComplete(completed);
  const values = {
    eventId: "usage.crash-completion",
    generatedAt: FIXED_TIME,
    scope: "run-total",
    phaseId: null,
    round: null,
    modelId: "model-v1",
    inputTokens: 10,
    outputTokens: 2,
    additionalTokens: 0,
    latencyMs: 5,
    estimatedCostUsd: "0.1",
  };
  process.env.AGENT_FLOW_LEDGER_FAULT_AFTER = "completion";
  const failed = recordVerifiedUsage(completed, values, "crash-completion");
  assert.equal(failed.ok, false);
  delete process.env.AGENT_FLOW_LEDGER_FAULT_AFTER;
  const recovered = validateExecutionStateLedger({
    runDir: completed.runDir,
    runId: "r1",
    mode: completed.mode,
    requireCompletion: true,
  });
  assert.equal(recovered.ok, true, recovered.error);
  assert.equal(recovered.completed, true);
  assert.equal(recovered.usage.length, 1);
});

test("tampered recovery journal is rejected before completing a partial append", (t) => {
  const previous = process.env.AGENT_FLOW_LEDGER_FAULT_AFTER;
  t.after(() => {
    if (previous === undefined) delete process.env.AGENT_FLOW_LEDGER_FAULT_AFTER;
    else process.env.AGENT_FLOW_LEDGER_FAULT_AFTER = previous;
  });
  const f = fixture(t, "ledger-always");
  process.env.AGENT_FLOW_LEDGER_FAULT_AFTER = "target";
  assert.equal(captureGate(f, writeGate(f.runDir)).ok, false);
  delete process.env.AGENT_FLOW_LEDGER_FAULT_AFTER;
  const journalPath = path.join(ledgerRoot(f.runDir), "transaction.json");
  const journal = readJson(journalPath);
  journal.completed_at = FIXED_TIME;
  fs.writeFileSync(journalPath, `${JSON.stringify(journal)}\n`, "utf8");
  const validation = validateExecutionStateLedger({
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
  });
  assert.equal(validation.ok, false);
  assert.match(validation.error, /transaction journal/);
  assert.equal(readJsonl(path.join(ledgerRoot(f.runDir), "events.jsonl")).length, 0);
  assert.equal(readJson(path.join(ledgerRoot(f.runDir), "ledger.json")).entries.status.length, 0);
  assert.equal(fs.existsSync(journalPath), true);
});

test("artifacts-only records prompt proxy observations without injecting content", (t) => {
  const f = fixture(t, "artifacts-only");
  const observed = observeExecutionStateInjection({
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
    experimentEnabled: true,
    phase: { id: "fix-loop" },
    projectRoot: f.root,
    round: 1,
    generatedAt: FIXED_TIME,
    promptBytes: 401,
  });
  assert.deepEqual(observed, { ok: true, observed: true, block: "" });
  const event = readJsonl(path.join(ledgerRoot(f.runDir), "injections.jsonl"))[0];
  assert.equal(event.injected, false);
  assert.equal(event.prompt_bytes, 401);
  assert.equal(event.byte_count, 0);
  const metrics = readJson(path.join(ledgerRoot(f.runDir), "metrics.json"));
  assert.equal(metrics.prompt_bytes, 401);
  assert.equal(metrics.prompt_token_proxy, 101);
  assert.equal(metrics.injected_bytes, 0);
});

test("artifacts-only captures immutable bytes and computes repeat and gate-attempt metrics", (t) => {
  const f = fixture(t, "artifacts-only");
  const first = writeGate(f.runDir);
  const firstBytes = fs.readFileSync(first);
  const captured = captureGate(f, first);
  assert.equal(captured.ok, true, captured.error);
  assert.equal(captured.captured, true);
  const replay = captureGate(f, first, { generatedAt: "2026-07-11T00:00:01.000Z" });
  assert.equal(replay.captured, false);
  assert.equal(replay.gate_attempt, 1);
  assert.equal(readJsonl(path.join(ledgerRoot(f.runDir), "captures.jsonl")).length, 1);

  writeGate(f.runDir, { suffix: " " });
  assert.equal(captureGate(f, first, {
    round: 2,
    fixLoopRounds: 1,
    generatedAt: "2026-07-11T00:01:00.000Z",
  }).ok, true);
  writeGate(f.runDir, { passed: true });
  assert.equal(captureGate(f, first, {
    round: 2,
    fixLoopRounds: 1,
    routeKey: "green",
    routedTo: "commit",
    generatedAt: "2026-07-11T00:02:00.000Z",
  }).ok, true);

  const captures = readJsonl(path.join(ledgerRoot(f.runDir), "captures.jsonl"));
  assert.equal(captures.length, 3);
  assert.equal(captures[0].measurement.failure_fingerprints.length, 1);
  const source = captures[0].source_provenance[0];
  assert.equal(fs.readFileSync(path.join(f.runDir, source.archive_path)).equals(firstBytes), true);
  const metrics = readJson(path.join(ledgerRoot(f.runDir), "metrics.json"));
  assert.equal(metrics.repeated_command_fingerprint_count, 2);
  assert.equal(metrics.repeated_failure_fingerprint_count, 1);
  assert.equal(metrics.repeated_diagnosis_fingerprint_count, 1);
  assert.equal(metrics.gate_green_attempt, 3);
  assert.equal(metrics.gate_green_le_3, true);
  assert.equal(metrics.fix_loop_rounds, 1);
  assert.equal(metrics.canonical_routing_violations, 0);
});

test("capture replay identity keeps distinct run-local artifact paths", (t) => {
  const f = fixture(t, "ledger-selective");
  const artifact = writeGate(f.runDir);
  assert.equal(captureGate(f, artifact).gate_attempt, 1);
  const alternate = path.join(f.runDir, "artifacts", "alternate-gate-results.json");
  fs.copyFileSync(artifact, alternate);
  const distinct = captureGate(f, alternate, {
    generatedAt: "2026-07-11T00:00:01.000Z",
  });
  assert.equal(distinct.captured, true);
  assert.equal(distinct.gate_attempt, 2);
  assert.equal(readJsonl(path.join(ledgerRoot(f.runDir), "captures.jsonl")).length, 2);
});

test("routing-only captures fix-loop refactor and pr-watch without ledger sources", (t) => {
  const workflowPhases = [
    { id: "fix-loop", routes: { default: "refactor" } },
    { id: "refactor", routes: { default: "pr-watch" } },
    { id: "pr-watch", routes: { green: "commit" } },
    { id: "commit" },
  ];
  const f = fixture(t, "ledger-always", { workflowPhases });
  const transitions = [
    [workflowPhases[0], "default", "refactor", "1".repeat(64)],
    [workflowPhases[1], "default", "pr-watch", "2".repeat(64)],
    [workflowPhases[2], "green", "commit", "3".repeat(64)],
    [workflowPhases[1], "default", "pr-watch", "4".repeat(64)],
    [workflowPhases[2], "green", "commit", "5".repeat(64)],
  ];
  for (const [phase, routeKey, routedTo, transitionOccurrenceId] of transitions) {
    const artifact = path.join(f.runDir, "artifacts", `${phase.id}.md`);
    fs.writeFileSync(artifact, `# ${phase.id}\n`, "utf8");
    const result = captureExecutionState({
      runDir: f.runDir,
      runId: "r1",
      mode: f.mode,
      experimentEnabled: true,
      phase,
      artifactPath: artifact,
      projectRoot: f.root,
      round: 1,
      fixLoopRounds: 2,
      generatedAt: FIXED_TIME,
      workflowId: "default",
      routeKey,
      routedTo,
      transitionOccurrenceId,
    });
    assert.equal(result.ok, true, result.error);
    assert.equal(result.captured, true);
  }

  const captures = readJsonl(path.join(ledgerRoot(f.runDir), "captures.jsonl"));
  assert.equal(captures.length, 5);
  for (const capture of captures) {
    assert.deepEqual(capture.source_provenance, []);
    assert.deepEqual(capture.entry_ids, []);
    assert.equal(capture.expected_target, transitions.find(([phase]) => phase.id === capture.phase)[2]);
  }
  assert.equal(fs.existsSync(path.join(ledgerRoot(f.runDir), "sources")), false);
  assert.deepEqual(readJson(path.join(ledgerRoot(f.runDir), "ledger.json")).entries, {
    status: [],
    knowledge: [],
    procedural: [],
  });
  const metrics = readJson(path.join(ledgerRoot(f.runDir), "metrics.json"));
  assert.equal(metrics.canonical_routing_violations, 0);
  assert.equal(metrics.canonical_transition_count, 5);
  assert.equal(metrics.canonical_route_coverage_complete, false);
  assert.ok(metrics.canonical_route_coverage_gap_count > 0);
  assert.equal(new Set(metrics.routing_provenance.map(
    (item) => item.transition_occurrence_id,
  )).size, 5);
  assert.deepEqual(new Set(metrics.routing_provenance.map((item) => item.phase)), new Set([
    "fix-loop",
    "refactor",
    "pr-watch",
  ]));
});

test("canonical route coverage requires every transition from workflow start to complete", (t) => {
  const workflowPhases = [
    { id: "implement" },
    { id: "gates", routes: { green: "complete" } },
  ];
  const complete = fixture(t, "artifacts-only", { workflowPhases });
  const implementArtifact = path.join(complete.runDir, "artifacts", "implement.md");
  fs.writeFileSync(implementArtifact, "# Implement\n", "utf8");
  assert.equal(captureExecutionState({
    runDir: complete.runDir,
    runId: "r1",
    mode: complete.mode,
    experimentEnabled: true,
    phase: workflowPhases[0],
    artifactPath: implementArtifact,
    projectRoot: complete.root,
    round: 1,
    generatedAt: FIXED_TIME,
    workflowId: "default",
    routeKey: "sequential",
    routedTo: "gates",
    transitionOccurrenceId: "1".repeat(64),
  }).ok, true);
  const gateArtifact = path.join(complete.runDir, "artifacts", "gate-results.json");
  fs.writeFileSync(gateArtifact, `${JSON.stringify({
    passed: true,
    status: "green",
    results: [{
      gate_id: "tests",
      argv: ["npm", "test"],
      required: true,
      passed: true,
      exit_code: 0,
      stdout: "ok",
      stderr: "",
    }],
  })}\n`, "utf8");
  assert.equal(captureExecutionState({
    runDir: complete.runDir,
    runId: "r1",
    mode: complete.mode,
    experimentEnabled: true,
    phase: workflowPhases[1],
    artifactPath: gateArtifact,
    projectRoot: complete.root,
    round: 1,
    generatedAt: "2026-07-11T00:00:01.000Z",
    workflowId: "default",
    routeKey: "green",
    routedTo: "complete",
    transitionOccurrenceId: "2".repeat(64),
  }).ok, true);
  const completeMetrics = readJson(path.join(ledgerRoot(complete.runDir), "metrics.json"));
  assert.equal(completeMetrics.canonical_route_coverage_complete, true);
  assert.equal(completeMetrics.canonical_route_coverage_gap_count, 0);

  const incomplete = fixture(t, "artifacts-only", { workflowPhases });
  const incompleteGate = path.join(incomplete.runDir, "artifacts", "gate-results.json");
  fs.copyFileSync(gateArtifact, incompleteGate);
  assert.equal(captureExecutionState({
    runDir: incomplete.runDir,
    runId: "r1",
    mode: incomplete.mode,
    experimentEnabled: true,
    phase: workflowPhases[1],
    artifactPath: incompleteGate,
    projectRoot: incomplete.root,
    round: 1,
    generatedAt: FIXED_TIME,
    workflowId: "default",
    routeKey: "green",
    routedTo: "complete",
    transitionOccurrenceId: "3".repeat(64),
  }).ok, true);
  const incompleteMetrics = readJson(path.join(ledgerRoot(incomplete.runDir), "metrics.json"));
  assert.equal(incompleteMetrics.canonical_route_coverage_complete, false);
  assert.ok(incompleteMetrics.canonical_route_coverage_gap_count > 0);
  assert.equal(incompleteMetrics.experiment_valid, false);
});

test("ledger-always injects verified entries within byte caps and excludes multi-review", (t) => {
  const f = fixture(t, "ledger-always");
  const artifact = writeGate(f.runDir, { diagnosis: `${f.root}/src/checkout.js failed\u0007` });
  assert.equal(captureGate(f, artifact).ok, true);
  const options = {
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
    experimentEnabled: true,
    projectRoot: f.root,
    round: 1,
  };
  const block = executionStatePromptBlock({ ...options, phase: { id: "implement" } });
  assert.match(block, /^## Execution ledger \(advisory; run r1, round 1\/3\)/);
  assert.doesNotMatch(block, new RegExp(f.root.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  assert.equal(block.split("\n").length <= 5, true);
  assert.equal(block.length <= 720, true);
  assert.equal(block.split("\n").slice(1).every((line) => Array.from(line).length <= 160), true);
  assert.equal(executionStatePromptBlock({
    ...options,
    phase: { id: "architecture-review", multi_review: true },
  }), "");
  assert.equal(executionStatePromptBlock({ ...options, phase: { id: "commit" } }), "");
  assert.equal(executionStatePromptBlock({ ...options, phase: { id: "push-pr" } }), "");
  assert.equal(executionStatePromptBlock({ ...options, phase: { id: "merge" } }), "");

  const first = observeExecutionStateInjection({
    ...options,
    phase: { id: "implement" },
    generatedAt: FIXED_TIME,
    promptBytes: 400,
  });
  const second = observeExecutionStateInjection({
    ...options,
    phase: { id: "implement" },
    generatedAt: "2026-07-11T00:00:01.000Z",
    promptBytes: 400,
  });
  assert.equal(first.observed, true);
  assert.equal(second.observed, true);
  const metrics = readJson(path.join(ledgerRoot(f.runDir), "metrics.json"));
  assert.equal(metrics.prompt_bytes, 800);
  assert.equal(metrics.injected_bytes, Buffer.byteLength(block, "utf8") * 2);
});

test("selective mode injects only fix phases or a repeated fingerprint", (t) => {
  const f = fixture(t, "ledger-selective");
  const artifact = writeGate(f.runDir);
  captureGate(f, artifact);
  const options = {
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
    experimentEnabled: true,
    projectRoot: f.root,
    round: 1,
  };
  assert.equal(executionStatePromptBlock({ ...options, phase: { id: "implement" } }), "");
  assert.match(executionStatePromptBlock({ ...options, phase: { id: "fix-loop" } }), /Gate tests/);

  writeGate(f.runDir, { suffix: " " });
  captureGate(f, artifact, {
    round: 2,
    fixLoopRounds: 1,
    generatedAt: "2026-07-11T00:01:00.000Z",
  });
  const repeated = executionStatePromptBlock({ ...options, phase: { id: "implement" }, round: 2 });
  assert.match(repeated, /repeated gate failure/);
});

test("selective and action arms expose first failures only in literal fix-loop", (t) => {
  const workflowPhases = [
    ...WORKFLOW_PHASES,
    { id: "pr-ci-fix" },
    { id: "pr-comment-fix" },
  ];
  for (const mode of ["ledger-selective", "action-self-review"]) {
    const f = fixture(t, mode, { workflowPhases });
    const artifact = writeGate(f.runDir);
    captureGate(f, artifact);
    const options = {
      runDir: f.runDir,
      runId: "r1",
      mode,
      experimentEnabled: true,
      projectRoot: f.root,
      round: 1,
    };
    assert.notEqual(executionStatePromptBlock({ ...options, phase: { id: "fix-loop" } }), "");
    assert.equal(executionStatePromptBlock({ ...options, phase: { id: "pr-ci-fix" } }), "");
    assert.equal(executionStatePromptBlock({ ...options, phase: { id: "pr-comment-fix" } }), "");

    captureGate(f, artifact, {
      round: 2,
      fixLoopRounds: 1,
      generatedAt: "2026-07-11T00:01:00.000Z",
    });
    assert.notEqual(executionStatePromptBlock({ ...options, phase: { id: "pr-ci-fix" }, round: 2 }), "");
    assert.notEqual(executionStatePromptBlock({ ...options, phase: { id: "pr-comment-fix" }, round: 2 }), "");
  }
});

test("action-self-review inlines a bounded verified raw view without exposing ledger content", (t) => {
  const f = fixture(t, "action-self-review");
  const artifact = writeGate(f.runDir, { diagnosis: "secret raw diagnosis" });
  captureGate(f, artifact);
  const block = executionStatePromptBlock({
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
    experimentEnabled: true,
    phase: { id: "fix-loop" },
    projectRoot: f.root,
    round: 1,
  });
  assert.match(block, /^## Action self-review \(bounded verified artifact\)\n/);
  assert.doesNotMatch(block, /Execution ledger|round|\.agent-flow\/|Inspect `/i);
  assert.match(block, /secret raw diagnosis/);
  assert.match(block, /^- Raw: /m);
  assert.equal(block.split("\n").length <= 5, true);
  assert.equal(Buffer.byteLength(block) <= 720, true);
});

test("action-self-review inspects the latest direct failure before an older repeat", (t) => {
  const f = fixture(t, "action-self-review");
  const artifact = writeGate(f.runDir, { diagnosis: "failure-A" });
  captureGate(f, artifact, { generatedAt: "2026-07-11T00:00:01.000Z" });
  writeGate(f.runDir, { diagnosis: "failure-A", suffix: " " });
  captureGate(f, artifact, {
    round: 2,
    fixLoopRounds: 1,
    generatedAt: "2026-07-11T00:00:02.000Z",
  });
  writeGate(f.runDir, { diagnosis: "failure-B" });
  captureGate(f, artifact, {
    round: 3,
    fixLoopRounds: 2,
    generatedAt: "2026-07-11T00:00:03.000Z",
  });

  const block = executionStatePromptBlock({
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
    experimentEnabled: true,
    phase: { id: "fix-loop" },
    projectRoot: f.root,
    round: 3,
  });
  assert.match(block, /failure-B/);
  assert.doesNotMatch(block, /Inspect `|\.agent-flow\//);
});

test("review findings come from the current artifact and ignore a stale global summary", (t) => {
  const f = fixture(t, "ledger-always");
  const summary = path.join(f.runDir, "review-summary.json");
  fs.writeFileSync(summary, JSON.stringify({
    verdict: "NEEDS_CHANGES",
    findings: ["stale global finding"],
  }), "utf8");
  const artifact = path.join(f.runDir, "artifacts", "final-review.md");
  fs.writeFileSync(artifact, "# Review\n\n## Reviewer 1\n\n- Must   fix checkout retry ordering.\n", "utf8");
  const captured = captureExecutionState({
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
    experimentEnabled: true,
    phase: {
      id: "final-review",
      multi_review: true,
      routes: { approve: "gates", "request-changes": "fix-loop" },
    },
    artifactPath: artifact,
    projectRoot: f.root,
    round: 1,
    fixLoopRounds: 0,
    generatedAt: FIXED_TIME,
    workflowId: "default",
    routeKey: "request-changes",
    routedTo: "fix-loop",
  });
  assert.equal(captured.ok, true, captured.error);
  assert.equal(captured.unsupported_count, 0);
  const replay = captureExecutionState({
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
    experimentEnabled: true,
    phase: {
      id: "final-review",
      multi_review: true,
      routes: { approve: "gates", "request-changes": "fix-loop" },
    },
    artifactPath: artifact,
    projectRoot: f.root,
    round: 1,
    fixLoopRounds: 0,
    generatedAt: "2026-07-11T00:00:01.000Z",
    workflowId: "default",
    routeKey: "request-changes",
    routedTo: "fix-loop",
  });
  assert.equal(replay.captured, false);
  assert.equal(readJsonl(path.join(ledgerRoot(f.runDir), "captures.jsonl")).length, 1);
  assert.equal(fs.readdirSync(path.join(ledgerRoot(f.runDir), "review-summaries")).length, 1);
  const ledger = readJson(path.join(ledgerRoot(f.runDir), "ledger.json"));
  assert.equal(ledger.entries.knowledge.length, 1);
  assert.equal(ledger.entries.knowledge[0].payload.finding, "Must fix checkout retry ordering.");
  assert.equal(ledger.entries.knowledge[0].provenance.selector, "/findings/0");
  assert.doesNotMatch(JSON.stringify(ledger), /stale global finding/);
});

test("a later approved review semantically resolves prior findings", (t) => {
  const f = fixture(t, "ledger-always");
  const artifact = path.join(f.runDir, "artifacts", "final-review.md");
  const phase = {
    id: "final-review",
    multi_review: true,
    routes: { approve: "gates", "request-changes": "fix-loop" },
  };
  fs.writeFileSync(artifact, "# Review\n\n## Findings\n\n- Fix checkout ordering.\n", "utf8");
  assert.equal(captureExecutionState({
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
    experimentEnabled: true,
    phase,
    artifactPath: artifact,
    projectRoot: f.root,
    round: 1,
    generatedAt: FIXED_TIME,
    workflowId: "default",
    routeKey: "request-changes",
    routedTo: "fix-loop",
  }).ok, true);
  const prompt = {
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
    experimentEnabled: true,
    projectRoot: f.root,
    round: 1,
  };
  assert.match(executionStatePromptBlock({ ...prompt, phase: { id: "fix-loop" } }), /Fix checkout ordering/);
  assert.equal(executionStatePromptBlock({ ...prompt, phase }), "");

  fs.writeFileSync(artifact, "# Review\n\n## Overall\n\nverdict: approve\n", "utf8");
  assert.equal(captureExecutionState({
    ...prompt,
    phase,
    artifactPath: artifact,
    round: 2,
    generatedAt: "2026-07-11T00:01:00.000Z",
    workflowId: "default",
    routeKey: "approve",
    routedTo: "gates",
  }).ok, true);
  const ledger = readJson(path.join(ledgerRoot(f.runDir), "ledger.json"));
  assert.equal(ledger.entries.knowledge[0].resolution.kind, "review-result");
  assert.equal(executionStatePromptBlock({ ...prompt, phase: { id: "fix-loop" }, round: 2 }), "");
});

test("tampered archives and corrupt ledger fail open without prompt injection", (t) => {
  const f = fixture(t, "ledger-always");
  const artifact = writeGate(f.runDir);
  captureGate(f, artifact);
  const capture = readJsonl(path.join(ledgerRoot(f.runDir), "captures.jsonl"))[0];
  fs.writeFileSync(path.join(f.runDir, capture.source_provenance[0].archive_path), "tampered", "utf8");
  const options = {
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
    experimentEnabled: true,
    phase: { id: "fix-loop" },
    projectRoot: f.root,
    round: 1,
  };
  assert.equal(executionStatePromptBlock(options), "");
  const observed = observeExecutionStateInjection({ ...options, generatedAt: FIXED_TIME });
  assert.equal(observed.ok, false);
  const metrics = readJson(path.join(ledgerRoot(f.runDir), "metrics.json"));
  assert.equal(metrics.stale_candidate_count, 0);
  assert.equal(metrics.stale_reminder_count, 0);

  fs.writeFileSync(path.join(ledgerRoot(f.runDir), "ledger.json"), "{broken", "utf8");
  assert.equal(executionStatePromptBlock(options), "");
  const captureAfterCorruption = captureGate(f, artifact, { round: 2 });
  assert.equal(captureAfterCorruption.ok, false);
});

test("source symlinks outside the run fail open instead of being archived", (t) => {
  const f = fixture(t, "ledger-always");
  const outside = path.join(f.root, "outside-gate-results.json");
  fs.writeFileSync(outside, JSON.stringify({ passed: false, results: [] }), "utf8");
  const artifact = path.join(f.runDir, "artifacts", "gate-results.json");
  fs.symlinkSync(outside, artifact);
  const captured = captureGate(f, artifact);
  assert.equal(captured.ok, false);
  assert.match(captured.error, /source artifact is outside the run/);
  assert.equal(fs.existsSync(path.join(ledgerRoot(f.runDir), "sources")), false);
});

test("resolved gate failures expire from subsequent prompts", (t) => {
  const f = fixture(t, "ledger-always");
  const artifact = writeGate(f.runDir, { diagnosis: "transient failure" });
  assert.equal(captureGate(f, artifact).ok, true);
  const prompt = {
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
    experimentEnabled: true,
    phase: { id: "fix-loop" },
    projectRoot: f.root,
    round: 1,
  };
  assert.match(executionStatePromptBlock(prompt), /transient failure/);

  writeGate(f.runDir, { passed: true });
  assert.equal(captureGate(f, artifact, {
    round: 2,
    routeKey: "green",
    routedTo: "commit",
    generatedAt: "2026-07-11T00:01:00.000Z",
  }).ok, true);
  const ledger = readJson(path.join(ledgerRoot(f.runDir), "ledger.json"));
  assert.equal(ledger.entries.status[0].resolution.kind, "gate-success");
  assert.equal(executionStatePromptBlock({ ...prompt, round: 2 }), "");
});

test("canonical gate green requires a non-fix route and passing required rows", (t) => {
  const f = fixture(t, "ledger-always");
  const artifact = path.join(f.runDir, "artifacts", "gate-results.json");
  fs.writeFileSync(artifact, `${JSON.stringify({
    passed: true,
    results: [{
      gate_id: "unit",
      argv: ["npm", "test"],
      required: true,
      passed: false,
      exit_code: 1,
      stdout: "",
      stderr: "still failing",
    }],
  })}\n`, "utf8");
  assert.equal(captureGate(f, artifact, {
    routeKey: "request-changes",
    routedTo: "fix-loop",
    fixLoopRounds: 1,
  }).ok, true);

  const metrics = readJson(path.join(ledgerRoot(f.runDir), "metrics.json"));
  const ledger = readJson(path.join(ledgerRoot(f.runDir), "ledger.json"));
  assert.equal(metrics.gate_green_le_3, null);
  assert.equal(metrics.gate_green_attempt, null);
  assert.equal(metrics.canonical_routing_violations, 0);
  assert.equal(ledger.entries.status[0].resolution, undefined);
  assert.match(executionStatePromptBlock({
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
    experimentEnabled: true,
    phase: { id: "fix-loop" },
    projectRoot: f.root,
    round: 1,
  }), /still failing/);
});

test("a complete later gate snapshot expires commands that disappeared", (t) => {
  const f = fixture(t, "ledger-always");
  const artifact = path.join(f.runDir, "artifacts", "gate-results.json");
  const writeFailure = (gateId, argv, diagnosis) => fs.writeFileSync(
    artifact,
    `${JSON.stringify({
      passed: false,
      results: [{
        gate_id: gateId,
        argv,
        required: true,
        passed: false,
        exit_code: 1,
        stdout: "",
        stderr: diagnosis,
      }],
    })}\n`,
    "utf8",
  );
  writeFailure("old", ["npm", "test"], "old failure");
  assert.equal(captureGate(f, artifact, { fixLoopRounds: 1 }).ok, true);
  writeFailure("current", ["npm", "run", "lint"], "current failure");
  assert.equal(captureGate(f, artifact, {
    round: 2,
    fixLoopRounds: 2,
    generatedAt: "2026-07-11T00:01:00.000Z",
  }).ok, true);

  const ledger = readJson(path.join(ledgerRoot(f.runDir), "ledger.json"));
  const oldEntry = ledger.entries.status.find((entry) => entry.payload.gate_id === "old");
  const currentEntry = ledger.entries.status.find((entry) => entry.payload.gate_id === "current");
  assert.equal(oldEntry.resolution.kind, "gate-snapshot-absent");
  assert.equal(currentEntry.resolution, undefined);
  const block = executionStatePromptBlock({
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
    experimentEnabled: true,
    phase: { id: "fix-loop" },
    projectRoot: f.root,
    round: 2,
  });
  assert.match(block, /current failure/);
  assert.doesNotMatch(block, /old failure/);
});

test("command repetition uses normalized argv rather than gate metadata", (t) => {
  const f = fixture(t, "artifacts-only");
  const artifact = path.join(f.runDir, "artifacts", "gate-results.json");
  for (const [index, gateId] of ["unit", "renamed-unit"].entries()) {
    fs.writeFileSync(artifact, `${JSON.stringify({
      passed: false,
      results: [{
        gate_id: gateId,
        argv: ["npm", "test"],
        required: true,
        passed: false,
        exit_code: 1,
        stdout: "",
        stderr: "same failure",
      }],
    })}\n`, "utf8");
    assert.equal(captureGate(f, artifact, {
      round: index + 1,
      fixLoopRounds: index + 1,
      generatedAt: `2026-07-11T00:0${index}:00.000Z`,
    }).ok, true);
  }
  const metrics = readJson(path.join(ledgerRoot(f.runDir), "metrics.json"));
  assert.equal(metrics.repeated_command_fingerprint_count, 1);
  assert.equal(metrics.repeated_failure_fingerprint_count, 1);
  assert.equal(metrics.repeated_diagnosis_fingerprint_count, 1);
});

test("ledger payload id extractor selector and resolution tampering invalidates evidence", (t) => {
  const mutations = [
    ["payload", (entry) => { entry.payload.diagnosis = "forged diagnosis"; }],
    ["id", (entry) => { entry.id = "0".repeat(64); }],
    ["extractor", (entry) => { entry.provenance.extractor_version = "forged"; }],
    ["selector", (entry) => { entry.provenance.selector = "/results/99"; }],
    ["resolution", (entry) => {
      entry.resolution = {
        kind: "gate-suite-success",
        round: entry.round,
        generated_at: entry.generated_at,
        provenance: { ...entry.provenance, selector: "/" },
      };
    }],
  ];
  for (const [label, mutate] of mutations) {
    const f = fixture(t, "ledger-always");
    const artifact = writeGate(f.runDir);
    assert.equal(captureGate(f, artifact).ok, true);
    const ledgerPath = path.join(ledgerRoot(f.runDir), "ledger.json");
    const ledger = readJson(ledgerPath);
    mutate(ledger.entries.status[0]);
    fs.writeFileSync(ledgerPath, `${JSON.stringify(ledger)}\n`, "utf8");
    assert.equal(executionStatePromptBlock({
      runDir: f.runDir,
      runId: "r1",
      mode: f.mode,
      experimentEnabled: true,
      phase: { id: "fix-loop" },
      projectRoot: f.root,
      round: 1,
    }), "", label);
    assert.equal(validateExecutionStateLedger({
      runDir: f.runDir,
      runId: "r1",
      mode: f.mode,
    }).verified, false, label);
  }
});

test("JSONL leaves and the sidecar parent reject symlink escapes", (t) => {
  for (const leaf of ["captures.jsonl", "injections.jsonl", "usage.jsonl", "events.jsonl"]) {
    const f = fixture(t, "ledger-always");
    const outside = path.join(f.root, `${leaf}.outside`);
    fs.writeFileSync(outside, "sentinel\n", "utf8");
    const target = path.join(ledgerRoot(f.runDir), leaf);
    fs.unlinkSync(target);
    fs.symlinkSync(outside, target);

    let result;
    if (leaf === "captures.jsonl") {
      result = captureGate(f, writeGate(f.runDir));
    } else if (leaf === "injections.jsonl") {
      result = observeExecutionStateInjection({
        runDir: f.runDir,
        runId: "r1",
        mode: f.mode,
        experimentEnabled: true,
        phase: { id: "fix-loop" },
        projectRoot: f.root,
        round: 1,
        generatedAt: FIXED_TIME,
      });
    } else {
      result = recordExecutionStateUsage({
        runDir: f.runDir,
        runId: "r1",
        mode: f.mode,
        experimentEnabled: true,
        eventId: "usage.phase.1",
        generatedAt: FIXED_TIME,
        scope: "phase",
        phaseId: "fix-loop",
        round: 1,
        inputTokens: 1,
        outputTokens: 0,
        additionalTokens: 0,
        latencyMs: 1,
        estimatedCostUsd: "0",
        modelId: "model-v1",
      });
    }
    assert.equal(result.ok, false, leaf);
    assert.equal(fs.readFileSync(outside, "utf8"), "sentinel\n", leaf);
  }

  const root = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-ledger-parent-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const runDir = path.join(root, "run");
  const outside = path.join(root, "outside");
  fs.mkdirSync(path.join(runDir, "artifacts"), { recursive: true });
  fs.mkdirSync(outside);
  fs.writeFileSync(path.join(outside, "sentinel"), "unchanged", "utf8");
  fs.symlinkSync(outside, path.join(runDir, "artifacts", "execution-ledger"));
  const initialized = initializeExecutionStateLedger({
    runDir,
    runId: "r1",
    mode: "ledger-always",
    experimentEnabled: true,
    task: "parent escape",
    workflowId: "default",
    workflowPhases: [],
    baseCommit: "1".repeat(40),
    experiment: EXPERIMENT,
  });
  assert.equal(initialized.ok, false);
  assert.deepEqual(fs.readdirSync(outside), ["sentinel"]);
});

test("JSON leaves reject symlink escapes without mutating their targets", (t) => {
  for (const leaf of ["config.json", "workflow.json", "ledger.json", "metrics.json"]) {
    const f = fixture(t, "ledger-always");
    const outside = path.join(f.root, `${leaf}.outside`);
    fs.writeFileSync(outside, "sentinel\n", "utf8");
    const target = path.join(ledgerRoot(f.runDir), leaf);
    fs.unlinkSync(target);
    fs.symlinkSync(outside, target);
    const result = captureGate(f, writeGate(f.runDir));
    assert.equal(result.ok, false, leaf);
    assert.equal(fs.readFileSync(outside, "utf8"), "sentinel\n", leaf);
  }
  const completed = fixture(t, "artifacts-only");
  markNodeComplete(completed);
  assert.equal(recordExecutionStateUsage({
    runDir: completed.runDir,
    runId: "r1",
    mode: completed.mode,
    experimentEnabled: true,
    eventId: "usage.completion-symlink",
    generatedAt: FIXED_TIME,
    scope: "run-total",
    phaseId: null,
    round: null,
    inputTokens: 1,
    outputTokens: 0,
    additionalTokens: 0,
    latencyMs: 1,
    estimatedCostUsd: "0",
    modelId: "model-v1",
  }).ok, true);
  const outside = path.join(completed.root, "completion.outside");
  fs.writeFileSync(outside, "sentinel\n", "utf8");
  const completionPath = path.join(ledgerRoot(completed.runDir), "completion.json");
  fs.unlinkSync(completionPath);
  fs.symlinkSync(outside, completionPath);
  assert.equal(validateExecutionStateLedger({
    runDir: completed.runDir,
    runId: "r1",
    mode: completed.mode,
    requireCompletion: true,
  }).ok, false);
  assert.equal(fs.readFileSync(outside, "utf8"), "sentinel\n");
});

test("all valid gate commands enter the denominator while only required failures enter status", (t) => {
  const f = fixture(t, "ledger-always");
  const artifact = path.join(f.runDir, "artifacts", "gate-results.json");
  const payload = {
    passed: false,
    results: [
      { gate_id: "failed", argv: ["npm", "test"], required: true, passed: false, exit_code: 1, stderr: "failed" },
      { gate_id: "passed", argv: ["npm", "run", "lint"], required: true, passed: true, exit_code: 0 },
      { gate_id: "optional", argv: ["npm", "run", "audit"], required: false, passed: false, exit_code: 1 },
      { argv: ["invalid"], required: true, passed: false },
    ],
  };
  fs.writeFileSync(artifact, `${JSON.stringify(payload)}\n`, "utf8");
  assert.equal(captureGate(f, artifact).unsupported_count, 1);
  assert.equal(captureGate(f, artifact, {
    round: 2,
    generatedAt: "2026-07-11T00:01:00.000Z",
  }).ok, true);
  const captures = readJsonl(path.join(ledgerRoot(f.runDir), "captures.jsonl"));
  assert.equal(captures[0].measurement.command_fingerprints.length, 3);
  assert.equal(captures[0].measurement.failure_fingerprints.length, 1);
  const ledger = readJson(path.join(ledgerRoot(f.runDir), "ledger.json"));
  assert.equal(ledger.entries.status.length, 2);
  const metrics = readJson(path.join(ledgerRoot(f.runDir), "metrics.json"));
  assert.equal(metrics.repeated_command_fingerprint_count, 3);
  assert.equal(metrics.repeated_failure_fingerprint_count, 1);
  assert.equal(metrics.malformed_source_count, 2);
  assert.equal(metrics.excluded_source_count, 0);
});

test("manual terminal usage remains summary-only and cannot validate an experiment", (t) => {
  const f = fixture(t, "ledger-always");
  markNodeComplete(f);
  const result = recordExecutionStateUsage({
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
    experimentEnabled: true,
    eventId: "usage.run-total.mismatch",
    generatedAt: FIXED_TIME,
    scope: "run-total",
    phaseId: null,
    round: null,
    inputTokens: 10,
    outputTokens: 2,
    additionalTokens: 1,
    latencyMs: 10,
    estimatedCostUsd: "0",
    modelId: "model-v1",
  });
  assert.equal(result.ok, true, result.error);
  const metrics = readJson(path.join(ledgerRoot(f.runDir), "metrics.json"));
  assert.equal(metrics.gate_green_le_3, false);
  assert.equal(metrics.gate_green_attempt, null);
  assert.equal(metrics.actual_usage_budget_matched, false);
  assert.equal(metrics.actual_usage_coverage, false);
  assert.equal(metrics.summary_usage_coverage, true);
  assert.equal(metrics.experiment_valid, false);
});

test("timestamps accept only strict real RFC3339 instants", (t) => {
  const invalid = [
    "2026-02-29T00:00:00Z",
    "2026-13-01T00:00:00Z",
    "2026-01-01T24:00:00Z",
    "2026-01-01T00:00:00",
    "0000-01-01T00:00:00Z",
    "0001-01-01T00:00:00+23:59",
    "2026-01-01T00:00:00+24:00",
    "2026-01-01T00:00:00.1234567Z",
  ];
  for (const [index, generatedAt] of invalid.entries()) {
    const f = fixture(t, "ledger-always");
    const result = recordExecutionStateUsage({
      runDir: f.runDir,
      runId: "r1",
      mode: f.mode,
      experimentEnabled: true,
      eventId: `invalid.${index}`,
      generatedAt,
      scope: "phase",
      phaseId: "fix-loop",
      round: 1,
      inputTokens: 1,
      outputTokens: 0,
      additionalTokens: 0,
      latencyMs: 1,
      estimatedCostUsd: "0",
      modelId: "model-v1",
    });
    assert.equal(result.ok, false, generatedAt);
    assert.match(result.error, /invalid execution ledger timestamp/);
  }

  const valid = fixture(t, "ledger-always");
  const accepted = recordExecutionStateUsage({
    runDir: valid.runDir,
    runId: "r1",
    mode: valid.mode,
    experimentEnabled: true,
    eventId: "valid.offset",
    generatedAt: "2026-07-11T09:30:00.123456+09:30",
    scope: "phase",
    phaseId: "fix-loop",
    round: 1,
    inputTokens: 1,
    outputTokens: 0,
    additionalTokens: 0,
    latencyMs: 1,
    estimatedCostUsd: "0",
    modelId: "model-v1",
  });
  assert.equal(accepted.ok, true, accepted.error);
  assert.equal(
    readJsonl(path.join(ledgerRoot(valid.runDir), "usage.jsonl"))[0].generated_at,
    "2026-07-11T00:00:00.123Z",
  );
});

test("selective and action arms have identical trigger counts and token-proxy budgets", (t) => {
  const runArm = (mode) => {
    const f = fixture(t, mode);
    const artifact = writeGate(f.runDir, { diagnosis: "same failure" });
    captureGate(f, artifact);
    const base = {
      runDir: f.runDir,
      runId: "r1",
      mode,
      experimentEnabled: true,
      projectRoot: f.root,
    };
    assert.equal(executionStatePromptBlock({ ...base, phase: { id: "implement" }, round: 1 }), "");
    observeExecutionStateInjection({
      ...base,
      phase: { id: "implement" },
      round: 1,
      generatedAt: FIXED_TIME,
    });
    assert.notEqual(executionStatePromptBlock({ ...base, phase: { id: "fix-loop" }, round: 1 }), "");
    observeExecutionStateInjection({
      ...base,
      phase: { id: "fix-loop" },
      round: 1,
      generatedAt: "2026-07-11T00:00:01.000Z",
    });
    writeGate(f.runDir, { diagnosis: "same failure", suffix: " " });
    captureGate(f, artifact, { round: 2, generatedAt: "2026-07-11T00:01:00.000Z" });
    assert.notEqual(executionStatePromptBlock({ ...base, phase: { id: "implement" }, round: 2 }), "");
    observeExecutionStateInjection({
      ...base,
      phase: { id: "implement" },
      round: 2,
      generatedAt: "2026-07-11T00:01:01.000Z",
    });
    return readJson(path.join(ledgerRoot(f.runDir), "metrics.json"));
  };
  const selective = runArm("ledger-selective");
  const action = runArm("action-self-review");
  assert.equal(selective.injected_event_count, 2);
  assert.equal(action.injected_event_count, 2);
  assert.equal(action.injected_token_proxy, selective.injected_token_proxy);
});

test("workflow, records, event chain, and completion are authoritatively replayable", (t) => {
  const f = fixture(t, "ledger-always");
  const artifact = writeGate(f.runDir);
  assert.equal(captureGate(f, artifact).ok, true);
  assert.equal(observeExecutionStateInjection({
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
    experimentEnabled: true,
    phase: { id: "fix-loop" },
    projectRoot: f.root,
    round: 1,
    generatedAt: "2026-07-11T00:00:01.000Z",
    promptBytes: 200,
  }).ok, true);
  const incomplete = validateExecutionStateLedger({
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
  });
  assert.equal(incomplete.ok, true, incomplete.error);
  assert.equal(incomplete.verified, true);
  assert.equal(incomplete.completed, false);
  assert.equal(incomplete.event_count, 2);
  assert.deepEqual(incomplete.reasons, []);
  assert.deepEqual(Object.keys(incomplete.workflow.phases[0]).sort(), [
    "artifact", "cite_lore", "description", "id", "index", "instruction",
    "multi_review", "optional", "pause_after", "prompt", "required_markers", "routes",
  ]);
  assert.equal(incomplete.config.workflow_sha256, incomplete.config.workflow_snapshot_sha256);
  assert.equal(incomplete.captures[0].sequence, 1);
  assert.equal(incomplete.injections[0].sequence, 2);
  assert.equal(
    incomplete.injections[0].previous_content_sha256,
    incomplete.captures[0].content_sha256,
  );
  markNodeComplete(f);
  const total = recordVerifiedUsage(f, {
    eventId: "usage.authoritative-total",
    generatedAt: "2026-07-11T00:00:02.000Z",
    scope: "run-total",
    phaseId: null,
    round: null,
    inputTokens: 100,
    outputTokens: 20,
    additionalTokens: 12,
    latencyMs: 50,
    estimatedCostUsd: "0.1",
    modelId: "model-v1",
  }, "authoritative-total");
  assert.equal(total.ok, true, total.error);
  const complete = validateExecutionStateLedger({
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
    requireCompletion: true,
  });
  assert.equal(complete.ok, true, complete.error);
  assert.equal(complete.completed, true);
  assert.equal(complete.event_count, 3);
  assert.equal(complete.usage[0].additional_token_scope, "condition-total");
  assert.equal(complete.metrics.actual_additional_token_coverage, true);
  const completion = readJson(path.join(ledgerRoot(f.runDir), "completion.json"));
  assert.equal(completion.terminal_state.runtime, "node");
  assert.equal(completion.event_head_sha256, complete.event_head_sha256);
  assert.equal(captureGate(f, artifact, {
    generatedAt: "2026-07-11T00:00:03.000Z",
  }).captured, false);
  const repeatedInjection = observeExecutionStateInjection({
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
    experimentEnabled: true,
    phase: { id: "fix-loop" },
    projectRoot: f.root,
    round: 1,
    generatedAt: "2026-07-11T00:00:01.000Z",
    promptBytes: 200,
  });
  assert.equal(repeatedInjection.ok, true, repeatedInjection.error);
  assert.equal(repeatedInjection.observed, false);
  const postCompletion = observeExecutionStateInjection({
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
    experimentEnabled: true,
    phase: { id: "fix-loop" },
    projectRoot: f.root,
    round: 2,
    generatedAt: "2026-07-11T00:00:03.000Z",
  });
  assert.equal(postCompletion.ok, false);
  assert.match(postCompletion.error, /finalized/);
});

test("commitment validation rejects content tamper, event deletion, and event reorder", (t) => {
  for (const mutation of ["content", "delete", "reorder"]) {
    const f = fixture(t, "artifacts-only");
    const artifact = writeGate(f.runDir);
    assert.equal(captureGate(f, artifact).ok, true);
    assert.equal(observeExecutionStateInjection({
      runDir: f.runDir,
      runId: "r1",
      mode: f.mode,
      experimentEnabled: true,
      phase: { id: "fix-loop" },
      projectRoot: f.root,
      round: 1,
      generatedAt: "2026-07-11T00:00:01.000Z",
    }).ok, true);
    assert.equal(validateExecutionStateLedger({ runDir: f.runDir, runId: "r1", mode: f.mode }).ok, true);
    const root = ledgerRoot(f.runDir);
    if (mutation === "content") {
      const capture = readJsonl(path.join(root, "captures.jsonl"))[0];
      capture.measurement.fix_loop_rounds = 99;
      fs.writeFileSync(path.join(root, "captures.jsonl"), `${JSON.stringify(capture)}\n`, "utf8");
    } else {
      const eventsPath = path.join(root, "events.jsonl");
      const lines = fs.readFileSync(eventsPath, "utf8").trim().split("\n");
      const changed = mutation === "delete" ? lines.slice(1) : lines.reverse();
      fs.writeFileSync(eventsPath, `${changed.join("\n")}\n`, "utf8");
    }
    const validation = validateExecutionStateLedger({ runDir: f.runDir, runId: "r1", mode: f.mode });
    assert.equal(validation.ok, false, mutation);
    assert.equal(validation.verified, false, mutation);
  }
});

test("run-total requires canonical complete terminal evidence before appending", (t) => {
  const f = fixture(t, "artifacts-only");
  const args = {
    runDir: f.runDir,
    runId: "r1",
    mode: f.mode,
    experimentEnabled: true,
    eventId: "usage.terminal-proof",
    generatedAt: FIXED_TIME,
    scope: "run-total",
    phaseId: null,
    round: null,
    inputTokens: 1,
    outputTokens: 0,
    additionalTokens: 0,
    latencyMs: 1,
    estimatedCostUsd: "0",
    modelId: "model-v1",
  };
  assert.match(recordExecutionStateUsage(args).error, /missing or ambiguous/);
  fs.writeFileSync(path.join(f.runDir, "manifest.json"), `${JSON.stringify({
    run_id: "r1", workflow: "default", status: "aborted", phase: "complete",
    phase_index: WORKFLOW_PHASES.length,
  })}\n`, "utf8");
  assert.match(recordExecutionStateUsage(args).error, /not complete/);
  fs.writeFileSync(path.join(f.runDir, "manifest.json"), `${JSON.stringify({
    run_id: "r1", workflow: "default", status: "complete", phase: "complete",
    phase_index: WORKFLOW_PHASES.length - 1,
  })}\n`, "utf8");
  assert.match(recordExecutionStateUsage(args).error, /identity mismatch/);
  assert.equal(readJsonl(path.join(ledgerRoot(f.runDir), "usage.jsonl")).length, 0);
  markNodeComplete(f);
  assert.equal(recordExecutionStateUsage(args).ok, true);
});

test("ledger-always covers every eligible action phase while duplicate rows require a new observation", (t) => {
  const always = fixture(t, "ledger-always");
  const alwaysArtifact = writeGate(always.runDir);
  assert.equal(captureGate(always, alwaysArtifact).ok, true);
  assert.match(executionStatePromptBlock({
    runDir: always.runDir,
    runId: "r1",
    mode: always.mode,
    experimentEnabled: true,
    phase: { id: "implement" },
    projectRoot: always.root,
    round: 1,
  }), /Execution ledger/);
  for (const phaseId of ["comment-authoring", "commit"]) {
    assert.equal(executionStatePromptBlock({
      runDir: always.runDir,
      runId: "r1",
      mode: always.mode,
      experimentEnabled: true,
      phase: { id: phaseId },
      projectRoot: always.root,
      round: 1,
    }), "");
  }
  assert.equal(executionStatePromptBlock({
    runDir: always.runDir,
    runId: "r1",
    mode: always.mode,
    experimentEnabled: true,
    phase: { id: "final-review", multi_review: true },
    projectRoot: always.root,
    round: 1,
  }), "");

  const selective = fixture(t, "ledger-selective");
  const duplicateArtifact = path.join(selective.runDir, "artifacts", "gate-results.json");
  const row = {
    gate_id: "tests",
    argv: ["npm", "test"],
    required: true,
    passed: false,
    exit_code: 1,
    stdout: "",
    stderr: "same failure",
  };
  fs.writeFileSync(duplicateArtifact, `${JSON.stringify({ passed: false, results: [row, row] })}\n`, "utf8");
  assert.equal(captureGate(selective, duplicateArtifact).ok, true);
  const prompt = {
    runDir: selective.runDir,
    runId: "r1",
    mode: selective.mode,
    experimentEnabled: true,
    phase: { id: "implement" },
    projectRoot: selective.root,
    round: 1,
  };
  assert.equal(executionStatePromptBlock(prompt), "");
  assert.equal(captureGate(selective, duplicateArtifact, {
    round: 2,
    fixLoopRounds: 1,
    generatedAt: "2026-07-11T00:01:00.000Z",
  }).ok, true);
  assert.match(executionStatePromptBlock({ ...prompt, round: 2 }), /Before retrying, change the action/);
  const metrics = readJson(path.join(ledgerRoot(selective.runDir), "metrics.json"));
  assert.equal(metrics.repeated_command_fingerprint_count, 3);
  assert.equal(metrics.repeated_failure_fingerprint_count, 1);
});

test("bugfix implement-fix is a first-class selective fix target in every ledger mode", (t) => {
  const workflowPhases = [{ id: "implement-fix" }, ...WORKFLOW_PHASES];
  for (const mode of [
    "artifacts-only",
    "ledger-always",
    "ledger-selective",
    "action-self-review",
  ]) {
    const f = fixture(t, mode, { workflowPhases });
    const artifact = writeGate(f.runDir);
    assert.equal(captureGate(f, artifact).ok, true);
    const block = executionStatePromptBlock({
      runDir: f.runDir,
      runId: "r1",
      mode,
      experimentEnabled: true,
      phase: { id: "implement-fix" },
      projectRoot: f.root,
      round: 1,
    });
    if (mode === "artifacts-only") {
      assert.equal(block, "");
    } else if (mode === "action-self-review") {
      assert.match(block, /Action self-review \(bounded verified artifact\)/);
      assert.doesNotMatch(block, /Execution ledger/);
    } else {
      assert.match(block, /Execution ledger/);
    }
    assert.equal(
      f.config.exposure_policy.selective_fix_phases.includes("implement-fix"),
      true,
    );
    assert.equal(f.config.exposure_policy.eligible_phases.includes("implement-fix"), true);
  }
});

test("overall gate green resolves removed failures and action review inlines the verified review slice", (t) => {
  const gates = fixture(t, "ledger-always");
  const gateArtifact = writeGate(gates.runDir);
  assert.equal(captureGate(gates, gateArtifact).ok, true);
  fs.writeFileSync(gateArtifact, `${JSON.stringify({
    passed: true,
    results: [{
      gate_id: "lint",
      argv: ["npm", "run", "lint"],
      required: true,
      passed: true,
      exit_code: 0,
      stdout: "ok",
      stderr: "",
    }],
  })}\n`, "utf8");
  assert.equal(captureGate(gates, gateArtifact, {
    generatedAt: "2026-07-11T00:01:00.000Z",
    routeKey: "green",
    routedTo: "commit",
  }).ok, true);
  const gateLedger = readJson(path.join(ledgerRoot(gates.runDir), "ledger.json"));
  assert.equal(gateLedger.entries.status[0].resolution.kind, "gate-snapshot-absent");
  assert.equal(executionStatePromptBlock({
    runDir: gates.runDir,
    runId: "r1",
    mode: gates.mode,
    experimentEnabled: true,
    phase: { id: "fix-loop" },
    projectRoot: gates.root,
    round: 2,
  }), "");

  const review = fixture(t, "action-self-review");
  const reviewArtifact = path.join(review.runDir, "artifacts", "final-review.md");
  fs.writeFileSync(reviewArtifact, "# Review\n\n## Findings\n\n- Fix retry order.\n", "utf8");
  const captured = captureExecutionState({
    runDir: review.runDir,
    runId: "r1",
    mode: review.mode,
    experimentEnabled: true,
    phase: {
      id: "final-review",
      multi_review: true,
      routes: { approve: "gates", "request-changes": "fix-loop" },
    },
    artifactPath: reviewArtifact,
    projectRoot: review.root,
    round: 1,
    generatedAt: FIXED_TIME,
    workflowId: "default",
    routeKey: "request-changes",
    routedTo: "fix-loop",
  });
  assert.equal(captured.ok, true, captured.error);
  const capture = readJsonl(path.join(ledgerRoot(review.runDir), "captures.jsonl"))[0];
  const observed = observeExecutionStateInjection({
    runDir: review.runDir,
    runId: "r1",
    mode: review.mode,
    experimentEnabled: true,
    phase: { id: "fix-loop" },
    projectRoot: review.root,
    round: 1,
    generatedAt: "2026-07-11T00:00:01.000Z",
  });
  assert.equal(observed.ok, true, observed.error);
  assert.match(observed.block, /Fix retry order/);
  assert.match(observed.block, new RegExp(capture.source_provenance[1].sha256));
  assert.doesNotMatch(observed.block, /Inspect `|\.agent-flow\//);
});

test("Node start pins an unset experiment off and status or next never creates sidecars", (t) => {
  const project = cliProject(t);
  const active = startCliRun(project, null);
  assert.equal(active.state.ledger_mode, "artifacts-only");
  assert.equal(active.state.experiment_enabled, false);
  assert.equal(fs.existsSync(ledgerRoot(active.runDir)), false);

  const changedEnv = experimentEnv("ledger-always");
  const status = runCli(["status"], active.state.workspace_root, project.home, changedEnv);
  const next = runCli(["run", "next"], active.state.workspace_root, project.home, changedEnv);
  assert.equal(status.status, 0, status.stderr || status.stdout);
  assert.equal(next.status, 0, next.stderr || next.stdout);
  assert.doesNotMatch(next.stdout, /Execution ledger|Action self-review/);
  assert.equal(fs.existsSync(ledgerRoot(active.runDir)), false);
});

test("Node explicit pilot initialization fails closed before committing run state", (t) => {
  const project = cliProject(t);
  const start = runCli(
    ["run", "start", "--task", "invalid pilot", "--workflow", "default", "--run-id", "invalid-pilot"],
    project.root,
    project.home,
    {
      ...experimentEnv("ledger-selective"),
      AGENT_FLOW_EXPERIMENT_TOOL_PERMISSIONS_SHA256: "invalid",
    },
  );

  assert.notEqual(start.status, 0);
  assert.match(start.stderr, /execution ledger pilot initialization failed/);
  assert.equal(fs.existsSync(path.join(project.root, ".agent-flow", "state", "current-run.json")), false);
  assert.equal(fs.existsSync(path.join(project.root, ".agent-flow", "runs", "default", "invalid-pilot")), false);
});

test("Node initial prompt observation failure leaves no active run", (t) => {
  const project = cliProject(t);
  const start = runCli(
    ["run", "start", "--task", "initial observation failure", "--workflow", "default", "--run-id", "initial-observation-failure"],
    project.root,
    project.home,
    {
      ...experimentEnv("ledger-always"),
      AGENT_FLOW_LEDGER_FAULT_AFTER: "target",
    },
  );

  assert.notEqual(start.status, 0);
  assert.match(start.stderr, /execution ledger pilot prompt observation failed/);
  const status = runCli(["status"], project.root, project.home, {});
  assert.notEqual(status.status, 0);
  assert.match(status.stderr, /no active run/);
});

test("Node run next records every repeated prompt exposure in the pinned run", (t) => {
  const project = cliProject(t);
  const run = startCliRun(project, "ledger-always");
  setCliPhase(run, "gates");
  writeGate(run.runDir);
  const advance = runCli(
    ["run", "advance"],
    run.state.workspace_root,
    project.home,
    run.env,
  );
  assert.equal(advance.status, 0, advance.stderr || advance.stdout);
  const injectionPath = path.join(ledgerRoot(run.runDir), "injections.jsonl");
  const before = readJsonl(injectionPath).length;
  const first = runCli(["run", "next"], run.state.workspace_root, project.home, run.env);
  const second = runCli(["run", "next"], run.state.workspace_root, project.home, run.env);
  assert.equal(first.status, 0, first.stderr || first.stdout);
  assert.equal(second.status, 0, second.stderr || second.stdout);
  assert.match(first.stdout, /Execution ledger/);
  assert.match(second.stdout, /Execution ledger/);
  const injections = readJsonl(injectionPath);
  assert.equal(injections.length, before + 2);
  assert.notEqual(injections.at(-2).id, injections.at(-1).id);
  assert.equal(injections.at(-2).phase, "fix-loop");
  assert.equal(injections.at(-1).phase, "fix-loop");
});

test("Node start next and advance phase outputs print the exact next_command", (t) => {
  const project = cliProject(t);
  const run = startCliRun(project, "ledger-always");
  const expected = /^next_command: \.\/\.agent-flow\/bin\/agent-flow run advance$/m;
  assert.match(run.startResult.stdout, expected);

  const next = runCli(["run", "next"], run.state.workspace_root, project.home, run.env);
  assert.equal(next.status, 0, next.stderr || next.stdout);
  assert.match(next.stdout, expected);

  setCliPhase(run, "gates");
  writeGate(run.runDir);
  const advance = runCli(["run", "advance"], run.state.workspace_root, project.home, run.env);
  assert.equal(advance.status, 0, advance.stderr || advance.stdout);
  assert.match(advance.stdout, expected);
});

test("Node prompt observation failure leaves canonical transition uncommitted", (t) => {
  const project = cliProject(t);
  const run = startCliRun(project, "ledger-always");
  setCliPhase(run, "gates");
  const gateArtifact = writeGate(run.runDir);
  const capture = captureExecutionState({
    runDir: run.runDir,
    runId: run.state.run_id,
    mode: "ledger-always",
    experimentEnabled: true,
    phase: {
      id: "gates",
      routes: { green: "commit", "request-changes": "fix-loop" },
    },
    artifactPath: gateArtifact,
    projectRoot: run.state.workspace_root,
    round: 1,
    fixLoopRounds: 1,
    workflowId: run.state.workflow,
    routeKey: "request-changes",
    routedTo: "fix-loop",
    transitionOccurrenceId: runnerTransitionOccurrenceId(run.state, "gates"),
  });
  assert.equal(capture.ok, true, capture.error);
  const injectionPath = path.join(ledgerRoot(run.runDir), "injections.jsonl");
  const before = readJsonl(injectionPath).length;
  const stateBefore = fs.readFileSync(run.statePath);
  const manifestPath = path.join(run.runDir, "manifest.json");
  const manifestBefore = fs.readFileSync(manifestPath);
  const lockPath = path.join(ledgerRoot(run.runDir), "transaction.lock");
  fs.writeFileSync(
    lockPath,
    `${JSON.stringify({ pid: process.pid, token: "live-test-owner" })}\n`,
    "utf8",
  );

  const failed = runCli(["run", "advance"], run.state.workspace_root, project.home, run.env);
  assert.notEqual(failed.status, 0);
  assert.match(failed.stderr, /execution ledger pilot prompt observation failed/);
  assert.equal(fs.readFileSync(run.statePath).equals(stateBefore), true);
  assert.equal(fs.readFileSync(manifestPath).equals(manifestBefore), true);
  assert.equal(readJson(run.statePath).phase, "gates");
  assert.equal(readJson(manifestPath).phase, "gates");
  assert.equal(fs.existsSync(gateArtifact), true);
  assert.equal(readJsonl(injectionPath).length, before);
  const pending = readJson(path.join(run.runDir, "transition-pending.json"));
  assert.equal(pending.capture.committed, true);
  fs.unlinkSync(lockPath);

  const retried = runCli(["run", "advance"], run.state.workspace_root, project.home, run.env);
  assert.equal(retried.status, 0, retried.stderr || retried.stdout);
  assert.match(retried.stdout, /## Execution ledger/);
  assert.equal(readJson(run.statePath).phase, "fix-loop");
  assert.equal(readJsonl(injectionPath).length, before + 1);
});

test("Node transition capture failure leaves canonical state uncommitted and retries", (t) => {
  const project = cliProject(t);
  const run = startCliRun(project, "ledger-always");
  setCliPhase(run, "gates");
  writeGate(run.runDir);
  const stateBefore = fs.readFileSync(run.statePath);
  const manifestPath = path.join(run.runDir, "manifest.json");
  const manifestBefore = fs.readFileSync(manifestPath);
  const lockPath = path.join(ledgerRoot(run.runDir), "transaction.lock");
  fs.writeFileSync(
    lockPath,
    `${JSON.stringify({ pid: process.pid, token: "live-capture-owner" })}\n`,
    "utf8",
  );

  const failed = runCli(["run", "advance"], run.state.workspace_root, project.home, run.env);
  assert.notEqual(failed.status, 0);
  assert.match(failed.stderr, /transition capture failed/);
  assert.equal(fs.readFileSync(run.statePath).equals(stateBefore), true);
  assert.equal(fs.readFileSync(manifestPath).equals(manifestBefore), true);
  assert.equal(readJson(run.statePath).phase, "gates");
  assert.equal(readJson(manifestPath).phase, "gates");
  const pending = readJson(path.join(run.runDir, "transition-pending.json"));
  assert.equal(pending.capture.committed, false);
  fs.unlinkSync(lockPath);

  const retried = runCli(["run", "advance"], run.state.workspace_root, project.home, run.env);
  assert.equal(retried.status, 0, retried.stderr || retried.stdout);
  assert.equal(readJson(run.statePath).phase, "fix-loop");
  const captures = readJsonl(path.join(ledgerRoot(run.runDir), "captures.jsonl"));
  assert.equal(captures.length, 1);
  assert.equal(captures[0].transition_occurrence_id,
    runnerTransitionOccurrenceId(run.state, "gates"));
});

test("Node pending transition rolls back a manifest-only partial publish", (t) => {
  const project = cliProject(t);
  const run = startCliRun(project, "ledger-always");
  setCliPhase(run, "gates");
  writeGate(run.runDir);
  const canonicalBefore = readJson(run.statePath);

  const failed = runCli(
    ["run", "advance"],
    run.state.workspace_root,
    project.home,
    {
      ...run.env,
      AGENT_FLOW_TEST_FAIL_AFTER_NODE_TRANSITION_MANIFEST: "1",
    },
  );
  assert.notEqual(failed.status, 0);
  assert.match(failed.stderr, /after manifest publish/);
  assert.deepEqual(readJson(run.statePath), canonicalBefore);
  assert.deepEqual(readJson(path.join(run.runDir, "manifest.json")), canonicalBefore);
  const pendingPath = path.join(run.runDir, "transition-pending.json");
  assert.equal(readJson(pendingPath).capture.committed, true);
  assert.equal(readJsonl(path.join(ledgerRoot(run.runDir), "captures.jsonl")).length, 1);
  const injectionCount = readJsonl(path.join(ledgerRoot(run.runDir), "injections.jsonl")).length;
  const status = runCli(["status"], run.state.workspace_root, project.home, run.env);
  assert.equal(status.status, 0, status.stderr || status.stdout);
  assert.match(status.stdout, /current_phase: gates/);

  const retried = runCli(["run", "advance"], run.state.workspace_root, project.home, run.env);
  assert.equal(retried.status, 0, retried.stderr || retried.stdout);
  assert.equal(readJson(run.statePath).phase, "fix-loop");
  assert.equal(readJson(path.join(run.runDir, "manifest.json")).phase, "fix-loop");
  assert.equal(fs.existsSync(pendingPath), false);
  assert.equal(readJsonl(path.join(ledgerRoot(run.runDir), "captures.jsonl")).length, 1);
  assert.equal(readJsonl(path.join(ledgerRoot(run.runDir), "injections.jsonl")).length, injectionCount);
});

test("Node explicit pilot rejects a changed live workflow before prompting", (t) => {
  const project = cliProject(t);
  const run = startCliRun(project, "ledger-always");
  const copiedKit = path.join(path.dirname(project.root), "mutated-kit");
  for (const directory of ["bin", "lib", "profiles", "workflows"]) {
    fs.cpSync(path.join(KIT_ROOT, directory), path.join(copiedKit, directory), { recursive: true });
  }
  const workflow = path.join(copiedKit, "workflows", "default.yaml");
  const original = fs.readFileSync(workflow, "utf8");
  assert.match(original, /Unified design phase/);
  fs.writeFileSync(workflow, original.replace("Unified design phase", "Changed design phase"), "utf8");
  const injections = path.join(ledgerRoot(run.runDir), "injections.jsonl");
  const before = readJsonl(injections).length;

  const next = spawnSync(process.execPath, [path.join(copiedKit, "bin", "agent-flow-kit.mjs"), "run", "next"], {
    cwd: run.state.workspace_root,
    encoding: "utf8",
    env: {
      ...process.env,
      HOME: project.home,
      AGENT_FLOW_SKIP_CODEX_TRUST: "1",
      ...run.env,
    },
  });

  assert.notEqual(next.status, 0);
  assert.match(next.stderr, /active pilot runner workflow snapshot changed/);
  assert.equal(readJsonl(injections).length, before);
});

test("Node command keeps one verified workflow snapshot when YAML changes after load", async (t) => {
  const project = cliProject(t);
  const run = startCliRun(project, "ledger-always");
  const copiedKit = path.join(path.dirname(project.root), "snapshot-kit");
  for (const directory of ["bin", "lib", "profiles", "workflows"]) {
    fs.cpSync(path.join(KIT_ROOT, directory), path.join(copiedKit, directory), { recursive: true });
  }
  const workflow = path.join(copiedKit, "workflows", "default.yaml");
  const marker = path.join(run.runDir, "logs", "workflow-snapshot-ready");
  const stateBefore = fs.readFileSync(run.statePath);
  const manifestPath = path.join(run.runDir, "manifest.json");
  const manifestBefore = fs.readFileSync(manifestPath);
  const child = spawn(
    process.execPath,
    [path.join(copiedKit, "bin", "agent-flow-kit.mjs"), "run", "next"],
    {
      cwd: run.state.workspace_root,
      env: {
        ...process.env,
        HOME: project.home,
        AGENT_FLOW_SKIP_CODEX_TRUST: "1",
        AGENT_FLOW_TEST_HOLD_AFTER_WORKFLOW_LOAD_MS: "1200",
        ...run.env,
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  let stdout = "";
  let stderr = "";
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const completion = new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("close", (status, signal) => resolve({ status, signal }));
  });

  const deadline = Date.now() + 2_000;
  while (!fs.existsSync(marker) && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.equal(fs.existsSync(marker), true, `workflow snapshot marker missing\n${stderr}`);
  const original = fs.readFileSync(workflow, "utf8");
  assert.match(original, /Produce the unified design document\./);
  fs.writeFileSync(
    workflow,
    original.replace("Produce the unified design document.", "MUTATED BETWEEN LOAD AND USE."),
    "utf8",
  );

  const result = await completion;
  assert.equal(result.status, 0, stderr || stdout || `signal=${result.signal}`);
  assert.match(stdout, /Produce the unified design document\./);
  assert.doesNotMatch(stdout, /MUTATED BETWEEN LOAD AND USE/);
  assert.equal(fs.readFileSync(run.statePath).equals(stateBefore), true);
  assert.equal(fs.readFileSync(manifestPath).equals(manifestBefore), true);
});

test("Node advance captures route evidence and resume ignores ledger env changes", (t) => {
  const project = cliProject(t);
  const active = startCliRun(project, "ledger-always");
  assert.equal(active.state.ledger_mode, "ledger-always");
  assert.equal(active.state.experiment_enabled, true);
  setCliPhase(active, "gates");
  const gate = writeGate(active.runDir, {
    diagnosis: `${active.state.workspace_root}/src/index.js integration failure`,
  });
  const original = fs.readFileSync(gate);

  const advance = runCli(
    ["run", "advance"],
    active.state.workspace_root,
    project.home,
    experimentEnv("action-self-review"),
  );
  assert.equal(advance.status, 0, advance.stderr || advance.stdout);
  assert.match(advance.stdout, /Current phase: fix-loop/);
  assert.match(advance.stdout, /## Execution ledger \(advisory; run ledger-cli, round 1\/3\)/);
  assert.doesNotMatch(advance.stdout, /## Action self-review/);
  const transitioned = readJson(active.statePath);
  assert.equal(transitioned.phase, "fix-loop");
  assert.equal(transitioned.fix_loop_rounds, 1);
  assert.equal(transitioned.ledger_mode, "ledger-always");
  assert.equal(fs.existsSync(gate), true);
  const capture = readJsonl(path.join(ledgerRoot(active.runDir), "captures.jsonl")).at(-1);
  assert.equal(capture.round, 1);
  assert.equal(fs.readFileSync(path.join(active.runDir, capture.source_provenance[0].archive_path)).equals(original), true);
  const ledger = readJson(path.join(ledgerRoot(active.runDir), "ledger.json"));
  assert.doesNotMatch(ledger.entries.status[0].payload.diagnosis, new RegExp(active.state.workspace_root));
  assert.equal(readJson(path.join(ledgerRoot(active.runDir), "metrics.json")).fix_loop_rounds, 1);

  active.state = transitioned;
  setCliPhase(active, "gates");
  writeGate(active.runDir, { diagnosis: "integration failure", suffix: " again" });
  const secondAdvance = runCli(["run", "advance"], active.state.workspace_root, project.home, {});
  assert.equal(secondAdvance.status, 0, secondAdvance.stderr || secondAdvance.stdout);
  assert.match(secondAdvance.stdout, /## Execution ledger \(advisory; run ledger-cli, round 2\/3\)/);
  const secondTransition = readJson(active.statePath);
  assert.equal(secondTransition.phase, "fix-loop");
  assert.equal(secondTransition.fix_loop_rounds, 2);
  const secondCapture = readJsonl(path.join(ledgerRoot(active.runDir), "captures.jsonl")).at(-1);
  assert.equal(secondCapture.round, 2);
  assert.equal(readJson(path.join(ledgerRoot(active.runDir), "metrics.json")).fix_loop_rounds, 2);

  const captureBytes = fs.readFileSync(path.join(ledgerRoot(active.runDir), "captures.jsonl"));
  const beforeInjectionCount = readJsonl(path.join(ledgerRoot(active.runDir), "injections.jsonl")).length;
  const beforeEventCount = readJsonl(path.join(ledgerRoot(active.runDir), "events.jsonl")).length;
  const next = runCli(["run", "next"], active.state.workspace_root, project.home, experimentEnv("artifacts-only"));
  const afterNext = {
    "captures.jsonl": fs.readFileSync(path.join(ledgerRoot(active.runDir), "captures.jsonl")),
    "injections.jsonl": fs.readFileSync(path.join(ledgerRoot(active.runDir), "injections.jsonl")),
    "events.jsonl": fs.readFileSync(path.join(ledgerRoot(active.runDir), "events.jsonl")),
    "metrics.json": fs.readFileSync(path.join(ledgerRoot(active.runDir), "metrics.json")),
  };
  const status = runCli(["status"], active.state.workspace_root, project.home, experimentEnv("artifacts-only"));
  assert.equal(next.status, 0, next.stderr || next.stdout);
  assert.equal(status.status, 0, status.stderr || status.stdout);
  assert.equal(afterNext["captures.jsonl"].equals(captureBytes), true);
  assert.equal(readJsonl(path.join(ledgerRoot(active.runDir), "injections.jsonl")).length, beforeInjectionCount + 1);
  assert.equal(readJsonl(path.join(ledgerRoot(active.runDir), "events.jsonl")).length, beforeEventCount + 1);
  for (const [file, bytes] of Object.entries(afterNext)) {
    assert.equal(fs.readFileSync(path.join(ledgerRoot(active.runDir), file)).equals(bytes), true, file);
  }
});

test("Node route artifact failure keeps canonical state and retries idempotently", (t) => {
  const project = cliProject(t);
  const active = startCliRun(project, "ledger-always", "full-feature", "ledger-backward");
  setCliPhase(active, "plan-review");
  const artifact = path.join(active.runDir, "artifacts", "plan-review.md");
  fs.writeFileSync(
    artifact,
    "# Plan Review\n\n## Findings\n\n- Split the oversized checkout slice.\n\nverdict: request-changes\n",
    "utf8",
  );
  const original = fs.readFileSync(artifact);

  const failed = runCli(
    ["run", "advance"],
    active.state.workspace_root,
    project.home,
    { AGENT_FLOW_TEST_FAIL_NODE_TRANSITION_ROUTE_ARTIFACT: "1" },
  );
  assert.notEqual(failed.status, 0);
  assert.match(failed.stderr, /route artifact failure/);
  assert.equal(readJson(active.statePath).phase, "plan-review");
  assert.equal(readJson(path.join(active.runDir, "manifest.json")).phase, "plan-review");
  assert.equal(fs.existsSync(artifact), true);
  const pendingPath = path.join(active.runDir, "transition-pending.json");
  assert.equal(readJson(pendingPath).capture.committed, true);
  const captures = readJsonl(path.join(ledgerRoot(active.runDir), "captures.jsonl"));
  assert.equal(captures.length, 1);
  const provenance = captures.at(-1).source_provenance.find(
    (source) => source.original_path === "artifacts/plan-review.md",
  );
  assert.ok(provenance);
  assert.equal(fs.readFileSync(path.join(active.runDir, provenance.archive_path)).equals(original), true);
  const injectionCount = readJsonl(path.join(ledgerRoot(active.runDir), "injections.jsonl")).length;

  const retried = runCli(["run", "advance"], active.state.workspace_root, project.home, {});
  assert.equal(retried.status, 0, retried.stderr || retried.stdout);
  assert.equal(readJson(active.statePath).phase, "slice-plan");
  assert.equal(readJson(path.join(active.runDir, "manifest.json")).phase, "slice-plan");
  assert.equal(fs.existsSync(artifact), false);
  assert.equal(fs.existsSync(pendingPath), false);
  assert.equal(readJsonl(path.join(ledgerRoot(active.runDir), "captures.jsonl")).length, 1);
  assert.equal(readJsonl(path.join(ledgerRoot(active.runDir), "injections.jsonl")).length, injectionCount);
});

test("corrupt Node ledger never changes the canonical route", (t) => {
  const project = cliProject(t);
  const active = startCliRun(project, "ledger-always");
  setCliPhase(active, "gates");
  writeGate(active.runDir, { diagnosis: "route must survive" });
  fs.writeFileSync(path.join(ledgerRoot(active.runDir), "ledger.json"), "{broken", "utf8");

  const advance = runCli(["run", "advance"], active.state.workspace_root, project.home, {});
  assert.equal(advance.status, 0, advance.stderr || advance.stdout);
  assert.equal(readJson(active.statePath).phase, "fix-loop");
  assert.doesNotMatch(advance.stdout, /Execution ledger|Action self-review/);
});
