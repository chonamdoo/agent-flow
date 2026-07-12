import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { parseWorkflowYaml } from "../lib/yaml-runtime.mjs";

const KIT_ROOT = fileURLToPath(new URL("..", import.meta.url));
const CLI = path.join(KIT_ROOT, "bin", "agent-flow-kit.mjs");

function run(args, cwd, env = {}) {
  return spawnSync(process.execPath, [CLI, ...args], {
    cwd,
    encoding: "utf8",
    env: {
      ...process.env,
      AGENT_FLOW_SKIP_CODEX_TRUST: "1",
      PYTHONPATH: [path.join(KIT_ROOT, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
      ...env,
    },
  });
}

test("Node changed-file query failure is fail closed", (t) => {
  const root = setupProject(t);
  const start = run([
    "run", "start", "--task", "changed file failure", "--workflow", "default", "--run-id", "changed-files",
  ], root);
  assert.equal(start.status, 0, start.stderr || start.stdout);
  const workspace = start.stdout.match(/^workspace_root: (.+)$/m)?.[1];
  assert.ok(workspace);
  const fakeBin = path.join(root, "fake-bin");
  fs.mkdirSync(fakeBin);
  const realGit = spawnSync("which", ["git"], { encoding: "utf8" }).stdout.trim();
  const fakeGit = path.join(fakeBin, "git");
  fs.writeFileSync(
    fakeGit,
    `#!/bin/sh\nif [ "$1" = "diff" ]; then exit 2; fi\nexec "${realGit}" "$@"\n`,
    { encoding: "utf8", mode: 0o755 },
  );

  const next = run(["run", "next"], workspace, {
    PATH: `${fakeBin}${path.delimiter}${process.env.PATH ?? ""}`,
  });
  assert.notEqual(next.status, 0);
  assert.match(next.stderr, /changed-file query failed: git diff/);
});

function git(cwd, args) {
  const result = spawnSync("git", args, { cwd, encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr || result.stdout);
}

function writeJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

function setupProject(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-node-semantics-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  git(root, ["init", "-b", "main"]);
  git(root, ["config", "user.email", "test@example.com"]);
  git(root, ["config", "user.name", "Test User"]);
  fs.writeFileSync(path.join(root, "README.md"), "fixture\n", "utf8");
  git(root, ["add", "README.md"]);
  git(root, ["commit", "-m", "init"]);
  const install = run(["install"], root);
  assert.equal(install.status, 0, install.stderr || install.stdout);
  return root;
}

function assertOrdered(text, values, label) {
  let cursor = -1;
  for (const value of values) {
    cursor = text.indexOf(value, cursor + 1);
    assert.notEqual(cursor, -1, `${label} missing ordered value ${value}`);
  }
}

test("Node workflow parser rejects empty routes for gates and multi-review", () => {
  for (const [id, extra] of [["gates", ""], ["multi-review", "    multi_review: true\n"]]) {
    const yaml = `id: invalid\nphases:\n  - id: ${id}\n${extra}    routes: {}\n`;
    assert.throws(
      () => parseWorkflowYaml(yaml, "invalid"),
      /requires non-empty routes/,
    );
  }
  assert.throws(
    () => parseWorkflowYaml(
      "id: invalid\nphases:\n  - id: multi-review\n    routes:\n      approve: block\n",
      "invalid",
    ),
    /must set multi_review: true/,
  );
});

test("public docs match the supported hosts and workflow contract", () => {
  const readme = fs.readFileSync(path.join(KIT_ROOT, "README.md"), "utf8");
  const packageJson = JSON.parse(fs.readFileSync(path.join(KIT_ROOT, "package.json"), "utf8"));
  for (const host of ["Claude", "Codex", "OMP"]) {
    assert.match(readme, new RegExp(host));
    assert.match(packageJson.description, new RegExp(host));
  }
  assert.match(readme, /active host must run at least two independent in-host sub-agents/);
  assert.match(readme, /other than the active host may add optional review evidence/);
  for (const command of [
    './.agent-flow/bin/agent-flow run "<task>"',
    "./.agent-flow/bin/agent-flow status",
    "./.agent-flow/bin/agent-flow run next",
    "./.agent-flow/bin/agent-flow run advance",
  ]) {
    assert.match(readme, new RegExp(command.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  }

  const defaultStart = readme.indexOf("default:\n");
  const fullStart = readme.indexOf("full-feature:\n", defaultStart);
  assert.notEqual(defaultStart, -1);
  assert.notEqual(fullStart, -1);
  assertOrdered(readme.slice(defaultStart, fullStart), [
    "design", "slice-plan", "worktree", "implement", "comment-authoring",
    "final-review", "gates", "fix-loop", "comment-authoring", "final-review",
    "gates", "commit", "push-pr", "pr-watch", "pr-comment-fix", "pr-ci-fix",
    "merge", "cleanup",
  ], "default workflow docs");
  assertOrdered(readme.slice(fullStart, readme.indexOf("```", fullStart)), [
    "domain-grill", "product-brief", "prd", "slice-plan", "plan-review",
    "ddd-design", "worktree", "run-start", "red", "green", "refactor",
    "comment-authoring", "multi-review", "architecture-review", "gates",
    "fix-loop", "comment-authoring", "multi-review", "architecture-review",
    "gates", "commit", "push-pr", "pr-watch", "pr-comment-fix", "pr-ci-fix",
    "merge-approval", "merge", "handoff",
  ], "full-feature workflow docs");
});

test("Node preserves cite_lore and pause_after workflow semantics", (t) => {
  const root = setupProject(t);
  const loreDir = path.join(root, ".agent-flow", "memory", "lore");
  fs.mkdirSync(loreDir, { recursive: true });
  fs.writeFileSync(path.join(loreDir, "auth.md"), [
    "---",
    "title: Auth token boundary",
    "type: decision",
    "date: 2026-07-11",
    "scope: auth",
    "weight: 1.0",
    "tags: [auth, token]",
    "---",
    "# Auth token boundary",
    "",
    "## Constraint",
    "Tokens stay outside the domain model.",
    "",
    "## Rejected",
    "- Domain token storage: leaks infrastructure.",
    "",
    "## Directive",
    "Map authentication at the repository boundary.",
    "",
  ].join("\n"), "utf8");

  const start = run([
    "run", "start", "--task", "auth token refactor", "--workflow", "default", "--run-id", "node-semantics",
  ], root);
  assert.equal(start.status, 0, start.stderr || start.stdout);
  assert.match(start.stdout, /## Relevant lore/);
  assert.match(start.stdout, /\.agent-flow\/memory\/lore\/auth\.md/);
  assert.match(start.stdout, /Tokens stay outside the domain model/);

  const statePath = path.join(root, ".agent-flow", "state", "current-run.json");
  const state = JSON.parse(fs.readFileSync(statePath, "utf8"));
  assert.ok(start.stdout.includes(`workspace_root: ${state.workspace_root}`));
  assert.match(start.stdout, /Work cwd policy: keep every source, build, test, and write tool call/);
  fs.writeFileSync(
    path.join(loreDir, "auth.md"),
    fs.readFileSync(path.join(loreDir, "auth.md"), "utf8")
      .replace("Tokens stay outside the domain model.", "MUTATED AFTER RUN START."),
    "utf8",
  );
  const pinnedPrompt = run(["run", "next"], state.workspace_root);
  assert.equal(pinnedPrompt.status, 0, pinnedPrompt.stderr || pinnedPrompt.stdout);
  assert.match(pinnedPrompt.stdout, /Tokens stay outside the domain model/);
  assert.doesNotMatch(pinnedPrompt.stdout, /MUTATED AFTER RUN START/);
  assert.ok(pinnedPrompt.stdout.includes(`workspace_root: ${state.workspace_root}`));
  const runDir = path.resolve(root, state.run_dir);
  const enteredAt = new Date(Date.now() - 5_000).toISOString();
  const sliceState = {
    ...state,
    phase_index: 1,
    phase: "slice-plan",
    status: "running",
    phase_entered_at: enteredAt,
    updated_at: enteredAt,
  };
  writeJson(statePath, sliceState);
  writeJson(path.join(runDir, "manifest.json"), sliceState);
  fs.writeFileSync(path.join(runDir, "slice-plan.md"), "# Slice plan\n", "utf8");

  const firstAdvance = run(["run", "advance"], state.workspace_root);
  assert.equal(firstAdvance.status, 0, firstAdvance.stderr || firstAdvance.stdout);
  assert.match(firstAdvance.stdout, /reason: pause_after/);
  assert.match(firstAdvance.stdout, /wait for explicit user approval/i);
  assert.ok(firstAdvance.stdout.includes(`workspace_root: ${state.workspace_root}`));
  const firstPausePayload = JSON.parse(
    firstAdvance.stdout.split("\n")
      .find((line) => line.startsWith("status_json: "))
      .slice("status_json: ".length),
  );
  assert.equal(
    firstPausePayload.required_artifact,
    fs.realpathSync(path.join(runDir, "slice-plan.md")),
  );
  assert.equal(path.isAbsolute(firstPausePayload.required_artifact), true);
  const paused = JSON.parse(fs.readFileSync(statePath, "utf8"));
  assert.equal(paused.phase, "slice-plan");
  assert.equal(paused.status, "blocked");
  assert.equal(paused.pause_after_pending.phase, "slice-plan");
  assert.equal(typeof paused.pause_after_pending.requested_at, "string");

  const status = run(["status"], state.workspace_root);
  assert.equal(status.status, 0, status.stderr || status.stdout);
  assert.match(status.stdout, /reason: pause_after/);
  assert.match(status.stdout, /next_command: \.\/\.agent-flow\/bin\/agent-flow run advance --approve-pause/);
  assert.ok(status.stdout.includes(`workspace_root: ${state.workspace_root}`));
  const statusPayload = JSON.parse(
    status.stdout.split("\n").find((line) => line.startsWith("status_json: ")).slice("status_json: ".length),
  );
  const hookStatus = run(["status", "--format", "hook"], state.workspace_root);
  assert.equal(hookStatus.status, 0, hookStatus.stderr || hookStatus.stdout);
  assert.deepEqual(JSON.parse(hookStatus.stdout), statusPayload);

  const repeatedUnapproved = run(["run", "advance"], state.workspace_root);
  assert.equal(repeatedUnapproved.status, 0, repeatedUnapproved.stderr || repeatedUnapproved.stdout);
  assert.match(repeatedUnapproved.stdout, /reason: pause_after/);
  const stillPaused = JSON.parse(fs.readFileSync(statePath, "utf8"));
  assert.deepEqual(stillPaused.pause_after_pending, paused.pause_after_pending);
  assert.equal(Object.hasOwn(stillPaused, "pause_after_approval"), false);

  fs.writeFileSync(path.join(runDir, "slice-plan.md"), "# Slice plan v2\n", "utf8");
  const staleApproval = run(["run", "advance", "--approve-pause"], state.workspace_root);
  assert.equal(staleApproval.status, 0, staleApproval.stderr || staleApproval.stdout);
  assert.match(staleApproval.stdout, /reason: pause_after/);
  const repaused = JSON.parse(fs.readFileSync(statePath, "utf8"));
  assert.notEqual(
    repaused.pause_after_pending.artifact_sha256,
    paused.pause_after_pending.artifact_sha256,
  );
  assert.equal(Object.hasOwn(repaused, "pause_after_approval"), false);

  const approvedAdvance = run(["run", "advance", "--approve-pause"], state.workspace_root);
  assert.equal(approvedAdvance.status, 0, approvedAdvance.stderr || approvedAdvance.stdout);
  assert.match(approvedAdvance.stdout, /Current phase: worktree/);
  assert.ok(approvedAdvance.stdout.includes(`workspace_root: ${state.workspace_root}`));
  const advanced = JSON.parse(fs.readFileSync(statePath, "utf8"));
  assert.equal(advanced.phase, "worktree");
  assert.equal(advanced.status, "running");
  assert.equal(Object.hasOwn(advanced, "pause_after_pending"), false);
  assert.equal(advanced.pause_after_approval.phase, "slice-plan");
  assert.equal(
    advanced.pause_after_approval.artifact_sha256,
    repaused.pause_after_pending.artifact_sha256,
  );
  assert.equal(typeof advanced.pause_after_approval.approved_at, "string");

  const invalidApproval = run(["run", "advance", "--approve-pause"], state.workspace_root);
  assert.notEqual(invalidApproval.status, 0);
  assert.match(invalidApproval.stderr, /invalid for non-pause phase worktree/);
});

test("Node recovers an active manifest before accepting another prompt", (t) => {
  const root = setupProject(t);
  const first = run([
    "run", "start", "--task", "checkout recovery", "--workflow", "default", "--run-id", "active-fallback",
  ], root);
  assert.equal(first.status, 0, first.stderr || first.stdout);

  const statePath = path.join(root, ".agent-flow", "state", "current-run.json");
  const before = spawnSync("git", ["worktree", "list", "--porcelain"], {
    cwd: root,
    encoding: "utf8",
  }).stdout.match(/^worktree /gm)?.length ?? 0;

  fs.unlinkSync(statePath);
  const missingPointer = run(["run", "start", "--task", "another task", "--workflow", "default"], root);
  assert.notEqual(missingPointer.status, 0);
  assert.match(missingPointer.stderr, /active run active-fallback/);
  assert.equal(JSON.parse(fs.readFileSync(statePath, "utf8")).run_id, "active-fallback");

  fs.writeFileSync(statePath, "{broken", "utf8");
  const corruptPointer = run(["run", "start", "--task", "third task", "--workflow", "default"], root);
  assert.notEqual(corruptPointer.status, 0);
  assert.match(corruptPointer.stderr, /active run active-fallback/);
  assert.equal(JSON.parse(fs.readFileSync(statePath, "utf8")).run_id, "active-fallback");

  const after = spawnSync("git", ["worktree", "list", "--porcelain"], {
    cwd: root,
    encoding: "utf8",
  }).stdout.match(/^worktree /gm)?.length ?? 0;
  assert.equal(after, before);
  assert.equal(
    fs.readdirSync(path.join(root, ".agent-flow", "runs", "default"), { withFileTypes: true })
      .filter((entry) => entry.isDirectory()).length,
    1,
  );
});
