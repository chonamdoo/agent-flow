import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { detectActiveHost } from "../lib/host-detection.mjs";
import { hashSkillTree, resolveInstallSelection } from "../lib/skill-selection.mjs";

const KIT_ROOT = fileURLToPath(new URL("..", import.meta.url));
const CLI = path.join(KIT_ROOT, "bin", "agent-flow-kit.mjs");
const MANAGED_HOST_PATHS = Object.freeze([
  ".Codex/agents/code-reviewer.md",
  ".claude/agents/code-reviewer.md",
  ".omp/agents/code-reviewer.md",
  ".omp/extensions/agent-flow-hooks.ts",
]);
const MANAGED_HOOK_CONFIG_PATHS = Object.freeze([
  ".Codex/hooks.json",
  ".codex/hooks.json",
  ".claude/settings.json",
]);
const MANAGED_HOOK_SCRIPT_PATHS = Object.freeze([
  "guard-worktree.sh",
  "guard-worktree-write.py",
  "guard-protected-branch.sh",
  "show-phase-status.sh",
  "comment-checker.py",
].map((name) => `.agent-flow/scripts/hooks/${name}`));
const HOST_ENV_KEYS = [
  "AGENT_FLOW_HOST",
  "OMP_PROFILE",
  "PI_CODING_AGENT_DIR",
  "CLAUDECODE",
  "CLAUDE_CLI",
  "CLAUDE_CONFIG_DIR",
  "CODEX_CLI",
  "CODEX_HOME",
  "CODEX_SHELL",
  "CODEX_THREAD_ID",
  "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
];

function run(args, cwd, home, env = {}) {
  return spawnSync(process.execPath, [CLI, ...args], {
    cwd,
    encoding: "utf8",
    env: cleanChildEnv(home, env),
  });
}

function runWithRestrictiveUmask(args, cwd, home, env = {}) {
  return spawnSync(
    "/bin/sh",
    ["-c", 'umask 077; exec "$@"', "agent-flow-umask", process.execPath, CLI, ...args],
    {
      cwd,
      encoding: "utf8",
      env: cleanChildEnv(home, env),
    },
  );
}

function cleanChildEnv(home, env = {}) {
  const cleanEnv = { ...process.env };
  for (const key of HOST_ENV_KEYS) delete cleanEnv[key];
  return {
    ...cleanEnv,
    HOME: home,
    AGENT_FLOW_SKIP_CODEX_TRUST: "1",
    PYTHONPATH: [path.join(KIT_ROOT, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
    ...env,
  };
}

function spawnRun(args, cwd, home, env = {}) {
  const child = spawn(process.execPath, [CLI, ...args], {
    cwd,
    env: cleanChildEnv(home, env),
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = "";
  let stderr = "";
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const completion = new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("close", (status) => resolve({ status, stdout, stderr }));
  });
  return { child, completion };
}

async function waitForPath(candidate, timeoutMs = 20_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (fs.existsSync(candidate)) return;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw new Error(`timed out waiting for ${candidate}`);
}

function managedHookCommand(settingsPath, scriptName) {
  const settings = JSON.parse(fs.readFileSync(settingsPath, "utf8"));
  for (const entries of Object.values(settings.hooks ?? {})) {
    for (const entry of entries) {
      for (const hook of entry.hooks ?? []) {
        const command = String(hook.command);
        const encodedPath = command.match(/ '([A-Za-z0-9+/=]+)' '[0-9a-f]{64}'$/)?.[1];
        if (
          command.includes(scriptName)
          || (encodedPath && Buffer.from(encodedPath, "base64").toString("utf8").endsWith(`/${scriptName}`))
        ) return hook.command;
      }
    }
  }
  throw new Error(`managed hook command not found: ${scriptName}`);
}

function runWithStagedHookReplacement({
  executable,
  args,
  cwd,
  env,
  input = "",
  hookPath,
  replacement,
}) {
  return new Promise((resolve, reject) => {
    const child = spawn(executable, args, {
      cwd,
      env: {
        ...env,
        AGENT_FLOW_TEST_HOLD_AFTER_MANAGED_HOOK_STAGE_MS: "500",
      },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    let replaced = false;
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error(`timed out waiting for managed hook execution: ${stderr}`));
    }, 5_000);
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
      if (!replaced && stderr.includes("agent-flow:test-hook-staged:")) {
        replaced = true;
        const temporary = `${hookPath}.replacement-${process.pid}`;
        fs.writeFileSync(temporary, replacement, { mode: 0o755 });
        fs.chmodSync(temporary, 0o755);
        fs.renameSync(temporary, hookPath);
      }
    });
    child.once("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.once("close", (status) => {
      clearTimeout(timer);
      resolve({ status, stdout, stderr, replaced });
    });
    child.stdin.end(input);
  });
}

function sha256(content) {
  return createHash("sha256").update(content).digest("hex");
}

function modeSensitiveTreeHash(treeRoot) {
  const entries = [];
  const walk = (current, relative) => {
    const metadata = fs.lstatSync(current);
    entries.push({ path: relative, type: "directory", mode: metadata.mode & 0o777 });
    for (const name of fs.readdirSync(current).sort()) {
      const child = path.join(current, name);
      const childRelative = relative ? `${relative}/${name}` : name;
      const childMetadata = fs.lstatSync(child);
      if (childMetadata.isDirectory()) walk(child, childRelative);
      else entries.push({
        path: childRelative,
        type: "file",
        mode: childMetadata.mode & 0o777,
        sha256: sha256(fs.readFileSync(child)),
      });
    }
  };
  walk(treeRoot, "");
  entries.sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
  return sha256(JSON.stringify({ version: 1, entries }));
}

function parseTomlDocument(text) {
  const script = [
    "import json, sys",
    "try:",
    " import tomllib as toml",
    "except ImportError:",
    " import tomli as toml",
    "print(json.dumps(toml.loads(sys.stdin.read()), separators=(',', ':')))",
  ].join("\n");
  const result = spawnSync("python3", ["-c", script], {
    encoding: "utf8",
    input: text,
  });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  return JSON.parse(result.stdout);
}

function managedHostCommitment(kit) {
  const files = Object.entries(kit.managed_host_files.files)
    .sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)
    .map(([relative, entry]) => [relative, entry.source, entry.sha256]);
  return sha256(JSON.stringify({
    version: 1,
    skill_plan_hash: kit.skill_plan_hash,
    files,
  }));
}

function managedHookCommitment(kit) {
  const configs = Object.entries(kit.managed_hook_contract.configs)
    .sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)
    .map(([relative, entry]) => [relative, entry.sha256]);
  const scripts = Object.entries(kit.managed_hook_contract.scripts)
    .sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)
    .map(([relative, entry]) => [relative, entry.sha256, "executable"]);
  return sha256(JSON.stringify({
    version: 2,
    skill_plan_hash: kit.skill_plan_hash,
    configs,
    scripts,
  }));
}

function skillLinksCommitment(kit, links) {
  const rows = links.map((link) => [
    link.name,
    link.host,
    link.path,
    link.status,
    link.tree_integrity ?? null,
  ]).sort((left, right) => {
    const leftValue = JSON.stringify(left);
    const rightValue = JSON.stringify(right);
    return leftValue < rightValue ? -1 : leftValue > rightValue ? 1 : 0;
  });
  return sha256(JSON.stringify({
    version: 1,
    skill_plan_hash: kit.skill_plan_hash,
    links: rows,
  }));
}

function managedHostBytes(root) {
  return new Map(
    MANAGED_HOST_PATHS.map((relative) => [relative, fs.readFileSync(path.join(root, relative))]),
  );
}

test("fresh install records and enforces managed host file provenance", (t) => {
  const project = setupProject(t);
  const kitPath = path.join(project.root, ".agent-flow", "kit.json");
  const kit = JSON.parse(fs.readFileSync(kitPath, "utf8"));
  const index = JSON.parse(
    fs.readFileSync(path.join(project.root, ".agent-flow", "skills", "index.json"), "utf8"),
  );
  assert.equal(kit.managed_host_files.version, 1);
  assert.equal(kit.skill_links_commitment_version, 1);
  assert.equal(kit.skill_links_commitment, skillLinksCommitment(kit, index.links));
  assert.equal(kit.managed_host_files_commitment_version, 1);
  assert.equal(kit.managed_host_files_commitment, managedHostCommitment(kit));
  assert.equal(kit.managed_hook_contract.version, 2);
  assert.equal(kit.managed_hook_contract_commitment_version, 2);
  assert.match(kit.managed_hook_contract_commitment, /^[0-9a-f]{64}$/);
  assert.deepEqual(Object.keys(kit.managed_hook_contract.configs), MANAGED_HOOK_CONFIG_PATHS);
  assert.deepEqual(Object.keys(kit.managed_hook_contract.scripts), MANAGED_HOOK_SCRIPT_PATHS);
  for (const relative of MANAGED_HOOK_SCRIPT_PATHS) {
    assert.equal(kit.managed_hook_contract.scripts[relative].mode, "executable", relative);
  if (process.platform !== "win32") {
    assert.notEqual(fs.statSync(path.join(project.root, relative)).mode & 0o111, 0, relative);
  }
  }
  assert.deepEqual(Object.keys(kit.managed_host_files.files), MANAGED_HOST_PATHS);
  for (const relative of MANAGED_HOST_PATHS) {
    const content = fs.readFileSync(path.join(project.root, relative));
    assert.equal(kit.managed_host_files.files[relative].sha256, sha256(content), relative);
  }
  assert.deepEqual(
    fs.readFileSync(path.join(project.root, ".Codex", "agents", "code-reviewer.md")),
    fs.readFileSync(path.join(KIT_ROOT, ".Codex", "agents", "code-reviewer.md")),
  );
  for (const relative of [
    ".claude/agents/code-reviewer.md",
    ".omp/agents/code-reviewer.md",
  ]) {
    assert.deepEqual(
      fs.readFileSync(path.join(project.root, relative)),
      fs.readFileSync(path.join(KIT_ROOT, ".claude", "agents", "code-reviewer.md")),
      relative,
    );
  }

  for (const relative of MANAGED_HOST_PATHS) {
    const destination = path.join(project.root, relative);
    const original = fs.readFileSync(destination);
    fs.writeFileSync(destination, `tampered ${relative}\n`, "utf8");
    const start = run(
      ["run", "start", "--task", "provenance check", "--workflow", "default"],
      project.root,
      project.home,
    );
    assert.notEqual(start.status, 0, relative);
    assert.match(start.stderr, /installed managed host file changed/, relative);
    fs.writeFileSync(destination, original);
  }
});

test("fresh install never chmods or trusts unauthenticated conflicting hook bytes", (t) => {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-hook-conflict-"));
  const root = path.join(sandbox, "project");
  const home = path.join(sandbox, "home");
  const codexHome = path.join(sandbox, "codex-home");
  const fakeCodex = path.join(sandbox, "fake-codex");
  const hook = path.join(root, ".agent-flow", "scripts", "hooks", "guard-worktree-write.py");
  fs.mkdirSync(path.dirname(hook), { recursive: true });
  fs.mkdirSync(home);
  fs.mkdirSync(codexHome);
  fs.writeFileSync(hook, "user-owned conflicting hook\n", { encoding: "utf8", mode: 0o600 });
  fs.copyFileSync(path.join(KIT_ROOT, "tests", "fixtures", "fake_codex_app_server.py"), fakeCodex);
  fs.chmodSync(fakeCodex, 0o755);
  t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));
  const before = fs.readFileSync(hook);
  const beforeMode = fs.statSync(hook).mode & 0o777;

  const install = run(
    ["install", "--profile", "node", "--force-managed"],
    root,
    home,
    {
      AGENT_FLOW_SKIP_CODEX_TRUST: "0",
      CODEX_CLI_PATH: fakeCodex,
      CODEX_HOME: codexHome,
    },
  );

  assert.notEqual(install.status, 0);
  assert.match(install.stderr, /unauthenticated managed hook script differs/);
  assert.deepEqual(fs.readFileSync(hook), before);
  assert.equal(fs.statSync(hook).mode & 0o777, beforeMode);
  assert.equal(fs.existsSync(path.join(root, ".agent-flow", "kit.json")), false);
  assert.equal(fs.existsSync(path.join(codexHome, "config.toml")), false);
});

test("reinstall replaces only hook bytes authenticated by the previous contract", (t) => {
  const project = setupProject(t);
  const relative = MANAGED_HOOK_SCRIPT_PATHS[0];
  const hook = path.join(project.root, relative);
  const kitPath = path.join(project.root, ".agent-flow", "kit.json");
  const kit = JSON.parse(fs.readFileSync(kitPath, "utf8"));
  const priorBytes = Buffer.from("authenticated prior hook version\n", "utf8");
  fs.writeFileSync(hook, priorBytes);
  fs.chmodSync(hook, 0o755);
  kit.managed_hook_contract.scripts[relative].sha256 = sha256(priorBytes);
  kit.managed_hook_contract_commitment = managedHookCommitment(kit);
  fs.writeFileSync(kitPath, `${JSON.stringify(kit, null, 2)}\n`, "utf8");

  const reinstall = run(["install", "--profile", "node"], project.root, project.home);

  assert.equal(reinstall.status, 0, reinstall.stderr || reinstall.stdout);
  assert.deepEqual(
    fs.readFileSync(hook),
    fs.readFileSync(path.join(KIT_ROOT, "scripts", "hooks", path.basename(relative))),
  );
});

test("reinstall rejects same-byte hard-linked managed hook and host files", (t) => {
  const project = setupProject(t);
  for (const [index, relative] of [
    MANAGED_HOOK_SCRIPT_PATHS[0],
    MANAGED_HOST_PATHS[0],
  ].entries()) {
    const destination = path.join(project.root, relative);
    const alias = path.join(project.root, `.hardlink-alias-${index}`);
    fs.linkSync(destination, alias);
    const reinstall = run(["install", "--profile", "node"], project.root, project.home);
    assert.notEqual(reinstall.status, 0, relative);
    assert.match(reinstall.stderr, /may not be hard-linked/, relative);
    fs.rmSync(alias);
  }
});

test("managed hook settings, script bytes, and executable mode are committed and fail closed", (t) => {
  const project = setupProject(t);
  const config = path.join(project.root, MANAGED_HOOK_CONFIG_PATHS[0]);
  const originalConfig = fs.readFileSync(config);
  const settings = JSON.parse(originalConfig.toString("utf8"));
  const writeGuardCommand = managedHookCommand(config, "guard-worktree-write.py");
  const hookCount = settings.hooks.PreToolUse[0].hooks.length;
  settings.hooks.PreToolUse[0].hooks = settings.hooks.PreToolUse[0].hooks.filter(
    (hook) => String(hook.command) !== String(writeGuardCommand),
  );
  assert.equal(settings.hooks.PreToolUse[0].hooks.length, hookCount - 1);
  fs.writeFileSync(config, `${JSON.stringify(settings, null, 2)}\n`, "utf8");
  const configStart = run(
    ["run", "start", "--task", "hook config tamper", "--workflow", "default"],
    project.root,
    project.home,
  );
  assert.notEqual(configStart.status, 0);
  assert.match(configStart.stderr, /managed hook settings changed/);
  fs.writeFileSync(config, originalConfig);

  const script = path.join(project.root, MANAGED_HOOK_SCRIPT_PATHS[0]);
  const originalScript = fs.readFileSync(script);
  fs.writeFileSync(script, "tampered hook\n", "utf8");
  const scriptStart = run(
    ["run", "start", "--task", "hook script tamper", "--workflow", "default"],
    project.root,
    project.home,
  );
  assert.notEqual(scriptStart.status, 0);
  assert.match(scriptStart.stderr, /managed hook (?:settings|script) changed/);
  fs.writeFileSync(script, originalScript);

  if (process.platform !== "win32") {
    fs.chmodSync(script, 0o644);
    for (const host of ["claude", "codex", "omp"]) {
      const modeStart = run(
        ["run", "start", "--task", "hook script mode tamper", "--workflow", "default"],
        project.root,
        project.home,
        { AGENT_FLOW_HOST: host },
      );
      assert.notEqual(modeStart.status, 0, host);
      assert.match(modeStart.stderr, /managed hook script is not executable/, host);
    }
  }
});

test("managed hook projection rejects raw absolute script commands", (t) => {
  const project = setupProject(t);
  const scriptName = "guard-worktree-write.py";

  const configs = MANAGED_HOOK_CONFIG_PATHS.map((relative) => {
    const config = path.join(project.root, relative);
    const settings = JSON.parse(fs.readFileSync(config, "utf8"));
    const verifierCommand = managedHookCommand(config, scriptName);
    const encodedPath = verifierCommand.match(/ '([A-Za-z0-9+/=]+)' '[0-9a-f]{64}'$/)?.[1];
    assert.ok(encodedPath, `${relative}: ${verifierCommand}`);
    const rawCommand = Buffer.from(encodedPath, "base64").toString("utf8");
    assert.equal(path.isAbsolute(rawCommand), true, relative);
    assert.equal(path.basename(rawCommand), scriptName, relative);
    return { config, rawCommand, relative, settings, verifierCommand };
  });

  for (const { config, rawCommand, relative, settings, verifierCommand } of configs) {
    let replacements = 0;
    for (const entries of Object.values(settings.hooks ?? {})) {
      for (const entry of entries) {
        for (const hook of entry.hooks ?? []) {
          if (hook.command === verifierCommand) {
            hook.command = rawCommand;
            replacements += 1;
          }
        }
      }
    }
    assert.ok(replacements > 0, relative);
    fs.writeFileSync(config, `${JSON.stringify(settings, null, 2)}\n`, "utf8");
  }

  const started = run(
    ["run", "start", "--task", "raw managed hook command", "--workflow", "default"],
    project.root,
    project.home,
  );

  assert.notEqual(started.status, 0, started.stderr || started.stdout);
  assert.match(started.stderr, /managed hook command is not immutable/);
});

test("managed hook projection rejects additive raw, wrapped, and forged managed commands", (t) => {
  for (const commandKind of ["raw", "forged", "wrapped", "double-quoted", "variable-path"]) {
    const project = setupProject(t);
    const visitedConfigs = new Set();
    for (const relative of MANAGED_HOOK_CONFIG_PATHS) {
      const config = path.join(project.root, relative);
      const metadata = fs.statSync(config);
      const identity = `${metadata.dev}:${metadata.ino}`;
      if (visitedConfigs.has(identity)) continue;
      visitedConfigs.add(identity);
      const settings = JSON.parse(fs.readFileSync(config, "utf8"));
      const verifierCommand = managedHookCommand(config, "guard-worktree.sh");
      const suffix = verifierCommand.match(/ '([A-Za-z0-9+/=]+)' '([0-9a-f]{64})'$/);
      assert.ok(suffix?.index, relative);
      const rawPath = Buffer.from(suffix[1], "base64").toString("utf8");
      const command = commandKind === "raw"
        ? rawPath
        : commandKind === "forged"
          ? `'/usr/bin/python3' -I -c 'pass'${verifierCommand.slice(suffix.index)}`
          : commandKind === "wrapped"
            ? `/bin/bash "${rawPath}"`
            : commandKind === "double-quoted"
              ? `${verifierCommand.slice(0, suffix.index)} "${suffix[1]}" "${suffix[2]}"`
              : `P='${suffix[1]}'; ${verifierCommand.slice(0, suffix.index)} "$P" '${suffix[2]}'`;
      settings.hooks.PreToolUse[0].hooks.push({ type: "command", command });
      fs.writeFileSync(config, `${JSON.stringify(settings, null, 2)}\n`, "utf8");
    }

    const started = run(
      ["run", "start", "--task", `additive ${commandKind} hook`, "--workflow", "default"],
      project.root,
      project.home,
    );
    assert.notEqual(started.status, 0, started.stderr || started.stdout);
    assert.match(started.stderr, /managed hook command is not immutable/);
  }
});

test("managed hook verifier hashes are bound to committed script provenance", async (t) => {
  const project = setupProject(t);
  const scriptName = "guard-worktree.sh";
  const script = path.join(project.root, ".agent-flow", "scripts", "hooks", scriptName);
  const transientBytes = Buffer.from("#!/bin/sh\necho transient-uncommitted-hook\n", "utf8");
  const transientHash = sha256(transientBytes);
  const visitedConfigs = new Set();

  for (const relative of MANAGED_HOOK_CONFIG_PATHS) {
    const config = path.join(project.root, relative);
    const configMetadata = fs.statSync(config);
    const canonicalConfig = `${configMetadata.dev}:${configMetadata.ino}`;
    if (visitedConfigs.has(canonicalConfig)) continue;
    visitedConfigs.add(canonicalConfig);
    const settings = JSON.parse(fs.readFileSync(config, "utf8"));
    const currentCommand = managedHookCommand(config, scriptName);
    const transientCommand = currentCommand.replace(/'[0-9a-f]{64}'$/, `'${transientHash}'`);
    assert.notEqual(transientCommand, currentCommand, relative);
    let replacements = 0;
    for (const entries of Object.values(settings.hooks ?? {})) {
      for (const entry of entries) {
        for (const hook of entry.hooks ?? []) {
          if (hook.command === currentCommand) {
            hook.command = transientCommand;
            replacements += 1;
          }
        }
      }
    }
    assert.ok(replacements > 0, relative);
    fs.writeFileSync(config, `${JSON.stringify(settings, null, 2)}\n`, "utf8");
  }
  fs.writeFileSync(script, transientBytes);
  fs.chmodSync(script, 0o755);

  const marker = path.join(
    project.root,
    ".agent-flow",
    "managed-hook-config-validation-ready",
  );
  const started = spawnRun(
    ["run", "start", "--task", "bind hook hash", "--workflow", "default"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_HOLD_AFTER_MANAGED_HOOK_CONFIG_VALIDATION_MS: "1200" },
  );
  let result = null;
  const completion = started.completion.then((value) => {
    result = value;
    return value;
  });
  const deadline = Date.now() + 3_000;
  while (result === null && !fs.existsSync(marker) && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  if (fs.existsSync(marker)) {
    started.child.kill("SIGKILL");
    await completion;
    assert.fail("uncommitted verifier hash reached post-config validation");
  }
  if (result === null) result = await completion;
  assert.notEqual(result.status, 0, result.stderr || result.stdout);
  assert.match(result.stderr, /managed hook command is not immutable/);
});

test("Claude and Codex managed hooks execute immutable staged bytes after source replacement", async (t) => {
  const project = setupProject(t);
  const started = run(
    ["run", "start", "--task", "managed hook staging", "--workflow", "default"],
    project.root,
    project.home,
  );
  assert.equal(started.status, 0, started.stderr || started.stdout);
  const scriptName = "comment-checker.py";
  const claudeCommand = managedHookCommand(
    path.join(project.root, ".claude", "settings.json"),
    scriptName,
  );
  const codexCommand = managedHookCommand(
    path.join(project.root, ".Codex", "hooks.json"),
    scriptName,
  );
  assert.equal(codexCommand, claudeCommand);
  const hookPath = path.join(project.root, ".agent-flow", "scripts", "hooks", scriptName);
  const result = await runWithStagedHookReplacement({
    executable: "/bin/sh",
    args: ["-c", claudeCommand],
    cwd: project.root,
    env: cleanChildEnv(project.home),
    input: `${JSON.stringify({ tool_name: "Read", tool_input: {} })}\n`,
    hookPath,
    replacement: "#!/usr/bin/python3\nimport sys\nprint('replacement executed', file=sys.stderr)\nraise SystemExit(73)\n",
  });

  assert.equal(result.replaced, true, result.stderr);
  assert.equal(result.status, 0, result.stderr || result.stdout);
  assert.doesNotMatch(result.stderr, /replacement executed/);

  const stopScript = "show-phase-status.sh";
  const stopCommand = managedHookCommand(
    path.join(project.root, ".claude", "settings.json"),
    stopScript,
  );
  const nested = path.join(project.root, "nested", "cwd");
  fs.mkdirSync(nested, { recursive: true });
  const stopResult = await runWithStagedHookReplacement({
    executable: "/bin/sh",
    args: ["-c", stopCommand],
    cwd: nested,
    env: cleanChildEnv(project.home),
    hookPath: path.join(project.root, ".agent-flow", "scripts", "hooks", stopScript),
    replacement: "#!/bin/bash\necho 'replacement executed' >&2\nexit 73\n",
  });
  assert.equal(stopResult.replaced, true, stopResult.stderr);
  assert.equal(stopResult.status, 0, stopResult.stderr || stopResult.stdout);
  assert.equal(typeof JSON.parse(stopResult.stdout).systemMessage, "string");
  assert.doesNotMatch(stopResult.stderr, /replacement executed/);
});

test("OMP managed hooks execute immutable staged bytes after source replacement", async (t) => {
  const project = setupProject(t);
  const extension = path.join(project.root, ".omp", "extensions", "agent-flow-hooks.ts");
  const executableExtension = path.join(project.root, ".omp", "extensions", "agent-flow-hooks-test.mjs");
  fs.copyFileSync(extension, executableExtension);
  const driver = path.join(project.root, "run-omp-hook.mjs");
  fs.writeFileSync(driver, [
    "import hooks from './.omp/extensions/agent-flow-hooks-test.mjs';",
    "const handlers = new Map();",
    "hooks({ on: (name, handler) => handlers.set(name, handler), setLabel() {} });",
    "const result = await handlers.get('tool_call')(",
    "  { type: 'tool_call', toolName: 'Bash', input: { command: 'git status --short' } },",
    "  { cwd: process.cwd() },",
    ");",
    "if (result?.block) { console.error(result.reason); process.exit(2); }",
    "",
  ].join("\n"), "utf8");
  const scriptName = "guard-worktree.sh";
  const hookPath = path.join(project.root, ".agent-flow", "scripts", "hooks", scriptName);
  const result = await runWithStagedHookReplacement({
    executable: process.execPath,
    args: [driver],
    cwd: project.root,
    env: cleanChildEnv(project.home),
    hookPath,
    replacement: "#!/bin/bash\necho 'replacement executed' >&2\nexit 73\n",
  });

  assert.equal(result.replaced, true, result.stderr);
  assert.equal(result.status, 0, result.stderr || result.stdout);
  assert.doesNotMatch(result.stderr, /replacement executed/);
});

test("invalid hook settings backup never clobbers a user-owned .bak file", (t) => {
  const project = setupProject(t);
  const settingsPath = path.join(project.root, ".claude", "settings.json");
  const fixedBackup = `${settingsPath}.bak`;
  const invalid = "{ invalid user settings\n";
  fs.writeFileSync(settingsPath, invalid, "utf8");
  fs.writeFileSync(fixedBackup, "user-owned backup\n", "utf8");

  const reinstall = run(["install", "--profile", "node"], project.root, project.home);
  assert.equal(reinstall.status, 0, reinstall.stderr || reinstall.stdout);
  assert.equal(fs.readFileSync(fixedBackup, "utf8"), "user-owned backup\n");
  const generated = fs.readdirSync(path.dirname(settingsPath))
    .filter((name) => name.startsWith("settings.json.bak-"));
  assert.equal(generated.length, 1);
  assert.equal(fs.readFileSync(path.join(path.dirname(settingsPath), generated[0]), "utf8"), invalid);
});

test("Codex project skills use .agents and authenticated legacy .Codex links are removed", (t) => {
  const project = setupProject(t);
  const canonical = path.join(project.root, ".agents", "skills", "agent-flow");
  assert.equal(fs.existsSync(path.join(canonical, "SKILL.md")), true);
  const legacy = path.join(project.root, ".Codex", "skills", "agent-flow");
  fs.mkdirSync(path.dirname(legacy), { recursive: true });
  fs.symlinkSync(
    path.relative(path.dirname(legacy), path.join(project.root, ".agent-flow", "skills", "agent-flow")),
    legacy,
  );
  const indexPath = path.join(project.root, ".agent-flow", "skills", "index.json");
  const index = JSON.parse(fs.readFileSync(indexPath, "utf8"));
  index.links.push({
    name: "agent-flow",
    host: "codex",
    path: ".Codex/skills/agent-flow",
    status: "linked",
    tree_integrity: null,
  });
  fs.writeFileSync(indexPath, `${JSON.stringify(index, null, 2)}\n`, "utf8");
  const kitPath = path.join(project.root, ".agent-flow", "kit.json");
  const kit = JSON.parse(fs.readFileSync(kitPath, "utf8"));
  kit.skill_links_commitment = skillLinksCommitment(kit, index.links);
  fs.writeFileSync(kitPath, `${JSON.stringify(kit, null, 2)}\n`, "utf8");

  const reinstall = run(["install", "--profile", "node"], project.root, project.home);
  assert.equal(reinstall.status, 0, reinstall.stderr || reinstall.stdout);
  assert.equal(fs.existsSync(legacy), false);
  assert.equal(fs.existsSync(path.join(canonical, "SKILL.md")), true);
});

test("uncommitted skill link rows cannot delete a user-owned legacy symlink", (t) => {
  const project = setupProject(t);
  const legacy = path.join(project.root, ".Codex", "skills", "code-generation-discipline");
  const source = path.join(project.root, ".agent-flow", "skills", "code-generation-discipline");
  fs.mkdirSync(path.dirname(legacy), { recursive: true });
  const target = path.relative(path.dirname(legacy), source);
  fs.symlinkSync(target, legacy);
  const indexPath = path.join(project.root, ".agent-flow", "skills", "index.json");
  const index = JSON.parse(fs.readFileSync(indexPath, "utf8"));
  index.links.push({
    name: "code-generation-discipline",
    host: "codex",
    path: ".Codex/skills/code-generation-discipline",
    status: "linked",
    tree_integrity: null,
  });
  fs.writeFileSync(indexPath, `${JSON.stringify(index, null, 2)}\n`, "utf8");

  const reinstall = run(["install", "--profile", "node"], project.root, project.home);
  assert.notEqual(reinstall.status, 0);
  assert.match(reinstall.stderr, /skill links do not match kit commitment/);
  assert.equal(fs.lstatSync(legacy).isSymbolicLink(), true);
  assert.equal(fs.readlinkSync(legacy), target);
});

test("actual installer preserves CRLF project skill metadata", (t) => {
  const project = setupProject(t);
  const skillDir = path.join(project.root, ".agent-flow", "local-skills", "crlf-policy");
  fs.mkdirSync(skillDir, { recursive: true });
  fs.writeFileSync(path.join(skillDir, "SKILL.md"), [
    "---",
    "name: crlf-policy",
    "description: CRLF policy",
    "activation: always",
    "hosts: [codex]",
    "workflowPhases: [implement]",
    "dependencies: [code-generation-discipline]",
    "---",
    "",
    "# CRLF policy",
    "",
  ].join("\r\n"), "utf8");

  const reinstall = run(["install", "--profile", "node"], project.root, project.home);
  assert.equal(reinstall.status, 0, reinstall.stderr || reinstall.stdout);
  const index = JSON.parse(
    fs.readFileSync(path.join(project.root, ".agent-flow", "skills", "index.json"), "utf8"),
  );
  const selected = index.skills.find((skill) => skill.name === "crlf-policy");
  assert.equal(selected.description, "CRLF policy");
  assert.equal(selected.activation, "always");
  assert.deepEqual(selected.hosts, ["claude", "codex", "omp"]);
  assert.deepEqual(selected.workflowPhases, ["implement"]);
  assert.deepEqual(selected.dependencies, ["code-generation-discipline"]);
  assert.equal(fs.existsSync(path.join(project.root, ".agents", "skills", "crlf-policy", "SKILL.md")), true);
  assert.equal(fs.existsSync(path.join(project.root, ".claude", "skills", "crlf-policy", "SKILL.md")), true);
  assert.equal(fs.existsSync(path.join(project.root, ".omp", "skills", "crlf-policy", "SKILL.md")), true);
});

test("late install failure restores host links and critical provenance files", (t) => {
  const project = setupProject(t);
  const beforeBin = modeSensitiveTreeHash(path.join(project.root, ".agent-flow", "bin"));
  const criticalPaths = [
    ".agent-flow/skills/index.json",
    ".agent-flow/kit.json",
    ...MANAGED_HOST_PATHS,
    ...MANAGED_HOOK_CONFIG_PATHS,
    ...MANAGED_HOOK_SCRIPT_PATHS,
  ];
  const before = new Map(
    criticalPaths.map((relative) => [relative, fs.readFileSync(path.join(project.root, relative))]),
  );
  writeSkill(path.join(project.root, ".agent-flow", "local-skills"), "late-local-skill");

  const failed = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_FAIL_AFTER_MANAGED_HOST_APPLY: "1" },
  );
  assert.notEqual(failed.status, 0);
  assert.match(failed.stderr, /injected install failure after managed host apply/);
  for (const [relative, bytes] of before) {
    assert.deepEqual(fs.readFileSync(path.join(project.root, relative)), bytes, relative);
  }
  assert.equal(modeSensitiveTreeHash(path.join(project.root, ".agent-flow", "bin")), beforeBin);
  for (const hostRoot of [".agents", ".claude", ".omp"]) {
    assert.equal(
      fs.existsSync(path.join(project.root, hostRoot, "skills", "late-local-skill")),
      false,
      hostRoot,
    );
  }
});

