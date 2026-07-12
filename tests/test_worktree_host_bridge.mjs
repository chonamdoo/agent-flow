import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import { ensureWorktreeHostBridge } from "../lib/worktree-host-bridge.mjs";
import {
  acquireCodexConfigTransactionLock,
  releaseCodexConfigTransactionLock,
  tomlBasicStringInterior,
} from "../lib/codex-hook-trust.mjs";

process.env.AGENT_FLOW_SKIP_CODEX_TRUST ??= "1";

const KIT_ROOT = fileURLToPath(new URL("..", import.meta.url));
const CLI = path.join(KIT_ROOT, "bin", "agent-flow-kit.mjs");
const CONTEXT_PATHS = ["AGENTS.md", "CLAUDE.md"];
const REVIEWER_PATHS = [
  ".Codex/agents/code-reviewer.md",
  ".claude/agents/code-reviewer.md",
  ".omp/agents/code-reviewer.md",
];
const REGULAR_HOST_PATHS = [
  ".Codex/hooks.json",
  ".codex/hooks.json",
  ".claude/settings.json",
];
const SYMLINK_PATHS = [
  ".agent-flow/bin",
  ".agent-flow/skills",
  ".agents/skills",
  ".claude/skills",
  ".omp/extensions/agent-flow-hooks.ts",
  ".omp/skills",
];
const BRIDGE_PATHS = [...CONTEXT_PATHS, ...REVIEWER_PATHS, ...REGULAR_HOST_PATHS, ...SYMLINK_PATHS];
const MANAGED_HOOK_VERIFIER = (() => {
  const source = fs.readFileSync(path.join(KIT_ROOT, "bin", "agent-flow-kit.mjs"), "utf8");
  const match = source.match(/const MANAGED_HOOK_VERIFIER = \[\n([\s\S]*?)\n\]\.join\("\\n"\);/);
  assert.ok(match, "managed hook verifier source is unavailable");
  return match[1]
    .split("\n")
    .filter((line) => line.trim())
    .map((line) => JSON.parse(line.trim().replace(/,$/, "")))
    .join("\n");
})();

function shellQuote(value) {
  return `'${String(value).replaceAll("'", "'\\''")}'`;
}

function installedHookCommand(leader, scriptName) {
  const scriptPath = path.join(
    fs.realpathSync.native(leader),
    ".agent-flow",
    "scripts",
    "hooks",
    scriptName,
  );
  const digest = crypto.createHash("sha256").update(fs.readFileSync(scriptPath)).digest("hex");
  return [
    shellQuote("/usr/bin/python3"),
    "-I",
    "-c",
    shellQuote(MANAGED_HOOK_VERIFIER),
    shellQuote(Buffer.from(scriptPath, "utf8").toString("base64")),
    shellQuote(digest),
  ].join(" ");
}

function setTestEnvironment(t, values) {
  const previous = new Map(Object.keys(values).map((key) => [key, process.env[key]]));
  for (const [key, value] of Object.entries(values)) {
    if (value === null) delete process.env[key];
    else process.env[key] = value;
  }
  t.after(() => {
    for (const [key, value] of previous) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  });
}

function configureFakeCodex(t, parent, mode = "ok") {
  const binary = path.join(parent, "fake-codex");
  fs.copyFileSync(path.join(KIT_ROOT, "tests", "fixtures", "fake_codex_app_server.py"), binary);
  fs.chmodSync(binary, 0o755);
  const codexHome = path.join(parent, "codex-home");
  fs.mkdirSync(codexHome);
  setTestEnvironment(t, {
    AGENT_FLOW_SKIP_CODEX_TRUST: null,
    CODEX_CLI_PATH: binary,
    CODEX_HOME: codexHome,
    FAKE_CODEX_MODE: mode === "ok" ? null : mode,
    FAKE_CODEX_SCRIPT_MUTATION: null,
    FAKE_CODEX_SCRIPT_MUTATION_QUERY: null,
  });
  return { binary, codexHome };
}

function flowBlock(version) {
  return [
    "<!-- agent-flow:start -->",
    "## Agent Flow",
    `contract: ${version}`,
    "hosts: Claude/Codex/OMP",
    "<!-- agent-flow:end -->",
  ].join("\n");
}

function git(cwd, args, { allowFailure = false } = {}) {
  const result = spawnSync("git", args, { cwd, encoding: "utf8" });
  if (!allowFailure) assert.equal(result.status, 0, result.stderr || result.stdout);
  return result;
}

function setupRepository(
  t,
  {
    trackedContext = true,
    trackedContextBytes = null,
    trackedReviewerConflict = false,
    trackedHostSettings = true,
    seedInstalled = true,
  } = {},
) {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-host-bridge-"));
  t.after(() => fs.rmSync(parent, { recursive: true, force: true }));
  const leader = path.join(parent, "leader");
  fs.mkdirSync(leader);
  git(leader, ["init", "-b", "main"]);
  git(leader, ["config", "user.email", "test@example.com"]);
  git(leader, ["config", "user.name", "Test User"]);
  fs.writeFileSync(path.join(leader, "README.md"), "fixture\n", "utf8");
  fs.writeFileSync(path.join(leader, ".gitignore"), "kept-entry\n", "utf8");
  const tracked = ["README.md", ".gitignore"];
  if (trackedContext) {
    const context = trackedContextBytes ?? {
      "AGENTS.md": Buffer.from("# User AGENTS\nkeep-agents\n", "utf8"),
      "CLAUDE.md": Buffer.from(
        `# User CLAUDE\nkeep-before\n${flowBlock("base")}\nkeep-after\n`,
        "utf8",
      ),
    };
    fs.writeFileSync(path.join(leader, "AGENTS.md"), context["AGENTS.md"]);
    fs.writeFileSync(path.join(leader, "CLAUDE.md"), context["CLAUDE.md"]);
    tracked.push("AGENTS.md", "CLAUDE.md");
  }
  if (trackedReviewerConflict) {
    for (const relative of [".Codex/agents/code-reviewer.md", ".claude/agents/code-reviewer.md"]) {
      const destination = path.join(leader, relative);
      fs.mkdirSync(path.dirname(destination), { recursive: true });
      fs.writeFileSync(destination, `user-owned ${relative}\n`, "utf8");
      tracked.push(relative);
    }
  }
  if (trackedHostSettings) {
    const settings = path.join(leader, ".claude", "settings.json");
    fs.mkdirSync(path.dirname(settings), { recursive: true });
    fs.writeFileSync(settings, `${JSON.stringify(userClaudeSettings(), null, 2)}\n`, "utf8");
    tracked.push(".claude/settings.json");
  }
  git(leader, ["add", ...tracked]);
  git(leader, ["commit", "-m", "base"]);
  if (seedInstalled) seedInstalledLeader(leader, "v1");
  return { parent, leader };
}

function seedInstalledLeader(leader, version) {
  for (const scriptName of [
    "guard-worktree.sh",
    "guard-worktree-write.py",
    "guard-protected-branch.sh",
    "show-phase-status.sh",
    "comment-checker.py",
  ]) {
    const script = path.join(leader, ".agent-flow", "scripts", "hooks", scriptName);
    fs.mkdirSync(path.dirname(script), { recursive: true });
    fs.writeFileSync(script, `# ${scriptName}\n`, "utf8");
    fs.chmodSync(script, 0o755);
  }
  const files = new Map([
    ["AGENTS.md", `# Leader AGENTS\nleader-only\n${flowBlock(version)}\nleader-tail\n`],
    ["CLAUDE.md", `# Leader CLAUDE\nleader-only\n${flowBlock(version)}\nleader-tail\n`],
    [".Codex/agents/code-reviewer.md", `# Codex Reviewer\nreview: ${version}\n`],
    [
      ".claude/agents/code-reviewer.md",
      `---\nname: code-reviewer\ndescription: Review code\n---\n\n# Claude Reviewer\nreview: ${version}\n`,
    ],
    [".Codex/hooks.json", `${JSON.stringify(installedHookSettings(leader, version, "codex"), null, 2)}\n`],
    [".codex/hooks.json", `${JSON.stringify(installedHookSettings(leader, version, "codex-lower"), null, 2)}\n`],
    [".claude/settings.json", `${JSON.stringify(installedHookSettings(leader, version, "claude"), null, 2)}\n`],
    [".omp/extensions/agent-flow-hooks.ts", `export const version = "${version}";\n`],
    [".agent-flow/bin/agent-flow", "#!/usr/bin/env node\n"],
    [".agent-flow/skills/index.json", `${JSON.stringify({ version: 2, skills: [{ name: "demo", path: ".agent-flow/skills/demo/SKILL.md" }] }, null, 2)}\n`],
    [".agent-flow/skills/demo/SKILL.md", `# canonical ${version}\n`],
    [".agents/skills/demo/SKILL.md", `# codex ${version}\n`],
    [".claude/skills/demo/SKILL.md", `# claude ${version}\n`],
    [".omp/skills/demo/SKILL.md", `# omp ${version}\n`],
  ]);
  for (const [relative, content] of files) {
    const destination = path.join(leader, relative);
    fs.mkdirSync(path.dirname(destination), { recursive: true });
    fs.writeFileSync(destination, content, "utf8");
  }
  seedManagedHookKit(leader);
}

