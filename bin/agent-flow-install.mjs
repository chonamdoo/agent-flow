#!/usr/bin/env node
// Project-local installer for agent-flow.
//
// Run from any project root:
//   npx <agent-flow-package> install
//
// The installer creates .agent-flow/ (runs, memory, kit metadata) and
// upserts an agent-flow block into CLAUDE.md / AGENTS.md / GEMINI.md so
// every host CLI sees the same workflow contract.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const KIT_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const PROJECT = process.cwd();
const AF_DIR = path.join(PROJECT, ".agent-flow");
const HOME = process.env.HOME || process.env.USERPROFILE || "";

function ensureDir(p) {
  fs.mkdirSync(p, { recursive: true });
}

function bootstrapMarkdown(label) {
  const tmplPath = path.join(KIT_ROOT, "bootstrap", `${label}.template`);
  if (!fs.existsSync(tmplPath)) {
    return;
  }
  const block = fs.readFileSync(tmplPath, "utf8");
  const targetPath = path.join(PROJECT, label);
  const start = "<!-- agent-flow:start -->";
  const end = "<!-- agent-flow:end -->";
  const current = fs.existsSync(targetPath)
    ? fs.readFileSync(targetPath, "utf8")
    : "";
  if (current.includes(start) && current.includes(end)) {
    const before = current.slice(0, current.indexOf(start));
    const after = current.slice(current.indexOf(end) + end.length);
    fs.writeFileSync(targetPath, before + block + after.replace(/^\n/, ""));
    return;
  }
  const prefix = current.trim() ? current.trimEnd() + "\n\n" : `# ${label}\n\n`;
  fs.writeFileSync(targetPath, prefix + block);
}

function detectProfile() {
  // Minimal stack detection. The Python runner does richer detection at
  // first run; this is just for the install banner.
  if (fs.existsSync(path.join(PROJECT, "next.config.js")) ||
      fs.existsSync(path.join(PROJECT, "next.config.mjs")) ||
      fs.existsSync(path.join(PROJECT, "next.config.ts"))) {
    return "nextjs";
  }
  if (fs.existsSync(path.join(PROJECT, "pyproject.toml")) ||
      fs.existsSync(path.join(PROJECT, "requirements.txt"))) {
    return "python";
  }
  if (
      fs.existsSync(path.join(PROJECT, "build.gradle")) ||
      fs.existsSync(path.join(PROJECT, "settings.gradle")) ||
      fs.existsSync(path.join(PROJECT, "build.gradle.kts")) ||
      fs.existsSync(path.join(PROJECT, "settings.gradle.kts"))
  ) {
    return "android";
  }
  if (fs.existsSync(path.join(PROJECT, "package.json"))) {
    return "node";
  }
  return "generic";
}

function copyDir(src, dest) {
  // Recursive copy without overwriting user-modified files. If a file exists
  // at dest with different content, leave it (user customization wins) and
  // print a notice. Brand-new files are always written.
  if (!fs.existsSync(src)) return { written: 0, skipped: 0 };
  let written = 0, skipped = 0;
  ensureDir(dest);
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const srcPath = path.join(src, entry.name);
    const destPath = path.join(dest, entry.name);
    if (entry.isDirectory()) {
      const r = copyDir(srcPath, destPath);
      written += r.written;
      skipped += r.skipped;
    } else if (entry.isFile()) {
      if (fs.existsSync(destPath)) {
        const srcContent = fs.readFileSync(srcPath, "utf8");
        const destContent = fs.readFileSync(destPath, "utf8");
        if (srcContent !== destContent) {
          skipped += 1;
          console.log(`  ! skipped (user-modified): ${path.relative(PROJECT, destPath)}`);
          continue;
        }
      }
      fs.copyFileSync(srcPath, destPath);
      written += 1;
    }
  }
  return { written, skipped };
}

function copyFileIfMissingOrSame(src, dest) {
  if (!fs.existsSync(src)) return false;
  ensureDir(path.dirname(dest));
  const srcContent = fs.readFileSync(src, "utf8");
  if (fs.existsSync(dest) && fs.readFileSync(dest, "utf8") !== srcContent) {
    console.log(`  ! skipped (user-modified): ${path.relative(PROJECT, dest)}`);
    return false;
  }
  fs.copyFileSync(src, dest);
  return true;
}

function pathExists(p) {
  try {
    fs.lstatSync(p);
    return true;
  } catch (e) {
    if (e && e.code === "ENOENT") return false;
    throw e;
  }
}