test("fresh late install failure rolls back the pinned launcher directory", (t) => {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-fresh-launcher-rollback-"));
  const root = path.join(sandbox, "project");
  const home = path.join(sandbox, "home");
  fs.mkdirSync(root, { recursive: true });
  fs.mkdirSync(home, { recursive: true });
  t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));

  const failed = run(
    ["install", "--profile", "node"],
    root,
    home,
    { AGENT_FLOW_TEST_FAIL_AFTER_MANAGED_HOST_APPLY: "1" },
  );
  assert.notEqual(failed.status, 0);
  assert.match(failed.stderr, /injected install failure after managed host apply/);
  assert.equal(fs.existsSync(path.join(root, ".agent-flow", "bin")), false);
  assert.equal(fs.existsSync(path.join(root, ".agent-flow", "kit.json")), false);
  assert.equal(fs.existsSync(path.join(root, ".agent-flow", "install-transaction")), false);
});

test("late install failure restores authenticated retired managed host files", (t) => {
  const project = setupProject(t);
  const relative = ".claude/agents/retired-reviewer.md";
  const destination = path.join(project.root, relative);
  const retiredBytes = Buffer.from("retired managed reviewer\n", "utf8");
  fs.writeFileSync(destination, retiredBytes);
  const kitPath = path.join(project.root, ".agent-flow", "kit.json");
  const kit = JSON.parse(fs.readFileSync(kitPath, "utf8"));
  kit.managed_host_files.files[relative] = {
    source: "legacy:retired-reviewer",
    sha256: sha256(retiredBytes),
  };
  kit.managed_host_files_commitment = managedHostCommitment(kit);
  fs.writeFileSync(kitPath, `${JSON.stringify(kit, null, 2)}\n`, "utf8");
  const kitBefore = fs.readFileSync(kitPath);

  const failed = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_FAIL_AFTER_MANAGED_HOST_APPLY: "1" },
  );
  assert.notEqual(failed.status, 0);
  assert.match(failed.stderr, /injected install failure after managed host apply/);
  assert.deepEqual(fs.readFileSync(destination), retiredBytes);
  assert.deepEqual(fs.readFileSync(kitPath), kitBefore);
});

test("late install failure restores authenticated legacy cleanup targets", (t) => {
  const project = setupProject(t);
  const legacyRootScripts = path.join(project.root, "scripts");
  fs.cpSync(path.join(KIT_ROOT, "scripts"), legacyRootScripts, {
    recursive: true,
    preserveTimestamps: true,
  });
  const staleScripts = ["check-context-docs.mjs", "check-context-docs.ts"]
    .map((name) => path.join(project.root, ".agent-flow", "scripts", name));
  for (const candidate of staleScripts) fs.writeFileSync(candidate, "stale managed script\n", "utf8");
  const gitignorePath = path.join(project.root, ".gitignore");
  fs.appendFileSync(gitignorePath, "scripts/check-context-docs.*\n", "utf8");
  const beforeRootScripts = modeSensitiveTreeHash(legacyRootScripts);
  const beforeGitignore = fs.readFileSync(gitignorePath);

  const failed = run(
    ["install", "--profile", "node", "--force-managed"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_FAIL_AFTER_MANAGED_HOST_APPLY: "1" },
  );
  assert.notEqual(failed.status, 0);
  assert.match(failed.stderr, /injected install failure after managed host apply/);
  assert.equal(modeSensitiveTreeHash(legacyRootScripts), beforeRootScripts);
  for (const candidate of staleScripts) {
    assert.equal(fs.readFileSync(candidate, "utf8"), "stale managed script\n");
  }
  assert.deepEqual(fs.readFileSync(gitignorePath), beforeGitignore);
});

test("late install failure restores updated managed skill snapshots", (t) => {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-install-snapshot-"));
  const root = path.join(sandbox, "project");
  const home = path.join(sandbox, "home");
  fs.mkdirSync(root, { recursive: true });
  fs.mkdirSync(home, { recursive: true });
  t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));
  const sourceRoot = path.join(home, ".agents", "skills");
  writeSkill(sourceRoot, "atomic-snapshot", "OLD SNAPSHOT");
  const initial = run(["install", "--profile", "node", "--skill", "atomic-snapshot"], root, home);
  assert.equal(initial.status, 0, initial.stderr || initial.stdout);
  const installed = path.join(root, ".agent-flow", "skills", "atomic-snapshot", "SKILL.md");
  const oldSnapshot = fs.readFileSync(installed);
  const oldIndex = fs.readFileSync(path.join(root, ".agent-flow", "skills", "index.json"));
  const oldKit = fs.readFileSync(path.join(root, ".agent-flow", "kit.json"));

  writeSkill(sourceRoot, "atomic-snapshot", "NEW SNAPSHOT");
  const failed = run(
    ["install", "--profile", "node", "--skill", "atomic-snapshot"],
    root,
    home,
    { AGENT_FLOW_TEST_FAIL_AFTER_MANAGED_HOST_APPLY: "1" },
  );
  assert.notEqual(failed.status, 0);
  assert.match(failed.stderr, /injected install failure after managed host apply/);
  assert.deepEqual(fs.readFileSync(installed), oldSnapshot);
  assert.deepEqual(fs.readFileSync(path.join(root, ".agent-flow", "skills", "index.json")), oldIndex);
  assert.deepEqual(fs.readFileSync(path.join(root, ".agent-flow", "kit.json")), oldKit);
});

test("next install recovers a process crash before reading provenance", (t) => {
  const project = setupProject(t);
  const beforeBin = modeSensitiveTreeHash(path.join(project.root, ".agent-flow", "bin"));
  const criticalPaths = [
    ".agent-flow/skills/index.json",
    ".agent-flow/kit.json",
    ...MANAGED_HOST_PATHS,
    ...MANAGED_HOOK_CONFIG_PATHS,
    ...MANAGED_HOOK_SCRIPT_PATHS,
  ];
  const before = new Map(
    criticalPaths.map((relative) => [relative, fs.readFileSync(path.join(project.root, relative))]),
  );
  writeSkill(path.join(project.root, ".agent-flow", "local-skills"), "crash-local-skill");

  const crashed = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_CRASH_AFTER_MANAGED_HOST_APPLY: "1" },
  );
  assert.equal(crashed.status, 86, crashed.stderr || crashed.stdout);
  assert.equal(
    fs.existsSync(path.join(project.root, ".agent-flow", "install-transaction", "journal.json")),
    true,
  );
  const crashedJournal = JSON.parse(
    fs.readFileSync(
      path.join(project.root, ".agent-flow", "install-transaction", "journal.json"),
      "utf8",
    ),
  );
  assert.equal(
    crashedJournal.files.some((entry) => entry.path === ".agent-flow/bin" && entry.kind === "directory"),
    true,
  );

  const recovered = run(
    ["install", "--profile", "does-not-exist"],
    project.root,
    project.home,
  );
  assert.notEqual(recovered.status, 0);
  assert.match(recovered.stderr, /unknown profile/);
  assert.equal(fs.existsSync(path.join(project.root, ".agent-flow", "install-transaction")), false);
  for (const [relative, bytes] of before) {
    assert.deepEqual(fs.readFileSync(path.join(project.root, relative)), bytes, relative);
  }
  assert.equal(modeSensitiveTreeHash(path.join(project.root, ".agent-flow", "bin")), beforeBin);
  for (const hostRoot of [".agents", ".claude", ".omp"]) {
    assert.equal(
      fs.existsSync(path.join(project.root, hostRoot, "skills", "crash-local-skill")),
      false,
      hostRoot,
    );
  }
});

test("recovery preflights every backup before mutating live files", (t) => {
  const project = setupProject(t);
  writeSkill(path.join(project.root, ".agent-flow", "local-skills"), "missing-backup-skill");
  const crashed = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_CRASH_AFTER_MANAGED_HOST_APPLY: "1" },
  );
  assert.equal(crashed.status, 86, crashed.stderr || crashed.stdout);
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  const journal = JSON.parse(fs.readFileSync(path.join(transactionRoot, "journal.json"), "utf8"));
  const skillsEntry = journal.files.find((entry) => entry.path === ".agent-flow/skills");
  assert.equal(skillsEntry.existed, true);
  fs.rmSync(path.join(project.root, skillsEntry.backup), { recursive: true, force: true });
  const liveKit = fs.readFileSync(path.join(project.root, ".agent-flow", "kit.json"));

  const recovery = run(["install", "--profile", "does-not-exist"], project.root, project.home);
  assert.notEqual(recovery.status, 0);
  assert.match(recovery.stderr, /backup is missing/);
  assert.deepEqual(fs.readFileSync(path.join(project.root, ".agent-flow", "kit.json")), liveKit);
  assert.equal(fs.existsSync(transactionRoot), true);
});

test("critical backups preserve directory modes, reject special files, and clean their own lock", (t) => {
  const project = setupProject(t);
  const modeDirectory = path.join(project.root, ".agent-flow", "scripts", "mode-fixture");
  fs.mkdirSync(modeDirectory);
  fs.writeFileSync(path.join(modeDirectory, "fixture.txt"), "fixture\n", "utf8");
  fs.chmodSync(modeDirectory, 0o711);
  const crashed = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_CRASH_AFTER_MANAGED_HOST_APPLY: "1" },
  );
  assert.equal(crashed.status, 86, crashed.stderr || crashed.stdout);
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  const journal = JSON.parse(fs.readFileSync(path.join(transactionRoot, "journal.json"), "utf8"));
  const scripts = journal.files.find((entry) => entry.path === ".agent-flow/scripts");
  const backupModeDirectory = path.join(project.root, scripts.backup, "mode-fixture");
  assert.equal(fs.statSync(backupModeDirectory).mode & 0o777, 0o711);
  const recovered = run(["install", "--profile", "does-not-exist"], project.root, project.home);
  assert.notEqual(recovered.status, 0);
  assert.match(recovered.stderr, /unknown profile/);

  const fifo = path.join(project.root, ".agent-flow", "scripts", "special.fifo");
  const mkfifo = spawnSync("mkfifo", [fifo], { encoding: "utf8" });
  if (mkfifo.error?.code === "ENOENT") {
    t.skip("mkfifo is unavailable");
    return;
  }
  assert.equal(mkfifo.status, 0, mkfifo.stderr);
  const failed = run(["install", "--profile", "node"], project.root, project.home);
  assert.notEqual(failed.status, 0);
  assert.match(failed.stderr, /special file/);
  assert.equal(fs.existsSync(transactionRoot), false);
  assert.equal(fs.lstatSync(fifo).isFIFO(), true);
});

test("recovery verifies file bytes and whole-tree modes before any live mutation", (t) => {
  const project = setupProject(t);
  writeSkill(path.join(project.root, ".agent-flow", "local-skills"), "integrity-skill");
  const crashed = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_CRASH_AFTER_MANAGED_HOST_APPLY: "1" },
  );
  assert.equal(crashed.status, 86, crashed.stderr || crashed.stdout);
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  const journal = JSON.parse(fs.readFileSync(path.join(transactionRoot, "journal.json"), "utf8"));
  const kitEntry = journal.files.find((entry) => entry.path === ".agent-flow/kit.json");
  const kitBackup = path.join(project.root, kitEntry.backup);
  const originalKitBackup = fs.readFileSync(kitBackup);
  const liveIndex = fs.readFileSync(path.join(project.root, ".agent-flow", "skills", "index.json"));
  fs.appendFileSync(kitBackup, "tamper\n", "utf8");
  let recovery = run(["install", "--profile", "does-not-exist"], project.root, project.home);
  assert.notEqual(recovery.status, 0);
  assert.match(recovery.stderr, /backup changed/);
  assert.deepEqual(fs.readFileSync(path.join(project.root, ".agent-flow", "skills", "index.json")), liveIndex);
  fs.writeFileSync(kitBackup, originalKitBackup);
  fs.chmodSync(kitBackup, kitEntry.mode);

  const scriptsEntry = journal.files.find((entry) => entry.path === ".agent-flow/scripts");
  const scriptsBackup = path.join(project.root, scriptsEntry.backup);
  const modeTarget = path.join(scriptsBackup, "check-agent-flow-parity.mjs");
  const originalMode = fs.statSync(modeTarget).mode & 0o777;
  fs.chmodSync(modeTarget, originalMode ^ 0o100);
  recovery = run(["install", "--profile", "does-not-exist"], project.root, project.home);
  assert.notEqual(recovery.status, 0);
  assert.match(recovery.stderr, /backup changed/);
  assert.deepEqual(fs.readFileSync(path.join(project.root, ".agent-flow", "skills", "index.json")), liveIndex);
  assert.equal(fs.existsSync(transactionRoot), true);
});

test("recovery rejects a critical backup changed after preflight", async (t) => {
  const project = setupProject(t);
  writeSkill(path.join(project.root, ".agent-flow", "local-skills"), "critical-backup-race-skill");
  const crashed = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_CRASH_AFTER_MANAGED_HOST_APPLY: "1" },
  );
  assert.equal(crashed.status, 86, crashed.stderr || crashed.stdout);
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  const journal = JSON.parse(fs.readFileSync(path.join(transactionRoot, "journal.json"), "utf8"));
  const skillsEntry = journal.files.find((entry) => entry.path === ".agent-flow/skills");
  assert(skillsEntry?.backup);
  const backup = path.join(project.root, skillsEntry.backup, "index.json");
  const liveIndexPath = path.join(project.root, ".agent-flow", "skills", "index.json");
  const liveIndex = fs.readFileSync(liveIndexPath);
  const recovery = spawnRun(
    ["install", "--profile", "does-not-exist"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_HOLD_AFTER_RECOVERY_PREFLIGHT_MS: "1200" },
  );
  await waitForPath(path.join(transactionRoot, "recovery-preflight-ready"));
  fs.appendFileSync(backup, "post-preflight backup tamper\n", "utf8");
  const result = await recovery.completion;
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /backup changed while staging|backup changed/);
  assert.deepEqual(fs.readFileSync(liveIndexPath), liveIndex);
  assert.equal(fs.existsSync(transactionRoot), true);
});

test("directory host backup recovery is mode-sensitive after preflight", async (t) => {
  const skillName = "host-backup-mode-skill";
  const project = setupProject(t, { localSkills: [skillName] });
  const index = JSON.parse(
    fs.readFileSync(path.join(project.root, ".agent-flow", "skills", "index.json"), "utf8"),
  );
  const indexedSkill = index.skills.find((skill) => skill.name === skillName);
  assert(indexedSkill?.path);
  const source = path.dirname(path.resolve(project.root, indexedSkill.path));
  const destination = path.join(project.root, ".agents", "skills", skillName);
  fs.rmSync(destination);
  fs.cpSync(source, destination, { recursive: true, preserveTimestamps: true });
  assert.equal(modeSensitiveTreeHash(destination), modeSensitiveTreeHash(source));
  writeSkill(
    path.join(project.root, ".agent-flow", "local-skills"),
    skillName,
    "Updated host backup mode policy.",
  );
  const crashed = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_CRASH_AFTER_MANAGED_HOST_APPLY: "1" },
  );
  assert.equal(crashed.status, 86, crashed.stderr || crashed.stdout);
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  const journal = JSON.parse(fs.readFileSync(path.join(transactionRoot, "journal.json"), "utf8"));
  const hostEntry = journal.hosts.find((entry) => (
    entry.name === skillName
    && entry.host === "codex"
    && entry.expected_kind === "directory"
  ));
  assert(hostEntry?.backup);
  const backup = path.join(project.root, hostEntry.backup);
  const modeTarget = path.join(backup, "SKILL.md");
  const liveTarget = fs.readlinkSync(destination);
  const recovery = spawnRun(
    ["install", "--profile", "does-not-exist"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_HOLD_AFTER_RECOVERY_PREFLIGHT_MS: "1200" },
  );
  await waitForPath(path.join(transactionRoot, "recovery-preflight-ready"));
  const originalMode = fs.statSync(modeTarget).mode & 0o777;
  fs.chmodSync(modeTarget, originalMode ^ 0o100);
  const result = await recovery.completion;
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /backup changed while staging|host backup changed/);
  assert.equal(fs.readlinkSync(destination), liveTarget);
  assert.equal(fs.existsSync(transactionRoot), true);
});

test("install transaction lock is exclusive and an unpublished dead owner is recoverable", async (t) => {
  const project = setupProject(t);
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  const first = spawnRun(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_HOLD_INSTALL_TRANSACTION_LOCK_MS: "1200" },
  );
  await waitForPath(path.join(transactionRoot, "owner.json"));
  const concurrent = run(["install", "--profile", "node"], project.root, project.home);
  assert.notEqual(concurrent.status, 0);
  assert.match(concurrent.stderr, /transaction is active|unresolved install transaction|start lock exists/);
  const completed = await first.completion;
  assert.equal(completed.status, 0, completed.stderr || completed.stdout);

  fs.mkdirSync(transactionRoot, { mode: 0o700 });
  fs.mkdirSync(path.join(transactionRoot, "files"));
  fs.mkdirSync(path.join(transactionRoot, "hosts"));
  writeJson(path.join(transactionRoot, "owner.json"), {
    version: 1,
    pid: 2_147_483_647,
    token: "a".repeat(32),
    created_at: new Date().toISOString(),
  });
  const kitBefore = fs.readFileSync(path.join(project.root, ".agent-flow", "kit.json"));
  const recovered = run(["install", "--profile", "does-not-exist"], project.root, project.home);
  assert.notEqual(recovered.status, 0);
  assert.match(recovered.stderr, /unknown profile/);
  assert.equal(fs.existsSync(transactionRoot), false);
  assert.deepEqual(fs.readFileSync(path.join(project.root, ".agent-flow", "kit.json")), kitBefore);
});

test("a concurrent install cannot recover a live open journal", async (t) => {
  const project = setupProject(t);
  const kitPath = path.join(project.root, ".agent-flow", "kit.json");
  const kitBefore = fs.readFileSync(kitPath);
  const first = spawnRun(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_HOLD_AFTER_DIRECTORY_SKELETON_MS: "1200" },
  );
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  await waitForPath(path.join(transactionRoot, "open-journal-ready"));
  const second = run(["install", "--profile", "node"], project.root, project.home);
  assert.notEqual(second.status, 0);
  assert.match(second.stderr, /install transaction is active|start lock exists/);
  assert.equal(fs.existsSync(transactionRoot), true);
  const completed = await first.completion;
  assert.equal(completed.status, 0, completed.stderr || completed.stdout);
  assert.notDeepEqual(fs.readFileSync(kitPath), Buffer.alloc(0));
  assert.equal(JSON.parse(fs.readFileSync(kitPath, "utf8")).profile, "generic");
  assert.equal(kitBefore.length > 0, true);
});

test("a dead node-install start lock is recovered without touching other lock types", async (t) => {
  const project = setupProject(t);
  const install = spawnRun(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_HOLD_INSTALL_TRANSACTION_LOCK_MS: "5000" },
  );
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  await waitForPath(path.join(transactionRoot, "owner.json"));
  install.child.kill("SIGKILL");
  await install.completion;
  const startLock = path.join(project.root, ".git", "agent-flow", "start.lock");
  assert.equal(fs.existsSync(startLock), true);

  const recovered = run(["install", "--profile", "does-not-exist"], project.root, project.home);
  assert.notEqual(recovered.status, 0);
  assert.match(recovered.stderr, /unknown profile/);
  assert.equal(fs.existsSync(startLock), false);
  assert.equal(fs.existsSync(transactionRoot), false);

  writeJson(startLock, {
    version: 1,
    token: "123e4567-e89b-42d3-a456-426614174000",
    pid: 2_147_483_647,
    runtime: "node",
    created_at: new Date().toISOString(),
    project_root: fs.realpathSync.native(project.root),
  });
  const preserved = run(["install", "--profile", "node"], project.root, project.home);
  assert.notEqual(preserved.status, 0);
  assert.match(preserved.stderr, /start lock exists/);
  assert.equal(fs.existsSync(startLock), true);
});

test("fresh early SIGKILL and a mid-install skill checkpoint recover deterministically", async (t) => {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-early-crash-"));
  const root = path.join(sandbox, "project");
  const home = path.join(sandbox, "home");
  fs.mkdirSync(root, { recursive: true });
  fs.mkdirSync(home, { recursive: true });
  t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));
  const child = spawnRun(
    ["install", "--profile", "node"],
    root,
    home,
    { AGENT_FLOW_TEST_HOLD_AFTER_DIRECTORY_SKELETON_MS: "1200" },
  );
  await waitForPath(path.join(root, ".agent-flow", "skills"));
  child.child.kill("SIGKILL");
  const killed = await child.completion;
  assert.equal(killed.status, null);
  let recovery = run(["install", "--profile", "does-not-exist"], root, home);
  assert.notEqual(recovery.status, 0);
  assert.match(recovery.stderr, /unknown profile/);
  assert.equal(fs.existsSync(path.join(root, ".agent-flow", "install-transaction")), false);

  const initial = run(["install", "--profile", "node"], root, home);
  assert.equal(initial.status, 0, initial.stderr || initial.stdout);
  writeSkill(path.join(root, ".agent-flow", "local-skills"), "mid-checkpoint-skill");
  const midCrash = run(
    ["install", "--profile", "node"],
    root,
    home,
    { AGENT_FLOW_TEST_CRASH_AFTER_SKILL_INDEX: "1" },
  );
  assert.equal(midCrash.status, 84, midCrash.stderr || midCrash.stdout);
  recovery = run(["install", "--profile", "does-not-exist"], root, home);
  assert.notEqual(recovery.status, 0);
  assert.match(recovery.stderr, /unknown profile/);
  assert.equal(fs.existsSync(path.join(root, ".agents", "skills", "mid-checkpoint-skill")), false);
});

test("rename-before-checkpoint and hook mode crashes recover from write-ahead pending state", (t) => {
  const project = setupProject(t);
  writeSkill(path.join(project.home, ".agents", "skills"), "rename-pending-skill");
  let crashed = run(
    ["install", "--profile", "node", "--skill", "rename-pending-skill"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_CRASH_AFTER_SKILL_SNAPSHOT_RENAME: "1" },
  );
  assert.equal(crashed.status, 82, crashed.stderr || crashed.stdout);
  let recovery = run(["install", "--profile", "does-not-exist"], project.root, project.home);
  assert.notEqual(recovery.status, 0);
  assert.match(recovery.stderr, /unknown profile/);
  assert.equal(fs.existsSync(path.join(project.root, ".agent-flow", "skills", "rename-pending-skill")), false);

  for (const relative of MANAGED_HOOK_SCRIPT_PATHS) {
    fs.chmodSync(path.join(project.root, relative), 0o644);
  }
  crashed = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_CRASH_AFTER_HOOK_CHMOD: "1" },
  );
  assert.equal(crashed.status, 81, crashed.stderr || crashed.stdout);
  recovery = run(["install", "--profile", "does-not-exist"], project.root, project.home);
  assert.notEqual(recovery.status, 0);
  assert.match(recovery.stderr, /unknown profile/);
  for (const relative of MANAGED_HOOK_SCRIPT_PATHS) {
    assert.equal(fs.statSync(path.join(project.root, relative)).mode & 0o111, 0, relative);
  }
});

test("run start and status fail closed while a reinstall transaction owns the project", async (t) => {
  const project = setupProject(t);
  const reinstall = spawnRun(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_HOLD_INSTALL_TRANSACTION_LOCK_MS: "1200" },
  );
  await waitForPath(path.join(project.root, ".agent-flow", "install-transaction", "owner.json"));
  for (const args of [
    ["run", "start", "--task", "must not race", "--workflow", "default"],
    ["status"],
  ]) {
    const blocked = run(args, project.root, project.home);
    assert.notEqual(blocked.status, 0, args.join(" "));
    assert.match(blocked.stderr, /install transaction is in progress/, args.join(" "));
  }
  const installed = await reinstall.completion;
  assert.equal(installed.status, 0, installed.stderr || installed.stdout);
  assert.equal(fs.existsSync(path.join(project.root, ".agent-flow", "state", "current-run.json")), false);
});

test("critical path ancestor symlinks are rejected before backup or writes", (t) => {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-critical-symlink-"));
  const root = path.join(sandbox, "project");
  const home = path.join(sandbox, "home");
  const outside = path.join(sandbox, "outside");
  fs.mkdirSync(path.join(root, ".Codex"), { recursive: true });
  fs.mkdirSync(home, { recursive: true });
  fs.mkdirSync(outside, { recursive: true });
  fs.writeFileSync(path.join(outside, "sentinel"), "unchanged\n", "utf8");
  fs.symlinkSync(outside, path.join(root, ".Codex", "rules"));
  t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));

  const install = run(["install", "--profile", "node"], root, home);
  assert.notEqual(install.status, 0);
  assert.match(install.stderr, /critical install path parent is unsafe/);
  assert.equal(fs.readFileSync(path.join(outside, "sentinel"), "utf8"), "unchanged\n");
  assert.equal(fs.existsSync(path.join(root, ".agent-flow", "install-transaction")), false);
});