function seedManagedHookKit(leader) {
  const projection = [
    ["PostToolUse", "^(apply_patch|Write|Edit|MultiEdit|NotebookEdit|Eval|Python|Notebook|write|edit|multi_edit|multiedit|notebook_edit|notebookedit|eval|python|notebook)$", "command", "comment-checker.py"],
    ["PreToolUse", "Bash", "command", "guard-protected-branch.sh"],
    ["PreToolUse", "Bash", "command", "guard-worktree-write.py"],
    ["PreToolUse", "Bash", "command", "guard-worktree.sh"],
    ["PreToolUse", "^(apply_patch|Write|Edit|MultiEdit|NotebookEdit|Eval|Python|Notebook|write|edit|multi_edit|multiedit|notebook_edit|notebookedit|eval|python|notebook)$", "command", "guard-worktree-write.py"],
    ["Stop", "", "command", "show-phase-status.sh"],
  ].sort();
  const projectionHash = crypto.createHash("sha256")
    .update(JSON.stringify(projection))
    .digest("hex");
  const configs = Object.fromEntries(
    [".Codex/hooks.json", ".claude/settings.json", ".codex/hooks.json"]
      .map((relative) => [relative, { sha256: projectionHash }]),
  );
  const scripts = Object.fromEntries(
    [
      "comment-checker.py",
      "guard-protected-branch.sh",
      "guard-worktree-write.py",
      "guard-worktree.sh",
      "show-phase-status.sh",
    ].map((name) => {
      const relative = `.agent-flow/scripts/hooks/${name}`;
      const sha256 = crypto.createHash("sha256")
        .update(fs.readFileSync(path.join(leader, relative)))
        .digest("hex");
      return [relative, { sha256, mode: "executable" }];
    }),
  );
  const contract = { version: 2, configs, scripts };
  const skillPlanHash = "0".repeat(64);
  const commitment = crypto.createHash("sha256").update(JSON.stringify({
    version: 2,
    skill_plan_hash: skillPlanHash,
    configs: Object.entries(configs).sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)
      .map(([relative, entry]) => [relative, entry.sha256]),
    scripts: Object.entries(scripts).sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)
      .map(([relative, entry]) => [relative, entry.sha256, "executable"]),
  })).digest("hex");
  fs.writeFileSync(path.join(leader, ".agent-flow", "kit.json"), `${JSON.stringify({
    skill_plan_hash: skillPlanHash,
    managed_hook_contract: contract,
    managed_hook_contract_commitment_version: 2,
    managed_hook_contract_commitment: commitment,
  }, null, 2)}\n`, "utf8");
}

function userClaudeSettings() {
  return {
    theme: "dark",
    hooks: {
      PreToolUse: [
        {
          matcher: "Bash",
          hooks: [{ type: "command", command: "user-hook" }],
        },
      ],
    },
  };
}

function installedHookSettings(leader, version, host) {
  const config = host === "claude" ? userClaudeSettings() : { host };
  config.hooks ??= {};
  config.hooks.PreToolUse ??= [];
  const commandHook = (scriptName) => ({
    type: "command",
    command: installedHookCommand(leader, scriptName),
    bridgeVersion: version,
  });
  let bash = config.hooks.PreToolUse.find((entry) => entry.matcher === "Bash");
  if (!bash) {
    bash = { matcher: "Bash", hooks: [] };
    config.hooks.PreToolUse.push(bash);
  }
  bash.hooks.push(
    commandHook("guard-worktree.sh"),
    commandHook("guard-protected-branch.sh"),
    commandHook("guard-worktree-write.py"),
  );
  config.hooks.PreToolUse.push({
    matcher: "^(apply_patch|Write|Edit|MultiEdit|NotebookEdit|Eval|Python|Notebook|write|edit|multi_edit|multiedit|notebook_edit|notebookedit|eval|python|notebook)$",
    hooks: [commandHook("guard-worktree-write.py")],
  });
  config.hooks.PostToolUse = [
    {
      matcher: "^(apply_patch|Write|Edit|MultiEdit|NotebookEdit|Eval|Python|Notebook|write|edit|multi_edit|multiedit|notebook_edit|notebookedit|eval|python|notebook)$",
      hooks: [commandHook("comment-checker.py")],
    },
  ];
  config.hooks.Stop = [
    {
      hooks: [commandHook("show-phase-status.sh")],
    },
  ];
  return config;
}

function addWorktree(leader, destination, branch) {
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  git(leader, ["worktree", "add", "-b", branch, destination, "main"]);
}

function seedWorktreeCodexHooks(leader, worktree) {
  for (const relative of [".Codex/hooks.json", ".codex/hooks.json"]) {
    const destination = path.join(worktree, relative);
    fs.mkdirSync(path.dirname(destination), { recursive: true });
    fs.copyFileSync(path.join(leader, relative), destination);
  }
}

function activeNodeState(leader) {
  const commonDir = path.resolve(
    leader,
    git(leader, ["rev-parse", "--git-common-dir"]).stdout.trim(),
  );
  const runtimeRoot = path.join(commonDir, "agent-flow", "worktrees");
  const pointers = [path.join(leader, ".agent-flow", "state", "current-run.json")];
  if (fs.existsSync(runtimeRoot)) {
    for (const entry of fs.readdirSync(runtimeRoot, { withFileTypes: true })) {
      if (entry.isDirectory() && !entry.isSymbolicLink()) {
        pointers.push(path.join(runtimeRoot, entry.name, ".agent-flow", "state", "current-run.json"));
      }
    }
  }
  const active = pointers.flatMap((pointer) => {
    if (!fs.existsSync(pointer)) return [];
    const state = JSON.parse(fs.readFileSync(pointer, "utf8"));
    return !["complete", "aborted"].includes(state.status) && state.phase !== "complete" ? [state] : [];
  });
  assert.equal(active.length, 1);
  return active[0];
}

function bridgeManifest(leader, worktree) {
  const directory = path.join(leader, ".git", "agent-flow", "worktree-host-bridges");
  for (const name of fs.readdirSync(directory)) {
    const file = path.join(directory, name);
    const bytes = fs.readFileSync(file);
    const value = JSON.parse(bytes.toString("utf8"));
    if (value.workspace_root === fs.realpathSync.native(worktree)) return { file, bytes, value };
  }
  assert.fail(`manifest not found for ${worktree}`);
}

function assertBridge(leader, worktree, version = "v1") {
  for (const relative of [...CONTEXT_PATHS, ...REVIEWER_PATHS, ...REGULAR_HOST_PATHS]) {
    const destination = path.join(worktree, relative);
    assert.equal(fs.lstatSync(destination).isFile(), true, relative);
    assert.equal(fs.lstatSync(destination).isSymbolicLink(), false, relative);
  }
  for (const relative of SYMLINK_PATHS) {
    const destination = path.join(worktree, relative);
    assert.equal(fs.lstatSync(destination).isSymbolicLink(), true, relative);
    assert.equal(fs.realpathSync.native(destination), fs.realpathSync.native(path.join(leader, relative)), relative);
  }
  for (const relative of CONTEXT_PATHS) {
    if (version) {
      assert.match(fs.readFileSync(path.join(worktree, relative), "utf8"), new RegExp(`contract: ${version}`));
    }
  }
  assert.deepEqual(
    fs.readFileSync(path.join(worktree, ".Codex/agents/code-reviewer.md")),
    fs.readFileSync(path.join(leader, ".Codex/agents/code-reviewer.md")),
  );
  for (const relative of [".claude/agents/code-reviewer.md", ".omp/agents/code-reviewer.md"]) {
    assert.deepEqual(
      fs.readFileSync(path.join(worktree, relative)),
      fs.readFileSync(path.join(leader, ".claude/agents/code-reviewer.md")),
    );
  }
  for (const relative of REGULAR_HOST_PATHS) {
    assert.deepEqual(fs.readFileSync(path.join(worktree, relative)), fs.readFileSync(path.join(leader, relative)));
  }
}

