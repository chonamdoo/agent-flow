import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { lintProfiles } from "../lib/architecture-lint.mjs";
import {
  initializeExecutionStateLedger,
  recordExecutionStateUsage,
} from "../lib/execution-state-ledger.mjs";
import {
  experimentHelpFromArgs,
  parseRecordUsageArgs,
  pythonJson,
  recordUsageFromArgs,
} from "../lib/native-experiment.mjs";
import {
  createCanonicalProfileLoader,
  nativeGateHelpFromArgs,
  parseArchitectureLintArgs,
  parseGatesArgs,
  profileGateCommands,
  runGate,
  writeGateResults,
} from "../lib/native-gates.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const NODE_CLI = path.join(ROOT, "bin", "agent-flow-kit.mjs");
const PYTHON_ENV = { ...process.env, PYTHONPATH: path.join(ROOT, "src") };
const loadProfile = createCanonicalProfileLoader(path.join(ROOT, "profiles"));

test("native command option parsing preserves files and gate controls", () => {
  assert.deepEqual(
    parseArchitectureLintArgs(["--files", "src/a.ts", "src/b.ts", "--profile", "typescript", "--worktree", "feat-a"]),
    { root: ".", profile: "typescript", files: ["src/a.ts", "src/b.ts"], worktree: "feat-a" },
  );
  assert.deepEqual(
    parseGatesArgs(["--root", "project", "--timeout", "12", "--run-dir", "run"]),
    { root: "project", profile: "auto", runDir: "run", timeoutSeconds: 12, worktree: null },
  );
  assert.deepEqual(
    parseGatesArgs(["--root=project", "--timeout=12", "--run-dir=run"]),
    { root: "project", profile: "auto", runDir: "run", timeoutSeconds: 12, worktree: null },
  );
});

test("native architecture-lint and gates help/error precedence matches argparse", () => {
  for (const command of ["architecture-lint", "gates"]) {
    assert.match(nativeGateHelpFromArgs(command, ["-h"]), /usage: agent-flow/);
    assert.match(nativeGateHelpFromArgs(command, ["--bogus", "--help"]), /usage: agent-flow/);
    assert.equal(nativeGateHelpFromArgs(command, ["--root", "--help"]), null);
  }
  assert.match(
    nativeGateHelpFromArgs("architecture-lint", ["--files", "src/a.ts", "--help"]),
    /architecture-lint/,
  );
  assert.equal(nativeGateHelpFromArgs("gates", ["--timeout", "invalid", "--help"]), null);
  assert.equal(nativeGateHelpFromArgs("gates", ["--timeout=invalid", "--help"]), null);
  assert.equal(nativeGateHelpFromArgs("gates", ["--root=--help"]), null);
  assert.equal(nativeGateHelpFromArgs("architecture-lint", ["--files=--help"]), null);
  assert.equal(parseGatesArgs(["--root=--help"]).root, "--help");
  assert.deepEqual(parseArchitectureLintArgs(["--files=--help"]).files, ["--help"]);
  assert.throws(() => parseArchitectureLintArgs(["--root"]), /expected one argument/);
  assert.throws(() => parseArchitectureLintArgs(["--root", "--bogus"]), /expected one argument/);
  assert.throws(() => parseArchitectureLintArgs(["--files", "-x"]), /unrecognized arguments/);
  assert.deepEqual(parseArchitectureLintArgs(["--files", "-1"]), {
    root: ".", profile: "auto", files: ["-1"], worktree: null,
  });
  assert.throws(() => parseGatesArgs(["--timeout", "invalid"]), /invalid int value/);
  assert.throws(() => parseGatesArgs(["--bogus"]), /unrecognized arguments/);

  for (const [args, status, streamPattern] of [
    [["architecture-lint", "-h"], 0, /architecture-lint/],
    [["gates", "--bogus", "--help"], 0, /usage: agent-flow gates/],
    [["architecture-lint", "--root", "--help"], 2, /expected one argument/],
    [["gates", "--timeout", "invalid", "--help"], 2, /invalid int value/],
    [["gates", "--timeout=invalid", "--help"], 2, /invalid int value/],
  ]) {
    const result = spawnSync(process.execPath, [NODE_CLI, ...args], { encoding: "utf8" });
    assert.equal(result.status, status, `${args.join(" ")}: ${result.stderr}`);
    assert.match(status === 0 ? result.stdout : result.stderr, streamPattern);
    assert.equal(status === 0 ? result.stderr : result.stdout, "");
  }
});

