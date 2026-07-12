import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  START_LOCK_KEYS,
  acquireProjectStartLock,
  projectStartLockPath,
  releaseProjectStartLock,
} from "../lib/start-lock.mjs";

const KIT_ROOT = fileURLToPath(new URL("..", import.meta.url));
const CLI = path.join(KIT_ROOT, "bin", "agent-flow-kit.mjs");

function command(args, cwd, env = {}) {
  return spawnSync(process.execPath, [CLI, ...args], {
    cwd,
    encoding: "utf8",
    env: { ...process.env, AGENT_FLOW_SKIP_CODEX_TRUST: "1", ...env },
  });
}

function git(cwd, args, check = true) {
  const result = spawnSync("git", args, { cwd, encoding: "utf8" });
  if (check) assert.equal(result.status, 0, result.stderr || result.stdout);
  return result;
}

function escapeRegex(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function temporaryRoot(t, prefix = "agent-flow-start-safety-") {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return root;
}

function setupGitProject(t) {
  const root = temporaryRoot(t);
  git(root, ["init", "-b", "main"]);
  git(root, ["config", "user.email", "test@example.com"]);
  git(root, ["config", "user.name", "Test User"]);
  fs.writeFileSync(path.join(root, "README.md"), "fixture\n", "utf8");
  git(root, ["add", "README.md"]);
  git(root, ["commit", "-m", "init"]);
  const install = command(["install"], root);
  assert.equal(install.status, 0, install.stderr || install.stdout);
  return root;
}

function nodePointerRecords(root) {
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
  return pointerPaths.flatMap((pointerPath) => {
    if (!fs.existsSync(pointerPath)) return [];
    const state = JSON.parse(fs.readFileSync(pointerPath, "utf8"));
    return [{
      pointerPath,
      stateRoot: path.dirname(path.dirname(path.dirname(pointerPath))),
      state,
    }];
  });
}

function activeNodePointer(root) {
  const active = nodePointerRecords(root).filter(({ state }) => (
    !["complete", "aborted"].includes(state.status) && state.phase !== "complete"
  ));
  assert.equal(active.length, 1, `expected one active Node pointer, found ${active.length}`);
  return active[0];
}

function nodeRunDir(record) {
  return path.isAbsolute(record.state.run_dir)
    ? record.state.run_dir
    : path.resolve(record.stateRoot, record.state.run_dir);
}

function completeNodeRun(root) {
  const record = activeNodePointer(root);
  const { pointerPath, state } = record;
  const completed = { ...state, status: "complete", phase: "complete" };
  fs.writeFileSync(pointerPath, `${JSON.stringify(completed, null, 2)}\n`, "utf8");
  fs.writeFileSync(
    path.join(nodeRunDir(record), "manifest.json"),
    `${JSON.stringify(completed, null, 2)}\n`,
    "utf8",
  );
  return state;
}

test("Node required profile rejects non-git start and disabled profile permits leader", (t) => {
  const root = temporaryRoot(t, "agent-flow-non-git-");
  const install = command(["install"], root);
  assert.equal(install.status, 0, install.stderr || install.stdout);

  const required = command([
    "run", "start", "--task", "required task", "--workflow", "default", "--run-id", "required",
  ], root);
  assert.notEqual(required.status, 0);
  assert.match(required.stderr, /requires a registered git worktree/);
  assert.equal(fs.existsSync(path.join(root, ".agent-flow", "runs", "default", "required")), false);

  const profilePath = path.join(root, ".agent-flow", "profiles", "generic.yaml");
  fs.writeFileSync(
    profilePath,
    fs.readFileSync(profilePath, "utf8").replace("worktree: required", "worktree: disabled"),
    "utf8",
  );
  const disabled = command([
    "run", "start", "--task", "disabled task", "--workflow", "default", "--run-id", "disabled",
  ], root);
  assert.equal(disabled.status, 0, disabled.stderr || disabled.stdout);
  const state = JSON.parse(fs.readFileSync(path.join(root, ".agent-flow", "state", "current-run.json"), "utf8"));
  assert.equal(state.workspace_root, fs.realpathSync.native(root));
  assert.equal(state.worktree_mode, "disabled");
});

test("Node start lock blocks cross-runtime starts before worktree creation", (t) => {
  const root = setupGitProject(t);
  const lock = acquireProjectStartLock(root, "node");
  t.after(() => {
    if (fs.existsSync(lock.path)) releaseProjectStartLock(lock);
  });
  assert.equal(lock.path, path.join(fs.realpathSync.native(root), ".git", "agent-flow", "start.lock"));
  assert.deepEqual(Object.keys(JSON.parse(fs.readFileSync(lock.path, "utf8"))), [...START_LOCK_KEYS]);
  assert.equal(projectStartLockPath(root), lock.path);

  const python = spawnSync("python3", [
    "-m", "agent_flow.cli", "run", "python contender", "--root", root, "--workflow", "default",
  ], {
    cwd: KIT_ROOT,
    encoding: "utf8",
    env: { ...process.env, PYTHONPATH: path.join(KIT_ROOT, "src") },
  });
  assert.equal(python.status, 2);
  assert.match(python.stderr, /start lock exists/);
  assert.equal(fs.existsSync(path.join(root, ".agent-flow", "worktrees", "feat-python-contender")), false);

  const node = command(["run", "start", "--task", "node contender", "--workflow", "default"], root);
  assert.notEqual(node.status, 0);
  assert.match(node.stderr, /start lock exists/);
  assert.equal(fs.existsSync(path.join(root, ".agent-flow", "worktrees", "feat-node-contender")), false);
  assert.equal(fs.existsSync(lock.path), true);
});

test("Node new runs suffix completed semantic worktree collisions including old-worktree cwd", (t) => {
  const root = setupGitProject(t);
  const first = command([
    "run", "start", "--task", "checkout.png payment bug", "--workflow", "default", "--run-id", "collision-1",
  ], root);
  assert.equal(first.status, 0, first.stderr || first.stdout);
  const firstState = completeNodeRun(root);
  assert.equal(path.basename(firstState.workspace_root), "feat-payment-bug");
  assert.equal(git(firstState.workspace_root, ["branch", "--show-current"]).stdout.trim(), "feat/payment-bug");

  const second = command([
    "run", "start", "--task", "checkout.png payment bug", "--workflow", "default", "--run-id", "collision-2",
  ], root);
  assert.equal(second.status, 0, second.stderr || second.stdout);
  const secondState = completeNodeRun(root);
  assert.equal(path.basename(secondState.workspace_root), "feat-payment-bug-2");
  assert.equal(git(secondState.workspace_root, ["branch", "--show-current"]).stdout.trim(), "feat/payment-bug-2");

  const third = command([
    "run", "start", "--task", "payment bug", "--workflow", "default", "--run-id", "collision-3",
  ], firstState.workspace_root);
  assert.equal(third.status, 0, third.stderr || third.stdout);
  const thirdState = activeNodePointer(root).state;
  assert.equal(path.basename(thirdState.workspace_root), "feat-payment-bug-3");
  assert.notEqual(fs.realpathSync(thirdState.workspace_root), fs.realpathSync(firstState.workspace_root));
});

test("Node collision suffix truncates non-BMP slugs by code point like Python", (t) => {
  const root = setupGitProject(t);
  const task = "𐐀".repeat(60);
  const normalized = task.normalize("NFKC").toLowerCase();
  const first = command([
    "run", "start", "--task", task, "--workflow", "default", "--run-id", "unicode-1",
  ], root);
  assert.equal(first.status, 0, first.stderr || first.stdout);
  const firstState = completeNodeRun(root);
  assert.equal(path.basename(firstState.workspace_root), `feat-${normalized}`);

  const second = command([
    "run", "start", "--task", task, "--workflow", "default", "--run-id", "unicode-2",
  ], root);
  assert.equal(second.status, 0, second.stderr || second.stdout);
  const secondState = activeNodePointer(root).state;
  const expectedStem = Array.from(normalized).slice(0, 58).join("");
  assert.equal(path.basename(secondState.workspace_root), `feat-${expectedStem}-2`);
  assert.equal(Array.from(path.basename(secondState.workspace_root).slice("feat-".length)).length, 60);
});

test("Node does not reuse a registered checkout with Python run history", (t) => {
  const root = setupGitProject(t);
  const task = "cross runtime history";
  const python = spawnSync("python3", [
    "-m",
    "agent_flow.cli",
    "run",
    task,
    "--root",
    root,
    "--workflow",
    "default",
  ], {
    cwd: KIT_ROOT,
    encoding: "utf8",
    env: {
      ...process.env,
      PYTHONPATH: path.join(KIT_ROOT, "src"),
      AGENT_FLOW_ADAPTER: "generic",
      AGENT_FLOW_GENERIC_MODE: "stub-success",
      AGENT_FLOW_SKIP_CODEX_TRUST: "1",
    },
  });
  assert.equal(python.status, 0, python.stderr || python.stdout);
  const pythonWorktree = path.join(root, ".agent-flow", "worktrees", "feat-cross-runtime-history");
  const commonDir = path.resolve(root, git(root, ["rev-parse", "--git-common-dir"]).stdout.trim());
  const runsRoot = path.join(
    commonDir,
    "agent-flow",
    "worktrees",
    "feat-cross-runtime-history",
    ".agent-flow",
    "runs",
  );
  const activeMarkers = fs.readdirSync(runsRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => path.join(runsRoot, entry.name, "active"))
    .filter((candidate) => fs.existsSync(candidate));
  assert.equal(activeMarkers.length, 1);
  fs.unlinkSync(activeMarkers[0]);

  const node = command([
    "run", "start", "--task", task, "--workflow", "default", "--run-id", "node-after-python",
  ], pythonWorktree);
  assert.equal(node.status, 0, node.stderr || node.stdout);
  const state = activeNodePointer(root).state;
  assert.equal(path.basename(state.workspace_root), "feat-cross-runtime-history-2");
  assert.notEqual(fs.realpathSync.native(state.workspace_root), fs.realpathSync.native(pythonWorktree));
});

test("Node suffixes a stale Python runtime registration after checkout removal", (t) => {
  const root = setupGitProject(t);
  const task = "stale python runtime";
  const python = spawnSync("python3", [
    "-m",
    "agent_flow.cli",
    "run",
    task,
    "--root",
    root,
    "--workflow",
    "default",
  ], {
    cwd: KIT_ROOT,
    encoding: "utf8",
    env: {
      ...process.env,
      PYTHONPATH: path.join(KIT_ROOT, "src"),
      AGENT_FLOW_ADAPTER: "generic",
      AGENT_FLOW_GENERIC_MODE: "stub-success",
      AGENT_FLOW_SKIP_CODEX_TRUST: "1",
    },
  });
  assert.equal(python.status, 0, python.stderr || python.stdout);
  const worktreeName = "feat-stale-python-runtime";
  const pythonWorktree = path.join(root, ".agent-flow", "worktrees", worktreeName);
  const commonDir = path.resolve(root, git(root, ["rev-parse", "--git-common-dir"]).stdout.trim());
  const runtimeRoot = path.join(commonDir, "agent-flow", "worktrees", worktreeName);
  const activeMarkers = fs.readdirSync(path.join(runtimeRoot, ".agent-flow", "runs"), { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => path.join(runtimeRoot, ".agent-flow", "runs", entry.name, "active"))
    .filter((candidate) => fs.existsSync(candidate));
  assert.equal(activeMarkers.length, 1);
  fs.unlinkSync(activeMarkers[0]);
  git(root, ["worktree", "remove", "--force", pythonWorktree]);
  git(root, ["branch", "-D", "feat/stale-python-runtime"]);
  assert.equal(fs.existsSync(pythonWorktree), false);
  assert.equal(fs.existsSync(runtimeRoot), true);

  const node = command([
    "run", "start", "--task", task, "--workflow", "default", "--run-id", "node-after-stale-python",
  ], root);
  assert.equal(node.status, 0, node.stderr || node.stdout);
  const state = activeNodePointer(root).state;
  assert.equal(path.basename(state.workspace_root), "feat-stale-python-runtime-2");
});

test("Node prints the canonical leader run artifact from the pinned worktree", (t) => {
  const root = setupGitProject(t);
  const started = command([
    "run", "start", "--task", "absolute artifact", "--workflow", "default", "--run-id", "absolute-artifact",
  ], root);
  assert.equal(started.status, 0, started.stderr || started.stdout);
  const state = activeNodePointer(root).state;
  const runDir = fs.realpathSync.native(state.run_dir);
  const expectedArtifact = path.join(runDir, "design.md");
  assert.match(started.stdout, new RegExp(`Required artifact: ${escapeRegex(expectedArtifact)}(?:\\n|$)`));

  const status = command(["status"], state.workspace_root);
  assert.equal(status.status, 0, status.stderr || status.stdout);
  assert.match(status.stdout, new RegExp(`required_artifact: ${escapeRegex(expectedArtifact)}(?:\\n|$)`));
  const statusJson = status.stdout
    .split(/\r?\n/)
    .find((line) => line.startsWith("status_json: "));
  assert.ok(statusJson);
  assert.equal(JSON.parse(statusJson.slice("status_json: ".length)).required_artifact, expectedArtifact);
  assert.equal(path.dirname(expectedArtifact).startsWith(fs.realpathSync.native(state.workspace_root)), false);
});

test("Node explicit worktree reuses an exact registered manifest and rejects branch-only collisions", (t) => {
  const root = setupGitProject(t);
  const first = command([
    "run", "start", "--task", "first explicit run", "--workflow", "default", "--run-id", "explicit-1",
    "--worktree", "shared slice", "--worktree-branch", "feat/shared-slice",
  ], root);
  assert.equal(first.status, 0, first.stderr || first.stdout);
  const firstState = completeNodeRun(root);
  assert.equal(path.basename(firstState.workspace_root), "feat-shared-slice");
  const commonDir = git(root, ["rev-parse", "--git-common-dir"]).stdout.trim();
  const registration = path.join(
    path.resolve(root, commonDir),
    "agent-flow", "worktrees", "feat-shared-slice", "manifest.json",
  );
  assert.equal(fs.lstatSync(registration).isFile(), true);

  const second = command([
    "run", "start", "--task", "second explicit run", "--workflow", "default", "--run-id", "explicit-2",
    "--worktree", "shared slice", "--worktree-branch", "feat/shared-slice",
  ], root);
  assert.equal(second.status, 0, second.stderr || second.stdout);
  const secondState = completeNodeRun(root);
  assert.equal(fs.realpathSync(secondState.workspace_root), fs.realpathSync(firstState.workspace_root));

  git(root, ["branch", "feat/branch-only", "main"]);
  const branchOnly = command([
    "run", "start", "--task", "branch only", "--workflow", "default", "--run-id", "branch-only",
    "--worktree-branch", "feat/branch-only",
  ], root);
  assert.notEqual(branchOnly.status, 0);
  assert.match(branchOnly.stderr, /explicit worktree branch already exists/);
  assert.equal(fs.existsSync(path.join(root, ".agent-flow", "worktrees", "feat-branch-only")), false);
});

test("Node stale start lock fails closed with manual recovery guidance", (t) => {
  const root = setupGitProject(t);
  const lockPath = projectStartLockPath(root);
  fs.mkdirSync(path.dirname(lockPath), { recursive: true });
  fs.writeFileSync(lockPath, "{broken\n", { encoding: "utf8", mode: 0o600 });

  const result = command(["run", "start", "--task", "stale lock task", "--workflow", "default"], root);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /inspect the lock and remove it manually/);
  assert.equal(fs.existsSync(lockPath), true);
  assert.equal(fs.existsSync(path.join(root, ".agent-flow", "worktrees", "feat-stale-lock-task")), false);
});

test("Node cleans a newly-created worktree when host bridge setup fails", (t) => {
  const root = setupGitProject(t);
  fs.unlinkSync(path.join(root, "CLAUDE.md"));
  const result = command([
    "run", "start", "--task", "cleanup bridge", "--workflow", "default", "--run-id", "cleanup",
  ], root);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /host bridge source is missing/);
  assert.equal(fs.existsSync(path.join(root, ".agent-flow", "worktrees", "feat-cleanup-bridge")), false);
  assert.notEqual(git(root, ["show-ref", "--verify", "refs/heads/feat/cleanup-bridge"], false).status, 0);
  assert.equal(fs.existsSync(projectStartLockPath(root)), false);
});