function runPythonBridge(leader, worktree) {
  const script = [
    "from pathlib import Path",
    "from agent_flow.core.host_bridge import ensure_worktree_host_bridge",
    "import sys",
    "ensure_worktree_host_bridge(leader_root=Path(sys.argv[1]), worktree_root=Path(sys.argv[2]))",
  ].join("; ");
  return spawnSync("python3", ["-c", script, leader, worktree], {
    cwd: KIT_ROOT,
    encoding: "utf8",
    env: { ...process.env, PYTHONPATH: path.join(KIT_ROOT, "src") },
  });
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

function outsideFlowBlock(text) {
  return text.replace(/<!-- agent-flow:start -->[\s\S]*?<!-- agent-flow:end -->/, "<FLOW-BLOCK>");
}

test("Node exposes current workflow context and reviewers without overwriting tracked user bytes", (t) => {
  const { parent, leader } = setupRepository(t);
  const internal = path.join(leader, ".agent-flow", "worktrees", "feat-internal");
  const external = path.join(parent, "external");
  addWorktree(leader, internal, "feat/internal");
  addWorktree(leader, external, "feat/external");
  const expectedAgentsOutside = "# User AGENTS\nkeep-agents\n\n<FLOW-BLOCK>\n";
  const expectedClaudeOutside = "# User CLAUDE\nkeep-before\n<FLOW-BLOCK>\nkeep-after\n";

  ensureWorktreeHostBridge(leader, internal);
  ensureWorktreeHostBridge(leader, external);
  const manifestBefore = bridgeManifest(leader, external).bytes;
  const excludeBefore = fs.readFileSync(path.join(leader, ".git", "info", "exclude"));
  const fileBytesBefore = new Map(
    [...CONTEXT_PATHS, ...REVIEWER_PATHS, ...REGULAR_HOST_PATHS].map((relative) => [
      relative,
      fs.readFileSync(path.join(external, relative)),
    ]),
  );
  ensureWorktreeHostBridge(leader, external);

  assertBridge(leader, internal);
  assertBridge(leader, external);
  assert.equal(outsideFlowBlock(fs.readFileSync(path.join(external, "AGENTS.md"), "utf8")), expectedAgentsOutside);
  assert.equal(outsideFlowBlock(fs.readFileSync(path.join(external, "CLAUDE.md"), "utf8")), expectedClaudeOutside);
  assert.deepEqual(bridgeManifest(leader, external).bytes, manifestBefore);
  assert.deepEqual(fs.readFileSync(path.join(leader, ".git", "info", "exclude")), excludeBefore);
  for (const [relative, bytes] of fileBytesBefore) {
    assert.deepEqual(fs.readFileSync(path.join(external, relative)), bytes, relative);
  }
  const status = git(external, ["status", "--short", "--untracked-files=all"]).stdout.trimEnd().split("\n").sort();
  assert.deepEqual(status, [" M .claude/settings.json", " M AGENTS.md", " M CLAUDE.md"]);
  assert.deepEqual(fs.readdirSync(path.join(external, ".agent-flow")).sort(), ["bin", "skills"]);
  const exclude = fs.readFileSync(path.join(leader, ".git", "info", "exclude"), "utf8");
  for (const relative of BRIDGE_PATHS) assert.match(exclude, new RegExp(`/${relative.replaceAll(".", "\\.")}`));
});

test("Node refreshes only owned blocks and files, then rejects context and manifest tampering", (t) => {
  const { parent, leader } = setupRepository(t);
  const worktree = path.join(parent, "refresh");
  addWorktree(leader, worktree, "feat/refresh");
  ensureWorktreeHostBridge(leader, worktree);
  fs.appendFileSync(path.join(worktree, "AGENTS.md"), "user-after-bridge\n", "utf8");
  const outsideBefore = outsideFlowBlock(fs.readFileSync(path.join(worktree, "AGENTS.md"), "utf8"));
  seedInstalledLeader(leader, "v2");

  ensureWorktreeHostBridge(leader, worktree);
  assertBridge(leader, worktree, "v2");
  assert.equal(outsideFlowBlock(fs.readFileSync(path.join(worktree, "AGENTS.md"), "utf8")), outsideBefore);

  const agents = path.join(worktree, "AGENTS.md");
  fs.writeFileSync(agents, fs.readFileSync(agents, "utf8").replace("contract: v2", "contract: tampered"), "utf8");
  assert.throws(
    () => ensureWorktreeHostBridge(leader, worktree),
    /managed worktree context block was modified/,
  );
  assert.match(fs.readFileSync(agents, "utf8"), /contract: tampered/);
  fs.writeFileSync(agents, fs.readFileSync(agents, "utf8").replace("contract: tampered", "contract: v2"), "utf8");

  const { file, value } = bridgeManifest(leader, worktree);
  value.links[0].hash = "0".repeat(64);
  fs.writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`, "utf8");
  assert.throws(() => ensureWorktreeHostBridge(leader, worktree), /hash mismatch/);
  assert.equal(JSON.parse(fs.readFileSync(file, "utf8")).links[0].hash, "0".repeat(64));
});

test("Node and Python reject raw, wrapped, and forged managed hook bridge sources", (t) => {
  for (const runtime of ["node", "python"]) {
    for (const commandKind of ["raw", "forged", "wrapped", "double-quoted", "variable-path"]) {
      const { parent, leader } = setupRepository(t);
      const worktree = path.join(parent, `${runtime}-${commandKind}-hook`);
      addWorktree(leader, worktree, `feat/${runtime}-${commandKind}-hook`);
      const config = path.join(leader, ".Codex", "hooks.json");
      const settings = JSON.parse(fs.readFileSync(config, "utf8"));
      const hook = settings.hooks.PreToolUse[0].hooks[0];
      const suffix = hook.command.match(/ '([A-Za-z0-9+/=]+)' '([0-9a-f]{64})'$/);
      assert.ok(suffix?.index);
      const rawPath = Buffer.from(suffix[1], "base64").toString("utf8");
      hook.command = commandKind === "raw"
        ? rawPath
        : commandKind === "forged"
          ? `'/usr/bin/python3' -I -c 'pass'${hook.command.slice(suffix.index)}`
          : commandKind === "wrapped"
            ? `/bin/bash "${rawPath}"`
            : commandKind === "double-quoted"
              ? `${hook.command.slice(0, suffix.index)} "${suffix[1]}" "${suffix[2]}"`
              : `P='${suffix[1]}'; ${hook.command.slice(0, suffix.index)} "$P" '${suffix[2]}'`;
      fs.writeFileSync(config, `${JSON.stringify(settings, null, 2)}\n`, "utf8");

      if (runtime === "node") {
        assert.throws(
          () => ensureWorktreeHostBridge(leader, worktree),
          /managed hook command is not immutable/,
        );
      } else {
        const result = runPythonBridge(leader, worktree);
        assert.notEqual(result.status, 0, result.stderr || result.stdout);
        assert.match(result.stderr, /managed hook command is not immutable/);
      }
      assert.equal(fs.existsSync(path.join(worktree, ".omp")), false);
    }
  }
});

test("Node fails before mutation for tracked reviewer conflicts and malformed or symlinked context", (t) => {
  const conflict = setupRepository(t, { trackedReviewerConflict: true });
  const conflictingWorktree = path.join(conflict.parent, "reviewer-conflict");
  addWorktree(conflict.leader, conflictingWorktree, "feat/reviewer-conflict");
  const agentsBefore = fs.readFileSync(path.join(conflictingWorktree, "AGENTS.md"));
  assert.throws(
    () => ensureWorktreeHostBridge(conflict.leader, conflictingWorktree),
    /unmanaged host bridge path already exists/,
  );
  assert.deepEqual(fs.readFileSync(path.join(conflictingWorktree, "AGENTS.md")), agentsBefore);
  assert.equal(fs.existsSync(path.join(conflictingWorktree, ".omp")), false);

  const malformed = setupRepository(t);
  const malformedWorktree = path.join(malformed.parent, "malformed");
  addWorktree(malformed.leader, malformedWorktree, "feat/malformed");
  fs.appendFileSync(path.join(malformedWorktree, "AGENTS.md"), "<!-- agent-flow:start -->\n", "utf8");
  assert.throws(
    () => ensureWorktreeHostBridge(malformed.leader, malformedWorktree),
    /malformed agent-flow context block/,
  );
  assert.equal(fs.existsSync(path.join(malformedWorktree, ".omp")), false);

  const unsafe = setupRepository(t);
  const unsafeWorktree = path.join(unsafe.parent, "unsafe");
  addWorktree(unsafe.leader, unsafeWorktree, "feat/unsafe");
  const outside = path.join(unsafe.parent, "outside");
  fs.mkdirSync(outside);
  fs.symlinkSync(outside, path.join(unsafeWorktree, ".omp"), "dir");
  assert.throws(
    () => ensureWorktreeHostBridge(unsafe.leader, unsafeWorktree),
    /unsafe host bridge parent path/,
  );
  assert.equal(fs.realpathSync.native(path.join(unsafeWorktree, ".omp")), fs.realpathSync.native(outside));
});

test("Node rejects source symlinks and rolls every bridge artifact back on a late manifest failure", (t) => {
  const unsafe = setupRepository(t);
  const unsafeWorktree = path.join(unsafe.parent, "source-symlink");
  addWorktree(unsafe.leader, unsafeWorktree, "feat/source-symlink");
  const realSkills = path.join(unsafe.parent, "real-skills");
  fs.renameSync(path.join(unsafe.leader, ".claude", "skills"), realSkills);
  fs.symlinkSync(realSkills, path.join(unsafe.leader, ".claude", "skills"), "dir");
  assert.throws(
    () => ensureWorktreeHostBridge(unsafe.leader, unsafeWorktree),
    /unsafe leader host bridge source symlink/,
  );
  assert.equal(fs.existsSync(path.join(unsafeWorktree, ".omp")), false);

  const rollback = setupRepository(t);
  const rollbackWorktree = path.join(rollback.parent, "rollback");
  addWorktree(rollback.leader, rollbackWorktree, "feat/rollback");
  const before = new Map(
    BRIDGE_PATHS
      .filter((relative) => {
        const destination = path.join(rollbackWorktree, relative);
        return fs.existsSync(destination) && fs.lstatSync(destination).isFile();
      })
      .map((relative) => [relative, fs.readFileSync(path.join(rollbackWorktree, relative))]),
  );
  const excludePath = path.join(rollback.leader, ".git", "info", "exclude");
  const excludeBefore = fs.readFileSync(excludePath);
  const linkSync = fs.linkSync;
  fs.linkSync = (source, destination) => {
    if (String(destination).includes(`${path.sep}worktree-host-bridges${path.sep}`)) {
      throw new Error("injected manifest failure");
    }
    return linkSync(source, destination);
  };
  try {
    assert.throws(
      () => ensureWorktreeHostBridge(rollback.leader, rollbackWorktree),
      /injected manifest failure/,
    );
  } finally {
    fs.linkSync = linkSync;
  }
  for (const relative of BRIDGE_PATHS) {
    if (before.has(relative)) {
      assert.deepEqual(fs.readFileSync(path.join(rollbackWorktree, relative)), before.get(relative), relative);
    } else {
      assert.equal(fs.existsSync(path.join(rollbackWorktree, relative)), false, relative);
    }
  }
  assert.deepEqual(fs.readFileSync(excludePath), excludeBefore);
  const manifestDirectory = path.join(rollback.leader, ".git", "agent-flow", "worktree-host-bridges");
  assert.equal(fs.existsSync(manifestDirectory), false);
});

test("Node rollback preserves same-content paths whose inode or link ownership changed", (t) => {
  const { parent, leader } = setupRepository(t);
  const worktree = path.join(parent, "rollback-ownership");
  addWorktree(leader, worktree, "feat/rollback-ownership");
  const replacedFile = path.join(worktree, ".Codex", "agents", "code-reviewer.md");
  const linkedFile = path.join(worktree, ".Codex", "hooks.json");
  const linkAlias = path.join(parent, "hook-settings-alias.json");
  const replacedSymlink = path.join(worktree, ".agents", "skills");
  const linkSync = fs.linkSync;
  fs.linkSync = (source, destination) => {
    if (String(destination).includes(`${path.sep}worktree-host-bridges${path.sep}`)) {
      const bytes = fs.readFileSync(replacedFile);
      const mode = fs.statSync(replacedFile).mode & 0o777;
      const replacement = `${replacedFile}.replacement`;
      fs.writeFileSync(replacement, bytes, { mode });
      fs.renameSync(replacement, replacedFile);
      linkSync(linkedFile, linkAlias);
      const target = fs.readlinkSync(replacedSymlink);
      fs.unlinkSync(replacedSymlink);
      fs.symlinkSync(target, replacedSymlink, "dir");
      throw new Error("injected manifest failure after ownership change");
    }
    return linkSync(source, destination);
  };
  try {
    assert.throws(
      () => ensureWorktreeHostBridge(leader, worktree),
      /injected manifest failure after ownership change/,
    );
  } finally {
    fs.linkSync = linkSync;
  }

  assert.equal(fs.existsSync(replacedFile), true);
  assert.equal(fs.existsSync(linkedFile), true);
  assert.equal(fs.statSync(linkedFile).nlink, 2);
  assert.equal(fs.lstatSync(replacedSymlink).isSymbolicLink(), true);
});

test("Node and Python fail closed when a bridge source has the wrong spec type", (t) => {
  const cases = [
    [".agents/skills", "file"],
    [".claude/skills", "file"],
    [".omp/skills", "file"],
    [".omp/extensions/agent-flow-hooks.ts", "directory"],
    [".Codex/agents/code-reviewer.md", "directory"],
    [".claude/agents/code-reviewer.md", "directory"],
  ];

  for (const [index, [relative, wrongType]] of cases.entries()) {
    const { parent, leader } = setupRepository(t);
    const nodeWorktree = path.join(parent, `node-wrong-type-${index}`);
    const pythonWorktree = path.join(parent, `python-wrong-type-${index}`);
    addWorktree(leader, nodeWorktree, `feat/node-wrong-type-${index}`);
    addWorktree(leader, pythonWorktree, `feat/python-wrong-type-${index}`);
    const source = path.join(leader, relative);
    const backup = path.join(parent, `source-backup-${index}`);
    fs.renameSync(source, backup);
    if (wrongType === "file") {
      fs.writeFileSync(source, "wrong source type\n", "utf8");
    } else {
      fs.mkdirSync(source);
    }

    const nodeAgentsBefore = fs.readFileSync(path.join(nodeWorktree, "AGENTS.md"));
    assert.throws(
      () => ensureWorktreeHostBridge(leader, nodeWorktree),
      /invalid leader host bridge source type/,
      `Node accepted ${relative} as a ${wrongType}`,
    );
    assert.deepEqual(fs.readFileSync(path.join(nodeWorktree, "AGENTS.md")), nodeAgentsBefore);
    assert.equal(fs.existsSync(path.join(nodeWorktree, ".omp")), false);

    const pythonAgentsBefore = fs.readFileSync(path.join(pythonWorktree, "AGENTS.md"));
    const python = runPythonBridge(leader, pythonWorktree);
    assert.notEqual(python.status, 0, `Python accepted ${relative} as a ${wrongType}`);
    assert.match(python.stderr, /invalid leader host bridge source type/);
    assert.deepEqual(fs.readFileSync(path.join(pythonWorktree, "AGENTS.md")), pythonAgentsBefore);
    assert.equal(fs.existsSync(path.join(pythonWorktree, ".omp")), false);

    fs.rmSync(source, { recursive: true, force: true });
    fs.renameSync(backup, source);
  }
});

test("Node and Python preserve tracked context bytes when appending the first flow block", (t) => {
  const trackedContextBytes = {
    "AGENTS.md": Buffer.from("# User AGENTS\r\nkeep-agents  \t", "utf8"),
    "CLAUDE.md": Buffer.from("# User CLAUDE\r\nkeep-claude \t\r\n", "utf8"),
  };
  const { parent, leader } = setupRepository(t, { trackedContextBytes });
  const nodeWorktree = path.join(parent, "node-crlf");
  const pythonWorktree = path.join(parent, "python-crlf");
  addWorktree(leader, nodeWorktree, "feat/node-crlf");
  addWorktree(leader, pythonWorktree, "feat/python-crlf");
  for (const relative of CONTEXT_PATHS) {
    assert.deepEqual(fs.readFileSync(path.join(nodeWorktree, relative)), trackedContextBytes[relative]);
    assert.deepEqual(fs.readFileSync(path.join(pythonWorktree, relative)), trackedContextBytes[relative]);
  }

  ensureWorktreeHostBridge(leader, nodeWorktree);
  const python = runPythonBridge(leader, pythonWorktree);
  assert.equal(python.status, 0, python.stderr || python.stdout);

  const expectedSuffixes = {
    "AGENTS.md": Buffer.from(`\r\n\r\n${flowBlock("v1")}\n`, "utf8"),
    "CLAUDE.md": Buffer.from(`\r\n${flowBlock("v1")}\n`, "utf8"),
  };
  for (const relative of CONTEXT_PATHS) {
    const expected = Buffer.concat([trackedContextBytes[relative], expectedSuffixes[relative]]);
    const nodeBytes = fs.readFileSync(path.join(nodeWorktree, relative));
    const pythonBytes = fs.readFileSync(path.join(pythonWorktree, relative));
    assert.deepEqual(nodeBytes, expected, `${relative} Node bytes`);
    assert.deepEqual(pythonBytes, expected, `${relative} Python bytes`);
    assert.deepEqual(nodeBytes, pythonBytes, `${relative} runtime parity`);
  }

  const nodeBeforeRefresh = new Map(
    CONTEXT_PATHS.map((relative) => [relative, fs.readFileSync(path.join(nodeWorktree, relative))]),
  );
  const pythonBeforeRefresh = new Map(
    CONTEXT_PATHS.map((relative) => [relative, fs.readFileSync(path.join(pythonWorktree, relative))]),
  );
  ensureWorktreeHostBridge(leader, nodeWorktree);
  const pythonRefresh = runPythonBridge(leader, pythonWorktree);
  assert.equal(pythonRefresh.status, 0, pythonRefresh.stderr || pythonRefresh.stdout);
  for (const relative of CONTEXT_PATHS) {
    assert.deepEqual(fs.readFileSync(path.join(nodeWorktree, relative)), nodeBeforeRefresh.get(relative));
    assert.deepEqual(fs.readFileSync(path.join(pythonWorktree, relative)), pythonBeforeRefresh.get(relative));
  }
});

test("Node and Python create byte-compatible bridges and accept each other's manifest", (t) => {
  const { parent, leader } = setupRepository(t);
  const nodeWorktree = path.join(parent, "node");
  const pythonWorktree = path.join(parent, "python");
  addWorktree(leader, nodeWorktree, "feat/node");
  addWorktree(leader, pythonWorktree, "feat/python");

  ensureWorktreeHostBridge(leader, nodeWorktree);
  const nodeManifestBeforePython = bridgeManifest(leader, nodeWorktree).bytes;
  const pythonOnNode = runPythonBridge(leader, nodeWorktree);
  assert.equal(pythonOnNode.status, 0, pythonOnNode.stderr || pythonOnNode.stdout);
  assert.deepEqual(bridgeManifest(leader, nodeWorktree).bytes, nodeManifestBeforePython);

  const pythonFirst = runPythonBridge(leader, pythonWorktree);
  assert.equal(pythonFirst.status, 0, pythonFirst.stderr || pythonFirst.stdout);
  const pythonManifestBeforeNode = bridgeManifest(leader, pythonWorktree).bytes;
  ensureWorktreeHostBridge(leader, pythonWorktree);
  assert.deepEqual(bridgeManifest(leader, pythonWorktree).bytes, pythonManifestBeforeNode);

  for (const relative of [...CONTEXT_PATHS, ...REVIEWER_PATHS, ...REGULAR_HOST_PATHS]) {
    assert.deepEqual(
      fs.readFileSync(path.join(nodeWorktree, relative)),
      fs.readFileSync(path.join(pythonWorktree, relative)),
      relative,
    );
  }
  for (const relative of SYMLINK_PATHS) {
    assert.equal(
      fs.readlinkSync(path.join(nodeWorktree, relative)),
      fs.readlinkSync(path.join(pythonWorktree, relative)),
      relative,
    );
  }
  const normalize = (bytes, worktree) => bytes
    .toString("utf8")
    .replaceAll(fs.realpathSync.native(worktree), "<WORKTREE>")
    .replace(/"manifest_hash": "[0-9a-f]{64}"/, '"manifest_hash": "<HASH>"');
  assert.equal(
    normalize(bridgeManifest(leader, nodeWorktree).bytes, nodeWorktree),
    normalize(bridgeManifest(leader, pythonWorktree).bytes, pythonWorktree),
  );
});

test("Node registers actual Codex worktree hook keys and Python preserves the same trust state", (t) => {
  const { parent, leader } = setupRepository(t);
  const worktree = path.join(parent, "codex-trusted");
  addWorktree(leader, worktree, "feat/codex-trusted");
  const { codexHome } = configureFakeCodex(t, parent);
  const configPath = path.join(codexHome, "config.toml");
  const userPrefix = '# user-owned bytes  \n[custom]\nvalue = "keep"\n';
  fs.writeFileSync(configPath, userPrefix, { encoding: "utf8", mode: 0o640 });

  ensureWorktreeHostBridge(leader, worktree);

  const afterNode = fs.readFileSync(configPath);
  const config = afterNode.toString("utf8");
  assert.equal(config.startsWith(userPrefix), true);
  assert.equal(config.includes(`[projects.${JSON.stringify(fs.realpathSync.native(worktree))}]`), true);
  assert.equal((config.match(/trusted_hash = "sha256:[0-9a-f]{64}"/g) ?? []).length, 6);
  assert.equal(fs.statSync(configPath).mode & 0o777, 0o640);
  assertBridge(leader, worktree);

  const python = runPythonBridge(leader, worktree);
  assert.equal(python.status, 0, python.stderr || python.stdout);
  assert.deepEqual(fs.readFileSync(configPath), afterNode);
});

test("Node and Python TOML encoders preserve C0 DEL quote and backslash values", () => {
  const value = "project-\u0000-\u0001-\b-\t-\n-\f-\r-\u001f-\u007f-\"-\\";
  const nodeEncoded = `"${tomlBasicStringInterior(value)}"`;
  const python = spawnSync(
    "python3",
    [
      "-c",
      "import sys; from agent_flow.core.codex_trust import _toml_string; sys.stdout.write(_toml_string(sys.stdin.read()))",
    ],
    {
      cwd: KIT_ROOT,
      encoding: "utf8",
      env: { ...process.env, PYTHONPATH: path.join(KIT_ROOT, "src") },
      input: value,
    },
  );
  assert.equal(python.status, 0, python.stderr || python.stdout);
  assert.equal(python.stdout, nodeEncoded);
  const parsed = parseTomlDocument(`[hooks.state.${nodeEncoded}]\nvalue = ${nodeEncoded}\n`);
  assert.equal(parsed.hooks.state[value].value, value);
});

test(
  "Python recovers Node worktree Codex trust after a hard kill",
  { skip: process.platform === "win32" },
  (t) => {
    const { parent, leader } = setupRepository(t);
    const worktree = path.join(parent, "codex-node-crash");
    addWorktree(leader, worktree, "feat/codex-node-crash");
    seedWorktreeCodexHooks(leader, worktree);
    const { codexHome } = configureFakeCodex(t, parent);
    const configPath = path.join(codexHome, "config.toml");
    const original = Buffer.from('# durable user bytes  \n[custom]\nvalue = "keep"\n', "utf8");
    fs.writeFileSync(configPath, original, { mode: 0o640 });
    const trustModule = pathToFileURL(path.join(KIT_ROOT, "lib", "codex-hook-trust.mjs")).href;
    const crashScript = [
      `import { ensureCodexWorktreeHookTrust } from ${JSON.stringify(trustModule)};`,
      "ensureCodexWorktreeHookTrust({ leaderRoot: process.argv[1], worktreeRoot: process.argv[2] });",
    ].join("\n");
    const crashed = spawnSync(
      process.execPath,
      ["--input-type=module", "--eval", crashScript, leader, worktree],
      {
        env: {
          ...process.env,
          AGENT_FLOW_TEST_HARD_KILL_AFTER_WORKTREE_CODEX_TRUST_WRITE: "1",
        },
        encoding: "utf8",
      },
    );
    assert.equal(crashed.signal, "SIGKILL", crashed.stderr || crashed.stdout);
    assert.notDeepEqual(fs.readFileSync(configPath), original);
    const transactionPath = path.join(leader, ".git", "agent-flow", "codex-worktree-trust.json");
    assert.equal(fs.existsSync(transactionPath), true);

    const recoveryScript = [
      "from pathlib import Path",
      "from agent_flow.core.codex_trust import ensure_codex_worktree_hook_trust",
      "import sys",
      "ensure_codex_worktree_hook_trust(leader_root=Path(sys.argv[1]), worktree_root=Path(sys.argv[2]))",
    ].join("; ");
    const recovered = spawnSync("python3", ["-c", recoveryScript, leader, worktree], {
      cwd: KIT_ROOT,
      encoding: "utf8",
      env: {
        ...process.env,
        PYTHONPATH: path.join(KIT_ROOT, "src"),
        CODEX_HOME: path.join(parent, "different-codex-home"),
        AGENT_FLOW_SKIP_CODEX_TRUST: "1",
        AGENT_FLOW_TEST_HARD_KILL_AFTER_WORKTREE_CODEX_TRUST_WRITE: "0",
      },
    });
    assert.equal(recovered.status, 0, recovered.stderr || recovered.stdout);
    assert.deepEqual(fs.readFileSync(configPath), original);
    assert.equal(fs.statSync(configPath).mode & 0o777, 0o640);
    assert.equal(fs.existsSync(transactionPath), false);
  },
);

test("Codex config lock serializes independent repositories and rejects a forged repo journal", (t) => {
  const { parent, leader } = setupRepository(t);
  const second = path.join(parent, "second");
  fs.mkdirSync(second);
  git(second, ["init", "-b", "main"]);
  const codexHome = path.join(parent, "shared-codex-home");
  fs.mkdirSync(codexHome);
  const configPath = path.join(codexHome, "config.toml");
  const original = Buffer.from('shared = "config"\n', "utf8");
  fs.writeFileSync(configPath, original, { mode: 0o640 });
  const journalFor = (root) => {
    const stateRoot = path.join(root, ".git", "agent-flow");
    fs.mkdirSync(stateRoot);
    return path.join(stateRoot, "codex-worktree-trust.json");
  };
  const first = acquireCodexConfigTransactionLock({
    leaderRoot: leader,
    configPath,
    journalPath: journalFor(leader),
    original: { exists: true, content: original, mode: 0o640 },
  });
  assert.throws(
    () => acquireCodexConfigTransactionLock({
      leaderRoot: second,
      configPath,
      journalPath: journalFor(second),
      original: { exists: true, content: original, mode: 0o640 },
    }),
    /transaction lock is already held|transaction is active/,
  );
  assert.deepEqual(fs.readFileSync(configPath), original);
  const journal = JSON.parse(fs.readFileSync(first.transactionPath, "utf8"));
  journal.transaction_token = "0".repeat(32);
  fs.writeFileSync(first.transactionPath, `${JSON.stringify(journal, null, 2)}\n`, "utf8");
  assert.throws(
    () => releaseCodexConfigTransactionLock(first),
    /receipt journal binding changed|does not authenticate/,
  );
  assert.equal(fs.existsSync(first.receiptPath), true);
  assert.deepEqual(fs.readFileSync(configPath), original);
  assert.equal(fs.statSync(configPath).mode & 0o777, 0o640);
});

test(
  "Python recovery preserves a mode-only edit in the displacement power window",
  { skip: process.platform === "win32" },
  (t) => {
    const { parent, leader } = setupRepository(t);
    const worktree = path.join(parent, "codex-displace-crash");
    addWorktree(leader, worktree, "feat/codex-displace-crash");
    seedWorktreeCodexHooks(leader, worktree);
    const { codexHome } = configureFakeCodex(t, parent);
    const configPath = path.join(codexHome, "config.toml");
    const original = Buffer.from('mode = "preserved"\n', "utf8");
    fs.writeFileSync(configPath, original, { mode: 0o640 });
    const trustModule = pathToFileURL(path.join(KIT_ROOT, "lib", "codex-hook-trust.mjs")).href;
    const crashScript = [
      `import { ensureCodexWorktreeHookTrust } from ${JSON.stringify(trustModule)};`,
      "ensureCodexWorktreeHookTrust({ leaderRoot: process.argv[1], worktreeRoot: process.argv[2] });",
    ].join("\n");
    const crashed = spawnSync(
      process.execPath,
      ["--input-type=module", "--eval", crashScript, leader, worktree],
      {
        env: {
          ...process.env,
          AGENT_FLOW_TEST_HARD_KILL_AFTER_WORKTREE_CODEX_TRUST_DISPLACE: "1",
        },
        encoding: "utf8",
      },
    );
    assert.equal(crashed.signal, "SIGKILL", crashed.stderr || crashed.stdout);
    assert.equal(fs.existsSync(configPath), false);
    const receiptPath = path.join(codexHome, ".config.toml.agent-flow.lock.json");
    const receipt = JSON.parse(fs.readFileSync(receiptPath, "utf8"));
    const displacedPath = receipt.rounds[0].displaced_path;
    fs.chmodSync(displacedPath, 0o600);
    const recoveryScript = [
      "from pathlib import Path",
      "from agent_flow.core.codex_trust import ensure_codex_worktree_hook_trust",
      "import sys",
      "ensure_codex_worktree_hook_trust(leader_root=Path(sys.argv[1]), worktree_root=Path(sys.argv[2]))",
    ].join("; ");
    const recovered = spawnSync("python3", ["-c", recoveryScript, leader, worktree], {
      cwd: KIT_ROOT,
      encoding: "utf8",
      env: { ...process.env, PYTHONPATH: path.join(KIT_ROOT, "src"), AGENT_FLOW_SKIP_CODEX_TRUST: "1" },
    });
    assert.equal(recovered.status, 0, recovered.stderr || recovered.stdout);
    assert.deepEqual(fs.readFileSync(configPath), original);
    assert.equal(fs.statSync(configPath).mode & 0o777, 0o600);
    assert.equal(fs.existsSync(receiptPath), false);
  },
);

test("Codex trust rejects configs over 2 MiB before lock or config mutation", (t) => {
  const { parent, leader } = setupRepository(t);
  const worktree = path.join(parent, "codex-large-config");
  addWorktree(leader, worktree, "feat/codex-large-config");
  const { codexHome } = configureFakeCodex(t, parent);
  const configPath = path.join(codexHome, "config.toml");
  const oversized = Buffer.alloc((2 * 1024 * 1024) + 1, 0x20);
  fs.writeFileSync(configPath, oversized, { mode: 0o640 });
  assert.throws(
    () => ensureWorktreeHostBridge(leader, worktree),
    /2 MiB trust transaction limit/,
  );
  assert.deepEqual(fs.readFileSync(configPath), oversized);
  assert.equal(fs.statSync(configPath).mode & 0o777, 0o640);
  assert.equal(
    fs.existsSync(path.join(codexHome, ".config.toml.agent-flow.lock.json")),
    false,
  );
});

test("Node Codex worktree trust fails closed on a semantic TOML table alias", (t) => {
  const { parent, leader } = setupRepository(t);
  const worktree = path.join(parent, "codex-semantic-table");
  addWorktree(leader, worktree, "feat/codex-semantic-table");
  const { codexHome } = configureFakeCodex(t, parent);
  const configPath = path.join(codexHome, "config.toml");
  const canonicalWorktree = fs.realpathSync.native(worktree);
  const original = Buffer.from(
    `[ projects . ${JSON.stringify(canonicalWorktree)} ]\ntrust_level = "untrusted"\n`,
    "utf8",
  );
  fs.writeFileSync(configPath, original, { mode: 0o640 });

  assert.throws(
    () => ensureWorktreeHostBridge(leader, worktree),
    /equivalent Codex config TOML target cannot be edited losslessly/,
  );
  assert.deepEqual(fs.readFileSync(configPath), original);
  assert.equal(fs.statSync(configPath).mode & 0o777, 0o640);
});

test(
  "real Codex hooks/list accepts Node and Python worktree trust state",
  { skip: process.env.AGENT_FLOW_REAL_CODEX_TEST !== "1" },
  (t) => {
    const { parent, leader } = setupRepository(t);
    for (const relative of [".Codex/hooks.json", ".codex/hooks.json"]) {
      const settingsPath = path.join(leader, relative);
      const settings = JSON.parse(fs.readFileSync(settingsPath, "utf8"));
      delete settings.host;
      for (const entries of Object.values(settings.hooks)) {
        for (const entry of entries) {
          for (const hook of entry.hooks) delete hook.bridgeVersion;
        }
      }
      fs.writeFileSync(settingsPath, `${JSON.stringify(settings, null, 2)}\n`, "utf8");
    }
    const worktree = path.join(parent, "real-codex-trusted");
    addWorktree(leader, worktree, "feat/real-codex-trusted");
    const codexHome = path.join(parent, "real-codex-home");
    fs.mkdirSync(codexHome);
    setTestEnvironment(t, {
      AGENT_FLOW_SKIP_CODEX_TRUST: null,
      CODEX_HOME: codexHome,
      CODEX_CLI_PATH: process.env.CODEX_CLI_PATH ?? "codex",
      FAKE_CODEX_MODE: null,
    });

    ensureWorktreeHostBridge(leader, worktree);
    const configPath = path.join(codexHome, "config.toml");
    const afterNode = fs.readFileSync(configPath);
    assert.match(afterNode.toString("utf8"), /trusted_hash = "sha256:[0-9a-f]{64}"/);

    const python = runPythonBridge(leader, worktree);
    assert.equal(python.status, 0, python.stderr || python.stdout);
    assert.deepEqual(fs.readFileSync(configPath), afterNode);
  },
);

test("Node and Python roll bridge and user Codex config back when hook discovery fails", (t) => {
  const { parent, leader } = setupRepository(t);
  const nodeWorktree = path.join(parent, "codex-node-failure");
  const pythonWorktree = path.join(parent, "codex-python-failure");
  addWorktree(leader, nodeWorktree, "feat/codex-node-failure");
  addWorktree(leader, pythonWorktree, "feat/codex-python-failure");
  const { codexHome } = configureFakeCodex(t, parent, "fail-query");
  const configPath = path.join(codexHome, "config.toml");
  const configBefore = Buffer.from('# keep exactly  \n[custom]\nvalue = "user"\n', "utf8");
  fs.writeFileSync(configPath, configBefore, { mode: 0o600 });
  const excludePath = path.join(leader, ".git", "info", "exclude");
  const excludeBefore = fs.readFileSync(excludePath);
  const nodeAgentsBefore = fs.readFileSync(path.join(nodeWorktree, "AGENTS.md"));

  assert.throws(
    () => ensureWorktreeHostBridge(leader, nodeWorktree),
    /Codex worktree hook discovery failed/,
  );
  assert.deepEqual(fs.readFileSync(configPath), configBefore);
  assert.deepEqual(fs.readFileSync(excludePath), excludeBefore);
  assert.deepEqual(fs.readFileSync(path.join(nodeWorktree, "AGENTS.md")), nodeAgentsBefore);
  assert.equal(fs.existsSync(path.join(nodeWorktree, ".omp")), false);

  const pythonAgentsBefore = fs.readFileSync(path.join(pythonWorktree, "AGENTS.md"));
  const python = runPythonBridge(leader, pythonWorktree);
  assert.notEqual(python.status, 0);
  assert.match(python.stderr, /Codex worktree hook discovery failed/);
  assert.deepEqual(fs.readFileSync(configPath), configBefore);
  assert.deepEqual(fs.readFileSync(excludePath), excludeBefore);
  assert.deepEqual(fs.readFileSync(path.join(pythonWorktree, "AGENTS.md")), pythonAgentsBefore);
  assert.equal(fs.existsSync(path.join(pythonWorktree, ".omp")), false);
  assert.equal(fs.existsSync(path.join(leader, ".git", "agent-flow", "worktree-host-bridges")), false);
});

test("Node rejects managed hook subset, extra, and duplicate sets during discovery and verification", (t) => {
  for (const mode of [
    "subset-managed",
    "extra-managed",
    "duplicate-managed",
    "verify-subset-managed",
    "verify-extra-managed",
    "verify-duplicate-managed",
  ]) {
    const { parent, leader } = setupRepository(t);
    const worktree = path.join(parent, `codex-${mode}`);
    addWorktree(leader, worktree, `feat/codex-${mode}`);
    const { codexHome } = configureFakeCodex(t, parent, mode);
    const configPath = path.join(codexHome, "config.toml");
    const configBefore = Buffer.from(`# ${mode}\n[custom]\nvalue = "user"\n`, "utf8");
    fs.writeFileSync(configPath, configBefore, { mode: 0o640 });

    assert.throws(
      () => ensureWorktreeHostBridge(leader, worktree),
      /incomplete, extra, or duplicate managed hook set/,
      mode,
    );
    assert.deepEqual(fs.readFileSync(configPath), configBefore, mode);
    assert.equal(fs.statSync(configPath).mode & 0o777, 0o640, mode);
    assert.equal(fs.existsSync(path.join(worktree, ".omp")), false, mode);
  }
});

