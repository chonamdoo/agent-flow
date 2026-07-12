import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { spawnSync } from "node:child_process";

import { detectActiveHost } from "./host-detection.mjs";

const MANAGED_HOOK_SCRIPTS = Object.freeze([
  "comment-checker.py",
  "guard-protected-branch.sh",
  "guard-worktree-write.py",
  "guard-worktree.sh",
  "show-phase-status.sh",
]);
const WRITE_TOOL_MATCHER = "^(apply_patch|Write|Edit|MultiEdit|NotebookEdit|Eval|Python|Notebook|write|edit|multi_edit|multiedit|notebook_edit|notebookedit|eval|python|notebook)$";
const EXPECTED_MANAGED_HOOK_PROJECTION = Object.freeze([
  ["PostToolUse", WRITE_TOOL_MATCHER, "command", "comment-checker.py"],
  ["PreToolUse", "Bash", "command", "guard-protected-branch.sh"],
  ["PreToolUse", "Bash", "command", "guard-worktree-write.py"],
  ["PreToolUse", "Bash", "command", "guard-worktree.sh"],
  ["PreToolUse", WRITE_TOOL_MATCHER, "command", "guard-worktree-write.py"],
  ["Stop", "", "command", "show-phase-status.sh"],
]);
const MANAGED_HOOK_CONTRACT_VERSION = 2;
const MANAGED_HOOK_CONTRACT_COMMITMENT_VERSION = 2;
const MANAGED_HOOK_VERIFIER_TOKEN_SHA256 = "6a31e12ab534093eeb9beffb0c9cb983e0893497cb2c70a7ef3dd2edb8e43191";
const MANAGED_HOOK_CONFIG_PATHS = Object.freeze([
  ".Codex/hooks.json",
  ".claude/settings.json",
  ".codex/hooks.json",
]);
const MAX_INSTALLED_KIT_BYTES = 2 * 1024 * 1024;
const TRUST_TRANSACTION_VERSION = 2;
const TRUST_RECEIPT_VERSION = 1;
const TRUST_TRANSACTION_FILE = "codex-worktree-trust.json";
const TRUST_LOCK_FILE = ".config.toml.agent-flow.lock.json";
const TRUST_TRANSACTION_DIR_PREFIX = ".config.toml.agent-flow-";
const MAX_CONFIG_BYTES = 2 * 1024 * 1024;
const MAX_RECEIPT_BYTES = 4 * MAX_CONFIG_BYTES;
const TRUST_CRASH_ENV = "AGENT_FLOW_TEST_HARD_KILL_AFTER_WORKTREE_CODEX_TRUST_WRITE";
const TRUST_DISPLACE_CRASH_ENV = "AGENT_FLOW_TEST_HARD_KILL_AFTER_WORKTREE_CODEX_TRUST_DISPLACE";

export function ensureCodexWorktreeHookTrust({ leaderRoot, worktreeRoot }) {
  recoverInterruptedTrustTransaction(leaderRoot);
  if (process.env.AGENT_FLOW_SKIP_CODEX_TRUST === "1") {
    return { status: "skipped", hooks: 0 };
  }

  const codexBinary = resolveCodexBinary();
  if (!codexBinary) {
    if (detectActiveHost(process.env) === "codex") {
      throw new Error("blocked: Codex worktree hook trust unavailable: Codex CLI not found");
    }
    return { status: "unavailable", hooks: 0 };
  }

  const configPath = resolveCodexConfigPath();
  if (!configPath) {
    throw new Error("blocked: Codex worktree hook trust unavailable: CODEX_HOME or HOME is required");
  }
  recoverCodexConfigTransactionLock(configPath);
  const hookContract = managedProjectHookContract(leaderRoot, worktreeRoot);

  const original = snapshotConfig(configPath);
  const createdDirectories = [];
  const transaction = createTrustTransaction(leaderRoot, configPath, original);
  let expected = original;
  try {
    const projectHeader = `[projects.${tomlString(worktreeRoot)}]`;
    expected = writeConfig(
      configPath,
      expected,
      semanticUpsertTomlValue(configText(expected, configPath), {
        tableHeader: projectHeader,
        tablePath: ["projects", worktreeRoot],
        key: "trust_level",
        value: '"trusted"',
        expectedValue: "trusted",
      }),
      createdDirectories,
      transaction,
      { beforePublish: () => assertManagedHookContractUnchanged(hookContract) },
    );

    assertManagedHookContractUnchanged(hookContract);
    const discoveryResult = queryCodexHooks(codexBinary, worktreeRoot);
    assertManagedHookContractUnchanged(hookContract);
    const discovered = managedProjectHooks(
      discoveryResult,
      leaderRoot,
      worktreeRoot,
      hookContract,
    );

    let trustedConfig = configText(expected, configPath);
    for (const hook of discovered) {
      const hookHeader = `[hooks.state.${tomlString(hook.key)}]`;
      trustedConfig = semanticUpsertTomlValue(trustedConfig, {
        tableHeader: hookHeader,
        tablePath: ["hooks", "state", hook.key],
        key: "trusted_hash",
        value: tomlString(hook.currentHash),
        expectedValue: hook.currentHash,
      });
    }
    expected = writeConfig(
      configPath,
      expected,
      trustedConfig,
      createdDirectories,
      transaction,
      { beforePublish: () => assertManagedHookContractUnchanged(hookContract) },
    );

    assertManagedHookContractUnchanged(hookContract);
    const verificationResult = queryCodexHooks(codexBinary, worktreeRoot);
    assertManagedHookContractUnchanged(hookContract);
    const verified = managedProjectHooks(
      verificationResult,
      leaderRoot,
      worktreeRoot,
      hookContract,
    );
    assertHooksTrusted(discovered, verified);
    completeTrustTransaction(transaction);
    return { status: "trusted", hooks: discovered.length };
  } catch (error) {
    const rollbackErrors = [];
    if (transaction.started) {
      try {
        recoverInterruptedTrustTransaction(leaderRoot, { allowOwnerPid: process.pid });
      } catch (rollbackError) {
        rollbackErrors.push(rollbackError instanceof Error ? rollbackError.message : String(rollbackError));
      }
    } else {
      restoreConfig(configPath, original, expected, rollbackErrors);
    }
    for (const directory of [...createdDirectories].reverse()) {
      removeEmptyDirectory(directory, rollbackErrors);
    }
    if (rollbackErrors.length > 0) {
      throw new Error(
        `${error instanceof Error ? error.message : String(error)}; Codex config rollback failed: ${rollbackErrors.join("; ")}`,
        { cause: error },
      );
    }
    throw error;
  }
}

export function loadManagedHookCommandPolicy(leaderRoot) {
  return installedManagedHookScriptContract(leaderRoot);
}

export function trustedManagedHookCommand(policy, command) {
  if (
    !policy
    || typeof policy !== "object"
    || typeof policy.leaderRoot !== "string"
    || !(policy.scriptHashes instanceof Map)
  ) {
    throw new Error("blocked: managed hook command policy is unavailable");
  }
  const verifier = managedHookVerifierDetails(command);
  if (!verifier) return null;
  for (const scriptName of MANAGED_HOOK_SCRIPTS) {
    const relative = `.agent-flow/scripts/hooks/${scriptName}`;
    const expectedPath = path.join(policy.leaderRoot, ...relative.split("/"));
    if (
      verifier.scriptPath.replaceAll("\\", "/") === expectedPath.replaceAll("\\", "/")
      && verifier.sha256 === policy.scriptHashes.get(relative)
    ) {
      return { scriptName, commandPath: expectedPath, sha256: verifier.sha256 };
    }
  }
  return null;
}

export function recoverInterruptedCodexTrustTransaction(
  leaderRoot,
  { allowOwnerPid = null } = {},
) {
  return recoverInterruptedTrustTransaction(leaderRoot, { allowOwnerPid });
}

export function canonicalCodexConfigPath(configPath) {
  return canonicalPathWithMissing(configPath);
}

export function applyCodexConfigTrustUpdates({
  leaderRoot,
  configPath,
  updates,
  beforePublish = null,
  afterPublish = null,
  beforeCleanup = null,
}) {
  recoverInterruptedTrustTransaction(leaderRoot);
  if (!Array.isArray(updates) || updates.length === 0) {
    throw new Error("blocked: Codex config trust updates are missing");
  }
  for (const update of updates) {
    if (
      !update
      || typeof update !== "object"
      || Array.isArray(update)
      || typeof update.tableHeader !== "string"
      || !Array.isArray(update.tablePath)
      || update.tablePath.length === 0
      || update.tablePath.some((segment) => typeof segment !== "string" || !segment)
      || typeof update.key !== "string"
      || !update.key
      || typeof update.value !== "string"
      || typeof update.expectedValue !== "string"
    ) {
      throw new Error("blocked: Codex config trust update is invalid");
    }
  }

  const canonicalConfig = canonicalPathWithMissing(configPath);
  recoverCodexConfigTransactionLock(canonicalConfig);
  const original = snapshotConfig(canonicalConfig);
  const createdDirectories = [];
  const transaction = createTrustTransaction(leaderRoot, canonicalConfig, original);
  let expected = original;
  try {
    let next = configText(original, canonicalConfig);
    for (const update of updates) {
      next = semanticUpsertTomlValue(next, update);
    }
    const nextBytes = Buffer.from(next.endsWith("\n") ? next : `${next}\n`, "utf8");
    if (original.exists && original.content.equals(nextBytes)) {
      if (typeof afterPublish === "function") afterPublish();
      return { changed: false, configPath: canonicalConfig };
    }
    expected = writeConfig(
      canonicalConfig,
      expected,
      next,
      createdDirectories,
      transaction,
      { beforePublish },
    );
    if (typeof afterPublish === "function") afterPublish();
    completeTrustTransaction(transaction, { beforeCleanup });
    return { changed: true, configPath: canonicalConfig };
  } catch (error) {
    const rollbackErrors = [];
    if (transaction.started) {
      try {
        recoverInterruptedTrustTransaction(leaderRoot, { allowOwnerPid: process.pid });
      } catch (rollbackError) {
        rollbackErrors.push(
          rollbackError instanceof Error ? rollbackError.message : String(rollbackError),
        );
      }
    } else {
      restoreConfig(canonicalConfig, original, expected, rollbackErrors);
    }
    for (const directory of [...createdDirectories].reverse()) {
      removeEmptyDirectory(directory, rollbackErrors);
    }
    if (rollbackErrors.length > 0) {
      throw new Error(
        `${error instanceof Error ? error.message : String(error)}; `
        + `Codex config rollback failed: ${rollbackErrors.join("; ")}`,
        { cause: error },
      );
    }
    throw error;
  }
}