test("native experiment help follows argparse command ordering", () => {
  assert.match(experimentHelpFromArgs(["--help", "bogus"]), /record-usage/);
  assert.match(experimentHelpFromArgs(["-h", "bogus"]), /record-usage/);
  assert.match(experimentHelpFromArgs(["record-usage", "bogus", "--help"]), /condition-total/);
  for (const option of ["--model-id", "--run-dir", "--scope"]) {
    assert.equal(experimentHelpFromArgs(["record-usage", option, "--help"]), null);
    assert.equal(experimentHelpFromArgs(["record-usage", option, "-h"]), null);
  }
  for (const args of [
    ["--scope", "nope"],
    ["--round", "abc"],
    ["--round", "-0.5"],
    ["--input-tokens", "1e3"],
    ["--model-id", "-1e3"],
  ]) {
    assert.equal(experimentHelpFromArgs(["record-usage", ...args, "--help"]), null);
  }
  assert.match(
    experimentHelpFromArgs(["record-usage", "--model-id", "-.5", "--help"]),
    /condition-total/,
  );
  assert.equal(experimentHelpFromArgs(["bogus", "--help"]), null);
  assert.equal(experimentHelpFromArgs(["bogus", "-h"]), null);
  assert.equal(experimentHelpFromArgs(["record-usage", "--round=abc", "--help"]), null);
  assert.equal(experimentHelpFromArgs(["record-usage", "--model-id=--help"]), null);
  assert.deepEqual(
    parseRecordUsageArgs([
      "record-usage",
      "--run-dir=first",
      "--round=1",
      "--run-dir",
      "second",
    ]),
    { "--run-dir": "second", "--round": 1 },
  );
  assert.throws(
    () => parseRecordUsageArgs([
      "record-usage", "--run-dir=run", "--scope=invalid", "--scope=phase",
    ]),
    /invalid choice/,
  );
  const invalidAttached = spawnSync(
    process.execPath,
    [NODE_CLI, "experiment", "record-usage", "--round=bad", "--help"],
    { encoding: "utf8" },
  );
  assert.equal(invalidAttached.status, 2);
  assert.equal(invalidAttached.stdout, "");
  assert.match(invalidAttached.stderr, /invalid int value/);
});

test("native Node gate plans preserve Python ordering and required semantics", () => {
  const pythonExecutable = runPython("import sys; print(sys.executable)").stdout.trim();
  for (const profileIds of [["typescript"], ["react-native", "android"], ["python", "nextjs"]]) {
    const nodePlan = profileGateCommands(profileIds, { loadProfile, pythonExecutable });
    const python = runPython(
      [
        "import json,sys",
        "from agent_flow.cli import _profile_gate_commands",
        "ids=json.loads(sys.argv[1])",
        "out=[]",
        "for item in _profile_gate_commands(ids):",
        " command=list(item.command)",
        " if item.gate_id == 'architecture-lint': command=['./.agent-flow/bin/agent-flow','architecture-lint','--profile',','.join(ids)]",
        " out.append({'gate_id':item.gate_id,'command':command,'required':item.required})",
        "print(json.dumps(out,separators=(',',':')))",
      ].join("\n"),
      [JSON.stringify(profileIds)],
    );
    assert.deepEqual(nodePlan, JSON.parse(python.stdout));
  }
});