test("Node rejects managed hook content, symlink, and mode races across trust queries", (t) => {
  for (const { mutation, query } of [
    { mutation: "content", query: "1" },
    { mutation: "symlink", query: "1" },
    { mutation: "mode", query: "2" },
  ]) {
    const { parent, leader } = setupRepository(t);
    const worktree = path.join(parent, `codex-script-${mutation}`);
    addWorktree(leader, worktree, `feat/codex-script-${mutation}`);
    const { codexHome } = configureFakeCodex(t, parent);
    setTestEnvironment(t, {
      FAKE_CODEX_SCRIPT_MUTATION: mutation,
      FAKE_CODEX_SCRIPT_MUTATION_QUERY: query,
    });
    const configPath = path.join(codexHome, "config.toml");
    const configBefore = Buffer.from(`# ${mutation}\n[custom]\nvalue = "user"\n`, "utf8");
    fs.writeFileSync(configPath, configBefore, { mode: 0o640 });

    assert.throws(
      () => ensureWorktreeHostBridge(leader, worktree),
      /managed hook script .*guard-worktree\.sh/,
      mutation,
    );
    assert.deepEqual(fs.readFileSync(configPath), configBefore, mutation);
    assert.equal(fs.statSync(configPath).mode & 0o777, 0o640, mutation);
    assert.equal(fs.existsSync(path.join(worktree, ".omp")), false, mutation);
  }
});