function resolveCodexBinary() {
  const configured = process.env.CODEX_CLI_PATH?.trim();
  const candidates = configured
    ? [configured]
    : ["/Applications/Codex.app/Contents/Resources/codex", "codex"];
  for (const candidate of candidates) {
    const result = spawnSync(candidate, ["--version"], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
      timeout: 3000,
    });
    if (!result.error && result.status === 0) return candidate;
  }
  return null;
}

function resolveCodexConfigPath() {
  const configuredHome = process.env.CODEX_HOME?.trim();
  if (configuredHome) {
    return canonicalPathWithMissing(path.join(path.resolve(configuredHome), "config.toml"));
  }
  const home = process.env.HOME || process.env.USERPROFILE;
  return home
    ? canonicalPathWithMissing(path.join(path.resolve(home), ".codex", "config.toml"))
    : null;
}

function queryCodexHooks(codexBinary, worktreeRoot) {
  const helper = String.raw`
const { spawn } = require("node:child_process");
const codexBinary = process.argv[1];
const root = process.argv[2];
let complete = false;
let buffer = "";
const proc = spawn(codexBinary, ["app-server", "--stdio"], { stdio: ["pipe", "pipe", "ignore"] });
const timer = setTimeout(() => finish(null, "timed out"), 5000);
function send(id, method, params) {
  proc.stdin.write(JSON.stringify({ id, method, params }) + "\n");
}
function finish(response, failure = "") {
  if (complete) return;
  complete = true;
  clearTimeout(timer);
  if (proc.exitCode === null) proc.kill("SIGTERM");
  if (failure || !response || response.error || !response.result) {
    if (failure) process.stderr.write(failure + "\n");
    process.exitCode = 1;
    return;
  }
  process.stdout.write(JSON.stringify(response.result));
}
function handle(response) {
  if (response?.id === 1) {
    if (response.error) finish(response);
    else send(2, "hooks/list", { cwds: [root] });
  } else if (response?.id === 2) {
    finish(response);
  }
}
proc.on("error", (error) => finish(null, error.message));
proc.on("exit", (code) => {
  if (!complete) finish(null, "Codex app-server exited before hooks/list completed (" + code + ")");
});
proc.stdout.on("data", (chunk) => {
  buffer += chunk.toString();
  let newline;
  while ((newline = buffer.indexOf("\n")) >= 0) {
    const line = buffer.slice(0, newline).trim();
    buffer = buffer.slice(newline + 1);
    if (!line) continue;
    try { handle(JSON.parse(line)); } catch {}
  }
});
send(1, "initialize", {
  clientInfo: { name: "agent-flow-worktree-bridge", title: null, version: "1" },
  capabilities: { experimentalApi: true, requestAttestation: false },
});
`;
  const result = spawnSync(process.execPath, ["-e", helper, codexBinary, worktreeRoot], {
    encoding: "utf8",
    env: process.env,
    stdio: ["ignore", "pipe", "pipe"],
    timeout: 8000,
  });
  if (result.error || result.status !== 0) {
    const detail = (result.stderr || result.error?.message || "hooks/list failed").trim();
    throw new Error(`blocked: Codex worktree hook discovery failed: ${detail}`);
  }
  let parsed;
  try {
    parsed = JSON.parse(result.stdout);
  } catch (error) {
    throw new Error(`blocked: Codex worktree hook discovery returned invalid JSON: ${error.message}`);
  }
  if (!parsed || typeof parsed !== "object" || !Array.isArray(parsed.data)) {
    throw new Error("blocked: Codex worktree hook discovery returned an invalid schema");
  }
  return parsed;
}

function installedManagedHookScriptContract(leaderRoot) {
  const kitPath = path.join(leaderRoot, ".agent-flow", "kit.json");
  const kitSnapshot = snapshotManagedHookFile(leaderRoot, kitPath, {
    label: "installed kit metadata",
    maxBytes: MAX_INSTALLED_KIT_BYTES,
  });
  const kitText = kitSnapshot.content.toString("utf8");
  if (!Buffer.from(kitText, "utf8").equals(kitSnapshot.content)) {
    throw new Error("blocked: installed kit metadata is not valid UTF-8");
  }
  let kit;
  try {
    kit = JSON.parse(kitText);
  } catch (error) {
    throw new Error(`blocked: installed kit metadata is invalid: ${error.message}`);
  }
  if (!kit || typeof kit !== "object" || Array.isArray(kit)) {
    throw new Error("blocked: installed kit metadata is invalid");
  }
  if (
    typeof kit.skill_plan_hash !== "string"
    || !/^[0-9a-f]{64}$/.test(kit.skill_plan_hash)
    || kit.managed_hook_contract_commitment_version !== MANAGED_HOOK_CONTRACT_COMMITMENT_VERSION
    || typeof kit.managed_hook_contract_commitment !== "string"
    || !/^[0-9a-f]{64}$/.test(kit.managed_hook_contract_commitment)
  ) {
    throw new Error("blocked: installed managed hook commitment is invalid");
  }
  const normalized = normalizeInstalledManagedHookContract(kit.managed_hook_contract);
  const computedCommitment = sha256(Buffer.from(JSON.stringify({
    version: MANAGED_HOOK_CONTRACT_COMMITMENT_VERSION,
    skill_plan_hash: kit.skill_plan_hash,
    configs: normalized.configRows,
    scripts: normalized.scriptRows,
  }), "utf8"));
  if (computedCommitment !== kit.managed_hook_contract_commitment) {
    throw new Error("blocked: installed managed hook commitment does not match provenance");
  }
  const scriptSnapshots = normalized.scriptRows.map(([relative, expectedHash]) => (
    snapshotManagedHookFile(leaderRoot, path.join(leaderRoot, ...relative.split("/")), {
      label: `managed hook script ${relative}`,
      expectedHash,
      executable: true,
    })
  ));
  return {
    configHashes: new Map(normalized.configRows.map(([relative, hash]) => [relative, hash])),
    kitSnapshot,
    leaderRoot: fs.realpathSync.native(leaderRoot),
    scriptHashes: new Map(normalized.scriptRows.map(([relative, hash]) => [relative, hash])),
    scriptSnapshots,
  };
}

function normalizeInstalledManagedHookContract(contract) {
  if (
    !contract
    || typeof contract !== "object"
    || Array.isArray(contract)
    || contract.version !== MANAGED_HOOK_CONTRACT_VERSION
  ) {
    throw new Error("blocked: installed managed hook contract is invalid");
  }
  const normalize = (entries, expectedPaths, label, requireExecutable = false) => {
    if (!entries || typeof entries !== "object" || Array.isArray(entries)) {
      throw new Error(`blocked: installed managed hook ${label} provenance is invalid`);
    }
    const actualPaths = Object.keys(entries).sort(compareCodePoints);
    const requiredPaths = [...expectedPaths].sort(compareCodePoints);
    if (JSON.stringify(actualPaths) !== JSON.stringify(requiredPaths)) {
      throw new Error(`blocked: installed managed hook ${label} provenance is incomplete`);
    }
    return actualPaths.map((relative) => {
      const entry = entries[relative];
      if (
        !entry
        || typeof entry !== "object"
        || Array.isArray(entry)
        || typeof entry.sha256 !== "string"
        || !/^[0-9a-f]{64}$/.test(entry.sha256)
        || (requireExecutable && entry.mode !== "executable")
      ) {
        throw new Error(`blocked: installed managed hook ${label} provenance is invalid: ${relative}`);
      }
      return requireExecutable
        ? [relative, entry.sha256, "executable"]
        : [relative, entry.sha256];
    });
  };
  return {
    configRows: normalize(contract.configs, MANAGED_HOOK_CONFIG_PATHS, "config"),
    scriptRows: normalize(
      contract.scripts,
      MANAGED_HOOK_SCRIPTS.map((name) => `.agent-flow/scripts/hooks/${name}`),
      "script",
      true,
    ),
  };
}

