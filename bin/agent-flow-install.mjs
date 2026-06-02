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
import crypto from "node:crypto";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const KIT_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const REQUESTED_PROJECT = process.cwd();
const HOME = process.env.HOME || process.env.USERPROFILE || "";
const PROJECT = resolveInstallProject(REQUESTED_PROJECT);
const AF_DIR = path.join(PROJECT, ".agent-flow");
const BUNDLED_HOST_SKILL_NAMES = new Set(["agent-flow"]);
const PROFILE_MANAGED_HOST_ONLY_SKILLS = new Set([
  "adaptive",
  "android-cli",
  "android-debugging",
  "android-module-creator",
  "android-mvi-feature",
  "appfunctions",
  "camera1-to-camerax",
  "compose-animations",
  "compose-focus-navigation",
  "compose-modifier-and-layout-style",
  "compose-recomposition-performance",
  "compose-side-effects",
  "compose-slot-api-pattern",
  "compose-stability-diagnostics",
  "compose-state-authoring",
  "compose-state-deferred-reads",
  "compose-state-hoisting",
  "compose-state-holder-ui-split",
  "compose-ui-testing-patterns",
  "display-glasses-with-jetpack-compose-glimmer",
  "edge-to-edge",
  "engage-sdk-integration",
  "kotlin-coroutines-structured-concurrency",
  "kotlin-flow-state-event-modeling",
  "kotlin-multiplatform-expect-actual",
  "kotlin-types-value-class",
  "navigation-3",
  "perfetto-sql",
  "perfetto-trace-analysis",
  "play-billing-library-version-upgrade",
  "r8-analyzer",
  "testing-setup",
  "verified-email",
]);

function ensureDir(p) {
  fs.mkdirSync(p, { recursive: true });
}

function resolveManagedWorktreeRoot(start) {
  const parts = path.resolve(start).split(path.sep);
  const markers = new Set([".agent-flow", ".codex", ".Codex"]);
  for (let index = parts.length - 2; index >= 0; index -= 1) {
    if (parts[index + 1] !== "worktrees") continue;
    if (!markers.has(parts[index])) continue;
    const root = parts.slice(0, index).join(path.sep) || path.sep;
    if (HOME && samePath(root, HOME) && (parts[index] === ".codex" || parts[index] === ".Codex")) {
      continue;
    }
    return root;
  }
  return null;
}

function resolveInstallProject(start) {
  const managedRoot = resolveManagedWorktreeRoot(start);
  if (managedRoot) return managedRoot;
  const gitCommonRoot = resolveGitCommonWorktreeRoot(start);
  if (gitCommonRoot) return gitCommonRoot;
  return start;
}

function resolveGitCommonWorktreeRoot(start) {
  const topLevel = gitOutput(start, ["rev-parse", "--show-toplevel"]);
  const commonDir = gitOutput(start, ["rev-parse", "--git-common-dir"]);
  if (!topLevel || !commonDir) {
    return null;
  }
  const resolvedCommonDir = path.resolve(topLevel, commonDir);
  if (path.basename(resolvedCommonDir) !== ".git") {
    return null;
  }
  return path.dirname(resolvedCommonDir);
}

function gitOutput(cwd, args) {
  const result = spawnSync("git", args, {
    cwd,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
  });
  if (result.error || result.status !== 0) {
    return null;
  }
  const output = result.stdout.trim();
  return output || null;
}

function samePath(left, right) {
  try {
    return fs.realpathSync.native(left) === fs.realpathSync.native(right);
  } catch {
    return path.resolve(left) === path.resolve(right);
  }
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
  // 설치 배너도 Python CLI와 같은 profile을 보여줘야 agent가 다른 guide를 고르지 않는다.
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
    const packageText = fs.readFileSync(path.join(PROJECT, "package.json"), "utf8");
    if (packageText.includes("react-native")) {
      return "react-native";
    }
    if (packageText.includes("\"next\"")) {
      return "nextjs";
    }
    if (fs.existsSync(path.join(PROJECT, "tsconfig.json"))) {
      return "typescript";
    }
    return "node";
  }
  // npm gate를 실행할 수 없는 tsconfig 단독 프로젝트는 generic으로 둔다.
  return "generic";
}

