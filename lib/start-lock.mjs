import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

export const START_LOCK_VERSION = 1;
export const START_LOCK_KEYS = Object.freeze([
  "version",
  "token",
  "pid",
  "runtime",
  "created_at",
  "project_root",
]);

export function projectStartLockPath(projectRoot) {
  const root = fs.realpathSync.native(projectRoot);
  const result = spawnSync("git", ["rev-parse", "--git-common-dir"], {
    cwd: root,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
  });
  if (!result.error && result.status === 0 && result.stdout.trim()) {
    return path.join(path.resolve(root, result.stdout.trim()), "agent-flow", "start.lock");
  }
  return path.join(root, ".agent-flow", "state", "start.lock");
}

export function acquireProjectStartLock(projectRoot, runtime = "node") {
  const root = fs.realpathSync.native(projectRoot);
  const lockPath = projectStartLockPath(root);
  ensurePlainParent(path.dirname(lockPath));
  const payload = {
    version: START_LOCK_VERSION,
    token: crypto.randomUUID(),
    pid: process.pid,
    runtime,
    created_at: new Date().toISOString(),
    project_root: root,
  };
  let descriptor;
  try {
    descriptor = fs.openSync(lockPath, "wx", 0o600);
  } catch (error) {
    if (error?.code === "EEXIST") throw startLockExistsError(lockPath);
    throw error;
  }
  try {
    fs.writeFileSync(descriptor, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
    fs.fsyncSync(descriptor);
  } catch (error) {
    fs.closeSync(descriptor);
    fs.rmSync(lockPath, { force: true });
    throw error;
  }
  fs.closeSync(descriptor);
  return { path: lockPath, payload };
}

export function releaseProjectStartLock(lock) {
  const stat = lstatIfExists(lock.path);
  if (!stat) return;
  if (stat.isSymbolicLink() || !stat.isFile()) throw startLockExistsError(lock.path);
  let payload;
  try {
    payload = JSON.parse(fs.readFileSync(lock.path, "utf8"));
  } catch {
    throw startLockExistsError(lock.path);
  }
  if (payload?.token !== lock.payload.token) throw startLockExistsError(lock.path);
  fs.unlinkSync(lock.path);
}

function startLockExistsError(lockPath) {
  return new Error(
    `blocked: agent-flow start lock exists at ${lockPath}; another start may be running. `
    + "If no start process is active, inspect the lock and remove it manually before retrying",
  );
}

function ensurePlainParent(directory) {
  const missing = [];
  let current = directory;
  while (!fs.existsSync(current)) {
    missing.push(current);
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
  }
  const existing = lstatIfExists(current);
  if (existing && (existing.isSymbolicLink() || !existing.isDirectory())) {
    throw new Error(`blocked: unsafe start lock parent: ${current}`);
  }
  for (const item of missing.reverse()) fs.mkdirSync(item);
}

function lstatIfExists(file) {
  try {
    return fs.lstatSync(file);
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}