function snapshotManagedHookFile(root, file, {
  label,
  expectedHash = null,
  executable = false,
  maxBytes = null,
} = {}) {
  const lexicalRoot = path.resolve(root);
  const lexicalFile = path.resolve(file);
  const lexicalRelative = path.relative(lexicalRoot, lexicalFile);
  if (!lexicalRelative || lexicalRelative.startsWith("..") || path.isAbsolute(lexicalRelative)) {
    throw new Error(`blocked: ${label} escapes the leader install`);
  }
  const canonicalRoot = fs.realpathSync.native(root);
  const absolute = path.join(canonicalRoot, lexicalRelative);
  const relative = path.relative(canonicalRoot, absolute);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`blocked: ${label} escapes the leader install`);
  }
  let cursor = canonicalRoot;
  for (const [index, part] of relative.split(path.sep).entries()) {
    cursor = path.join(cursor, part);
    const metadata = lstatIfExists(cursor);
    const final = index === relative.split(path.sep).length - 1;
    if (
      !metadata
      || metadata.isSymbolicLink()
      || (final ? !metadata.isFile() : !metadata.isDirectory())
    ) {
      throw new Error(`blocked: ${label} path is missing or unsafe: ${cursor}`);
    }
  }
  const before = fs.lstatSync(absolute, { bigint: true });
  const descriptor = fs.openSync(
    absolute,
    fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0),
  );
  let metadata;
  let finalMetadata;
  let content;
  try {
    metadata = fs.fstatSync(descriptor, { bigint: true });
    if (!metadata.isFile() || metadata.nlink !== 1n) {
      throw new Error(`blocked: ${label} must be a non-hard-linked regular file`);
    }
    if (maxBytes !== null && metadata.size > BigInt(maxBytes)) {
      throw new Error(`blocked: ${label} exceeds the size limit`);
    }
    content = fs.readFileSync(descriptor);
    finalMetadata = fs.fstatSync(descriptor, { bigint: true });
  } finally {
    fs.closeSync(descriptor);
  }
  const after = fs.lstatSync(absolute, { bigint: true });
  for (const current of [before, finalMetadata, after]) {
    if (
      current.isSymbolicLink()
      || !current.isFile()
      || current.nlink !== 1n
      || current.dev !== metadata.dev
      || current.ino !== metadata.ino
      || (current.mode & 0o777n) !== (metadata.mode & 0o777n)
      || current.size !== metadata.size
      || current.mtimeNs !== metadata.mtimeNs
      || current.ctimeNs !== metadata.ctimeNs
    ) {
      throw new Error(`blocked: ${label} changed while it was being authenticated`);
    }
  }
  const mode = Number(metadata.mode & 0o777n);
  if (executable && process.platform !== "win32" && (mode & 0o111) === 0) {
    throw new Error(`blocked: ${label} is not executable`);
  }
  const contentHash = sha256(content);
  if (expectedHash !== null && contentHash !== expectedHash) {
    throw new Error(`blocked: ${label} does not match installed provenance`);
  }
  return {
    path: absolute,
    content,
    dev: metadata.dev,
    ino: metadata.ino,
    ctimeNs: metadata.ctimeNs,
    mtimeNs: metadata.mtimeNs,
    mode,
    nlink: metadata.nlink,
    size: metadata.size,
    sha256: contentHash,
    executable,
    maxBytes,
    label,
  };
}

function sameManagedHookFileSnapshot(left, right) {
  return left.path === right.path
    && left.dev === right.dev
    && left.ino === right.ino
    && left.ctimeNs === right.ctimeNs
    && left.mtimeNs === right.mtimeNs
    && left.mode === right.mode
    && left.nlink === right.nlink
    && left.size === right.size
    && left.sha256 === right.sha256;
}

function managedProjectHookContract(leaderRoot, worktreeRoot) {
  const installed = installedManagedHookScriptContract(leaderRoot);
  const configSpecs = [
    { path: path.join(leaderRoot, ".Codex", "hooks.json"), relative: ".Codex/hooks.json" },
    { path: path.join(leaderRoot, ".codex", "hooks.json"), relative: ".codex/hooks.json" },
    { path: path.join(worktreeRoot, ".Codex", "hooks.json"), relative: ".Codex/hooks.json" },
    { path: path.join(worktreeRoot, ".codex", "hooks.json"), relative: ".codex/hooks.json" },
  ];
  const snapshots = [];
  let expectedHooks = null;
  for (const spec of configSpecs) {
    const configPath = spec.path;
    if (snapshots.some((snapshot) => sameExistingPath(snapshot.path, configPath))) continue;
    const metadata = lstatIfExists(configPath);
    if (!metadata?.isFile() || metadata.isSymbolicLink()) {
      throw new Error(`blocked: bridged Codex hook config is missing or unsafe: ${configPath}`);
    }
    const content = fs.readFileSync(configPath);
    let config;
    try {
      config = JSON.parse(content.toString("utf8"));
    } catch (error) {
      throw new Error(`blocked: bridged Codex hook config is invalid: ${configPath}: ${error.message}`);
    }
    const parsed = managedHookContractFromConfig(config, configPath, installed);
    if (JSON.stringify(parsed.projection) !== JSON.stringify(EXPECTED_MANAGED_HOOK_PROJECTION)) {
      throw new Error(`blocked: bridged Codex managed hook projection does not match the exact hook contract: ${configPath}`);
    }
    const committedHash = installed.configHashes.get(spec.relative);
    const projectionHash = sha256(Buffer.from(JSON.stringify(parsed.projection), "utf8"));
    if (!committedHash || projectionHash !== committedHash) {
      throw new Error(`blocked: bridged Codex managed hook projection does not match installed provenance: ${configPath}`);
    }
    if (expectedHooks === null) expectedHooks = parsed.hooks;
    snapshots.push({ path: configPath, content });
  }
  if (!expectedHooks || snapshots.length === 0) {
    throw new Error("blocked: bridged Codex managed hook contract is unavailable");
  }
  const contract = {
    expectedIdentities: expectedHooks.map(managedHookIdentity).sort(compareCodePoints),
    installed,
    snapshots,
  };
  assertManagedHookContractUnchanged(contract);
  return contract;
}

function managedHookContractFromConfig(config, configPath, installed) {
  if (!config || typeof config !== "object" || Array.isArray(config)) {
    throw new Error(`blocked: bridged Codex hook config is invalid: ${configPath}`);
  }
  if (!config.hooks || typeof config.hooks !== "object" || Array.isArray(config.hooks)) {
    throw new Error(`blocked: bridged Codex hook config has no managed hooks: ${configPath}`);
  }
  const projection = [];
  const hooks = [];
  const managedMatchers = new Set();
  for (const [event, entries] of Object.entries(config.hooks)) {
    if (!Array.isArray(entries)) {
      throw new Error(`blocked: bridged Codex hook config has an invalid schema: ${configPath}`);
    }
    for (const entry of entries) {
      if (!entry || typeof entry !== "object" || Array.isArray(entry) || !Array.isArray(entry.hooks)) {
        throw new Error(`blocked: bridged Codex hook config has an invalid schema: ${configPath}`);
      }
      if (Object.hasOwn(entry, "matcher") && typeof entry.matcher !== "string") {
        throw new Error(`blocked: bridged Codex hook config has an invalid matcher: ${configPath}`);
      }
      const matcher = entry.matcher ?? "";
      let managedCount = 0;
      for (const hook of entry.hooks) {
        if (!hook || typeof hook !== "object" || Array.isArray(hook)) {
          throw new Error(`blocked: bridged Codex hook config has an invalid schema: ${configPath}`);
        }
        const candidateName = managedHookCandidateName(hook.command);
        const trusted = trustedManagedHookCommand(installed, hook.command);
        if (!trusted) {
          if (candidateName) {
            throw new Error(`blocked: bridged Codex managed hook command is not immutable: ${configPath}`);
          }
          continue;
        }
        const { scriptName, commandPath } = trusted;
        if (hook.type !== "command") {
          throw new Error(`blocked: bridged Codex hook config has an invalid managed hook type: ${configPath}`);
        }
        managedCount += 1;
        projection.push([event, matcher, hook.type, scriptName]);
        hooks.push({ scriptName, commandPath });
      }
      if (managedCount > 0) {
        const matcherKey = `${event}\u0000${matcher}`;
        if (managedMatchers.has(matcherKey)) {
          throw new Error(`blocked: duplicate bridged Codex managed hook matcher: ${configPath}`);
        }
        managedMatchers.add(matcherKey);
      }
    }
  }
  projection.sort(compareHookProjectionRows);
  return { hooks, projection };
}

function compareHookProjectionRows(left, right) {
  for (let index = 0; index < Math.min(left.length, right.length); index += 1) {
    const compared = compareCodePoints(left[index], right[index]);
    if (compared !== 0) return compared;
  }
  return left.length - right.length;
}

function managedHookIdentity(hook) {
  return `${hook.scriptName}\u0000${hook.commandPath}`;
}

function assertManagedHookContractUnchanged(contract) {
  const installedSnapshots = [
    contract.installed.kitSnapshot,
    ...contract.installed.scriptSnapshots,
  ];
  for (const snapshot of installedSnapshots) {
    const current = snapshotManagedHookFile(contract.installed.leaderRoot, snapshot.path, {
      label: snapshot.label,
      expectedHash: snapshot.sha256,
      executable: snapshot.executable,
      maxBytes: snapshot.maxBytes,
    });
    if (!sameManagedHookFileSnapshot(snapshot, current)) {
      throw new Error(`blocked: ${snapshot.label} changed during Codex hook trust`);
    }
  }
  for (const snapshot of contract.snapshots) {
    const metadata = lstatIfExists(snapshot.path);
    if (
      !metadata?.isFile()
      || metadata.isSymbolicLink()
      || !fs.readFileSync(snapshot.path).equals(snapshot.content)
    ) {
      throw new Error(`blocked: bridged Codex hook config changed during trust discovery: ${snapshot.path}`);
    }
  }
}