function copyDir(src, dest, excludedRootDirs = new Set(), isRoot = true) {
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
      if (isRoot && excludedRootDirs.has(entry.name)) {
        const r = removeDirIfSame(srcPath, destPath);
        written += r.written;
        skipped += r.skipped;
        continue;
      }
      const r = copyDir(srcPath, destPath, excludedRootDirs, false);
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

function removeDirIfSame(src, dest) {
  if (!fs.existsSync(dest)) return { written: 0, skipped: 0 };
  if (!dirContentsMatch(src, dest)) return { written: 0, skipped: 1 };
  fs.rmSync(dest, { recursive: true, force: true });
  return { written: 1, skipped: 0 };
}

function dirContentsMatch(src, dest) {
  if (!fs.existsSync(src) || !fs.existsSync(dest)) return false;
  const srcEntries = fs.readdirSync(src, { withFileTypes: true });
  const destEntries = fs.readdirSync(dest, { withFileTypes: true });
  if (srcEntries.length !== destEntries.length) return false;
  const destByName = new Map(destEntries.map((entry) => [entry.name, entry]));
  for (const srcEntry of srcEntries) {
    const destEntry = destByName.get(srcEntry.name);
    if (!destEntry || srcEntry.isDirectory() !== destEntry.isDirectory() || srcEntry.isFile() !== destEntry.isFile()) {
      return false;
    }
    const srcPath = path.join(src, srcEntry.name);
    const destPath = path.join(dest, srcEntry.name);
    if (srcEntry.isDirectory()) {
      if (!dirContentsMatch(srcPath, destPath)) return false;
      continue;
    }
    if (srcEntry.isFile() && fs.readFileSync(srcPath, "utf8") !== fs.readFileSync(destPath, "utf8")) {
      return false;
    }
  }
  return true;
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

function installProjectSkills() {
  const previousIndex = readJsonIfExists(path.join(AF_DIR, "skills", "index.json"));
  const selected = selectProjectSkills();
  const links = [];
  for (const skill of selected.skills) {
    for (const host of skill.hosts) {
      links.push(linkProjectSkill(skill, host, previousIndex));
    }
  }
  links.push(...removeStaleProjectSkillLinks(selected.skills, previousIndex));
  const index = { ...selected, links };
  fs.writeFileSync(path.join(AF_DIR, "skills", "index.json"), `${JSON.stringify(index, null, 2)}\n`);
  return index;
}

function selectProjectSkills() {
  const discovered = [
    ...discoverSkills(path.join(AF_DIR, "local-skills"), "local", PROFILE_MANAGED_HOST_ONLY_SKILLS),
    ...discoverProjectSkills(),
    ...discoverSkills(path.join(AF_DIR, "skills"), "bundled", new Set(), BUNDLED_HOST_SKILL_NAMES),
  ];
  const byName = new Map();
  const warnings = [];
  for (const skill of discovered) {
    const current = byName.get(skill.name);
    if (!current || skill.priority < current.priority) byName.set(skill.name, skill);
    warnings.push(...skill.warnings);
  }
  const skills = [...byName.values()].sort((a, b) => a.name.localeCompare(b.name));
  const conflicts = skills.map((skill) => ({
    name: skill.name,
    selected: skill.path,
    ignored: discovered
      .filter((candidate) => candidate.name === skill.name && candidate.path !== skill.path)
      .sort((a, b) => a.priority - b.priority)
      .map((candidate) => candidate.path),
  })).filter((conflict) => conflict.ignored.length > 0);
  return {
    version: 1,
    skills: skills.map(({ priority, warnings: _warnings, ...skill }) => skill),
    conflicts,
    warnings,
  };
}

function discoverProjectSkills() {
  if (samePath(PROJECT, KIT_ROOT)) {
    return [];
  }
  return discoverSkills(path.join(PROJECT, "skills"), "project", PROFILE_MANAGED_HOST_ONLY_SKILLS);
}

function discoverSkills(baseDir, source, ignoredNames = new Set(), allowedNames = null) {
  if (!fs.existsSync(baseDir)) return [];
  const priority = { local: 0, project: 1, bundled: 2 }[source] ?? 99;
  const skills = [];
  for (const entry of fs.readdirSync(baseDir, { withFileTypes: true })) {
    if (!entry.isDirectory() || ignoredNames.has(entry.name)) continue;
    if (allowedNames && !allowedNames.has(entry.name)) continue;
    const skillPath = path.join(baseDir, entry.name, "SKILL.md");
    if (!fs.existsSync(skillPath)) continue;
    const text = fs.readFileSync(skillPath, "utf8");
    const metadata = parseSkillMetadata(text, entry.name);
    if (ignoredNames.has(metadata.name)) continue;
    const relativePath = path.relative(PROJECT, skillPath);
    skills.push({
      name: metadata.name,
      path: relativePath,
      source,
      hosts: metadata.hosts,
      tags: metadata.tags,
      description: metadata.description,
      trigger: metadata.trigger,
      hash: crypto.createHash("sha256").update(text).digest("hex"),
      priority,
      warnings: metadata.warnings.map((message) => `${relativePath}: ${message}`),
    });
  }
  return skills;
}

function parseSkillMetadata(text, fallbackName) {
  const frontmatter = splitSkillFrontmatter(text);
  const metadata = frontmatter ? parseSimpleYaml(frontmatter) : {};
  const warnings = [];
  const parsedName = String(metadata.name || fallbackName);
  const name = safeSkillName(parsedName);
  if (name !== parsedName) warnings.push(`unsafe skill name ignored: ${parsedName}`);
  const hostValues = Array.isArray(metadata.hosts) ? metadata.hosts : [];
  const knownHosts = new Set(["claude", "codex", "gemini"]);
  const hosts = [];
  for (const host of hostValues) {
    const normalized = String(host).trim().toLowerCase();
    if (knownHosts.has(normalized)) hosts.push(normalized);
    else if (normalized) warnings.push(`unknown host ignored: ${normalized}`);
  }
  const body = text.replace(/^---\n[\s\S]*?\n---\n?/, "");
  const useWhen = body.split(/\r?\n/).find((line) => /^\s*use when\b/i.test(line));
  return {
    name,
    description: String(metadata.description || useWhen || ""),
    hosts: hostValues.length > 0 ? [...new Set(hosts)] : ["claude", "codex", "gemini"],
    tags: Array.isArray(metadata.tags) ? metadata.tags.map(String) : [],
    trigger: String(metadata.trigger || metadata.description || useWhen || ""),
    warnings,
  };
}

function safeSkillName(value) {
  const candidate = String(value).trim();
  return /^[A-Za-z0-9._-]+$/.test(candidate) && !candidate.startsWith(".") && !candidate.includes("..") && candidate !== "."
    ? candidate
    : String(candidate || "skill")
        .toLowerCase()
        .replace(/[^a-z0-9_-]+/g, "-")
        .replace(/^-+|-+$/g, "") || "skill";
}

function removeStaleProjectSkillLinks(skills, previousIndex) {
  if (!previousIndex || !Array.isArray(previousIndex.links)) return [];
  const desired = new Set(skills.flatMap((skill) => skill.hosts.map((host) => `${host}:${skill.name}`)));
  const removed = [];
  for (const link of previousIndex.links) {
    if (!link || !link.name || !link.host || !link.path) continue;
    if (desired.has(`${link.host}:${link.name}`)) continue;
    const target = path.join(PROJECT, link.path);
    const hostRoot = path.join(PROJECT, `.${link.host}`, "skills");
    if (pathHasSymlink(PROJECT, hostRoot)) {
      removed.push({ name: link.name, host: link.host, path: link.path, status: "skipped-host-root-symlink" });
      continue;
    }
    ensureChildPath(hostRoot, target);
    const stat = lstatIfExists(target);
    if (!stat) continue;
    if (stat.isSymbolicLink()) {
      fs.unlinkSync(target);
      removed.push({ name: link.name, host: link.host, path: link.path, status: "removed-stale" });
      continue;
    }
    const previousHash = previousSkillHash(previousIndex, link.name);
    const skillFile = path.join(target, "SKILL.md");
    if (stat.isDirectory() && previousHash && fs.existsSync(skillFile)) {
      const currentHash = crypto.createHash("sha256").update(fs.readFileSync(skillFile, "utf8")).digest("hex");
      if (currentHash === previousHash) {
        fs.rmSync(target, { recursive: true, force: true });
        removed.push({ name: link.name, host: link.host, path: link.path, status: "removed-stale-copied" });
      }
    }
  }
  return removed;
}

function lstatIfExists(pathName) {
  try {
    return fs.lstatSync(pathName);
  } catch (error) {
    if (error && error.code === "ENOENT") return null;
    throw error;
  }
}

function splitSkillFrontmatter(text) {
  if (!text.startsWith("---\n")) return null;
  const end = text.indexOf("\n---\n", 4);
  return end === -1 ? null : text.slice(4, end);
}

function parseSimpleYaml(text) {
  const metadata = {};
  let listKey = null;
  for (const line of text.split(/\r?\n/)) {
    const listItem = line.match(/^\s+-\s*(.+)$/);
    if (listItem && listKey) {
      metadata[listKey].push(listItem[1].trim().replace(/^['"]|['"]$/g, ""));
      continue;
    }
    const match = line.match(/^([A-Za-z0-9_-]+):\s*(.*)$/);
    if (!match) {
      listKey = null;
      continue;
    }
    const raw = match[2].trim();
    const key = match[1];
    if (raw.startsWith("[") && raw.endsWith("]")) {
      metadata[key] = raw.slice(1, -1).split(",").map((item) => item.trim().replace(/^['"]|['"]$/g, "")).filter(Boolean);
      listKey = null;
    } else if (raw === "") {
      metadata[key] = [];
      listKey = key;
    } else {
      metadata[key] = raw.replace(/^['"]|['"]$/g, "");
      listKey = null;
    }
  }
  return metadata;
}

function linkProjectSkill(skill, host, previousIndex) {
  const srcDir = path.dirname(path.join(PROJECT, skill.path));
  const hostRoot = path.join(PROJECT, `.${host}`, "skills");
  if (pathHasSymlink(PROJECT, hostRoot)) {
    return { name: skill.name, host, path: path.relative(PROJECT, hostRoot), status: "skipped-host-root-symlink" };
  }
  const destDir = path.join(hostRoot, skill.name);
  ensureChildPath(hostRoot, destDir);
  const destSkill = path.join(destDir, "SKILL.md");
  const previousHash = previousSkillHash(previousIndex, skill.name);
  if (fs.existsSync(destDir)) {
    const stat = fs.lstatSync(destDir);
    if (stat.isSymbolicLink()) fs.unlinkSync(destDir);
    else if (fs.existsSync(destSkill)) {
      const currentHash = crypto.createHash("sha256").update(fs.readFileSync(destSkill, "utf8")).digest("hex");
      if (currentHash !== skill.hash && currentHash !== previousHash) {
        return { name: skill.name, host, path: path.relative(PROJECT, destDir), status: "skipped-user-modified" };
      }
      fs.rmSync(destDir, { recursive: true, force: true });
    } else {
      return { name: skill.name, host, path: path.relative(PROJECT, destDir), status: "skipped-existing" };
    }
  }
  return { name: skill.name, host, path: path.relative(PROJECT, destDir), status: linkOrCopyDir(srcDir, destDir) };
}

function previousSkillHash(previousIndex, name) {
  if (!previousIndex || !Array.isArray(previousIndex.skills)) return "";
  return previousIndex.skills.find((skill) => skill && skill.name === name)?.hash || "";
}

function readJsonIfExists(pathName) {
  if (!fs.existsSync(pathName)) return null;
  try {
    return JSON.parse(fs.readFileSync(pathName, "utf8"));
  } catch {
    return null;
  }
}

function ensureChildPath(parent, child) {
  const parentResolved = path.resolve(parent);
  const childResolved = path.resolve(child);
  const relative = path.relative(parentResolved, childResolved);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`path escapes parent: ${child}`);
  }
}

function pathHasSymlink(root, target) {
  const relative = path.relative(root, target);
  const parts = relative.split(path.sep).filter(Boolean);
  let cursor = root;
  for (const part of parts) {
    cursor = path.join(cursor, part);
    const stat = lstatIfExists(cursor);
    if (stat && stat.isSymbolicLink()) return true;
  }
  return false;
}

function install() {
  const managedRoot = resolveManagedWorktreeRoot(REQUESTED_PROJECT);
  if (managedRoot) {
    if (fs.existsSync(path.join(managedRoot, ".agent-flow", "kit.json"))) {
      console.log(`agent-flow already installed root=${managedRoot}`);
      console.log("worktree install skipped; reinstall from the leader checkout if needed");
      return;
    }
    console.error("managed worktree install blocked; install from the leader checkout first");
    process.exitCode = 1;
    return;
  }
  ensureDir(path.join(AF_DIR, "runs"));
  ensureDir(path.join(AF_DIR, "memory"));
  ensureDir(path.join(AF_DIR, "memory", "lore"));
  ensureDir(path.join(AF_DIR, "memory", "lore", "archive"));
  ensureDir(path.join(AF_DIR, "local-skills"));

  const profile = detectProfile();

  bootstrapMarkdown("CLAUDE.md");
  bootstrapMarkdown("AGENTS.md");
  bootstrapMarkdown("GEMINI.md");
  const gitignorePath = path.join(PROJECT, ".gitignore");
  upsertGitignore(gitignorePath, [
    ".agent-flow/",
    ".agent-flow/local-skills/",
    ".codex/",
    ".gemini/",
    ".claude/",
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    "AGENTS/",
    "CLAUDE/",
    "GEMINI/",
    "scripts/check-context-docs.*",
    "agent-flow/",
  ]);
  removeGitignoreEntries(gitignorePath, [
    "graphify/",
    "graphify-out/manifest.json",
    "graphify-out/cost.json",
  ]);
  removeLegacyProjectSkillCopies(PROJECT, "graphify");

  // Copy bundled skills into project-local skills dir.
  // Host-AI-specific skill paths (`.claude/skills/`, `.codex/skills/`) are
  // populated by symlinking from .agent-flow/skills/ where possible, so
  // updates to the kit propagate without re-installing.
  const skillsCopied = copyDir(
    path.join(KIT_ROOT, "skills"),
    path.join(AF_DIR, "skills"),
    PROFILE_MANAGED_HOST_ONLY_SKILLS,
  );
  const skillIndex = installProjectSkills();
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
  const codexAgentsCopied = copyDir(
    path.join(KIT_ROOT, ".Codex", "agents"),
    path.join(PROJECT, ".Codex", "agents"),
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
    codex_agents_copied: codexAgentsCopied,
    context_tree_copied: contextTreeCopied,
    skill_links: {
      claude: claudeSkillStatus,
      codex: codexSkillStatus,
      gemini: "GEMINI.md",
    },
    skill_index: {
      path: ".agent-flow/skills/index.json",
      skills: skillIndex.skills.length,
      conflicts: skillIndex.conflicts.length,
      warnings: skillIndex.warnings.length,
    },
  };
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
  console.log(``);
  console.log(`Next: /agent-flow <task>`);
  console.log(`      (or: agent-flow run "<task>")`);
  console.log(`(If 'agent-flow' isn't on PATH yet: pip install -e ${KIT_ROOT})`);
}

function upsertGitignore(pathName, entries) {
  const current = fs.existsSync(pathName) ? fs.readFileSync(pathName, "utf8") : "";
  const lines = current.split(/\r?\n/);
  const existing = new Set(lines.map((line) => line.trim()));
  const missing = entries.filter((entry) => !isGitignoreEntryCovered(entry, existing));
  if (missing.length === 0) {
    return;
  }
  const prefix = current.trimEnd();
  const next = `${prefix}${prefix ? "\n" : ""}${missing.join("\n")}\n`;
  fs.writeFileSync(pathName, next, "utf8");
}

function removeGitignoreEntries(pathName, entries) {
  if (!fs.existsSync(pathName)) return;
  const removals = new Set(entries);
  const current = fs.readFileSync(pathName, "utf8");
  const lines = current.split(/\r?\n/);
  const filtered = lines.filter((line) => !removals.has(line.trim()));
  if (filtered.length === lines.length) return;
  const next = `${filtered.join("\n").replace(/\n*$/, "")}\n`;
  fs.writeFileSync(pathName, next, "utf8");
}

function removeLegacyProjectSkillCopies(projectRoot, skillName) {
  for (const parent of [
    path.join(projectRoot, ".agent-flow", "skills"),
    path.join(projectRoot, ".claude", "skills"),
    path.join(projectRoot, ".codex", "skills"),
    path.join(projectRoot, ".Codex", "skills"),
    path.join(projectRoot, ".gemini", "skills"),
    path.join(projectRoot, ".gemini", "antigravity", "skills"),
  ]) {
    fs.rmSync(path.join(parent, skillName), { recursive: true, force: true });
  }
}

function isGitignoreEntryCovered(entry, existing) {
  if (existing.has(entry)) {
    return true;
  }
  const normalized = entry.replace(/^\/+/, "");
  const parts = normalized.split("/");
  for (let index = 1; index < parts.length; index += 1) {
    const parent = `${parts.slice(0, index).join("/")}/`;
    const parentWithoutSlash = parent.replace(/\/$/, "");
    if (
      existing.has(parent) ||
      existing.has(parentWithoutSlash) ||
      existing.has(`/${parent}`) ||
      existing.has(`/${parentWithoutSlash}`)
    ) {
      return true;
    }
  }
  return false;
}

const cmd = process.argv[2];
if (cmd === "install") {
  install();
} else if (cmd === "--help" || cmd === "-h" || !cmd) {
  console.log("Usage: npx <agent-flow-package> install");
  process.exit(0);
} else {
  console.error(`Unknown command: ${cmd}`);
  process.exit(1);
}