test("recovery rejects symlinked backup ancestors and forged host journal combinations", (t) => {
  const project = setupProject(t);
  writeSkill(path.join(project.root, ".agent-flow", "local-skills"), "forged-journal-skill");
  const crashed = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_CRASH_AFTER_MANAGED_HOST_APPLY: "1" },
  );
  assert.equal(crashed.status, 86, crashed.stderr || crashed.stdout);
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  const journalPath = path.join(transactionRoot, "journal.json");
  const originalJournal = JSON.parse(fs.readFileSync(journalPath, "utf8"));
  const liveKit = fs.readFileSync(path.join(project.root, ".agent-flow", "kit.json"));
  const entryIndex = originalJournal.hosts.findIndex((entry) => entry.name === "forged-journal-skill");
  assert.notEqual(entryIndex, -1);
  const mutations = [
    (entry) => { entry.name = "../escape"; },
    (entry) => { entry.host = "unknown"; },
    (entry) => { entry.source = null; },
    (entry) => { entry.backup = ".agent-flow/install-transaction/files/0.bin"; },
  ];
  for (const mutate of mutations) {
    const forged = structuredClone(originalJournal);
    mutate(forged.hosts[entryIndex]);
    writeJson(journalPath, forged);
    const recovery = run(["install", "--profile", "does-not-exist"], project.root, project.home);
    assert.notEqual(recovery.status, 0);
    assert.match(recovery.stderr, /host journal|host action|invalid install transaction|noncanonical/);
    assert.deepEqual(fs.readFileSync(path.join(project.root, ".agent-flow", "kit.json")), liveKit);
  }
  writeJson(journalPath, originalJournal);

  const outside = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-backup-outside-"));
  t.after(() => fs.rmSync(outside, { recursive: true, force: true }));
  const sentinel = path.join(outside, "sentinel");
  fs.writeFileSync(sentinel, "unchanged\n", "utf8");
  fs.rmSync(path.join(transactionRoot, "files"), { recursive: true, force: true });
  fs.symlinkSync(outside, path.join(transactionRoot, "files"));
  const unsafeRecovery = run(["install", "--profile", "does-not-exist"], project.root, project.home);
  assert.notEqual(unsafeRecovery.status, 0);
  assert.match(unsafeRecovery.stderr, /parent is unsafe/);
  assert.equal(fs.readFileSync(sentinel, "utf8"), "unchanged\n");
  assert.deepEqual(fs.readFileSync(path.join(project.root, ".agent-flow", "kit.json")), liveKit);
});

test("post-crash user edits are preserved and block automatic rollback", (t) => {
  const project = setupProject(t);
  writeSkill(path.join(project.root, ".agent-flow", "local-skills"), "post-crash-edit-skill");
  const crashed = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_CRASH_AFTER_MANAGED_HOST_APPLY: "1" },
  );
  assert.equal(crashed.status, 86, crashed.stderr || crashed.stdout);
  const indexPath = path.join(project.root, ".agent-flow", "skills", "index.json");
  fs.appendFileSync(indexPath, "post-crash user edit\n", "utf8");
  const edited = fs.readFileSync(indexPath);
  const recovery = run(["install", "--profile", "does-not-exist"], project.root, project.home);
  assert.notEqual(recovery.status, 0);
  assert.match(recovery.stderr, /destination changed after crash/);
  assert.deepEqual(fs.readFileSync(indexPath), edited);
  assert.equal(fs.existsSync(path.join(project.root, ".agent-flow", "install-transaction")), true);
});

test("post-crash user deletion is not mistaken for a recovery move", (t) => {
  const project = setupProject(t);
  const crashed = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_CRASH_AFTER_MANAGED_HOST_APPLY: "1" },
  );
  assert.equal(crashed.status, 86, crashed.stderr || crashed.stdout);
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  const kitPath = path.join(project.root, ".agent-flow", "kit.json");
  fs.rmSync(kitPath);

  const recovery = run(["install", "--profile", "does-not-exist"], project.root, project.home);
  assert.notEqual(recovery.status, 0);
  assert.match(recovery.stderr, /destination changed after crash/);
  assert.equal(fs.existsSync(kitPath), false);
  assert.equal(fs.existsSync(transactionRoot), true);
});

test("committed cleanup failure never rolls back and is cleaned on the next install", (t) => {
  const project = setupProject(t);
  writeSkill(path.join(project.root, ".agent-flow", "local-skills"), "committed-skill");
  const committed = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_FAIL_AFTER_INSTALL_COMMIT: "1" },
  );
  assert.notEqual(committed.status, 0);
  assert.match(committed.stderr, /cleanup failure after commit/);
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  const journal = JSON.parse(fs.readFileSync(path.join(transactionRoot, "journal.json"), "utf8"));
  assert.equal(journal.status, "committed");
  const indexPath = path.join(project.root, ".agent-flow", "skills", "index.json");
  const committedIndex = fs.readFileSync(indexPath);
  assert.equal(fs.existsSync(path.join(project.root, ".agents", "skills", "committed-skill")), true);

  const next = run(["install", "--profile", "does-not-exist"], project.root, project.home);
  assert.notEqual(next.status, 0);
  assert.match(next.stderr, /unknown profile/);
  assert.equal(fs.existsSync(transactionRoot), false);
  assert.deepEqual(fs.readFileSync(indexPath), committedIndex);
  assert.equal(fs.existsSync(path.join(project.root, ".agents", "skills", "committed-skill")), true);
});

test("forged committed status cannot discard rollback evidence or unblock run status", (t) => {
  const project = setupProject(t);
  writeSkill(path.join(project.root, ".agent-flow", "local-skills"), "forged-commit-skill");
  const crashed = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_CRASH_AFTER_MANAGED_HOST_APPLY: "1" },
  );
  assert.equal(crashed.status, 86, crashed.stderr || crashed.stdout);
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  const journalPath = path.join(transactionRoot, "journal.json");
  const journal = JSON.parse(fs.readFileSync(journalPath, "utf8"));
  journal.status = "committed";
  writeJson(journalPath, journal);
  const indexPath = path.join(project.root, ".agent-flow", "skills", "index.json");
  const liveIndex = fs.readFileSync(indexPath);

  const status = run(["status"], project.root, project.home);
  assert.notEqual(status.status, 0);
  assert.match(status.stderr, /install transaction is in progress/);
  assert.equal(fs.existsSync(transactionRoot), true);

  let recovery = run(["install", "--profile", "does-not-exist"], project.root, project.home);
  assert.notEqual(recovery.status, 0);
  assert.match(recovery.stderr, /committed install transaction proof is invalid/);
  assert.deepEqual(fs.readFileSync(indexPath), liveIndex);
  assert.equal(fs.existsSync(transactionRoot), true);

  writeJson(journalPath, { ...journal, files: [], hosts: [] });
  recovery = run(["install", "--profile", "does-not-exist"], project.root, project.home);
  assert.notEqual(recovery.status, 0);
  assert.match(recovery.stderr, /transaction proof is invalid|file journal is incomplete|journal is invalid/);
  assert.deepEqual(fs.readFileSync(indexPath), liveIndex);
  assert.equal(fs.existsSync(transactionRoot), true);
});

test("status-only commit forgery cannot accept a mixed bundle snapshot", (t) => {
  const project = setupProject(t);
  const workflow = path.join(project.root, ".agent-flow", "workflows", "default.yaml");
  const script = path.join(project.root, ".agent-flow", "scripts", "validate-skills.mjs");
  const desiredWorkflow = fs.readFileSync(path.join(KIT_ROOT, "workflows", "default.yaml"));
  fs.writeFileSync(workflow, "old managed workflow\n", "utf8");
  const oldScript = Buffer.from("old managed non-hook script\n", "utf8");
  fs.writeFileSync(script, oldScript);

  const crashed = run(
    ["install", "--profile", "node", "--force-managed"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_CRASH_AFTER_SKILL_INDEX: "1" },
  );
  assert.equal(crashed.status, 84, crashed.stderr || crashed.stdout);
  assert.deepEqual(fs.readFileSync(workflow), desiredWorkflow);
  assert.deepEqual(fs.readFileSync(script), oldScript);
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  const journalPath = path.join(transactionRoot, "journal.json");
  const journal = JSON.parse(fs.readFileSync(journalPath, "utf8"));
  assert.equal(journal.commit_proof, null);
  journal.status = "committed";
  writeJson(journalPath, journal);

  const recovery = run(["install", "--profile", "does-not-exist"], project.root, project.home);
  assert.notEqual(recovery.status, 0);
  assert.match(recovery.stderr, /committed install transaction proof is invalid/);
  assert.deepEqual(fs.readFileSync(workflow), desiredWorkflow);
  assert.deepEqual(fs.readFileSync(script), oldScript);
  assert.equal(fs.existsSync(transactionRoot), true);
});

test("committed cleanup verifies hook semantics beyond forged journal state", (t) => {
  const project = setupProject(t);
  writeSkill(path.join(project.root, ".agent-flow", "local-skills"), "committed-hook-invariant-skill");
  const committed = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_FAIL_AFTER_INSTALL_COMMIT: "1" },
  );
  assert.notEqual(committed.status, 0);
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  const journalPath = path.join(transactionRoot, "journal.json");
  const journal = JSON.parse(fs.readFileSync(journalPath, "utf8"));
  const hook = path.join(project.root, MANAGED_HOOK_SCRIPT_PATHS[0]);
  fs.appendFileSync(hook, "post-commit hook tamper\n", "utf8");
  const scriptsEntry = journal.files.find((entry) => entry.path === ".agent-flow/scripts");
  assert.equal(scriptsEntry?.applied_state?.kind, "directory");
  scriptsEntry.applied_state.tree_hash = modeSensitiveTreeHash(
    path.join(project.root, ".agent-flow", "scripts"),
  );
  writeJson(journalPath, journal);

  const recovery = run(["install", "--profile", "does-not-exist"], project.root, project.home);
  assert.notEqual(recovery.status, 0);
  assert.match(recovery.stderr, /transaction proof is invalid|installed managed hook script changed/);
  assert.equal(fs.existsSync(transactionRoot), true);
});

test("canonical committed installs tolerate a removed backup but reject a missing owner", (t) => {
  for (const scenario of ["backup", "owner"]) {
    const project = setupProject(t);
    writeSkill(
      path.join(project.root, ".agent-flow", "local-skills"),
      `partial-commit-cleanup-${scenario}`,
    );
    const committed = run(
      ["install", "--profile", "node"],
      project.root,
      project.home,
      { AGENT_FLOW_TEST_FAIL_AFTER_INSTALL_COMMIT: "1" },
    );
    assert.notEqual(committed.status, 0, scenario);
    assert.match(committed.stderr, /cleanup failure after commit/, scenario);
    const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
    const journal = JSON.parse(fs.readFileSync(path.join(transactionRoot, "journal.json"), "utf8"));
    assert.equal(journal.status, "committed", scenario);
    if (scenario === "backup") {
      const backupEntry = journal.files.find((entry) => entry.existed && entry.backup);
      assert(backupEntry?.backup);
      fs.rmSync(path.join(project.root, backupEntry.backup), { recursive: true, force: true });
    } else {
      fs.rmSync(path.join(transactionRoot, "owner.json"));
    }

    const recovery = run(["install", "--profile", "does-not-exist"], project.root, project.home);
    assert.notEqual(recovery.status, 0, scenario);
    if (scenario === "owner") {
      assert.match(recovery.stderr, /install transaction owner is missing/, scenario);
      assert.equal(fs.existsSync(transactionRoot), true, scenario);
    } else {
      assert.match(recovery.stderr, /unknown profile/, scenario);
      assert.equal(fs.existsSync(transactionRoot), false, scenario);
    }
    assert.equal(
      fs.existsSync(path.join(project.root, ".agents", "skills", `partial-commit-cleanup-${scenario}`)),
      true,
      scenario,
    );
  }
});

test("canonical committed cleanup tolerates missing transaction backup directories", (t) => {
  const filesProject = setupProject(t);
  writeSkill(
    path.join(filesProject.root, ".agent-flow", "local-skills"),
    "partial-files-directory-skill",
  );
  let committed = run(
    ["install", "--profile", "node"],
    filesProject.root,
    filesProject.home,
    { AGENT_FLOW_TEST_FAIL_AFTER_INSTALL_COMMIT: "1" },
  );
  assert.notEqual(committed.status, 0);
  let transactionRoot = path.join(filesProject.root, ".agent-flow", "install-transaction");
  fs.rmSync(path.join(transactionRoot, "files"), { recursive: true, force: true });
  let recovery = run(
    ["install", "--profile", "does-not-exist"],
    filesProject.root,
    filesProject.home,
  );
  assert.notEqual(recovery.status, 0);
  assert.match(recovery.stderr, /unknown profile/);
  assert.equal(fs.existsSync(transactionRoot), false);

  const skillName = "partial-hosts-directory-skill";
  const hostsProject = setupProject(t, { localSkills: [skillName] });
  const index = JSON.parse(
    fs.readFileSync(path.join(hostsProject.root, ".agent-flow", "skills", "index.json"), "utf8"),
  );
  const indexedSkill = index.skills.find((skill) => skill.name === skillName);
  assert(indexedSkill?.path);
  const source = path.dirname(path.resolve(hostsProject.root, indexedSkill.path));
  const destination = path.join(hostsProject.root, ".agents", "skills", skillName);
  fs.rmSync(destination);
  fs.cpSync(source, destination, { recursive: true, preserveTimestamps: true });
  writeSkill(
    path.join(hostsProject.root, ".agent-flow", "local-skills"),
    skillName,
    "Updated partial host cleanup policy.",
  );
  committed = run(
    ["install", "--profile", "node"],
    hostsProject.root,
    hostsProject.home,
    { AGENT_FLOW_TEST_FAIL_AFTER_INSTALL_COMMIT: "1" },
  );
  assert.notEqual(committed.status, 0);
  transactionRoot = path.join(hostsProject.root, ".agent-flow", "install-transaction");
  const journal = JSON.parse(fs.readFileSync(path.join(transactionRoot, "journal.json"), "utf8"));
  assert.equal(journal.hosts.some((entry) => entry.backup), true);
  fs.rmSync(path.join(transactionRoot, "hosts"), { recursive: true, force: true });
  recovery = run(
    ["install", "--profile", "does-not-exist"],
    hostsProject.root,
    hostsProject.home,
  );
  assert.notEqual(recovery.status, 0);
  assert.match(recovery.stderr, /unknown profile/);
  assert.equal(fs.existsSync(transactionRoot), false);
  assert.equal(fs.existsSync(destination), true);
});

test("committed transaction cleanup cannot race a replacement install transaction", async (t) => {
  const project = setupProject(t);
  writeSkill(path.join(project.root, ".agent-flow", "local-skills"), "commit-cleanup-race-skill");
  const first = spawnRun(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_HOLD_AFTER_INSTALL_COMMIT_MS: "1200" },
  );
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  await waitForPath(path.join(transactionRoot, "commit-cleanup-ready"));
  const ownerBefore = fs.readFileSync(path.join(transactionRoot, "owner.json"));

  const competing = run(["install", "--profile", "node"], project.root, project.home);
  assert.notEqual(competing.status, 0);
  assert.match(competing.stderr, /start lock exists/);
  assert.deepEqual(fs.readFileSync(path.join(transactionRoot, "owner.json")), ownerBefore);

  const completed = await first.completion;
  assert.equal(completed.status, 0, completed.stderr || completed.stdout);
  assert.equal(fs.existsSync(transactionRoot), false);
  const retry = run(["install", "--profile", "node"], project.root, project.home);
  assert.equal(retry.status, 0, retry.stderr || retry.stdout);
});

test("recovery keeps authenticated backups until every restore is complete and is re-entrant", (t) => {
  const project = setupProject(t);
  writeSkill(path.join(project.root, ".agent-flow", "local-skills"), "reentrant-recovery-skill");
  const crashed = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_CRASH_AFTER_MANAGED_HOST_APPLY: "1" },
  );
  assert.equal(crashed.status, 86, crashed.stderr || crashed.stdout);
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  const journal = JSON.parse(fs.readFileSync(path.join(transactionRoot, "journal.json"), "utf8"));
  const backupPaths = [
    ...journal.files.filter((entry) => entry.backup).map((entry) => path.join(project.root, entry.backup)),
    ...journal.hosts.filter((entry) => entry.backup).map((entry) => path.join(project.root, entry.backup)),
  ];
  const interrupted = run(
    ["install", "--profile", "does-not-exist"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_CRASH_DURING_RECOVERY_AFTER_DESTINATION_MOVE: "1" },
  );
  assert.equal(interrupted.status, 80, interrupted.stderr || interrupted.stdout);
  assert.equal(backupPaths.every((candidate) => fs.existsSync(candidate)), true);
  assert.equal(fs.existsSync(transactionRoot), true);

  const recovered = run(["install", "--profile", "does-not-exist"], project.root, project.home);
  assert.notEqual(recovered.status, 0);
  assert.match(recovered.stderr, /unknown profile/);
  assert.equal(fs.existsSync(transactionRoot), false);
  assert.equal(fs.existsSync(path.join(project.root, ".agents", "skills", "reentrant-recovery-skill")), false);
});

test("open rollback detaches transaction atomically before recursive cleanup", (t) => {
  const project = setupProject(t);
  writeSkill(path.join(project.root, ".agent-flow", "local-skills"), "rollback-detach-skill");
  const crashed = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_CRASH_AFTER_MANAGED_HOST_APPLY: "1" },
  );
  assert.equal(crashed.status, 86, crashed.stderr || crashed.stdout);
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  const interruptedCleanup = run(
    ["install", "--profile", "does-not-exist"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_CRASH_AFTER_RECOVERY_TRANSACTION_DETACH: "1" },
  );
  assert.equal(interruptedCleanup.status, 79, interruptedCleanup.stderr || interruptedCleanup.stdout);
  assert.equal(fs.existsSync(transactionRoot), false);
  assert.equal(
    fs.readdirSync(path.join(project.root, ".agent-flow"))
      .some((name) => name.startsWith(".install-transaction-cleanup-")),
    true,
  );

  const retry = run(["install", "--profile", "does-not-exist"], project.root, project.home);
  assert.notEqual(retry.status, 0);
  assert.match(retry.stderr, /unknown profile/);
  assert.equal(fs.existsSync(transactionRoot), false);
  assert.equal(fs.existsSync(path.join(project.root, ".agents", "skills", "rollback-detach-skill")), false);
});

test("recovery intent is durable across case-insensitive critical path aliases", (t) => {
  const project = setupProject(t);
  const upper = path.join(project.root, ".Codex", "hooks.json");
  const lower = path.join(project.root, ".codex", "hooks.json");
  if (fs.realpathSync.native(upper) !== fs.realpathSync.native(lower)) {
    t.skip("filesystem is case-sensitive");
    return;
  }
  const hooks = JSON.parse(fs.readFileSync(lower, "utf8"));
  hooks.hooks.PreToolUse[0].hooks[0].command = "/legacy/guard-worktree.sh";
  fs.writeFileSync(lower, `${JSON.stringify(hooks, null, 2)}\n`, "utf8");
  const crashed = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_CRASH_AFTER_MANAGED_HOST_APPLY: "1" },
  );
  assert.equal(crashed.status, 86, crashed.stderr || crashed.stdout);
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  const interrupted = run(
    ["install", "--profile", "does-not-exist"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_CRASH_RECOVERY_DESTINATION_SUFFIX: ".codex/hooks.json" },
  );
  assert.equal(interrupted.status, 80, interrupted.stderr || interrupted.stdout);
  const journal = JSON.parse(fs.readFileSync(path.join(transactionRoot, "journal.json"), "utf8"));
  const aliases = journal.files.filter((entry) => [".Codex/hooks.json", ".codex/hooks.json"].includes(entry.path));
  assert.equal(aliases.length, 2);
  assert.equal(aliases.every((entry) => entry.recovery_state === "restore-intent"), true);

  const recovered = run(["install", "--profile", "does-not-exist"], project.root, project.home);
  assert.notEqual(recovered.status, 0);
  assert.match(recovered.stderr, /unknown profile/);
  assert.equal(fs.existsSync(transactionRoot), false);
});

test("recovery accepts a missing backup only when live state is the authenticated original", (t) => {
  const project = setupProject(t);
  writeSkill(path.join(project.root, ".agent-flow", "local-skills"), "missing-after-restore-skill");
  const crashed = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_CRASH_AFTER_MANAGED_HOST_APPLY: "1" },
  );
  assert.equal(crashed.status, 86, crashed.stderr || crashed.stdout);
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  const journal = JSON.parse(fs.readFileSync(path.join(transactionRoot, "journal.json"), "utf8"));
  const kitEntry = journal.files.find((entry) => entry.path === ".agent-flow/kit.json");
  const backup = path.join(project.root, kitEntry.backup);
  const live = path.join(project.root, kitEntry.path);
  fs.copyFileSync(backup, live);
  fs.chmodSync(live, kitEntry.mode);
  fs.rmSync(backup);

  const recovered = run(["install", "--profile", "does-not-exist"], project.root, project.home);
  assert.notEqual(recovered.status, 0);
  assert.match(recovered.stderr, /unknown profile/);
  assert.equal(fs.existsSync(transactionRoot), false);
});

test("synchronous rollback preserves a concurrent user edit and leaves recovery evidence", async (t) => {
  const project = setupProject(t);
  const agentsPath = path.join(project.root, "AGENTS.md");
  const install = spawnRun(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    {
      AGENT_FLOW_TEST_FAIL_AFTER_MANAGED_HOST_APPLY: "1",
      AGENT_FLOW_TEST_HOLD_BEFORE_LATE_INSTALL_FAILURE_MS: "1200",
    },
  );
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  await waitForPath(path.join(transactionRoot, "late-failure-ready"));
  fs.appendFileSync(agentsPath, "concurrent user edit\n", "utf8");
  const edited = fs.readFileSync(agentsPath);
  const failed = await install.completion;
  assert.notEqual(failed.status, 0);
  assert.match(failed.stderr, /destination changed after crash/);
  assert.deepEqual(fs.readFileSync(agentsPath), edited);
  assert.equal(fs.existsSync(transactionRoot), true);
});

test("commit point revalidates every critical path after the late hold", async (t) => {
  const project = setupProject(t);
  const script = path.join(project.root, MANAGED_HOOK_SCRIPT_PATHS[0]);
  const install = spawnRun(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_HOLD_BEFORE_LATE_INSTALL_FAILURE_MS: "1200" },
  );
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  await waitForPath(path.join(transactionRoot, "late-failure-ready"));
  fs.appendFileSync(script, "concurrent precommit tamper\n", "utf8");
  const tampered = fs.readFileSync(script);
  const result = await install.completion;
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /changed during install|changed after crash/);
  assert.deepEqual(fs.readFileSync(script), tampered);
  assert.equal(fs.existsSync(transactionRoot), true);
});

test("commit point revalidates keep host actions", async (t) => {
  const project = setupProject(t);
  const keepDestination = path.join(project.root, ".agents", "skills", "agent-flow");
  assert.equal(fs.existsSync(keepDestination), true);
  const install = spawnRun(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_HOLD_BEFORE_LATE_INSTALL_FAILURE_MS: "1200" },
  );
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  await waitForPath(path.join(transactionRoot, "late-failure-ready"));
  fs.rmSync(keepDestination, { recursive: true, force: true });
  const result = await install.completion;
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /host destination changed before commit/);
  assert.equal(fs.existsSync(keepDestination), false);
});

test("host apply and rollback preserve a concurrent host destination edit", async (t) => {
  const project = setupProject(t);
  writeSkill(path.join(project.root, ".agent-flow", "local-skills"), "host-cas-skill");
  const install = spawnRun(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    {
      AGENT_FLOW_TEST_HOLD_AFTER_FIRST_HOST_APPLY_MS: "1200",
      AGENT_FLOW_TEST_FAIL_AFTER_FIRST_HOST_APPLY: "1",
    },
  );
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  await waitForPath(path.join(transactionRoot, "host-apply-ready"));
  const destinations = [".agents", ".claude", ".omp"]
    .map((host) => path.join(project.root, host, "skills", "host-cas-skill"));
  const destination = destinations.find((candidate) => fs.existsSync(candidate));
  assert(destination);
  fs.rmSync(destination, { recursive: true, force: true });
  fs.mkdirSync(destination, { recursive: true });
  fs.writeFileSync(path.join(destination, "USER.txt"), "concurrent host edit\n", "utf8");
  const failed = await install.completion;
  assert.notEqual(failed.status, 0);
  assert.match(failed.stderr, /host destination changed|no regular SKILL\.md/);
  assert.equal(fs.readFileSync(path.join(destination, "USER.txt"), "utf8"), "concurrent host edit\n");
  assert.equal(fs.existsSync(transactionRoot), true);
});

test("recovery rechecks observed state immediately before mutation", async (t) => {
  const project = setupProject(t);
  writeSkill(path.join(project.root, ".agent-flow", "local-skills"), "recovery-cas-skill");
  const crashed = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_CRASH_AFTER_MANAGED_HOST_APPLY: "1" },
  );
  assert.equal(crashed.status, 86, crashed.stderr || crashed.stdout);
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  const recovery = spawnRun(
    ["install", "--profile", "does-not-exist"],
    project.root,
    project.home,
    { AGENT_FLOW_TEST_HOLD_AFTER_RECOVERY_PREFLIGHT_MS: "1200" },
  );
  await waitForPath(path.join(transactionRoot, "recovery-preflight-ready"));
  const competingRecovery = run(["install", "--profile", "does-not-exist"], project.root, project.home);
  assert.notEqual(competingRecovery.status, 0);
  assert.match(competingRecovery.stderr, /start lock exists/);
  const indexPath = path.join(project.root, ".agent-flow", "skills", "index.json");
  fs.appendFileSync(indexPath, "concurrent recovery edit\n", "utf8");
  const edited = fs.readFileSync(indexPath);
  const result = await recovery.completion;
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /changed during recovery/);
  assert.deepEqual(fs.readFileSync(indexPath), edited);
  assert.equal(fs.existsSync(transactionRoot), true);
});

test("AGENTS cannot adopt a concurrent edit as installer-applied state", async (t) => {
  const project = setupProject(t);
  const agents = path.join(project.root, "AGENTS.md");
  const install = spawnRun(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    {
      AGENT_FLOW_TEST_HOLD_AFTER_DIRECTORY_SKELETON_MS: "1200",
      AGENT_FLOW_TEST_FAIL_AFTER_MANAGED_HOST_APPLY: "1",
    },
  );
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  await waitForPath(path.join(transactionRoot, "open-journal-ready"));
  fs.appendFileSync(agents, "concurrent user-owned AGENTS policy\n", "utf8");
  const edited = fs.readFileSync(agents);
  const failed = await install.completion;
  assert.notEqual(failed.status, 0);
  assert.match(failed.stderr, /changed during install|changed after crash/);
  assert.deepEqual(fs.readFileSync(agents), edited);
  assert.equal(fs.existsSync(transactionRoot), true);
});

test("failed installs preserve legacy user files and do not register Codex trust", (t) => {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-legacy-trust-"));
  const root = path.join(sandbox, "project");
  const home = path.join(sandbox, "home");
  const codexHome = path.join(sandbox, "codex-home");
  fs.mkdirSync(root, { recursive: true });
  fs.mkdirSync(home, { recursive: true });
  fs.mkdirSync(codexHome, { recursive: true });
  t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));
  const initial = run(["install", "--profile", "node"], root, home);
  assert.equal(initial.status, 0, initial.stderr || initial.stdout);
  const legacyScript = path.join(root, "scripts", "user.sh");
  const legacyGraphify = path.join(root, ".claude", "skills", "graphify", "SKILL.md");
  fs.mkdirSync(path.dirname(legacyScript), { recursive: true });
  fs.mkdirSync(path.dirname(legacyGraphify), { recursive: true });
  fs.writeFileSync(legacyScript, "user script\n", "utf8");
  fs.writeFileSync(legacyGraphify, "user graphify\n", "utf8");
  fs.appendFileSync(path.join(root, ".gitignore"), "graphify/\n", "utf8");

  const failed = run(
    ["install", "--profile", "node", "--force-managed"],
    root,
    home,
    {
      AGENT_FLOW_TEST_FAIL_AFTER_MANAGED_HOST_APPLY: "1",
      AGENT_FLOW_SKIP_CODEX_TRUST: "0",
      CODEX_HOME: codexHome,
    },
  );
  assert.notEqual(failed.status, 0);
  assert.equal(fs.readFileSync(legacyScript, "utf8"), "user script\n");
  assert.equal(fs.readFileSync(legacyGraphify, "utf8"), "user graphify\n");
  assert.match(fs.readFileSync(path.join(root, ".gitignore"), "utf8"), /^graphify\/$/m);
  assert.equal(fs.existsSync(path.join(codexHome, "config.toml")), false);
});

test("reinstall upgrades only host files proven to be prior managed bytes", (t) => {
  const project = setupProject(t);
  const canonical = managedHostBytes(project.root);
  const kitPath = path.join(project.root, ".agent-flow", "kit.json");
  const kit = JSON.parse(fs.readFileSync(kitPath, "utf8"));
  for (const relative of MANAGED_HOST_PATHS) {
    const previousManaged = Buffer.from(`previous managed ${relative}\n`, "utf8");
    fs.writeFileSync(path.join(project.root, relative), previousManaged);
    kit.managed_host_files.files[relative].sha256 = sha256(previousManaged);
  }
  kit.managed_host_files_commitment = managedHostCommitment(kit);
  fs.writeFileSync(kitPath, `${JSON.stringify(kit, null, 2)}\n`, "utf8");

  const reinstall = run(["install", "--profile", "node"], project.root, project.home);
  assert.equal(reinstall.status, 0, reinstall.stderr || reinstall.stdout);
  const upgradedKit = JSON.parse(fs.readFileSync(kitPath, "utf8"));
  for (const relative of MANAGED_HOST_PATHS) {
    const upgraded = fs.readFileSync(path.join(project.root, relative));
    assert.deepEqual(upgraded, canonical.get(relative), relative);
    assert.equal(upgradedKit.managed_host_files.files[relative].sha256, sha256(upgraded), relative);
  }
});