function managedProjectHooks(result, leaderRoot, worktreeRoot, contract) {
  assertManagedHookContractUnchanged(contract);
  const entry = result.data.find((candidate) => candidate?.cwd === worktreeRoot);
  if (!entry || !Array.isArray(entry.hooks)) {
    throw new Error("blocked: Codex worktree hook discovery omitted the worktree");
  }
  if (Array.isArray(entry.errors) && entry.errors.length > 0) {
    throw new Error("blocked: Codex worktree hook discovery reported configuration errors");
  }
  const sourcePaths = [
    path.join(leaderRoot, ".Codex", "hooks.json"),
    path.join(leaderRoot, ".codex", "hooks.json"),
    path.join(worktreeRoot, ".Codex", "hooks.json"),
    path.join(worktreeRoot, ".codex", "hooks.json"),
  ];
  const hooks = [];
  const keys = new Set();
  for (const hook of entry.hooks) {
    if (
      !hook
      || typeof hook !== "object"
      || typeof hook.sourcePath !== "string"
      || !sourcePaths.some((sourcePath) => sameExistingPath(sourcePath, hook.sourcePath))
    ) {
      continue;
    }
    const candidateName = managedHookCandidateName(hook.command);
    const trusted = trustedManagedHookCommand(contract.installed, hook.command);
    if (!trusted) {
      if (candidateName) {
        throw new Error("blocked: Codex discovered a non-immutable agent-flow hook");
      }
      continue;
    }
    const { scriptName, commandPath } = trusted;
    if (
      typeof hook.key !== "string"
      || !hook.key
      || typeof hook.currentHash !== "string"
      || !/^sha256:[0-9a-f]{64}$/.test(hook.currentHash)
      || typeof hook.trustStatus !== "string"
    ) {
      throw new Error("blocked: Codex worktree hook discovery returned invalid hook metadata");
    }
    if (keys.has(hook.key)) {
      throw new Error("blocked: Codex worktree hook discovery returned duplicate hook keys");
    }
    keys.add(hook.key);
    hooks.push({
      key: hook.key,
      currentHash: hook.currentHash,
      trustStatus: hook.trustStatus,
      scriptName,
      commandPath,
    });
  }
  const actualIdentities = hooks.map(managedHookIdentity).sort(compareCodePoints);
  if (JSON.stringify(actualIdentities) !== JSON.stringify(contract.expectedIdentities)) {
    throw new Error(
      "blocked: Codex worktree hook discovery returned an incomplete, extra, or duplicate managed hook set",
    );
  }
  assertManagedHookContractUnchanged(contract);
  return hooks.sort((left, right) => compareCodePoints(left.key, right.key));
}

function assertHooksTrusted(expected, actual) {
  const actualByKey = new Map(actual.map((hook) => [hook.key, hook]));
  for (const hook of expected) {
    const verified = actualByKey.get(hook.key);
    if (
      !verified
      || verified.currentHash !== hook.currentHash
      || verified.trustStatus !== "trusted"
    ) {
      throw new Error(`blocked: Codex worktree hook trust verification failed: ${hook.key}`);
    }
  }
}

