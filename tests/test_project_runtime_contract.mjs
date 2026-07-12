import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  NODE_RUNTIME_ENTRYPOINT_RELATIVE,
  assertProjectRuntimeContractPayload,
  assertProjectRuntimeInstalled,
  buildProjectRuntimeContract,
} from "../lib/project-runtime-contract.mjs";

test("project runtime contract deterministically authenticates launcher and whole Node tree", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-runtime-contract-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const launcher = path.join(root, ".agent-flow", "bin", "agent-flow");
  const runtimeRoot = path.join(root, ".agent-flow", "runtime", "node");
  const pythonRuntimeRoot = path.join(root, ".agent-flow", "runtime", "python");
  const entrypoint = path.join(root, ...NODE_RUNTIME_ENTRYPOINT_RELATIVE.split("/"));
  fs.mkdirSync(path.dirname(launcher), { recursive: true });
  fs.mkdirSync(path.dirname(entrypoint), { recursive: true });
  fs.writeFileSync(launcher, "#!/usr/bin/env node\n", { mode: 0o755 });
  fs.writeFileSync(entrypoint, "console.log('runtime');\n", "utf8");
  fs.mkdirSync(path.join(runtimeRoot, "lib"));
  fs.writeFileSync(path.join(runtimeRoot, "lib", "value.mjs"), "export const value = 1;\n", "utf8");
  fs.mkdirSync(path.join(pythonRuntimeRoot, "agent_flow"), { recursive: true });
  fs.writeFileSync(path.join(pythonRuntimeRoot, "agent_flow", "__init__.py"), "", "utf8");

  const launcherBytes = fs.readFileSync(launcher);
  const first = buildProjectRuntimeContract({ launcherBytes, nodeRuntimeRoot: runtimeRoot, pythonRuntimeRoot });
  const second = buildProjectRuntimeContract({ launcherBytes, nodeRuntimeRoot: runtimeRoot, pythonRuntimeRoot });
  assert.deepEqual(second, first);
  const payload = {
    node_runtime: {
      path: NODE_RUNTIME_ENTRYPOINT_RELATIVE,
      tree_hash: first.contract.node_runtime.tree_hash,
    },
    python_runtime: {
      path: ".agent-flow/runtime/python",
      tree_hash: first.contract.python_runtime.tree_hash,
    },
    project_runtime_contract: first.contract,
    project_runtime_contract_commitment_version: 2,
    project_runtime_contract_commitment: first.commitment,
  };
  assert.equal(assertProjectRuntimeContractPayload(payload), first.contract);
  assert.equal(assertProjectRuntimeInstalled(root, payload).launcher, launcher);

  fs.appendFileSync(launcher, "tamper\n");
  assert.throws(() => assertProjectRuntimeInstalled(root, payload), /launcher changed/);
  fs.writeFileSync(launcher, launcherBytes, { mode: 0o755 });
  fs.appendFileSync(entrypoint, "tamper\n");
  assert.throws(() => assertProjectRuntimeInstalled(root, payload), /runtime changed/);
  fs.writeFileSync(entrypoint, "console.log('runtime');\n", "utf8");
  fs.appendFileSync(path.join(pythonRuntimeRoot, "agent_flow", "__init__.py"), "# tamper\n");
  assert.throws(() => assertProjectRuntimeInstalled(root, payload), /Python runtime changed/);
});

test("project runtime contract rejects provenance drift and unsafe runtime entries", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-runtime-unsafe-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const launcher = path.join(root, ".agent-flow", "bin", "agent-flow");
  const runtimeRoot = path.join(root, ".agent-flow", "runtime", "node");
  const pythonRuntimeRoot = path.join(root, ".agent-flow", "runtime", "python");
  const entrypoint = path.join(root, ...NODE_RUNTIME_ENTRYPOINT_RELATIVE.split("/"));
  fs.mkdirSync(path.dirname(launcher), { recursive: true });
  fs.mkdirSync(path.dirname(entrypoint), { recursive: true });
  fs.writeFileSync(launcher, "#!/usr/bin/env node\n", { mode: 0o755 });
  fs.writeFileSync(entrypoint, "runtime\n", "utf8");
  fs.mkdirSync(path.join(pythonRuntimeRoot, "agent_flow"), { recursive: true });
  fs.writeFileSync(path.join(pythonRuntimeRoot, "agent_flow", "__init__.py"), "", "utf8");
  const built = buildProjectRuntimeContract({
    launcherBytes: fs.readFileSync(launcher),
    nodeRuntimeRoot: runtimeRoot,
    pythonRuntimeRoot,
  });
  const payload = {
    node_runtime: {
      path: NODE_RUNTIME_ENTRYPOINT_RELATIVE,
      tree_hash: built.contract.node_runtime.tree_hash,
    },
    python_runtime: {
      path: ".agent-flow/runtime/python",
      tree_hash: built.contract.python_runtime.tree_hash,
    },
    project_runtime_contract: built.contract,
    project_runtime_contract_commitment_version: 2,
    project_runtime_contract_commitment: built.commitment,
  };
  const changed = structuredClone(payload);
  changed.project_runtime_contract.launcher.sha256 = "0".repeat(64);
  assert.throws(() => assertProjectRuntimeContractPayload(changed), /does not match provenance/);

  fs.symlinkSync(entrypoint, path.join(runtimeRoot, "alias.mjs"));
  assert.throws(
    () => buildProjectRuntimeContract({
      launcherBytes: fs.readFileSync(launcher),
      nodeRuntimeRoot: runtimeRoot,
      pythonRuntimeRoot,
    }),
    /may not contain symlinks/,
  );
});

test("project runtime contract rejects hard-linked launcher and runtime files", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-runtime-hardlink-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const launcher = path.join(root, ".agent-flow", "bin", "agent-flow");
  const runtimeRoot = path.join(root, ".agent-flow", "runtime", "node");
  const pythonRuntimeRoot = path.join(root, ".agent-flow", "runtime", "python");
  const entrypoint = path.join(root, ...NODE_RUNTIME_ENTRYPOINT_RELATIVE.split("/"));
  fs.mkdirSync(path.dirname(launcher), { recursive: true });
  fs.mkdirSync(path.dirname(entrypoint), { recursive: true });
  fs.mkdirSync(pythonRuntimeRoot, { recursive: true });
  fs.writeFileSync(launcher, "#!/usr/bin/env node\n", { mode: 0o755 });
  fs.writeFileSync(entrypoint, "runtime\n", "utf8");
  fs.writeFileSync(path.join(pythonRuntimeRoot, "runtime.py"), "", "utf8");
  const built = buildProjectRuntimeContract({
    launcherBytes: fs.readFileSync(launcher),
    nodeRuntimeRoot: runtimeRoot,
    pythonRuntimeRoot,
  });
  const payload = {
    node_runtime: {
      path: NODE_RUNTIME_ENTRYPOINT_RELATIVE,
      tree_hash: built.contract.node_runtime.tree_hash,
    },
    python_runtime: {
      path: ".agent-flow/runtime/python",
      tree_hash: built.contract.python_runtime.tree_hash,
    },
    project_runtime_contract: built.contract,
    project_runtime_contract_commitment_version: 2,
    project_runtime_contract_commitment: built.commitment,
  };
  fs.linkSync(launcher, path.join(root, "launcher-alias"));
  assert.throws(() => assertProjectRuntimeInstalled(root, payload), /launcher may not be hard-linked/);
});