test("managed host upgrades preserve the existing regular file mode", (t) => {
  const project = setupProject(t);
  const relative = ".Codex/agents/code-reviewer.md";
  const destination = path.join(project.root, relative);
  const canonical = fs.readFileSync(destination);
  const previousManaged = Buffer.from("previous managed reviewer\n", "utf8");
  fs.writeFileSync(destination, previousManaged);
  fs.chmodSync(destination, 0o600);
  const kitPath = path.join(project.root, ".agent-flow", "kit.json");
  const kit = JSON.parse(fs.readFileSync(kitPath, "utf8"));
  kit.managed_host_files.files[relative].sha256 = sha256(previousManaged);
  kit.managed_host_files_commitment = managedHostCommitment(kit);
  fs.writeFileSync(kitPath, `${JSON.stringify(kit, null, 2)}\n`, "utf8");

  const reinstall = run(["install", "--profile", "node"], project.root, project.home);
  assert.equal(reinstall.status, 0, reinstall.stderr || reinstall.stdout);
  assert.deepEqual(fs.readFileSync(destination), canonical);
  assert.equal(fs.statSync(destination).mode & 0o777, 0o600);
});

test("atomic managed writes preserve existing modes under a restrictive umask", (t) => {
  if (process.platform === "win32") {
    t.skip("POSIX mode semantics are required");
    return;
  }
  const project = setupProject(t);
  const existingPaths = [
    path.join(project.root, "AGENTS.md"),
    path.join(project.root, ".Codex", "hooks.json"),
  ];
  for (const candidate of existingPaths) fs.chmodSync(candidate, 0o666);
  let install = runWithRestrictiveUmask(
    ["install", "--profile", "node"],
    project.root,
    project.home,
  );
  assert.equal(install.status, 0, install.stderr || install.stdout);
  for (const candidate of existingPaths) {
    assert.equal(fs.statSync(candidate).mode & 0o777, 0o666, candidate);
  }

  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-new-mode-policy-"));
  const root = path.join(sandbox, "project");
  const home = path.join(sandbox, "home");
  fs.mkdirSync(root, { recursive: true });
  fs.mkdirSync(home, { recursive: true });
  t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));
  install = runWithRestrictiveUmask(["install", "--profile", "node"], root, home);
  assert.equal(install.status, 0, install.stderr || install.stdout);
  assert.equal(fs.statSync(path.join(root, "AGENTS.md")).mode & 0o777, 0o600);
  assert.equal(fs.statSync(path.join(root, ".Codex", "hooks.json")).mode & 0o777, 0o600);
});

test("critical text and JSON writes recheck live state immediately before rename", async (t) => {
  for (const scenario of [
    { name: "text", relative: "AGENTS.md" },
    { name: "json", relative: ".agent-flow/kit.json" },
  ]) {
    const project = setupProject(t);
    const target = path.join(project.root, scenario.relative);
    const install = spawnRun(
      ["install", "--profile", "node"],
      project.root,
      project.home,
      {
        AGENT_FLOW_TEST_HOLD_AFTER_CRITICAL_PREPARE_MS: "1200",
        AGENT_FLOW_TEST_CRITICAL_PREPARE_SUFFIX: target,
      },
    );
    const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
    await waitForPath(path.join(transactionRoot, "critical-rename-ready"));
    fs.appendFileSync(target, `concurrent ${scenario.name} edit\n`, "utf8");
    fs.chmodSync(target, 0o640);
    const edited = fs.readFileSync(target);
    const result = await install.completion;
    assert.notEqual(result.status, 0, scenario.name);
    assert.match(result.stderr, /critical install destination changed before mutation/, scenario.name);
    assert.deepEqual(fs.readFileSync(target), edited, scenario.name);
    assert.equal(fs.statSync(target).mode & 0o777, 0o640, scenario.name);
    assert.equal(fs.existsSync(transactionRoot), true, scenario.name);
  }
});

test("critical delete directory-create and chmod paths share immediate CAS", async (t) => {
  const scenarios = [
    {
      name: "delete",
      target(project) {
        const candidate = path.join(project.root, ".agent-flow", "scripts", "check-context-docs.mjs");
        fs.writeFileSync(candidate, "stale managed script\n", "utf8");
        return candidate;
      },
      args: ["install", "--profile", "node", "--force-managed"],
      mutate(candidate) {
        fs.appendFileSync(candidate, "concurrent delete edit\n", "utf8");
      },
      assertPreserved(candidate) {
        assert.match(fs.readFileSync(candidate, "utf8"), /concurrent delete edit/);
      },
    },
    {
      name: "directory-create",
      target(project) {
        const candidate = path.join(project.root, ".agent-flow", "prompts");
        fs.rmSync(candidate, { recursive: true, force: true });
        return candidate;
      },
      args: ["install", "--profile", "node"],
      mutate(candidate) {
        fs.mkdirSync(candidate);
        fs.writeFileSync(path.join(candidate, "USER.txt"), "concurrent directory\n", "utf8");
      },
      assertPreserved(candidate) {
        assert.equal(fs.readFileSync(path.join(candidate, "USER.txt"), "utf8"), "concurrent directory\n");
      },
    },
    {
      name: "chmod",
      target(project) {
        const candidate = path.join(project.root, MANAGED_HOOK_SCRIPT_PATHS[0]);
        fs.chmodSync(candidate, 0o644);
        return candidate;
      },
      args: ["install", "--profile", "node"],
      mutate(candidate) {
        fs.appendFileSync(candidate, "concurrent chmod edit\n", "utf8");
        fs.chmodSync(candidate, 0o600);
      },
      assertPreserved(candidate) {
        assert.match(fs.readFileSync(candidate, "utf8"), /concurrent chmod edit/);
        assert.equal(fs.statSync(candidate).mode & 0o777, 0o600);
      },
    },
  ];
  for (const scenario of scenarios) {
    const project = setupProject(t);
    const target = scenario.target(project);
    const install = spawnRun(
      scenario.args,
      project.root,
      project.home,
      {
        AGENT_FLOW_TEST_HOLD_AFTER_CRITICAL_PREPARE_MS: "1200",
        AGENT_FLOW_TEST_CRITICAL_PREPARE_SUFFIX: target,
      },
    );
    const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
    await waitForPath(path.join(transactionRoot, "critical-rename-ready"));
    scenario.mutate(target);
    const result = await install.completion;
    assert.notEqual(result.status, 0, scenario.name);
    assert.match(result.stderr, /critical install destination changed before mutation/, scenario.name);
    scenario.assertPreserved(target);
    assert.equal(fs.existsSync(transactionRoot), true, scenario.name);
  }
});

test("managed host manifest tampering cannot authorize overwriting user bytes", (t) => {
  const project = setupProject(t);
  const kitPath = path.join(project.root, ".agent-flow", "kit.json");
  const kit = JSON.parse(fs.readFileSync(kitPath, "utf8"));
  const relative = ".Codex/agents/code-reviewer.md";
  const destination = path.join(project.root, relative);
  const userBytes = Buffer.from("user-owned reviewer bytes\n", "utf8");
  fs.writeFileSync(destination, userBytes);
  kit.managed_host_files.files[relative].sha256 = sha256(userBytes);
  fs.writeFileSync(kitPath, `${JSON.stringify(kit, null, 2)}\n`, "utf8");

  for (const args of [["install", "--profile", "node"], ["install", "--profile", "node", "--force-managed"]]) {
    const reinstall = run(args, project.root, project.home);
    assert.notEqual(reinstall.status, 0, args.join(" "));
    assert.match(reinstall.stderr, /managed host file commitment does not match provenance/);
    assert.deepEqual(fs.readFileSync(destination), userBytes);
  }
});

test("reinstall fails closed without overwriting user-modified host files", (t) => {
  const project = setupProject(t);
  const kitPath = path.join(project.root, ".agent-flow", "kit.json");
  const kitBefore = fs.readFileSync(kitPath);
  const modified = new Map();
  for (const relative of MANAGED_HOST_PATHS) {
    const content = Buffer.from(`user-owned ${relative}\n`, "utf8");
    modified.set(relative, content);
    fs.writeFileSync(path.join(project.root, relative), content);
  }

  for (const args of [["install", "--profile", "node"], ["install", "--profile", "node", "--force-managed"]]) {
    const reinstall = run(args, project.root, project.home);
    assert.notEqual(reinstall.status, 0, args.join(" "));
    assert.match(reinstall.stderr, /user-modified managed host file differs/);
    assert.deepEqual(fs.readFileSync(kitPath), kitBefore);
    for (const [relative, content] of modified) {
      assert.deepEqual(fs.readFileSync(path.join(project.root, relative)), content, relative);
    }
  }
});

test("installer writes Codex trust to explicit CODEX_HOME on fresh install and reinstall", (t) => {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-installer-codex-home-"));
  const root = path.join(sandbox, "project");
  const home = path.join(sandbox, "home");
  const codexHome = path.join(sandbox, "explicit-codex-home");
  const fakeCodex = path.join(sandbox, "fake-codex");
  fs.mkdirSync(root, { recursive: true });
  fs.mkdirSync(home, { recursive: true });
  fs.mkdirSync(codexHome, { recursive: true });
  const configPath = path.join(codexHome, "config.toml");
  fs.writeFileSync(configPath, "existing = \"setting\"\n", "utf8");
  fs.chmodSync(configPath, 0o660);
  fs.copyFileSync(path.join(KIT_ROOT, "tests", "fixtures", "fake_codex_app_server.py"), fakeCodex);
  fs.chmodSync(fakeCodex, 0o755);
  t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));
  const env = {
    AGENT_FLOW_SKIP_CODEX_TRUST: "0",
    CODEX_CLI_PATH: fakeCodex,
    CODEX_HOME: codexHome,
  };
  const canonicalRoot = fs.realpathSync.native(root);

  for (let round = 0; round < 2; round += 1) {
    const install = run(["install", "--profile", "node"], root, home, env);
    assert.equal(install.status, 0, install.stderr || install.stdout);
    const config = fs.readFileSync(configPath, "utf8");
    assert.equal(config.includes(`[projects."${canonicalRoot}"]`), true);
    assert.match(config, /trust_level = "trusted"/);
    assert.match(config, /trusted_hash = "sha256:[0-9a-f]{64}"/);
    assert.equal(fs.statSync(configPath).mode & 0o777, 0o660);
    assert.equal(fs.existsSync(path.join(home, ".codex", "config.toml")), false);
  }
});

test("active Codex install fails closed when hook trust cannot be registered", (t) => {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-active-codex-trust-"));
  t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));
  const project = { root: path.join(sandbox, "project"), home: path.join(sandbox, "home") };
  const codexHome = path.join(sandbox, "unavailable-trust-codex-home");
  fs.mkdirSync(project.root);
  fs.mkdirSync(project.home);
  fs.mkdirSync(codexHome);
  git(project.root, ["init", "-b", "main"]);
  git(project.root, ["config", "user.email", "test@example.com"]);
  git(project.root, ["config", "user.name", "Test User"]);
  fs.writeFileSync(path.join(project.root, "README.md"), "fixture\n", "utf8");
  git(project.root, ["add", "README.md"]);
  git(project.root, ["commit", "-m", "init"]);
  const fakeCodex = path.join(codexHome, "codex-without-hooks");
  fs.writeFileSync(
    fakeCodex,
    "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo fake-codex; exit 0; fi\nexit 1\n",
    { mode: 0o755 },
  );
  const install = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    {
      AGENT_FLOW_HOST: "codex",
      AGENT_FLOW_SKIP_CODEX_TRUST: "0",
      CODEX_CLI_PATH: fakeCodex,
      CODEX_HOME: codexHome,
    },
  );
  assert.notEqual(install.status, 0, install.stdout);
  assert.match(install.stderr, /active Codex hook trust registration returned no managed project hooks/);
  assert.equal(fs.existsSync(path.join(project.root, ".agent-flow", "kit.json")), false);
  assert.equal(fs.existsSync(path.join(codexHome, "config.toml")), false);
});

test("active Codex install restores trust config when the project commit fails", (t) => {
  const project = setupProject(t);
  const sandbox = path.dirname(project.root);
  const codexHome = path.join(sandbox, "commit-failure-codex-home");
  const fakeCodex = path.join(sandbox, "commit-failure-fake-codex");
  const configPath = path.join(codexHome, "config.toml");
  const initialConfig = Buffer.from("existing = \"setting\"\n", "utf8");
  const kitPath = path.join(project.root, ".agent-flow", "kit.json");
  const initialKit = fs.readFileSync(kitPath);
  fs.mkdirSync(codexHome, { recursive: true });
  fs.writeFileSync(configPath, initialConfig, { mode: 0o640 });
  fs.copyFileSync(path.join(KIT_ROOT, "tests", "fixtures", "fake_codex_app_server.py"), fakeCodex);
  fs.chmodSync(fakeCodex, 0o755);

  const install = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    {
      AGENT_FLOW_HOST: "codex",
      AGENT_FLOW_SKIP_CODEX_TRUST: "0",
      AGENT_FLOW_TEST_FAIL_BEFORE_INSTALL_COMMIT: "1",
      CODEX_CLI_PATH: fakeCodex,
      CODEX_HOME: codexHome,
    },
  );

  assert.notEqual(install.status, 0, install.stdout);
  assert.match(install.stderr, /injected install commit failure/);
  assert.deepEqual(fs.readFileSync(configPath), initialConfig);
  assert.equal(fs.statSync(configPath).mode & 0o777, 0o640);
  assert.deepEqual(fs.readFileSync(kitPath), initialKit);
  assert.equal(
    fs.readdirSync(codexHome).some((name) => name.startsWith(".config.toml.agent-flow-")),
    false,
  );

  fs.unlinkSync(configPath);
  const freshConfigInstall = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    {
      AGENT_FLOW_HOST: "codex",
      AGENT_FLOW_SKIP_CODEX_TRUST: "0",
      AGENT_FLOW_TEST_FAIL_BEFORE_INSTALL_COMMIT: "1",
      CODEX_CLI_PATH: fakeCodex,
      CODEX_HOME: codexHome,
    },
  );
  assert.notEqual(freshConfigInstall.status, 0, freshConfigInstall.stdout);
  assert.match(freshConfigInstall.stderr, /injected install commit failure/);
  assert.equal(fs.existsSync(configPath), false);
  assert.deepEqual(fs.readFileSync(kitPath), initialKit);
});

test("active Codex trust remains committed when project cleanup fails after commit", (t) => {
  const project = setupProject(t);
  const fixture = setupActiveCodexTrustFixture(project, "project-cleanup-failure", {
    configBytes: Buffer.from("existing = \"setting\"\n", "utf8"),
    mode: 0o640,
  });

  const install = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    {
      ...fixture.env,
      AGENT_FLOW_TEST_FAIL_AFTER_INSTALL_COMMIT: "1",
    },
  );

  assert.notEqual(install.status, 0, install.stdout);
  assert.match(install.stderr, /cleanup failure after commit/);
  const trusted = fs.readFileSync(fixture.configPath);
  assert.match(trusted.toString("utf8"), /trust_level = "trusted"/);
  assert.equal(fs.statSync(fixture.configPath).mode & 0o777, 0o640);
  assert.equal(
    fs.existsSync(path.join(fixture.codexHome, ".config.toml.agent-flow.lock.json")),
    false,
  );
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  const journal = JSON.parse(fs.readFileSync(path.join(transactionRoot, "journal.json"), "utf8"));
  assert.equal(journal.status, "committed");
  assert.equal(journal.codex_trust.version, 1);
  assert.equal(journal.codex_trust.config_path, fs.realpathSync.native(fixture.configPath));

  const recovered = run(
    ["install", "--profile", "does-not-exist"],
    project.root,
    project.home,
    { CODEX_HOME: path.join(path.dirname(project.root), "different-codex-home") },
  );
  assert.notEqual(recovered.status, 0);
  assert.match(recovered.stderr, /unknown profile/);
  assert.deepEqual(fs.readFileSync(fixture.configPath), trusted);
  assert.equal(fs.existsSync(transactionRoot), false);
});

test("committed project journal cannot recreate Codex trust without an external receipt", (t) => {
  const project = setupProject(t);
  const fixture = setupActiveCodexTrustFixture(project, "project-only-recovery", {
    configBytes: Buffer.from("existing = \"setting\"\n", "utf8"),
    mode: 0o640,
  });
  const install = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    {
      ...fixture.env,
      AGENT_FLOW_TEST_FAIL_AFTER_INSTALL_COMMIT: "1",
    },
  );
  assert.notEqual(install.status, 0, install.stdout);
  assert.match(install.stderr, /cleanup failure after commit/);
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  assert.equal(fs.existsSync(transactionRoot), true);
  assert.equal(
    fs.existsSync(path.join(fixture.codexHome, ".config.toml.agent-flow.lock.json")),
    false,
  );
  const externalEdit = Buffer.from("external = \"preserve\"\n", "utf8");
  fs.writeFileSync(fixture.configPath, externalEdit, { mode: 0o600 });
  fs.chmodSync(fixture.configPath, 0o600);

  const recovered = run(
    ["install", "--profile", "does-not-exist"],
    project.root,
    project.home,
    fixture.env,
  );

  assert.notEqual(recovered.status, 0);
  assert.match(recovered.stderr, /unknown profile/);
  assert.deepEqual(fs.readFileSync(fixture.configPath), externalEdit);
  assert.equal(fs.statSync(fixture.configPath).mode & 0o777, 0o600);
  assert.equal(fs.existsSync(transactionRoot), false);
});

test("active Codex crash recovery restores durable trust config bytes and mode", (t) => {
  const project = setupProject(t);
  const fixture = setupActiveCodexTrustFixture(project, "crash-existing", {
    configBytes: Buffer.from("existing = \"setting\"\n", "utf8"),
    mode: 0o640,
  });
  const kitPath = path.join(project.root, ".agent-flow", "kit.json");
  const initialKit = fs.readFileSync(kitPath);

  const crashed = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    {
      ...fixture.env,
      AGENT_FLOW_TEST_CRASH_AFTER_CODEX_TRUST_APPLY: "1",
    },
  );

  assert.equal(crashed.status, 87, crashed.stderr || crashed.stdout);
  const appliedConfig = fs.readFileSync(fixture.configPath);
  assert.notDeepEqual(appliedConfig, fixture.configBytes);
  const transactionRoot = path.join(project.root, ".agent-flow", "install-transaction");
  const projectJournal = JSON.parse(fs.readFileSync(
    path.join(transactionRoot, "journal.json"),
    "utf8",
  ));
  assert.equal(projectJournal.status, "committed");
  assert.equal(projectJournal.codex_trust.version, 1);
  const trustJournalPath = path.join(
    project.root,
    ".git",
    "agent-flow",
    "codex-worktree-trust.json",
  );
  const journal = JSON.parse(fs.readFileSync(trustJournalPath, "utf8"));
  assert.equal(journal.config_path, fs.realpathSync.native(fixture.configPath));
  const receiptPath = journal.receipt_path;
  assert.equal(fs.existsSync(receiptPath), true);
  const committedKit = fs.readFileSync(kitPath);
  assert.deepEqual(committedKit, initialKit);

  const recovered = run(
    ["install", "--profile", "does-not-exist"],
    project.root,
    project.home,
    { CODEX_HOME: path.join(path.dirname(project.root), "different-codex-home") },
  );

  assert.notEqual(recovered.status, 0);
  assert.match(recovered.stderr, /unknown profile/);
  assert.deepEqual(fs.readFileSync(fixture.configPath), appliedConfig);
  assert.equal(fs.statSync(fixture.configPath).mode & 0o777, 0o640);
  assert.deepEqual(fs.readFileSync(kitPath), committedKit);
  assert.equal(fs.existsSync(receiptPath), false);
  assert.equal(fs.existsSync(trustJournalPath), false);
  assert.equal(fs.existsSync(transactionRoot), false);
});

test(
  "active Codex trust encodes control characters in project and hook keys across recovery",
  { skip: process.platform === "win32" },
  (t) => {
    const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-control-toml-"));
    const root = path.join(sandbox, "project-\n-\t-\u0001-\u007f-\"-\\");
    const home = path.join(sandbox, "home");
    const codexHome = path.join(sandbox, "codex-home");
    const fakeCodex = path.join(sandbox, "fake-codex");
    const configPath = path.join(codexHome, "config.toml");
    const hookKeySuffix = "-hook-\n-\t-\u0002-\u007f-\"-\\";
    fs.mkdirSync(root);
    fs.mkdirSync(home);
    fs.mkdirSync(codexHome);
    fs.copyFileSync(path.join(KIT_ROOT, "tests", "fixtures", "fake_codex_app_server.py"), fakeCodex);
    fs.chmodSync(fakeCodex, 0o755);
    t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));
    git(root, ["init", "-b", "main"]);
    git(root, ["config", "user.email", "test@example.com"]);
    git(root, ["config", "user.name", "Test User"]);
    fs.writeFileSync(path.join(root, "README.md"), "fixture\n", "utf8");
    git(root, ["add", "README.md"]);
    git(root, ["commit", "-m", "init"]);
    const bootstrap = run(["install", "--profile", "node"], root, home);
    assert.equal(bootstrap.status, 0, bootstrap.stderr || bootstrap.stdout);

    const crashed = run(
      ["install", "--profile", "node"],
      root,
      home,
      {
        AGENT_FLOW_HOST: "codex",
        AGENT_FLOW_SKIP_CODEX_TRUST: "0",
        AGENT_FLOW_TEST_CRASH_AFTER_CODEX_TRUST_APPLY: "1",
        CODEX_CLI_PATH: fakeCodex,
        CODEX_HOME: codexHome,
        FAKE_CODEX_HOOK_KEY_SUFFIX: hookKeySuffix,
      },
    );
    assert.equal(crashed.status, 87, crashed.stderr || crashed.stdout);
    const canonicalRoot = fs.realpathSync.native(root);
    const applied = parseTomlDocument(fs.readFileSync(configPath, "utf8"));
    assert.equal(applied.projects[canonicalRoot].trust_level, "trusted");
    const appliedHookKeys = Object.keys(applied.hooks.state);
    assert.equal(appliedHookKeys.length, 6);
    assert.equal(appliedHookKeys.every((key) => key.endsWith(hookKeySuffix)), true);
    const transactionRoot = path.join(root, ".agent-flow", "install-transaction");
    const journal = JSON.parse(
      fs.readFileSync(path.join(transactionRoot, "journal.json"), "utf8"),
    );
    assert.equal(journal.status, "committed");
    assert.equal(journal.codex_trust.updates[0].tablePath[1], canonicalRoot);
    assert.equal(
      journal.codex_trust.updates.slice(1).every((update) => update.tablePath[2].endsWith(hookKeySuffix)),
      true,
    );

    const recovered = run(
      ["install", "--profile", "does-not-exist"],
      root,
      home,
      { CODEX_HOME: path.join(sandbox, "different-codex-home") },
    );
    assert.notEqual(recovered.status, 0);
    assert.match(recovered.stderr, /unknown profile/);
    const finalConfig = parseTomlDocument(fs.readFileSync(configPath, "utf8"));
    assert.equal(finalConfig.projects[canonicalRoot].trust_level, "trusted");
    assert.equal(Object.keys(finalConfig.hooks.state).length, 6);
    assert.equal(fs.existsSync(transactionRoot), false);
  },
);

test("active Codex crash recovery removes a newly created trust config", (t) => {
  const project = setupProject(t);
  const fixture = setupActiveCodexTrustFixture(project, "crash-fresh");

  const crashed = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    {
      ...fixture.env,
      AGENT_FLOW_TEST_CRASH_AFTER_CODEX_TRUST_APPLY: "1",
    },
  );

  assert.equal(crashed.status, 87, crashed.stderr || crashed.stdout);
  assert.equal(fs.existsSync(fixture.configPath), true);
  const trustJournalPath = path.join(
    project.root,
    ".git",
    "agent-flow",
    "codex-worktree-trust.json",
  );
  const journal = JSON.parse(fs.readFileSync(trustJournalPath, "utf8"));
  const receiptPath = journal.receipt_path;
  assert.equal(fs.existsSync(receiptPath), true);

  const recovered = run(
    ["install", "--profile", "does-not-exist"],
    project.root,
    project.home,
    { CODEX_HOME: path.join(path.dirname(project.root), "different-codex-home") },
  );

  assert.notEqual(recovered.status, 0);
  assert.match(recovered.stderr, /unknown profile/);
  assert.equal(fs.existsSync(fixture.configPath), true);
  assert.match(fs.readFileSync(fixture.configPath, "utf8"), /trust_level = "trusted"/);
  assert.equal(fs.existsSync(receiptPath), false);
  assert.equal(fs.existsSync(trustJournalPath), false);
  assert.equal(
    fs.existsSync(path.join(project.root, ".agent-flow", "install-transaction")),
    false,
  );
});

test("active Codex crash recovery preserves an external config edit and evidence", (t) => {
  const project = setupProject(t);
  const fixture = setupActiveCodexTrustFixture(project, "crash-external", {
    configBytes: Buffer.from("existing = \"setting\"\n", "utf8"),
    mode: 0o640,
  });

  const crashed = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    {
      ...fixture.env,
      AGENT_FLOW_TEST_CRASH_AFTER_CODEX_TRUST_APPLY: "1",
    },
  );
  assert.equal(crashed.status, 87, crashed.stderr || crashed.stdout);
  const trustJournalPath = path.join(
    project.root,
    ".git",
    "agent-flow",
    "codex-worktree-trust.json",
  );
  const journal = JSON.parse(fs.readFileSync(trustJournalPath, "utf8"));
  const externalBytes = Buffer.from("external = \"preserved\"\n", "utf8");
  fs.writeFileSync(fixture.configPath, externalBytes, { mode: 0o600 });

  const recovered = run(
    ["install", "--profile", "does-not-exist"],
    project.root,
    project.home,
    { CODEX_HOME: path.join(path.dirname(project.root), "different-codex-home") },
  );

  assert.notEqual(recovered.status, 0);
  assert.match(recovered.stderr, /unknown profile/);
  const recoveredConfig = fs.readFileSync(fixture.configPath, "utf8");
  assert.match(recoveredConfig, /external = "preserved"/);
  assert.match(recoveredConfig, /trust_level = "trusted"/);
  assert.equal(fs.existsSync(journal.receipt_path), false);
  assert.equal(fs.existsSync(trustJournalPath), false);
  assert.equal(
    fs.existsSync(path.join(project.root, ".agent-flow", "install-transaction")),
    false,
  );
});

test("active Codex committed trust cleanup uses the recorded config across CODEX_HOME changes", (t) => {
  const project = setupProject(t);
  const fixture = setupActiveCodexTrustFixture(project, "crash-committed", {
    configBytes: Buffer.from("existing = \"setting\"\n", "utf8"),
    mode: 0o640,
  });

  const crashed = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    {
      ...fixture.env,
      AGENT_FLOW_TEST_CRASH_AFTER_CODEX_TRUST_COMMIT: "1",
    },
  );

  assert.equal(crashed.status, 88, crashed.stderr || crashed.stdout);
  const trustedBytes = fs.readFileSync(fixture.configPath);
  assert.match(trustedBytes.toString("utf8"), /trust_level = "trusted"/);
  assert.equal(fs.statSync(fixture.configPath).mode & 0o777, 0o640);
  const trustJournalPath = path.join(
    project.root,
    ".git",
    "agent-flow",
    "codex-worktree-trust.json",
  );
  const journal = JSON.parse(fs.readFileSync(trustJournalPath, "utf8"));
  const receiptPath = journal.receipt_path;
  const receipt = JSON.parse(fs.readFileSync(receiptPath, "utf8"));
  assert.equal(receipt.status, "committed");

  const recovered = run(
    ["install", "--profile", "does-not-exist"],
    project.root,
    project.home,
    { CODEX_HOME: path.join(path.dirname(project.root), "different-codex-home") },
  );

  assert.notEqual(recovered.status, 0);
  assert.match(recovered.stderr, /unknown profile/);
  assert.deepEqual(fs.readFileSync(fixture.configPath), trustedBytes);
  assert.equal(fs.statSync(fixture.configPath).mode & 0o777, 0o640);
  assert.equal(fs.existsSync(receiptPath), false);
  assert.equal(fs.existsSync(trustJournalPath), false);
  assert.equal(
    fs.existsSync(path.join(project.root, ".agent-flow", "install-transaction")),
    false,
  );
});