function managedHookCandidateName(command) {
  if (typeof command !== "string") return "";
  for (const match of command.matchAll(/(?:^|[\s'"])([A-Za-z0-9+/]+={0,2})(?=$|[\s'"])/g)) {
    const encoded = match[1];
    const decoded = Buffer.from(encoded, "base64");
    if (decoded.toString("base64") === encoded) {
      const decodedPath = decoded.toString("utf8");
      if (Buffer.from(decodedPath, "utf8").equals(decoded)) {
        const scriptName = path.basename(decodedPath);
        if (MANAGED_HOOK_SCRIPTS.includes(scriptName)) return scriptName;
      }
    }
  }
  if (
    command.includes("AGENT_FLOW_MANAGED_HOOK_SOURCE_PATH")
    && command.includes("agent-flow-managed-hook-")
    && command.includes("descriptor execution unavailable")
  ) return "__managed-verifier__";
  const normalized = normalizedHookCommand(command)
    .replaceAll("\\", "/")
    .replaceAll("'", "")
    .replaceAll('"', "");
  return MANAGED_HOOK_SCRIPTS.find(
    (scriptName) => normalized.includes(`/.agent-flow/scripts/hooks/${scriptName}`)
      || normalized.includes(`/scripts/hooks/${scriptName}`),
  ) ?? "";
}

function managedHookVerifierDetails(command) {
  const prefix = "'/usr/bin/python3' -I -c ";
  if (typeof command !== "string" || !command.startsWith(prefix)) return null;
  const match = command.match(/ '([A-Za-z0-9+/=]+)' '([0-9a-f]{64})'$/);
  if (!match) return null;
  const verifierToken = command.slice(prefix.length, match.index);
  if (sha256(Buffer.from(verifierToken, "utf8")) !== MANAGED_HOOK_VERIFIER_TOKEN_SHA256) return null;
  const decoded = Buffer.from(match[1], "base64");
  if (decoded.toString("base64") !== match[1]) return null;
  const scriptPath = decoded.toString("utf8");
  if (!Buffer.from(scriptPath, "utf8").equals(decoded)) return null;
  return { scriptPath, sha256: match[2] };
}

function normalizedHookCommand(command) {
  let normalized = String(command).trim();
  if (normalized.startsWith("'") && normalized.endsWith("'")) {
    normalized = normalized.slice(1, -1).replaceAll("'\\''", "'");
  } else if (normalized.startsWith('"') && normalized.endsWith('"')) {
    normalized = normalized.slice(1, -1);
  }
  return normalized;
}

function configText(snapshot, configPath) {
  if (!snapshot.exists) return "";
  const text = snapshot.content.toString("utf8");
  if (!Buffer.from(text, "utf8").equals(snapshot.content)) {
    throw new Error(`blocked: Codex config is not valid UTF-8: ${configPath}`);
  }
  return text;
}

function upsertTomlValue(text, tableHeader, key, value) {
  const tablePattern = new RegExp(
    `(^|\\n)[ \\t]*${escapeRegex(tableHeader)}[ \\t]*(?:#.*)?\\r?\\n([\\s\\S]*?)(?=\\n[ \\t]*\\[[^\\n]+\\]|$)`,
  );
  const keyPattern = new RegExp(`(^|\\n)[ \\t]*${escapeRegex(key)}[ \\t]*=.*(?=\\r?\\n|$)`);
  const match = text.match(tablePattern);
  if (!match) {
    const separator = text.length === 0 ? "" : text.endsWith("\n") ? "\n" : "\n\n";
    return {
      text: `${text}${separator}${tableHeader}\n${key} = ${value}\n`,
      tableMatched: false,
      keyMatched: false,
    };
  }
  const keyMatched = keyPattern.test(match[2]);
  return {
    text: text.replace(tablePattern, (full, leading, body) => {
      const nextBody = keyPattern.test(body)
        ? body.replace(keyPattern, `$1${key} = ${value}`)
        : `${body}${body.endsWith("\n") || body.length === 0 ? "" : "\n"}${key} = ${value}\n`;
      return `${leading}${tableHeader}\n${nextBody}`;
    }),
    tableMatched: true,
    keyMatched,
  };
}

function semanticUpsertTomlValue(text, update) {
  const semantic = inspectCodexToml(text, update);
  if (semantic.valueMatches) return text;
  const rendered = upsertTomlValue(
    text,
    update.tableHeader,
    update.key,
    update.value,
  );
  if (
    semantic.tableExists !== rendered.tableMatched
    || semantic.keyExists !== rendered.keyMatched
  ) {
    throw new Error("blocked: equivalent Codex config TOML target cannot be edited losslessly");
  }
  const candidate = inspectCodexToml(rendered.text, update);
  if (!candidate.tableExists || !candidate.keyExists || !candidate.valueMatches) {
    throw new Error("blocked: rendered Codex config does not satisfy semantic trust target");
  }
  return rendered.text;
}

function inspectCodexToml(text, update) {
  const script = String.raw`
import json
import sys
try:
    import tomllib as toml
except ImportError:
    import tomli as toml
payload = json.load(sys.stdin)
try:
    cursor = toml.loads(payload["text"])
except Exception:
    raise SystemExit(2)
table_exists = True
for segment in payload["table_path"]:
    if not isinstance(cursor, dict) or segment not in cursor:
        table_exists = False
        break
    cursor = cursor[segment]
table_exists = table_exists and isinstance(cursor, dict)
key_exists = table_exists and payload["key"] in cursor
print(json.dumps({
    "table_exists": table_exists,
    "key_exists": key_exists,
    "value_matches": key_exists and cursor[payload["key"]] == payload["expected_value"],
}, separators=(",", ":")))
`;
  const python = preferredCodexTomlPython();
  const result = spawnSync(python, ["-c", script], {
    encoding: "utf8",
    input: JSON.stringify({
      text,
      table_path: update.tablePath,
      key: update.key,
      expected_value: update.expectedValue,
    }),
    timeout: 5000,
    maxBuffer: 2 * 1024 * 1024,
  });
  if (result.error || result.status !== 0) {
    throw new Error("blocked: Codex config is not valid TOML");
  }
  let parsed;
  try {
    parsed = JSON.parse(result.stdout);
  } catch {
    throw new Error("blocked: Codex config TOML validator returned invalid output");
  }
  if (
    !parsed
    || typeof parsed.table_exists !== "boolean"
    || typeof parsed.key_exists !== "boolean"
    || typeof parsed.value_matches !== "boolean"
  ) {
    throw new Error("blocked: Codex config TOML validator returned an invalid target");
  }
  return {
    tableExists: parsed.table_exists,
    keyExists: parsed.key_exists,
    valueMatches: parsed.value_matches,
  };
}

function preferredCodexTomlPython() {
  const forced = process.env.AGENT_FLOW_TEST_CODEX_TOML_PYTHON?.trim();
  const virtualEnvPython = process.env.VIRTUAL_ENV
    ? path.join(
        process.env.VIRTUAL_ENV,
        process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
      )
    : null;
  const candidates = forced
    ? [forced]
    : [
        process.env.PYTHON,
        process.env.PYTHON_EXECUTABLE,
        virtualEnvPython,
        "python3.14",
        "python3.13",
        "python3.12",
        "python3.11",
        "python3.10",
        "python3",
        "python",
      ].filter(Boolean);
  const probe = [
    "import importlib.util, sys",
    "sys.exit(0 if importlib.util.find_spec('tomllib') or importlib.util.find_spec('tomli') else 1)",
  ].join(";");
  for (const candidate of [...new Set(candidates)]) {
    const result = spawnSync(candidate, ["-c", probe], {
      stdio: "ignore",
      timeout: 3000,
    });
    if (!result.error && result.status === 0) return candidate;
  }
  throw new Error("blocked: no Python TOML parser is available for Codex config validation");
}

function writeConfig(
  configPath,
  expected,
  nextText,
  createdDirectories,
  transaction,
  { beforePublish = null } = {},
) {
  const next = Buffer.from(nextText.endsWith("\n") ? nextText : `${nextText}\n`, "utf8");
  if (expected.exists && expected.content.equals(next)) return expected;
  if (next.length > MAX_CONFIG_BYTES) {
    throw new Error("blocked: Codex config exceeds the 2 MiB trust transaction limit");
  }
  assertConfigUnchanged(configPath, expected);
  ensureConfigParent(configPath, createdDirectories);
  if (!transaction.started) {
    Object.assign(transaction, acquireCodexConfigTransactionLock({
      leaderRoot: transaction.leaderRoot,
      configPath,
      journalPath: transaction.transactionPath,
      original: transaction.original,
      createdDirectories: transaction.createdDirectories,
    }));
  }
  const after = {
    exists: true,
    content: next,
    mode: expected.exists ? expected.mode : 0o600,
  };
  const round = prepareTrustConfigWrite(transaction, expected, after);
  if (typeof beforePublish === "function") beforePublish();
  publishTrustConfigRound(transaction, round);
  transaction.mutationCount += 1;
  if (transaction.mutationCount === 1 && process.env[TRUST_CRASH_ENV] === "1") {
    process.kill(process.pid, "SIGKILL");
  }
  return after;
}

function createTrustTransaction(leaderRoot, configPath, original) {
  const canonicalLeader = fs.realpathSync.native(leaderRoot);
  const canonicalConfig = canonicalPathWithMissing(configPath);
  return {
    leaderRoot: canonicalLeader,
    configPath: canonicalConfig,
    original,
    createdDirectories: missingConfigParents(canonicalConfig),
    transactionPath: trustTransactionPath(canonicalLeader),
    started: false,
    mutationCount: 0,
    journalBytes: null,
  };
}

export function acquireCodexConfigTransactionLock({
  leaderRoot,
  configPath,
  journalPath,
  original,
  createdDirectories = [],
}) {
  const canonicalLeader = fs.realpathSync.native(leaderRoot);
  const canonicalConfig = canonicalPathWithMissing(configPath);
  const canonicalJournal = path.resolve(journalPath);
  if (
    !original
    || typeof original.exists !== "boolean"
    || !validMode(original.mode)
    || (original.exists && (!Buffer.isBuffer(original.content) || original.content.length > MAX_CONFIG_BYTES))
    || (!original.exists && original.content !== null)
  ) {
    throw new Error("blocked: invalid Codex config transaction snapshot");
  }
  recoverCodexConfigTransactionLock(canonicalConfig);
  const missingDirectories = missingConfigParents(canonicalConfig);
  const actualCreatedDirectories = [];
  ensureConfigParent(canonicalConfig, actualCreatedDirectories);
  assertConfigUnchanged(canonicalConfig, original);
  const token = crypto.randomBytes(16).toString("hex");
  const receiptPath = codexConfigLockPath(canonicalConfig);
  const transactionDir = codexConfigTransactionDir(canonicalConfig, token);
  const originalPath = path.join(transactionDir, "original");
  const rollbackPath = path.join(transactionDir, "rollback");
  const normalizedDirectories = [...new Set([
    ...createdDirectories,
    ...missingDirectories,
  ].map((directory) => canonicalPathWithMissing(directory)))];
  const journal = trustJournalPayload({
    status: "pending",
    leaderRoot: canonicalLeader,
    configPath: canonicalConfig,
    receiptPath,
    token,
    createdDirectories: normalizedDirectories,
  });
  const journalBytes = jsonBytes(journal);
  durableCreateJournal(canonicalJournal, journalBytes);
  const receipt = {
    version: TRUST_RECEIPT_VERSION,
    status: "open",
    owner_pid: process.pid,
    transaction_token: token,
    leader_root: canonicalLeader,
    journal_path: canonicalJournal,
    config_path: canonicalConfig,
    receipt_path: receiptPath,
    transaction_dir: transactionDir,
    original_path: original.exists ? originalPath : null,
    rollback_path: rollbackPath,
    original: encodedOriginalSnapshot(original),
    created_directories: normalizedDirectories,
    rounds: [],
  };
  const receiptBytes = jsonBytes(receipt);
  let receiptCreated = false;
  try {
    durableCreateJournal(receiptPath, receiptBytes);
    receiptCreated = true;
    fs.mkdirSync(transactionDir, { mode: 0o700 });
    fsyncDirectory(path.dirname(transactionDir));
    if (original.exists) {
      writeDurableTemporary(originalPath, original.content, original.mode);
      fsyncDirectory(transactionDir);
    }
    const authenticated = trustJournalPayload({
      status: "authenticated",
      leaderRoot: canonicalLeader,
      configPath: canonicalConfig,
      receiptPath,
      token,
      createdDirectories: normalizedDirectories,
    });
    const authenticatedBytes = jsonBytes(authenticated);
    durableReplaceJournal(canonicalJournal, journalBytes, authenticatedBytes);
    return {
      leaderRoot: canonicalLeader,
      configPath: canonicalConfig,
      transactionPath: canonicalJournal,
      createdDirectories: normalizedDirectories,
      started: true,
      mutationCount: 0,
      journalBytes: authenticatedBytes,
      receipt,
      receiptBytes,
      receiptPath,
      transactionDir,
    };
  } catch (error) {
    if (receiptCreated) {
      try {
        recoverCodexReceipt(receipt, receiptBytes, { allowOwnerPid: process.pid });
      } catch {
        // Preserve the original acquisition error; durable evidence remains for recovery.
      }
    } else {
      try {
        removeJournalIfUnchanged(canonicalJournal, journalBytes);
      } catch {
        // Preserve the original acquisition error.
      }
    }
    throw error?.code === "EEXIST"
      ? new Error(`blocked: Codex config transaction lock is already held: ${receiptPath}`, { cause: error })
      : error;
  }
}

function prepareTrustConfigWrite(transaction, before, after) {
  const index = transaction.receipt.rounds.length + 1;
  const candidatePath = path.join(transaction.transactionDir, `candidate-${index}`);
  const displacedPath = path.join(transaction.transactionDir, `displaced-${index}`);
  writeDurableTemporary(candidatePath, after.content, after.mode);
  fsyncDirectory(transaction.transactionDir);
  const round = {
    index,
    before: snapshotState(before),
    after: snapshotState(after),
    candidate_path: candidatePath,
    displaced_path: displacedPath,
  };
  replaceTrustReceipt(transaction, {
    ...transaction.receipt,
    rounds: [...transaction.receipt.rounds, round],
  });
  return round;
}

function publishTrustConfigRound(transaction, round) {
  const configPath = transaction.configPath;
  const before = round.before;
  if (before.exists) {
    fs.renameSync(configPath, round.displaced_path);
    fsyncDirectory(path.dirname(configPath));
    fsyncDirectory(transaction.transactionDir);
    if (process.env[TRUST_DISPLACE_CRASH_ENV] === "1") {
      process.kill(process.pid, "SIGKILL");
    }
    const displaced = snapshotConfig(round.displaced_path);
    if (!snapshotStatesEqual(snapshotState(displaced), before)) {
      restoreExternalDisplacement(configPath, round.displaced_path, displaced);
      throw new Error("blocked: Codex config changed at the no-clobber publish boundary");
    }
  } else if (lstatIfExists(configPath)) {
    throw new Error("blocked: Codex config changed at the no-clobber publish boundary");
  }
  try {
    fs.linkSync(round.candidate_path, configPath);
  } catch (error) {
    if (error?.code === "EEXIST") {
      throw new Error("blocked: Codex config changed at the no-clobber publish boundary", { cause: error });
    }
    throw error;
  }
  fsyncDirectory(path.dirname(configPath));
  const applied = snapshotConfig(configPath);
  if (!snapshotStatesEqual(snapshotState(applied), round.after)) {
    throw new Error("blocked: Codex config changed immediately after no-clobber publish");
  }
}

function restoreExternalDisplacement(configPath, displacedPath, displaced) {
  if (!lstatIfExists(configPath)) {
    fs.linkSync(displacedPath, configPath);
    fsyncDirectory(path.dirname(configPath));
  }
  const current = snapshotConfig(configPath);
  if (!snapshotStatesEqual(snapshotState(current), snapshotState(displaced))) {
    throw new Error(`blocked: concurrent Codex config bytes retained at ${displacedPath}`);
  }
}

function completeTrustTransaction(transaction, { beforeCleanup = null } = {}) {
  if (!transaction.started) return;
  const lastRound = transaction.receipt.rounds.at(-1);
  if (lastRound) {
    const current = snapshotConfig(transaction.configPath);
    if (!snapshotStatesEqual(snapshotState(current), lastRound.after)) {
      throw new Error("blocked: Codex config changed before trust transaction commit");
    }
  }
  replaceTrustReceipt(transaction, { ...transaction.receipt, status: "committed" });
  if (typeof beforeCleanup === "function") beforeCleanup();
  cleanupCodexTransaction(transaction.receipt, transaction.receiptBytes, transaction.journalBytes);
  transaction.started = false;
  transaction.journalBytes = null;
}

function recoverInterruptedTrustTransaction(leaderRoot, { allowOwnerPid = null } = {}) {
  const canonicalLeader = fs.realpathSync.native(leaderRoot);
  const transactionPath = trustTransactionPath(canonicalLeader, { createFallback: false });
  if (!transactionPath) return false;
  const metadata = lstatIfExists(transactionPath);
  if (!metadata) return false;
  if (metadata.isSymbolicLink() || !metadata.isFile()) {
    throw new Error(`blocked: unsafe Codex worktree trust transaction: ${transactionPath}`);
  }
  const bytes = fs.readFileSync(transactionPath);
  const journal = parseTrustJournal(bytes, canonicalLeader, transactionPath);
  const receiptMetadata = lstatIfExists(journal.receipt_path);
  if (!receiptMetadata) {
    if (journal.status !== "pending") {
      throw new Error("blocked: authenticated Codex config receipt is missing");
    }
    removeJournalIfUnchanged(transactionPath, bytes);
    return true;
  }
  if (
    receiptMetadata.isSymbolicLink()
    || !receiptMetadata.isFile()
    || receiptMetadata.nlink !== 1
  ) {
    throw new Error(`blocked: unsafe Codex config transaction receipt: ${journal.receipt_path}`);
  }
  const receiptBytes = fs.readFileSync(journal.receipt_path);
  const receipt = parseTrustReceipt(receiptBytes, {
    configPath: journal.config_path,
    leaderRoot: canonicalLeader,
    journalPath: transactionPath,
    token: journal.transaction_token,
  });
  recoverCodexReceipt(receipt, receiptBytes, { allowOwnerPid, journalBytes: bytes });
  return true;
}

export function recoverCodexConfigTransactionLock(configPath, { allowOwnerPid = null } = {}) {
  const canonicalConfig = canonicalPathWithMissing(configPath);
  const receiptPath = codexConfigLockPath(canonicalConfig);
  const metadata = lstatIfExists(receiptPath);
  if (!metadata) return false;
  if (metadata.isSymbolicLink() || !metadata.isFile() || metadata.nlink !== 1) {
    throw new Error(`blocked: unsafe Codex config transaction receipt: ${receiptPath}`);
  }
  const receiptBytes = fs.readFileSync(receiptPath);
  const receipt = parseTrustReceipt(receiptBytes, { configPath: canonicalConfig });
  recoverCodexReceipt(receipt, receiptBytes, { allowOwnerPid });
  return true;
}

export function releaseCodexConfigTransactionLock(transaction, { commit = false } = {}) {
  if (!transaction?.started) return;
  if (commit) {
    completeTrustTransaction(transaction);
    return;
  }
  recoverCodexReceipt(transaction.receipt, transaction.receiptBytes, {
    allowOwnerPid: process.pid,
    journalBytes: transaction.journalBytes,
  });
  transaction.started = false;
  transaction.journalBytes = null;
}

function trustTransactionPath(leaderRoot, { createFallback = true } = {}) {
  const result = spawnSync("git", ["rev-parse", "--git-common-dir"], {
    cwd: leaderRoot,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    timeout: 5000,
  });
  if (result.error || result.status !== 0 || !result.stdout.trim()) {
    const fallbackRoot = path.join(fs.realpathSync.native(leaderRoot), ".agent-flow");
    const metadata = lstatIfExists(fallbackRoot);
    if (!metadata) {
      if (!createFallback) return null;
      fs.mkdirSync(fallbackRoot, { mode: 0o700 });
      fsyncDirectory(path.dirname(fallbackRoot));
    } else if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
      throw new Error(`blocked: unsafe agent-flow trust state root: ${fallbackRoot}`);
    }
    return path.join(fallbackRoot, TRUST_TRANSACTION_FILE);
  }
  const candidate = path.resolve(leaderRoot, result.stdout.trim());
  const commonDir = fs.realpathSync.native(candidate);
  const metadata = fs.lstatSync(commonDir);
  if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
    throw new Error("blocked: unsafe git common directory for Codex worktree trust recovery");
  }
  const stateRoot = path.join(commonDir, "agent-flow");
  ensureTrustStateRoot(commonDir, stateRoot);
  return path.join(stateRoot, TRUST_TRANSACTION_FILE);
}

function ensureTrustStateRoot(commonDir, stateRoot) {
  const metadata = lstatIfExists(stateRoot);
  if (metadata) {
    if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
      throw new Error(`blocked: unsafe agent-flow git state root: ${stateRoot}`);
    }
    return;
  }
  try {
    fs.mkdirSync(stateRoot, { mode: 0o700 });
    fsyncDirectory(commonDir);
  } catch (error) {
    const raced = lstatIfExists(stateRoot);
    if (!raced?.isDirectory() || raced.isSymbolicLink()) throw error;
  }
}