test("OMP host precedence treats missing Codex as unavailable even when CODEX_HOME is inherited", (t) => {
  const { parent, leader } = setupRepository(t);
  const nodeWorktree = path.join(parent, "omp-node");
  const pythonWorktree = path.join(parent, "omp-python");
  addWorktree(leader, nodeWorktree, "feat/omp-node");
  addWorktree(leader, pythonWorktree, "feat/omp-python");
  setTestEnvironment(t, {
    AGENT_FLOW_SKIP_CODEX_TRUST: null,
    AGENT_FLOW_HOST: null,
    CLAUDECODE: null,
    CLAUDE_CLI: null,
    CODEX_CLI: null,
    CODEX_SHELL: null,
    CODEX_THREAD_ID: null,
    CODEX_INTERNAL_ORIGINATOR_OVERRIDE: null,
    OMP_PROFILE: "test",
    PI_CODING_AGENT_DIR: null,
    CODEX_HOME: path.join(parent, "inherited-codex-home"),
    CODEX_CLI_PATH: path.join(parent, "missing-codex"),
  });

  ensureWorktreeHostBridge(leader, nodeWorktree);
  const python = runPythonBridge(leader, pythonWorktree);
  assert.equal(python.status, 0, python.stderr || python.stdout);
  assertBridge(leader, nodeWorktree);
  assertBridge(leader, pythonWorktree);
  assert.equal(fs.existsSync(path.join(parent, "inherited-codex-home")), false);
});