test("Codex trust is not written when managed hooks change during the trust query", async (t) => {
  const project = setupProject(t);
  const sandbox = path.dirname(project.root);
  const codexHome = path.join(sandbox, "trust-race-codex-home");
  const fakeCodex = path.join(sandbox, "trust-race-fake-codex");
  fs.mkdirSync(codexHome, { recursive: true });
  fs.copyFileSync(path.join(KIT_ROOT, "tests", "fixtures", "fake_codex_app_server.py"), fakeCodex);
  fs.chmodSync(fakeCodex, 0o755);
  const install = spawnRun(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    {
      AGENT_FLOW_SKIP_CODEX_TRUST: "0",
      AGENT_FLOW_TEST_HOLD_AFTER_CODEX_TRUST_QUERY_MS: "1200",
      CODEX_CLI_PATH: fakeCodex,
      CODEX_HOME: codexHome,
    },
  );
  await waitForPath(path.join(project.root, ".agent-flow", "trust-query-ready"));
  const hooksPath = path.join(project.root, ".codex", "hooks.json");
  const hooks = JSON.parse(fs.readFileSync(hooksPath, "utf8"));
  hooks.hooks.PreToolUse[0].matcher = "TamperedBash";
  fs.writeFileSync(hooksPath, `${JSON.stringify(hooks, null, 2)}\n`, "utf8");
  const result = await install.completion;
  assert.notEqual(result.status, 0, result.stderr || result.stdout);
  assert.match(
    result.stderr,
    /managed hook commitment changed|managed hook settings do not match|installed managed hook settings changed/,
  );
  const configPath = path.join(codexHome, "config.toml");
  if (fs.existsSync(configPath)) {
    const config = fs.readFileSync(configPath, "utf8");
    assert.doesNotMatch(config, /trusted_hash|trust_level\s*=\s*"trusted"/);
  }
});

test("Codex trust config atomic update preserves a concurrent external edit", async (t) => {
  const project = setupProject(t);
  const sandbox = path.dirname(project.root);
  const codexHome = path.join(sandbox, "config-race-codex-home");
  const fakeCodex = path.join(sandbox, "config-race-fake-codex");
  const configPath = path.join(codexHome, "config.toml");
  fs.mkdirSync(codexHome, { recursive: true });
  fs.writeFileSync(configPath, "initial = \"config\"\n", { encoding: "utf8", mode: 0o640 });
  fs.copyFileSync(path.join(KIT_ROOT, "tests", "fixtures", "fake_codex_app_server.py"), fakeCodex);
  fs.chmodSync(fakeCodex, 0o755);
  const install = spawnRun(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    {
      AGENT_FLOW_SKIP_CODEX_TRUST: "0",
      AGENT_FLOW_TEST_HOLD_BEFORE_CODEX_CONFIG_RENAME_MS: "1200",
      CODEX_CLI_PATH: fakeCodex,
      CODEX_HOME: codexHome,
    },
  );
  await waitForPath(path.join(project.root, ".agent-flow", "codex-config-rename-ready"));
  const externalBytes = Buffer.from("external = \"edit\"\n", "utf8");
  fs.writeFileSync(configPath, externalBytes);
  const result = await install.completion;
  assert.notEqual(result.status, 0, result.stderr || result.stdout);
  assert.match(result.stderr, /Codex config changed at the no-clobber publish boundary/);
  assert.deepEqual(fs.readFileSync(configPath), externalBytes);
  const pendingStatus = run(["status"], project.root, project.home);
  assert.notEqual(pendingStatus.status, 0);
  assert.match(pendingStatus.stderr, /project install transaction is in progress/);
  assert.equal(
    fs.readdirSync(codexHome).some((name) => name.startsWith(".config.toml.agent-flow-")),
    false,
  );
  const recovered = run(
    ["install", "--profile", "does-not-exist"],
    project.root,
    project.home,
    { CODEX_HOME: path.join(sandbox, "different-codex-home") },
  );
  assert.notEqual(recovered.status, 0);
  assert.match(recovered.stderr, /unknown profile/);
  const recoveredConfig = fs.readFileSync(configPath, "utf8");
  assert.match(recoveredConfig, /external = "edit"/);
  assert.match(recoveredConfig, /trust_level = "trusted"/);
  assert.equal(
    fs.existsSync(path.join(project.root, ".agent-flow", "install-transaction")),
    false,
  );
});

test("Codex trust config atomic CAS preserves a concurrent mode-only edit", async (t) => {
  const project = setupProject(t);
  const sandbox = path.dirname(project.root);
  const codexHome = path.join(sandbox, "config-mode-race-codex-home");
  const fakeCodex = path.join(sandbox, "config-mode-race-fake-codex");
  const configPath = path.join(codexHome, "config.toml");
  fs.mkdirSync(codexHome, { recursive: true });
  const initialBytes = Buffer.from("initial = \"config\"\n", "utf8");
  fs.writeFileSync(configPath, initialBytes);
  fs.chmodSync(configPath, 0o640);
  fs.copyFileSync(path.join(KIT_ROOT, "tests", "fixtures", "fake_codex_app_server.py"), fakeCodex);
  fs.chmodSync(fakeCodex, 0o755);
  const install = spawnRun(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    {
      AGENT_FLOW_SKIP_CODEX_TRUST: "0",
      AGENT_FLOW_TEST_HOLD_BEFORE_CODEX_CONFIG_RENAME_MS: "1200",
      CODEX_CLI_PATH: fakeCodex,
      CODEX_HOME: codexHome,
    },
  );
  await waitForPath(path.join(project.root, ".agent-flow", "codex-config-rename-ready"));
  fs.chmodSync(configPath, 0o600);
  const result = await install.completion;
  assert.notEqual(result.status, 0, result.stderr || result.stdout);
  assert.match(result.stderr, /Codex config changed at the no-clobber publish boundary/);
  assert.deepEqual(fs.readFileSync(configPath), initialBytes);
  assert.equal(fs.statSync(configPath).mode & 0o777, 0o600);
});

test("Codex trust config transaction serializes installs from two projects", async (t) => {
  const first = setupProject(t);
  const sandbox = path.dirname(first.root);
  const secondRoot = path.join(sandbox, "project-two");
  const codexHome = path.join(sandbox, "shared-codex-home");
  const fakeCodex = path.join(sandbox, "shared-fake-codex");
  const configPath = path.join(codexHome, "config.toml");
  fs.mkdirSync(secondRoot);
  git(secondRoot, ["init", "-b", "main"]);
  git(secondRoot, ["config", "user.email", "test@example.com"]);
  git(secondRoot, ["config", "user.name", "Test User"]);
  fs.writeFileSync(path.join(secondRoot, "README.md"), "fixture\n", "utf8");
  git(secondRoot, ["add", "README.md"]);
  git(secondRoot, ["commit", "-m", "init"]);
  const secondBootstrap = run(["install", "--profile", "node"], secondRoot, first.home);
  assert.equal(secondBootstrap.status, 0, secondBootstrap.stderr || secondBootstrap.stdout);
  fs.mkdirSync(codexHome);
  fs.writeFileSync(configPath, "initial = \"config\"\n", { encoding: "utf8", mode: 0o640 });
  fs.copyFileSync(path.join(KIT_ROOT, "tests", "fixtures", "fake_codex_app_server.py"), fakeCodex);
  fs.chmodSync(fakeCodex, 0o755);
  const sharedEnv = {
    AGENT_FLOW_HOST: "codex",
    AGENT_FLOW_SKIP_CODEX_TRUST: "0",
    CODEX_CLI_PATH: fakeCodex,
    CODEX_HOME: codexHome,
  };

  const firstInstall = spawnRun(
    ["install", "--profile", "node"],
    first.root,
    first.home,
    {
      ...sharedEnv,
      AGENT_FLOW_TEST_HOLD_BEFORE_CODEX_CONFIG_RENAME_MS: "10000",
    },
  );
  await waitForPath(path.join(first.root, ".agent-flow", "codex-config-rename-ready"));
  const blockedSecond = run(
    ["install", "--profile", "node"],
    secondRoot,
    first.home,
    sharedEnv,
  );
  assert.notEqual(blockedSecond.status, 0, blockedSecond.stdout);
  assert.match(blockedSecond.stderr, /Codex config transaction is active|lock is already held/);
  const firstResult = await firstInstall.completion;
  assert.equal(firstResult.status, 0, firstResult.stderr || firstResult.stdout);

  const retriedSecond = run(
    ["install", "--profile", "node"],
    secondRoot,
    first.home,
    sharedEnv,
  );
  assert.equal(retriedSecond.status, 0, retriedSecond.stderr || retriedSecond.stdout);
  const config = fs.readFileSync(configPath, "utf8");
  assert.equal(config.includes(`[projects."${fs.realpathSync.native(first.root)}"]`), true);
  assert.equal(config.includes(`[projects."${fs.realpathSync.native(secondRoot)}"]`), true);
});

test("Codex trust updates an empty target table at EOF without duplicating it", (t) => {
  const project = setupProject(t);
  const sandbox = path.dirname(project.root);
  const codexHome = path.join(sandbox, "config-eof-table-codex-home");
  const fakeCodex = path.join(sandbox, "config-eof-table-fake-codex");
  const configPath = path.join(codexHome, "config.toml");
  fs.mkdirSync(codexHome, { recursive: true });
  const canonicalRoot = fs.realpathSync.native(project.root);
  const tableHeader = `[projects."${canonicalRoot.replaceAll("\\", "\\\\").replaceAll("\"", "\\\"")}"]`;
  fs.writeFileSync(configPath, tableHeader, "utf8");
  fs.copyFileSync(path.join(KIT_ROOT, "tests", "fixtures", "fake_codex_app_server.py"), fakeCodex);
  fs.chmodSync(fakeCodex, 0o755);

  const install = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    {
      AGENT_FLOW_SKIP_CODEX_TRUST: "0",
      CODEX_CLI_PATH: fakeCodex,
      CODEX_HOME: codexHome,
    },
  );
  assert.equal(install.status, 0, install.stderr || install.stdout);
  const config = fs.readFileSync(configPath, "utf8");
  assert.equal(config.split(tableHeader).length - 1, 1);
  assert.match(config, /trust_level = "trusted"/);
  assert.equal(config.startsWith(`${tableHeader}\ntrust_level = "trusted"\n`), true);
});

test("Codex trust preserves semantically equivalent TOML it cannot edit losslessly", (t) => {
  const project = setupProject(t);
  const sandbox = path.dirname(project.root);
  const codexHome = path.join(sandbox, "semantic-toml-codex-home");
  const fakeCodex = path.join(sandbox, "semantic-toml-fake-codex");
  const configPath = path.join(codexHome, "config.toml");
  fs.mkdirSync(codexHome, { recursive: true });
  fs.copyFileSync(path.join(KIT_ROOT, "tests", "fixtures", "fake_codex_app_server.py"), fakeCodex);
  fs.chmodSync(fakeCodex, 0o755);
  const canonicalRoot = fs.realpathSync.native(project.root);
  const basicRoot = canonicalRoot.replaceAll("\\", "\\\\").replaceAll("\"", "\\\"");
  assert.equal(canonicalRoot.includes("'"), false);
  const scenarios = [
    `[projects.'${canonicalRoot}']\n`,
    `[ projects . "${basicRoot}" ]\n`,
    `[projects."${basicRoot}"]\n'trust_level' = "untrusted"\n`,
  ];
  for (const original of scenarios) {
    const before = parseTomlDocument(original);
    assert.equal(typeof before.projects[canonicalRoot], "object");
    fs.writeFileSync(configPath, original, "utf8");
    const install = run(
      ["install", "--profile", "node"],
      project.root,
      project.home,
      {
        AGENT_FLOW_SKIP_CODEX_TRUST: "0",
        CODEX_CLI_PATH: fakeCodex,
        CODEX_HOME: codexHome,
      },
    );
    assert.notEqual(install.status, 0, install.stderr || install.stdout);
    assert.match(install.stderr, /cannot be edited losslessly/);
    const afterText = fs.readFileSync(configPath, "utf8");
    assert.equal(afterText, original);
    const after = parseTomlDocument(afterText);
    assert.deepEqual(after, before);
    assert.equal(Object.keys(after.projects).filter((key) => key === canonicalRoot).length, 1);
  }
});

test("Codex trust rejects a matching table header hidden in a multiline string", (t) => {
  const project = setupProject(t);
  const sandbox = path.dirname(project.root);
  const codexHome = path.join(sandbox, "toml-decoy-codex-home");
  const fakeCodex = path.join(sandbox, "toml-decoy-fake-codex");
  const configPath = path.join(codexHome, "config.toml");
  fs.mkdirSync(codexHome, { recursive: true });
  fs.copyFileSync(path.join(KIT_ROOT, "tests", "fixtures", "fake_codex_app_server.py"), fakeCodex);
  fs.chmodSync(fakeCodex, 0o755);
  const canonicalRoot = fs.realpathSync.native(project.root);
  assert.equal(canonicalRoot.includes("'"), false);
  const basicRoot = canonicalRoot.replaceAll("\\", "\\\\").replaceAll("\"", "\\\"");
  const original = [
    "note = \"\"\"",
    `[projects."${basicRoot}"]`,
    "decoy = \"text\"",
    "\"\"\"",
    `[projects.'${canonicalRoot}']`,
    "",
  ].join("\n");
  const before = parseTomlDocument(original);
  assert.equal(before.projects[canonicalRoot].trust_level, undefined);
  fs.writeFileSync(configPath, original, "utf8");

  const install = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    {
      AGENT_FLOW_SKIP_CODEX_TRUST: "0",
      CODEX_CLI_PATH: fakeCodex,
      CODEX_HOME: codexHome,
    },
  );
  assert.notEqual(install.status, 0, install.stderr || install.stdout);
  assert.match(install.stderr, /rendered Codex config does not satisfy semantic trust targets/);
  const afterText = fs.readFileSync(configPath, "utf8");
  assert.equal(afterText, original);
  const after = parseTomlDocument(afterText);
  assert.deepEqual(after, before);
  assert.equal(after.projects[canonicalRoot].trust_level, undefined);
});

test("Codex trust semantic no-op does not rewrite a multiline decoy", (t) => {
  const project = setupProject(t);
  const sandbox = path.dirname(project.root);
  const codexHome = path.join(sandbox, "toml-noop-decoy-codex-home");
  const fakeCodex = path.join(sandbox, "toml-noop-decoy-fake-codex");
  const configPath = path.join(codexHome, "config.toml");
  fs.mkdirSync(codexHome, { recursive: true });
  fs.copyFileSync(path.join(KIT_ROOT, "tests", "fixtures", "fake_codex_app_server.py"), fakeCodex);
  fs.chmodSync(fakeCodex, 0o755);
  const env = {
    AGENT_FLOW_SKIP_CODEX_TRUST: "0",
    CODEX_CLI_PATH: fakeCodex,
    CODEX_HOME: codexHome,
  };
  let install = run(["install", "--profile", "node"], project.root, project.home, env);
  assert.equal(install.status, 0, install.stderr || install.stdout);
  const canonicalRoot = fs.realpathSync.native(project.root);
  const basicRoot = canonicalRoot.replaceAll("\\", "\\\\").replaceAll("\"", "\\\"");
  const trustedConfig = fs.readFileSync(configPath, "utf8");
  const original = [
    "note = \"\"\"",
    `[projects."${basicRoot}"]`,
    'trust_level = "untrusted"',
    "\"\"\"",
    trustedConfig,
  ].join("\n");
  const before = parseTomlDocument(original);
  assert.equal(before.projects[canonicalRoot].trust_level, "trusted");
  assert.match(before.note, /trust_level = "untrusted"/);
  fs.writeFileSync(configPath, original, "utf8");

  install = run(["install", "--profile", "node"], project.root, project.home, env);
  assert.equal(install.status, 0, install.stderr || install.stdout);
  const afterText = fs.readFileSync(configPath, "utf8");
  assert.equal(afterText, original);
  assert.deepEqual(parseTomlDocument(afterText), before);
});

test("Python 3.10 TOML fallback is declared and missing parser preserves Codex config", (t) => {
  assert.match(
    fs.readFileSync(path.join(KIT_ROOT, "pyproject.toml"), "utf8"),
    /tomli>=2\.0; python_version < '3\.11'/,
  );
  const project = setupProject(t);
  const sandbox = path.dirname(project.root);
  const codexHome = path.join(sandbox, "missing-toml-parser-codex-home");
  const fakeCodex = path.join(sandbox, "missing-toml-parser-fake-codex");
  const noTomlPython = path.join(sandbox, "python-without-toml");
  const configPath = path.join(codexHome, "config.toml");
  fs.mkdirSync(codexHome, { recursive: true });
  fs.copyFileSync(path.join(KIT_ROOT, "tests", "fixtures", "fake_codex_app_server.py"), fakeCodex);
  fs.chmodSync(fakeCodex, 0o755);
  fs.writeFileSync(noTomlPython, "#!/bin/sh\nexit 1\n", "utf8");
  fs.chmodSync(noTomlPython, 0o755);
  const original = Buffer.from("existing = \"config\"\n", "utf8");
  fs.writeFileSync(configPath, original);

  const install = run(
    ["install", "--profile", "node"],
    project.root,
    project.home,
    {
      AGENT_FLOW_SKIP_CODEX_TRUST: "0",
      AGENT_FLOW_TEST_CODEX_TOML_PYTHON: noTomlPython,
      CODEX_CLI_PATH: fakeCodex,
      CODEX_HOME: codexHome,
    },
  );
  assert.notEqual(install.status, 0, install.stderr || install.stdout);
  assert.match(install.stderr, /no Python TOML parser is available/);
  assert.deepEqual(fs.readFileSync(configPath), original);
});

test("host detection uses one deterministic precedence across supported markers", () => {
  assert.equal(detectActiveHost({ AGENT_FLOW_HOST: " CoDeX ", PI_CODING_AGENT_DIR: "/tmp/omp" }), "codex");
  assert.equal(detectActiveHost({ AGENT_FLOW_HOST: "invalid", PI_CODING_AGENT_DIR: "/tmp/omp" }), "omp");
  assert.equal(detectActiveHost({ CLAUDECODE: "1", PI_CODING_AGENT_DIR: "/tmp/omp" }), "claude");
  assert.equal(detectActiveHost({ CODEX_THREAD_ID: "thread", PI_CODING_AGENT_DIR: "/tmp/omp" }), "codex");
  assert.equal(detectActiveHost({ OMP_PROFILE: "child", CODEX_HOME: "/tmp/codex" }), "omp");
  assert.equal(detectActiveHost({ PI_CODING_AGENT_DIR: "/tmp/omp" }), "omp");
  assert.equal(detectActiveHost({ CLAUDE_CLI: "1", CODEX_THREAD_ID: "thread" }), "claude");
  for (const marker of [
    "CODEX_CLI",
    "CODEX_HOME",
    "CODEX_SHELL",
    "CODEX_THREAD_ID",
    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
  ]) {
    assert.equal(detectActiveHost({ [marker]: "1" }), "codex", marker);
  }
  assert.equal(detectActiveHost({}), null);
});

test("installer treats PI as stronger than inherited weak CODEX_HOME", (t) => {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-host-precedence-"));
  const root = path.join(sandbox, "project");
  const home = path.join(sandbox, "home");
  const ompConfig = path.join(sandbox, "custom-omp-config");
  fs.mkdirSync(root, { recursive: true });
  fs.mkdirSync(home, { recursive: true });
  t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));
  git(root, ["init", "-b", "main"]);
  git(root, ["config", "user.email", "test@example.com"]);
  git(root, ["config", "user.name", "Test User"]);
  fs.writeFileSync(path.join(root, "README.md"), "fixture\n", "utf8");
  git(root, ["add", "README.md"]);
  git(root, ["commit", "-m", "init"]);
  writeSkill(path.join(home, ".codex", "skills"), "host-probe", "codex source");
  writeSkill(path.join(home, ".claude", "skills"), "host-probe", "claude source");
  writeSkill(path.join(ompConfig, "skills"), "host-probe", "omp source");

  const result = run(
    ["install", "--skill", "host-probe"],
    root,
    home,
    {
      PI_CODING_AGENT_DIR: ompConfig,
      CODEX_HOME: path.join(home, ".codex"),
    },
  );
  assert.equal(result.status, 0, result.stderr || result.stdout);
  const index = JSON.parse(
    fs.readFileSync(path.join(root, ".agent-flow", "skills", "index.json"), "utf8"),
  );
  const installed = index.skills.find((skill) => skill.name === "host-probe");
  assert.equal(installed.source, "host-bootstrap");
  assert.equal(installed.source_host, "omp");
  assert.match(
    fs.readFileSync(path.join(root, ".agent-flow", "skills", "host-probe", "SKILL.md"), "utf8"),
    /omp source/,
  );
});

test("installer resolves active Codex skills from explicit CODEX_HOME", (t) => {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-custom-codex-home-"));
  const root = path.join(sandbox, "project");
  const home = path.join(sandbox, "home");
  const codexHome = path.join(sandbox, "custom-codex-home");
  fs.mkdirSync(root, { recursive: true });
  fs.mkdirSync(home, { recursive: true });
  t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));
  git(root, ["init", "-b", "main"]);
  git(root, ["config", "user.email", "test@example.com"]);
  git(root, ["config", "user.name", "Test User"]);
  fs.writeFileSync(path.join(root, "README.md"), "fixture\n", "utf8");
  git(root, ["add", "README.md"]);
  git(root, ["commit", "-m", "init"]);
  writeSkill(path.join(codexHome, "skills"), "custom-home-probe", "custom codex source");

  const result = run(
    ["install", "--skill", "custom-home-probe"],
    root,
    home,
    { CODEX_HOME: codexHome },
  );

  assert.equal(result.status, 0, result.stderr || result.stdout);
  const installed = fs.readFileSync(
    path.join(root, ".agent-flow", "skills", "custom-home-probe", "SKILL.md"),
    "utf8",
  );
  assert.match(installed, /custom codex source/);
  const index = JSON.parse(
    fs.readFileSync(path.join(root, ".agent-flow", "skills", "index.json"), "utf8"),
  );
  const skill = index.skills.find((candidate) => candidate.name === "custom-home-probe");
  assert.equal(skill.source, "host-bootstrap");
  assert.equal(skill.source_host, "codex");
});

test("explicit external skills expose to every host and route in Node and Python prompts", (t) => {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-external-routing-"));
  const root = path.join(sandbox, "project");
  const home = path.join(sandbox, "home");
  const codexHome = path.join(sandbox, "codex-home");
  const externalRoot = path.join(codexHome, "skills");
  fs.mkdirSync(root, { recursive: true });
  fs.mkdirSync(home, { recursive: true });
  t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));
  git(root, ["init", "-b", "main"]);
  git(root, ["config", "user.email", "test@example.com"]);
  git(root, ["config", "user.name", "Test User"]);
  fs.writeFileSync(path.join(root, "README.md"), "fixture\n", "utf8");
  git(root, ["add", "README.md"]);
  git(root, ["commit", "-m", "init"]);

  writeSkill(
    externalRoot,
    "external-always",
    "always body",
    "activation: always\ndependencies: [external-dependency]\nrequires: [external-required]",
  );
  writeSkill(externalRoot, "external-dependency", "dependency body", "activation: always");
  writeSkill(externalRoot, "external-required", "required body", "activation: always");
  writeSkill(
    externalRoot,
    "external-figma",
    "figma body",
    "activation: conditional\nworkflowPhases: [design]\ntaskTerms: [figma handoff]\npathGlobs: []",
  );
  writeSkill(
    externalRoot,
    "external-path",
    "path body",
    "activation: conditional\nworkflowPhases: [implement]\ntaskTerms: []\npathGlobs: [src/**/*.tsx]",
  );
  writeSkill(externalRoot, "external-on-demand", "on demand body");
  writeSkill(externalRoot, "external-unselected", "must stay undiscovered", "activation: always");
  const selectedNames = [
    "external-always",
    "external-figma",
    "external-on-demand",
    "external-path",
  ];
  const exposedNames = [
    ...selectedNames,
    "external-dependency",
    "external-required",
    "external-unselected",
  ].sort();

  const install = run(
    ["install", "--profile", "node", "--skill", selectedNames.join(",")],
    root,
    home,
    { AGENT_FLOW_HOST: "codex", CODEX_HOME: codexHome },
  );
  assert.equal(install.status, 0, install.stderr || install.stdout);

  const index = JSON.parse(
    fs.readFileSync(path.join(root, ".agent-flow", "skills", "index.json"), "utf8"),
  );
  assert.deepEqual(index.selection.explicit_skills, selectedNames);
  assert.deepEqual(index.selection.external_exposure_skills, exposedNames);
  for (const name of exposedNames) {
    const snapshot = path.join(root, ".agent-flow", "skills", name);
    assert.equal(fs.existsSync(path.join(snapshot, "SKILL.md")), true, name);
    const links = index.links.filter((link) => link.name === name);
    assert.deepEqual(links.map((link) => link.host).sort(), ["claude", "codex", "omp"], name);
    for (const hostRoot of [".agents/skills", ".claude/skills", ".omp/skills"]) {
      const exposed = path.join(root, hostRoot, name);
      assert.equal(fs.existsSync(path.join(exposed, "SKILL.md")), true, `${hostRoot}/${name}`);
      const exposedTree = fs.lstatSync(exposed).isSymbolicLink() ? fs.realpathSync(exposed) : exposed;
      assert.equal(hashSkillTree(exposedTree), hashSkillTree(snapshot), `${hostRoot}/${name}`);
    }
  }
  const automaticUnselected = index.skills.find((skill) => skill.name === "external-unselected");
  assert.equal(automaticUnselected.activation, "on-demand");

  const project = { root, home };
  const active = startRun(project, { task: "prepare FIGMA HANDOFF", runId: "external-routing" });
  const designPrompt = run(["run", "next"], active.state.workspace_root, home);
  assert.equal(designPrompt.status, 0, designPrompt.stderr || designPrompt.stdout);
  assert.match(designPrompt.stdout, /external-figma\/SKILL\.md/);
  assert.doesNotMatch(designPrompt.stdout, /external-always\/SKILL\.md/);
  assert.doesNotMatch(designPrompt.stdout, /external-on-demand\/SKILL\.md/);
  assert.doesNotMatch(designPrompt.stdout, /external-unselected\/SKILL\.md/);

  const changed = path.join(active.state.workspace_root, "src", "ui", "Card.tsx");
  fs.mkdirSync(path.dirname(changed), { recursive: true });
  fs.writeFileSync(changed, "changed\n", "utf8");
  updateRunState(active, "implement", 3, { task: "unrelated implementation" });
  const implementPrompt = run(["run", "next"], active.state.workspace_root, home);
  assert.equal(implementPrompt.status, 0, implementPrompt.stderr || implementPrompt.stdout);
  assert.match(implementPrompt.stdout, /external-always\/SKILL\.md/);
  assert.match(implementPrompt.stdout, /external-dependency\/SKILL\.md/);
  assert.match(implementPrompt.stdout, /external-required\/SKILL\.md/);
  assert.match(implementPrompt.stdout, /external-path\/SKILL\.md/);
  assert.doesNotMatch(implementPrompt.stdout, /external-figma\/SKILL\.md/);
  assert.doesNotMatch(implementPrompt.stdout, /external-on-demand\/SKILL\.md/);

  const projectPython = path.join(KIT_ROOT, ".venv", "bin", "python");
  const pythonExecutable = process.env.PYTHON || (fs.existsSync(projectPython) ? projectPython : "python3");
  const pythonPrompt = (phase, task, changedFiles) => {
    const result = spawnSync(
      pythonExecutable,
      [
        "-c",
        [
          "import json, os",
          "from pathlib import Path",
          "from agent_flow.core.local_skills import local_skill_prompt_block",
          "print(local_skill_prompt_block(Path(os.environ['PROJECT_ROOT']), os.environ['PHASE'], os.environ['TASK'], json.loads(os.environ['CHANGED_FILES'])))",
        ].join(";"),
      ],
      {
        cwd: root,
        encoding: "utf8",
        env: cleanChildEnv(home, {
          PROJECT_ROOT: root,
          PHASE: phase,
          TASK: task,
          CHANGED_FILES: JSON.stringify(changedFiles),
        }),
      },
    );
    assert.equal(result.status, 0, result.stderr || result.stdout);
    return result.stdout;
  };
  const pythonDesign = pythonPrompt("design", "prepare FIGMA HANDOFF", []);
  assert.match(pythonDesign, /external-figma\/SKILL\.md/);
  assert.doesNotMatch(pythonDesign, /external-always\/SKILL\.md/);
  assert.doesNotMatch(pythonDesign, /external-on-demand\/SKILL\.md/);
  const pythonImplement = pythonPrompt("implement", "unrelated implementation", ["src/ui/Card.tsx"]);
  assert.match(pythonImplement, /external-always\/SKILL\.md/);
  assert.match(pythonImplement, /external-dependency\/SKILL\.md/);
  assert.match(pythonImplement, /external-required\/SKILL\.md/);
  assert.match(pythonImplement, /external-path\/SKILL\.md/);
  assert.doesNotMatch(pythonImplement, /external-figma\/SKILL\.md/);
  assert.doesNotMatch(pythonImplement, /external-on-demand\/SKILL\.md/);
  const pythonPlanHash = spawnSync(
    pythonExecutable,
    [
      "-c",
      "import os; from pathlib import Path; from agent_flow.core.local_skills import project_local_skill_plan_hash; print(project_local_skill_plan_hash(Path(os.environ['PROJECT_ROOT'])))",
    ],
    {
      cwd: root,
      encoding: "utf8",
      env: cleanChildEnv(home, { PROJECT_ROOT: root }),
    },
  );
  assert.equal(pythonPlanHash.status, 0, pythonPlanHash.stderr || pythonPlanHash.stdout);
  assert.equal(pythonPlanHash.stdout.trim(), active.state.local_skill_plan_hash);
  const pythonInstalledPin = spawnSync(
    pythonExecutable,
    [
      "-c",
      "import json, os; from pathlib import Path; from agent_flow.core.skill_plan import installed_skill_plan_pin; print(json.dumps(installed_skill_plan_pin(Path(os.environ['PROJECT_ROOT']))))",
    ],
    {
      cwd: root,
      encoding: "utf8",
      env: cleanChildEnv(home, { PROJECT_ROOT: root }),
    },
  );
  assert.equal(pythonInstalledPin.status, 0, pythonInstalledPin.stderr || pythonInstalledPin.stdout);
  const installedPin = JSON.parse(pythonInstalledPin.stdout);
  const kit = JSON.parse(fs.readFileSync(path.join(root, ".agent-flow", "kit.json"), "utf8"));
  assert.equal(installedPin.skill_plan_hash, kit.skill_plan_hash);
  assert.equal(installedPin.local_skill_plan_hash, active.state.local_skill_plan_hash);
});