test("native Node gate timeout and missing executable results match Python", () => {
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-native-gate-errors-"));
  try {
    for (const item of [
      { gate_id: "missing", command: ["agent-flow-command-that-does-not-exist"], required: true, timeout: 10 },
      { gate_id: "timeout", command: [process.execPath, "-e", "setTimeout(() => {}, 1000)"], required: false, timeout: 0 },
    ]) {
      const nodeResult = runGate(item, { cwd: fixture, timeoutSeconds: item.timeout });
      const python = runPython(
        [
          "import json,sys",
          "from dataclasses import asdict",
          "from pathlib import Path",
          "from agent_flow.core.gates import GateCommand,run_gate",
          "item=json.loads(sys.argv[2])",
          "result=run_gate(GateCommand(item['gate_id'],tuple(item['command']),required=item['required']),cwd=Path(sys.argv[1]),timeout_s=item['timeout'])",
          "print(json.dumps(asdict(result),separators=(',',':')))",
        ].join("\n"),
        [fixture, JSON.stringify(item)],
      );
      assert.deepEqual(nodeResult, JSON.parse(python.stdout));
    }
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true });
  }
});

test("native Node architecture findings match Python fixtures exactly", () => {
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-native-lint-"));
  try {
    const files = [
      "core/domain/billing/src/main/java/com/example/app/core/data/billing/BillingDto.kt",
      "android/app/src/main/java/com/example/CheckoutDTO.kt",
      "src/features/checkout/presentation/Checkout.tsx",
      "components/Button.tsx",
    ];
    writeFixture(fixture, files[0], "package com.example.app.core.data.billing\nclass BillingDto\n");
    writeFixture(fixture, files[1], "package com.example\nclass CheckoutDTO\n");
    writeFixture(fixture, files[2], "import { CheckoutEntity } from '../data/CheckoutEntity'\n");
    writeFixture(fixture, files[3], "export function Button() { return null }\n");
    const profiles = ["android", "react-native", "nextjs"];
    const nodeFindings = lintProfiles(fixture, profiles, { files, loadProfile });
    const python = runPython(
      [
        "import json,sys",
        "from dataclasses import asdict",
        "from pathlib import Path",
        "from agent_flow.core.architecture_lint import lint_profiles",
        "profiles=json.loads(sys.argv[2]); files=json.loads(sys.argv[3])",
        "result=lint_profiles(Path(sys.argv[1]), profiles, files=files)",
        "print(json.dumps({key:[asdict(item) for item in value] for key,value in result.items()},separators=(',',':')))",
      ].join("\n"),
      [fixture, JSON.stringify(profiles), JSON.stringify(files)],
    );
    assert.deepEqual(nodeFindings, JSON.parse(python.stdout));
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true });
  }
});

test("native Node gate artifact and run report bytes match Python", () => {
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-native-artifacts-"));
  const nodeRun = path.join(fixture, "node");
  const pythonRun = path.join(fixture, "python");
  const results = [
    {
      gate_id: "architecture-lint",
      command: ["agent-flow", "architecture-lint"],
      passed: true,
      exit_code: 0,
      stdout: "한글😀\n",
      stderr: "",
      required: true,
    },
    {
      gate_id: "lint",
      command: ["ruff", "check", "."],
      passed: false,
      exit_code: null,
      stdout: "",
      stderr: "missing",
      required: false,
    },
  ];
  try {
    for (const runDir of [nodeRun, pythonRun]) {
      fs.mkdirSync(path.join(runDir, "artifacts"), { recursive: true });
      fs.writeFileSync(
        path.join(runDir, "manifest.json"),
        JSON.stringify({ run_id: "fixture", workflow: "full-feature", task: "한글😀" }),
      );
      fs.writeFileSync(
        path.join(runDir, "artifacts", "implement.md"),
        "# Stage Result: implement\n\n- Status: completed\n- Evidence Type: observed\n- Confidence: high\n",
      );
      fs.writeFileSync(
        path.join(runDir, "review-summary.json"),
        JSON.stringify({ verdict: "needs_changes", findings: [{ id: 1 }] }),
      );
    }
    writeGateResults(nodeRun, results);
    runPython(
      [
        "import json,sys",
        "from pathlib import Path",
        "from agent_flow.core.artifacts import write_gate_results",
        "from agent_flow.core.gates import GateResult",
        "items=json.loads(sys.argv[2])",
        "results=[GateResult(item['gate_id'],tuple(item['command']),item['passed'],item['exit_code'],item['stdout'],item['stderr'],required=item['required']) for item in items]",
        "write_gate_results(run_dir=Path(sys.argv[1]),results=results)",
      ].join("\n"),
      [pythonRun, JSON.stringify(results)],
    );
    for (const relative of ["artifacts/gate-results.json", "gate-results.json", "RUN_REPORT.md"]) {
      assert.deepEqual(fs.readFileSync(path.join(nodeRun, relative)), fs.readFileSync(path.join(pythonRun, relative)));
    }
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true });
  }
});

