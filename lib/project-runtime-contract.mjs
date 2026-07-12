import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

export const PROJECT_RUNTIME_CONTRACT_VERSION = 2;
export const PROJECT_RUNTIME_CONTRACT_COMMITMENT_VERSION = 2;
export const PROJECT_LAUNCHER_RELATIVE = ".agent-flow/bin/agent-flow";
export const NODE_RUNTIME_ROOT_RELATIVE = ".agent-flow/runtime/node";
export const NODE_RUNTIME_ENTRYPOINT_RELATIVE = `${NODE_RUNTIME_ROOT_RELATIVE}/bin/agent-flow-kit.mjs`;
export const PYTHON_RUNTIME_ROOT_RELATIVE = ".agent-flow/runtime/python";

const SHA256 = /^[0-9a-f]{64}$/;

export function sha256Bytes(content) {
  return crypto.createHash("sha256").update(content).digest("hex");
}

export function hashRuntimeTree(root, { label = "pinned runtime" } = {}) {
  const metadata = fs.lstatSync(root);
  if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
    throw new Error(`${label} root is unsafe: ${root}`);
  }
  const files = [];
  const visit = (directory) => {
    const entries = fs.readdirSync(directory, { withFileTypes: true })
      .sort((left, right) => compareCodePoints(left.name, right.name));
    for (const entry of entries) {
      const candidate = path.join(directory, entry.name);
      if (entry.isSymbolicLink()) {
        throw new Error(`${label} may not contain symlinks: ${candidate}`);
      }
      if (entry.isDirectory()) {
        visit(candidate);
      } else if (entry.isFile()) {
        if (fs.lstatSync(candidate).nlink !== 1) {
          throw new Error(`${label} may not contain hard-linked files: ${candidate}`);
        }
        files.push(candidate);
      } else {
        throw new Error(`${label} may contain only regular files: ${candidate}`);
      }
    }
  };
  visit(root);
  files.sort(compareCodePoints);
  const digest = crypto.createHash("sha256");
  for (const file of files) {
    digest.update(path.relative(root, file).split(path.sep).join("/"));
    digest.update("\0");
    digest.update(fs.readFileSync(file));
    digest.update("\0");
  }
  return digest.digest("hex");
}

export function buildProjectRuntimeContract({ launcherBytes, nodeRuntimeRoot, pythonRuntimeRoot }) {
  const contract = {
    version: PROJECT_RUNTIME_CONTRACT_VERSION,
    launcher: {
      path: PROJECT_LAUNCHER_RELATIVE,
      sha256: sha256Bytes(launcherBytes),
    },
    node_runtime: {
      root: NODE_RUNTIME_ROOT_RELATIVE,
      entrypoint: NODE_RUNTIME_ENTRYPOINT_RELATIVE,
      tree_hash: hashRuntimeTree(nodeRuntimeRoot, { label: "pinned Node runtime" }),
    },
    python_runtime: {
      root: PYTHON_RUNTIME_ROOT_RELATIVE,
      tree_hash: hashRuntimeTree(pythonRuntimeRoot, { label: "pinned Python runtime" }),
    },
  };
  return {
    contract,
    commitment: projectRuntimeContractCommitment(contract),
  };
}

export function projectRuntimeContractCommitment(contract) {
  const normalized = normalizeProjectRuntimeContract(contract);
  return sha256Bytes(Buffer.from(JSON.stringify({
    version: PROJECT_RUNTIME_CONTRACT_COMMITMENT_VERSION,
    contract: normalized,
  }), "utf8"));
}

export function assertProjectRuntimeContractPayload(payload) {
  if (
    !isPlainObject(payload)
    || payload.project_runtime_contract_commitment_version
      !== PROJECT_RUNTIME_CONTRACT_COMMITMENT_VERSION
    || typeof payload.project_runtime_contract_commitment !== "string"
    || !SHA256.test(payload.project_runtime_contract_commitment)
  ) {
    throw new Error("installed project runtime commitment is invalid");
  }
  const normalized = normalizeProjectRuntimeContract(payload.project_runtime_contract);
  const expected = projectRuntimeContractCommitment(payload.project_runtime_contract);
  if (expected !== payload.project_runtime_contract_commitment) {
    throw new Error("installed project runtime commitment does not match provenance");
  }
  if (
    !isPlainObject(payload.node_runtime)
    || !hasExactKeys(payload.node_runtime, ["path", "tree_hash"])
    || payload.node_runtime.path !== NODE_RUNTIME_ENTRYPOINT_RELATIVE
    || payload.node_runtime.tree_hash !== normalized.node_runtime.tree_hash
    || !isPlainObject(payload.python_runtime)
    || !hasExactKeys(payload.python_runtime, ["path", "tree_hash"])
    || payload.python_runtime.path !== PYTHON_RUNTIME_ROOT_RELATIVE
    || payload.python_runtime.tree_hash !== normalized.python_runtime.tree_hash
  ) {
    throw new Error("installed runtime compatibility metadata is invalid");
  }
  return normalized;
}