test("OMP symlink extension does not add host-only root context synchronization", (t) => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-omp-context-"));
  t.after(() => fs.rmSync(parent, { recursive: true, force: true }));
  const leader = path.join(parent, "leader");
  fs.mkdirSync(leader);
  git(leader, ["init", "-b", "main"]);
  git(leader, ["config", "user.email", "test@example.com"]);
  git(leader, ["config", "user.name", "Test User"]);
  fs.writeFileSync(path.join(leader, "README.md"), "fixture\n", "utf8");
  fs.writeFileSync(path.join(leader, "AGENTS.md"), "# Leader agents\n", "utf8");
  fs.writeFileSync(path.join(leader, "CLAUDE.md"), "# Leader claude\n", "utf8");
  git(leader, ["add", "README.md", "AGENTS.md", "CLAUDE.md"]);
  git(leader, ["commit", "-m", "base"]);
  const env = {
    ...process.env,
    AGENT_FLOW_SKIP_CODEX_TRUST: "1",
  };
  const install = spawnSync(process.execPath, [CLI, "install"], {
    cwd: leader,
    env,
    encoding: "utf8",
  });
  assert.equal(install.status, 0, install.stderr || install.stdout);
  const start = spawnSync(process.execPath, [
    CLI,
    "run",
    "start",
    "--task",
    "omp pinned context",
    "--workflow",
    "default",
    "--run-id",
    "omp-pinned-context",
  ], {
    cwd: leader,
    env,
    encoding: "utf8",
  });
  assert.equal(start.status, 0, start.stderr || start.stdout);
  const state = activeNodeState(leader);
  const pinned = fs.realpathSync.native(state.workspace_root);
  const extension = path.join(pinned, ".omp", "extensions", "agent-flow-hooks.ts");
  assert.equal(fs.lstatSync(extension).isSymbolicLink(), true);
  assert.equal(
    fs.realpathSync.native(extension),
    fs.realpathSync.native(path.join(leader, ".omp", "extensions", "agent-flow-hooks.ts")),
  );

  const foreign = path.join(parent, "foreign-worktree");
  addWorktree(leader, foreign, "feat/foreign-worktree");
  ensureWorktreeHostBridge(leader, foreign);
  const leaderAgentsBefore = fs.readFileSync(path.join(leader, "AGENTS.md"));
  const leaderClaudeBefore = fs.readFileSync(path.join(leader, "CLAUDE.md"));
  fs.writeFileSync(path.join(pinned, "CLAUDE.md"), "pinned claude\n", "utf8");
  fs.writeFileSync(path.join(pinned, "AGENTS.md"), "pinned agents old\n", "utf8");
  fs.writeFileSync(path.join(foreign, "CLAUDE.md"), "foreign claude\n", "utf8");
  fs.writeFileSync(path.join(foreign, "AGENTS.md"), "foreign agents old\n", "utf8");

  const exercise = String.raw`
import fs from "node:fs";

const extensionUrl = process.argv[1];
const pinned = process.argv[2];
const foreign = process.argv[3];
const { default: agentFlowHooks } = await import(extensionUrl);
const handlers = new Map();
agentFlowHooks({
  setLabel() {},
  on(name, handler) { handlers.set(name, handler); },
});
const toolResult = handlers.get("tool_result");
if (typeof toolResult !== "function") throw new Error("missing OMP tool_result handler");

const failedClaudeBefore = fs.readFileSync(pinned + "/CLAUDE.md", "utf8");
const failedAgentsBefore = fs.readFileSync(pinned + "/AGENTS.md", "utf8");
let result = await toolResult(
  { toolName: "Edit", input: { path: "CLAUDE.md" }, isError: true },
  { cwd: pinned },
);
if (result !== undefined) throw new Error("failed OMP write result must be ignored");
if (
  fs.readFileSync(pinned + "/CLAUDE.md", "utf8") !== failedClaudeBefore
  || fs.readFileSync(pinned + "/AGENTS.md", "utf8") !== failedAgentsBefore
) {
  throw new Error("failed OMP write result changed root context");
}

result = await toolResult(
  { toolName: "Edit", input: { path: "CLAUDE.md" } },
  { cwd: pinned },
);
if (result?.isError) throw new Error(String(result.content?.[0]?.text || "pinned CLAUDE hook failed"));
if (fs.readFileSync(pinned + "/AGENTS.md", "utf8") !== "pinned agents old\n") {
  throw new Error("OMP added host-only CLAUDE.md to AGENTS.md synchronization");
}

fs.writeFileSync(pinned + "/AGENTS.md", "pinned agents\n", "utf8");
result = await toolResult(
  { toolName: "Write", input: { file_path: pinned + "/AGENTS.md" } },
  { cwd: pinned },
);
if (result?.isError) throw new Error(String(result.content?.[0]?.text || "pinned AGENTS hook failed"));
if (fs.readFileSync(pinned + "/CLAUDE.md", "utf8") !== "pinned claude\n") {
  throw new Error("OMP added host-only AGENTS.md to CLAUDE.md synchronization");
}

result = await toolResult(
  { toolName: "Edit", input: { path: "CLAUDE.md" } },
  { cwd: foreign },
);
if (result?.isError) throw new Error(String(result.content?.[0]?.text || "foreign context hook failed"));
if (fs.readFileSync(foreign + "/AGENTS.md", "utf8") !== "foreign agents old\n") {
  throw new Error("non-pinned registered worktree context was modified");
}
`;
  const exercised = spawnSync(process.execPath, [
    "--input-type=module",
    "--eval",
    exercise,
    pathToFileURL(extension).href,
    pinned,
    foreign,
  ], {
    cwd: pinned,
    env,
    encoding: "utf8",
  });
  assert.equal(exercised.status, 0, exercised.stderr || exercised.stdout);
  assert.deepEqual(fs.readFileSync(path.join(leader, "AGENTS.md")), leaderAgentsBefore);
  assert.deepEqual(fs.readFileSync(path.join(leader, "CLAUDE.md")), leaderClaudeBefore);
});

