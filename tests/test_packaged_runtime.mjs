import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { hashSkillTree } from "../lib/skill-selection.mjs";

const KIT_ROOT = fileURLToPath(new URL("..", import.meta.url));

test("packed npx install runs without PyYAML and pins launcher plus worktree skill snapshot", {
  skip: process.platform === "win32",
  timeout: 60_000,
}, (t) => {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-packed-runtime-"));
  t.after(() => fs.rmSync(sandbox, { recursive: true, force: true }));
  const packRoot = path.join(sandbox, "pack");
  const home = path.join(sandbox, "home");
  const cache = path.join(sandbox, "npm-cache");
  const fakeBin = path.join(sandbox, "fake-bin");
  const pythonLog = path.join(sandbox, "python-invoked.log");
  const staleLog = path.join(sandbox, "stale-agent-flow.log");
  const realPython = resolveRealPython();
  for (const directory of [packRoot, home, cache, fakeBin]) {
    fs.mkdirSync(directory, { recursive: true });
  }
  for (const binary of [
    "python",
    "python3",
    "python3.10",
    "python3.11",
    "python3.12",
    "python3.13",
    "python3.14",
  ]) {
    writeExecutable(
      path.join(fakeBin, binary),
      `#!/bin/sh\nprintf '%s\\n' "$0" >> "$AGENT_FLOW_TEST_PYTHON_LOG"\nexit 97\n`,
    );
  }
  writeExecutable(
    path.join(fakeBin, "agent-flow"),
    `#!/bin/sh\nprintf '%s\\n' "$0" >> "$AGENT_FLOW_TEST_STALE_LOG"\nexit 91\n`,
  );
  const env = {
    ...process.env,
    AGENT_FLOW_SKIP_CODEX_TRUST: "1",
    AGENT_FLOW_TEST_PYTHON_LOG: pythonLog,
    AGENT_FLOW_TEST_STALE_LOG: staleLog,
    HOME: home,
    PATH: `${fakeBin}${path.delimiter}${process.env.PATH ?? ""}`,
    PYTHON: path.join(fakeBin, "python"),
    PYTHONPATH: "",
    PYTHON_EXECUTABLE: path.join(fakeBin, "python"),
    npm_config_cache: cache,
  };
  delete env.VIRTUAL_ENV;

  const packed = spawnSync("npm", ["pack", "--json", "--pack-destination", packRoot], {
    cwd: KIT_ROOT,
    encoding: "utf8",
    env,
  });
  assert.equal(packed.status, 0, packed.stderr || packed.stdout);
  const packResult = JSON.parse(packed.stdout)[0];
  const packedPaths = new Set(packResult.files.map((entry) => entry.path));
  const pyproject = fs.readFileSync(path.join(KIT_ROOT, "pyproject.toml"), "utf8");
  assert.match(pyproject, /^agent-flow = "agent_flow\.cli:main"$/m);
  assert.match(pyproject, /^agent-flow-python = "agent_flow\.cli:main"$/m);
  assert.equal(packedPaths.has("lib/yaml-runtime-bundled.cjs"), true);
  assert.equal(packedPaths.has("lib/yaml-runtime-LICENSE"), true);
  assert.equal(packedPaths.has("lib/architecture-lint.mjs"), true);
  assert.equal(packedPaths.has("lib/native-gates.mjs"), true);
  assert.equal(packedPaths.has("lib/native-experiment.mjs"), true);
  assert.equal([...packedPaths].some((entry) => entry.endsWith(".tmp")), false);
  assert.equal([...packedPaths].some((entry) => entry.endsWith(".pyc")), false);
  assert.equal([...packedPaths].some((entry) => entry.includes("/__pycache__/")), false);
  const tarball = path.join(packRoot, packResult.filename);

  const projects = [
    { root: path.join(sandbox, "project-commonjs"), packageJson: null, runId: "packaged-runtime-commonjs" },
    { root: path.join(sandbox, "project-module"), packageJson: { type: "module" }, runId: "packaged-runtime-module" },
    { root: path.join(sandbox, "project-python-origin"), packageJson: null, runId: "packaged-runtime-python", pythonOnly: true },
  ];
  const nodeProjects = projects.filter((project) => project.pythonOnly !== true);
  for (const project of projects) {
    fs.mkdirSync(project.root);
    git(project.root, ["init", "-b", "main"]);
    git(project.root, ["config", "user.email", "test@example.com"]);
    git(project.root, ["config", "user.name", "Test User"]);
    fs.writeFileSync(path.join(project.root, "README.md"), "fixture\n", "utf8");
    if (project.packageJson) {
      fs.writeFileSync(path.join(project.root, "package.json"), `${JSON.stringify(project.packageJson)}\n`, "utf8");
    }
    git(project.root, ["add", "."]);
    git(project.root, ["commit", "-m", "init"]);
    const pythonRuntime = path.join(project.root, ".agent-flow", "runtime", "python");
    const staleRuntimeEntries = [
      path.join(pythonRuntime, "sitecustomize.py"),
      path.join(pythonRuntime, "yaml", "retired_loader.py"),
      path.join(pythonRuntime, "retired_package", "payload.py"),
    ];
    for (const stale of staleRuntimeEntries) {
      fs.mkdirSync(path.dirname(stale), { recursive: true });
      fs.writeFileSync(stale, "raise SystemExit(77)\n", "utf8");
    }

    const install = spawnSync(
      "npm",
      ["exec", "--yes", `--package=${tarball}`, "--", "agent-flow", "install", "--profile", "generic"],
      { cwd: project.root, encoding: "utf8", env },
    );
    assert.equal(install.status, 0, install.stderr || install.stdout);
    assert.match(install.stdout, /next_command: \.\/\.agent-flow\/bin\/agent-flow status/);
    assert.equal(fs.existsSync(path.join(project.root, "node_modules")), false);
    for (const stale of staleRuntimeEntries) assert.equal(fs.existsSync(stale), false, stale);
  }
  assert.equal(fs.existsSync(pythonLog), false, "public Node install invoked Python");
  assert.equal(fs.existsSync(staleLog), false, "install invoked a stale global agent-flow");

  const pinnedInstall = spawnSync(
    path.join(projects[0].root, ".agent-flow", "bin", "agent-flow"),
    ["install"],
    { cwd: projects[0].root, encoding: "utf8", env },
  );
  assert.notEqual(pinnedInstall.status, 0);
  assert.match(pinnedInstall.stderr, /pinned project runtime cannot install/);

  fs.rmSync(cache, { recursive: true, force: true });
  const noDetection = spawnSync(process.execPath, ["--no-experimental-detect-module", "--version"], {
    encoding: "utf8",
  }).status === 0;
  const launcherEnv = noDetection
    ? {
        ...env,
        NODE_OPTIONS: [env.NODE_OPTIONS, "--no-experimental-detect-module"].filter(Boolean).join(" "),
      }
    : env;
  const experimentEnv = {
    ...launcherEnv,
    AGENT_FLOW_LEDGER_MODE: "ledger-selective",
    AGENT_FLOW_EXPERIMENT_ID: "packaged-runtime-pilot",
    AGENT_FLOW_EXPERIMENT_MODEL_ID: "packaged-model",
    AGENT_FLOW_EXPERIMENT_TOOL_PERMISSIONS_SHA256: "a".repeat(64),
    AGENT_FLOW_EXPERIMENT_SYSTEM_PROMPT_SHA256: "b".repeat(64),
    AGENT_FLOW_EXPERIMENT_CAPS_SHA256: "c".repeat(64),
  };
  for (const project of nodeProjects) {
    const launcher = path.join(project.root, ".agent-flow", "bin", "agent-flow");
    const launcherSource = fs.readFileSync(launcher, "utf8");
    const kit = JSON.parse(fs.readFileSync(path.join(project.root, ".agent-flow", "kit.json"), "utf8"));
    assert.equal(kit.project_runtime_contract.version, 2);
    assert.equal(kit.project_runtime_contract.launcher.path, ".agent-flow/bin/agent-flow");
    assert.equal(kit.project_runtime_contract.python_runtime.root, ".agent-flow/runtime/python");
    assert.equal(kit.project_runtime_contract_commitment_version, 2);
    assert.match(kit.project_runtime_contract_commitment, /^[0-9a-f]{64}$/);
    assert.equal(
      fs.existsSync(path.join(project.root, ".agent-flow", "runtime", "node", "profiles", "generic.yaml")),
      true,
    );
    assert.notEqual(fs.statSync(launcher).mode & 0o111, 0);
    assert.match(launcherSource, /await import\("node:fs"\)/);
    assert.doesNotMatch(launcherSource, /^\s*import\s/m);
    assert.doesNotMatch(launcherSource, /\brequire\s*\(/);
    const architectureLint = spawnSync(launcher, ["architecture-lint", "--profile", "generic"], {
      cwd: project.root,
      encoding: "utf8",
      env: launcherEnv,
    });
    assert.equal(architectureLint.status, 0, architectureLint.stderr || architectureLint.stdout);
    assert.equal(architectureLint.stdout.trim(), "generic: architecture lint passed");
    const gateRunDir = path.join(project.root, ".agent-flow", "runs", "packaged-native-gates");
    const gates = spawnSync(launcher, [
      "gates",
      "--profile",
      "generic",
      "--run-dir",
      gateRunDir,
    ], {
      cwd: project.root,
      encoding: "utf8",
      env: launcherEnv,
    });
    assert.equal(gates.status, 0, gates.stderr || gates.stdout);
    assert.equal(gates.stdout.trim(), "generic: 1/1 gates passed");
    assert.equal(
      JSON.parse(fs.readFileSync(path.join(gateRunDir, "artifacts", "gate-results.json"), "utf8")).status,
      "green",
    );
    const start = spawnSync(launcher, [
      "run",
      "start",
      "--task",
      `packaged runtime bridge ${project.runId}`,
      "--workflow",
      "default",
      "--run-id",
      project.runId,
    ], { cwd: project.root, encoding: "utf8", env: experimentEnv });
    assert.equal(start.status, 0, start.stderr || start.stdout);

    const worktree = start.stdout.match(/^workspace_root: (.+)$/m)?.[1];
    assert.ok(worktree);
    const commonDirResult = spawnSync("git", ["rev-parse", "--git-common-dir"], {
      cwd: project.root,
      encoding: "utf8",
    });
    assert.equal(commonDirResult.status, 0, commonDirResult.stderr || commonDirResult.stdout);
    const commonDir = path.resolve(project.root, commonDirResult.stdout.trim());
    const state = JSON.parse(fs.readFileSync(path.join(
      commonDir,
      "agent-flow",
      "worktrees",
      path.basename(worktree),
      ".agent-flow",
      "state",
      "current-run.json",
    ), "utf8"));
    const activeGateRun = "active-worktree-gates";
    const activeGates = spawnSync(launcher, [
      "gates",
      "--profile",
      "generic",
      "--run-dir",
      activeGateRun,
    ], {
      cwd: project.root,
      encoding: "utf8",
      env: experimentEnv,
    });
    assert.equal(activeGates.status, 0, activeGates.stderr || activeGates.stdout);
    assert.equal(fs.existsSync(path.join(worktree, activeGateRun, "artifacts", "gate-results.json")), true);
    assert.equal(fs.existsSync(path.join(project.root, activeGateRun)), false);
    assert.equal(
      fs.realpathSync.native(path.join(worktree, ".agent-flow", "bin")),
      fs.realpathSync.native(path.join(project.root, ".agent-flow", "bin")),
    );
    assert.equal(
      fs.realpathSync.native(path.join(worktree, ".agent-flow", "skills")),
      fs.realpathSync.native(path.join(project.root, ".agent-flow", "skills")),
    );
    const indexPath = path.join(project.root, ".agent-flow", "skills", "index.json");
    const index = JSON.parse(fs.readFileSync(indexPath, "utf8"));
    assert.deepEqual(
      fs.readFileSync(path.join(worktree, ".agent-flow", "skills", "index.json")),
      fs.readFileSync(indexPath),
    );
    const indexedByName = new Map(index.skills.map((skill) => [skill.name, skill]));
    for (const skill of index.skills) {
      const skillRoot = path.dirname(path.join(project.root, skill.path));
      assert.equal(hashSkillTree(skillRoot), skill.tree_hash, skill.name);
      assert.equal(hashSkillTree(path.dirname(path.join(worktree, skill.path))), skill.tree_hash, skill.name);
    }
    for (const link of index.links) {
      const skill = indexedByName.get(link.name);
      assert.ok(skill, link.name);
      const worktreeLink = path.join(worktree, link.path);
      assert.equal(hashSkillTree(fs.realpathSync.native(worktreeLink)), skill.tree_hash, `${link.host}:${link.name}`);
    }

    const worktreeLauncher = path.join(worktree, ".agent-flow", "bin", "agent-flow");
    const status = spawnSync(worktreeLauncher, ["status"], {
      cwd: worktree,
      encoding: "utf8",
      env: experimentEnv,
    });
    assert.equal(status.status, 0, status.stderr || status.stdout);
    assert.match(status.stdout, /next_command: \.\/\.agent-flow\/bin\/agent-flow run next/);

    const usage = spawnSync(worktreeLauncher, [
      "experiment",
      "record-usage",
      "--run-dir", state.run_dir,
      "--event-id", `packaged.${project.runId}.design.0`,
      "--generated-at", "2026-07-11T00:00:00.000Z",
      "--scope", "phase",
      "--phase-id", "design",
      "--round", "0",
      "--model-id", "packaged-model",
      "--input-tokens", "100",
      "--output-tokens", "20",
      "--additional-tokens", "5",
      "--latency-ms", "50",
      "--estimated-cost-usd", "0.01",
    ], {
      cwd: worktree,
      encoding: "utf8",
      env: experimentEnv,
    });
    assert.equal(usage.status, 0, usage.stderr || usage.stdout);
    assert.deepEqual(JSON.parse(usage.stdout), {
      control_evidence_status: "summary-only",
      evidence_status: "summary-only",
      model_id: "packaged-model",
      ok: true,
      phase_id: "design",
      recorded: true,
    });
    assert.equal(
      fs.readFileSync(
        path.resolve(project.root, state.run_dir, "artifacts", "execution-ledger", "usage.jsonl"),
        "utf8",
      ).split(/\r?\n/).filter(Boolean).length,
      1,
    );

    fs.appendFileSync(launcher, "\n// launcher tamper\n", "utf8");
    const launcherTamper = spawnSync(launcher, ["status"], {
      cwd: project.root,
      encoding: "utf8",
      env: launcherEnv,
    });
    assert.notEqual(launcherTamper.status, 0);
    assert.match(launcherTamper.stderr, /launcher changed after install/);
    fs.writeFileSync(launcher, launcherSource, { encoding: "utf8", mode: 0o755 });

    const runtime = path.join(project.root, ".agent-flow", "runtime", "node", "bin", "agent-flow-kit.mjs");
    const runtimeBytes = fs.readFileSync(runtime);
    fs.appendFileSync(runtime, "\n// runtime tamper\n", "utf8");
    const runtimeTamper = spawnSync(launcher, ["status"], {
      cwd: project.root,
      encoding: "utf8",
      env: launcherEnv,
    });
    assert.notEqual(runtimeTamper.status, 0);
    assert.match(runtimeTamper.stderr, /runtime changed after install/);
    fs.writeFileSync(runtime, runtimeBytes);
  }
  const pythonProject = projects.find((project) => project.pythonOnly === true);
  const pythonRuntimeRoot = path.join(pythonProject.root, ".agent-flow", "runtime", "python");
  const packagedPrompt = path.join(
    pythonRuntimeRoot,
    "agent_flow",
    "templates",
    "_shared",
    "review",
    "architecture-design.md",
  );
  assert.equal(fs.existsSync(packagedPrompt), true);
  const pythonEnv = {
    ...launcherEnv,
    AGENT_FLOW_ADAPTER: "generic",
    AGENT_FLOW_GENERIC_MODE: "emit",
    PYTHONDONTWRITEBYTECODE: "1",
    PYTHONNOUSERSITE: "1",
    PYTHONPATH: pythonRuntimeRoot,
  };
  const yamlProbe = spawnSync(realPython, [
    "-c",
    "import pathlib,yaml,sys; root=pathlib.Path(sys.argv[1]).resolve(); module=pathlib.Path(yaml.__file__).resolve(); raise SystemExit(0 if module.is_relative_to(root) else 1)",
    pythonRuntimeRoot,
  ], { cwd: pythonProject.root, encoding: "utf8", env: pythonEnv });
  assert.equal(yamlProbe.status, 0, yamlProbe.stderr || yamlProbe.stdout);
  const promptProbe = spawnSync(realPython, [
    "-c",
    "from pathlib import Path; from agent_flow.adapters.hosted import _review_angle_prompt; print(_review_angle_prompt(Path.cwd(), 'templates/_shared/review/architecture-design.md'))",
  ], { cwd: pythonProject.root, encoding: "utf8", env: pythonEnv });
  assert.equal(promptProbe.status, 0, promptProbe.stderr || promptProbe.stdout);
  assert.match(promptProbe.stdout, /architecture/i);
  const pythonStart = spawnSync(realPython, [
    "-m", "agent_flow.cli", "run", "packaged Python-origin status relay",
    "--root", pythonProject.root,
    "--workflow", "default",
  ], { cwd: pythonProject.root, encoding: "utf8", env: pythonEnv });
  assert.equal(pythonStart.status, 0, pythonStart.stderr || pythonStart.stdout);
  const pythonLauncher = path.join(pythonProject.root, ".agent-flow", "bin", "agent-flow");
  const pythonStatus = spawnSync(pythonLauncher, ["status"], {
    cwd: pythonProject.root,
    encoding: "utf8",
    env: launcherEnv,
  });
  assert.equal(pythonStatus.status, 0, pythonStatus.stderr || pythonStatus.stdout);
  assert.match(pythonStatus.stdout, /next_command: agent-flow-python continue/);
  assert.equal(fs.existsSync(pythonLog), false, "pinned launchers invoked a PATH-shadowed Python");
  assert.equal(fs.existsSync(staleLog), false, "pinned launchers invoked a stale global agent-flow");
});

function resolveRealPython() {
  for (const candidate of [
    process.env.PYTHON,
    "/opt/homebrew/bin/python3.14",
    "/opt/homebrew/bin/python3.13",
    "/opt/homebrew/bin/python3.12",
    "/opt/homebrew/bin/python3.11",
    "/opt/homebrew/bin/python3.10",
    "python3",
  ].filter(Boolean)) {
    const result = spawnSync(candidate, [
      "-c",
      "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)",
    ], { encoding: "utf8" });
    if (result.status === 0) return candidate;
  }
  throw new Error("Python >= 3.10 is required for packaged Python-origin runtime test");
}

function git(cwd, args) {
  const result = spawnSync("git", args, { cwd, encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr || result.stdout);
}

function writeExecutable(file, content) {
  fs.writeFileSync(file, content, { encoding: "utf8", mode: 0o755 });
}