test("Node active required workspace rejects leader, unregistered, detached, and protected", (t) => {
  const root = setupGitProject(t);
  const started = command([
    "run", "start", "--task", "workspace policy", "--workflow", "default", "--run-id", "policy",
  ], root);
  assert.equal(started.status, 0, started.stderr || started.stdout);
  const record = activeNodePointer(root);
  const pointerPath = record.pointerPath;
  const manifestPath = path.join(nodeRunDir(record), "manifest.json");
  const original = record.state;
  const writeWorkspace = (workspace) => {
    const value = { ...original, workspace_root: workspace };
    fs.writeFileSync(pointerPath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
    fs.writeFileSync(manifestPath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
  };

  writeWorkspace(root);
  assert.match(command(["status"], root).stderr, /leader checkout/);
  writeWorkspace(path.join(root, "unregistered"));
  assert.match(command(["status"], root).stderr, /not a registered git worktree/);

  writeWorkspace(original.workspace_root);
  git(original.workspace_root, ["switch", "--detach"]);
  assert.match(command(["status"], root).stderr, /detached HEAD/);
  git(root, ["branch", "develop", "main"]);
  git(original.workspace_root, ["switch", "develop"]);
  assert.match(command(["run", "advance"], root).stderr, /protected branch develop/);
});

test("Node install scans active manifests when the current pointer is missing", (t) => {
  const root = setupGitProject(t);
  const started = command([
    "run", "start", "--task", "install guard", "--workflow", "default", "--run-id", "install-guard",
  ], root);
  assert.equal(started.status, 0, started.stderr || started.stdout);
  const pointer = activeNodePointer(root).pointerPath;
  fs.unlinkSync(pointer);

  const install = command(["install"], root);
  assert.notEqual(install.status, 0);
  assert.match(install.stderr, /install blocked while run install-guard is active/);
  assert.equal(JSON.parse(fs.readFileSync(pointer, "utf8")).run_id, "install-guard");
  fs.writeFileSync(pointer, "{broken\n", "utf8");
  const corrupt = command(["install"], root);
  assert.notEqual(corrupt.status, 0);
  assert.match(corrupt.stderr, /unreadable current run pointer/);
  assert.equal(fs.readFileSync(pointer, "utf8"), "{broken\n");
});

test("Python start is blocked after the Node start lock is released", (t) => {
  const root = setupGitProject(t);
  const started = command([
    "run", "start", "--task", "node active", "--workflow", "default", "--run-id", "node-active",
  ], root);
  assert.equal(started.status, 0, started.stderr || started.stdout);
  assert.equal(fs.existsSync(projectStartLockPath(root)), false);

  const python = spawnSync("python3", [
    "-m", "agent_flow.cli", "run", "python later", "--root", root, "--workflow", "default",
  ], {
    cwd: KIT_ROOT,
    encoding: "utf8",
    env: { ...process.env, PYTHONPATH: path.join(KIT_ROOT, "src") },
  });
  assert.equal(python.status, 2);
  assert.match(python.stdout, /already active: node-active/);
  assert.equal(fs.existsSync(path.join(root, ".agent-flow/worktrees/feat-python-later")), false);
});