test("project skill dependencies resolved from an inactive host expose and route on every host", (t) => {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-project-external-dependency-"));
  const root = path.join(sandbox, "project");
  const home = path.join(sandbox, "home");
  const codexHome = path.join(sandbox, "codex-home");
  const claudeHome = path.join(sandbox, "claude-home");
  fs.mkdirSync(root, { recursive: true });
  fs.mkdirSync(home, { recursive: true });
  fs.mkdirSync(codexHome, { recursive: true });
  t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));
  git(root, ["init", "-b", "main"]);
  git(root, ["config", "user.email", "test@example.com"]);
  git(root, ["config", "user.name", "Test User"]);
  fs.writeFileSync(path.join(root, "README.md"), "fixture\n", "utf8");
  git(root, ["add", "README.md"]);
  git(root, ["commit", "-m", "init"]);
  writeSkill(
    path.join(root, "skills"),
    "project-consumer",
    "project consumer body",
    "activation: always\ndependencies: [inactive-host-dependency]",
  );
  writeSkill(
    path.join(claudeHome, "skills"),
    "inactive-host-dependency",
    "inactive host dependency body",
    "activation: always",
  );

  const install = run(
    ["install", "--profile", "node"],
    root,
    home,
    {
      AGENT_FLOW_HOST: "codex",
      CODEX_HOME: codexHome,
      CLAUDE_CONFIG_DIR: claudeHome,
    },
  );
  assert.equal(install.status, 0, install.stderr || install.stdout);
  const index = JSON.parse(fs.readFileSync(path.join(root, ".agent-flow", "skills", "index.json"), "utf8"));
  assert.deepEqual(index.selection.external_exposure_skills, ["inactive-host-dependency"]);
  for (const name of ["project-consumer", "inactive-host-dependency"]) {
    assert.deepEqual(
      index.links.filter((link) => link.name === name).map((link) => link.host).sort(),
      ["claude", "codex", "omp"],
      name,
    );
  }

  const project = { root, home };
  const active = startRun(project, { task: "implement the project consumer", runId: "project-external-dependency" });
  updateRunState(active, "implement", 3);
  for (const name of ["project-consumer", "inactive-host-dependency"]) {
    for (const hostRoot of [".agents", ".claude", ".omp"]) {
      assert.equal(
        fs.existsSync(path.join(active.state.workspace_root, hostRoot, "skills", name, "SKILL.md")),
        true,
        `${hostRoot}:${name}`,
      );
    }
  }
  const nodePrompt = run(["run", "next"], active.state.workspace_root, home);
  assert.equal(nodePrompt.status, 0, nodePrompt.stderr || nodePrompt.stdout);
  assert.match(nodePrompt.stdout, /project-consumer\/SKILL\.md/);
  assert.match(nodePrompt.stdout, /inactive-host-dependency\/SKILL\.md/);

  const projectPython = path.join(KIT_ROOT, ".venv", "bin", "python");
  const pythonExecutable = process.env.PYTHON || (fs.existsSync(projectPython) ? projectPython : "python3");
  const pythonPrompt = spawnSync(
    pythonExecutable,
    [
      "-c",
      "import os; from pathlib import Path; from agent_flow.core.local_skills import local_skill_prompt_block; print(local_skill_prompt_block(Path(os.environ['PROJECT_ROOT']), 'implement', 'implement the project consumer', []))",
    ],
    {
      cwd: root,
      encoding: "utf8",
      env: cleanChildEnv(home, { PROJECT_ROOT: root }),
    },
  );
  assert.equal(pythonPrompt.status, 0, pythonPrompt.stderr || pythonPrompt.stdout);
  assert.match(pythonPrompt.stdout, /project-consumer\/SKILL\.md/);
  assert.match(pythonPrompt.stdout, /inactive-host-dependency\/SKILL\.md/);
});

test("active-host catalog adopts metadata-less skills on demand and pins add change delete dependency state", (t) => {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-automatic-external-"));
  const root = path.join(sandbox, "project");
  const home = path.join(sandbox, "home");
  const codexHome = path.join(sandbox, "codex-home");
  const externalRoot = path.join(codexHome, "skills");
  fs.mkdirSync(root, { recursive: true });
  fs.mkdirSync(home, { recursive: true });
  t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));
  git(root, ["init", "-b", "main"]);
  git(root, ["config", "user.email", "test@example.com"]);
  git(root, ["config", "user.name", "Test User"]);
  fs.writeFileSync(path.join(root, "README.md"), "fixture\n", "utf8");
  git(root, ["add", "README.md"]);
  git(root, ["commit", "-m", "init"]);
  writeSkill(
    externalRoot,
    "automatic-parent",
    "automatic parent body",
    "dependencies: [automatic-dependency]",
  );
  writeSkill(externalRoot, "automatic-dependency", "automatic dependency body");
  writeSkill(
    externalRoot,
    "ignored-overlong",
    Array.from({ length: 201 }, (_, index) => `line ${index}`).join("\n"),
  );
  const outsideSkill = path.join(sandbox, "outside-skill");
  writeSkill(sandbox, "outside-skill", "outside skill body");
  fs.symlinkSync(outsideSkill, path.join(externalRoot, "ignored-symlink"));
  const env = { AGENT_FLOW_HOST: "codex", CODEX_HOME: codexHome };

  const install = run(["install", "--profile", "node"], root, home, env);
  assert.equal(install.status, 0, install.stderr || install.stdout);
  const indexPath = path.join(root, ".agent-flow", "skills", "index.json");
  const installedIndexBytes = fs.readFileSync(indexPath);
  const index = JSON.parse(installedIndexBytes);
  assert.deepEqual(
    index.selection.external_exposure_skills,
    ["automatic-dependency", "automatic-parent"],
  );
  assert.equal(index.skills.some((skill) => skill.name === "ignored-overlong"), false);
  assert.equal(index.skills.some((skill) => skill.name === "ignored-symlink"), false);
  for (const name of ["automatic-parent", "automatic-dependency"]) {
    const skill = index.skills.find((candidate) => candidate.name === name);
    assert.equal(skill.source, "host-bootstrap", name);
    assert.equal(skill.source_host, "codex", name);
    assert.equal(skill.activation, "on-demand", name);
    assert.deepEqual(
      index.links.filter((link) => link.name === name).map((link) => link.host).sort(),
      ["claude", "codex", "omp"],
      name,
    );
  }

  const dependencySource = path.join(externalRoot, "automatic-dependency", "SKILL.md");
  const dependencyBytes = fs.readFileSync(dependencySource);
  fs.appendFileSync(dependencySource, "changed\n", "utf8");
  const changed = run(["install", "--profile", "node"], root, home, env);
  assert.notEqual(changed.status, 0);
  assert.match(changed.stderr, /pinned external skill source changed: automatic-dependency/);
  assert.equal(fs.readFileSync(indexPath).equals(installedIndexBytes), true);
  assert.equal(fs.existsSync(path.join(root, ".agent-flow", "install-transaction")), false);

  fs.writeFileSync(dependencySource, dependencyBytes);
  fs.rmSync(path.join(externalRoot, "automatic-parent"), { recursive: true });
  const removed = run(["install", "--profile", "node"], root, home, env);
  assert.notEqual(removed.status, 0);
  assert.match(removed.stderr, /pinned external skill source is unavailable: automatic-parent/);
  assert.equal(fs.readFileSync(indexPath).equals(installedIndexBytes), true);
  assert.equal(fs.existsSync(path.join(root, ".agent-flow", "install-transaction")), false);

  const project = { root, home };
  const active = startRun(project, { task: "unrelated implementation", runId: "automatic-external-on-demand" });
  updateRunState(active, "implement", 3);
  for (const name of ["automatic-parent", "automatic-dependency"]) {
    for (const hostRoot of [".agents", ".claude", ".omp"]) {
      assert.equal(
        fs.existsSync(path.join(active.state.workspace_root, hostRoot, "skills", name, "SKILL.md")),
        true,
        `${hostRoot}:${name}`,
      );
    }
  }
  const nodePrompt = run(["run", "next"], active.state.workspace_root, home);
  assert.equal(nodePrompt.status, 0, nodePrompt.stderr || nodePrompt.stdout);
  assert.doesNotMatch(nodePrompt.stdout, /automatic-parent\/SKILL\.md/);
  assert.doesNotMatch(nodePrompt.stdout, /automatic-dependency\/SKILL\.md/);

  const projectPython = path.join(KIT_ROOT, ".venv", "bin", "python");
  const pythonExecutable = process.env.PYTHON || (fs.existsSync(projectPython) ? projectPython : "python3");
  const pythonPrompt = spawnSync(
    pythonExecutable,
    [
      "-c",
      "import os; from pathlib import Path; from agent_flow.core.local_skills import local_skill_prompt_block; print(local_skill_prompt_block(Path(os.environ['PROJECT_ROOT']), 'implement', 'unrelated implementation', []))",
    ],
    {
      cwd: root,
      encoding: "utf8",
      env: cleanChildEnv(home, { PROJECT_ROOT: root }),
    },
  );
  assert.equal(pythonPrompt.status, 0, pythonPrompt.stderr || pythonPrompt.stdout);
  assert.doesNotMatch(pythonPrompt.stdout, /automatic-parent\/SKILL\.md/);
  assert.doesNotMatch(pythonPrompt.stdout, /automatic-dependency\/SKILL\.md/);
});

test("explicit external skill host metadata cannot narrow three-host exposure", (t) => {
  for (const [label, hosts, dependency] of [
    ["subset", "[codex]", false],
    ["unknown", "[future-host]", false],
    ["dependency-subset", "[codex]", true],
  ]) {
    const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), `agent-flow-external-hosts-${label}-`));
    const root = path.join(sandbox, "project");
    const home = path.join(sandbox, "home");
    const codexHome = path.join(sandbox, "codex-home");
    fs.mkdirSync(root, { recursive: true });
    fs.mkdirSync(home, { recursive: true });
    t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));
    git(root, ["init", "-b", "main"]);
    git(root, ["config", "user.email", "test@example.com"]);
    git(root, ["config", "user.name", "Test User"]);
    fs.writeFileSync(path.join(root, "README.md"), "fixture\n", "utf8");
    git(root, ["add", "README.md"]);
    git(root, ["commit", "-m", "init"]);
    const externalRoot = path.join(codexHome, "skills");
    const selectedName = dependency ? `external-${label}-parent` : `external-${label}`;
    const constrainedName = dependency ? `external-${label}-child` : selectedName;
    if (dependency) {
      writeSkill(
        externalRoot,
        selectedName,
        `${label} parent policy`,
        `dependencies: [${constrainedName}]`,
      );
    }
    writeSkill(externalRoot, constrainedName, `${label} host policy`, `hosts: ${hosts}`);

    const result = run(
      ["install", "--profile", "node", "--skill", selectedName],
      root,
      home,
      { AGENT_FLOW_HOST: "codex", CODEX_HOME: codexHome },
    );

    assert.equal(result.status, 0, result.stderr || result.stdout);
    const index = JSON.parse(fs.readFileSync(path.join(root, ".agent-flow", "skills", "index.json"), "utf8"));
    const names = dependency ? [selectedName, constrainedName] : [selectedName];
    for (const name of names) {
      const skill = index.skills.find((candidate) => candidate.name === name);
      assert.deepEqual(skill.hosts, ["claude", "codex", "omp"], `${label}:${name}`);
      for (const host of [".claude", ".agents", ".omp"]) {
        assert.equal(fs.existsSync(path.join(root, host, "skills", name, "SKILL.md")), true, `${label}:${host}:${name}`);
      }
    }
    assert.equal(fs.existsSync(path.join(root, ".agent-flow", "install-transaction")), false, label);
  }
});

test("Python lockfile-only projects use the Python auto profile", (t) => {
  for (const marker of ["requirements-dev.txt", "Pipfile", "poetry.lock", "uv.lock"]) {
    const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-python-marker-"));
    const root = path.join(sandbox, "project");
    const home = path.join(sandbox, "home");
    fs.mkdirSync(root, { recursive: true });
    fs.mkdirSync(home, { recursive: true });
    t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));
    fs.writeFileSync(path.join(root, marker), "\n", "utf8");

    const result = run(["install"], root, home);
    assert.equal(result.status, 0, result.stderr || result.stdout);
    const kit = JSON.parse(fs.readFileSync(path.join(root, ".agent-flow", "kit.json"), "utf8"));
    assert.equal(kit.primary_profile, "python", marker);
    assert.deepEqual(kit.profiles, ["python"], marker);
  }
});

test("runtime preflight authenticates external skill destinations for every host", (t) => {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-external-runtime-links-"));
  const root = path.join(sandbox, "project");
  const home = path.join(sandbox, "home");
  const codexHome = path.join(sandbox, "codex-home");
  const externalRoot = path.join(codexHome, "skills");
  fs.mkdirSync(root, { recursive: true });
  fs.mkdirSync(home, { recursive: true });
  t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));
  git(root, ["init", "-b", "main"]);
  git(root, ["config", "user.email", "test@example.com"]);
  git(root, ["config", "user.name", "Test User"]);
  fs.writeFileSync(path.join(root, "README.md"), "fixture\n", "utf8");
  git(root, ["add", "README.md"]);
  git(root, ["commit", "-m", "init"]);
  writeSkill(externalRoot, "external-guard", "guard source");
  writeSkill(externalRoot, "external-retarget", "retarget source");

  const install = run(
    ["install", "--profile", "node", "--skill", "external-guard,external-retarget"],
    root,
    home,
    { AGENT_FLOW_HOST: "codex", CODEX_HOME: codexHome },
  );
  assert.equal(install.status, 0, install.stderr || install.stdout);
  const active = startRun({ root, home }, { runId: "external-runtime-links" });
  const indexPath = path.join(root, ".agent-flow", "skills", "index.json");
  const kitPath = path.join(root, ".agent-flow", "kit.json");
  const index = JSON.parse(fs.readFileSync(indexPath, "utf8"));
  const kit = JSON.parse(fs.readFileSync(kitPath, "utf8"));
  const guardSkill = index.skills.find((skill) => skill.name === "external-guard");
  const retargetSkill = index.skills.find((skill) => skill.name === "external-retarget");
  assert(guardSkill?.path);
  assert(retargetSkill?.path);
  const guardSource = path.dirname(path.resolve(root, guardSkill.path));
  const retargetSource = path.dirname(path.resolve(root, retargetSkill.path));
  const guardLinks = index.links.filter((link) => link.name === "external-guard");
  assert.deepEqual(guardLinks.map((link) => link.host).sort(), ["claude", "codex", "omp"]);

  const projectPython = path.join(KIT_ROOT, ".venv", "bin", "python");
  const pythonExecutable = process.env.PYTHON || (fs.existsSync(projectPython) ? projectPython : "python3");
  const pythonPin = () => spawnSync(
    pythonExecutable,
    [
      "-c",
      "import os; from pathlib import Path; from agent_flow.core.skill_plan import installed_skill_plan_pin; installed_skill_plan_pin(Path(os.environ['PROJECT_ROOT']))",
    ],
    {
      cwd: root,
      encoding: "utf8",
      env: cleanChildEnv(home, { PROJECT_ROOT: root }),
    },
  );
  const assertRuntimePasses = (label) => {
    const nodeStatus = run(["status"], active.state.workspace_root, home);
    assert.equal(nodeStatus.status, 0, `${label}: ${nodeStatus.stderr || nodeStatus.stdout}`);
    const pythonStatus = pythonPin();
    assert.equal(pythonStatus.status, 0, `${label}: ${pythonStatus.stderr || pythonStatus.stdout}`);
  };
  const assertRuntimeRejects = (label, { prompt = false } = {}) => {
    const nodeStatus = run(["status"], active.state.workspace_root, home);
    assert.notEqual(nodeStatus.status, 0, `${label}: Node status`);
    assert.match(nodeStatus.stderr, /committed skill (symlink|copy) is not applied/, label);
    const pythonStatus = pythonPin();
    assert.notEqual(pythonStatus.status, 0, `${label}: Python pin`);
    assert.match(pythonStatus.stderr, /committed skill (symlink|copy) is not applied/, label);
    if (prompt) {
      const nodePrompt = run(["run", "next"], active.state.workspace_root, home);
      assert.notEqual(nodePrompt.status, 0, `${label}: Node prompt`);
      assert.match(nodePrompt.stderr, /committed skill symlink is not applied/, label);
    }
  };

  assertRuntimePasses("baseline");
  for (const link of guardLinks) {
    const destination = path.resolve(root, link.path);
    const canonicalTarget = fs.readlinkSync(destination);
    fs.rmSync(destination);
    assertRuntimeRejects(`${link.host} deletion`, { prompt: link.host === "codex" });
    fs.symlinkSync(canonicalTarget, destination);

    fs.rmSync(destination);
    fs.symlinkSync(path.relative(path.dirname(destination), retargetSource), destination);
    assertRuntimeRejects(`${link.host} retarget`);
    fs.rmSync(destination);
    fs.symlinkSync(canonicalTarget, destination);
  }

  const copiedLink = guardLinks.find((link) => link.host === "omp");
  assert(copiedLink);
  const copiedDestination = path.resolve(root, copiedLink.path);
  fs.rmSync(copiedDestination);
  fs.cpSync(guardSource, copiedDestination, { recursive: true, preserveTimestamps: true });
  assert.equal(modeSensitiveTreeHash(copiedDestination), modeSensitiveTreeHash(guardSource));
  copiedLink.status = "copied";
  kit.skill_links_commitment = skillLinksCommitment(kit, index.links);
  writeJson(indexPath, index);
  writeJson(kitPath, kit);
  assertRuntimePasses("authenticated copied tree");

  const copiedSkillFile = path.join(copiedDestination, "SKILL.md");
  const originalBytes = fs.readFileSync(copiedSkillFile);
  const originalMode = fs.statSync(copiedSkillFile).mode & 0o777;
  fs.appendFileSync(copiedSkillFile, "tampered bytes\n", "utf8");
  assertRuntimeRejects("copied bytes");
  fs.writeFileSync(copiedSkillFile, originalBytes);
  fs.chmodSync(copiedSkillFile, originalMode);
  assertRuntimePasses("restored copied bytes");

  fs.chmodSync(copiedSkillFile, originalMode ^ 0o100);
  assertRuntimeRejects("copied mode");
});

test("installer selects Codex host-bootstrap from a Codex Desktop marker without CODEX_HOME", (t) => {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-codex-desktop-"));
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
  writeSkill(path.join(home, ".codex", "skills"), "desktop-probe", "codex desktop source");

  const result = run(
    ["install", "--skill", "desktop-probe"],
    root,
    home,
    {
      CODEX_THREAD_ID: "desktop-thread",
      PI_CODING_AGENT_DIR: path.join(home, ".omp", "agent"),
    },
  );
  assert.equal(result.status, 0, result.stderr || result.stdout);
  const index = JSON.parse(
    fs.readFileSync(path.join(root, ".agent-flow", "skills", "index.json"), "utf8"),
  );
  const installed = index.skills.find((skill) => skill.name === "desktop-probe");
  assert.equal(installed.source, "host-bootstrap");
  assert.equal(installed.source_host, "codex");
  assert.match(
    fs.readFileSync(path.join(root, ".agent-flow", "skills", "desktop-probe", "SKILL.md"), "utf8"),
    /codex desktop source/,
  );
});

test("installer treats a Claude runtime marker as stronger than inherited PI", (t) => {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-claude-runtime-"));
  const root = path.join(sandbox, "project");
  const home = path.join(sandbox, "home");
  const claudeConfig = path.join(sandbox, "custom-claude-config");
  fs.mkdirSync(root, { recursive: true });
  fs.mkdirSync(home, { recursive: true });
  t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));
  git(root, ["init", "-b", "main"]);
  git(root, ["config", "user.email", "test@example.com"]);
  git(root, ["config", "user.name", "Test User"]);
  fs.writeFileSync(path.join(root, "README.md"), "fixture\n", "utf8");
  git(root, ["add", "README.md"]);
  git(root, ["commit", "-m", "init"]);
  writeSkill(path.join(claudeConfig, "skills"), "claude-probe", "claude runtime source");
  writeSkill(path.join(home, ".omp", "agent", "skills"), "claude-probe", "omp inherited source");

  const result = run(
    ["install", "--skill", "claude-probe"],
    root,
    home,
    {
      CLAUDECODE: "1",
      CLAUDE_CONFIG_DIR: claudeConfig,
      PI_CODING_AGENT_DIR: path.join(home, ".omp", "agent"),
    },
  );
  assert.equal(result.status, 0, result.stderr || result.stdout);
  const index = JSON.parse(
    fs.readFileSync(path.join(root, ".agent-flow", "skills", "index.json"), "utf8"),
  );
  const installed = index.skills.find((skill) => skill.name === "claude-probe");
  assert.equal(installed.source, "host-bootstrap");
  assert.equal(installed.source_host, "claude");
  assert.match(
    fs.readFileSync(path.join(root, ".agent-flow", "skills", "claude-probe", "SKILL.md"), "utf8"),
    /claude runtime source/,
  );
});

test("installer rejects pinned Node runtime symlinks without touching external targets", (t) => {
  for (const kind of ["leaf", "ancestor"]) {
    const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), `agent-flow-node-runtime-${kind}-`));
    const root = path.join(sandbox, "project");
    const home = path.join(sandbox, "home");
    const outside = path.join(sandbox, "outside");
    fs.mkdirSync(root, { recursive: true });
    fs.mkdirSync(home, { recursive: true });
    fs.mkdirSync(outside, { recursive: true });
    t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));
    const sentinel = path.join(outside, "sentinel.txt");
    fs.writeFileSync(sentinel, "unchanged\n", "utf8");

    if (kind === "leaf") {
      const bin = path.join(root, ".agent-flow", "runtime", "node", "bin");
      fs.mkdirSync(bin, { recursive: true });
      fs.symlinkSync(sentinel, path.join(bin, "agent-flow-kit.mjs"));
    } else {
      const runtime = path.join(root, ".agent-flow", "runtime");
      fs.mkdirSync(runtime, { recursive: true });
      fs.symlinkSync(outside, path.join(runtime, "node"));
    }

    const result = run(["install"], root, home);

    assert.notEqual(result.status, 0, kind);
    assert.match(result.stderr, /pinned Node runtime install target uses a symlink/, kind);
    assert.equal(fs.readFileSync(sentinel, "utf8"), "unchanged\n", kind);
  }
});

test("install prunes extraneous Python runtime entries before committing the contract", (t) => {
  const project = setupProject(t);
  const pythonRuntime = path.join(project.root, ".agent-flow", "runtime", "python");
  const rootStale = path.join(pythonRuntime, "sitecustomize.py");
  const nestedStale = path.join(pythonRuntime, "yaml", "retired_loader.py");
  const retiredPackage = path.join(pythonRuntime, "retired_package", "payload.py");
  fs.writeFileSync(rootStale, "import os; os._exit(77)\n", "utf8");
  fs.writeFileSync(nestedStale, "raise RuntimeError('stale')\n", "utf8");
  fs.mkdirSync(path.dirname(retiredPackage), { recursive: true });
  fs.writeFileSync(retiredPackage, "stale = True\n", "utf8");

  const reinstall = run(["install", "--profile", "node"], project.root, project.home);

  assert.equal(reinstall.status, 0, reinstall.stderr || reinstall.stdout);
  assert.equal(fs.existsSync(rootStale), false);
  assert.equal(fs.existsSync(nestedStale), false);
  assert.equal(fs.existsSync(path.dirname(retiredPackage)), false);
  assert.equal(fs.existsSync(path.join(pythonRuntime, "agent_flow", "__init__.py")), true);
  assert.equal(fs.existsSync(path.join(pythonRuntime, "yaml", "__init__.py")), true);
});

