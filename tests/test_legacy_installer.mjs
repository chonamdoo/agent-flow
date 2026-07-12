import assert from "node:assert/strict";
import fs from "node:fs";
import { createHash } from "node:crypto";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { initializeExecutionStateLedger } from "../lib/execution-state-ledger.mjs";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const CANONICAL = path.join(ROOT, "bin", "agent-flow-kit.mjs");
const LEGACY = path.join(ROOT, "bin", "agent-flow-install.mjs");
const LEGACY_MIGRATION_BASE = "997cbfcfcaffa2a249c0e7903797e4cf0a7c4a4b";

function run(installer, cwd, args = ["install"]) {
  return spawnSync(process.execPath, [installer, ...args], {
    cwd,
    encoding: "utf8",
    env: { ...process.env, AGENT_FLOW_SKIP_CODEX_TRUST: "1" },
  });
}

test("legacy installer preserves canonical managed Claude worktree guard", () => {
  for (const installer of [CANONICAL, LEGACY]) {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-claude-worktree-"));
    try {
      const worktree = path.join(root, ".claude", "worktrees", "feat-test");
      fs.mkdirSync(worktree, { recursive: true });
      const result = run(installer, worktree);
      assert.notEqual(result.status, 0);
      assert.match(result.stderr, /managed worktree install blocked/);
      assert.equal(fs.existsSync(path.join(root, ".agent-flow", "kit.json")), false);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  }
});

test("legacy installer forwards stdout stderr exit and skill arguments", () => {
  const roots = [
    fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-canonical-install-")),
    fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-legacy-install-")),
  ];
  try {
    const canonical = run(CANONICAL, roots[0]);
    const legacy = run(LEGACY, roots[1]);
    assert.equal(canonical.status, 0, canonical.stderr);
    assert.equal(legacy.status, canonical.status, legacy.stderr);
    assert.match(canonical.stdout, /agent-flow installed profile=generic/);
    assert.match(legacy.stdout, /agent-flow installed profile=generic/);

    const canonicalMissing = run(CANONICAL, roots[0], ["install", "--skill", "install"]);
    const legacyMissing = run(LEGACY, roots[1], ["install", "--skill", "install"]);
    assert.notEqual(canonicalMissing.status, 0);
    assert.equal(legacyMissing.status, canonicalMissing.status);
    assert.match(canonicalMissing.stderr, /missing required profile skills: install/);
    assert.match(legacyMissing.stderr, /missing required profile skills: install/);
  } finally {
    for (const root of roots) fs.rmSync(root, { recursive: true, force: true });
  }
});

test("current installer authenticates and upgrades the supported legacy snapshot", {
  skip: process.platform === "win32",
  timeout: 60_000,
}, (t) => {
  const available = spawnSync("git", ["cat-file", "-e", `${LEGACY_MIGRATION_BASE}^{commit}`], {
    cwd: ROOT,
    encoding: "utf8",
  });
  if (available.status !== 0) {
    t.skip("supported legacy base commit is unavailable in this checkout");
    return;
  }
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-supported-legacy-upgrade-"));
  t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));
  const legacyKit = path.join(sandbox, "legacy-kit");
  const project = path.join(sandbox, "project");
  const archive = path.join(sandbox, "legacy.tar");
  fs.mkdirSync(legacyKit);
  fs.mkdirSync(project);
  const archived = spawnSync(
    "git",
    ["archive", "--format=tar", `--output=${archive}`, LEGACY_MIGRATION_BASE],
    { cwd: ROOT, encoding: "utf8" },
  );
  assert.equal(archived.status, 0, archived.stderr);
  const extracted = spawnSync("tar", ["-xf", archive, "-C", legacyKit], { encoding: "utf8" });
  assert.equal(extracted.status, 0, extracted.stderr);

  const legacyInstall = run(path.join(legacyKit, "bin", "agent-flow-kit.mjs"), project, [
    "install", "--profile", "node",
  ]);
  assert.equal(legacyInstall.status, 0, legacyInstall.stderr);
  const upgrade = run(CANONICAL, project, ["install", "--profile", "node"]);
  assert.equal(upgrade.status, 0, upgrade.stderr);
  const kit = JSON.parse(fs.readFileSync(path.join(project, ".agent-flow", "kit.json"), "utf8"));
  assert.deepEqual(kit.migrated_from, {
    kind: "legacy-uncommitted-install",
    base_commit: LEGACY_MIGRATION_BASE,
  });
  assert.equal(kit.project_runtime_contract.version, 2);
  assert.equal(kit.managed_hook_contract.version, 2);
});