function missingConfigParents(configPath) {
  const missing = [];
  let current = path.dirname(configPath);
  while (!lstatIfExists(current)) {
    missing.push(path.resolve(current));
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
  }
  return missing.reverse();
}

function canonicalPathWithMissing(candidate) {
  let current = path.resolve(candidate);
  const missing = [];
  while (!lstatIfExists(current)) {
    const parent = path.dirname(current);
    if (parent === current) break;
    missing.unshift(path.basename(current));
    current = parent;
  }
  return path.join(fs.realpathSync.native(current), ...missing);
}

function encodedOriginalSnapshot(snapshot) {
  return {
    exists: snapshot.exists,
    mode: snapshot.mode,
    sha256: snapshot.exists ? sha256(snapshot.content) : null,
    content_base64: snapshot.exists ? snapshot.content.toString("base64") : null,
  };
}

function decodedOriginalSnapshot(encoded) {
  const content = encoded.exists ? Buffer.from(encoded.content_base64, "base64") : null;
  return { exists: encoded.exists, mode: encoded.mode, content };
}

function snapshotState(snapshot) {
  return {
    exists: snapshot.exists,
    mode: snapshot.mode,
    sha256: snapshot.exists ? sha256(snapshot.content) : null,
  };
}

function snapshotStatesEqual(left, right) {
  return left.exists === right.exists
    && left.mode === right.mode
    && left.sha256 === right.sha256;
}

function parseTrustJournal(bytes, leaderRoot, transactionPath) {
  let journal;
  try {
    journal = JSON.parse(bytes.toString("utf8"));
  } catch (error) {
    throw new Error(`blocked: invalid Codex worktree trust transaction: ${error.message}`);
  }
  if (
    !journal
    || typeof journal !== "object"
    || Array.isArray(journal)
    || !hasExactKeys(journal, [
      "config_path", "created_directories", "leader_root", "receipt_path",
      "status", "transaction_token", "version",
    ])
    || journal.version !== TRUST_TRANSACTION_VERSION
    || !["pending", "authenticated"].includes(journal.status)
    || journal.leader_root !== leaderRoot
    || typeof journal.config_path !== "string"
    || !path.isAbsolute(journal.config_path)
    || path.basename(journal.config_path) !== "config.toml"
    || typeof journal.receipt_path !== "string"
    || typeof journal.transaction_token !== "string"
    || !/^[0-9a-f]{32}$/.test(journal.transaction_token)
    || !Array.isArray(journal.created_directories)
  ) {
    throw new Error(`blocked: invalid Codex worktree trust transaction: ${transactionPath}`);
  }
  const configPath = canonicalPathWithMissing(journal.config_path);
  if (configPath !== journal.config_path) {
    throw new Error(`blocked: invalid Codex worktree trust transaction: ${transactionPath}`);
  }
  if (journal.receipt_path !== codexConfigLockPath(configPath)) {
    throw new Error(`blocked: invalid Codex worktree trust transaction: ${transactionPath}`);
  }
  const seen = new Set();
  for (const directory of journal.created_directories) {
    if (
      typeof directory !== "string"
      || !path.isAbsolute(directory)
      || canonicalPathWithMissing(directory) !== directory
      || directory === path.parse(directory).root
      || seen.has(directory)
      || !configPath.startsWith(`${directory}${path.sep}`)
    ) {
      throw new Error(`blocked: invalid Codex worktree trust transaction: ${transactionPath}`);
    }
    seen.add(directory);
  }
  return journal;
}

function trustJournalPayload({
  status,
  leaderRoot,
  configPath,
  receiptPath,
  token,
  createdDirectories,
}) {
  return {
    version: TRUST_TRANSACTION_VERSION,
    status,
    leader_root: leaderRoot,
    config_path: configPath,
    receipt_path: receiptPath,
    transaction_token: token,
    created_directories: createdDirectories,
  };
}

function codexConfigLockPath(configPath) {
  return path.join(path.dirname(configPath), TRUST_LOCK_FILE);
}

function codexConfigTransactionDir(configPath, token) {
  return path.join(path.dirname(configPath), `${TRUST_TRANSACTION_DIR_PREFIX}${token}`);
}

function jsonBytes(value) {
  return Buffer.from(`${JSON.stringify(value, null, 2)}\n`, "utf8");
}