function git(cwd, args) {
  const result = spawnSync("git", args, { cwd, encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr || result.stdout);
}

function writeJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

function writeSkill(base, name, body = "Use this project policy.", routingMetadata = "") {
  const root = path.join(base, name);
  fs.mkdirSync(root, { recursive: true });
  fs.writeFileSync(
    path.join(root, "SKILL.md"),
    `---\nname: ${name}\ndescription: ${name} policy\n${routingMetadata ? `${routingMetadata.trim()}\n` : ""}---\n\n# ${name}\n\n${body}\n`,
    "utf8",
  );
}

function setupProject(t, { profile = "node", localSkills = [] } = {}) {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-node-contract-"));
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
  for (const skill of localSkills) {
    if (typeof skill === "string") {
      writeSkill(path.join(root, ".agent-flow", "local-skills"), skill);
    } else {
      writeSkill(
        path.join(root, ".agent-flow", "local-skills"),
        skill.name,
        skill.body,
        skill.routingMetadata,
      );
    }
  }
  const install = run(["install", "--profile", profile], root, home);
  assert.equal(install.status, 0, install.stderr || install.stdout);
  return { root, home };
}

function setupActiveCodexTrustFixture(project, label, { configBytes = null, mode = 0o600 } = {}) {
  const sandbox = path.dirname(project.root);
  const codexHome = path.join(sandbox, `${label}-codex-home`);
  const fakeCodex = path.join(sandbox, `${label}-fake-codex`);
  const configPath = path.join(codexHome, "config.toml");
  fs.mkdirSync(codexHome);
  if (configBytes) fs.writeFileSync(configPath, configBytes, { mode });
  fs.copyFileSync(path.join(KIT_ROOT, "tests", "fixtures", "fake_codex_app_server.py"), fakeCodex);
  fs.chmodSync(fakeCodex, 0o755);
  return {
    codexHome,
    configBytes,
    configPath,
    env: {
      AGENT_FLOW_HOST: "codex",
      AGENT_FLOW_SKIP_CODEX_TRUST: "0",
      CODEX_CLI_PATH: fakeCodex,
      CODEX_HOME: codexHome,
    },
  };
}

function startRun(project, {
  workflow = "default",
  task = "contract task",
  runId = "contract-run",
  env = {},
} = {}) {
  const result = run(
    ["run", "start", "--task", task, "--workflow", workflow, "--run-id", runId],
    project.root,
    project.home,
    env,
  );
  assert.equal(result.status, 0, result.stderr || result.stdout);
  const commonDirResult = spawnSync("git", ["rev-parse", "--git-common-dir"], {
    cwd: project.root,
    encoding: "utf8",
  });
  assert.equal(commonDirResult.status, 0, commonDirResult.stderr || commonDirResult.stdout);
  const commonDir = path.resolve(project.root, commonDirResult.stdout.trim());
  const pointerPaths = [path.join(project.root, ".agent-flow", "state", "current-run.json")];
  const worktreeStateRoot = path.join(commonDir, "agent-flow", "worktrees");
  if (fs.existsSync(worktreeStateRoot)) {
    for (const entry of fs.readdirSync(worktreeStateRoot, { withFileTypes: true })) {
      if (entry.isDirectory() && !entry.isSymbolicLink()) {
        pointerPaths.push(path.join(worktreeStateRoot, entry.name, ".agent-flow", "state", "current-run.json"));
      }
    }
  }
  const pointers = pointerPaths.filter((candidate) => fs.existsSync(candidate));
  assert.equal(pointers.length, 1, `expected one active Node pointer, found ${pointers.length}`);
  const statePath = pointers[0];
  const stateRoot = path.dirname(path.dirname(path.dirname(statePath)));
  const state = JSON.parse(fs.readFileSync(statePath, "utf8"));
  return {
    ...project,
    state,
    statePath,
    runDir: path.isAbsolute(state.run_dir) ? state.run_dir : path.resolve(stateRoot, state.run_dir),
  };
}

function updateRunState(run, phase, phaseIndex, extra = {}) {
  const enteredAt = new Date(Date.now() - 5_000).toISOString();
  const state = {
    ...run.state,
    phase,
    phase_index: phaseIndex,
    status: "running",
    phase_entered_at: enteredAt,
    updated_at: enteredAt,
    ...extra,
  };
  writeJson(run.statePath, state);
  writeJson(path.join(run.runDir, "manifest.json"), state);
  run.state = state;
  return state;
}

let cachedFullFeature;
function fullFeatureDefinition() {
  if (cachedFullFeature) return cachedFullFeature;
  const result = spawnSync(
    "python3",
    ["-m", "agent_flow.cli", "workflow", "export", "--workflow", "full-feature", "--format", "json"],
    {
      cwd: KIT_ROOT,
      encoding: "utf8",
      env: {
        ...process.env,
        PYTHONPATH: [path.join(KIT_ROOT, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
      },
    },
  );
  assert.equal(result.status, 0, result.stderr || result.stdout);
  cachedFullFeature = JSON.parse(result.stdout);
  return cachedFullFeature;
}

function phaseFixture(id) {
  const phases = fullFeatureDefinition().phases;
  const index = phases.findIndex((phase) => phase.id === id);
  assert.notEqual(index, -1, `missing phase ${id}`);
  return { phase: phases[index], index };
}

function concreteMarker(marker) {
  if (marker.startsWith("#")) return marker;
  const separator = marker.indexOf(":");
  if (separator === -1) return marker;
  const key = marker.slice(0, separator);
  const options = marker.slice(separator + 1).split("|").map((value) => value.trim()).filter(Boolean);
  if (key === "missing-required-profile-skills") return `${key}: none`;
  if (key === "project-local-skills") return `${key}: n/a`;
  if (key === "project-local-skills-used") return `${key}: n/a`;
  if (key === "active-profiles") return `${key}: generic`;
  if (options.length === 0) return `${key}: value`;
  return `${key}: ${options[0]}`;
}

test("explicit kit profiles control Node worktree base and naming", (t) => {
  const project = setupProject(t, { profile: "spring" });
  git(project.root, ["branch", "develop", "main"]);
  const springProfile = path.join(project.root, ".agent-flow", "profiles", "spring.yaml");
  fs.writeFileSync(
    springProfile,
    fs.readFileSync(springProfile, "utf8").replace("max_slug_length: 60", "max_slug_length: 12"),
    "utf8",
  );

  const started = startRun(project, {
    task: "this task name must be truncated using spring naming",
    runId: "spring-profile",
  });

  assert.equal(started.state.base_ref, "develop");
  const branchResult = spawnSync("git", ["branch", "--show-current"], {
    cwd: started.state.workspace_root,
    encoding: "utf8",
  });
  assert.equal(branchResult.status, 0, branchResult.stderr || branchResult.stdout);
  const branch = branchResult.stdout.trim();
  assert.equal(branch.startsWith("feat/"), true);
  assert.equal(branch.slice("feat/".length).length <= 12, true);
  const kit = JSON.parse(fs.readFileSync(path.join(project.root, ".agent-flow", "kit.json"), "utf8"));
  assert.equal(kit.profile, "generic");
  assert.equal(kit.primary_profile, "spring");
  assert.equal(kit.profile_selection, "explicit");
  assert.deepEqual(kit.profiles, ["spring"]);
});

test("Node run base uses canonical primary instead of the first union profile", (t) => {
  const project = setupProject(t, { profile: "spring,python" });
  git(project.root, ["branch", "develop", "main"]);
  const springPath = path.join(project.root, ".agent-flow", "profiles", "spring.yaml");
  const pythonPath = path.join(project.root, ".agent-flow", "profiles", "python.yaml");
  fs.writeFileSync(
    springPath,
    fs.readFileSync(springPath, "utf8").replace("base: develop", "base: main"),
    "utf8",
  );
  fs.writeFileSync(
    pythonPath,
    fs.readFileSync(pythonPath, "utf8").replace("base: main", "base: develop"),
    "utf8",
  );
  const kitPath = path.join(project.root, ".agent-flow", "kit.json");
  const kit = JSON.parse(fs.readFileSync(kitPath, "utf8"));
  writeJson(kitPath, { ...kit, primary_profile: "python" });

  const started = startRun(project, {
    task: "primary base",
    runId: "primary-base",
    env: { AGENT_FLOW_PROFILE: "spring" },
  });

  assert.equal(started.state.base_ref, "develop");
});

test("Node worktree mode uses only the canonical primary regardless of union order", (t) => {
  const cases = [
    ["python", "python,node", true],
    ["python", "node,python", true],
    ["node", "python,node", false],
    ["node", "node,python", false],
  ];
  for (const [primary, profileOrder, expectsLeader] of cases) {
    const project = setupProject(t, { profile: profileOrder });
    const pythonPath = path.join(project.root, ".agent-flow", "profiles", "python.yaml");
    fs.writeFileSync(
      pythonPath,
      fs.readFileSync(pythonPath, "utf8").replace("worktree: required", "worktree: disabled"),
      "utf8",
    );
    const kitPath = path.join(project.root, ".agent-flow", "kit.json");
    const kit = JSON.parse(fs.readFileSync(kitPath, "utf8"));
    writeJson(kitPath, { ...kit, primary_profile: primary });

    const started = startRun(project, {
      task: `primary ${primary} union ${profileOrder}`,
      runId: `worktree-${primary}-${profileOrder.replace(",", "-")}`,
    });

    assert.equal(
      fs.realpathSync.native(started.state.workspace_root) === fs.realpathSync.native(project.root),
      expectsLeader,
    );
    assert.equal(started.state.worktree_mode, expectsLeader ? "disabled" : "required");
  }
});

test("Node rejects missing, symlinked, and wrong-id installed primary profiles", (t) => {
  const project = setupProject(t, { profile: "python" });
  const profilePath = path.join(project.root, ".agent-flow", "profiles", "python.yaml");
  const original = fs.readFileSync(profilePath, "utf8");

  fs.writeFileSync(profilePath, original.replace(/^id:\s*python/m, "id: android"), "utf8");
  let result = run(["run", "start", "--task", "wrong id", "--run-id", "wrong-id"], project.root, project.home);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /profile id mismatch/);

  fs.writeFileSync(profilePath, "id: python\n- malformed\n", "utf8");
  result = run(["run", "start", "--task", "malformed profile", "--run-id", "malformed-profile"], project.root, project.home);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /invalid YAML/);

  fs.rmSync(profilePath);
  result = run(["run", "start", "--task", "missing profile", "--run-id", "missing-profile"], project.root, project.home);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /installed profile python.*unreadable|unknown installed profile/);

  const outside = path.join(path.dirname(project.root), "python-profile.yaml");
  fs.writeFileSync(outside, original, "utf8");
  fs.symlinkSync(outside, profilePath);
  result = run(["run", "start", "--task", "symlink profile", "--run-id", "symlink-profile"], project.root, project.home);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /may not use symlinks/);
});

test("Node rejects the same installed profile runtime shapes as Python", (t) => {
  const project = setupProject(t, { profile: "python" });
  const profilePath = path.join(project.root, ".agent-flow", "profiles", "python.yaml");
  const cases = [
    ["root-sequence", "- id: python\n", /invalid YAML/],
    ["branching-list", "id: python\nbranching: []\n", /branching must be a mapping/],
    ["worktree-bool", "id: python\nbranching:\n  worktree: true\n", /branching\.worktree/],
    ["unsafe-base", "id: python\nbranching:\n  base: --upload-pack=malicious\n", /branching\.base is unsafe/],
    ["naming-scalar", "id: python\nbranching:\n  naming: scalar\n", /branching\.naming must be a mapping/],
    ["wrong-prefix", "id: python\nbranching:\n  naming:\n    prefix: feature/\n", /branching\.naming\.prefix/],
    ["short-slug", "id: python\nbranching:\n  naming:\n    max_slug_length: 11\n", /branching\.naming\.max_slug_length/],
    ["gates-mapping", "id: python\ngates: {}\n", /gates must be a list/],
  ];

  for (const [runId, text, expected] of cases) {
    fs.writeFileSync(profilePath, text, "utf8");
    const result = run(
      ["run", "start", "--task", `invalid ${runId}`, "--run-id", runId],
      project.root,
      project.home,
    );
    assert.notEqual(result.status, 0, `${runId}: ${result.stdout}`);
    assert.match(result.stderr, expected, runId);
  }
});

test("Node consumes the canonical Python-parsed inline YAML profile snapshot", (t) => {
  const project = setupProject(t, { profile: "python" });
  const profilePath = path.join(project.root, ".agent-flow", "profiles", "python.yaml");
  fs.writeFileSync(
    profilePath,
    "id: python\n"
      + "branching: {base: main, worktree: disabled, "
      + 'naming: {prefix: "feat/", max_slug_length: 12}}\n'
      + "gates: []\n",
    "utf8",
  );

  const started = startRun(project, {
    task: "canonical inline yaml profile",
    runId: "inline-yaml-profile",
  });

  assert.equal(started.state.worktree_mode, "disabled");
  assert.equal(
    fs.realpathSync.native(started.state.workspace_root),
    fs.realpathSync.native(project.root),
  );
});

test("explicit union reinstall preserves primary order", (t) => {
  const explicit = setupProject(t, { profile: "python,spring" });
  let explicitKit = JSON.parse(fs.readFileSync(path.join(explicit.root, ".agent-flow", "kit.json"), "utf8"));
  const initialNodeRuntime = explicitKit.node_runtime;
  assert.equal(initialNodeRuntime.path, ".agent-flow/runtime/node/bin/agent-flow-kit.mjs");
  assert.match(initialNodeRuntime.tree_hash, /^[a-f0-9]{64}$/);
  assert.equal(
    fs.existsSync(path.join(explicit.root, ".agent-flow", "runtime", "node", "lib", "codex-hook-trust.mjs")),
    true,
  );
  assert.equal(explicitKit.primary_profile, "python");
  assert.equal(explicitKit.profile_selection, "explicit");
  assert.deepEqual(explicitKit.profiles, ["python", "spring"]);
  writeSkill(path.join(explicit.root, "skills"), "extra");
  const additive = run(["install", "--skill", "extra"], explicit.root, explicit.home);
  assert.equal(additive.status, 0, additive.stderr || additive.stdout);
  explicitKit = JSON.parse(fs.readFileSync(path.join(explicit.root, ".agent-flow", "kit.json"), "utf8"));
  let explicitIndex = JSON.parse(fs.readFileSync(path.join(explicit.root, ".agent-flow", "skills", "index.json"), "utf8"));
  assert.equal(explicitKit.primary_profile, "python");
  assert.equal(explicitKit.profile_selection, "explicit");
  assert.deepEqual(explicitKit.profiles, ["python", "spring"]);
  assert(explicitIndex.selection.explicit_skills.includes("extra"));
  const reinstallExplicit = run(["install"], explicit.root, explicit.home);
  assert.equal(reinstallExplicit.status, 0, reinstallExplicit.stderr || reinstallExplicit.stdout);
  explicitKit = JSON.parse(fs.readFileSync(path.join(explicit.root, ".agent-flow", "kit.json"), "utf8"));
  assert.equal(explicitKit.primary_profile, "python");
  assert.deepEqual(explicitKit.profiles, ["python", "spring"]);
  assert.deepEqual(explicitKit.node_runtime, initialNodeRuntime);
  explicitIndex = JSON.parse(fs.readFileSync(path.join(explicit.root, ".agent-flow", "skills", "index.json"), "utf8"));
  assert(explicitIndex.selection.explicit_skills.includes("extra"));
});

test("auto install updates primary and profiles when the detected stack changes", (t) => {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-primary-auto-"));
  const root = path.join(sandbox, "project");
  const home = path.join(sandbox, "home");
  fs.mkdirSync(root, { recursive: true });
  fs.mkdirSync(home, { recursive: true });
  t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));

  let result = run(["install"], root, home);
  assert.equal(result.status, 0, result.stderr || result.stdout);
  let kit = JSON.parse(fs.readFileSync(path.join(root, ".agent-flow", "kit.json"), "utf8"));
  assert.equal(kit.primary_profile, "generic");
  assert.equal(kit.profile_selection, "auto");
  assert.deepEqual(kit.profiles, ["generic"]);

  fs.writeFileSync(path.join(root, "package.json"), '{"dependencies":{"react":"latest"}}\n', "utf8");
  result = run(["install"], root, home);
  assert.equal(result.status, 0, result.stderr || result.stdout);
  kit = JSON.parse(fs.readFileSync(path.join(root, ".agent-flow", "kit.json"), "utf8"));
  const index = JSON.parse(fs.readFileSync(path.join(root, ".agent-flow", "skills", "index.json"), "utf8"));
  assert.equal(kit.primary_profile, "react");
  assert.equal(kit.profile_selection, "auto");
  assert.deepEqual(kit.profiles, ["react"]);
  assert.equal(index.selection.profile_selection, "auto");
  assert.deepEqual(index.selection.profiles, ["react"]);
});

test("react-native-web alone stays React while exact React Native markers select RN", (t) => {
  const cases = [
    [{ react: "19", "react-native-web": "0.20" }, "react"],
    [{ react: "19", "react-native": "latest" }, "react-native"],
    [{ react: "19", expo: "latest" }, "react-native"],
  ];
  for (const [dependencies, expected] of cases) {
    const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), `agent-flow-profile-exact-${expected}-`));
    const root = path.join(sandbox, "project");
    const home = path.join(sandbox, "home");
    fs.mkdirSync(root, { recursive: true });
    fs.mkdirSync(home, { recursive: true });
    t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));
    fs.writeFileSync(path.join(root, "package.json"), `${JSON.stringify({ dependencies })}\n`, "utf8");

    const result = run(["install"], root, home);
    assert.equal(result.status, 0, result.stderr || result.stdout);
    const kit = JSON.parse(fs.readFileSync(path.join(root, ".agent-flow", "kit.json"), "utf8"));
    assert.equal(kit.primary_profile, expected);
    assert.deepEqual(kit.profiles, [expected]);
  }
});

test("TypeScript Node backend keeps node profile and installs conditional TypeScript guidance", (t) => {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-node-typescript-"));
  const root = path.join(sandbox, "project");
  const home = path.join(sandbox, "home");
  fs.mkdirSync(root, { recursive: true });
  fs.mkdirSync(home, { recursive: true });
  t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));
  fs.writeFileSync(
    path.join(root, "package.json"),
    '{"dependencies":{"express":"latest"},"devDependencies":{"typescript":"latest"}}\n',
    "utf8",
  );
  fs.writeFileSync(path.join(root, "tsconfig.json"), "{}\n", "utf8");

  const result = run(["install"], root, home);
  assert.equal(result.status, 0, result.stderr || result.stdout);
  const kit = JSON.parse(fs.readFileSync(path.join(root, ".agent-flow", "kit.json"), "utf8"));
  const index = JSON.parse(fs.readFileSync(path.join(root, ".agent-flow", "skills", "index.json"), "utf8"));
  assert.equal(kit.primary_profile, "node");
  assert.deepEqual(kit.profiles, ["node"]);
  assert(index.skills.some((skill) => skill.name === "node-development-guide"));
  assert(index.skills.some((skill) => skill.name === "typescript-development-guide"));
  assert.deepEqual(index.selection.conditional_skills.node, {
    implementation: ["typescript-development-guide"],
    review: ["typescript-development-guide"],
  });
});

test("install --skill keeps auto-detected profile requirements additive", (t) => {
  const cases = [
    ["android", "settings.gradle.kts", 'rootProject.name = "app"\n', "android-code-review"],
    ["react", "package.json", '{"dependencies":{"react":"latest"}}\n', "react-development-guide"],
    ["python", "pyproject.toml", '[project]\nname = "demo"\n', "python-development-guide"],
  ];
  for (const [profile, marker, content, requiredSkill] of cases) {
    const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), `agent-flow-skill-additive-${profile}-`));
    const root = path.join(sandbox, "project");
    const home = path.join(sandbox, "home");
    fs.mkdirSync(root, { recursive: true });
    fs.mkdirSync(home, { recursive: true });
    t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));
    fs.writeFileSync(path.join(root, marker), content, "utf8");
    writeSkill(path.join(root, "skills"), "extra");
    const selection = resolveInstallSelection({
      args: ["--skill", "extra"],
      detectedProfile: profile,
      kitRoot: KIT_ROOT,
      projectRoot: root,
    });
    for (const name of selection.skillNames) {
      if (!fs.existsSync(path.join(KIT_ROOT, "skills", name))) {
        writeSkill(path.join(home, ".agents", "skills"), name);
      }
    }

    const result = run(["install", "--skill", "extra"], root, home);
    assert.equal(result.status, 0, result.stderr || result.stdout);
    const kit = JSON.parse(fs.readFileSync(path.join(root, ".agent-flow", "kit.json"), "utf8"));
    const index = JSON.parse(fs.readFileSync(path.join(root, ".agent-flow", "skills", "index.json"), "utf8"));
    assert.deepEqual(kit.profiles, [profile]);
    assert.equal(kit.primary_profile, profile);
    assert.equal(kit.profile_selection, "auto");
    assert(index.skills.some((skill) => skill.name === "extra"), profile);
    assert(index.skills.some((skill) => skill.name === requiredSkill), profile);
  }
});

test("runtime project-local prompt unions indexed and pre-start unindexed always skills", (t) => {
  const project = setupProject(t, {
    localSkills: [{ name: "indexed", routingMetadata: "activation: always" }],
  });
  writeSkill(
    path.join(project.root, ".agent-flow", "local-skills"),
    "unindexed",
    undefined,
    "activation: always",
  );
  const active = startRun(project, { runId: "local-union" });
  updateRunState(active, "implement", 3);

  const next = run(["run", "next"], active.state.workspace_root, project.home);
  assert.equal(next.status, 0, next.stderr || next.stdout);
  assert.match(next.stdout, /\.agent-flow\/local-skills\/indexed\/SKILL\.md/);
  assert.match(next.stdout, /\.agent-flow\/local-skills\/unindexed\/SKILL\.md/);
});

test("metadata-less Samantha local skills stay host-discoverable without prompt injection", (t) => {
  const names = [
    "figma-screen-spec",
    "release-first-branch-pr",
    "samantha-architecture-guide",
    "samantha-translation-sync",
  ];
  const project = setupProject(t, { localSkills: names });
  const index = JSON.parse(
    fs.readFileSync(path.join(project.root, ".agent-flow", "skills", "index.json"), "utf8"),
  );
  for (const name of names) {
    const entry = index.skills.find((skill) => skill.name === name);
    assert.equal(entry.source, "local", name);
    assert.equal(Object.hasOwn(entry, "activation"), false, name);
    for (const hostRoot of [".agents", ".claude", ".omp"]) {
      const hostSkill = path.join(project.root, hostRoot, "skills", name, "SKILL.md");
      assert.equal(fs.existsSync(hostSkill), true, `${hostRoot}:${name}`);
      assert.match(fs.readFileSync(hostSkill, "utf8"), new RegExp(`# ${name}`));
    }
  }

  const active = startRun(project, {
    task: "Figma translation release architecture work",
    runId: "samantha-on-demand",
  });
  assert.equal(active.state.local_skill_plan_hash_version, 2);
  updateRunState(active, "implement", 3);
  const prompt = run(["run", "next"], active.state.workspace_root, project.home);
  assert.equal(prompt.status, 0, prompt.stderr || prompt.stdout);
  for (const name of names) {
    assert.doesNotMatch(prompt.stdout, new RegExp(`${name}/SKILL\\.md`), name);
    for (const hostRoot of [".agents", ".claude", ".omp"]) {
      assert.equal(
        fs.existsSync(path.join(active.state.workspace_root, hostRoot, "skills", name, "SKILL.md")),
        true,
        `${hostRoot}:${name}`,
      );
    }
  }
});

test("conditional Figma local skill is injected in a matching non-code phase for every host", (t) => {
  const project = setupProject(t, {
    localSkills: [{
      name: "figma-screen-spec",
      routingMetadata: [
        "activation: conditional",
        "workflowPhases: [domain-grill]",
        "taskTerms: [figma]",
        "pathGlobs: []",
      ].join("\n"),
    }],
  });
  const active = startRun(project, {
    workflow: "full-feature",
    task: "read the Figma screen before grilling",
    runId: "figma-non-code",
  });
  updateRunState(active, "domain-grill", 0);

  const prompt = run(["run", "next"], active.state.workspace_root, project.home);
  assert.equal(prompt.status, 0, prompt.stderr || prompt.stdout);
  assert.match(prompt.stdout, /figma-screen-spec\/SKILL\.md/);
  assert.match(prompt.stdout, /mandatory policy for this phase/);
  assert.match(prompt.stdout, /does not add local-skill completion markers/);
  assert.doesNotMatch(prompt.stdout, /project-local-skills: checked/);

  const index = JSON.parse(
    fs.readFileSync(path.join(project.root, ".agent-flow", "skills", "index.json"), "utf8"),
  );
  const indexed = index.skills.find((skill) => skill.name === "figma-screen-spec");
  assert.match(indexed.tree_hash, /^[0-9a-f]{64}$/);
  for (const hostRoot of [".agents", ".claude", ".omp"]) {
    const leaderHostSkills = path.join(project.root, hostRoot, "skills");
    const worktreeHostSkills = path.join(active.state.workspace_root, hostRoot, "skills");
    assert.equal(
      fs.realpathSync.native(worktreeHostSkills),
      fs.realpathSync.native(leaderHostSkills),
    );
    assert.equal(
      hashSkillTree(fs.realpathSync.native(path.join(worktreeHostSkills, "figma-screen-spec"))),
      indexed.tree_hash,
    );
    assert.deepEqual(
      fs.readFileSync(path.join(worktreeHostSkills, "figma-screen-spec", "SKILL.md")),
      fs.readFileSync(path.join(project.root, ".agent-flow", "local-skills", "figma-screen-spec", "SKILL.md")),
    );
  }
});