test("legacy installer forwards experiment usage help", () => {
  const roots = [
    fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-canonical-experiment-")),
    fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-legacy-experiment-")),
  ];
  try {
    const canonical = run(CANONICAL, roots[0], ["experiment", "--help"]);
    const legacy = run(LEGACY, roots[1], ["experiment", "--help"]);
    assert.equal(canonical.status, 0, canonical.stderr);
    assert.equal(legacy.status, canonical.status, legacy.stderr);
    assert.match(canonical.stdout, /record-usage/);
    assert.match(legacy.stdout, /record-usage/);
    const canonicalUsage = run(CANONICAL, roots[0], ["experiment", "record-usage", "--help"]);
    const legacyUsage = run(LEGACY, roots[1], ["experiment", "record-usage", "--help"]);
    assert.equal(canonicalUsage.status, 0, canonicalUsage.stderr);
    assert.equal(legacyUsage.status, canonicalUsage.status, legacyUsage.stderr);
    assert.match(canonicalUsage.stdout, /condition-total additional input tokens/);
    assert.match(legacyUsage.stdout, /condition-total additional input tokens/);
  } finally {
    for (const root of roots) fs.rmSync(root, { recursive: true, force: true });
  }
});

test("public and legacy CLIs record run-total usage through the native runtime", () => {
  for (const installer of [CANONICAL, LEGACY]) {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-experiment-usage-"));
    try {
      const runDir = path.join(root, "run");
      fs.mkdirSync(runDir, { recursive: true });
      const initialized = initializeExecutionStateLedger({
        runDir,
        runId: "usage-smoke",
        mode: "ledger-always",
        experimentEnabled: true,
        task: "record usage",
        workflowId: "default",
        workflowPhases: [{ id: "usage-complete" }],
        baseCommit: "a".repeat(40),
        experiment: {
          experiment_id: "experiment-1",
          model_id: "model-1",
          tool_permissions_sha256: "b".repeat(64),
          system_prompt_sha256: "c".repeat(64),
          caps_sha256: "d".repeat(64),
        },
        runSnapshot: {
          runtime_id: "test",
          profile_snapshot_sha256: "e".repeat(64),
          installed_skill_plan_sha256: "f".repeat(64),
          local_skill_plan_sha256: "1".repeat(64),
          lore_snapshot_sha256: "2".repeat(64),
          prompt_controls_sha256: "3".repeat(64),
        },
      });
      assert.equal(initialized.ok, true, initialized.error);
      fs.writeFileSync(path.join(runDir, "manifest.json"), `${JSON.stringify({
        run_id: "usage-smoke",
        workflow: "default",
        status: "complete",
        phase: "complete",
        phase_index: 1,
      })}\n`, "utf8");
      const receiptPath = path.join(runDir, "artifacts", "provider-usage-receipt.json");
      const receiptBytes = Buffer.from(`${JSON.stringify({
        schema_version: 1,
        kind: "provider-usage-receipt",
        provider: "test-provider",
        request_id: "request-run-total-smoke",
        receipt_id: "receipt-run-total-smoke",
        event_id: "run-total-smoke",
        run_id: "usage-smoke",
        generated_at: "2026-07-11T00:00:00.000Z",
        scope: "run-total",
        phase_id: null,
        round: null,
        model_id: "model-1",
        control_receipt: {
          schema_version: 1,
          kind: "execution-control-receipt",
          control_receipt_id: "control-run-total-smoke",
          run_id: "usage-smoke",
          experiment_id: "experiment-1",
          model_id: "model-1",
          runner_control_snapshot_sha256: initialized.config.runner_control_snapshot_sha256,
          tool_permissions_sha256: "b".repeat(64),
          system_prompt_sha256: "c".repeat(64),
          caps_sha256: "d".repeat(64),
          exposure_policy_sha256: initialized.config.exposure_policy_sha256,
          per_exposure_token_cap: initialized.config.per_exposure_token_cap,
        },
        usage: {
          input_tokens: 100,
          output_tokens: 20,
          additional_tokens: 5,
          additional_token_scope: "condition-total",
          latency_ms: 50,
          estimated_cost_usd: "0.0100",
        },
      })}\n`, "utf8");
      fs.writeFileSync(receiptPath, receiptBytes);
      const result = run(installer, root, [
        "experiment", "record-usage",
        "--run-dir", runDir,
        "--receipt", receiptPath,
        "--receipt-sha256", createHash("sha256").update(receiptBytes).digest("hex"),
      ]);
      assert.equal(result.status, 0, result.stderr);
      assert.match(result.stdout, /"recorded": true/);
      const metrics = JSON.parse(fs.readFileSync(
        path.join(runDir, "artifacts", "execution-ledger", "metrics.json"),
        "utf8",
      ));
      assert.equal(metrics.actual_usage_coverage, true);
      assert.equal(metrics.actual_total_tokens, 120);
      assert.equal(metrics.actual_additional_tokens, 5);
      assert.equal(metrics.actual_additional_token_coverage, true);
      assert.equal(metrics.estimated_cost, "0.01");
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  }
});