function replaceTrustReceipt(transaction, receipt) {
  const bytes = jsonBytes(receipt);
  durableReplaceJournal(transaction.receiptPath, transaction.receiptBytes, bytes);
  transaction.receipt = receipt;
  transaction.receiptBytes = bytes;
}

function parseTrustReceipt(bytes, expected = {}) {
  if (!Buffer.isBuffer(bytes) || bytes.length > MAX_RECEIPT_BYTES) {
    throw new Error("blocked: invalid Codex config transaction receipt");
  }
  let receipt;
  try {
    receipt = JSON.parse(bytes.toString("utf8"));
  } catch (error) {
    throw new Error(`blocked: invalid Codex config transaction receipt: ${error.message}`);
  }
  if (
    !receipt
    || typeof receipt !== "object"
    || Array.isArray(receipt)
    || !hasExactKeys(receipt, [
      "config_path", "created_directories", "journal_path", "leader_root", "original",
      "original_path", "owner_pid", "receipt_path", "rollback_path", "rounds", "status",
      "transaction_dir", "transaction_token", "version",
    ])
    || receipt.version !== TRUST_RECEIPT_VERSION
    || !["open", "committed", "rolled-back"].includes(receipt.status)
    || !Number.isSafeInteger(receipt.owner_pid)
    || receipt.owner_pid <= 0
    || typeof receipt.transaction_token !== "string"
    || !/^[0-9a-f]{32}$/.test(receipt.transaction_token)
    || typeof receipt.leader_root !== "string"
    || !path.isAbsolute(receipt.leader_root)
    || path.resolve(receipt.leader_root) !== receipt.leader_root
    || typeof receipt.journal_path !== "string"
    || !path.isAbsolute(receipt.journal_path)
    || path.resolve(receipt.journal_path) !== receipt.journal_path
    || typeof receipt.config_path !== "string"
    || !path.isAbsolute(receipt.config_path)
    || canonicalPathWithMissing(receipt.config_path) !== receipt.config_path
    || path.basename(receipt.config_path) !== "config.toml"
    || receipt.receipt_path !== codexConfigLockPath(receipt.config_path)
    || receipt.transaction_dir !== codexConfigTransactionDir(
      receipt.config_path,
      receipt.transaction_token,
    )
    || receipt.rollback_path !== path.join(receipt.transaction_dir, "rollback")
    || !validOriginalSnapshot(receipt.original)
    || receipt.original_path !== (
      receipt.original.exists ? path.join(receipt.transaction_dir, "original") : null
    )
    || !Array.isArray(receipt.created_directories)
    || !Array.isArray(receipt.rounds)
    || receipt.rounds.length > 8
  ) {
    throw new Error("blocked: invalid Codex config transaction receipt");
  }
  if (
    (expected.configPath && receipt.config_path !== canonicalPathWithMissing(expected.configPath))
    || (expected.leaderRoot && receipt.leader_root !== expected.leaderRoot)
    || (expected.journalPath && receipt.journal_path !== path.resolve(expected.journalPath))
    || (expected.token && receipt.transaction_token !== expected.token)
  ) {
    throw new Error("blocked: Codex config receipt does not authenticate the repository journal");
  }
  const originalContent = receipt.original.exists
    ? Buffer.from(receipt.original.content_base64, "base64")
    : null;
  if (
    receipt.original.exists
    && (
      originalContent.length > MAX_CONFIG_BYTES
      || originalContent.toString("base64") !== receipt.original.content_base64
      || sha256(originalContent) !== receipt.original.sha256
    )
  ) {
    throw new Error("blocked: invalid Codex config transaction receipt");
  }
  validateReceiptCreatedDirectories(receipt);
  validateReceiptRounds(receipt);
  return receipt;
}

function validateReceiptCreatedDirectories(receipt) {
  const seen = new Set();
  for (const directory of receipt.created_directories) {
    if (
      typeof directory !== "string"
      || !path.isAbsolute(directory)
      || canonicalPathWithMissing(directory) !== directory
      || directory === path.parse(directory).root
      || seen.has(directory)
      || !receipt.config_path.startsWith(`${directory}${path.sep}`)
    ) {
      throw new Error("blocked: invalid Codex config transaction receipt");
    }
    seen.add(directory);
  }
}

function validateReceiptRounds(receipt) {
  for (let offset = 0; offset < receipt.rounds.length; offset += 1) {
    const round = receipt.rounds[offset];
    const index = offset + 1;
    if (
      !round
      || typeof round !== "object"
      || Array.isArray(round)
      || !hasExactKeys(round, ["after", "before", "candidate_path", "displaced_path", "index"])
      || round.index !== index
      || !validSnapshotState(round.before)
      || !validSnapshotState(round.after)
      || round.after.exists !== true
      || round.candidate_path !== path.join(receipt.transaction_dir, `candidate-${index}`)
      || round.displaced_path !== path.join(receipt.transaction_dir, `displaced-${index}`)
    ) {
      throw new Error("blocked: invalid Codex config transaction receipt");
    }
  }
}

function recoverCodexReceipt(receipt, receiptBytes, { allowOwnerPid = null, journalBytes = null } = {}) {
  if (receipt.owner_pid !== allowOwnerPid && processIsAlive(receipt.owner_pid)) {
    throw new Error(`blocked: Codex config transaction is active in process ${receipt.owner_pid}`);
  }
  let currentReceipt = receipt;
  let currentBytes = receiptBytes;
  if (receipt.status === "open") {
    rollbackOpenReceipt(receipt);
    currentReceipt = { ...receipt, status: "rolled-back" };
    currentBytes = jsonBytes(currentReceipt);
    durableReplaceJournal(receipt.receipt_path, receiptBytes, currentBytes);
  }
  cleanupCodexTransaction(currentReceipt, currentBytes, journalBytes);
}

function rollbackOpenReceipt(receipt) {
  const original = decodedOriginalSnapshot(receipt.original);
  const originalState = snapshotState(original);
  const allowedStates = [originalState, ...receipt.rounds.flatMap((round) => [round.before, round.after])];
  recoverRollbackArtifact(receipt, original, originalState, allowedStates);
  for (const round of [...receipt.rounds].reverse()) {
    const metadata = lstatIfExists(round.displaced_path);
    if (!metadata) continue;
    const displaced = snapshotConfig(round.displaced_path);
    const displacedState = snapshotState(displaced);
    if (snapshotStatesEqual(displacedState, round.before)) continue;
    restoreExternalDisplacement(receipt.config_path, round.displaced_path, displaced);
  }
  restoreOriginalNoClobber(receipt, original, originalState, allowedStates);
}

function recoverRollbackArtifact(receipt, original, originalState, allowedStates) {
  const metadata = lstatIfExists(receipt.rollback_path);
  if (!metadata) return;
  const retained = snapshotConfig(receipt.rollback_path);
  const retainedState = snapshotState(retained);
  const current = snapshotConfig(receipt.config_path);
  if (!current.exists) {
    if (stateIn(retainedState, allowedStates)) {
      publishOriginalArtifact(receipt, original);
    } else {
      fs.linkSync(receipt.rollback_path, receipt.config_path);
      fsyncDirectory(path.dirname(receipt.config_path));
    }
  }
  const after = snapshotConfig(receipt.config_path);
  if (
    !snapshotStatesEqual(snapshotState(after), retainedState)
    && !snapshotStatesEqual(snapshotState(after), originalState)
  ) {
    throw new Error(`blocked: concurrent Codex config bytes retained at ${receipt.rollback_path}`);
  }
  fs.unlinkSync(receipt.rollback_path);
  fsyncDirectory(receipt.transaction_dir);
}

function restoreOriginalNoClobber(receipt, original, originalState, allowedStates) {
  let current = snapshotConfig(receipt.config_path);
  let currentState = snapshotState(current);
  if (snapshotStatesEqual(currentState, originalState)) return;
  if (!current.exists) {
    publishOriginalArtifact(receipt, original);
    return;
  }
  if (!stateIn(currentState, allowedStates)) return;
  if (lstatIfExists(receipt.rollback_path)) {
    throw new Error(`blocked: Codex rollback retention path already exists: ${receipt.rollback_path}`);
  }
  fs.renameSync(receipt.config_path, receipt.rollback_path);
  fsyncDirectory(path.dirname(receipt.config_path));
  fsyncDirectory(receipt.transaction_dir);
  const retained = snapshotConfig(receipt.rollback_path);
  const retainedState = snapshotState(retained);
  if (!stateIn(retainedState, allowedStates)) {
    restoreExternalDisplacement(receipt.config_path, receipt.rollback_path, retained);
    return;
  }
  publishOriginalArtifact(receipt, original);
  current = snapshotConfig(receipt.config_path);
  currentState = snapshotState(current);
  if (!snapshotStatesEqual(currentState, originalState)) {
    throw new Error("blocked: Codex config rollback did not restore the original state");
  }
  fs.unlinkSync(receipt.rollback_path);
  fsyncDirectory(receipt.transaction_dir);
}

function publishOriginalArtifact(receipt, original) {
  if (!original.exists) return;
  const metadata = lstatIfExists(receipt.original_path);
  if (!metadata) {
    throw new Error("blocked: Codex config original rollback artifact is missing");
  }
  const artifact = snapshotConfig(receipt.original_path);
  if (!snapshotStatesEqual(snapshotState(artifact), snapshotState(original))) {
    throw new Error("blocked: Codex config original rollback artifact changed");
  }
  try {
    fs.linkSync(receipt.original_path, receipt.config_path);
  } catch (error) {
    if (error?.code === "EEXIST") return;
    throw error;
  }
  fsyncDirectory(path.dirname(receipt.config_path));
}