test("Node run bridges installed context without installing or regenerating the worktree index", (t) => {
  const { parent, leader } = setupRepository(t, {
    trackedContext: false,
    seedInstalled: false,
  });
  const home = path.join(parent, "home");
  fs.mkdirSync(home);
  const env = {
    ...process.env,
    HOME: home,
    PYTHONPATH: "",
    AGENT_FLOW_SKIP_CODEX_TRUST: "1",
  };
  const install = spawnSync(process.execPath, [CLI, "install"], {
    cwd: leader,
    env,
    encoding: "utf8",
  });
  assert.equal(install.status, 0, install.stderr || install.stdout);
  const pinnedCli = path.join(leader, ".agent-flow", "runtime", "node", "bin", "agent-flow-kit.mjs");
  assert.equal(fs.existsSync(path.join(leader, ".agent-flow", "runtime", "node", "lib", "codex-hook-trust.mjs")), true);
  const indexPath = path.join(leader, ".agent-flow", "skills", "index.json");
  const indexBefore = fs.readFileSync(indexPath);
  const indexMtimeBefore = fs.statSync(indexPath).mtimeMs;
  const external = path.join(parent, "installed-external");
  git(leader, ["worktree", "add", "--detach", external, "main"]);

  const start = spawnSync(process.execPath, [
    pinnedCli,
    "run",
    "start",
    "--task",
    "external bridge",
    "--workflow",
    "default",
    "--run-id",
    "bridge-run",
  ], {
    cwd: external,
    env: { ...env, HOME: process.env.HOME },
    encoding: "utf8",
  });

  assert.equal(start.status, 0, start.stderr || start.stdout);
  assertBridge(leader, external, null);
  assert.deepEqual(fs.readdirSync(path.join(external, ".agent-flow")).sort(), ["bin", "skills"]);
  assert.deepEqual(fs.readFileSync(indexPath), indexBefore);
  assert.equal(fs.statSync(indexPath).mtimeMs, indexMtimeBefore);
});