function linkOrCopyDir(src, dest) {
  if (!fs.existsSync(src)) return "missing-source";
  if (pathExists(dest)) return "exists";
  ensureDir(path.dirname(dest));
  const relTarget = path.relative(path.dirname(dest), src);
  try {
    fs.symlinkSync(relTarget, dest, "dir");
    return "linked";
  } catch {
    const r = copyDir(src, dest);
    return `copied:${r.written}:${r.skipped}`;
  }
}

function copySkillDir(src, dest) {
  if (!fs.existsSync(src)) return "missing-source";
  const r = copyDir(src, dest);
  return `copied:${r.written}:${r.skipped}`;
}

function install() {
  ensureDir(path.join(AF_DIR, "runs"));
  ensureDir(path.join(AF_DIR, "memory"));
  ensureDir(path.join(AF_DIR, "memory", "lore"));
  ensureDir(path.join(AF_DIR, "memory", "lore", "archive"));

  const profile = detectProfile();

  bootstrapMarkdown("CLAUDE.md");
  bootstrapMarkdown("AGENTS.md");
  bootstrapMarkdown("GEMINI.md");
  upsertGitignore(path.join(PROJECT, ".gitignore"), ["graphify-out/manifest.json", "graphify-out/cost.json"]);

  // Copy bundled skills into project-local skills dir.
  // Host-AI-specific skill paths (`.claude/skills/`, `.codex/skills/`) are
  // populated by symlinking from .agent-flow/skills/ where possible, so
  // updates to the kit propagate without re-installing.
  const skillsCopied = copyDir(
    path.join(KIT_ROOT, "skills"),
    path.join(AF_DIR, "skills"),
  );
  const workflowsCopied = copyDir(
    path.join(KIT_ROOT, "workflows"),
    path.join(AF_DIR, "workflows"),
  );
  const profilesCopied = copyDir(
    path.join(KIT_ROOT, "profiles"),
    path.join(AF_DIR, "profiles"),
  );
  const templatesCopied = copyDir(
    path.join(KIT_ROOT, "templates"),
    path.join(AF_DIR, "templates"),
  );
  const scriptsCopied = copyDir(
    path.join(KIT_ROOT, "scripts"),
    path.join(PROJECT, "scripts"),
  );
  const contextRulesCopied = copyDir(
    path.join(KIT_ROOT, ".Codex", "rules", "context"),
    path.join(PROJECT, ".Codex", "rules", "context"),
  );
  const contextTreeCopied = copyDir(
    path.join(KIT_ROOT, ".Codex", "context"),
    path.join(PROJECT, ".Codex", "context"),
  );
  copyFileIfMissingOrSame(
    path.join(KIT_ROOT, ".Codex", "rules", "codebase-rubric.md"),
    path.join(PROJECT, ".Codex", "rules", "codebase-rubric.md"),
  );

  const agentFlowSkill = path.join(AF_DIR, "skills", "agent-flow");
  const claudeSkillStatus = linkOrCopyDir(
    agentFlowSkill,
    path.join(PROJECT, ".claude", "skills", "agent-flow"),
  );
  const codexSkillStatus = linkOrCopyDir(
    agentFlowSkill,
    path.join(PROJECT, ".codex", "skills", "agent-flow"),
  );
  const globalCodexSkillStatus = HOME
    ? copySkillDir(
        agentFlowSkill,
        path.join(HOME, ".codex", "skills", "agent-flow"),
      )
    : "missing-home";
  const globalClaudeSkillStatus = HOME
    ? copySkillDir(
        agentFlowSkill,
        path.join(HOME, ".claude", "skills", "agent-flow"),
      )
    : "missing-home";

  // Keep a small pointer file for users who inspect .claude/skills by hand.
  // The agent-flow skill itself has already been linked or copied above.
  const claudeSkillsDir = path.join(PROJECT, ".claude", "skills");
  if (fs.existsSync(path.join(PROJECT, ".claude")) || profile !== "generic") {
    ensureDir(claudeSkillsDir);
    const readme = path.join(claudeSkillsDir, "AGENT_FLOW_SKILLS.md");
    if (!fs.existsSync(readme)) {
      fs.writeFileSync(readme,
        "# agent-flow skills location\n\n" +
        "Bundled agent-flow skills live at `.agent-flow/skills/`.\n\n" +
        "The installer links `agent-flow` into `.claude/skills/agent-flow` " +
        "when possible, or copies it when symlinks are unavailable.\n");
    }
  }

  const kitJson = {
    kit: "agent-flow",
    version: "0.1.0",
    profile,
    project_root: PROJECT,
    installed_at: new Date().toISOString(),
    skills_copied: skillsCopied,
    workflows_copied: workflowsCopied,
    profiles_copied: profilesCopied,
    templates_copied: templatesCopied,
    context_tree_copied: contextTreeCopied,
    skill_links: {
      claude: claudeSkillStatus,
      codex: codexSkillStatus,
      global_claude: globalClaudeSkillStatus,
      global_codex: globalCodexSkillStatus,
      gemini: "GEMINI.md",
    },
  };
  if (!process.argv.includes("--without-graphify")) {
    kitJson.graphify = installGraphify();
  }
  fs.writeFileSync(
    path.join(AF_DIR, "kit.json"),
    JSON.stringify(kitJson, null, 2)
  );

  console.log(`agent-flow installed`);
  console.log(`  profile : ${profile}`);
  console.log(`  root    : ${AF_DIR}`);
  console.log(`  skills  : ${skillsCopied.written} written, ${skillsCopied.skipped} skipped`);
  console.log(`  workflows: ${workflowsCopied.written} written, ${workflowsCopied.skipped} skipped`);
  console.log(`  profiles : ${profilesCopied.written} written, ${profilesCopied.skipped} skipped`);
  console.log(`  claude  : agent-flow skill ${claudeSkillStatus}`);
  console.log(`  codex   : agent-flow skill ${codexSkillStatus}`);
  console.log(`  ~/.claude: agent-flow skill ${globalClaudeSkillStatus}`);
  console.log(`  ~/.codex : agent-flow skill ${globalCodexSkillStatus}`);
  if (kitJson.graphify) {
    console.log(`  graphify: ${kitJson.graphify.status}`);
  }
  console.log(``);
  console.log(`Next: /agent-flow <task>`);
  console.log(`      (or: agent-flow run "<task>")`);
  console.log(`(If 'agent-flow' isn't on PATH yet: pip install -e ${KIT_ROOT})`);
}