test("mixed-case local skill names keep Node and Python prompt/hash parity", (t) => {
  const project = setupProject(t, {
    localSkills: [{ name: "Figma-Reader", routingMetadata: "activation: always" }],
  });
  const active = startRun(project, {
    task: "read a Figma design",
    runId: "mixed-case-local-skill",
  });
  const pythonHash = spawnSync(
    "python3",
    [
      "-c",
      "from pathlib import Path; from agent_flow.core.local_skills import project_local_skill_plan_hash; import sys; print(project_local_skill_plan_hash(Path(sys.argv[1])))",
      project.root,
    ],
    {
      encoding: "utf8",
      env: {
        ...process.env,
        PYTHONPATH: [path.join(KIT_ROOT, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
      },
    },
  );
  assert.equal(pythonHash.status, 0, pythonHash.stderr || pythonHash.stdout);
  assert.equal(active.state.local_skill_plan_hash, pythonHash.stdout.trim());

  updateRunState(active, "implement", 3);
  const prompt = run(["run", "next"], active.state.workspace_root, project.home);
  assert.equal(prompt.status, 0, prompt.stderr || prompt.stdout);
  assert.match(prompt.stdout, /\.agent-flow\/local-skills\/Figma-Reader\/SKILL\.md/);
  assert.match(prompt.stdout, /\(`figma-reader`\)/);
});

test("Node phase output and installed prompts use the canonical Python profile projection", (t) => {
  const project = setupProject(t, { profile: "node" });
  const active = startRun(project, { task: "profile prompt parity", runId: "profile-prompt-parity" });
  updateRunState(active, "slice-plan", 1);

  const next = run(["run", "next"], active.state.workspace_root, project.home);
  assert.equal(next.status, 0, next.stderr || next.stdout);
  const script = [
    "from pathlib import Path",
    "from agent_flow.core.profiles import load_installed_profile_snapshot, render_profile_prompt_block",
    "import sys",
    "profile_id, profile = load_installed_profile_snapshot(Path(sys.argv[1]), ['node'], 'node')",
    "print(render_profile_prompt_block(profile_id, profile, 'slice-plan'), end='')",
  ].join("; ");
  const rendered = spawnSync("python3", ["-c", script, project.root], {
    encoding: "utf8",
    env: {
      ...process.env,
      PYTHONPATH: [
        path.join(project.root, ".agent-flow", "runtime", "python"),
        path.join(KIT_ROOT, "src"),
        process.env.PYTHONPATH,
      ].filter(Boolean).join(path.delimiter),
    },
  });
  assert.equal(rendered.status, 0, rendered.stderr || rendered.stdout);
  assert.equal(next.stdout.includes(rendered.stdout), true);
  assert.match(next.stdout, /## Active profile: `node`/);
  assert.match(next.stdout, /max_slug_length: 60/);
  assert.match(next.stdout, /command:\n\s+- npm\n\s+- test/);

  const installedPrompt = fs.readFileSync(
    path.join(project.root, ".agent-flow", "prompts", "slice-plan.md"),
    "utf8",
  );
  assert.equal(installedPrompt.includes(rendered.stdout), true);
});

test("pre-start unindexed conditional skills keep selector and hash parity with Python", (t) => {
  const project = setupProject(t);
  writeSkill(
    path.join(project.root, ".agent-flow", "local-skills"),
    "unindexed-conditional",
    undefined,
    [
      "activation: conditional",
      "workflowPhases: [implement]",
      "taskTerms: [deploy plan, Straße]",
      "pathGlobs: [packages/**/*.widget]",
    ].join("\n"),
  );
  const active = startRun(project, {
    task: "execute the DEPLOY PLAN",
    runId: "unindexed-conditional",
  });
  const index = JSON.parse(
    fs.readFileSync(path.join(project.root, ".agent-flow", "skills", "index.json"), "utf8"),
  );
  assert.equal(index.skills.some((skill) => skill.name === "unindexed-conditional"), false);
  const pythonHash = spawnSync(
    "python3",
    [
      "-c",
      "from pathlib import Path; from agent_flow.core.local_skills import project_local_skill_plan_hash; import sys; print(project_local_skill_plan_hash(Path(sys.argv[1])))",
      project.root,
    ],
    {
      encoding: "utf8",
      env: {
        ...process.env,
        PYTHONPATH: [path.join(KIT_ROOT, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
      },
    },
  );
  assert.equal(pythonHash.status, 0, pythonHash.stderr || pythonHash.stdout);
  assert.equal(active.state.local_skill_plan_hash_version, 2);
  assert.equal(active.state.local_skill_plan_hash, pythonHash.stdout.trim());

  updateRunState(active, "implement", 3);
  const matching = run(["run", "next"], active.state.workspace_root, project.home);
  assert.equal(matching.status, 0, matching.stderr || matching.stdout);
  assert.match(matching.stdout, /unindexed-conditional\/SKILL\.md/);

  updateRunState(active, "implement", 3, { task: "unrelated task" });
  const nonmatching = run(["run", "next"], active.state.workspace_root, project.home);
  assert.equal(nonmatching.status, 0, nonmatching.stderr || nonmatching.stdout);
  assert.doesNotMatch(nonmatching.stdout, /unindexed-conditional\/SKILL\.md/);
});

test("active run pins unindexed local add edit delete and rename mutations", (t) => {
  for (const mutation of ["add", "edit", "delete", "rename"]) {
    const project = setupProject(t);
    const base = path.join(project.root, ".agent-flow", "local-skills");
    writeSkill(
      base,
      "mutable-policy",
      undefined,
      "activation: conditional\nworkflowPhases: [implement]\ntaskTerms: [never-match]\npathGlobs: []",
    );
    const active = startRun(project, {
      task: "unrelated task",
      runId: `local-pin-${mutation}`,
    });
    updateRunState(active, "implement", 3);
    const mutable = path.join(base, "mutable-policy");
    if (mutation === "add") {
      writeSkill(
        base,
        "added-policy",
        undefined,
        "activation: conditional\ntaskTerms: [never-match]\npathGlobs: []",
      );
    } else if (mutation === "edit") {
      fs.appendFileSync(path.join(mutable, "SKILL.md"), "changed\n", "utf8");
    } else if (mutation === "delete") {
      fs.rmSync(mutable, { recursive: true, force: true });
    } else {
      fs.renameSync(mutable, path.join(base, "renamed-policy"));
    }
    const result = run(["run", "next"], active.state.workspace_root, project.home);
    assert.notEqual(result.status, 0, mutation);
    assert.match(result.stderr, /project-local skill plan changed/, mutation);
  }
});

test("active Node legacy and partial skill pins always fail closed", (t) => {
  const cases = [
    {
      name: "missing-main",
      command: ["run", "status"],
      mutate(state) {
        delete state.skill_plan_hash;
        delete state.skill_plan_hash_version;
      },
      expected: /missing its skill plan pin/,
    },
    {
      name: "old-main",
      command: ["run", "next"],
      mutate(state) {
        state.skill_plan_hash_version = 1;
      },
      expected: /obsolete skill plan pin/,
    },
    {
      name: "missing-local",
      command: ["run", "advance"],
      mutate(state) {
        delete state.local_skill_plan_hash;
        delete state.local_skill_plan_hash_version;
      },
      expected: /missing its project-local skill plan pin/,
    },
    {
      name: "partial-local",
      command: ["run", "status"],
      mutate(state) {
        delete state.local_skill_plan_hash;
      },
      expected: /invalid project-local skill plan pin/,
    },
    {
      name: "old-local",
      command: ["run", "next"],
      mutate(state) {
        state.local_skill_plan_hash_version = 1;
      },
      expected: /project-local skill plan changed/,
    },
  ];
  for (const fixture of cases) {
    const project = setupProject(t);
    const active = startRun(project, { runId: `legacy-${fixture.name}` });
    const state = { ...active.state };
    fixture.mutate(state);
    writeJson(active.statePath, state);
    writeJson(path.join(active.runDir, "manifest.json"), state);
    const result = run(fixture.command, active.state.workspace_root, project.home);
    assert.notEqual(result.status, 0, fixture.name);
    assert.match(result.stderr, fixture.expected, fixture.name);
  }
});

test("runtime project-local discovery uses canonical names and deterministic priority", (t) => {
  const project = setupProject(t, {
    localSkills: [{
      name: "directory-alias",
      routingMetadata: "name: canonical-policy\nactivation: always",
    }],
  });
  writeSkill(
    path.join(project.root, "skills"),
    "project-alias",
    undefined,
    "name: canonical-policy\nactivation: always",
  );
  const active = startRun(project, { runId: "local-priority" });
  updateRunState(active, "implement", 3);

  const preferred = run(["run", "next"], active.state.workspace_root, project.home);
  assert.equal(preferred.status, 0, preferred.stderr || preferred.stdout);
  assert.match(preferred.stdout, /\.agent-flow\/local-skills\/directory-alias\/SKILL\.md/);
  assert.doesNotMatch(preferred.stdout, /skills\/project-alias\/SKILL\.md/);
  assert.match(preferred.stdout, /project-local-skills-used: canonical-policy/);

  writeSkill(
    path.join(project.root, ".agent-flow", "local-skills"),
    "second-alias",
    undefined,
    "name: Canonical-Policy\nactivation: always",
  );
  const conflict = run(["run", "next"], active.state.workspace_root, project.home);
  assert.notEqual(conflict.status, 0);
  assert.match(conflict.stderr, /conflicting project-local skill paths/);
});

test("profile skill prompt uses verified leader snapshot from external detached worktree", (t) => {
  const project = setupProject(t);
  const external = path.join(path.dirname(project.root), "external-detached");
  git(project.root, ["worktree", "add", "--detach", external, "main"]);
  const started = run(["run", "external profile task"], external, project.home);
  assert.equal(started.status, 0, started.stderr || started.stdout);
  const statePath = path.join(project.root, ".agent-flow", "state", "current-run.json");
  const state = JSON.parse(fs.readFileSync(statePath, "utf8"));
  const active = {
    ...project,
    state,
    statePath,
    runDir: path.resolve(project.root, state.run_dir),
  };
  assert.equal(fs.realpathSync(state.workspace_root), fs.realpathSync(external));
  updateRunState(active, "green", 9);

  const prompt = run(["run", "next"], external, project.home);

  assert.equal(prompt.status, 0, prompt.stderr || prompt.stdout);
  const snapshot = path.join(
    project.root,
    ".agent-flow",
    "skills",
    "code-generation-discipline",
    "SKILL.md",
  );
  assert.match(prompt.stdout, /\.agent-flow\/skills\/code-generation-discipline\/SKILL\.md/);
  assert.equal(prompt.stdout.includes(`\`${fs.realpathSync(snapshot)}\``), true);
  assert.equal(fs.statSync(snapshot).isFile(), true);
  fs.appendFileSync(snapshot, "tampered\n", "utf8");
  const tampered = run(["run", "next"], external, project.home);
  assert.notEqual(tampered.status, 0);
  assert.match(tampered.stderr, /installed skill snapshot changed|skill plan changed/);
});

test("installed index and kit metadata fail closed on corrupt or non-object JSON", (t) => {
  const project = setupProject(t);
  const targets = [
    path.join(project.root, ".agent-flow", "skills", "index.json"),
    path.join(project.root, ".agent-flow", "kit.json"),
  ];
  for (const target of targets) {
    const original = fs.readFileSync(target);
    for (const payload of ["{invalid", "[]\n"]) {
      fs.writeFileSync(target, payload, "utf8");
      const result = run(
        ["run", "start", "--task", "strict metadata", "--run-id", `strict-${path.basename(target)}`],
        project.root,
        project.home,
      );
      assert.notEqual(result.status, 0);
      assert.match(result.stderr, /installed (skill index|kit metadata)/);
      fs.writeFileSync(target, original);
    }
  }

  const active = startRun(project, { task: "strict next", runId: "strict-next" });
  updateRunState(active, "implement", 3);
  const indexPath = path.join(project.root, ".agent-flow", "skills", "index.json");
  fs.writeFileSync(indexPath, "{invalid", "utf8");
  const next = run(["run", "next"], active.state.workspace_root, project.home);
  assert.notEqual(next.status, 0);
  assert.match(next.stderr, /installed skill index/);
});

test("installed kit profile schema and active profile mutations fail closed", (t) => {
  const project = setupProject(t);
  const kitPath = path.join(project.root, ".agent-flow", "kit.json");
  const original = JSON.parse(fs.readFileSync(kitPath, "utf8"));
  const cases = [
    { ...original, profile: "../node" },
    { ...original, profiles: "node" },
    { ...original, profiles: ["../node"] },
    { ...original, profiles: ["does-not-exist"] },
  ];
  for (const [index, payload] of cases.entries()) {
    writeJson(kitPath, payload);
    const result = run(
      ["run", "start", "--task", "invalid profile metadata", "--run-id", `invalid-profile-${index}`],
      project.root,
      project.home,
    );
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /profile|profiles/);
  }

  writeJson(kitPath, original);
  const active = startRun(project, { runId: "profile-pin" });
  writeJson(kitPath, { ...original, profiles: ["python"] });
  const next = run(["run", "next"], active.state.workspace_root, project.home);
  assert.notEqual(next.status, 0);
  assert.match(next.stderr, /profiles do not match|primary_profile/);
});

test("installed snapshot rejects symlinked metadata and skill ancestors", (t) => {
  for (const targetKind of ["agent-flow", "skills", "index", "skill-directory", "skill-file"]) {
    const project = setupProject(t);
    const agentFlow = path.join(project.root, ".agent-flow");
    const skills = path.join(agentFlow, "skills");
    const index = path.join(skills, "index.json");
    const skillDirectory = path.join(skills, "code-generation-discipline");
    const skillFile = path.join(skillDirectory, "SKILL.md");
    if (targetKind === "agent-flow") {
      const outside = path.join(project.root, "outside-agent-flow");
      fs.renameSync(agentFlow, outside);
      fs.symlinkSync(outside, agentFlow, "dir");
    } else if (targetKind === "skills") {
      const outside = path.join(project.root, "outside-skills");
      fs.renameSync(skills, outside);
      fs.symlinkSync(outside, skills, "dir");
    } else if (targetKind === "index") {
      const outside = path.join(project.root, "outside-index.json");
      fs.renameSync(index, outside);
      fs.symlinkSync(outside, index);
    } else if (targetKind === "skill-directory") {
      const outside = path.join(project.root, "outside-code-skill");
      fs.renameSync(skillDirectory, outside);
      fs.symlinkSync(outside, skillDirectory, "dir");
    } else {
      const outside = path.join(project.root, "outside-skill.md");
      fs.renameSync(skillFile, outside);
      fs.symlinkSync(outside, skillFile);
    }
    const result = run(
      ["run", "start", "--task", "symlink provenance", "--run-id", `symlink-${targetKind}`],
      project.root,
      project.home,
    );
    assert.notEqual(result.status, 0, targetKind);
    assert.match(result.stderr, /symlink/, targetKind);
  }
});

test("install rejects symlinked project-local source roots before snapshot write", (t) => {
  for (const sourceKind of ["local", "project"]) {
    const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), `agent-flow-source-symlink-${sourceKind}-`));
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
    const outside = path.join(root, `outside-${sourceKind}-skills`);
    writeSkill(outside, "unsafe-source");
    const sourceRoot = sourceKind === "local"
      ? path.join(root, ".agent-flow", "local-skills")
      : path.join(root, "skills");
    fs.mkdirSync(path.dirname(sourceRoot), { recursive: true });
    fs.symlinkSync(outside, sourceRoot, "dir");

    const result = run(["install", "--skill", "unsafe-source"], root, home);
    assert.notEqual(result.status, 0, sourceKind);
    assert.match(result.stderr, /real directory|symlink/, sourceKind);
    assert.equal(fs.existsSync(path.join(root, ".agent-flow", "kit.json")), false);
  }
});

test("project-local activation routes arbitrary skills by phase task and path", (t) => {
  const project = setupProject(t, {
    localSkills: [
      "on-demand-policy",
      {
        name: "always-policy",
        routingMetadata: "activation: always",
      },
      {
        name: "term-policy",
        routingMetadata: [
          "activation: conditional",
          "workflowPhases: [implement]",
          "taskTerms: [deploy plan, Straße]",
          "pathGlobs: []",
        ].join("\n"),
      },
      {
        name: "path-policy",
        routingMetadata: [
          "activation: conditional",
          "workflowPhases: [implement]",
          "taskTerms: []",
          "pathGlobs: [packages/**/*.widget]",
        ].join("\n"),
      },
      {
        name: "review-policy",
        routingMetadata: [
          "activation: conditional",
          "workflowPhases: [final-review]",
          "taskTerms: [deploy plan]",
          "pathGlobs: []",
        ].join("\n"),
      },
    ],
  });
  const index = JSON.parse(
    fs.readFileSync(path.join(project.root, ".agent-flow", "skills", "index.json"), "utf8"),
  );
  const termEntry = index.skills.find((skill) => skill.name === "term-policy");
  const onDemandEntry = index.skills.find((skill) => skill.name === "on-demand-policy");
  const alwaysEntry = index.skills.find((skill) => skill.name === "always-policy");
  assert.equal(Object.hasOwn(onDemandEntry, "activation"), false);
  assert.equal(alwaysEntry.activation, "always");
  assert.equal(termEntry.activation, "conditional");
  assert.deepEqual(termEntry.workflowPhases, ["implement"]);
  assert.deepEqual(termEntry.taskTerms, ["deploy plan", "Straße"]);
  assert.deepEqual(termEntry.pathGlobs, []);

  const active = startRun(project, { task: "execute the DEPLOY PLAN now", runId: "local-routing" });
  updateRunState(active, "implement", 3, { task: "execute the DEPLOY PLAN now" });
  const taskPrompt = run(["run", "next"], active.state.workspace_root, project.home);
  assert.equal(taskPrompt.status, 0, taskPrompt.stderr || taskPrompt.stdout);
  assert.doesNotMatch(taskPrompt.stdout, /on-demand-policy\/SKILL\.md/);
  assert.match(taskPrompt.stdout, /always-policy\/SKILL\.md/);
  assert.match(taskPrompt.stdout, /term-policy\/SKILL\.md/);
  assert.doesNotMatch(taskPrompt.stdout, /path-policy\/SKILL\.md/);
  assert.doesNotMatch(taskPrompt.stdout, /review-policy\/SKILL\.md/);
  const internalSnapshot = path.join(
    project.root,
    ".agent-flow",
    "skills",
    "code-generation-discipline",
    "SKILL.md",
  );
  assert.equal(taskPrompt.stdout.includes(`\`${fs.realpathSync(internalSnapshot)}\``), true);
  assert.equal(fs.lstatSync(internalSnapshot).isFile(), true);

  updateRunState(active, "implement", 3, { task: "redeploy planner" });
  const partialPrompt = run(["run", "next"], active.state.workspace_root, project.home);
  assert.equal(partialPrompt.status, 0, partialPrompt.stderr || partialPrompt.stdout);
  assert.doesNotMatch(partialPrompt.stdout, /on-demand-policy\/SKILL\.md/);
  assert.match(partialPrompt.stdout, /always-policy\/SKILL\.md/);
  assert.doesNotMatch(partialPrompt.stdout, /term-policy\/SKILL\.md/);

  updateRunState(active, "implement", 3, { task: "STRASSE migration" });
  const unicodePrompt = run(["run", "next"], active.state.workspace_root, project.home);
  assert.equal(unicodePrompt.status, 0, unicodePrompt.stderr || unicodePrompt.stdout);
  assert.match(unicodePrompt.stdout, /always-policy\/SKILL\.md/);
  assert.match(unicodePrompt.stdout, /term-policy\/SKILL\.md/);

  const changed = path.join(active.state.workspace_root, "packages", "ui", "Card.widget");
  fs.mkdirSync(path.dirname(changed), { recursive: true });
  fs.writeFileSync(changed, "changed\n", "utf8");
  updateRunState(active, "implement", 3, { task: "unrelated task" });
  const pathPrompt = run(["run", "next"], active.state.workspace_root, project.home);
  assert.equal(pathPrompt.status, 0, pathPrompt.stderr || pathPrompt.stdout);
  assert.match(pathPrompt.stdout, /always-policy\/SKILL\.md/);
  assert.match(pathPrompt.stdout, /path-policy\/SKILL\.md/);
  assert.doesNotMatch(pathPrompt.stdout, /term-policy\/SKILL\.md/);

  updateRunState(active, "final-review", 5, { task: "execute deploy plan" });
  const phasePrompt = run(["run", "next"], active.state.workspace_root, project.home);
  assert.equal(phasePrompt.status, 0, phasePrompt.stderr || phasePrompt.stdout);
  assert.match(phasePrompt.stdout, /always-policy\/SKILL\.md/);
  assert.match(phasePrompt.stdout, /review-policy\/SKILL\.md/);
  assert.doesNotMatch(phasePrompt.stdout, /term-policy\/SKILL\.md/);
});

test("conditional project-local activation rejects missing and unsafe selectors", (t) => {
  for (const [name, routingMetadata, expected] of [
    [
      "missing-selector",
      "activation: conditional\nworkflowPhases: [implement]\ntaskTerms: []\npathGlobs: []",
      /no selectors/,
    ],
    [
      "unsafe-selector",
      "activation: conditional\ntaskTerms: []\npathGlobs: [..\/outside\/**]",
      /pathGlobs/,
    ],
    [
      "scalar-phase-without-activation",
      "workflowPhases: implement\ntaskTerms: []\npathGlobs: []",
      /workflowPhases/,
    ],
    [
      "scalar-task-without-activation",
      "taskTerms: deploy\npathGlobs: []",
      /taskTerms/,
    ],
    [
      "scalar-path-without-activation",
      "taskTerms: []\npathGlobs: packages\/**",
      /pathGlobs/,
    ],
  ]) {
    assert.throws(
      () => setupProject(t, { localSkills: [{ name, routingMetadata }] }),
      expected,
    );
  }
});

test("runtime local skill union preserves hash and symlink fail-closed checks", (t) => {
  const project = setupProject(t, { localSkills: ["indexed"] });
  const active = startRun(project, { runId: "local-safety" });
  updateRunState(active, "implement", 3);
  const indexed = path.join(project.root, ".agent-flow", "local-skills", "indexed", "SKILL.md");
  fs.appendFileSync(indexed, "mutated\n", "utf8");
  const changed = run(["run", "next"], active.state.workspace_root, project.home);
  assert.notEqual(changed.status, 0);
  assert.match(changed.stderr, /installed skill snapshot changed|skill plan changed/);

  fs.writeFileSync(indexed, fs.readFileSync(indexed, "utf8").replace("mutated\n", ""), "utf8");
  const target = path.join(project.root, "outside-skill");
  writeSkill(project.root, "outside-skill");
  fs.symlinkSync(target, path.join(project.root, ".agent-flow", "local-skills", "unsafe"));
  const linked = run(["run", "next"], active.state.workspace_root, project.home);
  assert.notEqual(linked.status, 0);
  assert.match(linked.stderr, /skill source may not be a symlink/);
});

test("overlong project skill fails before snapshot or index write", (t) => {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-overlong-project-"));
  const root = path.join(sandbox, "project");
  const home = path.join(sandbox, "home");
  fs.mkdirSync(root, { recursive: true });
  fs.mkdirSync(home, { recursive: true });
  t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));
  writeSkill(
    path.join(root, "skills"),
    "overlong",
    Array.from({ length: 201 }, (_, index) => `line ${index}`).join("\n"),
  );

  const install = run(["install", "--profile", "node"], root, home);
  assert.notEqual(install.status, 0);
  assert.match(install.stderr, /max is 200/);
  assert.equal(fs.existsSync(path.join(root, ".agent-flow", "skills", "index.json")), false);
  assert.equal(fs.existsSync(path.join(root, ".agent-flow", "skills", "overlong")), false);
});

test("overlong active-host skill installs a self-contained bundled fallback", (t) => {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-bundled-fallback-"));
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
  writeSkill(
    path.join(home, ".codex", "skills"),
    "adaptive",
    Array.from({ length: 201 }, (_, index) => `host line ${index}`).join("\n"),
  );

  const install = run(
    ["install", "--skill", "adaptive"],
    root,
    home,
    { CODEX_HOME: path.join(home, ".codex") },
  );
  assert.equal(install.status, 0, install.stderr || install.stdout);
  const index = JSON.parse(fs.readFileSync(path.join(root, ".agent-flow", "skills", "index.json"), "utf8"));
  const adaptive = index.skills.find((skill) => skill.name === "adaptive");
  assert.equal(adaptive.source, "bundled");
  assert.equal(adaptive.source_host, null);
  const installed = path.join(root, ".agent-flow", "skills", "adaptive");
  const main = fs.readFileSync(path.join(installed, "SKILL.md"), "utf8");
  assert.equal(main.split(/\r?\n/).length <= 200, true);
  assert.doesNotMatch(main, /\/Users\/|https?:\/\//);
  for (const reference of [
    "workflow-and-navigation.md",
    "navigation3-multi-pane.md",
    "layouts-and-capabilities.md",
    "app-bars.md",
  ]) {
    assert.equal(fs.existsSync(path.join(installed, "references", reference)), true, reference);
  }
});

test("index-only overlong snapshot claims cannot authorize migration", (t) => {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-snapshot-migration-"));
  const root = path.join(sandbox, "project");
  const home = path.join(sandbox, "home");
  fs.mkdirSync(root, { recursive: true });
  fs.mkdirSync(home, { recursive: true });
  t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));

  const first = run(["install", "--skill", "adaptive"], root, home);
  assert.equal(first.status, 0, first.stderr || first.stdout);

  const installed = path.join(root, ".agent-flow", "skills", "adaptive");
  fs.writeFileSync(
    path.join(installed, "SKILL.md"),
    Array.from({ length: 201 }, (_, index) => `legacy host line ${index}`).join("\n"),
    "utf8",
  );
  const indexPath = path.join(root, ".agent-flow", "skills", "index.json");
  const previousIndex = JSON.parse(fs.readFileSync(indexPath, "utf8"));
  const previousAdaptive = previousIndex.skills.find((skill) => skill.name === "adaptive");
  previousAdaptive.source = "host-bootstrap";
  previousAdaptive.source_host = "codex";
  previousAdaptive.tree_hash = hashSkillTree(installed);
  writeJson(indexPath, previousIndex);
  const userBytes = fs.readFileSync(path.join(installed, "SKILL.md"));

  const reinstall = run(["install", "--skill", "adaptive"], root, home);
  assert.notEqual(reinstall.status, 0);
  assert.match(reinstall.stderr, /previous skill index does not match kit provenance/);
  assert.deepEqual(fs.readFileSync(path.join(installed, "SKILL.md")), userBytes);
});

test("review routes reject ambiguous fields and accept one Python-compatible field", (t) => {
  const project = setupProject(t);
  const active = startRun(project, { workflow: "full-feature", runId: "route-contract" });
  const planReview = phaseFixture("plan-review");
  updateRunState(active, "plan-review", planReview.index);
  const artifact = path.join(active.runDir, planReview.phase.artifact);
  fs.mkdirSync(path.dirname(artifact), { recursive: true });
  fs.writeFileSync(artifact, "# Plan Review\n\nstatus: blocked\nverdict: approve\n", "utf8");

  const contradictory = run(["run", "advance"], active.state.workspace_root, project.home);
  assert.notEqual(contradictory.status, 0);
  assert.match(contradictory.stderr, /invalid-route/);

  fs.writeFileSync(artifact, "# Plan Review\n\nverdict: approve\nverdict: approve\n", "utf8");
  const duplicate = run(["run", "advance"], active.state.workspace_root, project.home);
  assert.notEqual(duplicate.status, 0);
  assert.match(duplicate.stderr, /invalid-route/);

  fs.writeFileSync(artifact, "# Plan Review\n\nstatus: request-changes\n", "utf8");
  const wrongField = run(["run", "advance"], active.state.workspace_root, project.home);
  assert.notEqual(wrongField.status, 0);
  assert.match(wrongField.stderr, /invalid-route/);

  fs.writeFileSync(artifact, "# Plan Review\n\nverdict: request-changes\n", "utf8");
  const compatible = run(["run", "advance"], active.state.workspace_root, project.home);
  assert.equal(compatible.status, 0, compatible.stderr || compatible.stdout);
  assert.equal(JSON.parse(fs.readFileSync(active.statePath, "utf8")).phase, "slice-plan");
});

test("generic routes reject simultaneous status and verdict fields", (t) => {
  const project = setupProject(t);
  const active = startRun(project, { workflow: "full-feature", runId: "generic-route-contract" });
  const fixLoop = phaseFixture("fix-loop");
  updateRunState(active, "fix-loop", fixLoop.index);
  const artifact = path.join(active.runDir, fixLoop.phase.artifact);
  const markers = fixLoop.phase.required_markers.map(concreteMarker);
  fs.mkdirSync(path.dirname(artifact), { recursive: true });
  fs.writeFileSync(
    artifact,
    ["# Fix Loop", "", "## Completion Gate", ...markers, "status: green", "verdict: approve", ""].join("\n"),
    "utf8",
  );

  const contradictory = run(["run", "advance"], active.state.workspace_root, project.home);
  assert.notEqual(contradictory.status, 0);
  assert.match(contradictory.stderr, /invalid-route/);
  assert.equal(JSON.parse(fs.readFileSync(active.statePath, "utf8")).phase, "fix-loop");

  fs.writeFileSync(
    artifact,
    ["# Fix Loop", "", "## Completion Gate", ...markers, "status: green", ""].join("\n"),
    "utf8",
  );
  const statusOnly = run(["run", "advance"], active.state.workspace_root, project.home);
  assert.equal(statusOnly.status, 0, statusOnly.stderr || statusOnly.stdout);
  assert.equal(JSON.parse(fs.readFileSync(active.statePath, "utf8")).phase, "comment-authoring");

  updateRunState(active, "fix-loop", fixLoop.index);
  fs.writeFileSync(
    artifact,
    ["# Fix Loop", "", "## Completion Gate", ...markers, "verdict: approve", ""].join("\n"),
    "utf8",
  );
  const verdictOnly = run(["run", "advance"], active.state.workspace_root, project.home);
  assert.equal(verdictOnly.status, 0, verdictOnly.stderr || verdictOnly.stdout);
  assert.equal(JSON.parse(fs.readFileSync(active.statePath, "utf8")).phase, "comment-authoring");
});

test("gate and PR-watch routes reject contradictory or duplicate routing fields", (t) => {
  const project = setupProject(t);
  const active = startRun(project, { workflow: "full-feature", runId: "route-fail-closed" });
  const gates = phaseFixture("gates");
  updateRunState(active, "gates", gates.index);
  const gateArtifact = path.join(active.runDir, gates.phase.artifact);
  fs.mkdirSync(path.dirname(gateArtifact), { recursive: true });
  const result = {
    id: "test",
    command: "npm test",
    required: true,
    passed: true,
    exit_code: 0,
  };
  fs.writeFileSync(
    gateArtifact,
    `${JSON.stringify({ passed: true, status: "blocked", results: [result] })}\n`,
    "utf8",
  );

  const contradictoryGate = run(["run", "advance"], active.state.workspace_root, project.home);
  assert.notEqual(contradictoryGate.status, 0);
  assert.match(contradictoryGate.stderr, /invalid-route/);
  assert.equal(JSON.parse(fs.readFileSync(active.statePath, "utf8")).phase, "gates");

  fs.writeFileSync(
    gateArtifact,
    `{"passed":false,"passed":true,"results":[${JSON.stringify(result)}]}\n`,
    "utf8",
  );
  const duplicateGate = run(["run", "advance"], active.state.workspace_root, project.home);
  assert.notEqual(duplicateGate.status, 0);
  assert.match(duplicateGate.stderr, /invalid-route/);
  assert.equal(JSON.parse(fs.readFileSync(active.statePath, "utf8")).phase, "gates");

  fs.writeFileSync(
    gateArtifact,
    `{"passed":true,"results":[{"command":"npm test","passed":true,"exit_code":0,"evidence":{"id":1,"id":2}}]}\n`,
    "utf8",
  );
  const nestedDuplicateGate = run(["run", "advance"], active.state.workspace_root, project.home);
  assert.notEqual(nestedDuplicateGate.status, 0);
  assert.match(nestedDuplicateGate.stderr, /invalid-route/);
  assert.equal(JSON.parse(fs.readFileSync(active.statePath, "utf8")).phase, "gates");

  const prWatch = phaseFixture("pr-watch");
  updateRunState(active, "pr-watch", prWatch.index);
  const prWatchArtifact = path.join(active.runDir, prWatch.phase.artifact);
  fs.mkdirSync(path.dirname(prWatchArtifact), { recursive: true });
  fs.writeFileSync(prWatchArtifact, "status: green\nstatus: comments\n", "utf8");

  const duplicatePrWatch = run(["run", "advance"], active.state.workspace_root, project.home);
  assert.notEqual(duplicatePrWatch.status, 0);
  assert.match(duplicatePrWatch.stderr, /invalid-route/);
  assert.equal(JSON.parse(fs.readFileSync(active.statePath, "utf8")).phase, "pr-watch");
});

test("architecture blocked routes to refactor while merge ambiguity fails closed", (t) => {
  const project = setupProject(t);
  const active = startRun(project, { workflow: "full-feature", runId: "review-contract" });
  const architecture = phaseFixture("architecture-review");
  updateRunState(active, "architecture-review", architecture.index);
  const architectureArtifact = path.join(active.runDir, architecture.phase.artifact);
  const markerLines = architecture.phase.required_markers.map(concreteMarker);
  fs.mkdirSync(path.dirname(architectureArtifact), { recursive: true });
  fs.writeFileSync(architectureArtifact, [
    "# Architecture Review",
    "",
    "## Reviewer 1",
    "reviewer-source: sub-agent",
    "verdict: approve",
    "",
    "## Reviewer 2",
    "reviewer-source: sub-agent",
    "verdict: approve",
    "",
    "## Overall",
    "verdict: blocked",
    "",
    "## Completion Gate",
    ...markerLines,
    "",
  ].join("\n"), "utf8");
  const architectureResult = run(["run", "advance"], active.state.workspace_root, project.home);
  assert.equal(architectureResult.status, 0, architectureResult.stderr || architectureResult.stdout);
  assert.equal(JSON.parse(fs.readFileSync(active.statePath, "utf8")).phase, "refactor");

  updateRunState(active, "architecture-review", architecture.index);
  fs.writeFileSync(architectureArtifact, [
    "# Architecture Review",
    "",
    "status: blocked",
    "",
    "## Completion Gate",
    ...markerLines,
    "",
  ].join("\n"), "utf8");
  const statusBlocked = run(["run", "advance"], active.state.workspace_root, project.home);
  assert.equal(statusBlocked.status, 0, statusBlocked.stderr || statusBlocked.stdout);
  assert.equal(JSON.parse(fs.readFileSync(active.statePath, "utf8")).phase, "refactor");

  const merge = phaseFixture("merge-approval");
  updateRunState(active, "merge-approval", merge.index);
  const mergeArtifact = path.join(active.runDir, merge.phase.artifact);
  fs.writeFileSync(mergeArtifact, "# Merge Approval\n\nstatus: blocked\nverdict: approve\n", "utf8");
  const mergeResult = run(["run", "advance"], active.state.workspace_root, project.home);
  assert.notEqual(mergeResult.status, 0);
  assert.match(mergeResult.stderr, /route blocked|invalid-route/);
});

test("multi-review and gates cannot advance without explicit routes", () => {
  const source = fs.readFileSync(CLI, "utf8");
  const start = source.indexOf("function nextPhaseIndex");
  const end = source.indexOf("function syncRouteArtifacts", start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const evaluate = new Function(`${source.slice(start, end)}\nreturn nextPhaseIndex;`)();

  assert.throws(
    () => evaluate({ phase_index: 0 }, [{ id: "multi-review" }], { id: "multi-review", multi_review: true }, "unused"),
    /requires explicit routes/,
  );
  assert.throws(
    () => evaluate({ phase_index: 0 }, [{ id: "gates" }], { id: "gates" }, "unused"),
    /requires explicit routes/,
  );
  assert.equal(
    evaluate({ phase_index: 0 }, [{ id: "ordinary" }, { id: "next" }], { id: "ordinary" }, "unused"),
    1,
  );
});

test("artifact freshness falls through empty and invalid timestamps", (t) => {
  const project = setupProject(t);
  const active = startRun(project, { runId: "freshness-contract" });
  const artifact = path.join(active.runDir, "design.md");
  fs.writeFileSync(artifact, "# Design\n", "utf8");
  const old = new Date(Date.now() - 60_000);
  fs.utimesSync(artifact, old, old);
  updateRunState(active, "design", 0, {
    phase_entered_at: "not-a-date",
    updated_at: "",
    started_at: new Date(Date.now() + 60_000).toISOString(),
  });

  const status = run(["status", "--format", "hook"], active.state.workspace_root, project.home);
  assert.equal(status.status, 0, status.stderr || status.stdout);
  assert.equal(JSON.parse(status.stdout).reason, "stale_artifact");
});

test("generated completion gate prompts require normal unfenced marker lines", (t) => {
  const project = setupProject(t, {
    localSkills: [{ name: "project-policy", routingMetadata: "activation: always" }],
  });
  const prompt = fs.readFileSync(
    path.join(project.root, ".agent-flow", "prompts", "red.md"),
    "utf8",
  );
  assert.match(prompt, /normal, unfenced Markdown/);
  assert.match(prompt, /project-local-skills-used: project-policy/);
  assert.doesNotMatch(prompt, /project-local-skills-used: <comma-separated/);
  const localBlock = prompt.slice(
    prompt.indexOf("When this block appears"),
    prompt.indexOf("If this block is absent"),
  );
  assert.match(localBlock, /## Completion Gate/);
  assert.doesNotMatch(localBlock, /```/);
});