test("native record-usage dispatch calls the deterministic ledger implementation directly", () => {
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-native-usage-"));
  const directRoot = path.join(fixture, "direct");
  const commandRoot = path.join(fixture, "command");
  const relativeRunDir = path.join(".agent-flow", "runs", "default", "r1");
  const directRunDir = path.join(directRoot, relativeRunDir);
  const commandRunDir = path.join(commandRoot, relativeRunDir);
  const initialize = (runDir) => initializeExecutionStateLedger({
    runDir,
    runId: "r1",
    mode: "ledger-selective",
    experimentEnabled: true,
    task: "fixture",
    workflowId: "default",
    workflowPhases: [{ id: "fix-loop", routes: { default: "gates" } }, { id: "gates", routes: { green: "complete" } }],
    baseCommit: "1".repeat(40),
    experiment: {
      experiment_id: "pilot",
      model_id: "model-v1",
      tool_permissions_sha256: "a".repeat(64),
      system_prompt_sha256: "b".repeat(64),
      caps_sha256: "c".repeat(64),
    },
    runSnapshot: {
      runtime_id: "test",
      profile_snapshot_sha256: "d".repeat(64),
      installed_skill_plan_sha256: "e".repeat(64),
      local_skill_plan_sha256: "f".repeat(64),
      lore_snapshot_sha256: "1".repeat(64),
      prompt_controls_sha256: "2".repeat(64),
    },
  });
  try {
    fs.mkdirSync(path.join(directRunDir, "artifacts"), { recursive: true });
    fs.mkdirSync(path.join(commandRunDir, "artifacts"), { recursive: true });
    const directInitialized = initialize(directRunDir);
    const commandInitialized = initialize(commandRunDir);
    assert.equal(directInitialized.ok, true, directInitialized.error);
    assert.equal(commandInitialized.ok, true, commandInitialized.error);
    const values = {
      eventId: "usage.fix-loop.1",
      generatedAt: "2026-07-11T00:00:00.000Z",
      scope: "phase",
      phaseId: "fix-loop",
      round: 1,
      modelId: "model-v1",
      inputTokens: 100,
      outputTokens: 20,
      additionalTokens: 5,
      latencyMs: 50,
      estimatedCostUsd: "0.01",
    };
    const direct = recordExecutionStateUsage({
      runDir: directRunDir,
      runId: "r1",
      mode: "ledger-selective",
      experimentEnabled: true,
      ...values,
    });
    const command = recordUsageFromArgs([
      "record-usage",
      "--run-dir", relativeRunDir,
      "--event-id", values.eventId,
      "--generated-at", values.generatedAt,
      "--scope", values.scope,
      "--phase-id", values.phaseId,
      "--round", String(values.round),
      "--model-id", values.modelId,
      "--input-tokens", String(values.inputTokens),
      "--output-tokens", String(values.outputTokens),
      "--additional-tokens", String(values.additionalTokens),
      "--latency-ms", String(values.latencyMs),
      "--estimated-cost-usd", values.estimatedCostUsd,
    ], { root: commandRoot });
    assert.equal(command.exitCode, 0, command.stderr);
    assert.equal(command.stdout, `${pythonJson(direct)}\n`);
    for (const relative of ["usage.jsonl", "metrics.json"]) {
      assert.deepEqual(
        fs.readFileSync(path.join(commandRunDir, "artifacts", "execution-ledger", relative)),
        fs.readFileSync(path.join(directRunDir, "artifacts", "execution-ledger", relative)),
      );
    }
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true });
  }
});

function runPython(source, args = []) {
  const result = spawnSync(process.env.PYTHON || "python3", ["-c", source, ...args], {
    cwd: ROOT,
    env: PYTHON_ENV,
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  return result;
}

function writeFixture(root, relative, content) {
  const destination = path.join(root, ...relative.split("/"));
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  fs.writeFileSync(destination, content, "utf8");
}