function installGraphify() {
  if (process.env.AGENT_FLOW_GRAPHIFY_DRY_RUN === "1") {
    return {
      status: "dry-run",
      package: "graphifyy",
      command: "graphify",
      platforms: ["claude", "codex", "gemini"],
      skill_location: "~/.agents/skills/graphify",
      removed_duplicate_skills: [],
      graph: {
        status: "dry-run",
        command: "graphify .",
        output: "graphify-out/",
      },
    };
  }
  const installer = installGraphifyPackage();
  const skillInstall = runGraphifyInstall();
  const graph = runGraphifyProjectGraph();
  return {
    status: "installed",
    package: "graphifyy",
    command: "graphify",
    installer,
    platforms: skillInstall.platforms,
    skill_location: skillInstall.skillLocation,
    removed_duplicate_skills: skillInstall.removedDuplicates,
    graph,
  };
}

function upsertGitignore(pathName, entries) {
  const current = fs.existsSync(pathName) ? fs.readFileSync(pathName, "utf8") : "";
  const lines = current.split(/\r?\n/);
  const existing = new Set(lines.map((line) => line.trim()));
  const missing = entries.filter((entry) => !existing.has(entry) && !existing.has("graphify-out/"));
  if (missing.length === 0) {
    return;
  }
  const prefix = current.trimEnd();
  const next = `${prefix}${prefix ? "\n" : ""}${missing.join("\n")}\n`;
  fs.writeFileSync(pathName, next, "utf8");
}

function runChecked(commandName, args) {
  const result = spawnSync(commandName, args, {
    cwd: PROJECT,
    stdio: "inherit",
    env: process.env,
  });
  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error(`${commandName} ${args.join(" ")} failed with exit ${result.status}`);
  }
}

function runOptional(commandName, args) {
  const result = spawnSync(commandName, args, {
    cwd: PROJECT,
    stdio: "inherit",
    env: process.env,
  });
  if (result.error && result.error.code === "ENOENT") {
    return false;
  }
  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error(`${commandName} ${args.join(" ")} failed with exit ${result.status}`);
  }
  return true;
}

function installGraphifyPackage() {
  if (graphifyAvailable()) {
    return "existing";
  }
  if (runCandidate("uv", ["tool", "install", "--python", "3.12", "--force", "graphifyy"])) {
    return "uv tool";
  }
  const python = preferredPython();
  if (runCandidate("pipx", ["install", "--python", python, "--force", "graphifyy"])) {
    return "pipx";
  }
  runChecked(python, ["-m", "pip", "install", "graphifyy"]);
  return "pip";
}