function stateIn(candidate, states) {
  return states.some((state) => snapshotStatesEqual(candidate, state));
}

function cleanupCodexTransaction(receipt, receiptBytes, journalBytes = null) {
  const journalMetadata = lstatIfExists(receipt.journal_path);
  if (journalMetadata) {
    const bytes = fs.readFileSync(receipt.journal_path);
    const journal = parseTrustJournal(bytes, receipt.leader_root, receipt.journal_path);
    if (
      journal.config_path !== receipt.config_path
      || journal.receipt_path !== receipt.receipt_path
      || journal.transaction_token !== receipt.transaction_token
      || (journalBytes && !bytes.equals(journalBytes))
    ) {
      throw new Error("blocked: Codex config receipt journal binding changed before cleanup");
    }
    removeJournalIfUnchanged(receipt.journal_path, bytes);
  }
  removeValidatedTransactionDirectory(receipt);
  removeJournalIfUnchanged(receipt.receipt_path, receiptBytes);
  if (receipt.status === "rolled-back") {
    for (const directory of [...receipt.created_directories].reverse()) {
      const errors = [];
      removeEmptyDirectory(directory, errors);
      if (errors.length > 0) throw new Error(errors.join("; "));
    }
  }
}

function removeValidatedTransactionDirectory(receipt) {
  const metadata = lstatIfExists(receipt.transaction_dir);
  if (!metadata) return;
  if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
    throw new Error(`blocked: unsafe Codex config transaction directory: ${receipt.transaction_dir}`);
  }
  const allowed = new Set([
    ...(receipt.original_path ? [path.basename(receipt.original_path)] : []),
    path.basename(receipt.rollback_path),
    ...receipt.rounds.flatMap((round) => [
      path.basename(round.candidate_path),
      path.basename(round.displaced_path),
    ]),
  ]);
  for (const name of fs.readdirSync(receipt.transaction_dir)) {
    if (!allowed.has(name)) {
      throw new Error(`blocked: unknown Codex config transaction artifact: ${name}`);
    }
  }
  fs.rmSync(receipt.transaction_dir, { recursive: true });
  fsyncDirectory(path.dirname(receipt.transaction_dir));
}

function validOriginalSnapshot(value) {
  return value
    && typeof value === "object"
    && !Array.isArray(value)
    && hasExactKeys(value, ["content_base64", "exists", "mode", "sha256"])
    && typeof value.exists === "boolean"
    && validMode(value.mode)
    && (value.exists
      ? typeof value.content_base64 === "string" && /^[0-9a-f]{64}$/.test(value.sha256)
      : value.content_base64 === null && value.sha256 === null);
}

function validSnapshotState(value) {
  return value
    && typeof value === "object"
    && !Array.isArray(value)
    && hasExactKeys(value, ["exists", "mode", "sha256"])
    && typeof value.exists === "boolean"
    && validMode(value.mode)
    && (value.exists ? /^[0-9a-f]{64}$/.test(value.sha256) : value.sha256 === null);
}

function validMode(value) {
  return Number.isInteger(value) && value >= 0 && value <= 0o777;
}

function hasExactKeys(value, keys) {
  return JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...keys].sort());
}

function processIsAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error?.code !== "ESRCH";
  }
}

function durableCreateJournal(file, bytes) {
  const temporary = durableTemporaryPath(file);
  writeDurableTemporary(temporary, bytes);
  try {
    fs.linkSync(temporary, file);
    fsyncDirectory(path.dirname(file));
  } finally {
    fs.rmSync(temporary, { force: true });
  }
}

function durableReplaceJournal(file, expected, bytes) {
  assertJournalUnchanged(file, expected);
  const temporary = durableTemporaryPath(file);
  writeDurableTemporary(temporary, bytes);
  try {
    assertJournalUnchanged(file, expected);
    fs.renameSync(temporary, file);
    fsyncDirectory(path.dirname(file));
  } catch (error) {
    fs.rmSync(temporary, { force: true });
    throw error;
  }
}

function removeJournalIfUnchanged(file, expected) {
  assertJournalUnchanged(file, expected);
  fs.unlinkSync(file);
  fsyncDirectory(path.dirname(file));
}

function assertJournalUnchanged(file, expected) {
  const metadata = lstatIfExists(file);
  if (
    !metadata?.isFile()
    || metadata.isSymbolicLink()
    || !Buffer.isBuffer(expected)
    || !fs.readFileSync(file).equals(expected)
  ) {
    throw new Error(`blocked: Codex worktree trust transaction changed concurrently: ${file}`);
  }
}

function writeDurableTemporary(file, bytes, mode = 0o600) {
  const descriptor = fs.openSync(file, "wx", 0o600);
  try {
    fs.fchmodSync(descriptor, mode);
    fs.writeFileSync(descriptor, bytes);
    fs.fsyncSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
}

function durableTemporaryPath(file) {
  return `${file}.${process.pid}.${crypto.randomBytes(6).toString("hex")}.tmp`;
}

function fsyncDirectory(directory) {
  if (process.platform === "win32") return;
  const descriptor = fs.openSync(directory, "r");
  try {
    fs.fsyncSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
}

function sha256(content) {
  return crypto.createHash("sha256").update(content).digest("hex");
}

function snapshotConfig(configPath) {
  const stat = lstatIfExists(configPath);
  if (!stat) return { exists: false, content: null, mode: 0o600 };
  if (stat.isSymbolicLink() || !stat.isFile()) {
    throw new Error(`blocked: unsafe Codex config path: ${configPath}`);
  }
  if (stat.size > MAX_CONFIG_BYTES) {
    throw new Error("blocked: Codex config exceeds the 2 MiB trust transaction limit");
  }
  const content = fs.readFileSync(configPath);
  if (content.length > MAX_CONFIG_BYTES) {
    throw new Error("blocked: Codex config exceeds the 2 MiB trust transaction limit");
  }
  return { exists: true, content, mode: stat.mode & 0o777 };
}

function assertConfigUnchanged(configPath, expected) {
  const current = lstatIfExists(configPath);
  if (!expected.exists) {
    if (current) throw new Error(`blocked: Codex config changed during hook trust update: ${configPath}`);
    return;
  }
  if (
    !current?.isFile()
    || current.isSymbolicLink()
    || (current.mode & 0o777) !== expected.mode
    || !fs.readFileSync(configPath).equals(expected.content)
  ) {
    throw new Error(`blocked: Codex config changed during hook trust update: ${configPath}`);
  }
}

function restoreConfig(configPath, original, expected, errors) {
  if (snapshotsEqual(original, expected)) return;
  try {
    assertConfigUnchanged(configPath, expected);
    if (original.exists) atomicWrite(configPath, original.content, original.mode);
    else fs.unlinkSync(configPath);
  } catch (error) {
    errors.push(`${configPath}: ${error.message}`);
  }
}

function snapshotsEqual(left, right) {
  if (left.exists !== right.exists) return false;
  if (!left.exists) return true;
  return left.mode === right.mode && left.content.equals(right.content);
}

function ensureConfigParent(configPath, createdDirectories) {
  const missing = [];
  let current = path.dirname(configPath);
  while (!lstatIfExists(current)) {
    missing.push(current);
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
  }
  const anchor = lstatIfExists(current);
  if (!anchor?.isDirectory() || anchor.isSymbolicLink()) {
    throw new Error(`blocked: unsafe Codex config parent: ${current}`);
  }
  for (const directory of missing.reverse()) {
    fs.mkdirSync(directory, { mode: 0o700 });
    createdDirectories.push(directory);
  }
}

function removeEmptyDirectory(directory, errors) {
  try {
    const stat = lstatIfExists(directory);
    if (stat?.isDirectory() && !stat.isSymbolicLink() && fs.readdirSync(directory).length === 0) {
      fs.rmdirSync(directory);
    }
  } catch (error) {
    errors.push(`${directory}: ${error.message}`);
  }
}

function atomicWrite(file, content, mode) {
  const temporary = `${file}.agent-flow-${process.pid}-${Date.now().toString(16)}.tmp`;
  fs.writeFileSync(temporary, content, { flag: "wx", mode: mode & 0o777 });
  try {
    fs.renameSync(temporary, file);
  } catch (error) {
    fs.rmSync(temporary, { force: true });
    throw error;
  }
}

function createFileExclusive(file, content, mode) {
  const temporary = `${file}.agent-flow-${process.pid}-${Date.now().toString(16)}.tmp`;
  fs.writeFileSync(temporary, content, { flag: "wx", mode: mode & 0o777 });
  try {
    fs.linkSync(temporary, file);
  } finally {
    fs.rmSync(temporary, { force: true });
  }
}

export function tomlBasicStringInterior(value) {
  return JSON.stringify(String(value)).slice(1, -1).replaceAll("\u007f", "\\u007F");
}

function tomlString(value) {
  return `"${tomlBasicStringInterior(value)}"`;
}

function escapeRegex(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function compareCodePoints(left, right) {
  const a = Array.from(String(left), (character) => character.codePointAt(0));
  const b = Array.from(String(right), (character) => character.codePointAt(0));
  for (let index = 0; index < Math.min(a.length, b.length); index += 1) {
    if (a[index] !== b[index]) return a[index] - b[index];
  }
  return a.length - b.length;
}

function sameExistingPath(left, right) {
  try {
    return fs.realpathSync.native(left) === fs.realpathSync.native(right);
  } catch {
    return path.resolve(left) === path.resolve(right);
  }
}

function lstatIfExists(file) {
  try {
    return fs.lstatSync(file);
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}