export function assertProjectRuntimeInstalled(root, payload) {
  const contract = assertProjectRuntimeContractPayload(payload);
  const launcher = requireRegularDescendant(
    root,
    contract.launcher.path,
    "pinned project launcher",
  );
  if (process.platform !== "win32" && (fs.statSync(launcher).mode & 0o111) === 0) {
    throw new Error("pinned project launcher is not executable");
  }
  if (sha256Bytes(fs.readFileSync(launcher)) !== contract.launcher.sha256) {
    throw new Error("pinned project launcher changed after install");
  }
  const runtimeRoot = requireDirectoryDescendant(
    root,
    contract.node_runtime.root,
    "pinned Node runtime root",
  );
  requireRegularDescendant(
    root,
    contract.node_runtime.entrypoint,
    "pinned Node runtime entrypoint",
  );
  if (hashRuntimeTree(runtimeRoot, { label: "pinned Node runtime" }) !== contract.node_runtime.tree_hash) {
    throw new Error("pinned Node runtime changed after install");
  }
  const pythonRuntimeRoot = requireDirectoryDescendant(
    root,
    contract.python_runtime.root,
    "pinned Python runtime root",
  );
  if (
    hashRuntimeTree(pythonRuntimeRoot, { label: "pinned Python runtime" })
    !== contract.python_runtime.tree_hash
  ) {
    throw new Error("pinned Python runtime changed after install");
  }
  return { contract, launcher, runtimeRoot, pythonRuntimeRoot };
}

function normalizeProjectRuntimeContract(contract) {
  if (
    !isPlainObject(contract)
    || !hasExactKeys(contract, ["launcher", "node_runtime", "python_runtime", "version"])
    || contract.version !== PROJECT_RUNTIME_CONTRACT_VERSION
    || !isPlainObject(contract.launcher)
    || !hasExactKeys(contract.launcher, ["path", "sha256"])
    || contract.launcher.path !== PROJECT_LAUNCHER_RELATIVE
    || typeof contract.launcher.sha256 !== "string"
    || !SHA256.test(contract.launcher.sha256)
    || !isPlainObject(contract.node_runtime)
    || !hasExactKeys(contract.node_runtime, ["entrypoint", "root", "tree_hash"])
    || contract.node_runtime.root !== NODE_RUNTIME_ROOT_RELATIVE
    || contract.node_runtime.entrypoint !== NODE_RUNTIME_ENTRYPOINT_RELATIVE
    || typeof contract.node_runtime.tree_hash !== "string"
    || !SHA256.test(contract.node_runtime.tree_hash)
    || !isPlainObject(contract.python_runtime)
    || !hasExactKeys(contract.python_runtime, ["root", "tree_hash"])
    || contract.python_runtime.root !== PYTHON_RUNTIME_ROOT_RELATIVE
    || typeof contract.python_runtime.tree_hash !== "string"
    || !SHA256.test(contract.python_runtime.tree_hash)
  ) {
    throw new Error("installed project runtime contract is invalid");
  }
  return contract;
}

function requireRegularDescendant(root, relative, label) {
  return requireDescendant(root, relative, label, "file");
}

function requireDirectoryDescendant(root, relative, label) {
  return requireDescendant(root, relative, label, "directory");
}

function requireDescendant(root, relative, label, kind) {
  const projectRoot = path.resolve(root);
  let cursor = projectRoot;
  const parts = relative.split("/");
  for (let index = 0; index < parts.length; index += 1) {
    const part = parts[index];
    if (!part || part === "." || part === "..") {
      throw new Error(`${label} path is invalid`);
    }
    cursor = path.join(cursor, part);
    const metadata = fs.lstatSync(cursor);
    if (metadata.isSymbolicLink()) {
      throw new Error(`${label} path may not use symlinks: ${cursor}`);
    }
    const final = index === parts.length - 1;
    if (final ? (kind === "file" ? !metadata.isFile() : !metadata.isDirectory()) : !metadata.isDirectory()) {
      throw new Error(`${label} path has an invalid component: ${cursor}`);
    }
    if (final && kind === "file" && metadata.nlink !== 1) {
      throw new Error(`${label} may not be hard-linked: ${cursor}`);
    }
  }
  const relativeToRoot = path.relative(projectRoot, cursor);
  if (!relativeToRoot || relativeToRoot.startsWith("..") || path.isAbsolute(relativeToRoot)) {
    throw new Error(`${label} escapes the project`);
  }
  return cursor;
}

function isPlainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function hasExactKeys(value, keys) {
  const actual = Object.keys(value).sort(compareCodePoints);
  const expected = [...keys].sort(compareCodePoints);
  return JSON.stringify(actual) === JSON.stringify(expected);
}

function compareCodePoints(left, right) {
  const a = Array.from(String(left), (char) => char.codePointAt(0));
  const b = Array.from(String(right), (char) => char.codePointAt(0));
  for (let index = 0; index < Math.min(a.length, b.length); index += 1) {
    if (a[index] !== b[index]) return a[index] - b[index];
  }
  return a.length - b.length;
}