function graphifyAvailable() {
  if (runCandidateQuiet("graphify", ["--help"]) && runCandidateQuiet("graphify", ["install", "--help"])) {
    return true;
  }
  const graphify = graphifyExecutable();
  return (
    runCandidateQuiet(graphify.command, [...graphify.prefixArgs, "--help"]) &&
    runCandidateQuiet(graphify.command, [...graphify.prefixArgs, "install", "--help"])
  );
}

function runGraphifyInstall() {
  runGraphifyCommand(["install"]);
  runGraphifyCommand(["install", "--platform", "codex"]);
  runGraphifyCommand(["install", "--platform", "gemini"]);
  return {
    platforms: ["claude", "codex", "gemini"],
    skillLocation: "~/.agents/skills/graphify",
    removedDuplicates: canonicalizeGraphifySkill(),
  };
}

function canonicalizeGraphifySkill() {
  if (!HOME) {
    return [];
  }
  const canonical = path.join(HOME, ".agents", "skills", "graphify");
  const candidates = [
    canonical,
    path.join(HOME, ".codex", "skills", "graphify"),
    path.join(HOME, ".gemini", "skills", "graphify"),
    path.join(HOME, ".claude", "skills", "graphify"),
  ];
  const existing = candidates
    .filter((candidate) => fs.existsSync(path.join(candidate, "SKILL.md")))
    .sort((a, b) => {
      const aTime = fs.statSync(path.join(a, "SKILL.md")).mtimeMs;
      const bTime = fs.statSync(path.join(b, "SKILL.md")).mtimeMs;
      return bTime - aTime;
    });
  if (existing.length === 0) {
    throw new Error("graphify install completed, but no graphify skill was found");
  }
  const source = existing[0];
  if (source !== canonical) {
    fs.mkdirSync(path.dirname(canonical), { recursive: true });
    const tempCanonical = `${canonical}.tmp.${process.pid}`;
    fs.rmSync(tempCanonical, { recursive: true, force: true });
    fs.cpSync(source, tempCanonical, { recursive: true });
    fs.rmSync(canonical, { recursive: true, force: true });
    fs.renameSync(tempCanonical, canonical);
  }
  const removed = [];
  for (const duplicate of candidates.filter((candidate) => candidate !== canonical)) {
    if (fs.existsSync(duplicate)) {
      fs.rmSync(duplicate, { recursive: true, force: true });
      removed.push(duplicate.replace(`${HOME}/`, "~/"));
    }
  }
  return removed;
}

function runGraphifyProjectGraph() {
  runGraphifyCommand(["."]);
  return {
    status: "generated",
    command: "graphify .",
    output: "graphify-out/",
  };
}

function runGraphifyCommand(args) {
  if (runOptional("graphify", args)) {
    return;
  }
  const graphify = graphifyExecutable();
  runChecked(graphify.command, [...graphify.prefixArgs, ...args]);
}

function runCandidate(commandName, args) {
  const result = spawnSync(commandName, args, {
    cwd: PROJECT,
    stdio: "inherit",
    env: process.env,
  });
  if (result.error && result.error.code === "ENOENT") {
    return false;
  }
  if (result.error) {
    throw result.error;
  }
  return result.status === 0;
}

function runCandidateQuiet(commandName, args) {
  const result = spawnSync(commandName, args, {
    cwd: PROJECT,
    stdio: "ignore",
    env: process.env,
  });
  if (result.error) {
    return false;
  }
  return result.status === 0;
}

function preferredPython() {
  for (const candidate of ["python3.12", "python3.11", "python3.10", "python3", "python"]) {
    const result = spawnSync(candidate, ["--version"], { stdio: "ignore" });
    if (!result.error && result.status === 0) {
      return candidate;
    }
  }
  return "python3";
}

function graphifyExecutable() {
  for (const candidate of [
    path.join(process.env.UV_TOOL_BIN_DIR || "", "graphify"),
    path.join(process.env.PIPX_BIN_DIR || "", "graphify"),
    path.join(HOME, ".local", "bin", "graphify"),
  ]) {
    if (!candidate) {
      continue;
    }
    if (fs.existsSync(candidate)) {
      return { command: candidate, prefixArgs: [] };
    }
  }
  return { command: preferredPython(), prefixArgs: ["-m", "graphify"] };
}

const cmd = process.argv[2];
if (cmd === "install") {
  install();
} else if (cmd === "--help" || cmd === "-h" || !cmd) {
  console.log("Usage: npx <agent-flow-package> install [--without-graphify]");
  process.exit(0);
} else {
  console.error(`Unknown command: ${cmd}`);
  process.exit(1);
}
